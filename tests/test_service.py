from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service import PlannerService, ServiceError


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service = PlannerService(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def setup_household(self, income: str = "30000") -> None:
        self.service.update_profile(
            {"monthly_income": income, "currency": "CNY", "local_tz": "+08:00", "bank_tz": "UTC"}
        )
        self.service.add_fixed_expense({"name": "房贷", "amount": "10000", "day_of_month": 1})
        self.service.add_fixed_expense({"name": "信用卡分期", "amount": "3000", "day_of_month": 15})
        self.service.add_fixed_expense({"name": "生活费", "amount": "2000", "day_of_month": 1})

    def create_emergency_goal(self) -> int:
        result = self.service.create_goal(
            {
                "goal_type": "emergency_reserve",
                "name": "应急储备",
                "target_amount": "60000",
                "deadline": "2027-03-31",
                "as_of": "2026-09-18",
            }
        )
        return result["goal"]["id"]


class GoalAndVersionTest(ServiceTestBase):
    def test_create_goal_schedules_and_versions(self) -> None:
        self.setup_household()
        result = self.service.create_goal(
            {
                "goal_type": "race_fee",
                "name": "铁人三项报名",
                "target_amount": "4000",
                "deadline": "2026-12-31",
                "as_of": "2026-09-18",
            }
        )
        self.assertEqual(1, result["plan_version"])
        goal = result["goal"]
        self.assertEqual("4000", goal["target"])
        self.assertEqual("0", goal["funded"])
        self.assertEqual("on_track", goal["eta_status"])
        deductions = self.service.list_deductions(goal_id=goal["id"])
        self.assertEqual(4, len(deductions))
        self.assertTrue(all(d["status"] == "pending" for d in deductions))
        self.assertEqual("1000", deductions[0]["amount"])
        versions = self.service.list_versions()
        self.assertEqual(1, len(versions))
        self.assertIn("铁人三项报名", versions[0]["reason"])

    def test_invalid_goal_type_rejected(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.create_goal(
                {"goal_type": "yacht", "name": "x", "target_amount": "1", "deadline": "2027-01-01"}
            )
        self.assertEqual(400, ctx.exception.status)

    def test_profile_update_replans_when_goals_exist(self) -> None:
        self.setup_household()
        self.create_emergency_goal()
        before = len(self.service.list_versions())
        self.service.update_profile({"transfer_time": "20:30"})
        self.assertEqual(before + 1, len(self.service.list_versions()))


class IncomeShockFlowTest(ServiceTestBase):
    """收入减少 → 按优先级暂停；恢复 → 自动解除；已执行转账不被回算。"""

    def test_full_flow(self) -> None:
        self.setup_household()
        emergency_id = self.create_emergency_goal()
        travel = self.service.create_goal(
            {
                "goal_type": "travel",
                "name": "比赛旅行",
                "target_amount": "14000",
                "deadline": "2027-03-31",
                "as_of": "2026-09-18",
            }
        )
        travel_id = travel["goal"]["id"]

        # 先执行一笔应急金扣款（模拟已发生的银行转账）
        first = self.service.list_deductions(goal_id=emergency_id, status="pending")[0]
        executed = self.service.execute_deduction(
            first["id"], {"amount": "8571.428571", "posted_at": "2026-09-30T13:00:00+00:00"}
        )
        self.assertEqual("executed", executed["deduction"]["status"])

        # 收入从 30000 降到 24000：可支配 9000，应急金优先，旅行被暂停
        event = self.service.record_event(
            {
                "event_type": "income_change",
                "payload": {"new_monthly_income": "24000"},
                "occurred_on": "2026-10-05",
                "reason": "公司降薪，月收入降至 24000",
            }
        )
        self.assertEqual("公司降薪，月收入降至 24000", event["reason"])
        travel_funding = self.service.goal_funding(travel_id)
        self.assertTrue(travel_funding["paused"])
        condition = travel_funding["paused"][0]["recovery_condition"]
        self.assertEqual("insufficient_funds", condition["type"])
        self.assertEqual("automatic", condition["resume"])
        emergency_funding = self.service.goal_funding(emergency_id)
        self.assertFalse(emergency_funding["paused"])
        self.assertTrue(emergency_funding["sources"]["pending_deductions"])

        # 已执行的扣款不被重排覆盖
        after = [d for d in self.service.list_deductions(goal_id=emergency_id) if d["id"] == first["id"]][0]
        self.assertEqual("executed", after["status"])
        self.assertEqual("8571.428571", after["executed_amount"])
        self.assertEqual("2026-09-30T13:00:00+00:00", after["executed_at"])

        # 版本记录了触发原因、受影响目标与新的预计完成日
        version = self.service.get_version(event["plan_version"])
        self.assertEqual("公司降薪，月收入降至 24000", version["reason"])
        self.assertIn(travel_id, version["changes"]["affected_goals"])
        self.assertEqual("income_change", version["changes"]["trigger"]["event_type"])
        self.assertTrue(version["changes"]["goal_updates"])

        # 收入恢复后，暂停的扣款自动恢复
        self.service.record_event(
            {
                "event_type": "income_change",
                "payload": {"new_monthly_income": "30000"},
                "occurred_on": "2026-11-05",
            }
        )
        self.assertFalse(self.service.goal_funding(travel_id)["paused"])
        resumed_version = self.service.get_version(len(self.service.list_versions()))
        self.assertTrue(resumed_version["changes"]["resumed"])


class EmergencyEventTest(ServiceTestBase):
    def test_bumps_existing_reserve(self) -> None:
        self.setup_household()
        goal_id = self.create_emergency_goal()
        event = self.service.record_event(
            {
                "event_type": "emergency",
                "payload": {"amount": "8000", "description": "门诊医疗"},
                "occurred_on": "2026-10-02",
            }
        )
        self.assertEqual("target", event["applied"][0]["field"])
        goal = self.service.get_goal(goal_id)
        self.assertEqual("68000", goal["target"])

    def test_creates_reserve_when_missing(self) -> None:
        self.setup_household()
        event = self.service.record_event(
            {
                "event_type": "emergency",
                "payload": {"amount": "5000", "description": "急诊"},
                "occurred_on": "2026-10-02",
            }
        )
        self.assertEqual("created", event["applied"][0]["field"])
        goals = [g for g in self.service.list_goals() if g["goal_type"] == "emergency_reserve"]
        self.assertEqual(1, len(goals))
        self.assertEqual(1, goals[0]["priority"])
        self.assertEqual("5000", goals[0]["target"])
        self.assertEqual("2027-04-30", goals[0]["deadline"])  # 默认 6 个月补足期


class RacePostponedTest(ServiceTestBase):
    def test_postpone_extends_deadline_and_lowers_installments(self) -> None:
        self.setup_household()
        race = self.service.create_goal(
            {
                "goal_type": "race_fee",
                "name": "铁人三项报名",
                "target_amount": "4000",
                "deadline": "2026-12-31",
                "as_of": "2026-09-18",
            }
        )
        race_id = race["goal"]["id"]
        before = [d["amount"] for d in self.service.list_deductions(goal_id=race_id)]
        self.assertEqual(["1000"] * 4, before)

        event = self.service.record_event(
            {
                "event_type": "race_postponed",
                "payload": {"goal_id": race_id, "new_deadline": "2027-03-31"},
                "occurred_on": "2026-10-01",
                "reason": "赛事官方延期至 2027 年 3 月",
            }
        )
        self.assertEqual("2027-03-31", self.service.get_goal(race_id)["deadline"])
        after = self.service.list_deductions(goal_id=race_id, status="pending")
        self.assertEqual(6, len(after))  # 2026-10 .. 2027-03
        self.assertEqual("666.666666", after[0]["amount"])
        self.assertEqual("666.66667", after[-1]["amount"])  # 整除余数并入最后一期
        version = self.service.get_version(event["plan_version"])
        self.assertEqual("赛事官方延期至 2027 年 3 月", version["reason"])
        self.assertIn(race_id, version["changes"]["affected_goals"])


class BankImportTest(ServiceTestBase):
    def test_import_is_idempotent(self) -> None:
        self.setup_household()
        goal_id = self.create_emergency_goal()
        deduction = self.service.list_deductions(goal_id=goal_id, status="pending")[0]
        payload = {
            "account": "main",
            "transactions": [
                {
                    "external_id": "tx-20260930-01",
                    "amount": deduction["amount"],
                    "posted_at": "2026-09-30T13:00:00+00:00",
                    "value_date": "2026-09-30",
                    "deduction_id": deduction["id"],
                }
            ],
        }
        first = self.service.import_bank_transactions(payload)
        self.assertEqual(1, first["imported"])
        self.assertEqual(0, first["duplicates"])
        self.assertEqual(deduction["id"], first["items"][0]["matched_deduction"])

        second = self.service.import_bank_transactions(payload)
        self.assertEqual(0, second["imported"])
        self.assertEqual(1, second["duplicates"])
        self.assertEqual(first["batch"], second["batch"])

        # 重复导入不会重复入账
        funding = self.service.goal_funding(goal_id)
        self.assertEqual(deduction["amount"], funding["funded"])
        self.assertEqual(1, len(funding["sources"]["executed_deductions"]))

    def test_direct_transaction_counts_as_funding(self) -> None:
        self.setup_household()
        goal_id = self.create_emergency_goal()
        self.service.import_bank_transactions(
            {
                "account": "main",
                "transactions": [
                    {
                        "external_id": "tx-gift-01",
                        "amount": "2000",
                        "posted_at": "2026-09-20T02:00:00+00:00",
                        "goal_id": goal_id,
                        "memo": "家人赞助",
                    }
                ],
            }
        )
        funding = self.service.goal_funding(goal_id)
        self.assertEqual("2000", funding["funded"])
        self.assertEqual(1, len(funding["sources"]["direct_transactions"]))

    def test_execute_twice_rejected(self) -> None:
        self.setup_household()
        goal_id = self.create_emergency_goal()
        deduction = self.service.list_deductions(goal_id=goal_id, status="pending")[0]
        self.service.execute_deduction(deduction["id"], {})
        with self.assertRaises(ServiceError) as ctx:
            self.service.execute_deduction(deduction["id"], {})
        self.assertEqual(409, ctx.exception.status)


class ValueDateServiceTest(ServiceTestBase):
    def test_bank_timezone_shifts_value_date(self) -> None:
        self.service.update_profile(
            {
                "monthly_income": "30000",
                "currency": "CNY",
                "local_tz": "+08:00",
                "bank_tz": "+12:00",
                "transfer_time": "21:00",
            }
        )
        self.service.create_goal(
            {
                "goal_type": "equipment",
                "name": "铁三车",
                "target_amount": "12000",
                "deadline": "2026-12-31",
                "as_of": "2026-09-18",
            }
        )
        deductions = self.service.list_deductions()
        self.assertEqual("2026-09-30", deductions[0]["scheduled_date"])
        self.assertEqual("2026-10-01", deductions[0]["value_date"])


class ExportTest(ServiceTestBase):
    def test_versioned_export_is_stable(self) -> None:
        self.setup_household()
        self.create_emergency_goal()
        export_v1 = self.service.export_version(1)
        self.assertEqual(1, export_v1["plan_version"])
        self.assertEqual("family_training_savings_plan", export_v1["export_kind"])
        self.assertTrue(export_v1["checksum"])
        self.assertEqual(1, len(export_v1["plan"]["goals"]))

        # 后续事件产生新版本，但旧版本导出内容不变
        self.service.record_event(
            {
                "event_type": "income_change",
                "payload": {"new_monthly_income": "24000"},
                "occurred_on": "2026-10-05",
            }
        )
        export_v1_again = self.service.export_version(1)
        self.assertEqual(export_v1["checksum"], export_v1_again["checksum"])
        self.assertEqual(export_v1["plan"], export_v1_again["plan"])

        export_v2 = self.service.export_version(2)
        self.assertNotEqual(export_v1["checksum"], export_v2["checksum"])
        self.assertEqual("income_change", export_v2["trigger"]["event_type"])
        self.assertEqual("24000", export_v2["plan"]["profile"]["monthly_income"])

    def test_export_missing_version_404(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.export_version(42)
        self.assertEqual(404, ctx.exception.status)


class ResumeAndCancelTest(ServiceTestBase):
    def test_manual_resume_creates_version(self) -> None:
        self.setup_household(income="16000")  # 容量 1000，目标必然被暂停
        goal_id = self.create_emergency_goal()
        paused = self.service.list_deductions(goal_id=goal_id, status="paused")
        self.assertTrue(paused)
        result = self.service.resume_deduction(paused[0]["id"])
        self.assertEqual("pending", result["deduction"]["status"])
        version = self.service.get_version(result["plan_version"])
        self.assertTrue(version["changes"]["manual_resume"])

    def test_cancel_goal_cancels_pending_deductions(self) -> None:
        self.setup_household()
        goal_id = self.create_emergency_goal()
        self.service.record_event(
            {
                "event_type": "goal_change",
                "payload": {"goal_id": goal_id, "status": "cancelled"},
                "occurred_on": "2026-10-01",
            }
        )
        self.assertEqual("cancelled", self.service.get_goal(goal_id)["status"])
        self.assertEqual([], self.service.list_deductions(goal_id=goal_id, status="pending"))


class TimelineTest(ServiceTestBase):
    def test_timeline_merges_all_kinds(self) -> None:
        from datetime import date

        self.setup_household()
        goal_id = self.create_emergency_goal()
        self.service.add_training_block(
            {"name": "基础期", "start_date": "2026-09-01", "end_date": "2026-11-30", "goal_ids": [goal_id]}
        )
        items = self.service.timeline(date(2026, 9, 1), date(2027, 4, 30))
        kinds = {item["kind"] for item in items}
        self.assertIn("income", kinds)
        self.assertIn("expense", kinds)
        self.assertIn("scheduled_transfer", kinds)
        self.assertIn("training_block", kinds)
        self.assertIn("goal_deadline", kinds)
        dates = [item["date"] for item in items]
        self.assertEqual(sorted(dates), dates)


if __name__ == "__main__":
    unittest.main()

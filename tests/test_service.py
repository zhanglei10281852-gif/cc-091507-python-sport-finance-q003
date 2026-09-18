from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from service import ApiError, Service
from store import Store


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="planner-test-")
        self.s = Service(Store(self.tmp))
        self.s.configure({
            "plan_start_month": "2026-09",
            "plan_end_month": "2027-06",
            "as_of_date": "2026-09-18",
            "transfer_day": 5,
        })
        self.race = self.s.create_race(
            {"name": "测试赛", "race_date": "2027-03-20", "tz": "Asia/Shanghai"})["race"]["id"]
        self.g_emergency = self.s.create_goal({
            "name": "应急金", "type": "emergency_reserve",
            "target_amount": "30000", "deadline": "2027-02-28", "account": "c",
        })["goal"]["id"]
        self.g_race = self.s.create_goal({
            "name": "报名费", "type": "race_fee",
            "target_amount": "1200", "deadline": "2026-12-31",
            "account": "c", "race_id": self.race,
        })["goal"]["id"]
        self.g_equip = self.s.create_goal({
            "name": "装备", "type": "equipment",
            "target_amount": "18000", "deadline": "2027-03-10", "account": "c",
        })["goal"]["id"]
        self.s.upsert_income({"month": "2026-09", "account": "c", "amount": "9000"})
        self.s.create_fixed_expense({
            "name": "固定支出", "category": "housing",
            "account": "c", "amount": "5000", "start_month": "2026-09",
        })
        self.s.trigger_replan({"trigger_type": "baseline", "reason": "基线"})

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def statuses(self, month: str | None = None) -> dict[str, tuple[str, str]]:
        out = {}
        for d in self.s.list_deductions()["deductions"]:
            if month and d["month"] != month:
                continue
            out[d["goal_name"]] = (d["status"], d["amount"])
        return out

    # ------------------------------------------------------------ priority
    def test_priority_emergency_funded_first(self) -> None:
        # 月可支配 4000 全部进应急金；报名费与装备当月暂停
        self.assertEqual("scheduled", self.statuses("2026-09")["应急金"][0])
        self.assertEqual("4000.000000", self.statuses("2026-09")["应急金"][1])
        self.assertEqual("paused", self.statuses("2026-09")["报名费"][0])
        self.assertEqual("paused", self.statuses("2026-09")["装备"][0])
        paused = [d for d in self.s.list_deductions(status_filter="paused")["deductions"]
                  if d["month"] == "2026-09" and d["goal_name"] == "装备"][0]
        self.assertEqual("cash_available", paused["resume"]["condition"]["type"])
        self.assertFalse(paused["resume"]["currently_met"])

    # ------------------------------------------------------------ immutable
    def test_executed_transfers_never_recomputed(self) -> None:
        before = [d for d in self.s.list_deductions()["deductions"]
                  if d["month"] == "2026-10" and d["goal_name"] == "应急金"][0]
        # 银行流水：10 月实际向应急金转入 1 元（与计划完全不同）
        self.s.bank_import({"entries": [{
            "account": "c", "goal_id": self.g_emergency,
            "amount": "1", "business_time": "2026-10-05T10:00:00",
            "external_id": "TX-E-1",
        }]})
        matched = [d for d in self.s.list_deductions()["deductions"] if d["id"] == before["id"]][0]
        self.assertEqual("executed", matched["status"])
        self.assertEqual("4000.000000", matched["amount"])  # 计划额保留，实际额以账本为准
        ledger = [e for e in self.s.store.state["ledger"] if e["external_id"] == "TX-E-1"]
        self.assertEqual(1, len(ledger))
        self.assertEqual(1_000_000, ledger[0]["amount"])  # "1" 元 = 10^6 最小单位
        self.assertEqual(before["id"], ledger[0]["deduction_id"])
        # 无论怎么重排，该实例保持 executed
        self.s.upsert_income({"month": "2026-11", "account": "c", "amount": "1000"},
                             trigger=True, reason="再降薪")
        again = [d for d in self.s.list_deductions()["deductions"] if d["id"] == before["id"]][0]
        self.assertEqual("executed", again["status"])

    # ------------------------------------------------------------ idempotency
    def test_bank_import_idempotent(self) -> None:
        payload = {"batch_id": "B", "entries": [
            {"account": "c", "goal_id": self.g_race, "amount": "300",
             "business_time": "2026-10-06T10:00:00", "external_id": "R-1"},
            {"account": "c", "goal_id": self.g_race, "amount": "300",
             "business_time": "2026-10-06T10:00:00", "external_id": "R-1"},
        ]}
        first = self.s.bank_import(payload)
        self.assertEqual(1, first["imported"])
        self.assertEqual(1, first["duplicates"])
        second = self.s.bank_import({"batch_id": "B2", "entries": [payload["entries"][0]]})
        self.assertEqual(0, second["imported"])
        self.assertEqual(1, second["duplicates"])
        self.assertIsNone(second["replan"])  # 全重复不产生新版本
        self.assertEqual(1, len(self.s.store.state["ledger"]))

    def test_bank_import_content_hash_without_external_id(self) -> None:
        row = {"account": "c", "goal_id": self.g_equip, "amount": "500",
               "business_time": "2026-10-07T10:00:00", "description": "车店"}
        self.assertEqual(1, self.s.bank_import({"entries": [dict(row)]})["imported"])
        again = self.s.bank_import({"entries": [dict(row)]})
        self.assertEqual(0, again["imported"])
        self.assertEqual(1, again["duplicates"])

    # ------------------------------------------------------ value date rules
    def test_value_date_drives_posting_month(self) -> None:
        out = self.s.add_one_off_expense({
            "name": "急诊", "category": "medical", "account": "c",
            "amount": "3500", "business_time": "2026-09-30T23:30:00",
            "tz": "Asia/Hong_Kong",
        })
        self.assertEqual("2026-10-01", out["value_date"])
        self.assertEqual("2026-10", out["posting_month"])
        # 医疗支出进入 10 月瀑布：4000 - 3500 = 500 给应急金
        self.assertEqual("500.000000", self.statuses("2026-10")["应急金"][1])

    # --------------------------------------------------------- income shock
    def test_income_drop_pauses_and_recovery_resumes(self) -> None:
        self.s.upsert_income({"month": "2026-10", "account": "c", "amount": "5000"},
                             trigger=True, reason="绩效取消")
        # 10 月可支配为 0，应急金被暂停
        self.assertEqual("paused", self.statuses("2026-10")["应急金"][0])
        equip_paused = [d for d in self.s.list_deductions(status_filter="paused")["deductions"]
                        if d["month"] == "2026-10" and d["goal_name"] == "装备"][0]
        self.assertIn("更高优先级", equip_paused["pause_reason"])
        self.assertFalse(equip_paused["resume"]["currently_met"])

        # 11 月收入恢复到 12000：可支配 7000，应急金恢复，报名费也能排上
        self.s.upsert_income({"month": "2026-11", "account": "c", "amount": "12000"},
                             trigger=True, reason="奖金到账")
        self.assertEqual("scheduled", self.statuses("2026-11")["应急金"][0])
        self.assertEqual("scheduled", self.statuses("2026-11")["报名费"][0])

    # ------------------------------------------------------- race postponed
    def test_race_postponement_updates_deadline_and_eta(self) -> None:
        result = self.s.postpone_race(self.race, {"new_date": "2027-05-15"})
        self.assertEqual([self.g_race], result["linked_goals"])
        goal = self.s.goal_detail(self.g_race)["goal"]
        self.assertEqual("2027-05-15", goal["deadline"])
        adj = self.s.list_adjustments()["adjustments"][0]
        self.assertEqual("race_postponed", adj["trigger_type"])
        self.assertIn("2027-05-15", adj["reason"])

    # ------------------------------------------------------------- audit log
    def test_adjustment_records_reason_goals_and_eta(self) -> None:
        self.s.upsert_income({"month": "2026-11", "account": "c", "amount": "12000"},
                             trigger=True, reason="家庭收入增加")
        adj = self.s.list_adjustments()["adjustments"][0]
        self.assertEqual("income_change", adj["trigger_type"])
        self.assertEqual("家庭收入增加", adj["reason"])
        self.assertEqual(adj["plan_version_before"] + 1, adj["plan_version_after"])
        self.assertTrue(adj["affected_goals"])
        for item in adj["affected_goals"]:
            self.assertIn("goal_id", item)
            self.assertIn("old_eta", item)
            self.assertIn("new_eta", item)

    # --------------------------------------------------- manual pause/resume
    def test_manual_pause_blocks_auto_resume_and_pin_holds(self) -> None:
        ded = [d for d in self.s.list_deductions()["deductions"]
               if d["month"] == "2026-11" and d["goal_name"] == "应急金"][0]
        self.s.pause_deduction(ded["id"], {"reason": "用户临时叫停",
                                           "resume_detail": "等确认奖金"})
        # 即使收入大增触发重排，手动暂停不自动恢复
        self.s.upsert_income({"month": "2026-10", "account": "c", "amount": "30000"},
                             trigger=True, reason="大额收入")
        again = [d for d in self.s.list_deductions()["deductions"] if d["id"] == ded["id"]][0]
        self.assertEqual("paused", again["status"])
        self.assertEqual("manual", again["resume"]["condition"]["type"])

        self.s.resume_deduction(ded["id"], {"pin": True})
        # pin 后再重排，该实例金额与状态不被瀑布改动
        self.s.upsert_income({"month": "2026-12", "account": "c", "amount": "5000"},
                             trigger=True, reason="再次降薪")
        pinned = [d for d in self.s.list_deductions()["deductions"] if d["id"] == ded["id"]][0]
        self.assertEqual("scheduled", pinned["status"])
        self.assertTrue(pinned["pinned"])

    # ---------------------------------------------------------------- query
    def test_goal_funding_query(self) -> None:
        self.s.bank_import({"entries": [{
            "account": "c", "goal_id": self.g_race, "amount": "300",
            "business_time": "2026-10-05T10:00:00", "external_id": "R-9",
        }]})
        info = self.s.goal_funding(self.g_race)
        self.assertEqual("300.000000", info["funding"]["executed_total"]["amount"])
        sources = info["funding"]["sources_executed"]
        self.assertEqual("R-9", sources[0]["external_id"])
        self.assertEqual("c", sources[0]["account"])
        self.assertIn("scheduled_total", info["funding"])
        self.assertTrue("paused_deductions")

    # -------------------------------------------------------------- exports
    def test_versioned_exports_are_immutable(self) -> None:
        v1 = self.s.export_snapshot(1)
        self.assertEqual("family-training-plan/v1", v1["schema"])
        first_ledger_len = len(v1["ledger"])
        path = Path(self.tmp) / "exports" / "plan_v1.json"
        self.assertTrue(path.exists())
        # 后续变更不影响历史快照
        self.s.upsert_income({"month": "2026-11", "account": "c", "amount": "12000"},
                             trigger=True, reason="变化")
        again = self.s.export_snapshot(1)
        self.assertEqual(first_ledger_len, len(again["ledger"]))
        latest = self.s.list_exports()["exports"][0]
        self.assertRegex(latest["sha256"], r"^[0-9a-f]{64}$")

    # -------------------------------------------------------------- timeline
    def test_timeline_merges_all_event_types(self) -> None:
        self.s.create_training_phase({
            "name": "基础期", "start_date": "2026-09-01", "end_date": "2026-11-30"})
        items = self.s.timeline()["timeline"]
        types = {i["type"] for i in items}
        self.assertIn("training_phase_start", types)
        self.assertIn("race", types)
        self.assertIn("income", types)
        self.assertIn("fixed_expense", types)
        self.assertIn("goal_deadline", types)
        self.assertIn("deduction_scheduled", types)
        self.assertIn("deduction_paused", types)

    def test_goal_completed_when_fully_funded(self) -> None:
        self.s.bank_import({"entries": [{
            "account": "c", "goal_id": self.g_race, "amount": "1200",
            "business_time": "2026-10-05T10:00:00", "external_id": "FULL",
        }]})
        goal = self.s.goal_detail(self.g_race)["goal"]
        self.assertEqual("completed", goal["status"])

    def test_validation_errors(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            self.s.create_goal({"name": "x", "type": "unknown",
                                "target_amount": "1", "deadline": "2027-01-01",
                                "account": "c"})
        self.assertEqual(400, ctx.exception.status)
        with self.assertRaises(ApiError):
            self.s.goal_detail("不存在")

    def test_persistence_across_service_restart(self) -> None:
        goals_before = self.s.list_goals()["goals"]
        version_before = self.s.store.state["plan_version"]
        reopened = Service(Store(self.tmp))
        self.assertEqual(len(goals_before), len(reopened.list_goals()["goals"]))
        self.assertEqual(version_before, reopened.store.state["plan_version"])
        # 重开后再做一次重排，账本仍只有此前导入的事实
        reopened.trigger_replan({"reason": "重开后重排"})
        self.assertEqual(
            len(self.s.store.state["ledger"]),
            len(reopened.store.state["ledger"]),
        )


    def test_time_rolls_forward_unexecuted_past_instances_cancelled(self) -> None:
        # 9 月报名费是 paused；推进到 11 月后：paused 过期取消，新月份实例生成
        self.s.configure({"as_of_date": "2026-11-02"})
        self.s.trigger_replan({"reason": "时间推进到 11 月"})
        sep = [d for d in self.s.list_deductions()["deductions"]
               if d["month"] == "2026-09" and d["goal_name"] == "报名费"][0]
        self.assertEqual("cancelled", sep["status"])
        self.assertIn("暂停月份已过", sep["cancel_reason"])
        jun = [d for d in self.s.list_deductions()["deductions"] if d["month"] == "2027-06"]
        self.assertTrue(jun)  # 计划区间延伸月份仍有实例


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import planner
from planner import build_schedule, replan

M = 1_000_000  # 1 元 = 1e6 微单位
AS_OF = date(2026, 9, 18)


def make_state(income=20_000 * M, fixed=(), goals=(), deductions=(), direct=(), **profile_kw):
    profile = {
        "monthly_income_micros": income,
        "currency": "CNY",
        "local_tz": "+08:00",
        "bank_tz": "UTC",
        "transfer_time": "21:00",
    }
    profile.update(profile_kw)
    return {
        "profile": profile,
        "fixed_expenses": list(fixed),
        "goals": list(goals),
        "deductions": list(deductions),
        "direct_transactions": list(direct),
    }


def make_goal(gid, target, deadline, priority=5, goal_type="equipment", status="active", currency="CNY"):
    return {
        "id": gid,
        "goal_type": goal_type,
        "name": f"goal{gid}",
        "target_micros": target,
        "currency": currency,
        "deadline": deadline,
        "priority": priority,
        "status": status,
        "eta_date": None,
        "eta_status": None,
    }


def make_deduction(did, goal_id, amount, scheduled, value, status="pending",
                   executed_amount=None, recovery=None):
    return {
        "id": did,
        "goal_id": goal_id,
        "amount_micros": amount,
        "scheduled_date": scheduled,
        "value_date": value,
        "status": status,
        "recovery_condition": recovery,
        "executed_amount_micros": executed_amount,
    }


class BuildScheduleTest(unittest.TestCase):
    def test_even_split_with_remainder_on_last(self) -> None:
        entries = build_schedule(10_000_001, AS_OF, date(2026, 12, 31))
        self.assertEqual(4, len(entries))
        self.assertEqual([2_500_000, 2_500_000, 2_500_000, 2_500_001], [a for _, a in entries])
        self.assertEqual(10_000_001, sum(a for _, a in entries))

    def test_month_end_clamped_to_deadline(self) -> None:
        entries = build_schedule(1000 * M, AS_OF, date(2026, 11, 15))
        days = [d for d, _ in entries]
        self.assertEqual([date(2026, 9, 30), date(2026, 10, 31), date(2026, 11, 15)], days)

    def test_skips_past_months(self) -> None:
        entries = build_schedule(1000 * M, date(2026, 9, 30), date(2026, 10, 31))
        self.assertEqual([date(2026, 9, 30), date(2026, 10, 31)], [d for d, _ in entries])

    def test_overdue_deadline_single_entry(self) -> None:
        entries = build_schedule(500 * M, AS_OF, date(2026, 8, 31))
        self.assertEqual([(AS_OF, 500 * M)], entries)

    def test_zero_remaining(self) -> None:
        self.assertEqual([], build_schedule(0, AS_OF, date(2026, 12, 31)))


class ReplanCapacityTest(unittest.TestCase):
    def test_all_pending_when_capacity_sufficient(self) -> None:
        state = make_state(
            income=30_000 * M,
            fixed=[{"amount_micros": 15_000 * M, "active": 1}],
            goals=[make_goal(1, 6000 * M, "2026-12-31", priority=1)],
        )
        result = replan(state, AS_OF)
        inserts = [op for op in result["ops"] if op["op"] == "insert"]
        self.assertEqual(4, len(inserts))
        self.assertTrue(all(op["status"] == "pending" for op in inserts))
        self.assertEqual([], result["changes"]["paused"])
        self.assertEqual("on_track", result["goal_updates"][1]["eta_status"])
        self.assertEqual("2026-12-31", result["goal_updates"][1]["eta_date"])

    def test_lower_priority_paused_when_capacity_short(self) -> None:
        # 容量 10000/月：应急金每月 8571.428571 优先；旅行每月 2000 在
        # 结余不足的账期被暂停（未用完额度结转后续账单月）
        state = make_state(
            income=25_000 * M,
            fixed=[{"amount_micros": 15_000 * M, "active": 1}],
            goals=[
                make_goal(1, 60_000 * M, "2027-03-31", priority=1, goal_type="emergency_reserve"),
                make_goal(2, 8000 * M, "2026-12-31", priority=4, goal_type="travel"),
            ],
        )
        result = replan(state, AS_OF)
        by_goal: dict[int, list] = {}
        for op in result["ops"]:
            if op["op"] == "insert":
                by_goal.setdefault(op["goal_id"], []).append(op)

        # 应急金 7 期全部 pending（10-31 周末顺延至 11-02 由 10 月结余承接）
        emergency = by_goal[1]
        self.assertEqual(7, len(emergency))
        self.assertTrue(all(op["status"] == "pending" for op in emergency))
        self.assertEqual("on_track", result["goal_updates"][1]["eta_status"])

        # 旅行 4 期：9 月与 12 月结余不足被暂停，顺延进 11 月的两期由结余承接
        travel = by_goal[2]
        paused = [op for op in travel if op["status"] == "paused"]
        self.assertEqual({"2026-09-30", "2026-12-31"}, {op["scheduled_date"] for op in paused})
        condition = paused[0]["recovery_condition"]
        self.assertEqual("insufficient_funds", condition["type"])
        self.assertEqual("automatic", condition["resume"])
        # 旅行只有 2 期 pending，累计 4000 < 8000 → 无法预计完成
        self.assertEqual("unfunded", result["goal_updates"][2]["eta_status"])

    def test_executed_deductions_never_touched(self) -> None:
        executed = make_deduction(99, 1, 4000 * M, "2026-08-31", "2026-08-31",
                                  status="executed", executed_amount=4000 * M)
        state = make_state(
            goals=[make_goal(1, 10_000 * M, "2026-12-31", priority=1)],
            deductions=[executed],
        )
        result = replan(state, AS_OF)
        touched_ids = {op.get("id") for op in result["ops"] if op["op"] != "insert"}
        self.assertNotIn(99, touched_ids)
        inserts = [op for op in result["ops"] if op["op"] == "insert"]
        # 剩余 6000 分 4 期
        self.assertEqual(4, len(inserts))
        self.assertEqual(6000 * M, sum(op["amount_micros"] for op in inserts))

    def test_paused_resumes_when_capacity_restored(self) -> None:
        paused = make_deduction(5, 1, 1000 * M, "2026-09-30", "2026-09-30",
                                status="paused",
                                recovery={"type": "monthly_capacity", "month": "2026-09"})
        state = make_state(
            goals=[make_goal(1, 4000 * M, "2026-12-31", priority=1)],
            deductions=[paused],
        )
        result = replan(state, AS_OF)
        self.assertIn(5, result["changes"]["resumed"])
        update = next(op for op in result["ops"] if op["op"] == "update" and op["id"] == 5)
        self.assertEqual("pending", update["status"])
        self.assertIsNone(update["recovery_condition"])

    def test_completed_goal_cancels_pending(self) -> None:
        executed = make_deduction(8, 1, 1000 * M, "2026-08-31", "2026-08-31",
                                  status="executed", executed_amount=1000 * M)
        pending = make_deduction(9, 1, 500 * M, "2026-09-30", "2026-09-30")
        state = make_state(
            goals=[make_goal(1, 1000 * M, "2026-12-31", priority=1)],
            deductions=[executed, pending],
        )
        result = replan(state, AS_OF)
        self.assertIn(9, result["changes"]["cancelled"])
        self.assertEqual("completed", result["goal_updates"][1]["status"])
        self.assertEqual("funded", result["goal_updates"][1]["eta_status"])

    def test_direct_transactions_count_as_funding(self) -> None:
        state = make_state(
            goals=[make_goal(1, 4000 * M, "2026-12-31", priority=1)],
            direct=[{"goal_id": 1, "amount_micros": 1000 * M}],
        )
        result = replan(state, AS_OF)
        inserts = [op for op in result["ops"] if op["op"] == "insert"]
        self.assertEqual(3000 * M, sum(op["amount_micros"] for op in inserts))

    def test_foreign_currency_skips_capacity_check(self) -> None:
        state = make_state(
            income=0,
            goals=[make_goal(1, 4000 * M, "2026-12-31", priority=1, currency="HKD")],
        )
        result = replan(state, AS_OF)
        inserts = [op for op in result["ops"] if op["op"] == "insert"]
        self.assertTrue(all(op["status"] == "pending" for op in inserts))


class ReplanValueDateTest(unittest.TestCase):
    def test_capacity_follows_value_date_month(self) -> None:
        # 银行时区 +12：9 月 30 日发起的扣款 10 月 1 日才入账，占用 10 月容量
        goals = [
            make_goal(1, 1000 * M, "2026-09-30", priority=1),
            make_goal(2, 3000 * M, "2026-10-15", priority=2),
        ]
        shifted = make_state(income=1500 * M, goals=goals, bank_tz="+12:00")
        result = replan(shifted, AS_OF)
        by_goal: dict[int, list] = {}
        for op in result["ops"]:
            if op["op"] == "insert":
                by_goal.setdefault(op["goal_id"], []).append(op)
        # 目标 1 的扣款价值日跨入 10 月；9 月结余结转后 10 月可用 3000
        self.assertEqual("2026-10-01", by_goal[1][0]["value_date"])
        self.assertEqual("pending", by_goal[1][0]["status"])
        # 目标 2 两期各 1500 都落在 10 月：第一期由结余承接，第二期超出 → 暂停
        g2 = sorted(by_goal[2], key=lambda op: op["scheduled_date"])
        self.assertEqual("pending", g2[0]["status"])
        self.assertEqual("paused", g2[1]["status"])
        self.assertEqual("2026-10", g2[1]["recovery_condition"]["month"])

        # 同一场景、银行时区与本地一致时，被暂停的变成 9 月那一期：
        # 证明容量归属由价值日账期决定，而非计划日
        same_tz = make_state(income=1500 * M, goals=goals, bank_tz="+08:00")
        result2 = replan(same_tz, AS_OF)
        paused_sched = [
            op["scheduled_date"] for op in result2["ops"]
            if op["op"] == "insert" and op["status"] == "paused"
        ]
        self.assertEqual(["2026-09-30"], paused_sched)

    def test_eta_uses_value_date_and_can_exceed_deadline(self) -> None:
        goals = [make_goal(1, 1000 * M, "2026-09-30", priority=1)]
        state = make_state(goals=goals, bank_tz="+12:00")
        result = replan(state, AS_OF)
        update = result["goal_updates"][1]
        self.assertEqual("2026-10-01", update["eta_date"])
        self.assertEqual("at_risk", update["eta_status"])


class ReplanEtaTest(unittest.TestCase):
    def test_unfunded_when_no_capacity(self) -> None:
        state = make_state(
            income=10_000 * M,
            fixed=[{"amount_micros": 10_000 * M, "active": 1}],
            goals=[make_goal(1, 4000 * M, "2026-12-31", priority=1)],
        )
        result = replan(state, AS_OF)
        self.assertIsNone(result["goal_updates"][1]["eta_date"])
        self.assertEqual("unfunded", result["goal_updates"][1]["eta_status"])


if __name__ == "__main__":
    unittest.main()

"""端到端：通过真实 HTTP 服务复现用户故事主线。

上班族备战铁人三项：应急储备/报名/装备/旅行四个目标与房贷、信用卡
分期等固定支出排在同一时间轴；收入骤降时系统按优先级暂停低优先级
扣款，已执行转账不被回算，银行流水重复导入保持幂等，计划可版本化导出。
"""
from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server
from service import PlannerService


class ApiStoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        service = PlannerService(os.path.join(cls.tmp.name, "api.db"))
        cls.server = create_server("127.0.0.1", 0, service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method: str, path: str, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(
                method,
                path,
                json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
                {"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode("utf-8"))
        finally:
            conn.close()

    def test_00_health(self) -> None:
        status, data = self.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", data["status"])

    def test_01_setup_household(self) -> None:
        status, data = self.request(
            "PUT",
            "/household/profile",
            {
                "monthly_income": "30000",
                "currency": "CNY",
                "local_tz": "+08:00",
                "bank_tz": "UTC",
                "transfer_time": "21:00",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("30000", data["profile"]["monthly_income"])
        for name, amount, day in [("房贷", "10000", 1), ("信用卡分期", "3000", 15), ("生活费", "2000", 1)]:
            status, _ = self.request(
                "POST", "/household/fixed-expenses", {"name": name, "amount": amount, "day_of_month": day}
            )
            self.assertEqual(201, status)

    def test_02_create_goals(self) -> None:
        goals = [
            ("emergency_reserve", "应急储备", "60000", "2027-03-31", "on_track"),
            ("race_fee", "铁人三项报名", "4000", "2026-12-31", "on_track"),
            # 截止日 2027-02-28 是周日：最后一期顺延至 03-01 入账，晚于截止日
            ("equipment", "铁三装备", "12000", "2027-02-28", "at_risk"),
            ("travel", "比赛旅行", "14000", "2027-03-31", "on_track"),
        ]
        for goal_type, name, target, deadline, expected_eta in goals:
            status, data = self.request(
                "POST",
                "/goals",
                {
                    "goal_type": goal_type,
                    "name": name,
                    "target_amount": target,
                    "deadline": deadline,
                    "as_of": "2026-09-18",
                },
            )
            self.assertEqual(201, status, data)
            self.assertEqual(expected_eta, data["goal"]["eta_status"], name)
        status, data = self.request("GET", "/goals")
        self.assertEqual(4, len(data["items"]))
        # 全部扣款在容量内，无暂停
        status, data = self.request("GET", "/deductions?status=paused")
        self.assertEqual([], data["items"])

    def test_03_timeline_merges_everything(self) -> None:
        status, data = self.request("GET", "/timeline?from=2026-09-01&to=2027-04-30")
        self.assertEqual(200, status)
        kinds = {item["kind"] for item in data["items"]}
        self.assertIn("income", kinds)
        self.assertIn("expense", kinds)
        self.assertIn("scheduled_transfer", kinds)
        self.assertIn("goal_deadline", kinds)
        transfers = [i for i in data["items"] if i["kind"] == "scheduled_transfer"]
        self.assertTrue(all("value_date" in t for t in transfers))

    def test_04_import_bank_statement_idempotent(self) -> None:
        status, data = self.request("GET", "/goals")
        emergency = next(g for g in data["items"] if g["goal_type"] == "emergency_reserve")
        self.__class__.emergency_id = emergency["id"]
        status, data = self.request("GET", f"/deductions?goal_id={emergency['id']}&status=pending")
        first_two = data["items"][:2]
        payload = {
            "account": "main",
            "transactions": [
                {
                    "external_id": f"tx-{d['id']}",
                    "amount": d["amount"],
                    "posted_at": f"{d['value_date']}T13:00:00+00:00",
                    "value_date": d["value_date"],
                    "deduction_id": d["id"],
                }
                for d in first_two
            ],
        }
        status, data = self.request("POST", "/bank/import", payload)
        self.assertEqual(201, status)
        self.assertEqual(2, data["imported"])
        # 重复导入同一文件：幂等，不重复入账
        status, data = self.request("POST", "/bank/import", payload)
        self.assertEqual(0, data["imported"])
        self.assertEqual(2, data["duplicates"])
        status, data = self.request("GET", f"/goals/{emergency['id']}/funding")
        self.assertEqual("17142.857142", data["funded"])

    def test_05_income_drop_reprioritizes(self) -> None:
        status, data = self.request(
            "POST",
            "/events",
            {
                "event_type": "income_change",
                "payload": {"new_monthly_income": "24000"},
                "occurred_on": "2026-10-05",
                "reason": "连续两月加大训练量后收入结构调整，降至 24000",
            },
        )
        self.assertEqual(201, status)
        self.__class__.income_drop_version = data["plan_version"]
        status, data = self.request("GET", "/goals")
        by_type = {g["goal_type"]: g for g in data["items"]}
        self.__class__.travel_id = by_type["travel"]["id"]
        self.__class__.equipment_id = by_type["equipment"]["id"]
        self.__class__.race_id = by_type["race_fee"]["id"]
        # 应急金与报名优先保障，装备与旅行被暂停
        status, data = self.request("GET", f"/goals/{self.emergency_id}/funding")
        self.assertEqual([], data["paused"])
        status, data = self.request("GET", f"/goals/{self.race_id}/funding")
        self.assertEqual([], data["paused"])
        for gid in (self.travel_id, self.equipment_id):
            status, data = self.request("GET", f"/goals/{gid}/funding")
            self.assertTrue(data["paused"], gid)
            condition = data["paused"][0]["recovery_condition"]
            self.assertEqual("insufficient_funds", condition["type"])
            self.assertEqual("automatic", condition["resume"])

    def test_06_executed_transfers_untouched(self) -> None:
        status, data = self.request("GET", f"/deductions?goal_id={self.emergency_id}&status=executed")
        self.assertEqual(2, len(data["items"]))
        self.assertTrue(all(d["executed_amount"] for d in data["items"]))

    def test_07_medical_emergency_bumps_reserve(self) -> None:
        status, data = self.request(
            "POST",
            "/events",
            {
                "event_type": "emergency",
                "payload": {"amount": "8000", "description": "训练受伤门诊"},
                "occurred_on": "2026-10-10",
            },
        )
        self.assertEqual(201, status)
        status, data = self.request("GET", f"/goals/{self.emergency_id}")
        self.assertEqual("68000", data["target"])

    def test_08_race_postponed(self) -> None:
        status, data = self.request(
            "POST",
            "/events",
            {
                "event_type": "race_postponed",
                "payload": {"goal_id": self.race_id, "new_deadline": "2027-03-31"},
                "occurred_on": "2026-10-12",
            },
        )
        self.assertEqual(201, status)
        status, data = self.request("GET", f"/goals/{self.race_id}")
        self.assertEqual("2027-03-31", data["deadline"])

    def test_09_income_restored_resumes(self) -> None:
        status, data = self.request(
            "POST",
            "/events",
            {
                "event_type": "income_change",
                "payload": {"new_monthly_income": "30000"},
                "occurred_on": "2026-11-05",
            },
        )
        self.assertEqual(201, status)
        # 装备全部恢复；医疗补充后的应急金占用更高，旅行仍部分暂停
        status, data = self.request("GET", f"/goals/{self.equipment_id}/funding")
        self.assertEqual([], data["paused"])
        status, data = self.request("GET", f"/goals/{self.travel_id}/funding")
        self.assertTrue(data["paused"])
        # 手动恢复一笔旅行扣款
        paused_id = data["paused"][0]["id"]
        status, data = self.request("POST", f"/deductions/{paused_id}/resume")
        self.assertEqual(200, status)
        self.assertEqual("pending", data["deduction"]["status"])

    def test_10_versioned_export_for_advisor(self) -> None:
        status, data = self.request("GET", "/plans/versions")
        versions = data["items"]
        self.assertGreaterEqual(len(versions), 7)
        # 首版导出在后续变更后内容保持不变
        status, first = self.request("GET", "/plans/versions/1/export")
        self.assertEqual(200, status)
        status, again = self.request("GET", "/plans/versions/1/export")
        self.assertEqual(first["checksum"], again["checksum"])
        self.assertEqual(first["plan"], again["plan"])
        # 收入骤降版本：触发原因、受影响目标、新预计完成日齐备
        status, drop = self.request("GET", f"/plans/versions/{self.income_drop_version}/export")
        self.assertEqual("income_change", drop["trigger"]["event_type"])
        self.assertIn("收入", drop["reason"])
        self.assertIn(self.travel_id, drop["changes"]["affected_goals"])
        self.assertTrue(drop["changes"]["goal_updates"])
        self.assertTrue(drop["plan"]["goals"])
        self.assertTrue(drop["checksum"])

    def test_11_query_errors(self) -> None:
        status, _ = self.request("GET", "/goals/999")
        self.assertEqual(404, status)
        status, data = self.request(
            "POST", "/goals", {"goal_type": "yacht", "name": "x", "target_amount": "1", "deadline": "2027-01-01"}
        )
        self.assertEqual(400, status)
        self.assertIn("error", data)
        status, _ = self.request("GET", "/nope")
        self.assertEqual(404, status)


if __name__ == "__main__":
    unittest.main()

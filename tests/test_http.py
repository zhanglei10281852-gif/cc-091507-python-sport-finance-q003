from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import app


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="planner-http-")
        app.reset_service(cls.tmp)
        cls.port = _free_port()
        cls.server = app.create_server("127.0.0.1", cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        url = "http://127.0.0.1:{}{}".format(
            self.port, urllib.parse.quote(path, safe="/?&="))
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self) -> None:
        status, payload = self.call("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_full_flow_over_http(self) -> None:
        self.assertEqual(200, self.call("PUT", "/config", {
            "plan_start_month": "2026-09", "plan_end_month": "2027-03",
            "as_of_date": "2026-09-18",
        })[0])
        _, race = self.call("POST", "/races", {"name": "夏铁", "race_date": "2027-03-20"})
        race_id = race["race"]["id"]
        _, goal = self.call("POST", "/goals", {
            "name": "报名费", "type": "race_fee", "target_amount": "1200",
            "deadline": "2026-12-31", "account": "c", "race_id": race_id,
        })
        gid = goal["goal"]["id"]
        self.call("PUT", "/income", {"month": "2026-09", "account": "c", "amount": "20000"})
        self.call("POST", "/fixed-expenses", {
            "name": "房租", "category": "housing", "account": "c",
            "amount": "6000", "start_month": "2026-09"})
        _, replan = self.call("POST", "/replan", {"trigger_type": "baseline", "reason": "基线"})
        self.assertTrue(replan["recorded"])

        _, funding = self.call("GET", f"/goals/{gid}/funding")
        self.assertIn("funding", funding)

        _, imp = self.call("POST", "/bank-imports", {"entries": [{
            "account": "c", "goal_id": gid, "amount": "300",
            "business_time": "2026-10-05T10:00:00", "external_id": "W-1",
        }]})
        self.assertEqual(1, imp["imported"])

        status, timeline = self.call("GET", "/timeline")
        self.assertEqual(200, status)
        self.assertTrue(timeline["count"] > 0)

        status, exports = self.call("GET", "/exports")
        self.assertEqual(200, status)
        self.assertTrue(exports["exports"])

        status, snap = self.call("GET", f"/exports/{exports['exports'][-1]['version']}")
        self.assertEqual(200, status)
        self.assertEqual("family-training-plan/v1", snap["schema"])

    def test_error_mapping(self) -> None:
        status, payload = self.call("GET", "/goals/不存在")
        self.assertEqual(404, status)
        self.assertIn("error", payload)
        status, _ = self.call("GET", "/nope")
        self.assertEqual(404, status)
        # 非法 JSON
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/goals", data=b"{bad",
            method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(400, ctx.exception.code)


if __name__ == "__main__":
    unittest.main()

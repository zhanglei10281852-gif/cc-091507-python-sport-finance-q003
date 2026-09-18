"""HTTP 接口层：标准库实现的 JSON 路由。

所有接口返回 JSON；业务错误映射为 4xx，未捕获异常映射为 500。
持久化文件写入 .runtime/（可用环境变量 PLANNER_DB 覆盖）。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from service import PlannerService, ServiceError

SERVICE_NAME = '家庭训练储蓄编排器'


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


# ----------------------------------------------------------------------
# 路由处理器：fn(service, match, query, body) -> (status, payload[, headers])
# ----------------------------------------------------------------------
def _h_health(service, match, query, body):
    return 200, health_payload()


def _h_domain(service, match, query, body):
    return 200, service.domain


def _h_get_profile(service, match, query, body):
    return 200, service.get_profile()


def _h_put_profile(service, match, query, body):
    return 200, service.update_profile(body)


def _h_add_expense(service, match, query, body):
    return 201, service.add_fixed_expense(body)


def _h_list_expenses(service, match, query, body):
    return 200, {"items": service.list_fixed_expenses()}


def _h_delete_expense(service, match, query, body):
    return 200, service.delete_fixed_expense(int(match.group(1)))


def _h_create_goal(service, match, query, body):
    return 201, service.create_goal(body)


def _h_list_goals(service, match, query, body):
    return 200, {"items": service.list_goals()}


def _h_get_goal(service, match, query, body):
    return 200, service.get_goal(int(match.group(1)))


def _h_goal_funding(service, match, query, body):
    return 200, service.goal_funding(int(match.group(1)))


def _h_create_event(service, match, query, body):
    return 201, service.record_event(body)


def _h_list_events(service, match, query, body):
    return 200, {"items": service.list_events()}


def _h_replan(service, match, query, body):
    body = body or {}
    return 201, service.replan_now(body.get("reason"), body.get("as_of"))


def _h_list_versions(service, match, query, body):
    return 200, {"items": service.list_versions()}


def _h_get_version(service, match, query, body):
    return 200, service.get_version(int(match.group(1)))


def _h_export_version(service, match, query, body):
    version_id = int(match.group(1))
    doc = service.export_version(version_id)
    return 200, doc, {"Content-Disposition": f'attachment; filename="plan-v{version_id}.json"'}


def _h_list_deductions(service, match, query, body):
    goal_id = query.get("goal_id")
    return 200, {
        "items": service.list_deductions(
            status=query.get("status"),
            goal_id=int(goal_id) if goal_id is not None else None,
        )
    }


def _h_resume_deduction(service, match, query, body):
    return 200, service.resume_deduction(int(match.group(1)))


def _h_execute_deduction(service, match, query, body):
    return 200, service.execute_deduction(int(match.group(1)), body)


def _h_import_bank(service, match, query, body):
    return 201, service.import_bank_transactions(body)


def _h_list_bank_txns(service, match, query, body):
    return 200, {"items": service.list_bank_transactions(query.get("account"))}


def _h_add_block(service, match, query, body):
    return 201, service.add_training_block(body)


def _h_list_blocks(service, match, query, body):
    return 200, {"items": service.list_training_blocks()}


def _h_timeline(service, match, query, body):
    today = date.today()
    try:
        start = date.fromisoformat(query["from"]) if query.get("from") else today - timedelta(days=31)
        end = date.fromisoformat(query["to"]) if query.get("to") else today + timedelta(days=366)
    except ValueError:
        raise ServiceError(400, "from/to 必须是 ISO 日期（YYYY-MM-DD）")
    return 200, {"items": service.timeline(start, end)}


ROUTES = [
    ("GET", re.compile(r"/health"), _h_health),
    ("GET", re.compile(r"/reference/domain"), _h_domain),
    ("GET", re.compile(r"/household/profile"), _h_get_profile),
    ("PUT", re.compile(r"/household/profile"), _h_put_profile),
    ("POST", re.compile(r"/household/fixed-expenses"), _h_add_expense),
    ("GET", re.compile(r"/household/fixed-expenses"), _h_list_expenses),
    ("DELETE", re.compile(r"/household/fixed-expenses/(\d+)"), _h_delete_expense),
    ("POST", re.compile(r"/goals"), _h_create_goal),
    ("GET", re.compile(r"/goals"), _h_list_goals),
    ("GET", re.compile(r"/goals/(\d+)"), _h_get_goal),
    ("GET", re.compile(r"/goals/(\d+)/funding"), _h_goal_funding),
    ("POST", re.compile(r"/events"), _h_create_event),
    ("GET", re.compile(r"/events"), _h_list_events),
    ("POST", re.compile(r"/replan"), _h_replan),
    ("GET", re.compile(r"/plans/versions"), _h_list_versions),
    ("GET", re.compile(r"/plans/versions/(\d+)"), _h_get_version),
    ("GET", re.compile(r"/plans/versions/(\d+)/export"), _h_export_version),
    ("GET", re.compile(r"/deductions"), _h_list_deductions),
    ("POST", re.compile(r"/deductions/(\d+)/resume"), _h_resume_deduction),
    ("POST", re.compile(r"/deductions/(\d+)/execute"), _h_execute_deduction),
    ("POST", re.compile(r"/bank/import"), _h_import_bank),
    ("GET", re.compile(r"/bank/transactions"), _h_list_bank_txns),
    ("POST", re.compile(r"/training-blocks"), _h_add_block),
    ("GET", re.compile(r"/training-blocks"), _h_list_blocks),
    ("GET", re.compile(r"/timeline"), _h_timeline),
]


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(parsed.path)
            if not match:
                continue
            headers = None
            try:
                body = self._read_body() if method in ("POST", "PUT", "DELETE") else None
                result = handler(self.server.service, match, query, body)  # type: ignore[attr-defined]
                status, payload = result[0], result[1]
                if len(result) > 2:
                    headers = result[2]
            except ServiceError as exc:
                status, payload = exc.status, {"error": exc.message}
            except Exception as exc:  # pragma: no cover - 兜底
                status, payload = 500, {"error": f"内部错误：{exc}"}
            self._send(status, payload, headers)
            return
        self._send(404, {"error": "接口不存在"})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ServiceError(400, "请求体必须是合法 JSON")
        if not isinstance(data, dict):
            raise ServiceError(400, "请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, payload: dict, headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int, service: PlannerService | None = None) -> ThreadingHTTPServer:
    if service is None:
        service = PlannerService(os.getenv("PLANNER_DB") or None)
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.service = service  # type: ignore[attr-defined]
    return server

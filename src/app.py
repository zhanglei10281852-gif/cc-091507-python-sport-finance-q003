"""HTTP 入口：仅做路由、JSON 编解码与错误映射，业务在 service 层。"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from service import ApiError, Service
from store import Store

SERVICE_NAME = '家庭训练储蓄编排器'

_STORE: Store | None = None
_SERVICE: Service | None = None


def get_service() -> Service:
    global _STORE, _SERVICE
    if _SERVICE is None:
        runtime = os.getenv("RUNTIME_DIR", ".runtime")
        _STORE = Store(runtime)
        _SERVICE = Service(_STORE)
    return _SERVICE


def reset_service(runtime_dir: str = ".runtime") -> Service:
    """测试辅助：用指定目录重建服务。"""
    global _STORE, _SERVICE
    _STORE = Store(runtime_dir)
    _SERVICE = Service(_STORE)
    return _SERVICE


def health_payload() -> dict[str, str]:
    return {"status": "ok", "service": SERVICE_NAME}


# 路由表：(method, path 模式) -> handler(service, body, query, params)
def _route_cases():  # noqa: C901
    return [
        ("GET", "/health", lambda s, b, q, p: health_payload()),
        ("GET", "/reference", lambda s, b, q, p: s.reference()),
        ("GET", "/config", lambda s, b, q, p: {"config": s.store.state["config"]}),
        ("PUT", "/config", lambda s, b, q, p: s.configure(b)),

        ("POST", "/goals", lambda s, b, q, p: s.create_goal(b)),
        ("GET", "/goals", lambda s, b, q, p: s.list_goals()),
        ("GET", "/goals/{id}", lambda s, b, q, p: s.goal_detail(p["id"])),
        ("GET", "/goals/{id}/funding", lambda s, b, q, p: s.goal_funding(p["id"])),

        ("PUT", "/income", lambda s, b, q, p: s.upsert_income(
            b, trigger=str(q.get("replan", [""])[0]).lower() in ("1", "true", "yes"),
            reason=q.get("reason", [""])[0])),

        ("POST", "/fixed-expenses", lambda s, b, q, p: s.create_fixed_expense(b)),
        ("POST", "/one-off-expenses", lambda s, b, q, p: s.add_one_off_expense(
            b, replan=str(q.get("replan", ["true"])[0]).lower() != "false",
            reason=q.get("reason", [""])[0])),

        ("POST", "/training-phases", lambda s, b, q, p: s.create_training_phase(b)),
        ("POST", "/races", lambda s, b, q, p: s.create_race(b)),
        ("POST", "/races/{id}/postpone", lambda s, b, q, p: s.postpone_race(p["id"], b)),

        ("POST", "/bank-imports", lambda s, b, q, p: s.bank_import(b)),
        ("POST", "/transfers", lambda s, b, q, p: s.manual_transfer(b)),

        ("GET", "/deductions", lambda s, b, q, p: s.list_deductions(q.get("status", [None])[0])),
        ("POST", "/deductions/{id}/pause", lambda s, b, q, p: s.pause_deduction(p["id"], b)),
        ("POST", "/deductions/{id}/resume", lambda s, b, q, p: s.resume_deduction(p["id"], b)),
        ("POST", "/deductions/{id}/pin", lambda s, b, q, p: s.pin_deduction(p["id"], b)),
        ("POST", "/replan", lambda s, b, q, p: s.trigger_replan(b)),

        ("GET", "/timeline", lambda s, b, q, p: s.timeline(
            q.get("from", [None])[0], q.get("to", [None])[0])),

        ("GET", "/adjustments", lambda s, b, q, p: s.list_adjustments()),
        ("GET", "/adjustments/{id}", lambda s, b, q, p: s.adjustment_detail(p["id"])),
        ("GET", "/exports", lambda s, b, q, p: s.list_exports()),
        ("GET", "/exports/{version}", lambda s, b, q, p: s.export_snapshot(int(p["version"]))),
    ]


def _match(method: str, path: str):
    parts = [seg for seg in path.split("/") if seg != ""]
    for m, pattern, handler in _route_cases():
        if m != method:
            continue
        pat_parts = [seg for seg in pattern.split("/") if seg != ""]
        if len(pat_parts) != len(parts):
            continue
        params: dict[str, str] = {}
        ok = True
        for pp, ap in zip(pat_parts, parts):
            if pp.startswith("{") and pp.endswith("}"):
                params[pp[1:-1]] = ap
            elif pp != ap:
                ok = False
                break
        if ok:
            return handler, params
    return None, None


class RequestHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        split = urlsplit(self.path)
        query = parse_qs(split.query)
        handler, params = _match(method, split.path)
        if handler is None:
            self._send(404, {"error": "Not Found", "path": split.path})
            return

        body: dict = {}
        if method in ("POST", "PUT"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "请求体不是合法 JSON"})
                return
            if not isinstance(body, dict):
                self._send(400, {"error": "请求体必须是 JSON 对象"})
                return

        try:
            result = handler(get_service(), body, query, params or {})
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message, "status": exc.status})
            return
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(200, result)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), RequestHandler)

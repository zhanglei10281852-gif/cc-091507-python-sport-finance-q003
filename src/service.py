"""服务层：HTTP 与计划引擎之间的编排。

负责：家庭画像与固定支出、目标生命周期、触发事件应用、重排与版本
快照、银行流水幂等导入、资金来源/暂停扣款查询、时间轴与顾问导出。
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import planner
from money import MoneyError, to_amount_str, to_micros
from store import connect, init_db, utcnow
from valuedate import parse_hhmm, parse_tz

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = str(ROOT / ".runtime" / "planner.db")
DOMAIN_PATH = ROOT / "reference" / "domain.json"

DEFAULT_DOMAIN = {
    "trigger_types": ["income_change", "race_postponed", "emergency", "goal_change", "note"],
    "goal_types": ["race_fee", "equipment", "travel", "emergency_reserve"],
    "goal_statuses": ["active", "completed", "cancelled"],
    "deduction_statuses": ["pending", "paused", "executed", "cancelled"],
    "currencies": ["CNY", "HKD", "USD"],
}
DEFAULT_PRIORITY = {"emergency_reserve": 1, "race_fee": 2, "equipment": 3, "travel": 4}
DEFAULT_REASONS = {
    "income_change": "收入变化，按优先级重排未来扣款",
    "race_postponed": "比赛延期，重排报名与旅行目标",
    "emergency": "临时医疗/紧急支出，优先补充应急储备",
    "goal_change": "目标调整，重排未来扣款",
    "note": "手动备注触发重排",
}
EVENT_KIND_MAP = {"income_change": "income", "emergency": "emergency"}


class ServiceError(Exception):
    """可直接映射为 HTTP 状态码的业务错误。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _require(data: dict, field: str):
    value = data.get(field)
    if value is None:
        raise ServiceError(400, f"缺少字段：{field}")
    return value


def _parse_date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ServiceError(400, f"{field} 必须是 ISO 日期（YYYY-MM-DD）")


def _parse_money(value: object, field: str) -> int:
    try:
        return to_micros(value)
    except MoneyError as exc:
        raise ServiceError(400, f"{field}：{exc}")


def _load_domain() -> dict:
    domain = dict(DEFAULT_DOMAIN)
    try:
        with open(DOMAIN_PATH, encoding="utf-8") as fh:
            domain.update(json.load(fh))
    except OSError:
        pass
    return domain


class PlannerService:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DEFAULT_DB
        init_db(self.db_path)
        self.domain = _load_domain()

    # ------------------------------------------------------------------
    # 连接与通用读取
    # ------------------------------------------------------------------
    @contextmanager
    def _read(self):
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self):
        """写事务：BEGIN IMMEDIATE 保证重排期间版本号与扣款状态一致。"""
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _next_version_id(conn) -> int:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS v FROM plan_versions").fetchone()
        return int(row["v"])

    @staticmethod
    def _profile_row(conn) -> dict:
        return dict(conn.execute("SELECT * FROM profile WHERE id = 1").fetchone())

    @staticmethod
    def _goal_row(conn, goal_id: int):
        return conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()

    def _must_goal(self, conn, goal_id: int):
        row = self._goal_row(conn, goal_id)
        if row is None:
            raise ServiceError(404, f"目标不存在：{goal_id}")
        return row

    @staticmethod
    def _funded_micros(conn, goal_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(executed_amount_micros, amount_micros)), 0) AS s "
            "FROM deductions WHERE goal_id = ? AND status = 'executed'",
            (goal_id,),
        ).fetchone()
        direct = conn.execute(
            "SELECT COALESCE(SUM(amount_micros), 0) AS s "
            "FROM bank_transactions WHERE goal_id = ? AND deduction_id IS NULL",
            (goal_id,),
        ).fetchone()
        return int(row["s"]) + int(direct["s"])

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    @staticmethod
    def _profile_view(row: dict) -> dict:
        return {
            "monthly_income": to_amount_str(row["monthly_income_micros"]),
            "currency": row["currency"],
            "local_tz": row["local_tz"],
            "bank_tz": row["bank_tz"],
            "transfer_time": row["transfer_time"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _expense_view(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "amount": to_amount_str(row["amount_micros"]),
            "day_of_month": row["day_of_month"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
        }

    def _goal_view(self, conn, row) -> dict:
        funded = self._funded_micros(conn, row["id"])
        return {
            "id": row["id"],
            "goal_type": row["goal_type"],
            "name": row["name"],
            "target": to_amount_str(row["target_micros"]),
            "funded": to_amount_str(funded),
            "remaining": to_amount_str(max(row["target_micros"] - funded, 0)),
            "currency": row["currency"],
            "deadline": row["deadline"],
            "priority": row["priority"],
            "status": row["status"],
            "eta_date": row["eta_date"],
            "eta_status": row["eta_status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _deduction_view(row) -> dict:
        return {
            "id": row["id"],
            "goal_id": row["goal_id"],
            "amount": to_amount_str(row["amount_micros"]),
            "scheduled_date": row["scheduled_date"],
            "value_date": row["value_date"],
            "status": row["status"],
            "recovery_condition": json.loads(row["recovery_condition"]) if row["recovery_condition"] else None,
            "executed_amount": (
                to_amount_str(row["executed_amount_micros"])
                if row["executed_amount_micros"] is not None
                else None
            ),
            "executed_at": row["executed_at"],
            "bank_txn_id": row["bank_txn_id"],
            "created_version": row["created_version"],
            "updated_version": row["updated_version"],
        }

    @staticmethod
    def _txn_view(row) -> dict:
        return {
            "id": row["id"],
            "account": row["account"],
            "external_id": row["external_id"],
            "posted_at": row["posted_at"],
            "value_date": row["value_date"],
            "amount": to_amount_str(row["amount_micros"]),
            "currency": row["currency"],
            "goal_id": row["goal_id"],
            "deduction_id": row["deduction_id"],
            "memo": row["memo"],
            "import_batch": row["import_batch"],
            "imported_at": row["imported_at"],
        }

    # ------------------------------------------------------------------
    # 家庭画像与固定支出
    # ------------------------------------------------------------------
    def get_profile(self) -> dict:
        with self._read() as conn:
            return self._profile_view(self._profile_row(conn))

    def update_profile(self, data: dict) -> dict:
        data = data or {}
        updates: dict[str, object] = {}
        if "monthly_income" in data:
            income = _parse_money(data["monthly_income"], "monthly_income")
            if income < 0:
                raise ServiceError(400, "monthly_income 不能为负")
            updates["monthly_income_micros"] = income
        if "currency" in data:
            currency = str(data["currency"]).upper()
            if currency not in self.domain["currencies"]:
                raise ServiceError(400, f"不支持的币种：{currency}")
            updates["currency"] = currency
        for field in ("local_tz", "bank_tz"):
            if field in data:
                parse_tz(data[field])  # 校验可解析
                updates[field] = str(data[field])
        if "transfer_time" in data:
            parse_hhmm(data["transfer_time"])
            updates["transfer_time"] = str(data["transfer_time"])
        if not updates:
            raise ServiceError(400, "没有可更新的字段")
        with self._tx() as conn:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            conn.execute(
                f"UPDATE profile SET {assignments}, updated_at = ? WHERE id = 1",
                (*updates.values(), utcnow()),
            )
            version = None
            if self._has_active_goals(conn):
                version = self._replan(conn, "更新家庭财务画像", date.today())
            profile = self._profile_view(self._profile_row(conn))
        result: dict = {"profile": profile}
        if version:
            result["plan_version"] = version["id"]
        return result

    @staticmethod
    def _has_active_goals(conn) -> bool:
        row = conn.execute("SELECT COUNT(*) AS c FROM goals WHERE status = 'active'").fetchone()
        return int(row["c"]) > 0

    def add_fixed_expense(self, data: dict) -> dict:
        data = data or {}
        name = str(_require(data, "name")).strip()
        if not name:
            raise ServiceError(400, "name 不能为空")
        amount = _parse_money(_require(data, "amount"), "amount")
        if amount <= 0:
            raise ServiceError(400, "amount 必须大于 0")
        day = int(data.get("day_of_month") or 1)
        if not 1 <= day <= 31:
            raise ServiceError(400, "day_of_month 必须在 1..31 之间")
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO fixed_expenses (name, amount_micros, day_of_month, created_at) VALUES (?,?,?,?)",
                (name, amount, day, utcnow()),
            )
            version = None
            if self._has_active_goals(conn):
                version = self._replan(conn, f"新增固定支出：{name}", date.today())
            row = conn.execute("SELECT * FROM fixed_expenses WHERE id = ?", (cur.lastrowid,)).fetchone()
        result: dict = {"expense": self._expense_view(row)}
        if version:
            result["plan_version"] = version["id"]
        return result

    def list_fixed_expenses(self) -> list:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM fixed_expenses ORDER BY id").fetchall()
            return [self._expense_view(r) for r in rows]

    def delete_fixed_expense(self, expense_id: int) -> dict:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM fixed_expenses WHERE id = ?", (expense_id,)).fetchone()
            if row is None or not row["active"]:
                raise ServiceError(404, f"固定支出不存在：{expense_id}")
            conn.execute("UPDATE fixed_expenses SET active = 0 WHERE id = ?", (expense_id,))
            version = None
            if self._has_active_goals(conn):
                version = self._replan(conn, f"移除固定支出：{row['name']}", date.today())
        result: dict = {"deleted": expense_id}
        if version:
            result["plan_version"] = version["id"]
        return result

    # ------------------------------------------------------------------
    # 目标
    # ------------------------------------------------------------------
    def create_goal(self, data: dict) -> dict:
        data = data or {}
        goal_type = _require(data, "goal_type")
        if goal_type not in self.domain["goal_types"]:
            raise ServiceError(400, f"未知目标类型：{goal_type}")
        name = str(_require(data, "name")).strip()
        if not name:
            raise ServiceError(400, "name 不能为空")
        target = _parse_money(_require(data, "target_amount"), "target_amount")
        if target <= 0:
            raise ServiceError(400, "target_amount 必须大于 0")
        deadline = _parse_date(_require(data, "deadline"), "deadline")
        priority = data.get("priority")
        priority = int(priority) if priority is not None else DEFAULT_PRIORITY.get(goal_type, 5)
        as_of = _parse_date(data["as_of"], "as_of") if data.get("as_of") else date.today()
        with self._tx() as conn:
            profile = self._profile_row(conn)
            currency = str(data.get("currency") or profile["currency"]).upper()
            if currency not in self.domain["currencies"]:
                raise ServiceError(400, f"不支持的币种：{currency}")
            now = utcnow()
            cur = conn.execute(
                "INSERT INTO goals (goal_type, name, target_micros, currency, deadline, priority, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (goal_type, name, target, currency, deadline.isoformat(), priority, now, now),
            )
            goal_id = cur.lastrowid
            version = self._replan(conn, f"新增目标：{name}", as_of)
            goal = self._goal_view(conn, self._goal_row(conn, goal_id))
        return {"goal": goal, "plan_version": version["id"]}

    def list_goals(self) -> list:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM goals ORDER BY priority, deadline, id").fetchall()
            return [self._goal_view(conn, r) for r in rows]

    def get_goal(self, goal_id: int) -> dict:
        with self._read() as conn:
            return self._goal_view(conn, self._must_goal(conn, goal_id))

    def goal_funding(self, goal_id: int) -> dict:
        """资金来源、被暂停的扣款与恢复条件。"""
        with self._read() as conn:
            goal = self._must_goal(conn, goal_id)
            executed = conn.execute(
                "SELECT d.*, b.account AS bank_account, b.external_id AS bank_external_id "
                "FROM deductions d LEFT JOIN bank_transactions b ON b.id = d.bank_txn_id "
                "WHERE d.goal_id = ? AND d.status = 'executed' ORDER BY d.value_date, d.id",
                (goal_id,),
            ).fetchall()
            executed_items = []
            for row in executed:
                item = self._deduction_view(row)
                item["bank_transaction"] = (
                    {"account": row["bank_account"], "external_id": row["bank_external_id"]}
                    if row["bank_txn_id"] is not None
                    else None
                )
                executed_items.append(item)
            pending = conn.execute(
                "SELECT * FROM deductions WHERE goal_id = ? AND status = 'pending' ORDER BY value_date, id",
                (goal_id,),
            ).fetchall()
            paused = conn.execute(
                "SELECT * FROM deductions WHERE goal_id = ? AND status = 'paused' ORDER BY value_date, id",
                (goal_id,),
            ).fetchall()
            direct = conn.execute(
                "SELECT * FROM bank_transactions WHERE goal_id = ? AND deduction_id IS NULL ORDER BY value_date, id",
                (goal_id,),
            ).fetchall()
            funded = self._funded_micros(conn, goal_id)
            return {
                "goal": self._goal_view(conn, goal),
                "funded": to_amount_str(funded),
                "remaining": to_amount_str(max(goal["target_micros"] - funded, 0)),
                "sources": {
                    "executed_deductions": executed_items,
                    "direct_transactions": [self._txn_view(r) for r in direct],
                    "pending_deductions": [self._deduction_view(r) for r in pending],
                },
                "paused": [self._deduction_view(r) for r in paused],
            }

    # ------------------------------------------------------------------
    # 事件与重排
    # ------------------------------------------------------------------
    def _planner_state(self, conn) -> dict:
        expenses = [dict(r) for r in conn.execute("SELECT * FROM fixed_expenses").fetchall()]
        goals = [dict(r) for r in conn.execute("SELECT * FROM goals").fetchall()]
        deductions = []
        for r in conn.execute("SELECT * FROM deductions WHERE status != 'cancelled'").fetchall():
            row = dict(r)
            row["recovery_condition"] = json.loads(row["recovery_condition"]) if row["recovery_condition"] else None
            deductions.append(row)
        direct = [
            dict(r)
            for r in conn.execute(
                "SELECT goal_id, amount_micros FROM bank_transactions "
                "WHERE goal_id IS NOT NULL AND deduction_id IS NULL"
            ).fetchall()
        ]
        return {
            "profile": self._profile_row(conn),
            "fixed_expenses": expenses,
            "goals": goals,
            "deductions": deductions,
            "direct_transactions": direct,
        }

    def _snapshot(self, conn, as_of: date, version_id: int) -> dict:
        profile = self._profile_view(self._profile_row(conn))
        expenses = [
            self._expense_view(r)
            for r in conn.execute("SELECT * FROM fixed_expenses WHERE active = 1 ORDER BY id").fetchall()
        ]
        goals = [
            self._goal_view(conn, r)
            for r in conn.execute("SELECT * FROM goals ORDER BY id").fetchall()
        ]
        deductions = [
            self._deduction_view(r)
            for r in conn.execute("SELECT * FROM deductions ORDER BY id").fetchall()
        ]
        return {
            "version": version_id,
            "as_of": as_of.isoformat(),
            "generated_at": utcnow(),
            "profile": profile,
            "fixed_expenses": expenses,
            "goals": goals,
            "deductions": deductions,
        }

    def _insert_version(self, conn, version_id: int, event_id, reason: str, changes: dict, as_of: date) -> dict:
        snapshot = self._snapshot(conn, as_of, version_id)
        conn.execute(
            "INSERT INTO plan_versions (id, event_id, reason, changes, snapshot, created_at) VALUES (?,?,?,?,?,?)",
            (
                version_id,
                event_id,
                reason,
                json.dumps(changes, ensure_ascii=False),
                json.dumps(snapshot, ensure_ascii=False),
                utcnow(),
            ),
        )
        return {"id": version_id, "reason": reason, "changes": changes}

    def _replan(self, conn, reason: str, as_of: date, event_id=None, extra_changes: dict | None = None) -> dict:
        """执行一次重排并落库一个新版本。已执行扣款不会被修改。"""
        state = self._planner_state(conn)
        result = planner.replan(state, as_of)
        version_id = self._next_version_id(conn)
        now = utcnow()
        id_map: dict[int, int] = {}
        ded_goal = {d["id"]: d["goal_id"] for d in state["deductions"]}
        for op in result["ops"]:
            if op["op"] == "insert":
                cur = conn.execute(
                    "INSERT INTO deductions (goal_id, amount_micros, scheduled_date, value_date, status,"
                    " recovery_condition, created_version, updated_version) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        op["goal_id"],
                        op["amount_micros"],
                        op["scheduled_date"],
                        op["value_date"],
                        op["status"],
                        json.dumps(op["recovery_condition"], ensure_ascii=False)
                        if op["recovery_condition"]
                        else None,
                        version_id,
                        version_id,
                    ),
                )
                id_map[op["temp_id"]] = cur.lastrowid
            elif op["op"] == "update":
                # WHERE 守卫：已执行扣款即使被误引用也不会被覆盖
                conn.execute(
                    "UPDATE deductions SET amount_micros = ?, scheduled_date = ?, value_date = ?,"
                    " status = ?, recovery_condition = ?, updated_version = ?"
                    " WHERE id = ? AND status IN ('pending','paused')",
                    (
                        op["amount_micros"],
                        op["scheduled_date"],
                        op["value_date"],
                        op["status"],
                        json.dumps(op["recovery_condition"], ensure_ascii=False)
                        if op["recovery_condition"]
                        else None,
                        version_id,
                        op["id"],
                    ),
                )
            elif op["op"] == "cancel":
                conn.execute(
                    "UPDATE deductions SET status = 'cancelled', updated_version = ?"
                    " WHERE id = ? AND status IN ('pending','paused')",
                    (version_id, op["id"]),
                )
        old_goals = {g["id"]: g for g in state["goals"]}
        goal_entries = []
        for gid, upd in result["goal_updates"].items():
            conn.execute(
                "UPDATE goals SET status = ?, eta_date = ?, eta_status = ?, updated_at = ? WHERE id = ?",
                (upd["status"], upd["eta_date"], upd["eta_status"], now, gid),
            )
            old = old_goals.get(gid)
            if old and (
                old["eta_date"] != upd["eta_date"]
                or old["eta_status"] != upd["eta_status"]
                or old["status"] != upd["status"]
            ):
                goal_entries.append(
                    {
                        "goal_id": gid,
                        "name": old["name"],
                        "old_eta": old["eta_date"],
                        "new_eta": upd["eta_date"],
                        "old_status": old["status"],
                        "new_status": upd["status"],
                        "eta_status": upd["eta_status"],
                    }
                )
        changes = {key: [id_map.get(i, i) for i in ids] for key, ids in result["changes"].items()}
        changes["goal_updates"] = goal_entries
        if extra_changes:
            changes.update(extra_changes)
        affected = set(result["goal_updates"].keys())
        for op in result["ops"]:
            if op["op"] == "insert":
                affected.add(op["goal_id"])
            else:
                gid = ded_goal.get(op.get("id"))
                if gid is not None:
                    affected.add(gid)
        changes["affected_goals"] = sorted(affected)
        return self._insert_version(conn, version_id, event_id, reason, changes, as_of)

    def replan_now(self, reason: str | None = None, as_of: str | None = None) -> dict:
        as_of_date = _parse_date(as_of, "as_of") if as_of else date.today()
        with self._tx() as conn:
            version = self._replan(conn, reason or "手动重排", as_of_date)
        return {"plan_version": version["id"], "reason": version["reason"], "changes": version["changes"]}

    def _apply_event(self, conn, event_type: str, payload: dict, occurred: date) -> list:
        """把事件载荷应用到画像/目标，返回变更记录（写入版本 changes）。"""
        applied: list[dict] = []
        if event_type == "income_change":
            new_income = _parse_money(_require(payload, "new_monthly_income"), "new_monthly_income")
            if new_income < 0:
                raise ServiceError(400, "new_monthly_income 不能为负")
            old = self._profile_row(conn)["monthly_income_micros"]
            conn.execute(
                "UPDATE profile SET monthly_income_micros = ?, updated_at = ? WHERE id = 1",
                (new_income, utcnow()),
            )
            applied.append(
                {
                    "field": "monthly_income",
                    "old": to_amount_str(old),
                    "new": to_amount_str(new_income),
                }
            )
        elif event_type == "race_postponed":
            goal_ids = payload.get("goal_ids")
            if not goal_ids:
                goal_ids = [payload.get("goal_id")]
            goal_ids = [int(g) for g in goal_ids if g is not None]
            if not goal_ids:
                raise ServiceError(400, "race_postponed 需要 goal_id 或 goal_ids")
            new_deadline = _parse_date(_require(payload, "new_deadline"), "new_deadline")
            for gid in goal_ids:
                goal = self._must_goal(conn, gid)
                conn.execute(
                    "UPDATE goals SET deadline = ?, updated_at = ? WHERE id = ?",
                    (new_deadline.isoformat(), utcnow(), gid),
                )
                applied.append(
                    {"goal_id": gid, "field": "deadline", "old": goal["deadline"], "new": new_deadline.isoformat()}
                )
        elif event_type == "emergency":
            amount = _parse_money(_require(payload, "amount"), "amount")
            if amount <= 0:
                raise ServiceError(400, "amount 必须大于 0")
            description = str(payload.get("description") or "临时医疗/紧急支出")
            goal_id = payload.get("goal_id")
            goal = None
            if goal_id is not None:
                goal = self._must_goal(conn, int(goal_id))
            else:
                goal = conn.execute(
                    "SELECT * FROM goals WHERE goal_type = 'emergency_reserve' AND status = 'active'"
                    " ORDER BY id LIMIT 1"
                ).fetchone()
            if goal is None:
                refill_months = int(payload.get("refill_months") or 6)
                refilled = planner.add_months(occurred, refill_months)
                deadline = planner.month_end(refilled.year, refilled.month)
                now = utcnow()
                cur = conn.execute(
                    "INSERT INTO goals (goal_type, name, target_micros, currency, deadline, priority,"
                    " created_at, updated_at) VALUES ('emergency_reserve', ?, ?, ?, ?, 1, ?, ?)",
                    (
                        description,
                        amount,
                        self._profile_row(conn)["currency"],
                        deadline.isoformat(),
                        now,
                        now,
                    ),
                )
                applied.append(
                    {
                        "goal_id": cur.lastrowid,
                        "field": "created",
                        "new": to_amount_str(amount),
                        "name": description,
                    }
                )
            else:
                new_target = goal["target_micros"] + amount
                conn.execute(
                    "UPDATE goals SET target_micros = ?, updated_at = ? WHERE id = ?",
                    (new_target, utcnow(), goal["id"]),
                )
                applied.append(
                    {
                        "goal_id": goal["id"],
                        "field": "target",
                        "old": to_amount_str(goal["target_micros"]),
                        "new": to_amount_str(new_target),
                        "description": description,
                    }
                )
        elif event_type == "goal_change":
            goal_id = int(_require(payload, "goal_id"))
            goal = self._must_goal(conn, goal_id)
            if "target" in payload:
                new_target = _parse_money(payload["target"], "target")
                if new_target <= 0:
                    raise ServiceError(400, "target 必须大于 0")
                conn.execute(
                    "UPDATE goals SET target_micros = ?, updated_at = ? WHERE id = ?",
                    (new_target, utcnow(), goal_id),
                )
                applied.append(
                    {
                        "goal_id": goal_id,
                        "field": "target",
                        "old": to_amount_str(goal["target_micros"]),
                        "new": to_amount_str(new_target),
                    }
                )
            if "deadline" in payload:
                new_deadline = _parse_date(payload["deadline"], "deadline")
                conn.execute(
                    "UPDATE goals SET deadline = ?, updated_at = ? WHERE id = ?",
                    (new_deadline.isoformat(), utcnow(), goal_id),
                )
                applied.append(
                    {"goal_id": goal_id, "field": "deadline", "old": goal["deadline"], "new": new_deadline.isoformat()}
                )
            if "priority" in payload:
                conn.execute(
                    "UPDATE goals SET priority = ?, updated_at = ? WHERE id = ?",
                    (int(payload["priority"]), utcnow(), goal_id),
                )
                applied.append(
                    {"goal_id": goal_id, "field": "priority", "old": goal["priority"], "new": int(payload["priority"])}
                )
            if "status" in payload:
                status = str(payload["status"])
                if status not in ("active", "cancelled"):
                    raise ServiceError(400, "status 仅支持 active / cancelled")
                conn.execute(
                    "UPDATE goals SET status = ?, updated_at = ? WHERE id = ?",
                    (status, utcnow(), goal_id),
                )
                entry: dict = {"goal_id": goal_id, "field": "status", "old": goal["status"], "new": status}
                if status == "cancelled":
                    # 目标取消时一并取消其未执行扣款，避免永远挂账
                    cur = conn.execute(
                        "UPDATE deductions SET status = 'cancelled'"
                        " WHERE goal_id = ? AND status IN ('pending','paused')",
                        (goal_id,),
                    )
                    entry["cancelled_deductions"] = cur.rowcount
                applied.append(entry)
            if not applied:
                raise ServiceError(400, "goal_change 未包含可应用的字段")
        elif event_type == "note":
            pass
        else:  # pragma: no cover - 入口已校验
            raise ServiceError(400, f"未知事件类型：{event_type}")
        return applied

    def record_event(self, data: dict) -> dict:
        data = data or {}
        event_type = _require(data, "event_type")
        if event_type not in self.domain["trigger_types"]:
            raise ServiceError(400, f"未知事件类型：{event_type}")
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            raise ServiceError(400, "payload 必须是对象")
        occurred = _parse_date(data["occurred_on"], "occurred_on") if data.get("occurred_on") else date.today()
        reason = str(data.get("reason") or DEFAULT_REASONS[event_type])
        with self._tx() as conn:
            applied = self._apply_event(conn, event_type, payload, occurred)
            cur = conn.execute(
                "INSERT INTO events (event_type, payload, reason, occurred_on, created_at) VALUES (?,?,?,?,?)",
                (event_type, json.dumps(payload, ensure_ascii=False), reason, occurred.isoformat(), utcnow()),
            )
            event_id = cur.lastrowid
            version = self._replan(
                conn,
                reason,
                occurred,
                event_id=event_id,
                extra_changes={
                    "trigger": {"event_id": event_id, "event_type": event_type, "applied": applied}
                },
            )
            conn.execute("UPDATE events SET plan_version_id = ? WHERE id = ?", (version["id"], event_id))
        return {
            "event_id": event_id,
            "plan_version": version["id"],
            "reason": reason,
            "applied": applied,
        }

    def list_events(self) -> list:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
            return [
                {
                    "id": r["id"],
                    "event_type": r["event_type"],
                    "payload": json.loads(r["payload"]),
                    "reason": r["reason"],
                    "occurred_on": r["occurred_on"],
                    "plan_version_id": r["plan_version_id"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # 扣款
    # ------------------------------------------------------------------
    def list_deductions(self, status: str | None = None, goal_id: int | None = None) -> list:
        sql = "SELECT * FROM deductions WHERE 1 = 1"
        params: list = []
        if status:
            if status not in self.domain["deduction_statuses"]:
                raise ServiceError(400, f"未知扣款状态：{status}")
            sql += " AND status = ?"
            params.append(status)
        if goal_id is not None:
            sql += " AND goal_id = ?"
            params.append(int(goal_id))
        sql += " ORDER BY value_date, id"
        with self._read() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._deduction_view(r) for r in rows]

    def resume_deduction(self, deduction_id: int) -> dict:
        """手动恢复被暂停的扣款；下一次事件重排仍可能按容量再次暂停。"""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM deductions WHERE id = ?", (deduction_id,)).fetchone()
            if row is None:
                raise ServiceError(404, f"扣款不存在：{deduction_id}")
            if row["status"] != "paused":
                raise ServiceError(409, "仅暂停状态的扣款可以恢复")
            version_id = self._next_version_id(conn)
            conn.execute(
                "UPDATE deductions SET status = 'pending', recovery_condition = NULL, updated_version = ?"
                " WHERE id = ?",
                (version_id, deduction_id),
            )
            changes = {
                "created": [],
                "updated": [],
                "cancelled": [],
                "paused": [],
                "resumed": [deduction_id],
                "goal_updates": [],
                "affected_goals": [row["goal_id"]],
                "manual_resume": True,
            }
            self._insert_version(conn, version_id, None, f"手动恢复扣款 #{deduction_id}", changes, date.today())
            view = self._deduction_view(
                conn.execute("SELECT * FROM deductions WHERE id = ?", (deduction_id,)).fetchone()
            )
        return {"deduction": view, "plan_version": version_id}

    def execute_deduction(self, deduction_id: int, data: dict) -> dict:
        """手动登记一笔扣款已执行（如线下转账）；执行后不再参与重排。"""
        data = data or {}
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM deductions WHERE id = ?", (deduction_id,)).fetchone()
            if row is None:
                raise ServiceError(404, f"扣款不存在：{deduction_id}")
            if row["status"] == "executed":
                raise ServiceError(409, "扣款已执行，不能重复执行")
            if row["status"] == "cancelled":
                raise ServiceError(409, "扣款已取消")
            amount = (
                _parse_money(data["amount"], "amount") if data.get("amount") is not None else row["amount_micros"]
            )
            if amount <= 0:
                raise ServiceError(400, "amount 必须大于 0")
            executed_at = str(data.get("posted_at") or utcnow())
            conn.execute(
                "UPDATE deductions SET status = 'executed', executed_amount_micros = ?, executed_at = ?"
                " WHERE id = ?",
                (amount, executed_at, deduction_id),
            )
            view = self._deduction_view(
                conn.execute("SELECT * FROM deductions WHERE id = ?", (deduction_id,)).fetchone()
            )
        return {"deduction": view}

    # ------------------------------------------------------------------
    # 银行流水（幂等导入）
    # ------------------------------------------------------------------
    def import_bank_transactions(self, data: dict) -> dict:
        data = data or {}
        account = str(data.get("account") or "default")
        txns = data.get("transactions")
        if not isinstance(txns, list) or not txns:
            raise ServiceError(400, "transactions 必须是非空数组")
        batch = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        items = []
        imported = duplicates = 0
        with self._tx() as conn:
            for txn in txns:
                if not isinstance(txn, dict):
                    raise ServiceError(400, "每条流水必须是对象")
                external_id = str(_require(txn, "external_id"))
                amount = _parse_money(_require(txn, "amount"), "amount")
                posted_at = str(txn.get("posted_at") or utcnow())
                value_date = str(txn.get("value_date") or posted_at[:10])
                currency = str(txn.get("currency") or "CNY").upper()
                deduction_id = txn.get("deduction_id")
                goal_id = txn.get("goal_id")
                memo = txn.get("memo")
                existing = conn.execute(
                    "SELECT id FROM bank_transactions WHERE account = ? AND external_id = ?",
                    (account, external_id),
                ).fetchone()
                if existing is not None:
                    duplicates += 1
                    items.append({"external_id": external_id, "status": "duplicate"})
                    continue
                cur = conn.execute(
                    "INSERT INTO bank_transactions (account, external_id, posted_at, value_date, amount_micros,"
                    " currency, goal_id, deduction_id, memo, import_batch, imported_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        account,
                        external_id,
                        posted_at,
                        value_date,
                        amount,
                        currency,
                        int(goal_id) if goal_id is not None else None,
                        int(deduction_id) if deduction_id is not None else None,
                        memo,
                        batch,
                        utcnow(),
                    ),
                )
                imported += 1
                item: dict = {"external_id": external_id, "status": "imported", "id": cur.lastrowid}
                if deduction_id is not None:
                    ded = conn.execute(
                        "SELECT * FROM deductions WHERE id = ?", (int(deduction_id),)
                    ).fetchone()
                    if ded is None:
                        item["note"] = "deduction not found"
                    elif ded["status"] in ("pending", "paused"):
                        conn.execute(
                            "UPDATE deductions SET status = 'executed', executed_amount_micros = ?,"
                            " executed_at = ?, bank_txn_id = ? WHERE id = ?",
                            (amount, posted_at, cur.lastrowid, int(deduction_id)),
                        )
                        item["matched_deduction"] = int(deduction_id)
                    else:
                        item["note"] = f"deduction status is {ded['status']}"
                items.append(item)
        return {
            "batch": batch,
            "account": account,
            "imported": imported,
            "duplicates": duplicates,
            "items": items,
        }

    def list_bank_transactions(self, account: str | None = None) -> list:
        sql = "SELECT * FROM bank_transactions"
        params: list = []
        if account:
            sql += " WHERE account = ?"
            params.append(account)
        sql += " ORDER BY value_date, id"
        with self._read() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._txn_view(r) for r in rows]

    # ------------------------------------------------------------------
    # 计划版本与导出
    # ------------------------------------------------------------------
    def list_versions(self) -> list:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM plan_versions ORDER BY id").fetchall()
            return [
                {
                    "id": r["id"],
                    "event_id": r["event_id"],
                    "reason": r["reason"],
                    "affected_goals": json.loads(r["changes"]).get("affected_goals", []),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def get_version(self, version_id: int) -> dict:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM plan_versions WHERE id = ?", (version_id,)).fetchone()
            if row is None:
                raise ServiceError(404, f"计划版本不存在：{version_id}")
            return {
                "id": row["id"],
                "event_id": row["event_id"],
                "reason": row["reason"],
                "changes": json.loads(row["changes"]),
                "created_at": row["created_at"],
            }

    def export_version(self, version_id: int) -> dict:
        """导出可供理财顾问复核的版本化计划（附校验和）。"""
        with self._read() as conn:
            row = conn.execute("SELECT * FROM plan_versions WHERE id = ?", (version_id,)).fetchone()
            if row is None:
                raise ServiceError(404, f"计划版本不存在：{version_id}")
            event = None
            if row["event_id"] is not None:
                ev = conn.execute("SELECT * FROM events WHERE id = ?", (row["event_id"],)).fetchone()
                if ev is not None:
                    event = {
                        "id": ev["id"],
                        "event_type": ev["event_type"],
                        "payload": json.loads(ev["payload"]),
                        "occurred_on": ev["occurred_on"],
                    }
            doc = {
                "export_kind": "family_training_savings_plan",
                "format_version": 1,
                "plan_version": row["id"],
                "generated_at": row["created_at"],
                "reason": row["reason"],
                "trigger": event,
                "changes": json.loads(row["changes"]),
                "plan": json.loads(row["snapshot"]),
                "value_date_rule": "扣款按银行时区结算日（价值日）入账，非工作日顺延；账期以价值日为准",
            }
        # 校验和只覆盖确定性内容，exported_at 不参与，保证同一版本重复导出一致
        checksum = hashlib.sha256(
            json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        doc["exported_at"] = utcnow()
        doc["checksum"] = checksum
        return doc

    # ------------------------------------------------------------------
    # 训练周期与时间轴
    # ------------------------------------------------------------------
    def add_training_block(self, data: dict) -> dict:
        data = data or {}
        name = str(_require(data, "name")).strip()
        if not name:
            raise ServiceError(400, "name 不能为空")
        start = _parse_date(_require(data, "start_date"), "start_date")
        end = _parse_date(_require(data, "end_date"), "end_date")
        if end < start:
            raise ServiceError(400, "end_date 不能早于 start_date")
        goal_ids = [int(g) for g in (data.get("goal_ids") or [])]
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO training_blocks (name, start_date, end_date, goal_ids, created_at) VALUES (?,?,?,?,?)",
                (name, start.isoformat(), end.isoformat(), json.dumps(goal_ids), utcnow()),
            )
            row = conn.execute("SELECT * FROM training_blocks WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {"block": self._block_view(row)}

    @staticmethod
    def _block_view(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "goal_ids": json.loads(row["goal_ids"]),
            "created_at": row["created_at"],
        }

    def list_training_blocks(self) -> list:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM training_blocks ORDER BY start_date, id").fetchall()
            return [self._block_view(r) for r in rows]

    def timeline(self, start: date, end: date) -> list:
        """统一时间轴：收入、固定支出、自动转入、目标截止日、事件与训练周期。"""
        if end < start:
            raise ServiceError(400, "to 不能早于 from")
        items: list[dict] = []
        with self._read() as conn:
            profile = self._profile_row(conn)
            for year, month in planner.iter_months(start, end):
                day = date(year, month, 1)
                if start <= day <= end:
                    items.append(
                        {
                            "date": day.isoformat(),
                            "kind": "income",
                            "label": "月度收入",
                            "amount": to_amount_str(profile["monthly_income_micros"]),
                            "currency": profile["currency"],
                        }
                    )
            for exp in conn.execute("SELECT * FROM fixed_expenses WHERE active = 1").fetchall():
                for year, month in planner.iter_months(start, end):
                    day = date(year, month, min(exp["day_of_month"], planner.month_end(year, month).day))
                    if start <= day <= end:
                        items.append(
                            {
                                "date": day.isoformat(),
                                "kind": "expense",
                                "label": exp["name"],
                                "amount": to_amount_str(exp["amount_micros"]),
                                "currency": profile["currency"],
                            }
                        )
            goal_names = {
                r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM goals").fetchall()
            }
            for row in conn.execute(
                "SELECT * FROM deductions WHERE status != 'cancelled' AND value_date BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            ).fetchall():
                items.append(
                    {
                        "date": row["value_date"],
                        "kind": "scheduled_transfer",
                        "label": goal_names.get(row["goal_id"], f"目标 #{row['goal_id']}"),
                        "goal_id": row["goal_id"],
                        "deduction_id": row["id"],
                        "amount": to_amount_str(row["amount_micros"]),
                        "status": row["status"],
                        "scheduled_date": row["scheduled_date"],
                        "value_date": row["value_date"],
                    }
                )
            for row in conn.execute(
                "SELECT * FROM goals WHERE deadline BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            ).fetchall():
                items.append(
                    {
                        "date": row["deadline"],
                        "kind": "goal_deadline",
                        "label": row["name"],
                        "goal_id": row["id"],
                        "goal_type": row["goal_type"],
                        "status": row["status"],
                    }
                )
            for row in conn.execute(
                "SELECT * FROM events WHERE occurred_on BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            ).fetchall():
                items.append(
                    {
                        "date": row["occurred_on"],
                        "kind": EVENT_KIND_MAP.get(row["event_type"], "goal_change"),
                        "subtype": row["event_type"],
                        "label": row["reason"],
                        "event_id": row["id"],
                    }
                )
            for row in conn.execute("SELECT * FROM training_blocks WHERE end_date >= ? AND start_date <= ?",
                                    (start.isoformat(), end.isoformat())).fetchall():
                items.append(
                    {
                        "date": row["start_date"],
                        "end_date": row["end_date"],
                        "kind": "training_block",
                        "label": row["name"],
                        "block_id": row["id"],
                    }
                )
        items.sort(key=lambda item: (item["date"], item["kind"]))
        return items

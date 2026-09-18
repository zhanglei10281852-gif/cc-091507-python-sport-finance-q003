"""服务层：输入校验、命令处理与查询，HTTP 层只做路由。

金额输入接受数字/字符串，内部一律转为最小货币单位整数；
输出同时提供 *_minor（整数）与格式化字符串。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Optional

import banking
from models import (
    FixedExpense,
    Goal,
    Income,
    LedgerEntry,
    OneOffExpense,
    RaceEvent,
    TrainingPhase,
)
from money import format_money, to_minor
from planner import Planner, public_projection
from store import Store

REFERENCE = json.loads(
    (Path(__file__).resolve().parents[1] / "reference" / "domain.json").read_text("utf-8")
)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Service:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.planner = Planner(store)

    # ------------------------------------------------------------- helpers
    def _replan(self, trigger_type: str, reason: str, payload: Optional[dict] = None) -> dict[str, Any]:
        r = self.planner.replan(trigger_type, reason, payload)
        if "projection" in r:
            r = dict(r)
            r["projection"] = public_projection(r["projection"])
        return r

    def reference(self) -> dict[str, Any]:
        return REFERENCE

    def _get_goal(self, goal_id: str) -> dict[str, Any]:
        goal = self.store.state["goals"].get(goal_id)
        if not goal:
            raise ApiError(404, f"目标不存在: {goal_id}")
        return goal

    def _amount(self, body: dict[str, Any], key: str = "amount") -> int:
        if key not in body:
            raise ApiError(400, f"缺少金额字段: {key}")
        try:
            return to_minor(body[key])
        except Exception as exc:
            raise ApiError(400, f"金额无法解析: {body[key]}") from exc

    def _money_view(self, minor: int, currency: str) -> dict[str, Any]:
        return {"amount": format_money(minor), "amount_minor": minor, "currency": currency}

    def _goal_view(self, g: dict[str, Any], projection: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        view = dict(g)
        view["target_amount_minor"] = int(g["target_amount"])
        view["target_amount"] = format_money(int(g["target_amount"]))
        funded = self.planner._executed_funding().get(g["id"], 0)
        view["funded_minor"] = funded
        view["funded"] = format_money(funded)
        view["remaining_minor"] = max(int(g["target_amount"]) - funded, 0)
        if projection is None:
            projection = self.planner._project()
        view["eta"] = projection["eta"].get(g["id"])
        return view

    def _ded_view(self, d: dict[str, Any], projection: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        view = {k: v for k, v in d.items()}
        view["amount"] = format_money(int(d["amount"]))
        view["goal_name"] = self.store.state["goals"].get(d["goal_id"], {}).get("name", "")
        if d["status"] == "paused":
            cond = self.planner.condition_status(d, projection or self.planner._project())
            view["resume"] = cond
        return view

    # -------------------------------------------------------------- config
    def configure(self, body: dict[str, Any]) -> dict[str, Any]:
        cfg = self.store.state["config"]
        allowed = {
            "plan_start_month", "plan_end_month", "default_currency",
            "default_tz", "transfer_day", "cutoff_hour", "holidays",
            "priority_overrides", "as_of_date",
        }
        for key in allowed:
            if key in body:
                cfg[key] = body[key]
        for m_key in ("plan_start_month", "plan_end_month", "as_of_date"):
            if m_key in cfg and cfg[m_key]:
                self._validate_month_or_date(m_key, cfg[m_key])
        self.store.save()
        return {"config": cfg}

    @staticmethod
    def _validate_month_or_date(key: str, value: str) -> None:
        try:
            if key == "as_of_date":
                date.fromisoformat(value)
            else:
                datetime.strptime(value, "%Y-%m")
        except ValueError as exc:
            raise ApiError(400, f"{key} 格式应为 YYYY-MM 或 YYYY-MM-DD") from exc

    # --------------------------------------------------------------- goals
    def create_goal(self, body: dict[str, Any]) -> dict[str, Any]:
        for key in ("name", "type", "deadline", "account"):
            if not body.get(key):
                raise ApiError(400, f"缺少字段: {key}")
        gtype = body["type"]
        if gtype not in REFERENCE["goal_types"]:
            raise ApiError(400, f"未知目标类型: {gtype}")
        currency = body.get("currency", self.store.state["config"].get("default_currency", "CNY"))
        if currency not in REFERENCE["currencies"]:
            raise ApiError(400, f"不支持的币种: {currency}")
        date.fromisoformat(body["deadline"])  # 校验
        priority = int(body.get("priority", REFERENCE["default_priority"].get(gtype, 99)))
        goal = Goal(
            id=body.get("id") or _uid("goal"),
            name=body["name"],
            type=gtype,
            target_amount=self._amount(body, "target_amount"),
            currency=currency,
            deadline=body["deadline"],
            priority=priority,
            created_month=body.get("created_month", body["deadline"][:7]),
            race_id=body.get("race_id"),
            account=body["account"],
        )
        if goal.id in self.store.state["goals"]:
            raise ApiError(409, f"目标已存在: {goal.id}")
        if goal.race_id and goal.race_id not in self.store.state["races"]:
            raise ApiError(400, f"关联比赛不存在: {goal.race_id}")
        self.store.state["goals"][goal.id] = goal.to_dict()
        self.store.save()
        return {"goal": self._goal_view(self.store.state["goals"][goal.id])}

    def list_goals(self) -> dict[str, Any]:
        projection = self.planner._project()
        return {"goals": [self._goal_view(g, projection) for g in self.store.state["goals"].values()]}

    def goal_detail(self, goal_id: str) -> dict[str, Any]:
        return {"goal": self._goal_view(self._get_goal(goal_id))}

    def goal_funding(self, goal_id: str) -> dict[str, Any]:
        """查询某目标的资金来源、被暂停扣款与恢复条件。"""
        g = self._get_goal(goal_id)
        projection = self.planner._project()
        executed = [e for e in self.store.state["ledger"] if e["goal_id"] == goal_id]
        executed_total = sum(int(e["amount"]) for e in executed)

        planned_raw = [d for d in self.store.state["deductions"].values()
                       if d["goal_id"] == goal_id and d["status"] == "scheduled"]
        paused_raw = [d for d in self.store.state["deductions"].values()
                      if d["goal_id"] == goal_id and d["status"] == "paused"]
        cancelled_raw = [d for d in self.store.state["deductions"].values()
                         if d["goal_id"] == goal_id and d["status"] == "cancelled"]
        planned = [self._ded_view(d, projection) for d in planned_raw]
        paused = [self._ded_view(d, projection) for d in paused_raw]
        cancelled = [self._ded_view(d, projection) for d in cancelled_raw]
        future_total = sum(int(d["amount"]) for d in planned_raw)
        by_account: dict[str, int] = {}
        for e in executed:
            by_account[e["account"]] = by_account.get(e["account"], 0) + int(e["amount"])

        return {
            "goal": self._goal_view(g, projection),
            "funding": {
                "executed_total": self._money_view(executed_total, g["currency"]),
                "scheduled_total": self._money_view(future_total, g["currency"]),
                "gap_to_target": self._money_view(
                    max(int(g["target_amount"]) - executed_total - future_total, 0), g["currency"]
                ),
                "sources_executed": [
                    {
                        "entry_id": e["id"],
                        "account": e["account"],
                        "value_date": e["value_date"],
                        "value_month": e["value_month"],
                        "business_time": e["business_time"],
                        "source": e["source"],
                        "external_id": e["external_id"],
                        "description": e.get("description", ""),
                        **self._money_view(int(e["amount"]), e["currency"]),
                    }
                    for e in sorted(executed, key=lambda x: x["value_date"])
                ],
                "sources_planned": planned,
                "funded_by_account": [
                    {"account": acc, **self._money_view(amt, g["currency"])}
                    for acc, amt in sorted(by_account.items())
                ],
            },
            "paused_deductions": paused,
            "cancelled_deductions": cancelled,
            "eta": projection["eta"].get(goal_id),
        }

    # -------------------------------------------------------------- income
    def upsert_income(self, body: dict[str, Any], trigger: bool = False,
                      reason: str = "") -> dict[str, Any]:
        for key in ("month", "account"):
            if not body.get(key):
                raise ApiError(400, f"缺少字段: {key}")
        datetime.strptime(body["month"], "%Y-%m")
        income = Income(
            month=body["month"],
            account=body["account"],
            amount=self._amount(body),
            currency=body.get("currency", self.store.state["config"].get("default_currency", "CNY")),
            tz=body.get("tz", self.store.state["config"].get("default_tz", "Asia/Shanghai")),
            source=body.get("source", "salary"),
            day_of_month=int(body.get("day_of_month", 10)),
        )
        self.store.state["incomes"][f"{income.month}|{income.account}"] = income.to_dict()
        self.store.save()
        result: dict[str, Any] = {"income": income.to_dict()}
        if trigger:
            result["replan"] = self._replan(
                "income_change",
                reason or f"{income.account} 自 {income.month} 起收入调整",
                {"account": income.account, "month": income.month, "new_amount_minor": income.amount},
            )
        return result

    # ------------------------------------------------------- fixed expenses
    def create_fixed_expense(self, body: dict[str, Any]) -> dict[str, Any]:
        for key in ("name", "category", "account", "start_month"):
            if not body.get(key):
                raise ApiError(400, f"缺少字段: {key}")
        if body["category"] not in REFERENCE["expense_categories"]:
            raise ApiError(400, f"未知支出类别: {body['category']}")
        datetime.strptime(body["start_month"], "%Y-%m")
        if body.get("end_month"):
            datetime.strptime(body["end_month"], "%Y-%m")
        exp = FixedExpense(
            id=body.get("id") or _uid("fix"),
            name=body["name"],
            category=body["category"],
            account=body["account"],
            amount=self._amount(body),
            currency=body.get("currency", self.store.state["config"].get("default_currency", "CNY")),
            start_month=body["start_month"],
            end_month=body.get("end_month"),
            day_of_month=int(body.get("day_of_month", 1)),
            tz=body.get("tz", self.store.state["config"].get("default_tz", "Asia/Shanghai")),
        )
        if exp.id in self.store.state["fixed_expenses"]:
            raise ApiError(409, f"固定支出已存在: {exp.id}")
        self.store.state["fixed_expenses"][exp.id] = exp.to_dict()
        self.store.save()
        return {"fixed_expense": exp.to_dict()}

    # -------------------------------------------------------- one-off costs
    def add_one_off_expense(self, body: dict[str, Any], replan: bool = True,
                            reason: str = "") -> dict[str, Any]:
        for key in ("name", "category", "account", "business_time", "tz"):
            if key not in body:
                raise ApiError(400, f"缺少字段: {key}")
        if body["category"] not in REFERENCE["expense_categories"]:
            raise ApiError(400, f"未知支出类别: {body['category']}")
        cfg = self.store.state["config"]
        holidays = frozenset(date.fromisoformat(h) for h in cfg.get("holidays", []))
        cutoff = time(int(cfg.get("cutoff_hour", 17)), 0)
        bdt = banking.parse_business_dt(body["business_time"], body["tz"])
        vd = banking.value_date(bdt, body["tz"], cutoff=cutoff, holidays=holidays)
        exp = OneOffExpense(
            id=body.get("id") or _uid("one"),
            name=body["name"],
            category=body["category"],
            account=body["account"],
            amount=self._amount(body),
            currency=body.get("currency", cfg.get("default_currency", "CNY")),
            value_month=vd.posting_month,
            business_time=body["business_time"],
            tz=body["tz"],
            value_date=vd.value_date.isoformat(),
        )
        if exp.id in self.store.state["one_off_expenses"]:
            raise ApiError(409, f"临时支出已存在: {exp.id}")
        self.store.state["one_off_expenses"][exp.id] = exp.to_dict()
        self.store.save()
        result: dict[str, Any] = {
            "one_off_expense": exp.to_dict(),
            "value_date": vd.value_date.isoformat(),
            "posting_month": vd.posting_month,
        }
        if replan:
            result["replan"] = self._replan(
                "medical_expense" if exp.category == "medical" else "manual",
                reason or f"临时支出「{exp.name}」按价值日 {vd.value_date} 进入 {vd.posting_month} 现金流",
                {"expense_id": exp.id, "value_date": exp.value_date, "amount_minor": exp.amount},
            )
        return result

    # --------------------------------------------------- phases & races
    def create_training_phase(self, body: dict[str, Any]) -> dict[str, Any]:
        for key in ("name", "start_date", "end_date"):
            if not body.get(key):
                raise ApiError(400, f"缺少字段: {key}")
        phase = TrainingPhase(
            id=body.get("id") or _uid("phase"),
            name=body["name"],
            start_date=body["start_date"],
            end_date=body["end_date"],
            intensity=body.get("intensity", "base"),
        )
        date.fromisoformat(phase.start_date)
        date.fromisoformat(phase.end_date)
        if phase.end_date < phase.start_date:
            raise ApiError(400, "训练周期结束日早于开始日")
        self.store.state["training_phases"][phase.id] = phase.to_dict()
        self.store.save()
        return {"training_phase": phase.to_dict()}

    def create_race(self, body: dict[str, Any]) -> dict[str, Any]:
        for key in ("name", "race_date"):
            if not body.get(key):
                raise ApiError(400, f"缺少字段: {key}")
        date.fromisoformat(body["race_date"])
        race = RaceEvent(
            id=body.get("id") or _uid("race"),
            name=body["name"],
            race_date=body["race_date"],
            tz=body.get("tz", self.store.state["config"].get("default_tz", "Asia/Shanghai")),
            location=body.get("location", ""),
        )
        self.store.state["races"][race.id] = race.to_dict()
        self.store.save()
        return {"race": race.to_dict()}

    def postpone_race(self, race_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """比赛延期：更新比赛日及关联目标截止日，然后按优先级重排未来扣款。"""
        race = self.store.state["races"].get(race_id)
        if not race:
            raise ApiError(404, f"比赛不存在: {race_id}")
        new_date = body.get("new_date") or body.get("race_date")
        date.fromisoformat(new_date)
        if new_date <= race["race_date"]:
            raise ApiError(400, "延期日期必须晚于当前比赛日")
        old_date = race["race_date"]
        race["race_date"] = new_date
        linked = []
        for g in self.store.state["goals"].values():
            if g.get("race_id") == race_id and g["deadline"] <= new_date:
                g["deadline"] = new_date
                linked.append(g["id"])
        self.store.save()
        replan = self._replan(
            "race_postponed",
            body.get("reason", f"比赛「{race['name']}」由 {old_date} 延期至 {new_date}"),
            {"race_id": race_id, "old_date": old_date, "new_date": new_date,
             "affected_goal_ids": linked},
        )
        return {"race": race, "linked_goals": linked, "replan": replan}

    # ------------------------------------------------------------- ledger
    def _append_ledger_entry(
        self,
        *,
        goal_id: str,
        account: str,
        amount: int,
        currency: str,
        business_time: str,
        tz: str,
        source: str,
        external_id: Optional[str],
        dedupe_key: str,
        batch: Optional[str],
        description: str,
        deduction_id: Optional[str] = None,
    ) -> LedgerEntry:
        cfg = self.store.state["config"]
        holidays = frozenset(date.fromisoformat(h) for h in cfg.get("holidays", []))
        cutoff = time(int(cfg.get("cutoff_hour", 17)), 0)
        bdt = banking.parse_business_dt(business_time, tz)
        vd = banking.value_date(bdt, tz, cutoff=cutoff, holidays=holidays)
        entry = LedgerEntry(
            id=f"led_{dedupe_key[:16]}",
            goal_id=goal_id,
            account=account,
            amount=amount,
            currency=currency,
            value_date=vd.value_date.isoformat(),
            value_month=vd.posting_month,
            business_time=business_time,
            source=source,
            external_id=external_id,
            import_batch=batch,
            dedupe_key=dedupe_key,
            description=description,
            deduction_id=deduction_id,
        )
        self.store.state["ledger"].append(entry.to_dict())
        return entry

    def _match_deduction(self, goal_id: str, value_month: str) -> Optional[str]:
        """把实际入账匹配到同目标同计划月份最早的 scheduled/paused 扣款。"""
        candidates = [
            d for d in self.store.state["deductions"].values()
            if d["goal_id"] == goal_id and d["month"] == value_month
            and d["status"] in ("scheduled", "paused")
        ]
        if candidates:
            return sorted(candidates, key=lambda d: d["id"])[0]["id"]
        return None

    def manual_transfer(self, body: dict[str, Any]) -> dict[str, Any]:
        g = self._get_goal(body.get("goal_id", ""))
        if not body.get("account") or not body.get("business_time"):
            raise ApiError(400, "缺少 account 或 business_time")
        amount = self._amount(body)
        if amount <= 0:
            raise ApiError(400, "金额必须为正")
        tz = body.get("tz", self.store.state["config"].get("default_tz", "Asia/Shanghai"))
        dedupe = hashlib.sha256(
            f"manual|{g['id']}|{body['account']}|{body['business_time']}|{amount}".encode()
        ).hexdigest()
        if dedupe in self.store.state["imports"]:
            return {"duplicate": True, "imports": self.store.state["imports"][dedupe]}
        cfg = self.store.state["config"]
        holidays = frozenset(date.fromisoformat(h) for h in cfg.get("holidays", []))
        vd = banking.value_date(banking.parse_business_dt(body["business_time"], tz), tz,
                                cutoff=time(int(cfg.get("cutoff_hour", 17)), 0), holidays=holidays)
        match_id = self._match_deduction(g["id"], vd.posting_month)
        entry = self._append_ledger_entry(
            goal_id=g["id"], account=body["account"], amount=amount,
            currency=body.get("currency", g["currency"]),
            business_time=body["business_time"], tz=tz, source="manual",
            external_id=body.get("external_id"), dedupe_key=dedupe,
            batch=body.get("batch_id"), description=body.get("description", "手工登记"),
            deduction_id=match_id,
        )
        self.store.state["imports"][dedupe] = {"kind": "ledger", "id": entry.id}
        replan_result = self._replan(
            "manual", "手工登记已执行转账，按实际入账重排未来扣款",
            {"entry_id": entry.id, "matched_deduction_id": match_id},
        )
        self.store.save()
        return {"entry": entry.to_dict(), "duplicate": False,
                "matched_deduction_id": match_id, "replan": replan_result}

    def bank_import(self, body: dict[str, Any]) -> dict[str, Any]:
        """幂等导入银行流水：account+external_id（缺失则行内容哈希）去重。

        重复导入整批中已存在的行全部跳过、不报错。
        """
        entries = body.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ApiError(400, "entries 必须是非空数组")
        batch = body.get("batch_id") or f"batch_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        default_tz = body.get("tz", self.store.state["config"].get("default_tz", "Asia/Shanghai"))
        imported, duplicates = [], []

        with self.store.lock():
            for raw in entries:
                for key in ("account", "business_time", "goal_id"):
                    if not raw.get(key):
                        raise ApiError(400, f"流水行缺少字段: {key}")
                g = self._get_goal(raw["goal_id"])
                amount = self._amount(raw)
                if amount <= 0:
                    raise ApiError(400, "流水金额必须为正")
                tz = raw.get("tz", default_tz)
                ext = raw.get("external_id")
                if ext:
                    dedupe = hashlib.sha256(f"{raw['account']}|{ext}".encode()).hexdigest()
                else:
                    basis = "|".join(str(raw.get(k, "")) for k in
                                     ("account", "business_time", "amount", "currency",
                                      "counterparty", "description", "goal_id"))
                    dedupe = hashlib.sha256(basis.encode()).hexdigest()
                if dedupe in self.store.state["imports"]:
                    duplicates.append({"external_id": ext, "dedupe_key": dedupe,
                                       "existing": self.store.state["imports"][dedupe]})
                    continue
                match_id = self._match_deduction(g["id"], self._value_month(raw["business_time"], tz))
                entry = self._append_ledger_entry(
                    goal_id=g["id"], account=raw["account"], amount=amount,
                    currency=raw.get("currency", g["currency"]),
                    business_time=raw["business_time"], tz=tz, source="bank_import",
                    external_id=ext, dedupe_key=dedupe, batch=batch,
                    description=raw.get("description", ""),
                    deduction_id=match_id,
                )
                self.store.state["imports"][dedupe] = {"kind": "ledger", "id": entry.id,
                                                       "batch_id": batch}
                imported.append(entry.to_dict())

            replan_result = None
            if imported:
                replan_result = self._replan(
                    "bank_import",
                    f"银行流水批次 {batch} 导入 {len(imported)} 笔，按价值日重排未来扣款",
                    {"batch_id": batch, "imported": len(imported), "duplicates": len(duplicates)},
                )
            self.store.save()

        return {
            "batch_id": batch,
            "received": len(entries),
            "imported": len(imported),
            "duplicates": len(duplicates),
            "imported_entries": imported,
            "duplicate_entries": duplicates,
            "replan": replan_result,
        }

    def _value_month(self, business_time: str, tz: str) -> str:
        cfg = self.store.state["config"]
        holidays = frozenset(date.fromisoformat(h) for h in cfg.get("holidays", []))
        vd = banking.value_date(
            banking.parse_business_dt(business_time, tz), tz,
            cutoff=time(int(cfg.get("cutoff_hour", 17)), 0), holidays=holidays,
        )
        return vd.posting_month

    # -------------------------------------------------- deduction commands
    def _get_deduction(self, deduction_id: str) -> dict[str, Any]:
        d = self.store.state["deductions"].get(deduction_id)
        if not d:
            raise ApiError(404, f"扣款不存在: {deduction_id}")
        return d

    def pause_deduction(self, deduction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """手动暂停：重排不得自动恢复，只记录恢复条件。"""
        d = self._get_deduction(deduction_id)
        if d["status"] not in ("scheduled", "paused"):
            raise ApiError(409, f"扣款状态为 {d['status']}，不可暂停")
        d["status"] = "paused"
        d["paused_at"] = self.planner._now()
        d["pause_reason"] = body.get("reason", "用户手动暂停")
        d["resume_when"] = body.get("resume_when") or {
            "type": "manual",
            "detail": body.get("resume_detail", "等待用户手动恢复"),
        }
        d["pinned"] = False
        self.store.save()
        return {"deduction": self._ded_view(d)}

    def resume_deduction(self, deduction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        d = self._get_deduction(deduction_id)
        if d["status"] != "paused":
            raise ApiError(409, f"扣款状态为 {d['status']}，不可恢复")
        pin = bool(body.get("pin", True))
        amount = self._amount(body, "amount") if body.get("amount") else int(d["amount"])
        d["status"] = "scheduled"
        d["amount"] = amount
        d["paused_at"] = ""
        d["pause_reason"] = ""
        d["resume_when"] = {}
        d["pinned"] = pin  # 手动恢复的实例被强制保留，后续瀑布不会再把它挤掉
        self.store.save()
        return {"deduction": self._ded_view(d),
                "note": "已强制保留" if pin else "未锁定，下次重排可能再次调整"}

    def pin_deduction(self, deduction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        d = self._get_deduction(deduction_id)
        if d["status"] != "scheduled":
            raise ApiError(409, "只有 scheduled 状态的扣款可锁定")
        d["pinned"] = bool(body.get("pinned", True))
        self.store.save()
        return {"deduction": self._ded_view(d)}

    def list_deductions(self, status_filter: Optional[str] = None) -> dict[str, Any]:
        projection = self.planner._project()
        items = []
        for d in self.store.state["deductions"].values():
            if status_filter and d["status"] != status_filter:
                continue
            items.append(self._ded_view(d, projection))
        items.sort(key=lambda x: (x["month"], x["status"], x["id"]))
        return {"deductions": items, "count": len(items)}

    def trigger_replan(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._replan(
            body.get("trigger_type", "manual"),
            body.get("reason", "手动触发重排"),
            body.get("payload", {}),
        )

    # -------------------------------------------------------------- timeline
    def timeline(self, from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for phase in self.store.state["training_phases"].values():
            items.append({"date": phase["start_date"], "type": "training_phase_start",
                          "id": phase["id"], "name": phase["name"], "intensity": phase["intensity"],
                          "end_date": phase["end_date"]})
            items.append({"date": phase["end_date"], "type": "training_phase_end",
                          "id": phase["id"], "name": phase["name"], "intensity": phase["intensity"]})
        for race in self.store.state["races"].values():
            items.append({"date": race["race_date"], "type": "race", **race})
        for g in self.store.state["goals"].values():
            items.append({"date": g["deadline"], "type": "goal_deadline", "goal_id": g["id"],
                          "name": g["name"], "goal_type": g["type"]})
        cfg = self.store.state["config"]
        start_m = cfg.get("plan_start_month")
        end_m = cfg.get("plan_end_month")
        if start_m and end_m:
            for month in banking.month_iter(start_m, end_m):
                for inc in self.store.state["incomes"].values():
                    if inc["month"] == month:
                        items.append({"date": f"{month}-{inc['day_of_month']:02d}",
                                      "type": "income", **inc})
                for exp in self.store.state["fixed_expenses"].values():
                    if exp["start_month"] <= month and (not exp["end_month"] or month <= exp["end_month"]):
                        items.append({"date": f"{month}-{exp['day_of_month']:02d}",
                                      "type": "fixed_expense", **exp})
        for e in self.store.state["one_off_expenses"].values():
            items.append({"date": e["value_date"], "type": "one_off_expense", **e})
        for e in self.store.state["ledger"]:
            items.append({"date": e["value_date"], "type": "executed_transfer", **e})
        for d in self.store.state["deductions"].values():
            if d["status"] in ("scheduled", "paused"):
                items.append({
                    "date": f"{d['month']}-{d['scheduled_day']:02d}",
                    "type": f"deduction_{d['status']}",
                    **d,
                })
            elif d["status"] == "executed" and d.get("executed_at"):
                items.append({"date": d["executed_at"][:10], "type": "deduction_executed", **d})

        if from_date:
            items = [i for i in items if i["date"] >= from_date]
        if to_date:
            items = [i for i in items if i["date"] <= to_date]
        items.sort(key=lambda x: (x["date"], x["type"]))
        return {"timeline": items, "count": len(items)}

    # ------------------------------------------------------- audit/exports
    def list_adjustments(self) -> dict[str, Any]:
        return {"adjustments": list(reversed(self.store.state["adjustments"])),
                "plan_version": self.store.state["plan_version"]}

    def adjustment_detail(self, adjustment_id: str) -> dict[str, Any]:
        for adj in self.store.state["adjustments"]:
            if adj["id"] == adjustment_id or str(adj["seq"]) == adjustment_id:
                return {"adjustment": adj}
        raise ApiError(404, f"调整记录不存在: {adjustment_id}")

    def list_exports(self) -> dict[str, Any]:
        return {"exports": list(reversed(self.store.state["exports"])),
                "plan_version": self.store.state["plan_version"]}

    def export_snapshot(self, version: int) -> dict[str, Any]:
        path = self.store.dir / "exports" / f"plan_v{version}.json"
        if not path.exists():
            raise ApiError(404, f"导出版本不存在: v{version}")
        return json.loads(path.read_text("utf-8"))

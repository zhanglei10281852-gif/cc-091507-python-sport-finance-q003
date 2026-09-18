"""核心规划器：月度现金流瀑布、扣款重排、暂停/恢复、ETA 与版本快照。

不变量：
- 账本（ledger）append-only，已执行转账永远不被重排修改；
- 重排只动 scheduled / paused 的未来扣款；executed 与 cancelled 不可变；
- 每次真正改变计划的重排生成 Adjustment，plan_version 递增并导出不可变快照。
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from datetime import date, datetime
from typing import Any, Optional

from banking import month_iter, shift_month
from models import Adjustment, Deduction, Goal


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def public_projection(p: dict[str, Any]) -> dict[str, Any]:
    """把内部投影（含 tuple 键）转为 JSON 安全结构。"""
    accounts = sorted({k[0] for k in p["discretionary"]})
    return {
        "months": p["months"],
        "future_months": p["future_months"],
        "eta": p["eta"],
        "funded": p["funded"],
        "monthly": [
            {
                "month": m,
                "discretionary": {a: p["discretionary"].get((a, m)) for a in accounts},
                "surplus": {a: p["surplus"].get((a, m)) for a in accounts},
                "allocations": [
                    {"goal_id": g, "amount_minor": a}
                    for (g, mm), a in sorted(p["alloc"].items()) if mm == m
                ],
            }
            for m in p["future_months"]
        ],
    }


class Planner:
    def __init__(self, store: Any) -> None:
        self.store = store

    # ------------------------------------------------------------------ utils
    @property
    def state(self) -> dict[str, Any]:
        return self.store.state

    def _cfg(self) -> dict[str, Any]:
        return self.state["config"]

    def _as_of_month(self) -> str:
        raw = self._cfg().get("as_of_date")
        base = date.fromisoformat(raw) if raw else date.today()
        return f"{base.year:04d}-{base.month:02d}"

    def _now(self) -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _goals(self) -> list[Goal]:
        overrides = self._cfg().get("priority_overrides", {})
        goals = [Goal.from_dict(g) for g in self.state["goals"].values()]
        for g in goals:
            if g.id in overrides:
                g.priority = int(overrides[g.id])
        return goals

    def _executed_funding(self) -> dict[str, int]:
        """每个目标已入账金额（事实，永远不被重算覆盖）。"""
        totals: dict[str, int] = {}
        for e in self.state["ledger"]:
            totals[e["goal_id"]] = totals.get(e["goal_id"], 0) + int(e["amount"])
        return totals

    def _income_for(self, account: str, month: str) -> int:
        """缺失月份沿用最近一个已登记月份的金额（薪资按月延续）。"""
        incomes = self.state["incomes"]
        if f"{month}|{account}" in incomes:
            return int(incomes[f"{month}|{account}"]["amount"])
        cursor = month
        for _ in range(1200):
            cursor = shift_month(cursor, -1)
            key = f"{cursor}|{account}"
            if key in incomes:
                return int(incomes[key]["amount"])
            if self._cfg()["plan_start_month"] and cursor < self._cfg()["plan_start_month"]:
                break
        return 0

    def _fixed_for(self, account: str, month: str) -> int:
        total = 0
        for e in self.state["fixed_expenses"].values():
            if e["account"] != account:
                continue
            if e["start_month"] <= month and (not e["end_month"] or month <= e["end_month"]):
                total += int(e["amount"])
        return total

    def _oneoff_for(self, account: str, month: str) -> int:
        return sum(
            int(e["amount"])
            for e in self.state["one_off_expenses"].values()
            if e["account"] == account and e["value_month"] == month
        )

    # -------------------------------------------------------------- reconcile
    def _reconcile(self) -> None:
        """银行/手工事实翻转计划状态；事实优先。"""
        deductions = self.state["deductions"]
        funded = self._executed_funding()
        for e in self.state["ledger"]:
            did = e.get("deduction_id")
            if did and did in deductions and deductions[did]["status"] != "executed":
                d = deductions[did]
                d["status"] = "executed"
                d["executed_at"] = e["business_time"]
                d["resume_when"] = {}
                d["pause_reason"] = ""
                if int(e["amount"]) != int(d["amount"]):
                    d["note"] = (d.get("note", "") + " 实际入账金额与计划不同，以账本为准。").strip()
        for goal in self._goals():
            if goal.status == "active" and funded.get(goal.id, 0) >= goal.target_amount:
                self.state["goals"][goal.id]["status"] = "completed"

    def _due_day(self, month: str) -> int:
        """实例在该月内的实际扣款日；当月已过配置转账日则顺延到明天（封顶 28）。"""
        cfg = self._cfg()
        day = int(cfg.get("transfer_day", 5))
        if month == self._as_of_month():
            raw = cfg.get("as_of_date")
            today = date.fromisoformat(raw) if raw else date.today()
            if day <= today.day:
                day = min(today.day + 1, 28)
        return day

    # ------------------------------------------------------------- projection
    def _project(self) -> dict[str, Any]:
        """按月度瀑布计算理想分配、ETA、每月可支配资金与结余。

        瀑布顺序（同一账户每月）：
          收入 → 固定支出/临时支出 → 按优先级（数字小者优先）补给各目标，
          每个目标按截止前剩余月份均摊，当月资金不足则低优先级目标当月分配为 0。
        """
        cfg = self._cfg()
        start, end = cfg["plan_start_month"], cfg["plan_end_month"]
        current = self._as_of_month()
        months = month_iter(start, end) if start and end else []
        future = [m for m in months if m >= current]

        funded = self._executed_funding()
        goals = [g for g in self._goals() if g.status == "active"]
        accounts = sorted({g.account for g in goals if g.account})

        # 手动暂停（type=manual）的月份不参与自动分配；用户强制保留的扣款先占资金
        blocked: set[tuple[str, str]] = set()
        pinned: dict[tuple[str, str], int] = {}
        for d in self.state["deductions"].values():
            if d["status"] == "paused" and (d.get("resume_when") or {}).get("type") == "manual":
                blocked.add((d["goal_id"], d["month"]))
            if d["status"] == "scheduled" and d.get("pinned"):
                pinned[(d["goal_id"], d["month"])] = int(d["amount"])

        alloc: dict[tuple[str, str], int] = {}
        desired_map: dict[tuple[str, str], int] = {}
        discretionary: dict[tuple[str, str], int] = {}
        surplus: dict[tuple[str, str], int] = {}
        cumulative = dict(funded)
        eta: dict[str, Optional[str]] = {gid: None for gid in self.state["goals"]}

        # 已完成目标的 ETA：累计首次达到目标额的账本月份
        for gid in self.state["goals"]:
            target = int(self.state["goals"][gid]["target_amount"])
            if funded.get(gid, 0) < target:
                continue
            running = 0
            for e in sorted(self.state["ledger"], key=lambda x: (x["value_month"], x["value_date"], x["id"])):
                if e["goal_id"] != gid:
                    continue
                running += int(e["amount"])
                if running >= target:
                    eta[gid] = e["value_month"]
                    break

        for m in future:
            for account in accounts:
                income = self._income_for(account, m)
                fixed = self._fixed_for(account, m)
                oneoff = self._oneoff_for(account, m)
                disc = income - fixed - oneoff
                discretionary[(account, m)] = disc
                cash = max(disc, 0)

                # 用户强制保留的扣款优先占款（即使当月现金不足也保留，由用户负责）
                account_goals = sorted(
                    (g for g in goals if g.account == account and cumulative.get(g.id, 0) < g.target_amount),
                    key=lambda g: (g.priority, g.deadline, g.created_month or start, g.id),
                )
                for g in account_goals:
                    claim = pinned.get((g.id, m))
                    if claim:
                        amount = min(claim, max(g.target_amount - cumulative.get(g.id, 0), 0))
                        alloc[(g.id, m)] = amount
                        cash -= amount
                        cumulative[g.id] = cumulative.get(g.id, 0) + amount
                        if eta.get(g.id) is None and cumulative[g.id] >= g.target_amount:
                            eta[g.id] = m

                for g in account_goals:
                    remaining = g.target_amount - cumulative.get(g.id, 0)
                    if remaining <= 0 or (g.id, m) in blocked:
                        continue
                    deadline_month = g.deadline[:7]
                    last_month = min(deadline_month, end) if deadline_month else end
                    left = [x for x in month_iter(m, last_month)] if last_month >= m else [m]
                    want = math.ceil(remaining / len(left))
                    # 期望月付独立记录：即使当月现金为 0、该目标一分未得，
                    # 也要作为「被暂停的扣款」的恢复依据
                    desired_map[(g.id, m)] = want
                    if cash <= 0:
                        continue
                    amount = min(want, remaining, cash)
                    cash -= amount
                    alloc[(g.id, m)] = alloc.get((g.id, m), 0) + amount
                    cumulative[g.id] = cumulative.get(g.id, 0) + amount
                    if eta.get(g.id) is None and cumulative[g.id] >= g.target_amount:
                        eta[g.id] = m
                surplus[(account, m)] = cash

        return {
            "months": months,
            "future_months": future,
            "alloc": alloc,
            "desired": desired_map,
            "discretionary": discretionary,
            "surplus": surplus,
            "eta": eta,
            "funded": funded,
        }

    # ---------------------------------------------------------------- resume
    def _resume_condition_met(self, d: dict[str, Any], projection: dict[str, Any]) -> bool:
        cond = d.get("resume_when") or {}
        ctype = cond.get("type")
        if ctype == "manual":
            return False
        if ctype == "goal_reactivated":
            goal = self.state["goals"].get(cond.get("goal_id", ""))
            return bool(goal and goal["status"] == "active")
        if ctype == "cash_available":
            # 只认该扣款所属月份的瀑布分配；月份已过则窗口关闭
            if d["month"] < self._as_of_month():
                return False
            return projection["alloc"].get((d["goal_id"], d["month"]), 0) > 0
        return False

    def condition_status(self, d: dict[str, Any], projection: dict[str, Any]) -> dict[str, Any]:
        """供查询接口使用：恢复条件 + 当前是否满足。"""
        cond = d.get("resume_when") or {}
        return {
            "condition": cond,
            "currently_met": self._resume_condition_met(d, projection),
        }

    # ------------------------------------------------------------- replan core
    def replan(
        self,
        trigger_type: str,
        reason: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        with self.store.lock():
            self._reconcile()
            projection = self._project()
            alloc, desired, disc = projection["alloc"], projection["desired"], projection["discretionary"]

            version_before = self.state["plan_version"]
            old_eta_map = self.state.get("last_projection", {}).get("eta", {})
            now = self._now()
            current = self._as_of_month()
            affected: dict[str, dict[str, Any]] = {}
            paused_ids: list[str] = []
            resumed_ids: list[str] = []

            def mark(goal_id: str, change: str) -> None:
                item = affected.setdefault(
                    goal_id,
                    {
                        "goal_id": goal_id,
                        "name": self.state["goals"].get(goal_id, {}).get("name", ""),
                        "old_eta": None,
                        "new_eta": None,
                        "changes": [],
                    },
                )
                item["changes"].append(change)

            # 1) 遍历既有 scheduled / paused 实例（executed/cancelled 永不触碰）
            for d in list(self.state["deductions"].values()):
                status = d["status"]
                if status in ("executed", "cancelled"):
                    continue
                key = (d["goal_id"], d["month"])
                goal_done = self.state["goals"].get(d["goal_id"], {}).get("status") == "completed"

                if status == "paused":
                    if goal_done:
                        d["status"] = "cancelled"
                        d["cancelled_at"] = now
                        d["cancel_reason"] = "目标已完成"
                        mark(d["goal_id"], f"cancelled:{d['id']}@{d['month']}")
                        continue
                    if d["month"] < current:
                        d["status"] = "cancelled"
                        d["cancelled_at"] = now
                        d["cancel_reason"] = "暂停月份已过且恢复条件未满足，资金已重排至后续月份"
                        mark(d["goal_id"], f"expired_paused:{d['id']}@{d['month']}")
                    elif self._resume_condition_met(d, projection):
                        d["status"] = "scheduled"
                        d["amount"] = alloc.get(key, int(d["amount"]))
                        d["scheduled_day"] = self._due_day(d["month"])
                        d["pause_reason"] = ""
                        d["resume_when"] = {}
                        d["paused_at"] = ""
                        d["plan_version"] = version_before + 1
                        resumed_ids.append(d["id"])
                        mark(d["goal_id"], f"resumed:{d['id']}@{d['month']}")
                    else:
                        # 仍未满足恢复条件：刷新期望月付与条件金额
                        want_now = desired.get(key)
                        if want_now and int(d["amount"]) != want_now:
                            d["amount"] = want_now
                            d["resume_when"]["amount_needed"] = want_now
                            mark(d["goal_id"], f"desired_changed:{d['month']}:{want_now}")
                    continue

                # scheduled
                if d.get("pinned"):
                    # 用户强制保留的扣款：瀑布不得改金额或暂停
                    continue
                if d["month"] < current:
                    d["status"] = "cancelled"
                    d["cancelled_at"] = now
                    d["cancel_reason"] = "历史月份未确认执行，已被新月度计划取代"
                    mark(d["goal_id"], f"cancelled:{d['id']}@{d['month']}")
                    continue

                if goal_done:
                    d["status"] = "cancelled"
                    d["cancelled_at"] = now
                    d["cancel_reason"] = "目标已完成"
                    mark(d["goal_id"], f"cancelled:{d['id']}@{d['month']}")
                    continue

                new_amount = alloc.get(key, 0)
                due_day = self._due_day(d["month"])
                if int(d.get("scheduled_day", 0)) != due_day:
                    d["scheduled_day"] = due_day
                    mark(d["goal_id"], f"due_day_moved:{d['month']}->{due_day}")
                if new_amount > 0:
                    if int(d["amount"]) != new_amount:
                        mark(d["goal_id"], f"amount_changed:{d['month']}:{d['amount']}->{new_amount}")
                        d["amount"] = new_amount
                    d["plan_version"] = version_before + 1
                else:
                    month_disc = disc.get((d["account"], d["month"]))
                    if month_disc is not None and month_disc <= 0:
                        why = "当月可支配资金（收入-固定支出-临时支出）不足"
                        detail = "当月收入恢复并高于固定与临时支出之和"
                    else:
                        why = "当月资金被更高优先级目标占用"
                        detail = "更高优先级目标已完成（或当月结余增加），瀑布重新轮到本目标"
                    d["status"] = "paused"
                    d["paused_at"] = now
                    d["pause_reason"] = why
                    d["amount"] = desired.get(key, int(d["amount"]))
                    d["resume_when"] = {
                        "type": "cash_available",
                        "account": d["account"],
                        "month": d["month"],
                        "amount_needed": desired.get(key, int(d["amount"])),
                        "detail": detail,
                    }
                    paused_ids.append(d["id"])
                    mark(d["goal_id"], f"paused:{d['id']}@{d['month']}")

            # 2) 为瀑布覆盖的 (目标, 月份) 生成实例：
            #    alloc 有金额 -> scheduled；只有期望（desired）无金额 -> paused 并记恢复条件
            all_keys = sorted(set(alloc) | set(desired), key=lambda k: (k[1], k[0]))
            for (goal_id, month) in all_keys:
                if month < current:
                    continue
                exists = next(
                    (d for d in self.state["deductions"].values()
                     if d["goal_id"] == goal_id and d["month"] == month
                     and d["status"] in ("scheduled", "paused")),
                    None,
                )
                if exists:
                    # 同月实例已在第 1 步处理（恢复/继续暂停/金额更新），不重复生成
                    continue
                goal = self.state["goals"][goal_id]
                amount = alloc.get((goal_id, month), 0)
                deduction = Deduction(
                    id=f"ded_{_uid()}",
                    goal_id=goal_id,
                    account=goal["account"],
                    currency=goal["currency"],
                    month=month,
                    scheduled_day=self._due_day(month),
                    amount=amount if amount > 0 else desired[(goal_id, month)],
                    status="scheduled",
                    plan_version=version_before + 1,
                )
                if amount <= 0:
                    month_disc = disc.get((goal["account"], month))
                    if month_disc is not None and month_disc <= 0:
                        why = "当月可支配资金（收入-固定支出-临时支出）不足"
                        detail = "当月收入恢复并高于固定与临时支出之和"
                    else:
                        why = "当月资金被更高优先级目标占用"
                        detail = "更高优先级目标已完成（或当月结余增加），瀑布重新轮到本目标"
                    deduction.status = "paused"
                    deduction.paused_at = now
                    deduction.pause_reason = why
                    deduction.resume_when = {
                        "type": "cash_available",
                        "account": goal["account"],
                        "month": month,
                        "amount_needed": desired[(goal_id, month)],
                        "detail": detail,
                    }
                    paused_ids.append(deduction.id)
                    mark(goal_id, f"paused:{deduction.id}@{month}")
                else:
                    mark(goal_id, f"scheduled:{month}:{amount}")
                self.state["deductions"][deduction.id] = deduction.to_dict()

            # 3) ETA 差异（与上一版投影比较，首轮基线 old 全为 None）
            eta_after = projection["eta"]
            for goal_id in self.state["goals"]:
                old_eta = old_eta_map.get(goal_id)
                new_eta = eta_after.get(goal_id)
                if old_eta != new_eta:
                    item = affected.setdefault(
                        goal_id,
                        {
                            "goal_id": goal_id,
                            "name": self.state["goals"][goal_id].get("name", ""),
                            "old_eta": old_eta,
                            "new_eta": new_eta,
                            "changes": [],
                        },
                    )
                    item["old_eta"], item["new_eta"] = old_eta, new_eta
                    if not item["changes"]:
                        item["changes"].append("eta_changed")

            has_goals = bool(self.state["goals"])
            is_first = version_before == 0
            changed = bool(affected) or paused_ids or resumed_ids
            if not has_goals or (not is_first and not changed and trigger_type == "baseline"):
                self.store.save()
                return {"recorded": False, "plan_version": version_before, "projection": projection}

            # 外部事件（医疗/降薪/延期/导入/手工）即使未改变计划，也留审计痕；
            # 不递增 plan_version、不产出版本快照。
            no_plan_change = not is_first and not changed
            new_version = version_before if no_plan_change else version_before + 1
            seq = len(self.state["adjustments"]) + 1
            if no_plan_change:
                payload = {**payload, "no_plan_change": True}
            adjustment = Adjustment(
                id=f"adj_{_uid()}",
                seq=seq,
                triggered_at=now,
                trigger_type=trigger_type,
                reason=reason,
                payload=payload,
                plan_version_before=version_before,
                plan_version_after=new_version,
                affected_goals=list(affected.values()),
                paused=paused_ids,
                resumed=resumed_ids,
            )
            self.state["adjustments"].append(adjustment.to_dict())
            self.state["plan_version"] = new_version
            self.state["last_projection"] = {"eta": eta_after}

            if no_plan_change:
                self.store.save()
                return {"recorded": True, "plan_version": new_version,
                        "adjustment": adjustment.to_dict(), "projection": projection,
                        "no_plan_change": True}

            snapshot = self._snapshot(adjustment.to_dict(), projection)
            rel = self.store.write_export(adjustment.plan_version_after, snapshot)
            self.state["exports"].append(
                {
                    "version": adjustment.plan_version_after,
                    "path": rel,
                    "at": now,
                    "adjustment_id": adjustment.id,
                    "trigger_type": trigger_type,
                    "sha256": hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
                }
            )
            self.store.save()
            return {
                "recorded": True,
                "plan_version": adjustment.plan_version_after,
                "adjustment": adjustment.to_dict(),
                "projection": projection,
            }

    # -------------------------------------------------------------- snapshots
    def _snapshot(self, adjustment: dict[str, Any], projection: dict[str, Any]) -> dict[str, Any]:
        accounts = sorted({k[0] for k in projection["discretionary"]})
        return {
            "schema": "family-training-plan/v1",
            "plan_version": adjustment["plan_version_after"],
            "generated_at": adjustment["triggered_at"],
            "adjustment_id": adjustment["id"],
            "trigger": {
                "type": adjustment["trigger_type"],
                "reason": adjustment["reason"],
                "payload": adjustment["payload"],
            },
            "config": copy.deepcopy(self.state["config"]),
            "goals": copy.deepcopy(list(self.state["goals"].values())),
            "incomes": copy.deepcopy(list(self.state["incomes"].values())),
            "fixed_expenses": copy.deepcopy(list(self.state["fixed_expenses"].values())),
            "one_off_expenses": copy.deepcopy(list(self.state["one_off_expenses"].values())),
            "training_phases": copy.deepcopy(list(self.state["training_phases"].values())),
            "races": copy.deepcopy(list(self.state["races"].values())),
            "ledger": copy.deepcopy(self.state["ledger"]),
            "deductions": copy.deepcopy(list(self.state["deductions"].values())),
            "projection": {
                "eta": projection["eta"],
                "monthly": [
                    {
                        "month": m,
                        "discretionary": {
                            a: projection["discretionary"].get((a, m)) for a in accounts
                        },
                        "surplus": {a: projection["surplus"].get((a, m)) for a in accounts},
                        "allocations": [
                            {"goal_id": g, "amount": a}
                            for (g, mm), a in sorted(projection["alloc"].items())
                            if mm == m
                        ],
                    }
                    for m in projection["future_months"]
                ],
            },
            "adjustments": copy.deepcopy(self.state["adjustments"]),
        }

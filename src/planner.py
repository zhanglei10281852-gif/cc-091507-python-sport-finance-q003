"""计划引擎：在同一时间轴上重排未来扣款。

输入为普通字典（由服务层从数据库读取），输出为写操作与变更记录，
引擎本身不接触数据库，便于独立测试。核心不变量：

- 已执行（executed）的扣款不参与重排，历史转账不被回算覆盖；
- 容量按银行价值日所在月份计算，而不是计划日月份；未用完的额度
  结转后续账期（银行账户语义），周末/跨时区顺延由上月结余承接；
- 同一账期内按目标优先级分配，结余不足的扣款进入 paused 并携带
  恢复条件，下次重排时额度允许即自动恢复。
"""
from __future__ import annotations

import calendar
from datetime import date

from money import to_amount_str
from valuedate import compute_value_date, parse_hhmm


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def add_months(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) + months
    year, month = total // 12, total % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def iter_months(start: date, end: date):
    """按 (year, month) 遍历 start 到 end 覆盖的月份（含两端）。"""
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1


def build_schedule(remaining_micros: int, as_of: date, deadline: date) -> list[tuple[date, int]]:
    """把剩余金额均分到 [as_of, deadline] 的每个月末（不超过 deadline）。

    返回 (计划日, 金额微单位) 列表；整除余数并入最后一期。
    """
    if remaining_micros <= 0:
        return []
    days: list[date] = []
    for year, month in iter_months(as_of, deadline):
        day = month_end(year, month)
        if day > deadline:
            day = deadline
        if day < as_of:
            continue
        days.append(day)
    if not days:
        days = [as_of]
    base = remaining_micros // len(days)
    extra = remaining_micros - base * len(days)
    amounts = [base] * (len(days) - 1) + [base + extra]
    return list(zip(days, amounts))


def month_gap(prev: str, current: str) -> int:
    """两个 YYYY-MM 账期之间相差的月数（current 在 prev 之后为正）。"""
    y1, m1 = int(prev[:4]), int(prev[5:7])
    y2, m2 = int(current[:4]), int(current[5:7])
    return (y2 - y1) * 12 + (m2 - m1)


def replan(state: dict, as_of: date) -> dict:
    """根据当前目标与现金流重排未来扣款。

    state: {"profile", "fixed_expenses", "goals", "deductions",
            "direct_transactions"}，其中 deductions 不含已取消行。
    返回 {"ops", "changes", "goal_updates"}。
    """
    profile = state["profile"]
    fixed_monthly = sum(e["amount_micros"] for e in state["fixed_expenses"] if e["active"])
    capacity_default = profile["monthly_income_micros"] - fixed_monthly
    plan_ccy = profile["currency"]
    t_time = parse_hhmm(profile.get("transfer_time"))
    local_tz = profile.get("local_tz") or "UTC"
    bank_tz = profile.get("bank_tz") or "UTC"

    goals = [g for g in state["goals"] if g["status"] == "active"]
    goals.sort(key=lambda g: (g["priority"], g["deadline"], g["id"]))
    deductions = state["deductions"]

    # 已入账资金 = 已执行扣款（按实际执行额）+ 直接入账的银行流水
    funded: dict[int, int] = {}
    for d in deductions:
        if d["status"] == "executed":
            amount = d["executed_amount_micros"]
            if amount is None:
                amount = d["amount_micros"]
            funded[d["goal_id"]] = funded.get(d["goal_id"], 0) + amount
    for txn in state.get("direct_transactions", []):
        funded[txn["goal_id"]] = funded.get(txn["goal_id"], 0) + txn["amount_micros"]

    ops: list[dict] = []
    future: list[dict] = []
    changes: dict[str, list] = {"created": [], "updated": [], "cancelled": [], "paused": [], "resumed": []}
    goal_updates: dict[int, dict] = {}
    temp_id = 0

    # 1) 每个活跃目标按剩余额度与截止日生成理想排期，与现有未执行扣款按序配对复用
    for goal in goals:
        gid = goal["id"]
        remaining = goal["target_micros"] - funded.get(gid, 0)
        existing = sorted(
            (d for d in deductions if d["goal_id"] == gid and d["status"] in ("pending", "paused")),
            key=lambda d: (d["value_date"], d["id"]),
        )
        if remaining <= 0:
            for d in existing:
                ops.append({"op": "cancel", "id": d["id"]})
                changes["cancelled"].append(d["id"])
            goal_updates[gid] = {"status": "completed", "eta_date": None, "eta_status": "funded"}
            continue
        rows = []
        for sched, amount in build_schedule(remaining, as_of, date.fromisoformat(goal["deadline"])):
            rows.append({
                "goal_id": gid,
                "amount_micros": amount,
                "scheduled_date": sched.isoformat(),
                "value_date": compute_value_date(sched, t_time, local_tz, bank_tz).isoformat(),
            })
        for index, row in enumerate(rows):
            if index < len(existing):
                prev = existing[index]
                row["row_id"] = prev["id"]
                row["prev_status"] = prev["status"]
                row["prev_recovery"] = prev.get("recovery_condition")
                row["prev_amount"] = prev["amount_micros"]
                row["prev_scheduled"] = prev["scheduled_date"]
                row["prev_value"] = prev["value_date"]
            else:
                temp_id -= 1
                row["row_id"] = temp_id
                row["prev_status"] = None
                row["prev_recovery"] = None
            row["priority"] = goal["priority"]
            row["currency"] = goal["currency"]
            future.append(row)
        for d in existing[len(rows):]:
            ops.append({"op": "cancel", "id": d["id"]})
            changes["cancelled"].append(d["id"])

    # 2) 容量分配：账期按价值日月份划分，账期内按目标优先级扣减；
    #    未用完的额度结转后续账期（银行账户语义：钱不会在月末消失），
    #    因此周末/跨时区顺延到次月的扣款由上月结余自然承接。
    #    结转从 as_of 所在账期起算，首个有扣款的账期补齐之前各月额度。
    ordered = sorted(future, key=lambda r: (r["value_date"][:7], r["priority"], r["value_date"], r["row_id"]))
    start_month = as_of.isoformat()[:7]
    available = 0
    current_month: str | None = None
    for row in ordered:
        month = row["value_date"][:7]
        if month != current_month:
            if current_month is None:
                available = (month_gap(start_month, month) + 1) * capacity_default
            else:
                available += month_gap(current_month, month) * capacity_default
            current_month = month
        if row["currency"] != plan_ccy:
            # 外币目标不参与本币容量约束（不做汇率换算，见 docs/domain.md）
            row["status"] = "pending"
            row["recovery_condition"] = None
        elif row["amount_micros"] <= available:
            available -= row["amount_micros"]
            row["status"] = "pending"
            row["recovery_condition"] = None
        else:
            row["status"] = "paused"
            row["recovery_condition"] = {
                "type": "insufficient_funds",
                "month": month,
                "required": to_amount_str(row["amount_micros"]),
                "available": to_amount_str(max(available, 0)),
                "resume": "automatic",
                "detail": "可支配结余不足（含往月结转），收入恢复或更高优先级目标完成后自动恢复",
            }
        prev_status = row["prev_status"]
        if row["status"] == "paused" and prev_status != "paused":
            changes["paused"].append(row["row_id"])
        elif row["status"] == "pending" and prev_status == "paused":
            changes["resumed"].append(row["row_id"])

    # 3) 生成写操作：新行插入、既有行仅在内容变化时更新
    for row in future:
        if row["row_id"] < 0:
            ops.append({
                "op": "insert",
                "temp_id": row["row_id"],
                "goal_id": row["goal_id"],
                "amount_micros": row["amount_micros"],
                "scheduled_date": row["scheduled_date"],
                "value_date": row["value_date"],
                "status": row["status"],
                "recovery_condition": row["recovery_condition"],
            })
            changes["created"].append(row["row_id"])
        else:
            dirty = (
                row["prev_amount"] != row["amount_micros"]
                or row["prev_scheduled"] != row["scheduled_date"]
                or row["prev_value"] != row["value_date"]
                or row["prev_status"] != row["status"]
                or row["prev_recovery"] != row["recovery_condition"]
            )
            if dirty:
                ops.append({
                    "op": "update",
                    "id": row["row_id"],
                    "amount_micros": row["amount_micros"],
                    "scheduled_date": row["scheduled_date"],
                    "value_date": row["value_date"],
                    "status": row["status"],
                    "recovery_condition": row["recovery_condition"],
                })
                if (
                    row["prev_amount"] != row["amount_micros"]
                    or row["prev_scheduled"] != row["scheduled_date"]
                    or row["prev_value"] != row["value_date"]
                ):
                    changes["updated"].append(row["row_id"])

    # 4) 预计完成日：已入账 + pending 扣款按价值日累计，首次达标之日
    pending_by_goal: dict[int, list] = {}
    for row in future:
        if row["status"] == "pending":
            pending_by_goal.setdefault(row["goal_id"], []).append(row)
    for goal in goals:
        gid = goal["id"]
        if gid in goal_updates:
            continue
        cumulative = funded.get(gid, 0)
        eta = None
        for row in sorted(pending_by_goal.get(gid, []), key=lambda r: (r["value_date"], r["row_id"])):
            cumulative += row["amount_micros"]
            if cumulative >= goal["target_micros"]:
                eta = row["value_date"]
                break
        if eta is None:
            eta_status = "unfunded"
        elif eta <= goal["deadline"]:
            eta_status = "on_track"
        else:
            eta_status = "at_risk"
        goal_updates[gid] = {"status": "active", "eta_date": eta, "eta_status": eta_status}

    return {"ops": ops, "changes": changes, "goal_updates": goal_updates}

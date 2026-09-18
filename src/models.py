"""领域模型：全部为可 JSON 序列化的 dataclass，状态以 dict 形式持久化。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Optional


class GoalType(str, Enum):
    RACE_FEE = "race_fee"
    EQUIPMENT = "equipment"
    TRAVEL = "travel"
    EMERGENCY_RESERVE = "emergency_reserve"


class DeductionStatus(str, Enum):
    SCHEDULED = "scheduled"
    EXECUTED = "executed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


@dataclass
class Goal:
    id: str
    name: str
    type: str
    target_amount: int          # 最小货币单位
    currency: str
    deadline: str               # ISO 日期（比赛日/采购目标日）
    priority: int
    status: str = "active"      # active | completed
    created_month: str = ""
    race_id: Optional[str] = None
    account: str = ""           # 该目标的自动扣款账户

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Goal:
        return cls(**d)


@dataclass
class Income:
    """月收入。同一 (month, account) 后到的记录覆盖先到的（收入调整场景）。"""
    month: str                  # YYYY-MM
    account: str
    amount: int
    currency: str
    tz: str = "Asia/Shanghai"
    source: str = "salary"
    day_of_month: int = 10

    def key(self) -> tuple[str, str]:
        return (self.month, self.account)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Income:
        return cls(**d)


@dataclass
class FixedExpense:
    """家庭固定支出（房租、保险、信用卡分期等），按月发生。"""
    id: str
    name: str
    category: str
    account: str
    amount: int
    currency: str
    start_month: str
    end_month: Optional[str] = None   # None 表示持续到计划结束
    day_of_month: int = 1
    tz: str = "Asia/Shanghai"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FixedExpense:
        return cls(**d)


@dataclass
class OneOffExpense:
    """临时支出，典型为医疗支出。按月度价值日进入对应月份的瀑布。"""
    id: str
    name: str
    category: str
    account: str
    amount: int
    currency: str
    value_month: str
    business_time: str
    tz: str
    value_date: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OneOffExpense:
        return cls(**d)


@dataclass
class TrainingPhase:
    """训练周期：用于统一时间轴展示，不直接参与扣款。"""
    id: str
    name: str
    start_date: str
    end_date: str
    intensity: str = "base"      # base | build | peak | taper

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrainingPhase:
        return cls(**d)


@dataclass
class RaceEvent:
    id: str
    name: str
    race_date: str
    tz: str = "Asia/Shanghai"
    location: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RaceEvent:
        return cls(**d)


@dataclass
class LedgerEntry:
    """append-only 账本条目，已执行的转账永不回算覆盖。"""
    id: str
    goal_id: str
    account: str
    amount: int
    currency: str
    value_date: str
    value_month: str
    business_time: str
    source: str                 # bank_import | manual
    external_id: Optional[str] = None
    import_batch: Optional[str] = None
    dedupe_key: str = ""
    deduction_id: Optional[str] = None   # 若对应某月计划扣款，记录其 id
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LedgerEntry:
        return cls(**d)


@dataclass
class Deduction:
    """未来/历史的计划扣款实例，由规划器按月生成与重排。"""
    id: str
    goal_id: str
    account: str
    currency: str
    month: str                  # 该实例归属的计划月份
    scheduled_day: int          # 月内计划扣款日
    amount: int
    status: str = DeductionStatus.SCHEDULED.value
    plan_version: int = 1
    # 暂停时记录的结构化恢复条件
    pause_reason: str = ""
    resume_when: dict[str, Any] = field(default_factory=dict)
    paused_at: str = ""
    cancelled_at: str = ""
    executed_at: str = ""
    cancel_reason: str = ""
    note: str = ""
    pinned: bool = False       # 手动恢复/强制保留的扣款，瀑布不得改其金额或暂停

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Deduction:
        return cls(**d)


@dataclass
class Adjustment:
    """一次重排的完整审计记录。"""
    id: str
    seq: int
    triggered_at: str           # 接收时间（系统时间，ISO）
    trigger_type: str           # income_change | medical_expense | race_postponed | manual | baseline
    reason: str
    payload: dict[str, Any]
    plan_version_before: int
    plan_version_after: int
    affected_goals: list[dict[str, Any]]   # [{goal_id, old_eta, new_eta, change}]
    paused: list[str]           # 本次被暂停的 deduction id
    resumed: list[str]          # 本次恢复的 deduction id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Adjustment:
        return cls(**d)


def today_iso() -> str:
    return date.today().isoformat()

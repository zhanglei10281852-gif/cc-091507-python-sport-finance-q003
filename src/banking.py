"""银行日历与价值日（value date）处理。

月末跨时区入账规则：
1. 业务时间先按来源时区解释为本地日期时间；
2. 超过该行截止时间（cutoff，默认 17:00）的交易视为下一工作日发起；
3. 周末（及登记的公共假期）顺延到下一个工作日；
4. 以最终工作日作为银行价值日，所有月度归集都按价值日所在年月归桶。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class ValueDateResult:
    value_date: date
    posting_month: str  # "YYYY-MM"，按价值日归桶


def _is_business_day(day: date, holidays: frozenset[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def next_business_day(day: date, holidays: frozenset[date] | None = None) -> date:
    holidays = holidays or frozenset()
    while not _is_business_day(day, holidays):
        day += timedelta(days=1)
    return day


def resolve_timezone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - 防御性分支
        raise ValueError(f"未知时区: {tz_name}") from exc


def value_date(
    business_dt: datetime,
    source_tz: str,
    cutoff: time = time(17, 0),
    holidays: frozenset[date] | None = None,
) -> ValueDateResult:
    """根据业务发生时间（naive 视为 source_tz 本地时间，aware 会先转换）计算价值日。"""
    tz = resolve_timezone(source_tz)
    if business_dt.tzinfo is None:
        business_dt = business_dt.replace(tzinfo=tz)
    else:
        business_dt = business_dt.astimezone(tz)

    day = business_dt.date()
    if business_dt.time() >= cutoff:
        day += timedelta(days=1)
    day = next_business_day(day, holidays)
    return ValueDateResult(value_date=day, posting_month=f"{day.year:04d}-{day.month:02d}")


def parse_business_dt(raw: str, source_tz: str) -> datetime:
    """解析输入时间；带偏移量的 ISO 字符串按字面量解释，naive 由调用方补时区。"""
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=resolve_timezone(source_tz))
    return dt


def month_iter(start_month: str, end_month: str) -> list[str]:
    """返回闭区间内的 "YYYY-MM" 列表。"""
    sy, sm = (int(x) for x in start_month.split("-"))
    ey, em = (int(x) for x in end_month.split("-"))
    months: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return months


def shift_month(month: str, delta: int) -> str:
    y, m = (int(x) for x in month.split("-"))
    idx = (y * 12 + (m - 1)) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"

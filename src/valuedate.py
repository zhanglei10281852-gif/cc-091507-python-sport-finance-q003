"""银行价值日（value date）计算。

扣款在本地时区的计划日发起，按银行时区的结算日记账；结算日落在
周末或节假日时顺延到下一个工作日。月末跨时区入账因此可能计入下一个
账期——容量分配与预计完成日一律以价值日为准，而不是计划日。
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def parse_tz(name: object) -> timezone:
    """解析时区：IANA 名称、UTC/GMT/Z 或 ±HH:MM 偏移；未知名称回退 UTC。"""
    if not name:
        return timezone.utc
    text = str(name).strip()
    if text.upper() in {"UTC", "GMT", "Z"}:
        return timezone.utc
    match = _OFFSET_RE.match(text)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        hours, minutes = int(match.group(2)), int(match.group(3))
        if hours > 14 or minutes > 59:
            raise ValueError(f"无效的时区偏移: {name!r}")
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    if ZoneInfo is not None:
        try:
            return ZoneInfo(text)  # type: ignore[return-value]
        except Exception:
            pass
    return timezone.utc


def parse_hhmm(value: object, default: str = "21:00") -> time:
    """解析 HH:MM 转账发起时间。"""
    text = str(value if value else default).strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"无效的时间格式: {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"无效的时间格式: {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"无效的时间格式: {value!r}")
    return time(hour, minute)


def compute_value_date(
    scheduled: date,
    transfer_time: time,
    local_tz: str,
    bank_tz: str,
    holidays: frozenset[str] = frozenset(),
) -> date:
    """计算银行价值日。

    本地计划日 + 发起时间 → 换算到银行时区 → 取结算日 →
    周末/节假日顺延到下一个工作日。
    """
    local_dt = datetime.combine(scheduled, transfer_time, tzinfo=parse_tz(local_tz))
    bank_dt = local_dt.astimezone(parse_tz(bank_tz))
    day = bank_dt.date()
    while day.weekday() >= 5 or day.isoformat() in holidays:
        day += timedelta(days=1)
    return day

"""金额工具：内部统一使用整数最小货币单位，避免浮点误差。"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# 参考 reference/domain.json 的 quantity_precision
MINOR_PRECISION = 6
_Q = Decimal(10) ** -MINOR_PRECISION


def to_minor(amount: float | int | str | Decimal) -> int:
    """把任意金额表示转换为整数最小单位（保留 6 位小数后截断进位）。"""
    if isinstance(amount, bool):  # bool 是 int 的子类，显式拒绝
        raise TypeError("amount 不能为布尔值")
    value = Decimal(str(amount))
    return int((value / _Q).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_decimal(minor: int) -> Decimal:
    return Decimal(minor) * _Q


def format_money(minor: int) -> str:
    """固定 6 位小数字符串，供导出与日志使用。"""
    return str(to_decimal(minor).quantize(_Q))

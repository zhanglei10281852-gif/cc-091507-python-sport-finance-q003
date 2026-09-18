"""金额工具。

所有金额在内部以微单位（1e-6）整数保存，对应 reference/domain.json
中的 quantity_precision=6，避免浮点误差进入长期留存的记录。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SCALE = 1_000_000
_QUANT = Decimal("1")


class MoneyError(ValueError):
    """金额无法解析或超出精度。"""


def to_micros(value: object) -> int:
    """把数值/字符串金额转换为微单位整数，按 6 位小数四舍五入。"""
    if value is None or isinstance(value, bool):
        raise MoneyError("金额必须是数字或数字字符串")
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"无法解析金额: {value!r}") from exc
    if not dec.is_finite():
        raise MoneyError("金额必须是有限数值")
    return int((dec * SCALE).quantize(_QUANT, rounding=ROUND_HALF_UP))


def to_amount_str(micros: int) -> str:
    """微单位整数 → 十进制字符串（去掉多余尾零）。"""
    dec = (Decimal(int(micros)) / SCALE).normalize()
    return format(dec, "f")

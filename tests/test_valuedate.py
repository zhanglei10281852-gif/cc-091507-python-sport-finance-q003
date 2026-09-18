from __future__ import annotations

import sys
import unittest
from datetime import date, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datetime import date, time, timedelta

from valuedate import compute_value_date, parse_hhmm, parse_tz


class ParseTzTest(unittest.TestCase):
    def test_offsets(self) -> None:
        self.assertEqual(timedelta(hours=8), parse_tz("+08:00").utcoffset(None))
        self.assertEqual(-timedelta(hours=5, minutes=30), parse_tz("-0530").utcoffset(None))
        self.assertEqual(timedelta(0), parse_tz("UTC").utcoffset(None))

    def test_unknown_falls_back_to_utc(self) -> None:
        self.assertEqual(timedelta(0), parse_tz("Not/AZone").utcoffset(None))


class ParseHhmmTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(time(21, 0), parse_hhmm("21:00"))
        self.assertEqual(time(9, 5), parse_hhmm("09:05"))

    def test_invalid(self) -> None:
        for bad in ("25:00", "12", "ab:cd", "12:60"):
            with self.assertRaises(ValueError, msg=bad):
                parse_hhmm(bad)


class ValueDateTest(unittest.TestCase):
    def test_same_day_when_bank_behind(self) -> None:
        # 本地 +08 21:00 发起 = UTC 当天 13:00，价值日不变
        day = compute_value_date(date(2026, 9, 30), time(21, 0), "+08:00", "UTC")
        self.assertEqual(date(2026, 9, 30), day)

    def test_cross_month_when_bank_ahead(self) -> None:
        # 本地 +08 21:00 发起 = +12 时区次日凌晨，月末入账跨入下一账期
        day = compute_value_date(date(2026, 9, 30), time(21, 0), "+08:00", "+12:00")
        self.assertEqual(date(2026, 10, 1), day)

    def test_cross_month_negative_offset(self) -> None:
        # 本地 -05 21:00 发起 = UTC 次日凌晨
        day = compute_value_date(date(2026, 9, 30), time(21, 0), "-05:00", "UTC")
        self.assertEqual(date(2026, 10, 1), day)

    def test_weekend_rolls_forward(self) -> None:
        # 2026-10-02（周五）23:00 -05:00 发起 → UTC 2026-10-03（周六）→ 顺延至周一
        day = compute_value_date(date(2026, 10, 2), time(23, 0), "-05:00", "UTC")
        self.assertEqual(date(2026, 10, 5), day)

    def test_holiday_rolls_forward(self) -> None:
        day = compute_value_date(
            date(2026, 9, 30), time(21, 0), "+08:00", "+12:00",
            holidays=frozenset({"2026-10-01"}),
        )
        self.assertEqual(date(2026, 10, 2), day)


if __name__ == "__main__":
    unittest.main()

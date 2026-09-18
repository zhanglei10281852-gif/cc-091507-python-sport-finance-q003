from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from banking import month_iter, next_business_day, shift_month, value_date


class BankingTest(unittest.TestCase):
    def test_cutoff_pushes_to_next_day(self) -> None:
        r = value_date(datetime.fromisoformat("2026-09-18T17:30:00"), "Asia/Shanghai")
        self.assertEqual(date(2026, 9, 21), r.value_date)  # 18 日 17:30 -> 19 是周六 -> 21 日周一
        self.assertEqual("2026-09", r.posting_month)

    def test_weekend_rolls_forward(self) -> None:
        r = value_date(datetime.fromisoformat("2026-09-19T10:00:00"), "Asia/Shanghai")
        self.assertEqual(date(2026, 9, 21), r.value_date)

    def test_holidays_roll_forward(self) -> None:
        holidays = frozenset({date(2026, 9, 21)})
        r = value_date(datetime.fromisoformat("2026-09-19T10:00:00"), "Asia/Shanghai",
                       holidays=holidays)
        self.assertEqual(date(2026, 9, 22), r.value_date)

    def test_month_end_timezone_crosses_month(self) -> None:
        # 9 月 30 日深夜（香港时间）超过截止时间 -> 价值日 10 月 1 日
        r = value_date(datetime.fromisoformat("2026-09-30T23:30:00"), "Asia/Hong_Kong")
        self.assertEqual(date(2026, 10, 1), r.value_date)
        self.assertEqual("2026-10", r.posting_month)

    def test_naive_datetime_uses_source_tz(self) -> None:
        r = value_date(datetime.fromisoformat("2026-09-18T09:00:00"), "Asia/Hong_Kong")
        self.assertEqual(date(2026, 9, 18), r.value_date)

    def test_month_iter_and_shift(self) -> None:
        self.assertEqual(["2026-11", "2026-12", "2027-01"], month_iter("2026-11", "2027-01"))
        self.assertEqual("2027-02", shift_month("2026-11", 3))
        self.assertEqual("2026-09", shift_month("2026-12", -3))

    def test_next_business_day(self) -> None:
        self.assertEqual(date(2026, 9, 21), next_business_day(date(2026, 9, 19)))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from money import MoneyError, to_amount_str, to_micros


class MoneyTest(unittest.TestCase):
    def test_parse_plain(self) -> None:
        self.assertEqual(1_200_000_000, to_micros(1200))
        self.assertEqual(1_200_000_000, to_micros("1200"))
        self.assertEqual(1_234_567_890, to_micros("1234.567890"))

    def test_precision_six_decimals(self) -> None:
        self.assertEqual(1, to_micros("0.000001"))
        # 超出 6 位小数按四舍五入处理
        self.assertEqual(1, to_micros("0.0000005"))
        self.assertEqual(1_234_567_890, to_micros("1234.5678901"))

    def test_negative(self) -> None:
        self.assertEqual(-5_000_000, to_micros("-5"))

    def test_reject_invalid(self) -> None:
        for bad in ("abc", None, True, "", "NaN", "inf"):
            with self.assertRaises(MoneyError, msg=repr(bad)):
                to_micros(bad)

    def test_format(self) -> None:
        self.assertEqual("0", to_amount_str(0))
        self.assertEqual("1200", to_amount_str(1_200_000_000))
        self.assertEqual("8571.428571", to_amount_str(8_571_428_571))
        self.assertEqual("-5", to_amount_str(-5_000_000))

    def test_round_trip(self) -> None:
        self.assertEqual(8_571_428_571, to_micros(to_amount_str(8_571_428_571)))


if __name__ == "__main__":
    unittest.main()

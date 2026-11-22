# -*- coding: utf-8 -*-
"""
Test cho ``kai_core/prices.py`` và ``kai_core/savings.py`` — giai đoạn 1
(nâng cấp đời thực: giá cả thị trường + sổ tiết kiệm đa dạng).

Trọng tâm là các bất biến tiền: giá không âm, lãi suất hợp lý, và quan
trọng nhất là **tiền không thể bị tạo ra từ hư không**.
"""
from __future__ import annotations

import time
import unittest

from kai_core import prices, savings

#: Mốc thời gian cố định để test không phụ thuộc "hôm nay là ngày nào".
#: Chọn đúng 00:00:00 UTC của một ngày để cộng giây/ngày không lệch ngày.
T0 = 1_700_000_000
T0_START = T0 - (T0 % 86400)      # mốc đầu ngày
DAY = 86400


class PriceTableTests(unittest.TestCase):
    """Bảng giá đời thật."""

    def test_every_good_has_valid_price(self) -> None:
        for key, good in prices.GOODS.items():
            self.assertEqual(good.key, key, "key trong dict phải khớp")
            self.assertGreater(good.base_price, 0, f"{key} giá phải > 0")
            self.assertTrue(good.unit, f"{key} thiếu đơn vị")

    def test_price_is_same_for_everyone_in_a_day(self) -> None:
        """Cùng một ngày thì mọi người mua cùng giá — không trúng số."""
        # Cùng ngày (cộng 1–5 giây, không sang ngày mới).
        self.assertEqual(
            prices.quote("gao", T0_START),
            prices.quote("gao", T0_START + 1),
        )
        self.assertEqual(
            prices.quote("thit_ga", T0_START),
            prices.quote("thit_ga", T0_START + 5),
        )

    def test_price_changes_across_days(self) -> None:
        base = prices.quote("gao", T0)
        later = prices.quote("gao", T0 + 86400 * 3)
        self.assertNotEqual(base, later, "Giá phải nhích theo ngày như thị trường")

    def test_price_never_negative_or_zero(self) -> None:
        for day in range(0, 40, 3):
            for key in prices.GOODS:
                price = prices.quote(key, T0 + day * 86400)
                self.assertGreater(price, 0, f"{key} giá phải > 0")

    def test_unknown_key_is_zero(self) -> None:
        self.assertEqual(prices.quote("khong_ton_tai"), 0)
        self.assertEqual(prices.quote_change("khong_ton_tai"), 0.0)

    def test_change_percent_is_within_band(self) -> None:
        """Biến động phải nằm trong biên, cộng thêm sai số làm tròn bán lẻ.

        Món rẻ làm tròn 100–500đ nên sai số tương đối lớn hơn; biên kiểm
        tra phải cộng thêm để không báo động giả.
        """
        for day in range(0, 20):
            for key, good in prices.GOODS.items():
                change = abs(prices.quote_change(key, T0_START + day * DAY))
                # Sai số tối đa do làm tròn: nửa bước làm tròn / giá gốc.
                step = 100 if good.base_price < 10_000 else (
                    500 if good.base_price < 100_000 else 1_000
                )
                tolerance = (step / 2 / good.base_price) * 100 + 0.5
                self.assertLessEqual(
                    change, prices.DAILY_SWING * 100 + tolerance,
                    f"{key} dao động vượt biên",
                )

    def test_prices_are_rounded_to_sensible_step(self) -> None:
        """Giá phải làm tròn đúng bậc: món rẻ tròn 100đ, món đắt tròn 1.000đ."""
        for key, good in prices.GOODS.items():
            price = prices.quote(key, T0)
            step = 100 if price < 10_000 else (500 if price < 100_000 else 1_000)
            self.assertEqual(price % step, 0, f"{key} phải làm tròn bậc {step}")

    def test_essentials_are_marked(self) -> None:
        essentials = [g.key for g in prices.GOODS.values() if g.essential]
        self.assertGreaterEqual(len(essentials), 8, "Phải có đủ nhu yếu phẩm")

    def test_all_quotes_covers_every_good(self) -> None:
        self.assertEqual(len(prices.all_quotes(T0)), len(prices.GOODS))


class SavingsRateTests(unittest.TestCase):
    """Lãi suất phải hợp lý như ngân hàng thật."""

    def test_annual_rates_are_realistic(self) -> None:
        for acc in savings.ACCOUNTS.values():
            self.assertGreaterEqual(acc.annual_rate, 1.0, f"{acc.key} quá thấp")
            self.assertLessEqual(
                acc.annual_rate, 12.0,
                f"{acc.key} lãi {acc.annual_rate}%/năm là vô lý so với ngân hàng thật",
            )

    def test_longer_term_pays_more(self) -> None:
        """Kỳ hạn dài hơn thì lãi phải cao hơn — nguyên tắc tiền gửi."""
        terms = [a for a in savings.ACCOUNTS.values() if a.term_days > 0]
        rates = [a.annual_rate for a in sorted(terms, key=lambda a: a.term_days)]
        self.assertEqual(rates, sorted(rates), "Lãi phải tăng dần theo kỳ hạn")
        self.assertGreater(len(set(rates)), 1, "Phải khác nhau giữa các kỳ hạn")

    def test_daily_rate_matches_annual(self) -> None:
        for acc in savings.ACCOUNTS.values():
            self.assertAlmostEqual(
                acc.daily_rate * 365 * 100, acc.annual_rate, places=6
            )

    def test_annual_percent_roundtrip(self) -> None:
        self.assertAlmostEqual(
            savings.annual_percent(savings.rate_for("1nam")), 8.0, places=6
        )

    def test_progressive_tiers_increase_with_balance(self) -> None:
        small = savings.progressive_rate(1_000_000)
        mid = savings.progressive_rate(20_000_000)
        big = savings.progressive_rate(500_000_000)
        self.assertLess(small, mid)
        self.assertLess(mid, big, "Tích lũy: để nhiều phải được lãi cao hơn")

    def test_unknown_account_has_zero_rate(self) -> None:
        self.assertEqual(savings.rate_for("khong_co"), 0.0)
        self.assertEqual(savings.annual_for("khong_co"), 0.0)


class SavingsMathTests(unittest.TestCase):
    """Tính lãi và phạt rút sớm — không được tạo tiền từ hư không."""

    def test_interest_zero_for_empty_or_unknown(self) -> None:
        self.assertEqual(savings.interest_for("1nam", 0), 0)
        self.assertEqual(savings.interest_for("khong_co", 10_000_000), 0)
        self.assertEqual(savings.interest_for("1nam", 10_000_000, 0), 0)

    def test_one_year_interest_is_eight_percent(self) -> None:
        """10 triệu gửi kỳ hạn 1 năm (8%/năm) → lãi đúng 800.000đ."""
        self.assertEqual(savings.interest_for("1nam", 10_000_000, 365), 800_000)

    def test_interest_never_exceeds_balance(self) -> None:
        for key in savings.ACCOUNTS:
            for balance in (100_000, 10_000_000, 1_000_000_000):
                self.assertLess(
                    savings.interest_for(key, balance, 365), balance,
                    f"{key} lãi phải nhỏ hơn số dư",
                )

    def test_flexible_account_has_no_penalty(self) -> None:
        self.assertEqual(
            savings.early_withdraw_penalty("linh_dong", 100_000_000), 0
        )

    def test_term_account_loses_interest_on_early_withdraw(self) -> None:
        penalty = savings.early_withdraw_penalty("1nam", 100_000_000)
        self.assertGreater(penalty, 0, "Rút sớm phải mất lãi")
        self.assertLess(
            penalty, savings.interest_for("1nam", 100_000_000),
            "Chỉ mất một nửa lãi kỳ hạn",
        )

    def test_term_lock_respects_deadline(self) -> None:
        # Truyền tường minh ``now_ts`` — không so với thời gian thật.
        opened = T0_START
        self.assertTrue(savings.is_locked("1nam", opened, opened + 1))
        self.assertTrue(
            savings.is_locked("1nam", opened, opened + 364 * DAY)
        )
        self.assertFalse(
            savings.is_locked("1nam", opened, opened + 366 * DAY)
        )
        self.assertEqual(savings.term_left("1nam", opened, opened), 365)
        self.assertEqual(
            savings.term_left("1nam", opened, opened + 400 * DAY), 0
        )

    def test_flexible_account_never_locked(self) -> None:
        self.assertFalse(savings.is_locked("linh_dong", T0_START, T0_START + 1))
        self.assertEqual(savings.term_left("linh_dong", T0_START, T0_START), 0)

    def test_penalty_and_interest_are_bounded(self) -> None:
        """Tiền lãi và phạt phải là số nguyên dương, không âm vô hạn."""
        for key in savings.ACCOUNTS:
            interest = savings.interest_for(key, 1_000_000_000, 365)
            penalty = savings.early_withdraw_penalty(key, 1_000_000_000)
            self.assertGreaterEqual(interest, 0)
            self.assertGreaterEqual(penalty, 0)
            self.assertLess(penalty, 1_000_000_000)


if __name__ == "__main__":
    unittest.main()
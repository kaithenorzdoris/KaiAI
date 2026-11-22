# -*- coding: utf-8 -*-
"""
Test toán thuần cho ``kai_core/career.py`` — giáo dục, nghề nghiệp, lương.

Trọng tâm là các bất biến: không nhảy cấp học, không xin được nghề vượt
bằng cấp, lương tăng đơn điệu theo bậc, và thưởng streak có trần (nếu không
thì chỉ cần không nghỉ việc là nhân lương vô hạn).
"""
from __future__ import annotations

import unittest

from kai_core import career

T0 = 1_700_000_000
DAY = career.DAY


class SchoolTableTests(unittest.TestCase):
    """Bảng học vấn."""

    def test_order_is_sequential(self) -> None:
        self.assertEqual(
            career.SCHOOL_ORDER,
            ("chua_hoc", "thpt", "dai_hoc", "thac_si", "tien_sy"),
        )

    def test_every_school_has_valid_numbers(self) -> None:
        for key, school in career.SCHOOLS.items():
            self.assertEqual(school.key, key, "key trong dict phải khớp")
            self.assertGreaterEqual(school.tuition, 0, f"{key} học phí >= 0")
            self.assertGreaterEqual(school.days, 0, f"{key} thời gian >= 0")
            self.assertTrue(school.name and school.emoji, key)

    def test_higher_school_costs_more_and_takes_longer(self) -> None:
        tuits = [career.SCHOOLS[k].tuition for k in career.SCHOOL_ORDER]
        self.assertEqual(tuits, sorted(tuits), "Học cấp cao phải đắt hơn")
        days = [career.SCHOOLS[k].days for k in career.SCHOOL_ORDER]
        self.assertEqual(days, sorted(days), "Học cấp cao phải lâu hơn")

    def test_tuition_is_realistically_large(self) -> None:
        """Học phí phải đủ nặng để không thể bơm học vấn trong vài ngày."""
        self.assertGreaterEqual(career.SCHOOLS["dai_hoc"].tuition, 30_000_000)
        self.assertGreaterEqual(career.SCHOOLS["tien_sy"].tuition, 200_000_000)

    def test_rank_and_unknown(self) -> None:
        self.assertEqual(career.school_rank("chua_hoc"), 0)
        self.assertEqual(career.school_rank("tien_sy"), 4)
        self.assertEqual(career.school_rank("khong_co"), -1)

    def test_next_school(self) -> None:
        self.assertEqual(career.next_school("chua_hoc").key, "thpt")
        self.assertIsNone(career.next_school("tien_sy"), "Đã cao nhất")
        self.assertIsNone(career.next_school("khong_co"))

    def test_cannot_skip_a_level(self) -> None:
        """Không được nhảy từ chưa tốt nghiệp lên Đại học."""
        self.assertFalse(career.can_enroll("chua_hoc", "dai_hoc"))
        self.assertTrue(career.can_enroll("chua_hoc", "thpt"))
        self.assertTrue(career.can_enroll("thpt", "dai_hoc"))
        self.assertFalse(career.can_enroll("thpt", "thac_si"))
        self.assertFalse(career.can_enroll("chua_hoc", "khong_co"))

    def test_cannot_repeat_current_level(self) -> None:
        self.assertFalse(career.can_enroll("thpt", "thpt"))

    def test_study_progress(self) -> None:
        school = career.SCHOOLS["dai_hoc"]
        self.assertEqual(career.study_left(school, T0, T0), 45)
        self.assertEqual(career.study_left(school, T0, T0 + 44 * DAY), 1)
        self.assertEqual(career.study_left(school, T0, T0 + 45 * DAY), 0)
        self.assertEqual(career.study_left(school, T0, T0 + 999 * DAY), 0)
        self.assertTrue(career.is_graduated(school, T0, T0 + 45 * DAY))
        self.assertFalse(career.is_graduated(school, T0, T0 + 44 * DAY))

    def test_zero_day_school_always_graduated(self) -> None:
        self.assertTrue(career.is_graduated(career.SCHOOLS["chua_hoc"], T0, T0))


class JobTableTests(unittest.TestCase):
    """Bảng nghề."""

    def test_every_job_is_valid(self) -> None:
        for key, job in career.JOBS.items():
            self.assertEqual(job.key, key)
            self.assertGreater(job.daily_salary, 0, f"{key} lương phải > 0")
            self.assertIn(job.min_school, career.SCHOOL_ORDER, f"{key} sai bằng cấp")

    def test_higher_education_unlocks_better_paying_jobs(self) -> None:
        """Lương tối đa phải tăng theo bằng cấp — học có ý nghĩa."""
        best = []
        for key in career.SCHOOL_ORDER:
            jobs = career.jobs_for(key)
            best.append(max((j.daily_salary for j in jobs), default=0))
        self.assertEqual(best, sorted(best), "Bằng cấp cao phải mở nghề lương cao hơn")
        self.assertGreater(best[-1], best[0])

    def test_can_apply_respects_education(self) -> None:
        self.assertTrue(career.can_apply("chua_hoc", "cong_nhan"))
        self.assertFalse(career.can_apply("chua_hoc", "bac_si"))
        self.assertTrue(career.can_apply("dai_hoc", "bac_si"))
        self.assertFalse(career.can_apply("dai_hoc", "lanh_daoo"))
        self.assertTrue(career.can_apply("tien_sy", "lanh_daoo"))

    def test_can_apply_rejects_unknown_job(self) -> None:
        self.assertFalse(career.can_apply("tien_sy", "phi_han_tinh"))

    def test_jobs_for_unknown_school_is_empty(self) -> None:
        self.assertEqual(career.jobs_for("khong_co"), [])

    def test_salary_is_monotonic_in_seniority(self) -> None:
        for key in career.JOBS:
            pays = [career.salary_for(key, lvl) for lvl in range(career.MAX_SENIORITY + 1)]
            self.assertEqual(pays, sorted(pays), f"{key} lương phải tăng theo bậc")
            self.assertGreater(pays[-1], pays[0], f"{key} bậc cao phải lương cao hơn")

    def test_salary_clamps_out_of_range_level(self) -> None:
        self.assertEqual(career.salary_for("cong_nhan", -5),
                         career.salary_for("cong_nhan", 0))
        self.assertEqual(career.salary_for("cong_nhan", 99),
                         career.salary_for("cong_nhan", career.MAX_SENIORITY))

    def test_unknown_job_pays_zero(self) -> None:
        self.assertEqual(career.salary_for("khong_co", 2), 0)
        self.assertEqual(career.work_pay("khong_co", 2, 5), 0)

    def test_salary_scales_match_economy(self) -> None:
        """Lương/ngày phải cùng cỡ với ``games.DAILY_BASE`` (300.000đ)."""
        self.assertGreaterEqual(career.JOBS["cong_nhan"].daily_salary, 300_000)
        self.assertLessEqual(career.JOBS["cong_nhan"].daily_salary, 500_000)


class SeniorityTests(unittest.TestCase):
    """Bậc kinh nghiệm."""

    def test_thresholds_match_table(self) -> None:
        for idx, (need, _mult, _title) in enumerate(career.SENIORITY):
            if idx == 0:
                self.assertEqual(career.seniority_for(need - 1), 0)
                continue
            self.assertEqual(career.seniority_for(need - 1), idx - 1, f"bậc {idx}")
            self.assertEqual(career.seniority_for(need), idx, f"bậc {idx}")
        self.assertEqual(career.seniority_for(10_000), career.MAX_SENIORITY)

    def test_negative_days_is_level_zero(self) -> None:
        self.assertEqual(career.seniority_for(-5), 0)

    def test_titles_differ_per_level(self) -> None:
        titles = [career.seniority_title(i) for i in range(career.MAX_SENIORITY + 1)]
        self.assertEqual(len(set(titles)), len(titles), "Chức danh phải khác nhau")

    def test_next_promotion_counts_remaining_days(self) -> None:
        self.assertEqual(career.next_promotion(0), (30, career.SENIORITY[1][2]))
        self.assertEqual(career.next_promotion(29), (1, career.SENIORITY[1][2]))

    def test_next_promotion_none_at_max(self) -> None:
        self.assertIsNone(career.next_promotion(10_000))


class WorkPayTests(unittest.TestCase):
    """Tiền công — phần dễ bị khai thác nhất."""

    def test_no_streak_pays_base(self) -> None:
        self.assertEqual(career.work_pay("cong_nhan", 0, 1),
                         career.salary_for("cong_nhan", 0))

    def test_streak_increases_pay(self) -> None:
        base = career.work_pay("cong_nhan", 0, 1)
        self.assertGreater(career.work_pay("cong_nhan", 0, 5), base)
        self.assertGreater(career.work_pay("cong_nhan", 0, 20),
                           career.work_pay("cong_nhan", 0, 5))

    def test_streak_bonus_is_capped(self) -> None:
        """Không thể nhân lương vô hạn chỉ bằng cách không nghỉ việc."""
        at_cap = career.work_pay("cong_nhan", 0, career.STREAK_MAX + 1)
        way_past = career.work_pay("cong_nhan", 0, 100_000)
        self.assertEqual(at_cap, way_past, "Phải trần hoá thưởng streak")
        ceiling = career.salary_for("cong_nhan", 0) * (
            1 + career.STREAK_BONUS * career.STREAK_MAX) + 1
        self.assertLessEqual(at_cap, ceiling, "Thưởng không được vượt trần")

    def test_zero_or_negative_streak_pays_base(self) -> None:
        base = career.work_pay("cong_nhan", 0, 1)
        self.assertEqual(career.work_pay("cong_nhan", 0, 0), base)
        self.assertEqual(career.work_pay("cong_nhan", 0, -3), base)

    def test_pay_always_positive_for_valid_job(self) -> None:
        for key in career.JOBS:
            for lvl in range(career.MAX_SENIORITY + 1):
                self.assertGreater(career.work_pay(key, lvl, 10), 0, f"{key} lvl={lvl}")

    def test_top_pay_is_bounded(self) -> None:
        """Lương cao nhất game không được vượt xa kiến trúc kinh tế hiện có."""
        top = max(career.work_pay(k, career.MAX_SENIORITY, 999) for k in career.JOBS)
        self.assertLess(top, 100_000_000, "Lương đỉnh phải vẫn là tiền trong game")


if __name__ == "__main__":
    unittest.main()


# -*- coding: utf-8 -*-
"""
Giáo dục & sự nghiệp — học vấn quyết định nghề, nghề quyết định lương
====================================================================

Vì sao cần module này
---------------------
Trước đây tiền chỉ đến từ ``!work`` của ``cogs/games.py`` (mức cố định) và
từ công ty. Không có đường để **kiếm nhiều hơn bằng cách học giỏi** — nên
"đời thực" chỉ dừng ở giá cả. Module này khép lại vòng lặp:

    học vấn (tốn học phí + thời gian) -> xin việc -> lương/ngày
    -> dùng lương đó mua thực phẩm ở ``!muadi`` của ``cogs/realworld.py``.

Nguyên tắc thiết kế
-------------------
* **Lương/ngày** suy từ lương tháng thực tế Việt Nam chia 30, cho khớp nhịp
  ``cogs/games.py`` vốn trả lương theo ngày.
* **Học phí** trả **một lần** lúc nhập học, học **theo thời gian thật** nên
  không thể bơm học vấn bằng cách spam lệnh.
* **Thăng chức** cần đủ số ngày đã đi làm — không có đường rút tiền nhanh.
* Học vấn là **chuỗi tuần tự**: không thể nhảy từ THPT lên Tiến sĩ.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

#: Một ngày tính theo giây.
DAY = 86_400

#: Thưởng khi làm việc liên tục nhiều ngày (streak), tính theo ngày.
STREAK_BONUS = 0.05          # 5% mỗi ngày liên tục
STREAK_MAX = 20              # trần: tối đa nhân 100%
#: Ngày nghỉ bắt buộc sau khi nghỉ việc — chống nghỉ/ứng tuyển lại để reset
#: seniority rồi leo lại từ đầu.
REHIRE_COOLDOWN = 3

#: Bậc kinh nghiệm: ``(số ngày đi làm tối thiểu, hệ số lương, chức danh)``.
#: Chỉ lên được một bậc mỗi lần, và phải đủ ngày.
SENIORITY: tuple[tuple[int, float, str], ...] = (
    (0, 1.00, "Nhân viên"),
    (30, 1.35, "Chuyên viên"),
    (90, 1.75, "Trưởng phòng"),
    (180, 2.20, "Giám đốc"),
)
#: Bậc cao nhất (chỉ số vào ``SENIORITY``).
MAX_SENIORITY = len(SENIORITY) - 1


@dataclass(frozen=True, slots=True)
class School:
    """Một bậc học trong hệ thống."""

    key: str
    name: str
    tuition: int          # học phí trả một lần (VNĐ)
    days: int             # thời gian học (ngày)
    emoji: str
    desc: str


#: Chuỗi học vấn, đi theo đúng thứ tự: cấp 3 -> cử nhân -> thạc sĩ -> tiến sĩ.
#: ``chua_hoc`` = mặc định, chưa có bằng nào (ai cũng bắt đầu ở đây).
SCHOOLS: dict[str, School] = {
    s.key: s
    for s in (
        School("chua_hoc", "Chưa tốt nghiệp", 0, 0, ":question:",
               "Chưa có bằng. Muốn việc ổn định thì hãy học tiếp."),
        School("thpt", "Trung học phổ thông", 0, 14, ":school:",
               "Miễn phí. Nền tảng để xin các công việc kỹ thuật."),
        School("dai_hoc", "Đại học (Cử nhân)", 45_000_000, 45, ":mortar_board:",
               "Mở khóa nghề giỏi như kỹ sư, bác sĩ, luật sư."),
        School("thac_si", "Thạc sĩ", 120_000_000, 90, ":scroll:",
               "Chuyên sâu hơn, mở khóa vị trí quản lý chuyên môn."),
        School("tien_sy", "Tiến sĩ", 250_000_000, 180, ":mortar_board:",
               "Cao nhất. Dẫn dắt doanh nghiệp, lương khá nhất game."),
    )
}
#: Thứ tự học vấn từ thấp đến cao (dùng để kiểm tra "bước kế tiếp").
SCHOOL_ORDER: tuple[str, ...] = ("chua_hoc", "thpt", "dai_hoc", "thac_si", "tien_sy")


@dataclass(frozen=True, slots=True)
class Job:
    """Một nghề nghiệp, mở khoá theo bằng cấp."""

    key: str
    name: str
    min_school: str        # bằng cấp tối thiểu
    daily_salary: int      # lương một ngày ở bậc 0 (VNĐ)
    emoji: str
    desc: str


#: Lương/ngày suy ra từ lương tháng thực tế ÷ 30 cho đúng nhịp ``games.py``.
JOBS: dict[str, Job] = {
    j.key: j
    for j in (
        Job("cong_nhan", "Công nhân", "chua_hoc", 300_000, ":construction_worker:",
            "Làm nhiều, thu nhập ổn, không cần bằng."),
        Job("van_phong", "Nhân viên văn phòng", "chua_hoc", 400_000, ":clipboard:",
            "Công việc giấy tờ, ngày ngày 8 tiếng."),
        Job("ban_hang", "Nhân viên bán hàng", "chua_hoc", 500_000, ":package:",
            "Chăm sóc khách, thuộng theo đơn hàng."),
        Job("ky_su_it", "Kỹ sư IT", "thpt", 830_000, ":computer:",
            "Lương cao, đòi hỏi tốt nghiệp THPT trở lên."),
        Job("y_ta", "Điều dưỡng", "thpt", 600_000, ":syringe:",
            "Bệnh viện, ca kíp dài. Ổn định, ít biến động."),
        Job("giang_vien", "Giảng viên", "dai_hoc", 1_000_000, ":books:",
            "Dạy học, nghề ổn định, được kính trọng."),
        Job("bac_si", "Bác sĩ", "dai_hoc", 1_500_000, ":stethoscope:",
            "Cử nhân y khoa. Lương cao, nghề đòi hỏi."),
        Job("luat_su", "Luật sư", "dai_hoc", 1_600_000, ":scales:",
            "Tư vấn pháp lý, thu nhập cao nếu có kinh nghiệm."),
        Job("ke_toan", "Kế toán trưởng", "thac_si", 2_000_000, ":bar_chart:",
            "Quản lý số sách — cần học vấn cao."),
        Job("lanh_daoo", "Tổng giám đốc", "tien_sy", 2_600_000, ":trophy:",
            "Đỉnh cao của sự nghiệp. Chỉ dành cho tiến sĩ."),
    )
}


def school_rank(key: str) -> int:
    """Vị trí trong thang học vấn (-1 nếu không biết)."""
    try:
        return SCHOOL_ORDER.index(key)
    except ValueError:
        return -1


def next_school(key: str) -> School | None:
    """Bậc học kế tiếp sau ``key`` (``None`` nếu đã cao nhất)."""
    idx = school_rank(key)
    if idx < 0 or idx + 1 >= len(SCHOOL_ORDER):
        return None
    return SCHOOLS[SCHOOL_ORDER[idx + 1]]


def can_enroll(current: str, target: str) -> bool:
    """Được nhập học ``target`` không (chỉ bước kế tiếp, không nhảy cấp)."""
    nxt = next_school(current)
    return nxt is not None and nxt.key == target


def jobs_for(school: str) -> list[Job]:
    """Danh sách nghề mở khoá với bằng cấp ``school`` (cao hơn thì cũng mở)."""
    rank = school_rank(school)
    if rank < 0:
        return []
    return [j for j in JOBS.values() if school_rank(j.min_school) <= rank]


def can_apply(school: str, job: str) -> bool:
    """Bằng cấp có đủ để xin nghề ``job`` không."""
    entry = JOBS.get(job)
    if entry is None:
        return False
    return school_rank(school) >= school_rank(entry.min_school)


def seniority_for(days_worked: int) -> int:
    """Bậc kinh nghiệm hiện đạt được sau ``days_worked`` ngày đi làm."""
    level = 0
    for idx, (need, _mult, _title) in enumerate(SENIORITY):
        if days_worked >= need:
            level = idx
        else:
            break
    return min(level, MAX_SENIORITY)


def seniority_title(level: int) -> str:
    """Chức danh ứng với bậc ``level``."""
    idx = max(0, min(int(level), MAX_SENIORITY))
    return SENIORITY[idx][2]


def next_promotion(days_worked: int) -> tuple[int, str] | None:
    """Thông tin bậc kế tiếp: ``(số ngày còn thiếu, chức danh)``.

    ``None`` nghĩa là đã lên tối đa rồi.
    """
    current = seniority_for(days_worked)
    if current >= MAX_SENIORITY:
        return None
    need, _mult, title = SENIORITY[current + 1]
    return max(0, need - days_worked), title


def salary_for(job: str, level: int) -> int:
    """Lương một ngày của nghề ``job`` ở bậc ``level``."""
    entry = JOBS.get(job)
    if entry is None:
        return 0
    idx = max(0, min(int(level), MAX_SENIORITY))
    return int(entry.daily_salary * SENIORITY[idx][1])


def work_pay(job: str, level: int, streak: int = 0) -> int:
    """Tiền công một ngày làm việc, gồm thưởng chuyên cần.

    Thưởng streak bị **chặn trần** ở :data:`STREAK_MAX` ngày để không thể
    nhân lương vô hạn chỉ bằng cách không nghỉ việc.
    """
    base = salary_for(job, level)
    if base <= 0:
        return 0
    capped = max(0, min(int(streak) - 1, STREAK_MAX))
    return int(base * (1.0 + STREAK_BONUS * capped))


def study_left(school: School, started_at: float, now: float | None = None) -> int:
    """Số ngày còn lại để tốt nghiệp (0 nếu đã xong)."""
    if school.days <= 0:
        return 0
    current = time.time() if now is None else now
    return max(0, school.days - int((current - started_at) // DAY))


def is_graduated(school: School, started_at: float, now: float | None = None) -> bool:
    """Đã hết thời gian học chưa."""
    return study_left(school, started_at, now) == 0


__all__ = (
    "DAY",
    "JOBS",
    "Job",
    "MAX_SENIORITY",
    "REHIRE_COOLDOWN",
    "SCHOOLS",
    "SCHOOL_ORDER",
    "SENIORITY",
    "STREAK_BONUS",
    "STREAK_MAX",
    "School",
    "can_apply",
    "can_enroll",
    "is_graduated",
    "jobs_for",
    "next_promotion",
    "next_school",
    "salary_for",
    "school_rank",
    "seniority_for",
    "seniority_title",
    "study_left",
    "work_pay",
)


# -*- coding: utf-8 -*-
"""
Sổ tiết kiệm nhiều loại — mô phỏng ngân hàng Việt Nam
====================================================

Vì sao thêm ngoài ``users.bank`` hiện có
---------------------------------------
``users.bank`` là **một** khoản tiết kiệm không kỳ hạn duy nhất, lãi suất
nhỏ, không rút trước hạn được. Đời thật người ta mở **nhiều sổ** theo mục
đích khác nhau: kỳ hạn ngắn để có lãi cao, kỳ hạn dài để khóa lãi, tích
lũy tăng dần... Đây là hệ thống bổ sung, **không thay thế** sổ cũ — tiền
gửi ở đây vẫn nằm trong cùng hệ thống, ai cũng yên tâm.

Cơ chế
------
* Mỗi sổ có ``term_days`` (ngày) và lãi suất **cao hơn kỳ hạn càng dài** —
  đúng nguyên tắc đời thật.
* Rút trước hạn được nhưng **mất lãi** cho kỳ hạn đó (ngân hàng thật vậy).
* Lãi tính **đơn theo ngày** (~4,5–9,6%/năm, như ngân hàng thật), cộng
  vào số dư — KHÔNG lãi kép vì sẽ thành hơn 100%/năm.
* Mở sổ tốn phí thủ tục, tối đa số sổ để không lạm phát.

Lưu ý tiền tệ
--------------
Đây là **tiền ảo trong Discord**, không liên quan tới thanh toán thật.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

#: Phí mở sổ (giống phí thủ tục nhỏ ở ngân hàng).
OPEN_FEE = 50_000
#: Tối đa số sổ mỗi người — chặn spam lệnh và giữ DB gọn.
MAX_ACCOUNTS = 8
#: Số dư tối thiểu mới được mở sổ (mở sổ 10.000đ thì vô nghĩa).
MIN_OPEN = 100_000
#: Phần lãi bị mất khi rút trước hạn.
EARLY_PENALTY = 0.5


@dataclass(frozen=True, slots=True)
class Account:
    """Một sổ tiết kiệm.

    ``annual_rate`` là mức %/năm **mục tiêu** theo thực tế ngân hàng Việt
    Nam (~4,4% cho linh động tới ~8% cho kỳ hạn 12 tháng). ``daily_rate``
    được **suy ra** từ đó mỗi ngày (chia đơn cho 365) để người chơi không
    phải tự nhân vật.
    """

    key: str
    name: str
    term_days: int          # 0 = không kỳ hạn (rút tự do)
    annual_rate: float      # %/năm (lãi đơn)
    emoji: str
    desc: str

    @property
    def daily_rate(self) -> float:
        """Lãi suất lãi đơn mỗi ngày (phần thập phân)."""
        return self.annual_rate / 100.0 / 365.0

    @property
    def is_flexible(self) -> bool:
        """Sổ không kỳ hạn: rút bất cứ lúc nào, không mất lãi."""
        return self.term_days == 0


#: Danh mục sổ. Mức %/năm bám theo sản phẩm tiết kiệm thật của ngân hàng
#: Việt Nam: linh động ~4,4%/năm, kỳ hạn càng dài lãi càng cao tới ~8%.
#: Lãi **đơn theo ngày** — KHÔNG lãi kép, vì lãi kép 0,6%/ngày sẽ thành
#: hơn 100%/năm, vô lý so với ngân hàng thật.
ACCOUNTS: dict[str, Account] = {
    a.key: a
    for a in (
        Account(
            "linh_dong", "Tiết kiệm linh động", 0, 4.4, ":money_with_wings:",
            "Rút bất cứ lúc nào, không mất lãi. Lãi thấp nhưng tiện.",
        ),
        Account(
            "1tuan", "Kỳ hạn 1 tuần", 7, 5.2, ":calendar:",
            "Lãi cao hơn, nhưng rút sớm sẽ mất một nửa lãi kỳ hạn.",
        ),
        Account(
            "1thang", "Kỳ hạn 1 tháng", 30, 5.8, ":date:",
            "Lãi khá hơn — quỹ dự phòng chi phí phát sinh trong tháng.",
        ),
        Account(
            "3thang", "Kỳ hạn 3 tháng", 90, 6.6, ":calendar:",
            "Lãi tốt. Phù hợp để dành tiền mua sắm lớn.",
        ),
        Account(
            "6thang", "Kỳ hạn 6 tháng", 180, 7.4, ":calendar:",
            "Lãi khá cao, đánh đổi thời gian khóa vốn.",
        ),
        Account(
            "1nam", "Kỳ hạn 1 năm", 365, 8.0, ":trophy:",
            "Lãi cao nhất trong các kỳ hạn tiền gửi.",
        ),
        Account(
            "tich_luy", "Sổ tích lũy", 0, 5.5, ":piggy_bank:",
            "Mục tiêu dài hạn: lãi tăng dần theo số dư đang có.",
        ),
    )
}

#: Sổ tích lũy trả lãi **cộng dồn** theo bậc số dư (giống sản phẩm
#: "tích lũy tăng dần" thật): càng để nhiều, lãi càng cao. Đơn vị %/năm.
PROGRESSIVE_TIERS: tuple[tuple[int, float], ...] = (
    (0, 5.0),                 # dưới 10 triệu
    (10_000_000, 5.8),        # từ 10 triệu
    (50_000_000, 6.6),        # từ 50 triệu
    (200_000_000, 7.4),       # từ 200 triệu
)


def progressive_rate(balance: int) -> float:
    """Lãi suất %/năm của sổ tích lũy theo bậc số dư hiện có."""
    rate = PROGRESSIVE_TIERS[0][1]
    for threshold, value in PROGRESSIVE_TIERS:
        if balance >= threshold:
            rate = value
    return rate


def annual_for(key: str, balance: int = 0) -> float:
    """Lãi suất %/năm thực tế áp dụng cho một sổ (xử lý sổ tích lũy)."""
    account = ACCOUNTS.get(key)
    if account is None:
        return 0.0
    if account.key == "tich_luy":
        return progressive_rate(balance)
    return account.annual_rate


def rate_for(key: str, balance: int = 0) -> float:
    """Lãi suất lãi đơn mỗi ngày (phần thập phân) của một sổ."""
    return annual_for(key, balance) / 100.0 / 365.0


def interest_for(key: str, balance: int, days: int = 1) -> int:
    """Lãi 1 khoản (lãi kép: chỉ trả 1 lần, lãi mới nhập vào số dư)."""
    account = ACCOUNTS.get(key)
    if account is None or balance <= 0 or days <= 0:
        return 0
    return int(balance * rate_for(key, balance) * days)


def early_withdraw_penalty(key: str, balance: int) -> int:
    """Tiền lãi bị mất khi rút trước hạn (0 nếu sổ linh động)."""
    account = ACCOUNTS.get(key)
    if account is None or account.is_flexible or balance <= 0:
        return 0
    return int(interest_for(key, balance, 1) * EARLY_PENALTY)


def is_locked(key: str, opened_at: float, now_ts: float | None = None) -> bool:
    """Sổ còn trong kỳ hạn (chưa đủ hạn) hay đã được rút tự do."""
    account = ACCOUNTS.get(key)
    if account is None or account.is_flexible:
        return False
    current = time.time() if now_ts is None else now_ts
    return current < opened_at + account.term_days * 86_400


def term_left(key: str, opened_at: float, now_ts: float | None = None) -> int:
    """Số ngày còn lại của kỳ hạn (0 nếu linh động hoặc đã hết hạn)."""
    account = ACCOUNTS.get(key)
    if account is None or account.is_flexible:
        return 0
    current = time.time() if now_ts is None else now_ts
    left = account.term_days - int((current - opened_at) // 86_400)
    return max(0, left)


def annual_percent(daily: float) -> float:
    """Quy đổi lãi/ngày (đơn) ra %/năm — con số người chơi dễ hình dung."""
    return daily * 365.0 * 100.0


def daily_percent(key: str, balance: int = 0) -> float:
    """Lãi/ngày hiển thị dạng % (vd ``0.022`` = 0,022%/ngày)."""
    return rate_for(key, balance) * 100.0


__all__ = (
    "ACCOUNTS",
    "Account",
    "EARLY_PENALTY",
    "MAX_ACCOUNTS",
    "MIN_OPEN",
    "OPEN_FEE",
    "PROGRESSIVE_TIERS",
    "annual_for",
    "annual_percent",
    "daily_percent",
    "early_withdraw_penalty",
    "interest_for",
    "is_locked",
    "progressive_rate",
    "rate_for",
    "term_left",
)
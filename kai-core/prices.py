# -*- coding: utf-8 -*-
"""
Giá cả ngoài đời thật Việt Nam + sổ tiết kiệm nhiều loại
=========================================================

Vì sao tách riêng
----------------
Giá hàng hoá trong ``cogs/games.py`` là **tài sản dài hạn** (xe, nhà, đất)
— mua một lần, giữ nhiều năm. Còn ở đây là **tiêu sảm tiêu dùng hằng ngày**
(ăn, đi chợ, xăng) — mua đi tiêu, giá nhích theo thị trường.

Hai hệ thống khác nhau về bản chất nên tách file, dễ test và dễ bảo trì.

Giá biến động thế nào
---------------------
Giá mỗi ngày **cố định cho cả server** (seed theo ngày) — y hệt cách
``games.daily_rate()`` công bố lãi suất: không ai "trúng số" mua rẻ hơn
người khác trong cùng một ngày, nhưng đến ngày mai thì giá đổi. Biên độ
nhỏ (vài %) vì giá thực tế không biến động mạnh như sàn chứng khoán.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

#: Biên độ dao động giá hằng ngày (0.08 = ±8%). Thực tế giá thực phẩm Việt
#: Nam nhích quanh ±5–8% trong một ngày; chọn biên hẹp để giá "đời thật".
#: Lưu ý: giá bán lẻ được làm tròn theo bậc tiền nên biên thực tế có thể
#: lệch thêm một bước làm tròn — cùng ngày thì giá vẫn cố định cho cả server.
DAILY_SWING = 0.08
#: Giá sàn: không bao giờ rơi quá 60% mức niêm yết (chống rẻ 0 đồng quá xa
#: thực tế và tránh lạm phát/lỗ hổng do giá âm).
FLOOR_RATIO = 0.60


def day_seed(stamp: float | None = None) -> int:
    """Số ngày epoch — seed chung cho toàn server."""
    return int((stamp if stamp is not None else time.time()) // 86_400)


def day_rng(stamp: float | None = None) -> random.Random:
    """RNG theo ngày — cùng một ngày luôn cho cùng kết quả."""
    return random.Random(day_seed(stamp))


@dataclass(frozen=True, slots=True)
class Good:
    """Một mặt hàng tiêu sảm."""

    key: str
    name: str
    unit: str            # đơn vị bán ra: "kg", "hộp", "chai", "cái"...
    base_price: int      # giá niêm yết (VNĐ)
    emoji: str
    group: str           # nhóm để xem gọn trong !giatham
    essential: bool = False   # True = nhu yếu phẩm (AI hay gợi ý khi thiếu)


#: Bảng giá lấy theo giá thị trường Việt Nam 2024–2025. ``base_price`` là giá
#: **một đơn vị bán lẻ** (1kg thịt = giá 1kg, không phải giá 1 lạng).
GOODS: dict[str, Good] = {
    g.key: g
    for g in (
        # --- Thực phẩm tươi sống ---
        Good("gao", "Gạo", "kg", 18_000, ":rice:", "thuc_pham", True),
        Good("thit_bo", "Thịt bò", "kg", 240_000, ":cow2:", "thuc_pham", True),
        Good("thit_ga", "Thịt gà", "kg", 65_000, ":chicken:", "thuc_pham", True),
        Good("ca", "Cá", "kg", 90_000, ":fish:", "thuc_pham", True),
        Good("trung", "Trứng", "quả", 4_500, ":egg:", "thuc_pham", True),
        Good("rau", "Rau xanh", "kg", 12_000, ":herb:", "thuc_pham", True),
        Good("bia", "Bia", "lon", 15_000, ":beer:", "thuc_pham", False),
        # --- Gia vị & đồ ăn sẵn ---
        Good("nuoc_moi", "Nước mắm", "chai", 32_000, ":bottle:", "gia_vi", True),
        Good("dau_an", "Dầu ăn", "chai", 78_000, ":oil_drum:", "gia_vi", True),
        Good("muoi", "Muối ăn", "kg", 7_000, ":salt:", "gia_vi", True),
        Good("duong", "Đường", "kg", 22_000, ":candy:", "gia_vi", True),
        Good("mi_an", "Mì gói", "hộp", 6_500, ":instant_noodles:", "do_an", True),
        # --- Nước uống ---
        Good("nuoc_loc", "Nước lọc", "thùng", 25_000, ":droplet:", "nuoc_uong", True),
        Good("coca", "Nước ngọt", "chai", 18_000, ":bottle:", "nuoc_uong", False),
        Good("ca_phe", "Cà phê", "ly", 30_000, ":coffee:", "nuoc_uong", False),
        # --- Nhà cửa ---
        Good("dien", "Điện", "kWh", 4_200, ":bulb:", "nha_cua", True),
        Good("nuoc", "Nước máy", "m3", 16_000, ":droplet:", "nha_cua", True),
        Good("xang", "Xăng", "lít", 22_500, ":fuelpump:", "di_chuyen", True),
        Good("internet", "Internet", "tháng", 250_000, ":satellite:", "nha_cua", True),
        Good("dien_mat", "Hóa đơn điện", "tháng", 380_000, ":zap:", "nha_cua", True),
        Good("nuoc_mat", "Hóa đơn nước", "tháng", 150_000, ":water_buffalo:", "nha_cua", True),
    )
}


def _round_price(value: float) -> int:
    """Làm tròn giá theo **mức tiền** cho hợp lý với bán lẻ ở Việt Nam.

    Món rẻ (4.500đ/quả trứng) **không** được làm tròn tới nghìn — sẽ vỡ
    nát giá và giá "nhích" theo ngày sẽ thành gãy lạ (một ngày rẻ nhất,
    hôm sau đắt nhất). Nên:
      * dưới 10.000đ  -> tròn 100đ
      * dưới 100.000đ -> tròn 500đ
      * còn lại        -> tròn 1.000đ
    """
    if value < 10_000:
        step = 100
    elif value < 100_000:
        step = 500
    else:
        step = 1_000
    return max(step, int(round(value / step) * step))


def quote(key: str, stamp: float | None = None) -> int:
    """Giá bán lẻ hôm nay của một mặt hàng (VNĐ).

    Dao động **theo ngày** và **theo mặt hàng** (seed lẫn ``key``) để rau với
    thịt không biến động giống nhau.
    """
    good = GOODS.get(key)
    if good is None:
        return 0
    rng = random.Random(f"{day_seed(stamp)}:{key}")
    factor = 1.0 + rng.uniform(-DAILY_SWING, DAILY_SWING)
    price = good.base_price * factor
    price = max(good.base_price * FLOOR_RATIO, price)
    return _round_price(price)


def quote_change(key: str, stamp: float | None = None) -> float:
    """Biến động (%) so với giá niêm yết — dương là tăng, âm là giảm."""
    good = GOODS.get(key)
    if good is None or good.base_price <= 0:
        return 0.0
    return (quote(key, stamp) - good.base_price) / good.base_price * 100.0


def all_quotes(stamp: float | None = None) -> list[tuple[Good, int, float]]:
    """Danh sách ``(hàng, giá hôm nay, % biến động)`` theo nhóm."""
    return [
        (good, quote(key, stamp), quote_change(key, stamp))
        for key, good in GOODS.items()
    ]


def group_label(group: str) -> str:
    """Tên nhóm hiển thị tiếng Việt."""
    return {
        "thuc_pham": "🥩 Thực phẩm tươi",
        "gia_vi": "🧂 Gia vị & nguyên liệu",
        "do_an": "🍜 Đồ ăn sẵn",
        "nuoc_uong": "🥤 Nước uống",
        "nha_cua": "🏠 Hóa đơn nhà cửa",
        "di_chuyen": "🛵 Di chuyển",
    }.get(group, group)


__all__ = (
    "DAILY_SWING",
    "Good",
    "GOODS",
    "all_quotes",
    "day_rng",
    "day_seed",
    "group_label",
    "quote",
    "quote_change",
)
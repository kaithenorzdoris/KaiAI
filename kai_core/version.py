"""Single source of truth cho version của KaiAI.

Yêu cầu #35: mọi nơi trong dự án (README, ``/about``, ``/version``, manifest,
Docker label, tài liệu) **phải** đọc version từ đây thay vì hard-code chuỗi
riêng, để không xảy ra tình trạng "5.0" ở file này, "6.0" ở file kia.
"""

from __future__ import annotations

from pathlib import Path

#: Phiên bản chuẩn của dự án (semver).
VERSION = "7.0.0"

#: Tên hiển thị.
APP_NAME = "KaiAI"

#: Mô tả ngắn dùng cho user-agent và slash command description.
APP_SUMMARY = "AI ?? Games ?? Community"

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"


def read_version_file() -> str | None:
    """Đọc ``VERSION`` nếu tồn tại (dùng khi đóng gói release)."""
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def version_string() -> str:
    """Chuỗi hiển thị đầy đủ, ví dụ ``KaiAI v6.0.0``."""
    return f"{APP_NAME} v{VERSION}"


def ensure_version_file() -> bool:
    """Đồng bộ ``VERSION`` với :data:`VERSION`. Trả về True nếu đã ghi."""
    target = read_version_file()
    if target == VERSION:
        return False
    VERSION_FILE.write_text(VERSION + "\n", encoding="utf-8")
    return True


__all__ = ("VERSION", "APP_NAME", "APP_SUMMARY", "version_string", "read_version_file", "ensure_version_file")

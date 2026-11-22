"""
KaiAI Ultimate V7 — Plugin Manifest
===================================

Định nghĩa cấu trúc đặc tả (Manifest) cho các plugin mở rộng của KaiAI V7:
- Định danh (id), tên hiển thị, phiên bản, tác giả.
- Khai báo phiên bản tương thích tối thiểu (min_bot_version).
- Khai báo quyền hạn (permissions): 'read_economy', 'write_economy', 'ai_tools', 'system_admin'.
- Danh sách plugin phụ thuộc (dependencies).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PluginManifest:
    """Đặc tả siêu dữ liệu của một Plugin KaiAI."""

    id: str
    name: str
    version: str
    author: str = "KaiAI Community"
    description: str = ""
    min_bot_version: str = "7.0.0"
    permissions: set[str] = field(default_factory=set)
    dependencies: list[str] = field(default_factory=list)
    enabled_by_default: bool = True

    def validate(self) -> list[str]:
        """Kiểm tra tính hợp lệ của manifest."""
        errors: list[str] = []
        if not self.id or not self.id.replace("_", "").isalnum():
            errors.append("Plugin ID chỉ được chứa ký tự chữ cái, số và dấu gạch dưới.")
        if not self.name:
            errors.append("Tên hiển thị (name) không được để trống.")
        if not self.version:
            errors.append("Phiên bản (version) không được để trống.")
        return errors


__all__ = ("PluginManifest",)


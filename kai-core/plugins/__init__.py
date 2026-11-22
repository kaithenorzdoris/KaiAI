"""
KaiAI Ultimate V7 — Extensible Plugin System
============================================

Cung cấp kiến trúc plugin mở rộng:
- PluginManifest: Khai báo metadata, quyền hạn, phiên bản.
- BasePlugin: Lớp cơ sở có lifecycle và error isolation.
- PluginRegistry: Quản trị và kiểm tra tương thích.
"""

from __future__ import annotations

from .base import BasePlugin
from .manifest import PluginManifest
from .registry import GLOBAL_PLUGIN_REGISTRY, PluginError, PluginRegistry

__all__ = (
    "BasePlugin",
    "GLOBAL_PLUGIN_REGISTRY",
    "PluginError",
    "PluginManifest",
    "PluginRegistry",
)


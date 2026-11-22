"""
KaiAI Ultimate V7 — Plugin Registry & Lifecycle Manager
======================================================

Quản lý đăng ký, kích hoạt, vô hiệu hoá và kiểm tra tương thích plugin:
- Quản lý phiên bản tương thích.
- Xác thực dependencies và permissions.
- Cô lập lỗi khi nạp/dỡ plugin.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..logging_setup import get_logger
from ..version import VERSION
from .base import BasePlugin
from .manifest import PluginManifest

log = get_logger("plugins.registry")


class PluginError(Exception):
    """Lỗi quản lý plugin."""


class PluginRegistry:
    """Trung tâm quản trị toàn bộ các plugin trong KaiAI V7."""

    def __init__(self, bot: Any = None) -> None:
        self.bot = bot
        self._plugins: dict[str, BasePlugin] = {}
        self._lock = asyncio.Lock()

    @property
    def plugins(self) -> dict[str, BasePlugin]:
        return dict(self._plugins)

    def is_compatible(self, min_version: str) -> bool:
        """Kiểm tra tính tương thích phiên bản bot."""
        try:
            bot_parts = [int(p) for p in VERSION.split(".")[:3]]
            req_parts = [int(p) for p in min_version.split(".")[:3]]
            return bot_parts >= req_parts
        except Exception:
            return True

    async def register(self, plugin: BasePlugin) -> bool:
        """Đăng ký và khởi tạo một plugin mới."""
        async with self._lock:
            manifest = plugin.manifest
            errors = manifest.validate()
            if errors:
                raise PluginError(f"Manifest không hợp lệ: {', '.join(errors)}")

            if not self.is_compatible(manifest.min_bot_version):
                log.warning(
                    "Bỏ qua plugin '%s': Yêu cầu KaiAI >= v%s nhưng hiện tại là v%s",
                    manifest.id,
                    manifest.min_bot_version,
                    VERSION,
                )
                return False

            # Kiểm tra dependencies
            for dep in manifest.dependencies:
                if dep not in self._plugins:
                    log.warning(
                        "Plugin '%s' thiếu dependency '%s' chưa được nạp",
                        manifest.id,
                        dep,
                    )
                    return False

            if manifest.id in self._plugins:
                log.info("Plugin '%s' đã được đăng ký trước đó.", manifest.id)
                return True

            try:
                await plugin.on_load()
                self._plugins[manifest.id] = plugin
                log.info("✅ Đã đăng ký plugin: %s v%s", manifest.name, manifest.version)
                if manifest.enabled_by_default:
                    await plugin.on_enable()
                return True
            except Exception as exc:
                log.error("Không thể nạp plugin '%s': %s", manifest.id, exc, exc_info=True)
                return False

    async def unregister(self, plugin_id: str) -> bool:
        """Dỡ bỏ một plugin khỏi hệ thống."""
        async with self._lock:
            plugin = self._plugins.get(plugin_id)
            if not plugin:
                return False

            try:
                if plugin.is_enabled:
                    await plugin.on_disable()
                await plugin.on_unload()
            except Exception as exc:
                log.error("Lỗi khi dỡ plugin '%s': %s", plugin_id, exc, exc_info=True)

            self._plugins.pop(plugin_id, None)
            log.info("Đã dỡ plugin: %s", plugin_id)
            return True

    async def enable(self, plugin_id: str) -> bool:
        """Bật một plugin đã nạp."""
        async with self._lock:
            plugin = self._plugins.get(plugin_id)
            if not plugin or plugin.is_enabled:
                return False
            try:
                await plugin.on_enable()
                log.info("Đã bật plugin: %s", plugin_id)
                return True
            except Exception as exc:
                log.error("Lỗi khi bật plugin '%s': %s", plugin_id, exc)
                return False

    async def disable(self, plugin_id: str) -> bool:
        """Tắt một plugin."""
        async with self._lock:
            plugin = self._plugins.get(plugin_id)
            if not plugin or not plugin.is_enabled:
                return False
            try:
                await plugin.on_disable()
                log.info("Đã tắt plugin: %s", plugin_id)
                return True
            except Exception as exc:
                log.error("Lỗi khi tắt plugin '%s': %s", plugin_id, exc)
                return False

    def get(self, plugin_id: str) -> BasePlugin | None:
        return self._plugins.get(plugin_id)


#: Singleton Registry
GLOBAL_PLUGIN_REGISTRY = PluginRegistry()

__all__ = (
    "GLOBAL_PLUGIN_REGISTRY",
    "PluginError",
    "PluginRegistry",
)


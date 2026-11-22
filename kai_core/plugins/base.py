"""
KaiAI Ultimate V7 — Base Plugin Class
=====================================

Lớp cơ sở cho toàn bộ các plugin mở rộng:
- Vòng đời: on_load, on_enable, on_disable, on_unload.
- Cô lập lỗi (Error Isolation): mọi exception trong callback đều được bắt và log an toàn.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..logging_setup import get_logger
from .manifest import PluginManifest

log = get_logger("plugins.base")


class BasePlugin:
    """Lớp nền tảng cho plugin mở rộng trong KaiAI V7."""

    def __init__(self, bot: Any, manifest: PluginManifest) -> None:
        self.bot = bot
        self.manifest = manifest
        self._enabled: bool = False

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    async def on_load(self) -> None:
        """Kích hoạt khi plugin được nạp vào bộ nhớ lần đầu."""
        pass

    async def on_enable(self) -> None:
        """Kích hoạt khi plugin được bật."""
        self._enabled = True

    async def on_disable(self) -> None:
        """Kích hoạt khi plugin bị tắt."""
        self._enabled = False

    async def on_unload(self) -> None:
        """Kích hoạt khi plugin được dỡ bỏ khỏi hệ thống."""
        self._enabled = False

    async def safe_execute(self, coro_factory, default_return: Any = None) -> Any:
        """Thực thi tác vụ có cô lập lỗi, ngăn chặn crash hệ thống chính."""
        try:
            return await coro_factory()
        except Exception as exc:
            log.error(
                "Lỗi trong plugin '%s' (%s): %s",
                self.id,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return default_return


__all__ = ("BasePlugin",)


"""
Unit & Integration Tests for KaiAI Ultimate V7 Plugin Architecture:
- Manifest validation
- Lifecycle management (register, enable, disable, unregister)
- Dependency checking
- Error isolation
"""

from __future__ import annotations

import unittest

from kai_core.plugins import (
    BasePlugin,
    PluginError,
    PluginManifest,
    PluginRegistry,
)


class DummyMockPlugin(BasePlugin):
    def __init__(self, bot, manifest: PluginManifest) -> None:
        super().__init__(bot, manifest)
        self.loaded = False
        self.unloaded = False

    async def on_load(self) -> None:
        self.loaded = True

    async def on_unload(self) -> None:
        self.unloaded = True


class FaultyPlugin(BasePlugin):
    async def on_load(self) -> None:
        raise RuntimeError("Crash on load!")


class PluginArchitectureTests(unittest.IsolatedAsyncioTestCase):
    """Kiểm thử tính đúng đắn và cô lập lỗi của Plugin Registry."""

    async def asyncSetUp(self) -> None:
        self.registry = PluginRegistry(bot=None)

    def test_manifest_validation(self) -> None:
        # Hợp lệ
        valid = PluginManifest(id="stock_radar", name="Stock Radar", version="1.0.0")
        self.assertEqual(len(valid.validate()), 0)

        # Sai ID
        invalid_id = PluginManifest(id="invalid-id!", name="Test", version="1.0.0")
        self.assertGreater(len(invalid_id.validate()), 0)

    async def test_register_and_lifecycle(self) -> None:
        manifest = PluginManifest(
            id="market_analyzer",
            name="Market Analyzer",
            version="1.0.0",
            permissions={"read_economy"},
        )
        plugin = DummyMockPlugin(bot=None, manifest=manifest)

        # Đăng ký
        ok = await self.registry.register(plugin)
        self.assertTrue(ok)
        self.assertTrue(plugin.loaded)
        self.assertTrue(plugin.is_enabled)
        self.assertEqual(self.registry.get("market_analyzer"), plugin)

        # Tắt
        disabled = await self.registry.disable("market_analyzer")
        self.assertTrue(disabled)
        self.assertFalse(plugin.is_enabled)

        # Bật lại
        enabled = await self.registry.enable("market_analyzer")
        self.assertTrue(enabled)
        self.assertTrue(plugin.is_enabled)

        # Dỡ bỏ
        unregistered = await self.registry.unregister("market_analyzer")
        self.assertTrue(unregistered)
        self.assertTrue(plugin.unloaded)
        self.assertIsNone(self.registry.get("market_analyzer"))

    async def test_dependency_checking(self) -> None:
        manifest = PluginManifest(
            id="crypto_trader",
            name="Crypto Trader",
            version="1.0.0",
            dependencies=["non_existent_plugin"],
        )
        plugin = DummyMockPlugin(bot=None, manifest=manifest)
        ok = await self.registry.register(plugin)
        self.assertFalse(ok, "Không được phép nạp khi thiếu dependency")

    async def test_error_isolation_on_load(self) -> None:
        manifest = PluginManifest(id="broken_plugin", name="Broken", version="1.0.0")
        plugin = FaultyPlugin(bot=None, manifest=manifest)
        ok = await self.registry.register(plugin)
        self.assertFalse(ok, "Plugin lỗi phải trả về False mà không làm vỡ registry")
        self.assertIsNone(self.registry.get("broken_plugin"))

    async def test_safe_execute_catches_exception(self) -> None:
        manifest = PluginManifest(id="safe_test", name="Safe Test", version="1.0.0")
        plugin = BasePlugin(bot=None, manifest=manifest)

        async def _bad_coro():
            raise ZeroDivisionError("Lỗi toán học!")

        res = await plugin.safe_execute(_bad_coro, default_return="recovered")
        self.assertEqual(res, "recovered")


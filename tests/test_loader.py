"""Kiểm thử bộ nạp Cog của ``main.py``.

Các test này cần discord.py; nếu thiếu, chúng tự động được bỏ qua thay vì báo lỗi.
"""

from __future__ import annotations

import pathlib
import unittest

try:
    import main as main_module
except Exception as exc:  # pragma: no cover - thiếu dependency
    main_module = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None

EXPECTED_COGS = {
    "admin",
    "ai_chat",
    "database",
    "diagnostics",
    "games",
    # Giai đoạn 1 — chợ + tiết kiệm. Đặt SAU ``games`` vì ``cogs/realworld.py``
    # cần bảng ``users`` / ``transactions`` mà ``games`` tạo ra.
    "realworld",
    # Giáo dục & sự nghiệp — đọc ``users``/``transactions`` của ``games``,
    # tự tạo bảng ``study``/``jobs`` (idempotent).
    "career",
    "kai_premium",
    "logs",
    "logs_center",
    "love",
    "system",
    "utility_pro",
}


@unittest.skipIf(main_module is None, f"Không import được main.py: {IMPORT_ERROR}")
class CogLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        assert main_module is not None
        self.root = pathlib.Path(main_module.__file__).resolve().parent

    def test_preferred_cogs_constant(self) -> None:
        self.assertEqual(set(main_module.PREFERRED_COGS), EXPECTED_COGS)

    def test_discover_returns_expected_modules(self) -> None:
        discovered = set(main_module.discover_cogs())
        self.assertTrue(
            EXPECTED_COGS.issubset(discovered),
            f"Thiếu: {EXPECTED_COGS - discovered}",
        )

    def test_discover_ignores_infrastructure_files(self) -> None:
        discovered = set(main_module.discover_cogs())
        for blocked in ("main", "keep_alive", "core", "config", "utils"):
            self.assertNotIn(blocked, discovered)

    def test_has_setup_function_detection(self) -> None:
        self.assertTrue(main_module._has_setup_function(self.root / "cogs" / "admin.py"))
        self.assertTrue(main_module._has_setup_function(self.root / "cogs" / "system.py"))
        self.assertFalse(main_module._has_setup_function(self.root / "keep_alive.py"))

    def test_keep_alive_module_exposes_start_health_server(self) -> None:
        import keep_alive

        self.assertTrue(callable(getattr(keep_alive, "start_health_server", None)))
        self.assertTrue(callable(getattr(keep_alive, "set_ready_probe", None)))


class CommandErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_prefix_command_returns_help_hint(self) -> None:
        if main_module is None:
            self.skipTest(f"Không import được main.py: {IMPORT_ERROR}")

        class FakeContext:
            clean_prefix = "!"

            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send(self, message: str) -> None:
                self.sent.append(message)

        ctx = FakeContext()
        await main_module.KaiAI.on_command_error(
            None, ctx, main_module.commands.CommandNotFound("!khongtontai")
        )

        self.assertEqual(
            ctx.sent,
            ["❓ Mình chưa nhận ra lệnh này. Dùng `!help` để xem danh sách lệnh."],
        )


if __name__ == "__main__":
    unittest.main()

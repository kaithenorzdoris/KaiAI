"""Test cho ``kai_core/ai/tools.py`` -- lop ket noi AI voi he thong.

Bao phu:

1. Doc dung so lieu cua mot nguoi (AI tra loi 'toi co bao nhieu tien').
2. Doc thong ke server, bang xep hang, gia san, lai suat.
3. Giao dich **luon can xac nhan** truoc khi chay -- khong co confirm thi khong
   tien nao doi, day la lop bao ve quan trong nhat.
4. Loi trong cog khong duoc lam AI chat vo.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

try:
    import aiosqlite
except ImportError:  # pragma: no cover - thieu dependency
    aiosqlite = None

from cogs.games import Games
from kai_core.ai.tools import AITools, ToolResult, fmt_money


class FmtMoneyTests(unittest.TestCase):
    """Định dạng tiền kiểu Việt Nam."""

    def test_thousand_separator_is_dot(self) -> None:
        self.assertEqual(fmt_money(1_250_000), "1.250.000")

    def test_zero_and_negative(self) -> None:
        self.assertEqual(fmt_money(0), "0")
        self.assertEqual(fmt_money(-5000), "-5.000")

    def test_bad_input_does_not_raise(self) -> None:
        self.assertEqual(fmt_money(None), "0")
        self.assertEqual(fmt_money("abc"), "0")


class ToolResultTests(unittest.TestCase):
    """Kiểm tra cấu trúc kết quả trả về cho AI."""

    def test_str_returns_message(self) -> None:
        self.assertEqual(str(ToolResult(ok=True, message="xin chào")), "xin chào")

    def test_defaults_are_safe(self) -> None:
        result = ToolResult(ok=False)
        self.assertFalse(result.need_confirm)
        self.assertEqual(result.data, {})


@unittest.skipUnless(aiosqlite, "aiosqlite chưa được cài đặt")
class AIToolsDbTests(unittest.IsolatedAsyncioTestCase):
    """Tích hợp trên SQLite tạm."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cog = Games(bot=None)
        self.cog.db_path = str(pathlib.Path(self._tmp.name) / "economy.db")
        for command in self.cog.__cog_commands__:
            command.cog = self.cog
        await self.cog.setup_db()
        self.tools = AITools(self.cog)
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("INSERT INTO users(user_id,balance,bank) VALUES(1,5000000,1000000)")
            await db.execute("INSERT INTO users(user_id,balance,bank) VALUES(2,0,0)")
            await db.commit()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _balance(self, uid: int = 1) -> int:
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute("SELECT balance FROM users WHERE user_id=?", (uid,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    # ---------------- đọc dữ liệu ----------------

    async def test_look_up_user_returns_real_numbers(self) -> None:
        result = await self.tools.look_up_user(1)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["summary"]["balance"], 5_000_000)
        self.assertEqual(result.data["summary"]["bank"], 1_000_000)
        self.assertEqual(result.data["summary"]["net_worth"], 6_000_000)
        self.assertIn("5.000.000", result.message)

    async def test_look_up_other_user(self) -> None:
        """AI phải đọc được tài sản của người khác, không chỉ của chính mình."""
        result = await self.tools.look_up_user(2)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["summary"]["balance"], 0,
                         "Tai khoan da tao san trong setup co balance 0")

    async def test_server_stats(self) -> None:
        result = await self.tools.server_stats()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["players"], 2)

    async def test_bot_identity_reports_real_guilds(self) -> None:
        """AI phai biet MINH dang o bao nhieu server (khong duoc bia)."""
        members = [("A", 10), ("B", 25), ("C", 7)]

        class _G:
            def __init__(self, name, member_count):
                self.name = name
                self.member_count = member_count

        class _Cmd:
            def __init__(self, name):
                self.name = name

        class _Bot:
            guilds = [_G(n, c) for n, c in members]
            latency = 0.05
            commands = [_Cmd("pay"), _Cmd("dep")]

        self.cog.bot = _Bot()
        result = await self.tools.bot_identity()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["guild_count"], 3)
        self.assertEqual(result.data["total_members"], 42)
        self.assertIn("3", result.message)
        self.assertNotIn("1 server", result.message.replace("3", "X"))

    async def test_leaderboard_orders_by_total(self) -> None:
        result = await self.tools.leaderboard(5)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["rows"][0]["user_id"], 1)

    async def test_market_prices_not_empty(self) -> None:
        result = await self.tools.market_prices()
        self.assertTrue(result.ok)
        self.assertTrue(result.data["rows"])
        self.assertIn("KAI", result.message)

    async def test_interest_rates_mentions_three_rates(self) -> None:
        result = await self.tools.interest_rates()
        self.assertTrue(result.ok)
        self.assertIn("%/ngay", result.message)

    # ---------------- xác nhận trước giao dịch ----------------

    async def test_transfer_requires_confirmation(self) -> None:
        result = await self.tools.do_transfer(1, 2, 100_000)
        self.assertFalse(result.ok)
        self.assertTrue(result.need_confirm)
        self.assertEqual(await self._balance(1), 5_000_000, "Chưa xác nhận thì không trừ tiền")
        self.assertEqual(await self._balance(2), 0)

    async def test_transfer_after_confirmation_moves_money(self) -> None:
        result = await self.tools.do_transfer(1, 2, 100_000, confirm=True)
        self.assertTrue(result.ok)
        self.assertEqual(await self._balance(1), 4_900_000)
        self.assertEqual(await self._balance(2), 100_000)

    async def test_parallel_confirmed_transfers_cannot_overdraw(self) -> None:
        """5 transfer song song 2M tu vi 5M -> toi da 2 lenh thanh cong."""
        import asyncio

        results = await asyncio.gather(*[
            self.tools.do_transfer(1, 2, 2_000_000, confirm=True)
            for _ in range(5)
        ])
        ok_count = sum(1 for r in results if r.ok)
        self.assertLessEqual(ok_count, 2, "Khong the in tien bang transfer song song")
        self.assertGreaterEqual(await self._balance(1), 0, "Vi khong duoc am")

    async def test_deposit_and_withdraw_require_confirmation(self) -> None:
        dep = await self.tools.do_deposit(1, 200_000)
        self.assertTrue(dep.need_confirm)
        self.assertEqual(await self._balance(1), 5_000_000)

        wd = await self.tools.do_withdraw(1, 100_000)
        self.assertTrue(wd.need_confirm)

    async def test_deposit_then_withdraw_after_confirm(self) -> None:
        await self.tools.do_deposit(1, 1_000_000, confirm=True)
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute("SELECT bank FROM users WHERE user_id=1") as cur:
                self.assertEqual((await cur.fetchone())[0], 2_000_000)

        await self.tools.do_withdraw(1, 1_000_000, confirm=True)
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute("SELECT bank FROM users WHERE user_id=1") as cur:
                self.assertEqual((await cur.fetchone())[0], 1_000_000)

    # ---------------- chặn sai ----------------

    async def test_transfer_to_self_rejected(self) -> None:
        result = await self.tools.do_transfer(1, 1, 1000, confirm=True)
        self.assertFalse(result.ok)
        self.assertEqual(await self._balance(1), 5_000_000)

    async def test_non_positive_amount_rejected(self) -> None:
        self.assertFalse((await self.tools.do_transfer(1, 2, 0, confirm=True)).ok)
        self.assertFalse((await self.tools.do_transfer(1, 2, -100, confirm=True)).ok)

    async def test_transfer_without_enough_money_fails_safely(self) -> None:
        result = await self.tools.do_transfer(1, 2, 999_999_999, confirm=True)
        self.assertFalse(result.ok)
        self.assertEqual(await self._balance(1), 5_000_000, "Thất bại thì không mất tiền")

    async def test_bad_input_does_not_raise(self) -> None:
        self.assertFalse((await self.tools.do_transfer("a", "b", "c", confirm=True)).ok)

    # ---------------- lỗi cog không làm hỏng chat ----------------

    async def test_broken_cog_returns_error_not_exception(self) -> None:
        class Broken:
            db_path = "missing.db"

        broken = AITools(Broken())
        for coro in (
            broken.look_up_user(1),
            broken.server_stats(),
            broken.leaderboard(),
            broken.market_prices(),
            broken.interest_rates(),
        ):
            result = await coro
            self.assertFalse(result.ok)

    async def test_not_ready_when_cog_missing(self) -> None:
        empty = AITools(None)
        self.assertFalse(empty.is_ready())
        self.assertFalse((await empty.look_up_user(1)).ok)


if __name__ == "__main__":
    unittest.main()
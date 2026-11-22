"""Test BAO MAT cho vat pham + ``!reset``.

Muc tieu: chung minh bang test rang khong ai co the
1. xac nhan reset thay nguoi khac,
2. ban vuot so luong vat pham minh co (ke ca khi chay song song),
3. reset khong chạy khi ma xac nhan sai / het han / dung hai lan.
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
import unittest

try:
    import aiosqlite
except ImportError:  # pragma: no cover - thieu dependency
    aiosqlite = None

from cogs.games import ITEMS, STARTING_BALANCE, Games, item_price


class _FakeMember:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.display_name = f"user{uid}"
        self.mention = f"<@{uid}>"


class _FakeCtx:
    def __init__(self, uid: int) -> None:
        self.author = _FakeMember(uid)
        self.sent: list = []

    async def send(self, content=None, embed=None, **kwargs):
        self.sent.append(content if content is not None else embed)

    @property
    def last(self) -> str:
        return self.sent[-1]


def _extract_token(message: str) -> str:
    """Lay ma xac nhan 6 ky tu hex trong tin nhan canh bao."""
    import re

    for m in re.finditer(r"`([0-9a-fA-F]{6})`", message):
        return m.group(1)
    return ""


@unittest.skipUnless(aiosqlite, "aiosqlite chua duoc cai dat")
class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cog = Games(bot=None)
        self.cog.db_path = str(pathlib.Path(self._tmp.name) / "economy.db")
        for command in self.cog.__cog_commands__:
            command.cog = self.cog
        # Tat cooldown: lenh co cooldown 60s se chan cac lan goi
        # trong cung mot test, khac vooi hanh vi that cua bot.
        for command in self.cog.__cog_commands__:
            # ``cooldown`` la property khong co setter -> dua vao __dict__.
            if command.__dict__.get("_cooldown") is not None:
                command.__dict__["_cooldown"] = None
        await self.cog.setup_db()
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("INSERT INTO users(user_id,balance,bank) VALUES(1,50000000,0)")
            await db.execute("INSERT INTO users(user_id,balance,bank) VALUES(2,50000000,0)")
            await db.commit()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _balance(self, uid: int = 1) -> int:
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute("SELECT balance FROM users WHERE user_id=?", (uid,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def _inv(self, uid: int = 1) -> dict:
        return await self.cog._get_inventory(uid)

    async def _grant(self, uid: int, key: str, amount: int) -> None:
        async with aiosqlite.connect(self.cog.db_path) as db:
            await self.cog._add_item(db, uid, key, amount)
            await db.commit()

    # ==================================================================
    # 1) Reset: khac nguoi khong xac nhan thay duoc
    # ==================================================================

    async def test_other_cannot_reset_my_account(self) -> None:
        """Nguoi 2 go '!reset' + ma cua nguoi 1 -> nguoi 2 KHONG bi reset,
        va nguoi 1 giu nguyen tai san. Day la loi bao dat chinh."""
        # Nguoi 1 xin token
        ctx1 = _FakeCtx(1)
        await self.cog.reset_life(ctx1, "")
        token1 = _extract_token(ctx1.last)
        self.assertTrue(token1, "Phai sinh ma xac nhan")

        # Nguoi 2 lay ma cua nguoi 1 roi dung
        ctx2 = _FakeCtx(2)
        await self.cog.reset_life(ctx2, token1)
        self.assertIn("Ma xac nhan khong dung", ctx2.last)
        self.assertEqual(await self._balance(2), 50_000_000, "Nguoi 2 khong bi reset")

        # Nguoi 1 van binh thuong
        self.assertEqual(await self._balance(1), 50_000_000)

    async def test_wrong_token_rejected(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.reset_life(ctx, "")
        await self.cog.reset_life(ctx, "zzzzzz")
        self.assertIn("Ma xac nhan khong dung", ctx.last)
        self.assertEqual(await self._balance(1), 50_000_000)

    async def test_token_is_single_use(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.reset_life(ctx, "")
        token = _extract_token(ctx.last)
        await self.cog.reset_life(ctx, token)          # lan 1 - thanh cong
        self.assertEqual(await self._balance(1), STARTING_BALANCE)
        # Lay token moi
        ctx2 = _FakeCtx(1)
        await self.cog.reset_life(ctx2, "")
        token2 = _extract_token(ctx2.last)
        await self.cog.reset_life(ctx2, token2)
        token3 = None
        ctx3 = _FakeCtx(1)
        await self.cog.reset_life(ctx3, "")
        token3 = _extract_token(ctx3.last)
        # Dung lai token cu (da ton tai) -> phai bi tu choi
        await self.cog.reset_life(ctx3, token2)
        self.assertIn("Ma xac nhan khong dung", ctx3.last)

    async def test_expired_token_rejected(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.reset_life(ctx, "")
        token = _extract_token(ctx.last)
        # Tua han: -1 giay
        for uid in list(getattr(self.cog, "_reset_tokens", {})):
            tok, exp = self.cog._reset_tokens[uid]
            self.cog._reset_tokens[uid] = (tok, exp - 1000)
        await self.cog.reset_life(ctx, token)
        self.assertIn("Ma xac nhan khong dung", ctx.last)
        self.assertEqual(await self._balance(1), 50_000_000)

    # ==================================================================
    # 2) Ban vuot so luong (loi TOCTOU)
    # ==================================================================

    async def test_cannot_sell_more_than_owned(self) -> None:
        await self._grant(1, "nuoctra", 1)
        before = await self._balance(1)
        ctx = _FakeCtx(1)
        await self.cog.sell_item(ctx, "nuoctra", "5")
        self.assertIn("chi co", ctx.last)
        self.assertEqual(await self._balance(1), before, "Khong duoc nhan tien")
        self.assertEqual(await self._inv(), {"nuoctra": 1})

    async def test_concurrent_sell_never_goes_negative(self) -> None:
        """Hai lenh ban chay song song chi duoc ban dung so hang co that."""
        await self._grant(1, "nuoctra", 1)
        before = await self._balance(1)

        # nuoctra gia 0 nen dung item co gia de thay tien
        await self._grant(1, "dienthoai", 1)
        price = item_price("dienthoai")

        ctx_a, ctx_b = _FakeCtx(1), _FakeCtx(1)
        await asyncio.gather(
            self.cog.sell_item(ctx_a, "dienthoai", "1"),
            self.cog.sell_item(ctx_b, "dienthoai", "1"),
        )

        remaining = await self._inv()
        # Ton kho khong bao gio am
        for key, amount in remaining.items():
            self.assertGreaterEqual(amount, 0, f"Ton kho am o {key}!")
        # Chi nhận tien cho 1 cai
        gained = (await self._balance(1)) - before
        self.assertLessEqual(gained, int(price * 0.7), "Khong duoc ban trung gia")
        self.assertNotIn("dienthoai", {k for k, v in remaining.items() if v > 0})

    async def test_inventory_never_negative_after_failed_sell(self) -> None:
        await self._grant(1, "oto", 1)
        ctx = _FakeCtx(1)
        await self.cog.sell_item(ctx, "oto", "3")
        self.assertEqual((await self._inv()).get("oto", 0), 1)

    async def test_concurrent_buy_with_insufficient_funds(self) -> None:
        """Nhieu lenh mua cung luc khong duoc tien se khong lam am so du."""
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("UPDATE users SET balance=1000000 WHERE user_id=1")
            await db.commit()
        ctx_a, ctx_b = _FakeCtx(1), _FakeCtx(1)
        # gia nha 75M >> so du 1M
        await asyncio.gather(
            self.cog.buy_item(ctx_a, "nha", "1"),
            self.cog.buy_item(ctx_b, "nha", "1"),
        )
        bal = await self._balance(1)
        self.assertGreaterEqual(bal, 0, "So du khong duoc am")
        self.assertEqual(await self._inv(), {}, "Khong mua duoc gi")

    # ==================================================================
    # 3) Danh tinh gia tri
    # ==================================================================

    async def test_item_value_counts_in_net_worth(self) -> None:
        await self._grant(1, "maytinh", 2)
        summary = await self.cog.get_financial_summary(1)
        self.assertEqual(summary["item_value"], item_price("maytinh") * 2)
        self.assertEqual(summary["item_count"], 2)
        # Net worth phai tang dung bang gia tri vat pham
        self.assertEqual(
            summary["net_worth"],
            50_000_000 + item_price("maytinh") * 2,
        )

    async def test_reset_clears_item_value(self) -> None:
        await self._grant(1, "oto", 1)
        ctx = _FakeCtx(1)
        await self.cog.reset_life(ctx, "")
        token = _extract_token(ctx.last)
        await self.cog.reset_life(ctx, token)
        summary = await self.cog.get_financial_summary(1)
        self.assertEqual(summary["item_value"], 0)
        self.assertEqual(summary["net_worth"], STARTING_BALANCE)

    def test_catalog_sane(self) -> None:
        for key in ("oto", "dienthoai", "maytinh", "nha"):
            self.assertIn(key, ITEMS)


if __name__ == "__main__":
    unittest.main()
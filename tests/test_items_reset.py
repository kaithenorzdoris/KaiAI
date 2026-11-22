"""Test cho vat pham moi + lenh ``!reset`` trong ``cogs/games.py``.

Bao phu cac loi kinh dien:

1. Mua vat pham tru tien, ghi tang inventory.
2. Mua qua so du -> bi tu choi, khong doi so du.
3. Ban tra lai 70% gia.
4. Ban nhieu hon so dang co -> bi tu choi.
5. ``!reset`` **khong chay** neu chua go chu xac nhan.
6. ``!reset xacnhan`` xoa het no/co phi/vat pham/XP va cap lai tien khoi dau.
7. Reset khong duoc xoa du lieu cua nguoi khac.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

try:
    import aiosqlite
except ImportError:  # pragma: no cover - thieu dependency
    aiosqlite = None

from cogs.games import (
    ITEMS,
    STARTING_BALANCE,
    Games,
    inventory_value,
    item_name,
    item_price,
)


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


class CatalogTests(unittest.TestCase):
    """Catalog vật phẩm phải hợp lệ và nhất quán."""

    def test_catalog_has_required_items(self) -> None:
        for key in ("oto", "dienthoai", "maytinh", "nha"):
            self.assertIn(key, ITEMS, f"Thiếu vật phẩm {key}")

    def test_every_item_has_five_fields(self) -> None:
        for key, entry in ITEMS.items():
            self.assertEqual(len(entry), 5, f"{key} phai co 5 truong")
            name, price, _emoji, desc, _nhom = entry
            self.assertTrue(name)
            self.assertIsInstance(price, int)
            self.assertGreaterEqual(price, 0)
            self.assertTrue(desc)

    def test_prices_are_unique_per_key(self) -> None:
        # Tranh trung ten de nguoi dung khop am
        names = [entry[0] for entry in ITEMS.values()]
        self.assertEqual(len(names), len(set(names)), "Ten vật phẩm phải là duy nhất")

    def test_helpers_are_safe_for_unknown_key(self) -> None:
        self.assertEqual(item_name("khong_co"), "khong_co")
        self.assertEqual(item_price("khong_co"), 0)

    def test_inventory_value_ignores_zero_and_unknown(self) -> None:
        self.assertEqual(inventory_value({"khong_co": 5}), 0)
        self.assertEqual(inventory_value({"oto": 0}), 0)
        self.assertEqual(inventory_value({"nuoctra": 3}), 0)


def _token(msg: str) -> str:
    """Lay ma xac nhan 6 ky tu hex trong tin nhan canh bao."""
    import re

    for m in re.finditer(r"`([0-9a-fA-F]{6})`", msg):
        return m.group(1)
    return ""


@unittest.skipUnless(aiosqlite, "aiosqlite chưa được cài đặt")
class ItemCommandTests(unittest.IsolatedAsyncioTestCase):
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
            await db.execute("INSERT INTO users(user_id,balance,bank) VALUES(1,200000000,0)")
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

    # ---------------- catalog lệnh ----------------

    def test_new_commands_registered(self) -> None:
        names = {c.name for c in self.cog.__cog_commands__}
        for expected in ("shop", "muado", "bando", "tudovanpham", "reset"):
            self.assertIn(expected, names, f"Thiếu lệnh {expected}")

    # ---------------- mua/bán ----------------

    async def test_buy_deducts_money_and_adds_item(self) -> None:
        price = item_price("dienthoai")
        ctx = _FakeCtx(1)
        await self.cog.buy_item(ctx, "dienthoai", "1")
        self.assertEqual(await self._balance(1), 200_000_000 - price)
        self.assertEqual(await self._inv(), {"dienthoai": 1})

    async def test_buy_multiple(self) -> None:
        price = item_price("dienthoai")
        ctx = _FakeCtx(1)
        await self.cog.buy_item(ctx, "dienthoai", "3")
        self.assertEqual(await self._balance(1), 200_000_000 - price * 3)
        self.assertEqual(await self._inv(), {"dienthoai": 3})

    async def test_buy_expensive_item_without_money_rejected(self) -> None:
        await aiosqlite.connect(self.cog.db_path).close()
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("UPDATE users SET balance=1000 WHERE user_id=1")
            await db.commit()
        ctx = _FakeCtx(1)
        await self.cog.buy_item(ctx, "oto", "1")
        self.assertEqual(await self._balance(1), 1000, "Không đủ tiền thì không trừ")
        self.assertEqual(await self._inv(), {})

    async def test_buy_unknown_item_rejected(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.buy_item(ctx, "xe_lua", "1")
        self.assertIn("❌", ctx.last)
        self.assertEqual(await self._balance(1), 200_000_000)

    async def test_sell_refunds_seventy_percent(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.buy_item(ctx, "dienthoai", "2")
        before = await self._balance(1)
        await self.cog.sell_item(ctx, "dienthoai", "1")
        expected = int(item_price("dienthoai") * 0.7)
        self.assertEqual(await self._balance(1), before + expected)
        self.assertEqual(await self._inv(), {"dienthoai": 1})

    async def test_sell_more_than_owned_rejected(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.buy_item(ctx, "nuoctra", "2")
        before = await self._balance(1)
        await self.cog.sell_item(ctx, "nuoctra", "5")
        self.assertEqual(await self._balance(1), before, "Không đủ hàng để bán")
        self.assertEqual(await self._inv(), {"nuoctra": 2})

    async def test_sell_without_owning_rejected(self) -> None:
        before = await self._balance(1)
        ctx = _FakeCtx(1)
        await self.cog.sell_item(ctx, "nha", "1")
        self.assertEqual(await self._balance(1), before)

    async def _issue(self, ctx) -> _FakeCtx:
        """Goi !reset de nhan ma xac nhan roi tra ve ctx."""
        await self.cog.reset_life(ctx, "")
        return ctx

    # ---------------- reset ----------------

    async def test_reset_without_confirm_does_nothing(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.reset_life(ctx, "")
        self.assertEqual(await self._balance(1), 200_000_000, "Chưa xác nhận thì không reset")
        self.assertTrue(_token(ctx.last), "Phai cap ma xac nhan de xac nhan")

    async def test_reset_wrong_token_does_nothing(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.reset_life(ctx, "sala")
        self.assertEqual(await self._balance(1), 200_000_000)

    async def test_reset_confirm_restores_starting_state(self) -> None:
        # Gom nhiều loại dữ liệu trước khi reset
        ctx = _FakeCtx(1)
        await self.cog.buy_item(ctx, "oto", "1")
        await self.cog.deposit(ctx, "1000000")
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("INSERT INTO loans(user_id,amount) VALUES(1,500000)")
            await db.execute("UPDATE users SET xp=999,level=9,streak=5 WHERE user_id=1")
            await db.commit()

        await self.cog.reset_life(ctx, _token((await self._issue(ctx)).last))

        balance, bank, xp, level, streak = await self._scalar_row(1)
        self.assertEqual(balance, STARTING_BALANCE)
        self.assertEqual(bank, 0)
        self.assertEqual(xp, 0)
        self.assertEqual(level, 1)
        self.assertEqual(streak, 0)
        self.assertEqual(await self._inv(), {}, "Phải xóa sạch vật phẩm")

    async def test_reset_clears_debt_and_shares(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.buy_item(ctx, "maytinh", "1")
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("INSERT INTO loans(user_id,amount) VALUES(1,777777)")
            await db.execute(
                "INSERT INTO companies(name,owner_id,total_shares,price_per_share,cash,created_at)"
                " VALUES('xco',1,1000,1000,0,0)"
            )
            await db.execute("INSERT INTO shares(user_id,company_id,amount) VALUES(1,1,500)")
            await db.commit()

        await self.cog.reset_life(ctx, _token((await self._issue(ctx)).last))

        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM loans WHERE user_id=1") as cur:
                self.assertEqual((await cur.fetchone())[0], 0)
            async with db.execute("SELECT COUNT(*) FROM shares WHERE user_id=1") as cur:
                self.assertEqual((await cur.fetchone())[0], 0)

    async def test_reset_does_not_touch_other_users(self) -> None:
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("INSERT INTO users(user_id,balance,bank) VALUES(2,777777,555555)")
            await db.commit()

        ctx = _FakeCtx(1)
        await self.cog.reset_life(ctx, _token((await self._issue(ctx)).last))

        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute("SELECT balance,bank FROM users WHERE user_id=2") as cur:
                row = await cur.fetchone()
        self.assertEqual((row[0], row[1]), (777777, 555555), "Reset không được chạm người khác")

    async def test_reset_writes_transaction_log(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.reset_life(ctx, _token((await self._issue(ctx)).last))
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id=1 AND kind='reset'"
            ) as cur:
                self.assertEqual((await cur.fetchone())[0], 1, "Reset phải được ghi log")

    async def _scalar_row(self, uid: int = 1):
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute(
                "SELECT balance,bank,xp,level,streak FROM users WHERE user_id=?", (uid,)
            ) as cur:
                return await cur.fetchone()


if __name__ == "__main__":
    unittest.main()
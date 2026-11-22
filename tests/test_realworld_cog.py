# -*- coding: utf-8 -*-
"""
Test TÍCH HỢP cho ``cogs/realworld.py`` — các lệnh Discord của giai đoạn 1.

``tests/test_realworld.py`` chỉ kiểm tra toán thuần của ``prices``/``savings``.
File này chạy **các lệnh thật** trên SQLite tạm để chứng minh:

1. Schema ``pantry``/``savings`` được tạo idempotent, không mất bảng cũ.
2. Mua/bán/ăn giữ **tiền cân bằng** — tổng tiền không tự sinh ra.
3. Không ai rút được sổ của người khác (ownership).
4. Lãi cộng đúng 1 lần/ngày, gọi lại nhiều lần không sinh tiền.
5. Rút trước hạn bị trừ phạt; rút hết thì xoá sổ.
"""

from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

try:
    import aiosqlite
except ImportError:  # pragma: no cover
    aiosqlite = None

from cogs.realworld import (
    FRESH_DAYS,
    MAX_PANTRY_ITEMS,
    MEAL_CAP,
    RESELL_RATIO,
    RealWorld,
    fmt_vnd,
    normalize_key,
)
from kai_core import prices, savings

DAY = 86_400


class _Member:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.display_name = f"u{uid}"
        self.mention = f"<@{uid}>"


class _Ctx:
    """Context giả: chỉ cần ``author`` và ``send``."""

    def __init__(self, uid: int) -> None:
        self.author = _Member(uid)
        self.sent: list = []

    async def send(self, content=None, embed=None, **kwargs):
        self.sent.append(content if content is not None else embed)

    @property
    def last(self):
        return self.sent[-1]


class _Bot:
    def __init__(self) -> None:
        self.waited = False

    async def wait_until_ready(self) -> None:
        self.waited = True


class HelperTests(unittest.TestCase):
    """Hàm thuần — không cần DB."""

    def test_fmt_vnd_uses_dots(self) -> None:
        self.assertEqual(fmt_vnd(1_250_000), "1.250.000")
        self.assertEqual(fmt_vnd("x"), "0")

    def test_normalize_key_known(self) -> None:
        self.assertEqual(normalize_key("Gạo"), "gao")
        self.assertEqual(normalize_key("  TRUNG  "), "trung")
        self.assertEqual(normalize_key("Kỳ hạn 1 năm"), "1nam")

    def test_normalize_key_unknown_returns_slug(self) -> None:
        self.assertEqual(normalize_key("Xa May"), "xa_may")

    def test_resell_never_profits(self) -> None:
        """Mua đi bán lại phải lỗ — nếu lãi thì tạo tiền từ hư không."""
        self.assertLess(RESELL_RATIO, 1.0)
        for key in prices.GOODS:
            self.assertLess(int(prices.quote(key) * RESELL_RATIO), prices.quote(key), key)


@unittest.skipUnless(aiosqlite, "aiosqlite chưa được cài đặt")
class RealWorldDBTests(unittest.IsolatedAsyncioTestCase):
    """Chạy lệnh thật trên SQLite tạm."""

    START_BALANCE = 100_000_000

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(pathlib.Path(self._tmp.name) / "economy.db")
        async with aiosqlite.connect(self.db) as conn:
            await conn.executescript(
                """
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    bank INTEGER NOT NULL DEFAULT 0,
                    xp INTEGER, level INTEGER, streak INTEGER, reputation INTEGER
                );
                CREATE TABLE transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, amount INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL, created_at REAL NOT NULL,
                    note TEXT DEFAULT ''
                );
                """
            )
            await conn.execute(
                "INSERT INTO users(user_id,balance) VALUES(1,?)", (self.START_BALANCE,)
            )
            await conn.execute(
                "INSERT INTO users(user_id,balance) VALUES(2,?)", (self.START_BALANCE,)
            )
            await conn.commit()
        self.cog = RealWorld(_Bot(), db_path=self.db)
        # Gắn cog + bỏ cooldown để gọi lệnh trực tiếp được (như test_company.py).
        # Không gọi ``cog_load`` vì hàm đó bật task nền cộng lãi -> rò event loop.
        for cmd in self.cog.__cog_commands__:
            cmd.cog = self.cog
            if cmd.__dict__.get("_cooldown") is not None:
                cmd.__dict__["_cooldown"] = None
        from cogs.realworld import SCHEMA

        async with aiosqlite.connect(self.db) as conn:
            await conn.executescript(SCHEMA)
            await conn.commit()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    # -- tiện ích -----------------------------------------------------

    async def _bal(self, uid: int) -> int:
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT balance FROM users WHERE user_id=?", (int(uid),)
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _pantry(self, uid: int) -> dict:
        """Chỉ lấy dòng còn hàng (``qty>0``) — giống truy vấn của ``!tudon``.

        Khi bán/ăn hết, code ghi ``qty=0`` thay vì xoá dòng (để giữ lịch sử
        mua), nên test phải lọc giống hệt thì mới so sánh được.
        """
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT item,qty FROM pantry WHERE user_id=? AND qty>0", (int(uid),)
            ) as cur:
                return {r[0]: int(r[1]) for r in await cur.fetchall()}

    async def _savings(self, uid: int) -> list:
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT id,kind,balance FROM savings WHERE user_id=? ORDER BY id",
                (int(uid),),
            ) as cur:
                return [(int(r[0]), r[1], int(r[2])) for r in await cur.fetchall()]

    async def _rep(self, uid: int) -> int:
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT COALESCE(reputation,0) FROM users WHERE user_id=?", (int(uid),)
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    # -- schema -------------------------------------------------------

    async def test_schema_is_idempotent(self) -> None:
        """Chạy SCHEMA nhiều lần không lỗi và không mất dữ liệu cũ."""
        from cogs.realworld import SCHEMA

        for _ in range(3):
            async with aiosqlite.connect(self.db) as conn:
                await conn.executescript(SCHEMA)
                await conn.commit()
        self.assertEqual(await self._bal(1), self.START_BALANCE)
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                names = {r[0] for r in await cur.fetchall()}
        self.assertIn("pantry", names)
        self.assertIn("savings", names)

    # -- chợ ----------------------------------------------------------

    async def test_buy_puts_item_in_pantry_and_charges_money(self) -> None:
        before = await self._bal(1)
        unit = prices.quote("gao")
        await self.cog.buy_item(_Ctx(1), "gao", 3)
        self.assertEqual((await self._pantry(1)).get("gao"), 3)
        self.assertEqual(await self._bal(1), before - unit * 3)

    async def test_buy_fails_when_broke(self) -> None:
        """Không đủ tiền thì không được trừ và không tạo hàng."""
        async with aiosqlite.connect(self.db) as conn:
            await conn.execute("UPDATE users SET balance=1 WHERE user_id=1")
            await conn.commit()
        await self.cog.buy_item(_Ctx(1), "thit_bo", 5)
        self.assertEqual(await self._bal(1), 1, "Không được trừ quá số dư")
        self.assertEqual(await self._pantry(1), {}, "Không được tạo hàng khi thiếu tiền")

    async def test_buy_unknown_item_rejected(self) -> None:
        await self.cog.buy_item(_Ctx(1), "o_hoi_bay", 1)
        self.assertEqual(await self._bal(1), self.START_BALANCE)

    async def test_buy_rejects_bad_quantity(self) -> None:
        for bad in (0, -5, 10_000):
            await self.cog.buy_item(_Ctx(1), "gao", bad)
            self.assertEqual(await self._pantry(1), {}, f"qty={bad} phải bị từ chối")

    async def test_balance_never_goes_negative(self) -> None:
        """Bất biến tiền: số dư không bao giờ âm."""
        async with aiosqlite.connect(self.db) as conn:
            await conn.execute("UPDATE users SET balance=0 WHERE user_id=1")
            await conn.commit()
        await self.cog.buy_item(_Ctx(1), "thit_bo", 1)
        self.assertGreaterEqual(await self._bal(1), 0)

    async def test_sell_returns_less_than_buy_price(self) -> None:
        unit = prices.quote("trung")
        await self.cog.buy_item(_Ctx(1), "trung", 2)
        after_buy = await self._bal(1)
        await self.cog.sell_item(_Ctx(1), "trung", 2)
        gained = (await self._bal(1)) - after_buy
        self.assertEqual(gained, int(unit * RESELL_RATIO) * 2)
        self.assertLess(gained, unit * 2, "Bán lại không được lãi")
        self.assertEqual(await self._pantry(1), {})

    async def test_sell_more_than_owned_fails(self) -> None:
        await self.cog.buy_item(_Ctx(1), "gao", 1)
        after = await self._bal(1)
        await self.cog.sell_item(_Ctx(1), "gao", 99)
        self.assertEqual(await self._bal(1), after, "Không được bán quá số đang có")
        self.assertEqual((await self._pantry(1)).get("gao"), 1)

    async def test_pantry_full_rejects_new_item(self) -> None:
        async with aiosqlite.connect(self.db) as conn:
            await conn.executemany(
                "INSERT INTO pantry(user_id,item,qty,bought_at,paid) VALUES(1,?,1,?,0)",
                [(f"do_dung_{i}", time.time()) for i in range(MAX_PANTRY_ITEMS)],
            )
            await conn.commit()
        before = await self._bal(1)
        await self.cog.buy_item(_Ctx(1), "thit_ga", 1)
        self.assertEqual(await self._bal(1), before)
        self.assertNotIn("thit_ga", await self._pantry(1))

    # -- ăn -----------------------------------------------------------

    async def test_eat_consumes_item_and_gives_reputation(self) -> None:
        await self.cog.buy_item(_Ctx(1), "gao", 2)
        await self.cog.eat(_Ctx(1), "gao", 1)
        self.assertEqual((await self._pantry(1)).get("gao"), 1)
        self.assertEqual(await self._rep(1), 2)

    async def test_eat_does_not_create_food(self) -> None:
        """Ăn khi tủ trống phải thất bại, không tạo ra đồ ăn."""
        before = await self._pantry(1)
        await self.cog.eat(_Ctx(1), "gao", 1)
        self.assertEqual(await self._pantry(1), before)
        self.assertEqual(await self._rep(1), 0)

    async def test_eat_respects_daily_cap(self) -> None:
        """Không thể ăn vô hạn để farm uy tín trong một ngày."""
        await self.cog.buy_item(_Ctx(1), "gao", 10)
        for _ in range(MEAL_CAP):
            await self.cog.eat(_Ctx(1), "gao", 1)
        self.assertEqual(await self._rep(1), MEAL_CAP * 2, "Phải bị chặn sau MEAL_CAP bữa")
        self.assertEqual((await self._pantry(1)).get("gao"), 10 - MEAL_CAP)

    async def test_eat_rejects_non_food(self) -> None:
        await self.cog.buy_item(_Ctx(1), "dien", 1)
        await self.cog.eat(_Ctx(1), "dien", 1)
        self.assertEqual((await self._pantry(1)).get("dien"), 1, "Hóa đơn điện không ăn được")

    async def test_pantry_expiry_flag_shown(self) -> None:
        """Hàng mua từ lâu phải bị đánh dấu hết hạn trong ``!tudon``."""
        await self.cog.buy_item(_Ctx(1), "gao", 1)
        async with aiosqlite.connect(self.db) as conn:
            await conn.execute(
                "UPDATE pantry SET bought_at=? WHERE user_id=1",
                (time.time() - (FRESH_DAYS + 1) * DAY,),
            )
            await conn.commit()
        ctx = _Ctx(1)
        await self.cog.pantry(ctx)
        self.assertIn("hạn", ctx.sent[-1].footer.text.lower())

    # -- tiết kiệm ----------------------------------------------------

    async def test_open_savings_charges_fee_and_deposit(self) -> None:
        before = await self._bal(1)
        money = 1_000_000
        await self.cog.open_savings(_Ctx(1), "linh_dong", money)
        rows = await self._savings(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], money)
        self.assertEqual(await self._bal(1), before - money - savings.OPEN_FEE)

    async def test_top_up_does_not_charge_fee_again(self) -> None:
        await self.cog.open_savings(_Ctx(1), "linh_dong", 1_000_000)
        before = await self._bal(1)
        await self.cog.open_savings(_Ctx(1), "linh_dong", 500_000)
        rows = await self._savings(1)
        self.assertEqual(len(rows), 1, "Phải dùng chung sổ đã có")
        self.assertEqual(rows[0][2], 1_500_000)
        self.assertEqual(await self._bal(1), before - 500_000, "Không thu phí lần hai")

    async def test_open_rejects_below_minimum(self) -> None:
        await self.cog.open_savings(_Ctx(1), "linh_dong", savings.MIN_OPEN - 1)
        self.assertEqual(await self._savings(1), [])
        self.assertEqual(await self._bal(1), self.START_BALANCE)

    async def test_open_rejects_unknown_kind(self) -> None:
        await self.cog.open_savings(_Ctx(1), "so_vang", 1_000_000)
        self.assertEqual(await self._savings(1), [])

    async def test_cannot_withdraw_other_people_savings(self) -> None:
        """Bảo mật quan trọng nhất: không ai rút được sổ của người khác."""
        await self.cog.open_savings(_Ctx(1), "linh_dong", 1_000_000)
        acc_id = (await self._savings(1))[0][0]
        before_2 = await self._bal(2)
        await self.cog.withdraw(_Ctx(2), acc_id, "all")
        self.assertEqual(await self._bal(2), before_2, "Ví người 2 không được đổi")
        rows = await self._savings(1)
        self.assertEqual(len(rows), 1, "Sổ người 1 phải còn nguyên")
        self.assertEqual(rows[0][2], 1_000_000)

    async def test_withdraw_all_from_flexible_account(self) -> None:
        await self.cog.open_savings(_Ctx(1), "linh_dong", 1_000_000)
        acc_id = (await self._savings(1))[0][0]
        before = await self._bal(1)
        await self.cog.withdraw(_Ctx(1), acc_id, "all")
        # Sổ linh động không mất lãi -> nhận đúng số tiền.
        self.assertEqual(await self._bal(1), before + 1_000_000)
        self.assertEqual(await self._savings(1), [], "Rút hết thì xoá sổ")

    async def test_early_withdraw_loses_interest(self) -> None:
        """Rút sớm sổ kỳ hạn phải bị trừ phạt — không nhận đủ số tiền."""
        await self.cog.open_savings(_Ctx(1), "1nam", 10_000_000)
        acc_id = (await self._savings(1))[0][0]
        before = await self._bal(1)
        await self.cog.withdraw(_Ctx(1), acc_id, "all")
        gained = (await self._bal(1)) - before
        self.assertLess(gained, 10_000_000, "Phải mất một phần lãi")
        self.assertGreater(gained, 0)

    async def test_partial_withdraw_keeps_account(self) -> None:
        await self.cog.open_savings(_Ctx(1), "linh_dong", 1_000_000)
        acc_id = (await self._savings(1))[0][0]
        before = await self._bal(1)
        await self.cog.withdraw(_Ctx(1), acc_id, "200000")
        rows = await self._savings(1)
        self.assertEqual(len(rows), 1, "Rút một phần thì sổ phải còn")
        self.assertEqual(rows[0][2], 800_000)
        self.assertEqual(await self._bal(1), before + 200_000)

    async def test_withdraw_more_than_balance_clamps(self) -> None:
        await self.cog.open_savings(_Ctx(1), "linh_dong", 1_000_000)
        acc_id = (await self._savings(1))[0][0]
        before = await self._bal(1)
        await self.cog.withdraw(_Ctx(1), acc_id, "999999999")
        self.assertEqual(await self._bal(1), before + 1_000_000, "Không được rút quá số dư sổ")

    # -- lãi ----------------------------------------------------------

    async def test_interest_not_credited_before_one_day(self) -> None:
        await self.cog.open_savings(_Ctx(1), "linh_dong", 10_000_000)
        credited = await self.cog.accrue_interest(time.time() + 3600)
        self.assertEqual(credited, 0, "Chưa qua 1 ngày thì chưa được cộng lãi")
        self.assertEqual((await self._savings(1))[0][2], 10_000_000)

    async def test_interest_credited_after_one_day(self) -> None:
        await self.cog.open_savings(_Ctx(1), "linh_dong", 10_000_000)
        expected = savings.interest_for("linh_dong", 10_000_000, 1)
        credited = await self.cog.accrue_interest(time.time() + DAY + 10)
        self.assertEqual(credited, 1)
        self.assertEqual((await self._savings(1))[0][2], 10_000_000 + expected)

    async def test_interest_cannot_be_farmed_by_repeated_calls(self) -> None:
        """Gọi lại 10 lần trong cùng ngày chỉ được cộng 1 lần."""
        await self.cog.open_savings(_Ctx(1), "linh_dong", 10_000_000)
        base = time.time() + DAY + 10
        for _ in range(10):
            await self.cog.accrue_interest(base)
        expected = savings.interest_for("linh_dong", 10_000_000, 1)
        self.assertEqual((await self._savings(1))[0][2], 10_000_000 + expected)

    async def test_interest_for_many_days_in_one_pass(self) -> None:
        """Qua 3 ngày thì cộng đúng 3 ngày lãi, không phải 3 lần gọi."""
        await self.cog.open_savings(_Ctx(1), "linh_dong", 10_000_000)
        await self.cog.accrue_interest(time.time() + 3 * DAY + 5)
        expected = savings.interest_for("linh_dong", 10_000_000, 3)
        self.assertEqual((await self._savings(1))[0][2], 10_000_000 + expected)

    async def test_interest_records_in_transaction_log(self) -> None:
        await self.cog.open_savings(_Ctx(1), "linh_dong", 10_000_000)
        await self.cog.accrue_interest(time.time() + DAY + 10)
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE kind='savings:interest'"
            ) as cur:
                self.assertEqual(int((await cur.fetchone())[0]), 1)

    async def test_interest_never_doubles_balance(self) -> None:
        """Lãi phải nhỏ hơn số dư — chống lạm phát."""
        await self.cog.open_savings(_Ctx(1), "1nam", 10_000_000)
        await self.cog.accrue_interest(time.time() + 365 * DAY)
        balance = (await self._savings(1))[0][2]
        self.assertLess(balance, 20_000_000)
        self.assertGreater(balance, 10_000_000)

    # -- lệnh chỉ đọc --------------------------------------------------

    async def test_market_board_filters_unknown_group(self) -> None:
        ctx = _Ctx(1)
        await self.cog.market_board(ctx, "khong_ton_tai")
        self.assertIn("❌", str(ctx.last))

    async def test_catalogs_send_something(self) -> None:
        for name in ("market_board", "savings_catalog"):
            ctx = _Ctx(1)
            await getattr(self.cog, name)(ctx)
            self.assertTrue(ctx.sent, f"{name} phải gửi gì đó")

    async def test_my_savings_empty_message(self) -> None:
        ctx = _Ctx(2)
        await self.cog.my_savings(ctx)
        self.assertIn("!guitien", str(ctx.last))

    async def test_all_commands_registered(self) -> None:
        """Chống hồi quy: lệnh mới phải còn đủ tên."""
        names = {c.name for c in self.cog.__cog_commands__}
        for expected in ("giatham", "muadi", "tudon", "an", "banra",
                         "moso", "guitien", "rutso", "socuatoa"):
            self.assertIn(expected, names)

    def test_no_name_or_alias_collision_with_other_cogs(self) -> None:
        """Trùng tên/alias làm discord.py raise lúc ``add_cog`` -> bot chết.

        Đã từng xảy ra: alias ``rut`` của ``!rutso`` trùng với lệnh rút ngân
        hàng cũ trong ``cogs/games.py``. Test này chặn tái phạm.
        """
        from cogs.games import Games
        from cogs.company import Company
        from cogs.business import Business

        def names_of(cls) -> set:
            out = set()
            for attr in vars(cls).values():
                name = getattr(attr, "name", None)
                if name and hasattr(attr, "callback"):
                    out.add(name)
                    out.update(getattr(attr, "aliases", None) or [])
            return out

        mine = names_of(type(self.cog))
        self.assertIn("rutso", mine)
        for other, label in ((Games, "Games"), (Company, "Company"), (Business, "Business")):
            clash = mine & names_of(other)
            self.assertFalse(clash, f"Trùng lệnh với {label}: {sorted(clash)}")



if __name__ == "__main__":
    unittest.main()

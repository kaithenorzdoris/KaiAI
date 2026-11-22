"""Test TAN CONG cho he thong tien cua KaiAI.

Moi test mo ta mot cach "tanh cong" co that va chung minh bot chan duoc:

1. Cuoc khong tru tien (parallel) - KHONG duoc loi nhieu.
2. Vay song song vuot han muc - KHONG duoc vay tran.
3. Tra no khong du tien - KHONG duoc lam so du am.
4. Phat tien vuot so du - so du KHONG duoc am.
5. Roulette be la chon "den" - KHONG duoc an nham mau khac.
"""

from __future__ import annotations

import asyncio
import pathlib
import random
import re
import tempfile
import unittest

try:
    import aiosqlite
except ImportError:  # pragma: no cover - thieu dependency
    aiosqlite = None

from cogs.games import Games


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


GAMES_SOURCE = str(pathlib.Path(__file__).resolve().parents[1] / "cogs" / "games.py")

@unittest.skipUnless(aiosqlite, "aiosqlite chưa được cài đặt")
class MoneyAttackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cog = Games(bot=None)
        self.cog.db_path = str(pathlib.Path(self._tmp.name) / "economy.db")
        for command in self.cog.__cog_commands__:
            command.cog = self.cog
            # Tắt cooldown để test được gọi nhiều lần trong 1 test.
            if command.__dict__.get("_cooldown") is not None:
                command.__dict__["_cooldown"] = None
        await self.cog.setup_db()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _set(self, balance: int = 0, bank: int = 0, level: int = 1, uid: int = 1):
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute(
                "INSERT INTO users(user_id,balance,bank,level) VALUES(?,?,?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance,"
                " bank=excluded.bank, level=excluded.level",
                (uid, balance, bank, level),
            )
            await db.commit()

    async def _get(self, uid: int = 1) -> tuple:
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute("SELECT balance,bank FROM users WHERE user_id=?", (uid,)) as c:
                r = await c.fetchone()
        return (r[0], r[1]) if r else (None, None)

    async def _debt(self, uid: int = 1) -> int:
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute("SELECT COALESCE(amount,0) FROM loans WHERE user_id=?", (uid,)) as c:
                r = await c.fetchone()
        return int(r[0]) if r else 0

    # ==================================================================
    # 1. Cuoc khong tru tien
    # ==================================================================

    async def test_place_bet_is_atomic(self) -> None:
        await self._set(balance=100_000)
        ok = await self.cog.place_bet(1, 50_000, "test")
        self.assertTrue(ok)
        self.assertEqual((await self._get())[0], 50_000)

    async def test_place_bet_rejects_when_insufficient(self) -> None:
        await self._set(balance=10_000)
        ok = await self.cog.place_bet(1, 50_000, "test")
        self.assertFalse(ok, "Không đủ tiền thì phải từ chối")
        self.assertEqual((await self._get())[0], 10_000, "Tiền không bị đụng")

    async def test_parallel_bets_cannot_overdraw(self) -> None:
        """5 lệnh cược song song mỗi lệnh 30k, chỉ có 100k -> tối đa 3 lệnh thắng."""
        await self._set(balance=100_000)
        results = await asyncio.gather(
            *[self.cog.place_bet(1, 30_000, "atk") for _ in range(5)]
        )
        accepted = sum(1 for r in results if r)
        balance, _ = await self._get()
        self.assertLessEqual(accepted, 3, "Không được vượt số dư")
        self.assertEqual(balance, 100_000 - accepted * 30_000, "Tiền phải khớp")
        self.assertGreaterEqual(balance, 0, "Số dư không được âm")

    async def test_parallel_bets_exactly_atomic(self) -> None:
        """Hai lệnh cược 60k trên số dư 100k -> chỉ một lệnh được."""
        await self._set(balance=100_000)
        results = await asyncio.gather(
            self.cog.place_bet(1, 60_000, "a"),
            self.cog.place_bet(1, 60_000, "b"),
        )
        self.assertEqual(sum(1 for r in results if r), 1)
        self.assertEqual((await self._get())[0], 40_000)

    async def test_rps_game_does_not_pay_without_bet(self) -> None:
        """Chơi rps khi không đủ tiền -> không được trả thưởng."""
        await self._set(balance=50)
        ctx = _FakeCtx(1)
        await self.cog.rps(ctx, "100000", "keo")
        self.assertEqual((await self._get())[0], 50, "Tiền phải giữ nguyên")

    # ==================================================================
    # 2. Vay song song
    # ==================================================================

    async def test_loan_blocked_when_already_in_debt(self) -> None:
        await self._set(balance=0, level=3)  # han muc 1.5M
        ctx = _FakeCtx(1)
        await self.cog.loan(ctx, "100000")
        self.assertEqual(await self._debt(), 100_000)
        ctx2 = _FakeCtx(1)
        await self.cog.loan(ctx2, "100000")
        self.assertEqual(await self._debt(), 100_000, "Không được vay lần 2")
        self.assertIn("con no", ctx2.last.lower() + ctx2.last.lower())

    async def test_parallel_loans_cannot_multiply_debt(self) -> None:
        """5 lệnh vay song song, chỉ 1 lệnh được thành công."""
        await self._set(balance=0, level=5)  # han muc 2.5M
        ctxs = [_FakeCtx(1) for _ in range(5)]
        await asyncio.gather(*[self.cog.loan(c, "1000000") for c in ctxs])
        self.assertEqual(await self._debt(), 1_000_000, "Chỉ được vay 1 lần")
        self.assertEqual((await self._get())[0], 1_000_000)

    # ==================================================================
    # 3. Tra no khong du tien
    # ==================================================================

    async def test_repay_rejected_when_insufficient(self) -> None:
        await self._set(balance=1_000, level=3)
        ctx = _FakeCtx(1)
        await self.cog.loan(ctx, "500000")
        # Rút bớt tiền để không đủ trả nợ
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("UPDATE users SET balance=1000 WHERE user_id=1")
            await db.commit()
        ctx2 = _FakeCtx(1)
        await self.cog.repay(ctx2, "400000")
        self.assertEqual((await self._get())[0], 1_000, "Tiền không bị trừ")
        self.assertEqual(await self._debt(), 500_000, "Nợ không giảm")

    async def test_parallel_repay_cannot_overdraw(self) -> None:
        await self._set(balance=100_000, level=3)
        ctx = _FakeCtx(1)
        await self.cog.loan(ctx, "1_000_000")
        # balance = 1.1M, no = 1M. Trả 900k song song 3 lệnh -> chỉ 1 được.
        ctxs = [_FakeCtx(1) for _ in range(3)]
        await asyncio.gather(*[self.cog.repay(c, "900000") for c in ctxs])
        balance, _ = await self._get()
        self.assertGreaterEqual(balance, 0, "Số dư không được âm")

    # ==================================================================
    # 4. Phat tien vuot so du
    # ==================================================================

    async def _force_crime_caught(self) -> None:
        """Ep 'bi bat' va xoa cooldown de goi nhieu lan trong 1 test."""
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("DELETE FROM cooldowns WHERE key='crime'")
            await db.commit()

    async def test_crime_fine_never_negative(self) -> None:
        """Bi bat nhieu lan lien tiep -> so du KHONG duoc am."""
        await self._set(balance=5_000)
        orig = random.random
        random.random = lambda: 0.99  # > 0.62 -> bi bat
        try:
            for _ in range(3):
                await self._force_crime_caught()
                await self.cog.crime(_FakeCtx(1))
        finally:
            random.random = orig
        balance, _ = await self._get()
        self.assertGreaterEqual(balance, 0, "Tien phat khong duoc lam am so du")
        self.assertEqual(balance, 0, "Phat het so du con lai")

    async def test_crime_parallel_fine_stays_non_negative(self) -> None:
        """Chay song song: so du van khong duoc am."""
        await self._set(balance=30_000)
        orig = random.random
        random.random = lambda: 0.99
        try:
            await self._force_crime_caught()
            ctxs = [_FakeCtx(1) for _ in range(3)]
            await asyncio.gather(*[self.cog.crime(c) for c in ctxs])
        finally:
            random.random = orig
        balance, _ = await self._get()
        self.assertGreaterEqual(balance, 0)

    async def test_crime_fine_single_keeps_non_negative(self) -> None:
        await self._set(balance=3_000)
        orig = random.random
        random.random = lambda: 0.99
        try:
            await self._force_crime_caught()
            await self.cog.crime(_FakeCtx(1))
        finally:
            random.random = orig
        balance, _ = await self._get()
        self.assertGreaterEqual(balance, 0)
        self.assertEqual(balance, 0, "Phat het so du con lai")

    # ==================================================================
    # 5. Roulette be la
    # ==================================================================

    async def test_roulette_den_loses_on_red_number(self) -> None:
        """Chon 'den' khi ra mau DO -> THUA.

        Loi cu: neu kiem tra bang ``startswith`` thi "den".startswith("de") la
        True va co the lam nham. Sua loi = so khop chinh xac.
        """
        from unittest import mock

        from cogs import games as games_mod

        await self._set(balance=1_000_000)
        # 1 thuoc ve mau do (red_numbers) -> phai THUA khi chon den.
        with mock.patch.object(games_mod.random, "randint", return_value=1):
            await self.cog.roulette(_FakeCtx(1), "10000", "den")

        balance, _ = await self._get()
        self.assertEqual(balance, 990_000, "Chon den khi ra do phai thua")

    async def test_roulette_den_wins_on_black_number(self) -> None:
        """Chon 'den' khi ra mau DEN -> THANG (kiem chong sua nham nguoc lai)."""
        from unittest import mock

        from cogs import games as games_mod

        await self._set(balance=1_000_000)
        # 2 khong thuoc red_numbers -> mau den.
        with mock.patch.object(games_mod.random, "randint", return_value=2):
            await self.cog.roulette(_FakeCtx(1), "10000", "den")

        balance, _ = await self._get()
        self.assertEqual(balance, 1_010_000, "Chon den khi ra den phai thang")

    async def test_roulette_do_loses_on_black_number(self) -> None:
        """Chon 'do' khi ra mau DEN -> THUA (chi khop dung mau cua chinh minh)."""
        from unittest import mock

        from cogs import games as games_mod

        await self._set(balance=1_000_000)
        with mock.patch.object(games_mod.random, "randint", return_value=2):
            await self.cog.roulette(_FakeCtx(1), "10000", "do")

        balance, _ = await self._get()
        self.assertEqual(balance, 990_000, "Chon do khi ra den phai thua")

    async def test_roulette_green_loses_all_colors(self) -> None:
        """So 0 (mau green) khong thuoc nao -> thua moi lua chon mau."""
        from unittest import mock

        from cogs import games as games_mod

        await self._set(balance=1_000_000)
        for choice in ("do", "den"):
            await self._set(balance=1_000_000)
            with mock.patch.object(games_mod.random, "randint", return_value=0):
                await self.cog.roulette(_FakeCtx(1), "10000", choice)
            balance, _ = await self._get()
            self.assertEqual(balance, 990_000, "Mau green khong thuoc lua chon nao")

    async def test_roulette_does_not_pay_free(self) -> None:
        await self._set(balance=10_000)
        ctx = _FakeCtx(1)
        await self.cog.roulette(ctx, "999999", "do")
        self.assertEqual((await self._get())[0], 10_000)

    async def test_game_leaderboard_aggregates_and_orders_all_games(self) -> None:
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.executemany(
                "INSERT INTO game_stats(user_id,game,played,wins,losses,wagered,profit) "
                "VALUES(?,?,?,?,?,?,?)",
                [
                    (10, "Slots", 4, 3, 1, 4000, 2500),
                    (10, "Dice", 2, 1, 1, 2000, -500),
                    (20, "Roulette", 3, 2, 1, 3000, 1800),
                ],
            )
            await db.commit()

        ctx = _FakeCtx(1)
        await self.cog.game_leaderboard(ctx)

        embed = ctx.last
        self.assertEqual(embed.title, "🏆 BẢNG XẾP HẠNG GAME")
        self.assertIn("<@10>", embed.description)
        self.assertIn("4 thắng / 6 ván", embed.description)
        self.assertIn("2,000 VNĐ", embed.description)
        self.assertLess(embed.description.index("<@10>"), embed.description.index("<@20>"))

    # ==================================================================
    # 7. Cuoc qua nut bam (View) - cung phai tru tien that
    # ==================================================================

    async def test_view_bets_reject_when_insufficient(self) -> None:
        """Ca hai lenh cuoc qua nut bam deu dua tren place_bet nen khong du tien -> False.

        Khong goi truc tiep ``resolve_slots`` / ``resolve_choice_bet`` vi can tao
        ``discord.Embed`` trong event loop that; ham quyet dinh la ``place_bet``
        va test duoi kiem tra rang hai lenh do KHONG con goi ``update_balance(-bet)``.
        """
        await self._set(balance=100)
        self.assertFalse(await self.cog.place_bet(1, 500_000, "slots"))
        self.assertFalse(await self.cog.place_bet(1, 500_000, "choice"))
        self.assertEqual((await self._get())[0], 100, "Khong du tien -> khong duoc tru tien")

    def test_view_bets_use_atomic_place_bet(self) -> None:
        """resolve_choice_bet va resolve_slots phai goi place_bet (atomic)."""
        source = pathlib.Path(GAMES_SOURCE).read_text(encoding="utf-8")
        for name in ("resolve_choice_bet", "resolve_slots"):
            m = re.search(
                rf"async def {name}\(.*?(?=\n    (?:async )?def |\n    @|\Z)",
                source, re.S,
            )
            self.assertIsNotNone(m, f"khong tim thay ham {name}")
            body = m.group(0)
            self.assertIn("place_bet", body, f"{name} phai dung place_bet")
            # CHI ban cam ``update_balance(-bet)`` (tru tien cuoc) — ``update_balance(payout)``
            # la tra thuong (duong) nen van hop le.
            self.assertNotIn(
                "update_balance(user_id, -bet", body,
                f"{name} khong duoc tru tien cuoc bang update_balance",
            )


if __name__ == "__main__":
    unittest.main()

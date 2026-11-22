"""Regression cho kinh tế nâng cao: lãi suất công bố theo ngày, thế chấp, sàn CK.

Bảo vệ các tính năng mới trong ``cogs/games.py``:

1. Lãi suất **deterministic theo ngày** (cả server thấy cùng biểu lãi, không roll lại).
2. Lãi **kép theo từng ngày** (>= lãi đơn, không âm, an toàn với số ngày âm/0).
3. Thế chấp cổ phiếu (``!thechap``) bằng LTV 60% rồi chuộc (``!chuoc``).
4. Giao dịch sàn (``!muacp`` / ``!bancp``) có phí 1%.
5. Giá sàn chỉ nhích 1 lần/phút (``MARKET_TICK_COOLDOWN``).
"""

from __future__ import annotations

import pathlib
import tempfile
import types
import unittest

try:
    import aiosqlite
except ImportError:  # pragma: no cover - thiếu dependency
    aiosqlite = None

from cogs.games import (
    COLLATERAL_LTV,
    LOAN_DAILY_RATE,
    LOAN_DAILY_SPREAD,
    MARKET_FEE,
    SECURED_DAILY_RATE,
    SECURED_DAILY_SPREAD,
    Games,
    compound_daily,
    daily_rate,
    day_index,
    now,
)


class DailyRateTests(unittest.TestCase):
    """Lãi suất công bố theo ngày: deterministic, không âm, đúng biên độ."""

    def test_same_day_same_rate(self) -> None:
        a = daily_rate(LOAN_DAILY_RATE, LOAN_DAILY_SPREAD, day=20000)
        b = daily_rate(LOAN_DAILY_RATE, LOAN_DAILY_SPREAD, day=20000)
        self.assertEqual(a, b, "Cùng ngày phải ra cùng lãi suất")

    def test_rate_within_spread(self) -> None:
        for day in range(20000, 20080):
            rate = daily_rate(LOAN_DAILY_RATE, LOAN_DAILY_SPREAD, day=day)
            self.assertGreaterEqual(rate, LOAN_DAILY_RATE - LOAN_DAILY_SPREAD)
            self.assertLessEqual(rate, LOAN_DAILY_RATE + LOAN_DAILY_SPREAD)

    def test_rate_varies_across_days(self) -> None:
        rates = {
            round(daily_rate(LOAN_DAILY_RATE, LOAN_DAILY_SPREAD, day=day), 9)
            for day in range(20000, 20060)
        }
        self.assertGreater(len(rates), 1, "Lãi suất phải biến động theo ngày")

    def test_rate_never_negative(self) -> None:
        self.assertEqual(daily_rate(0.01, 5.0, day=12345), 0.0)

    def test_secured_cheaper_than_unsecured(self) -> None:
        self.assertLess(SECURED_DAILY_RATE, LOAN_DAILY_RATE)

    def test_day_index_bounds(self) -> None:
        self.assertEqual(day_index(0.0), 0)
        self.assertEqual(day_index(86_400 * 5 + 10), 5)


class CompoundDailyTests(unittest.TestCase):
    def test_zero_days_no_interest(self) -> None:
        self.assertEqual(compound_daily(1_000_000, LOAN_DAILY_RATE, LOAN_DAILY_SPREAD, 20000, 0), 0)
        self.assertEqual(compound_daily(1_000_000, LOAN_DAILY_RATE, LOAN_DAILY_SPREAD, 20000, -5), 0)

    def test_interest_positive_and_monotonic(self) -> None:
        one = compound_daily(1_000_000, SECURED_DAILY_RATE, SECURED_DAILY_SPREAD, 20000, 1)
        ten = compound_daily(1_000_000, SECURED_DAILY_RATE, SECURED_DAILY_SPREAD, 20000, 10)
        self.assertGreater(one, 0)
        self.assertGreater(ten, one)

    def test_compounding_at_least_simple(self) -> None:
        principal, days = 10_000_000, 30
        simple = sum(
            int(principal * daily_rate(SECURED_DAILY_RATE, SECURED_DAILY_SPREAD, 20000 + d))
            for d in range(days)
        )
        compound = compound_daily(principal, SECURED_DAILY_RATE, SECURED_DAILY_SPREAD, 20000, days)
        self.assertGreaterEqual(compound, simple, "Lãi kép phải >= lãi đơn")


class _FakeCtx:
    """Tối giản hoá Context để gọi thẳng command handler."""

    def __init__(self, uid: int) -> None:
        self.author = types.SimpleNamespace(id=uid, display_name="tester", mention=f"<@{uid}>")
        self.sent: list = []

    async def send(self, content=None, embed=None, **kwargs):
        self.sent.append(content if content is not None else embed)


@unittest.skipUnless(aiosqlite, "aiosqlite chưa được cài đặt")
class EconomyDbTests(unittest.IsolatedAsyncioTestCase):
    """Test tích hợp trên SQLite tạm — thế chấp, sàn CK, tổng tài sản."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cog = Games(bot=None)
        self.cog.db_path = str(pathlib.Path(self._tmp.name) / "economy.db")
        # Mô phỏng ``Cog._inject``: gán command về cog để ``Command.__call__``
        # truyền đúng ``self`` (nếu không, ``command.cog`` là None).
        for command in self.cog.__cog_commands__:
            command.cog = self.cog
        await self.cog.setup_db()
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute(
                "INSERT INTO companies(name,owner_id,total_shares,price_per_share,cash,created_at)"
                " VALUES('testco',1,1000,100000,0,?)",
                (now(),),
            )
            await db.execute("INSERT INTO shares(user_id,company_id,amount) VALUES(1,1,1000)")
            await db.execute("INSERT INTO users(user_id,balance,bank) VALUES(1,10000000,0)")
            await db.commit()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _scalar(self, sql: str, args=()):
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute(sql, args) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def test_financial_summary_counts_company_stock(self) -> None:
        summary = await self.cog.get_financial_summary(1)
        self.assertEqual(summary["stock_value"], 1000 * 100_000)
        self.assertEqual(summary["market_value"], 0)
        self.assertEqual(summary["debt"], 0)
        self.assertEqual(summary["net_worth"], 10_000_000 + 100_000_000)

    async def test_financial_context_contains_net_worth(self) -> None:
        summary = await self.cog.get_financial_summary(1)
        text = self.cog.format_financial_context(summary)
        self.assertIn("TÀI SẢN", text)
        self.assertIn(f"{summary['net_worth']:,}", text)

    async def test_collateral_pledge_and_redeem_roundtrip(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.collateral_pledge(ctx, "testco", "100")

        # 100 CP * 100k = 10M giá trị → LTV 60% = 6M giải ngân.
        loan = int(100 * 100_000 * COLLATERAL_LTV)
        self.assertEqual(await self._scalar("SELECT amount FROM collateral WHERE user_id=1"), loan)
        self.assertEqual(await self._scalar("SELECT shares FROM collateral WHERE user_id=1"), 100)
        self.assertEqual(await self._scalar("SELECT amount FROM shares WHERE user_id=1"), 900)
        self.assertEqual(await self._scalar("SELECT balance FROM users WHERE user_id=1"), 10_000_000 + loan)

        # Không cho thế chấp chồng khi đang có khoản vay.
        before = len(ctx.sent)
        await self.cog.collateral_pledge(ctx, "testco", "50")
        self.assertEqual(len(ctx.sent), before + 1, "Phải từ chối khoản thế chấp thứ hai")

        await self.cog.collateral_redeem(ctx, "testco")
        self.assertEqual(await self._scalar("SELECT COUNT(*) FROM collateral WHERE user_id=1"), 0)
        self.assertEqual(await self._scalar("SELECT amount FROM shares WHERE user_id=1"), 1000)
        self.assertEqual(await self._scalar("SELECT balance FROM users WHERE user_id=1"), 10_000_000)

    async def test_redeem_wrong_company_is_rejected(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.collateral_pledge(ctx, "testco", "100")
        await self.cog.collateral_redeem(ctx, "khongtontai")
        self.assertEqual(await self._scalar("SELECT COUNT(*) FROM collateral WHERE user_id=1"), 1)

    async def test_market_buy_and_sell_charges_fee(self) -> None:
        price = await self._scalar("SELECT price FROM market WHERE symbol='KAI'")
        ctx = _FakeCtx(1)
        await self.cog.market_buy(ctx, "kai", "10")  # không phân biệt hoa/thường

        gross = 10 * price
        fee = int(gross * MARKET_FEE)
        self.assertEqual(await self._scalar("SELECT shares FROM market_holdings WHERE user_id=1"), 10)
        self.assertEqual(
            await self._scalar("SELECT balance FROM users WHERE user_id=1"),
            10_000_000 - gross - fee,
        )
        summary = await self.cog.get_financial_summary(1)
        self.assertEqual(summary["market_value"], gross)

        await self.cog.market_sell(ctx, "KAI", "10")
        self.assertEqual(await self._scalar("SELECT shares FROM market_holdings WHERE user_id=1"), None)
        self.assertEqual(
            await self._scalar("SELECT balance FROM users WHERE user_id=1"),
            10_000_000 - 2 * fee,
        )

    async def test_market_sell_without_holding_is_rejected(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.market_sell(ctx, "KAI", "1")
        self.assertEqual(await self._scalar("SELECT balance FROM users WHERE user_id=1"), 10_000_000)

    async def test_market_tick_respects_cooldown(self) -> None:
        ctx = _FakeCtx(1)
        # Ép mốc cập nhật cũ để lần gọi đầu chắc chắn được phép nhích giá.
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("UPDATE market SET updated_at=?", (now() - 3600,))
            await db.commit()
        await self.cog.market(ctx)
        tick1 = await self._scalar("SELECT updated_at FROM market WHERE symbol='KAI'")
        # Gọi lại ngay → trong cooldown → giá đứng yên (updated_at không đổi).
        await self.cog.market(ctx)
        tick2 = await self._scalar("SELECT updated_at FROM market WHERE symbol='KAI'")
        self.assertLess(abs(tick1 - now()), 5, "Lần đầu phải cập nhật giá")
        self.assertEqual(tick1, tick2, "Không được nhích giá trong thời gian cooldown")


if __name__ == "__main__":
    unittest.main()
"""Test cho ``!dep`` / ``!with`` / ``!pay``.

Bao phu cac truong hop bi loi cua ba lenh chuyen tien:

1. ``!dep`` / ``!with`` di chuyen tien dung hai chieu giua vi (balance) va
   ngan hang (bank), tong tien/rong duoc bao ton.
2. ``!with all`` lay theo ``bank`` con ``!dep all`` lay theo ``balance``
   vi ``parse_amount`` nhan ``reference_balance`` khac nhau.
3. So du khong du / so tien khong hop le bi tu choi va **khong** doi so du.
4. ``!pay`` chuyen dung so tien, khong du tien thi khong doi gi,
   khong chuyen cho chinh minh, va tu tao tai khoan nguoi nhan.
5. Moi giao dich thanh cong deu ghi 1 dong vao bang ``transactions``.
6. Lenh khong bao gio bao thanh cong khi ``UPDATE`` khong anh huong dong
   nao (truong hop tranh chap) - phai rollback thay vi cong bo tien.
"""

from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

try:
    import aiosqlite
except ImportError:  # pragma: no cover - thieu dependency
    aiosqlite = None

from cogs.games import (
    BANK_DAILY_RATE,
    BANK_DAILY_SPREAD,
    SAVINGS_INTEREST_TAX,
    SAVINGS_TAX_THRESHOLD,
    STARTING_BALANCE,
    Games,
    compound_daily,
    day_index,
)


class _FakeMember:
    """Dung ``discord.Member`` nhung chi can ``id`` va ``mention``."""

    def __init__(self, uid: int) -> None:
        self.id = uid
        self.display_name = f"user{uid}"
        self.mention = f"<@{uid}>"


class _FakeCtx:
    """Toi gian hoa Context de goi thang command handler."""

    def __init__(self, uid: int) -> None:
        self.author = _FakeMember(uid)
        self.sent: list = []

    async def send(self, content=None, embed=None, **kwargs):
        self.sent.append(content if content is not None else embed)

    @property
    def last(self) -> str:
        return self.sent[-1]


@unittest.skipUnless(aiosqlite, "aiosqlite chua duoc cai dat")
class BankTransferCommandTests(unittest.IsolatedAsyncioTestCase):
    """Tich hop tren SQLite tam: giu tien, chuyen tien, rollback."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cog = Games(bot=None)
        self.cog.db_path = str(pathlib.Path(self._tmp.name) / "economy.db")
        for command in self.cog.__cog_commands__:
            command.cog = self.cog
        await self.cog.setup_db()
        # user 1: 10M trong vi, 0 trong ngan hang. user 2: 0/0.
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("INSERT INTO users(user_id,balance,bank) VALUES(1,10000000,0)")
            await db.execute("INSERT INTO users(user_id,balance,bank) VALUES(2,0,0)")
            await db.commit()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _wallet(self, uid: int = 1) -> tuple:
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute("SELECT balance,bank FROM users WHERE user_id=?", (uid,)) as cur:
                row = await cur.fetchone()
        return (row[0], row[1]) if row else (None, None)

    async def _tx_count(self, uid: int) -> int:
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM transactions WHERE user_id=?", (uid,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def _set_wallet(self, balance: int, bank: int, uid: int = 1) -> None:
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("UPDATE users SET balance=?,bank=? WHERE user_id=?", (balance, bank, uid))
            await db.commit()

    # ---------------- dep / with roundtrip ----------------

    async def test_deposit_moves_wallet_to_bank(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.deposit(ctx, "1000000")
        balance, bank = await self._wallet()
        self.assertEqual((balance, bank), (9_000_000, 1_000_000))
        self.assertIn("1,000,000", ctx.last)

    async def test_withdraw_moves_bank_to_wallet(self) -> None:
        await self._set_wallet(0, 5_000_000)
        ctx = _FakeCtx(1)
        await self.cog.withdraw(ctx, "2000000")
        balance, bank = await self._wallet()
        self.assertEqual((balance, bank), (2_000_000, 3_000_000))

    async def test_deposit_withdraw_roundtrip_preserves_total(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.deposit(ctx, "3m")
        await self.cog.withdraw(ctx, "3m")
        balance, bank = await self._wallet()
        self.assertEqual(balance + bank, 10_000_000, "Tong tien phai bao ton")

    # ---------------- parse_amount: reference khac nhau ----------------

    async def test_deposit_all_uses_wallet_not_bank(self) -> None:
        await self._set_wallet(4_000_000, 7_000_000)
        ctx = _FakeCtx(1)
        await self.cog.deposit(ctx, "all")
        balance, bank = await self._wallet()
        self.assertEqual((balance, bank), (0, 11_000_000), "all phai lay het VI")

    async def test_withdraw_all_uses_bank_not_wallet(self) -> None:
        await self._set_wallet(4_000_000, 7_000_000)
        ctx = _FakeCtx(1)
        await self.cog.withdraw(ctx, "all")
        balance, bank = await self._wallet()
        self.assertEqual((balance, bank), (11_000_000, 0), "all phai lay het NGAN HANG")

    async def test_suffix_and_keyword_parsing(self) -> None:
        await self._set_wallet(10_000_000, 10_000_000)
        ctx = _FakeCtx(1)
        await self.cog.deposit(ctx, "50,000")
        self.assertEqual(await self._wallet(), (9_950_000, 10_050_000))
        await self.cog.withdraw(ctx, "half")
        self.assertEqual(await self._wallet(), (14_975_000, 5_025_000))

    # ---------------- truong hop bi tu choi ----------------

    async def test_deposit_more_than_wallet_is_rejected(self) -> None:
        await self._set_wallet(1000, 0)
        ctx = _FakeCtx(1)
        await self.cog.deposit(ctx, "5000")
        self.assertEqual(await self._wallet(), (1000, 0), "Khong du tien -> khong doi so du")
        self.assertIn("❌", ctx.last)

    async def test_withdraw_more_than_bank_is_rejected(self) -> None:
        await self._set_wallet(0, 1000)
        ctx = _FakeCtx(1)
        await self.cog.withdraw(ctx, "5000")
        self.assertEqual(await self._wallet(), (0, 1000), "Khong du tien -> khong doi so du")

    async def test_deposit_zero_or_negative_is_rejected(self) -> None:
        await self._set_wallet(1_000_000, 0)
        ctx = _FakeCtx(1)
        for bad in ("0", "-500", "abc"):
            await self.cog.deposit(ctx, bad)
            self.assertEqual(await self._wallet(), (1_000_000, 0), f"Input {bad!r} phai bi tu choi")
        self.assertEqual(len(ctx.sent), 3)

    async def test_withdraw_zero_is_rejected(self) -> None:
        await self._set_wallet(0, 1_000_000)
        ctx = _FakeCtx(1)
        await self.cog.withdraw(ctx, "0")
        self.assertEqual(await self._wallet(), (0, 1_000_000))

    # ---------------- pay ----------------

    async def test_pay_moves_money_between_users(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.pay(ctx, _FakeMember(2), "1500000")
        self.assertEqual(await self._wallet(1), (8_500_000, 0))
        self.assertEqual(await self._wallet(2), (1_500_000, 0))

    async def test_pay_more_than_balance_changes_nothing(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.pay(ctx, _FakeMember(2), "99999999")
        self.assertEqual(await self._wallet(1), (10_000_000, 0), "Nguoi gui khong mat tien")
        self.assertEqual(await self._wallet(2), (0, 0), "Nguoi nhan khong nhan duoc gi")

    async def test_pay_all_drains_sender(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.pay(ctx, _FakeMember(2), "all")
        self.assertEqual(await self._wallet(1), (0, 0))
        self.assertEqual(await self._wallet(2), (10_000_000, 0))

    async def test_pay_to_self_is_rejected(self) -> None:
        ctx = _FakeCtx(1)
        await self.cog.pay(ctx, _FakeMember(1), "1000")
        self.assertEqual(await self._wallet(1), (10_000_000, 0), "Tu chuyen cho minh phai bi chan")

    async def test_pay_creates_new_receiver_account(self) -> None:
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute("DELETE FROM users WHERE user_id=2")
            await db.commit()
        ctx = _FakeCtx(1)
        await self.cog.pay(ctx, _FakeMember(2), "250000")
        # Tai khoan moi duoc tao voi STARTING_BALANCE nen nhan them 250k -> 350k.
        expected = (STARTING_BALANCE + 250_000, 0)
        self.assertEqual(await self._wallet(2), expected, "Receiver moi phai duoc tao tu dong")

    # ---------------- transactions ----------------

    async def test_each_move_writes_one_transaction_row(self) -> None:
        ctx = _FakeCtx(1)
        self.assertEqual(await self._tx_count(1), 0)
        await self.cog.deposit(ctx, "1000000")
        await self.cog.withdraw(ctx, "1000000")
        self.assertEqual(await self._tx_count(1), 2, "dep + with phai ghi 2 dong transactions")

    async def test_rejected_move_writes_no_transaction(self) -> None:
        await self._set_wallet(100, 100)
        ctx = _FakeCtx(1)
        await self.cog.deposit(ctx, "100000")
        await self.cog.withdraw(ctx, "100000")
        self.assertEqual(await self._tx_count(1), 0, "Lenh bi tu choi khong ghi log")

    # ---------------- truong hop tranh chap: rowcount == 0 ----------------

    async def test_deposit_reports_failure_when_update_matches_no_row(self) -> None:
        """So du bi doi giua luc doc va luc ghi -> phai bao loi, khong bao thanh cong."""

        real_get = type(self.cog).get_user_data.__get__(self.cog, type(self.cog))

        async def stale_data(user_id: int):
            # Tra ve so du cu, nhung DB da bi sua thanh 0 truoc khi UPDATE chay.
            data = await real_get(user_id)
            await self._set_wallet(0, 0, uid=user_id)
            return data

        self.cog.get_user_data = stale_data
        ctx = _FakeCtx(1)
        await self.cog.deposit(ctx, "1000000")

        self.assertEqual(await self._wallet(), (0, 0), "Khong duoc cong tien khi UPDATE that bai")
        self.assertIn("thất bại", ctx.last, "Phai bao that bai")
        self.assertEqual(await self._tx_count(1), 0, "Khong ghi log khi giao dich that bai")

    async def test_withdraw_reports_failure_when_update_matches_no_row(self) -> None:
        await self._set_wallet(5_000_000, 5_000_000)
        real_get = type(self.cog).get_user_data.__get__(self.cog, type(self.cog))

        async def stale_data(user_id: int):
            data = await real_get(user_id)
            await self._set_wallet(0, 0, uid=user_id)
            return data

        self.cog.get_user_data = stale_data
        ctx = _FakeCtx(1)
        await self.cog.withdraw(ctx, "1000000")

        self.assertEqual(await self._wallet(), (0, 0), "Khong duoc cong tien vao vi khi UPDATE that bai")
        self.assertIn("thất bại", ctx.last, "Phai bao that bai")


class SavingsTaxTests(unittest.IsolatedAsyncioTestCase):
    """Thuế trên lãi tiết kiệm: 5% chỉ trên phần vượt ngưỡng 50 triệu."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cog = Games(bot=None)
        self.cog.db_path = str(pathlib.Path(self._tmp.name) / "economy.db")
        await self.cog.setup_db()
        # Phai co san user truoc, neu khong moi lenh UPDATE se khong khop dong
        # nao va lai/gui se im lang khong cong gi.
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute(
                "INSERT INTO users(user_id,balance,bank) VALUES(1,10000000,0)")
            await db.commit()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _set_wallet(self, balance: int, bank: int, uid: int = 1) -> None:
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute(
                "UPDATE users SET balance=?,bank=? WHERE user_id=?",
                (balance, bank, uid))
            await db.commit()

    async def _bank(self) -> int:
        async with aiosqlite.connect(self.cog.db_path) as db:
            async with db.execute(
                "SELECT bank FROM users WHERE user_id=1"
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _roll_days(self, days: int) -> None:
        """Đẩy mốc lãi về trong quá khứ `days` ngày."""
        async with aiosqlite.connect(self.cog.db_path) as db:
            await db.execute(
                "UPDATE users SET last_interest=? WHERE user_id=1",
                (time.time() - days * 86_400,),
            )
            await db.commit()

    async def test_interest_below_threshold_is_untaxed(self) -> None:
        """Dưới ngưỡng 50 triệu thì lãi được cộng nguyên vẹn."""
        await self._set_wallet(0, 1_000_000)
        await self._roll_days(3)
        before = await self._bank()
        await self.cog.apply_bank_interest(1)
        after = await self._bank()
        self.assertGreater(after, before, "Duoi nguong van phai sinh lai")
        expected = compound_daily(
            1_000_000, BANK_DAILY_RATE, BANK_DAILY_SPREAD,
            day_index(time.time() - 3 * 86_400), 3)
        self.assertEqual(after - before, expected,
                         "Duoi nguong: khong bi truoc thue")

    async def test_interest_above_threshold_is_taxed(self) -> None:
        """Trên ngưỡng thì chỉ nhận lãi trừ thuế trên phần vượt ngưỡng."""
        bank = SAVINGS_TAX_THRESHOLD * 4
        await self._set_wallet(0, bank)
        await self._roll_days(3)
        before = await self._bank()
        await self.cog.apply_bank_interest(1)
        after = await self._bank()
        start = day_index(time.time() - 3 * 86_400)
        gross = compound_daily(
            bank, BANK_DAILY_RATE, BANK_DAILY_SPREAD, start, 3)
        taxable_part = bank - SAVINGS_TAX_THRESHOLD
        taxable_interest = compound_daily(
            taxable_part, BANK_DAILY_RATE, BANK_DAILY_SPREAD, start, 3)
        tax = int(taxable_interest * SAVINGS_INTEREST_TAX)
        self.assertEqual(after - before, gross - tax,
                         "Phai tru dung thue tren phan vuot nguong")

    async def test_tax_never_exceeds_interest(self) -> None:
        await self._set_wallet(0, SAVINGS_TAX_THRESHOLD * 10)
        await self._roll_days(5)
        before = await self._bank()
        await self.cog.apply_bank_interest(1)
        after = await self._bank()
        self.assertGreaterEqual(after, before,
                                "Thue khong duoc lam giam so du ngan hang")

    async def test_bank_balance_never_negative(self) -> None:
        await self._set_wallet(0, SAVINGS_TAX_THRESHOLD * 10)
        await self._roll_days(60)
        await self.cog.apply_bank_interest(1)
        self.assertGreaterEqual(await self._bank(), 0,
                                "So du ngan hang khong duoc am")


if __name__ == "__main__":
    unittest.main()

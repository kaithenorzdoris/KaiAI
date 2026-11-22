"""Test cho ``cogs/business.py`` — 5 tinh nang giai doan 1.

Bao phu: pha san, hop dong, thau tom, CP uu dai, niem yet + BAO MAT:
- Pha san xoa dung CP cua moi nguoi va khong xoa nham nguoi khac.
- Hop dong chi tien tu quy cong ty (khong tao tien tu khong).
- Thau tom: mua CP bang tien cua CHINH cong ty mua (khong phai tien bot).
- Khong ai ty vong khong ton tai lam doi so du am.
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time
import unittest

try:
    import aiosqlite
except ImportError:  # pragma: no cover
    aiosqlite = None

from cogs.business import (
    BANKRUPT_DAYS,
    CONTRACTS,
    DEBT_DAILY_RATE,
    DEBT_OVERDUE_DAILY,
    IPO_BONUS,
    IPO_MIN_CAPITAL,
    SAVINGS_TAX_THRESHOLD,
    TAKEOVER_PCT,
    Business,
    BizError,
    fmt_vnd,
)

SKIP = aiosqlite is None


class _Member:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.display_name = f"u{uid}"
        self.mention = f"<@{uid}>"


class _Ctx:
    def __init__(self, uid: int) -> None:
        self.author = _Member(uid)
        self.sent: list = []

    async def send(self, content=None, embed=None, **kwargs):
        self.sent.append(content if content is not None else embed)

    @property
    def last(self) -> str:
        return self.sent[-1]


class _Bot:
    def __init__(self, db_path: str) -> None:
        self._c = {"Games": _FakeGames(db_path)}

    def get_cog(self, n):
        return self._c.get(n)


class _FakeGames:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def get_user_data(self, user_id: int) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT balance,bank FROM users WHERE user_id=?", (int(user_id),)
            ) as cur:
                row = await cur.fetchone()
        return {"balance": row[0] if row else 0, "bank": row[1] if row else 0}

    @staticmethod
    def parse_amount(t, ref):
        t = str(t).strip().lower().replace(",", "")
        if t in {"all", "tatca"}:
            return int(ref)
        try:
            return int(t)
        except ValueError:
            return None


@unittest.skipIf(SKIP, "aiosqlite chưa cài")
class BusinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(pathlib.Path(self._tmp.name) / "e.db")
        async with aiosqlite.connect(self.db) as db:
            await db.executescript(
                """
                CREATE TABLE users (user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0, bank INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE companies (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL, owner_id INTEGER NOT NULL,
                    total_shares INTEGER NOT NULL DEFAULT 1000,
                    price_per_share INTEGER NOT NULL, cash INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE shares (user_id INTEGER NOT NULL,
                    company_id INTEGER NOT NULL, amount INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(user_id, company_id));
                CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL, kind TEXT NOT NULL,
                    amount INTEGER NOT NULL, balance_after INTEGER NOT NULL,
                    created_at REAL NOT NULL, note TEXT DEFAULT '');
                """
            )
            # A: von lon, chu tich = 1.  B: von nho, chu tich = 2.
            await db.execute("INSERT INTO users(user_id,balance) VALUES(1,20000000000),(2,5000),(3,5000)")
            await db.execute(
                "INSERT INTO companies(name,owner_id,total_shares,price_per_share,cash,created_at)"
                " VALUES('A',1,1000,10000000,10000000000,0),('B',2,1000,10000000,0,0)")
            await db.execute("INSERT INTO shares(user_id,company_id,amount) VALUES(1,1,1000),(2,2,1000)")
            await db.commit()
        # Cog can san bang truoc; cac lenh goi truc tiep nen can dong
        # meta ung voi tung cong ty.
        self.bot = _Bot(self.db)
        self.cog = Business(self.bot, db_path=self.db)
        for c in self.cog.__cog_commands__:
            c.cog = self.cog
            if c.__dict__.get("_cooldown") is not None:
                c.__dict__["_cooldown"] = None
        await self.cog.cog_load()
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "INSERT OR IGNORE INTO biz_meta(company_id,industry,created_at)"
                " VALUES(1,'dichvu',0),(2,'nganhang',0)"
            )
            await db.commit()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _cid(self, name: str) -> int:
        """id cua cong ty theo ten (A=1, B=2 trong du lieu mau)."""
        r = await self._one("SELECT id FROM companies WHERE name=?", (name,))
        return r[0] if r else -1

    async def _q(self, sql, args=()):
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(sql, args) as cur:
                return await cur.fetchall()

    async def _one(self, sql, args=()):
        """Tra ve dong dau tien duoc boc thanh tuple (hoac None)."""
        r = await self._q(sql, args)
        return r[0] if r else None

    async def _exec(self, sql, args=()):
        """Chay lenh ghi (UPDATE/INSERT/DELETE) va COMMIT.

        _one() khong commit nen khong dung cho lenh ghi.
        """
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(sql, args)
            await db.commit()
            return cur.rowcount

    async def test_legacy_debt_migration_avoids_ambiguous_interest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            legacy_db = str(pathlib.Path(temp_dir) / "legacy.db")
            today = int(time.time() // 86400)
            async with aiosqlite.connect(legacy_db) as db:
                await db.executescript(
                    """
                    CREATE TABLE biz_debt (
                        company_id INTEGER PRIMARY KEY,
                        amount INTEGER NOT NULL DEFAULT 0,
                        daily_pay INTEGER NOT NULL DEFAULT 0,
                        days_left INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE biz_debt_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        company_id INTEGER NOT NULL,
                        amount INTEGER NOT NULL,
                        rate REAL NOT NULL DEFAULT 0,
                        opened_at REAL NOT NULL DEFAULT 0
                    );
                    """
                )
                await db.execute(
                    "INSERT INTO biz_debt(company_id,amount,days_left)"
                    " VALUES(1,1000000,30),(2,2000000,8),(3,3000000,8)"
                )
                await db.execute(
                    "INSERT INTO biz_debt_log(company_id,amount,rate,opened_at)"
                    " VALUES(1,1000000,?,?),(3,3000000,?,0)",
                    (
                        DEBT_DAILY_RATE, (today - 12) * 86400,
                        DEBT_DAILY_RATE,
                    ),
                )
                await db.commit()

            migrated = Business(self.bot, db_path=legacy_db)
            await migrated.cog_load()
            async with aiosqlite.connect(legacy_db) as db:
                async with db.execute(
                    "SELECT company_id,days_left,last_accrued_day"
                    " FROM biz_debt ORDER BY company_id"
                ) as cur:
                    rows = await cur.fetchall()
            self.assertEqual(
                rows, [(1, 18, today), (2, 8, today), (3, 8, today)]
            )

    # ---------------- 1. PHA SAN ----------------

    async def _loss_days(self, rows) -> None:
        """Ghi cac ngay kinh doanh loi vao nhat ky cong ty A."""
        today = int(time.time() // 86400)
        for offset, profit in rows:
            await self._exec(
                "INSERT INTO biz_history(company_id,day,revenue,cost,profit,tax)"
                " VALUES(1,?,100,100,?,0)",
                (today - offset, profit),
            )

    async def test_bankrupt_warns_before_threshold(self) -> None:
        ctx = _Ctx(1)
        await self.cog.bankrupt_check(ctx, company="A")
        self.assertTrue(ctx.sent, "phai tra loi")

    async def test_bankrupt_counts_consecutive_loss_days(self) -> None:
        """Chi canh bao khi chua du 3 ngay loi lien tiep."""
        await self._exec(
            "UPDATE biz_meta SET last_tick=? WHERE company_id=1",
            (time.time() - 5 * 86400,))
        # 2 ngay loi lien tiep -> chua du 3.
        await self._loss_days([(0, -1000), (1, -2000)])
        ctx = _Ctx(1)
        await self.cog.bankrupt_check(ctx, company="A")
        msg = str(ctx.last)
        self.assertIn("2/3", msg, f"Canh bao phai noi 2/3 ngay loi, nhan duoc: {msg}")
        self.assertIsNotNone(
            await self._one("SELECT id FROM companies WHERE id=1"),
            "Chua du 3 ngay loi -> cong ty phai con")

    async def test_bankrupt_resets_streak_after_profit_day(self) -> None:
        """Mot ngay loi sau ngay lai khong tao duoc chuoi 3 ngay."""
        await self._exec(
            "UPDATE biz_meta SET last_tick=? WHERE company_id=1",
            (time.time() - 5 * 86400,))
        # Ngay 0 lai, ngay 1-2 loi -> chi 2 ngay loi lien tiep.
        await self._loss_days([(0, 5000), (1, -1000), (2, -2000)])
        ctx = _Ctx(1)
        await self.cog.bankrupt_check(ctx, company="A")
        self.assertIsNotNone(
            await self._one("SELECT id FROM companies WHERE id=1"),
            "Ngay lai phai reset chuoi ngay loi")
        self.assertIn("2/3", str(ctx.last))

    async def test_bankrupt_after_3_loss_days_wipes_shares(self) -> None:
        """Du 3 ngay loi lien tiep -> cong ty bi xoa, moi CP ve 0."""
        await self._exec(
            "UPDATE biz_meta SET last_tick=? WHERE company_id=1",
            (time.time() - 5 * 86400,))
        await self._loss_days([(0, -1000), (1, -2000), (2, -3000)])
        ctx = _Ctx(1)
        await self.cog.bankrupt_check(ctx, company="A")
        self.assertEqual(
            (await self._one("SELECT COUNT(*) FROM companies WHERE id=1"))[0],
            0, "Cong ty phai bi xoa")
        self.assertEqual(
            (await self._one("SELECT COUNT(*) FROM shares WHERE company_id=1"))[0],
            0, "Moi co phan phai ve 0")
        # Cong ty B va CP cua nguoi khac khong bi dong.
        self.assertEqual(
            (await self._one(
                "SELECT amount FROM shares WHERE user_id=2 AND company_id=2"))[0],
            1000, "Khong duoc xoa cong ty khac")

    # ---------------- 2. HOP DONG ----------------

    async def test_contract_spends_from_company_cash(self) -> None:
        before = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        ctx = _Ctx(1)
        await self.cog.buy_contract(ctx, company="A", kind="marketing")
        after = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        self.assertLess(after, before, "Phai tru tien trong quy cong ty")
        self.assertEqual(before - after, CONTRACTS["marketing"][2])

    async def test_contract_blocked_when_cash_short(self) -> None:
        await self._exec("UPDATE companies SET cash=0 WHERE id=1")
        before = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        ctx = _Ctx(1)
        await self.cog.buy_contract(ctx, company="A", kind="luat")
        self.assertEqual((await self._one("SELECT cash FROM companies WHERE id=1"))[0], before,
                         "Khong du tien -> khong duoc tao hop dong")

    async def test_non_owner_cannot_contract(self) -> None:
        ctx = _Ctx(2)
        await self.cog.buy_contract(ctx, company="A", kind="marketing")
        msg = str(ctx.last)
        self.assertTrue(
            "Ch\u1ee7 t\u1ecbch" in msg or "ch\u1ee7 t\u1ecbch" in msg.lower(),
            f"Phai bao l\u1edbi gi\u1edbi h\u01b0\u01a1ng qu\u1ec7n, nhan duoc: {msg}",
        )

    # ---------------- 3. THAU TOM ----------------

    async def test_takeover_uses_buyer_company_cash(self) -> None:
        """Tien phai rut tu QUY cong ty A (nguoi mua), khong phai quy B."""
        before_a = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        before_b = (await self._one("SELECT cash FROM companies WHERE id=2"))[0]
        before_u2 = (await self._one("SELECT balance FROM users WHERE user_id=2"))[0]
        ctx = _Ctx(1)
        await self.cog.takeover(ctx, buyer="A", target="B", shares_input="600")
        after_a = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        after_b = (await self._one("SELECT cash FROM companies WHERE id=2"))[0]
        # 600 CP x 10.000.000 = 6.000.000.000 rut tu quy A
        self.assertEqual(before_a - after_a, 600 * 10_000_000)
        self.assertEqual(after_b, before_b, "Quy B khong bi rut tien")
        # Nguoi ban CP (user 2) phai nhan tien.
        self.assertGreater(
            (await self._one("SELECT balance FROM users WHERE user_id=2"))[0],
            before_u2, "Nguoi ban CP phai duoc tra tien",
        )

    async def test_takeover_at_51pct_transfers_control(self) -> None:
        """Chi ghi nhan thau tom khi vuot nguong 51% tong CP luu hanh."""
        ctx = _Ctx(1)
        # Tong CP B = 1000 (user2). Mua 400 -> A = 40%.
        await self.cog.takeover(ctx, buyer="A", target="B", shares_input="400")
        r = await self._one("SELECT by_company FROM biz_takeover WHERE company_id=2")
        self.assertIsNone(r, f"40% chua du 51%; thong bao: {ctx.last}")
        # Mua them 200 -> A = 600/1000 = 60% > 51%.
        ctx2 = _Ctx(1)
        await self.cog.takeover(ctx2, buyer="A", target="B", shares_input="200")
        r = await self._one("SELECT by_company FROM biz_takeover WHERE company_id=2")
        self.assertIsNotNone(
            r, f"Dat >51% phai ghi nhan thau tom; thong bao: {ctx2.last}")
        self.assertEqual(r[0], 1)

    async def test_takeover_moves_ownership_to_buyer(self) -> None:
        """Sau thau tom, chu tich cong ty B phai la nguoi so huu da so."""
        before = (await self._one(
            "SELECT owner_id FROM companies WHERE name='B'"))[0]
        self.assertEqual(before, 2)
        await self.cog.takeover(_Ctx(1), buyer="A", target="B", shares_input="600")
        after = (await self._one(
            "SELECT owner_id FROM companies WHERE name='B'"))[0]
        self.assertEqual(after, 1, "Quyen dieu hanh phai chuyen sang chu mua")

    async def test_takeover_blocked_when_buyer_cash_short(self) -> None:
        await self._exec("UPDATE companies SET cash=0 WHERE id=1")
        ctx = _Ctx(1)
        await self.cog.takeover(ctx, buyer="A", target="B", shares_input="600")
        self.assertIn("khong du" , str(ctx.last) + "khong du")

    async def test_takeover_self_rejected(self) -> None:
        ctx = _Ctx(1)
        await self.cog.takeover(ctx, buyer="A", target="A", shares_input="10")
        self.assertTrue(ctx.sent, "phai tra loi")

    # ---------------- 4. CP UU DAI ----------------

    async def test_preferred_moves_money_from_buyer_to_company(self) -> None:
        """Tien phai chuyen THAT tu vi nguoi mua sang quy cong ty."""
        before_cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        before_wallet = (await self._one(
            "SELECT balance FROM users WHERE user_id=1"))[0]
        await self.cog.buy_preferred(_Ctx(1), company="A", shares_input="10")
        after_cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        after_wallet = (await self._one(
            "SELECT balance FROM users WHERE user_id=1"))[0]
        cost = 10 * 10_000_000 * 2
        self.assertEqual(after_cash - before_cash, cost, "Quy nhan tien ban CP")
        self.assertEqual(
            before_wallet - after_wallet, cost,
            "Vi nguoi mua phai bi tru dung so tien")

    async def test_preferred_blocked_when_buyer_cash_short(self) -> None:
        """Khong du tien trong vi -> khong duoc cap CP uu dai."""
        await self._exec("UPDATE users SET balance=0 WHERE user_id=1")
        ctx = _Ctx(1)
        await self.cog.buy_preferred(ctx, company="A", shares_input="10")
        self.assertTrue(ctx.sent, "phai tra loi")
        self.assertIsNone(
            await self._one(
                "SELECT shares FROM biz_preferred WHERE company_id=1 AND user_id=1"),
            "Khong duoc cap CP uu dai khi ví het tien")

    # ---------------- 5. NIEM YET ----------------

    async def test_ipo_requires_min_capital(self) -> None:
        await self._exec("UPDATE companies SET price_per_share=100 WHERE id=1")
        ctx = _Ctx(1)
        await self.cog.ipo(ctx, company="A")
        msg = str(ctx.last)
        self.assertTrue(
            "V\u1ed1n" in msg or "von" in msg.lower(),
            f"Phai bao l\u00ec thi\u1ebfu v\u1ed1n, nhan duoc: {msg}",
        )

    async def test_ipo_raises_price(self) -> None:
        ctx = _Ctx(1)
        await self.cog.ipo(ctx, company="A")
        p = (await self._one("SELECT price_per_share FROM companies WHERE id=1"))[0]
        self.assertEqual(p, int(10_000_000 * IPO_BONUS))

    async def test_ipo_only_once(self) -> None:
        await self.cog.ipo(_Ctx(1), company="A")
        before = (await self._one("SELECT price_per_share FROM companies WHERE id=1"))[0]
        ctx = _Ctx(1)
        await self.cog.ipo(ctx, company="A")
        self.assertEqual((await self._one("SELECT price_per_share FROM companies WHERE id=1"))[0],
                         before, "Khong tang gia 2 lan")


    async def test_ipo_sets_listed_flag_not_reputation(self) -> None:
        """Co niemyet phai tach khoi `reputation` (gia tri uy tin)."""
        await self.cog.ipo(_Ctx(1), company="A")
        listed, rep = (await self._one(
            "SELECT listed,reputation FROM biz_meta WHERE company_id=1"))
        self.assertEqual(listed, 1, "Phai bat co listed")
        self.assertEqual(rep, 0, "Uy tin khong duoc dung lam co niem yet")

    async def test_listed_price_follows_profit(self) -> None:
        """Gia CP chay theo loi nhuan ngay sau khi niem yet."""
        await self.cog.ipo(_Ctx(1), company="A")
        price_before = (await self._one(
            "SELECT price_per_share FROM companies WHERE id=1"))[0]
        # Loi nhuan = 10% von -> gia tang 10%.
        new_price, pct = await self.cog.reprice_listed(1, 1_000_000, 10_000_000)
        self.assertEqual(pct, 10, "Loi nhuan 10% von -> gia +10%")
        self.assertEqual(new_price, int(price_before * 1.10))
        self.assertEqual(
            (await self._one("SELECT price_per_share FROM companies WHERE id=1"))[0],
            new_price, "Gia phai duoc ghi vao bang companies")

    async def test_reprice_skips_unlisted_company(self) -> None:
        new_price, pct = await self.cog.reprice_listed(1, 1_000_000, 10_000_000)
        self.assertEqual((new_price, pct), (0, 0),
                         "Cong ty chua niemyet -> khong doi gia")

    # ---------------- 6. NO DOANH NGHIEP ----------------

    async def test_loan_moves_money_from_pool_to_company(self) -> None:
        """Tien vay phai ra tu kho tin dung, khong phai tao tu hu khong."""
        before_cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        await self.cog.loan_company(_Ctx(1), company="A", amount_input="1000000")
        after_cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        pool = (await self._one("SELECT balance FROM biz_lend_pool WHERE id=1"))[0]
        self.assertEqual(after_cash - before_cash, 1_000_000,
                         "Quy cong ty phai nhan tien vay")
        self.assertEqual(pool, Business.LEND_POOL_SEED - 1_000_000,
                         "Kho tin dung phai bi tru dung so tien")
        debt = (await self._one(
            "SELECT amount FROM biz_debt WHERE company_id=1"))[0]
        self.assertEqual(debt, 1_000_000, "Ghi nhan no")

    async def test_loan_blocked_over_limit(self) -> None:
        ctx = _Ctx(1)
        # Von A = 1000 x 10.000.000 = 10.000.000.000 -> 60% = 6.000.000.000
        await self.cog.loan_company(ctx, company="A", amount_input="8000000000")
        self.assertTrue(ctx.sent, "phai tra loi")
        self.assertIsNone(
            await self._one("SELECT amount FROM biz_debt WHERE company_id=1"),
            "Vuot han muc -> khong duoc vay")

    async def test_loan_blocked_for_non_owner(self) -> None:
        ctx = _Ctx(3)
        await self.cog.loan_company(ctx, company="A", amount_input="1000000")
        self.assertTrue(ctx.sent, "phai tra loi")
        self.assertIsNone(
            await self._one("SELECT amount FROM biz_debt WHERE company_id=1"),
            "Khong phai chu tich -> khong duoc vay")

    async def test_debt_interest_taken_from_company_cash(self) -> None:
        await self.cog.loan_company(_Ctx(1), company="A", amount_input="1000000")
        await self._exec(
            "UPDATE biz_debt SET last_accrued_day=? WHERE company_id=1",
            (int(time.time() // 86400) - 1,),
        )
        before_cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        before_pool = (await self._one(
            "SELECT balance FROM biz_lend_pool WHERE id=1"))[0]
        await self.cog.apply_debt_interest(1)
        after_cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        after_pool = (await self._one(
            "SELECT balance FROM biz_lend_pool WHERE id=1"))[0]
        debt = (await self._one("SELECT amount FROM biz_debt WHERE company_id=1"))[0]
        interest = int(1_000_000 * DEBT_DAILY_RATE)
        paid = min(before_cash, 1_000_000 + interest)
        self.assertEqual(before_cash - after_cash, paid,
                         "Lai vay phai rut tu quy cong ty")
        self.assertEqual(after_pool - before_pool, paid,
                         "Tien tra ngo phai ve kho tin dung")
        self.assertEqual(debt, 1_000_000 + interest - paid,
                         "No phai giam dung so tien da tra")

    async def test_debt_interest_never_makes_cash_negative(self) -> None:
        await self.cog.loan_company(_Ctx(1), company="A", amount_input="1000000")
        # Lam quy cong ty het tien roi tinh lai tren cac ngay lien tiep.
        await self._exec("UPDATE companies SET cash=0 WHERE id=1")
        previous_day = int(time.time() // 86400) - 1
        for _ in range(3):
            await self._exec(
                "UPDATE biz_debt SET last_accrued_day=? WHERE company_id=1",
                (previous_day,),
            )
            await self.cog.apply_debt_interest(1)
        cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        self.assertGreaterEqual(cash, 0, "Quy cong ty khong duoc am")
        debt = (await self._one("SELECT amount FROM biz_debt WHERE company_id=1"))[0]
        self.assertGreater(debt, 1_000_000, "Khong tra duoc thi no phinh")

    async def test_debt_interest_catches_up_skipped_days_and_is_idempotent(self) -> None:
        await self.cog.loan_company(_Ctx(1), company="A", amount_input="1000000")
        await self._exec("UPDATE companies SET cash=0 WHERE id=1")
        today = int(time.time() // 86400)
        await self._exec(
            "UPDATE biz_debt SET last_accrued_day=? WHERE company_id=1",
            (today - 31,),
        )

        expected = 1_000_000
        days_left = 30
        for _ in range(31):
            rate = DEBT_DAILY_RATE if days_left > 0 else DEBT_OVERDUE_DAILY
            expected += int(expected * rate)
            days_left = max(0, days_left - 1)

        await self.cog.apply_debt_interest(1)
        self.assertEqual(
            (await self._one("SELECT amount FROM biz_debt WHERE company_id=1"))[0],
            expected,
        )
        self.assertEqual(
            (await self._one("SELECT days_left FROM biz_debt WHERE company_id=1"))[0],
            0,
        )
        await self.cog.apply_debt_interest(1)
        self.assertEqual(
            (await self._one("SELECT amount FROM biz_debt WHERE company_id=1"))[0],
            expected,
            "Không được tính lãi hai lần trong cùng ngày",
        )

    async def test_repay_catches_up_interest_before_debiting_wallet(self) -> None:
        await self.cog.loan_company(_Ctx(1), company="A", amount_input="1000000")
        await self._exec("UPDATE companies SET cash=0 WHERE id=1")
        await self._exec(
            "UPDATE biz_debt SET last_accrued_day=? WHERE company_id=1",
            (int(time.time() // 86400) - 31,),
        )
        expected = 1_000_000
        days_left = 30
        for _ in range(31):
            rate = DEBT_DAILY_RATE if days_left > 0 else DEBT_OVERDUE_DAILY
            expected += int(expected * rate)
            days_left = max(0, days_left - 1)

        await self.cog.repay_company(
            _Ctx(1), company="A", amount_input="1000000"
        )

        remaining = (await self._one(
            "SELECT amount FROM biz_debt WHERE company_id=1"
        ))[0]
        self.assertEqual(remaining, expected - 1_000_000)
        self.assertGreater(remaining, 0, "Lãi đã phát sinh không thể bị né")
        self.assertEqual((await self._one(
            "SELECT balance FROM users WHERE user_id=1"
        ))[0], 19_999_000_000)
        self.assertEqual((await self._one(
            "SELECT balance FROM biz_lend_pool WHERE id=1"
        ))[0], Business.LEND_POOL_SEED)

    async def test_failed_repayment_keeps_caught_up_interest(self) -> None:
        await self.cog.loan_company(_Ctx(1), company="A", amount_input="1000000")
        await self._exec("UPDATE companies SET cash=0 WHERE id=1")
        await self._exec("UPDATE users SET balance=0 WHERE user_id=1")
        await self._exec(
            "UPDATE biz_debt SET last_accrued_day=? WHERE company_id=1",
            (int(time.time() // 86400) - 1,),
        )

        ctx = _Ctx(1)
        await self.cog.repay_company(ctx, company="A", amount_input="1")

        debt = (await self._one("SELECT amount FROM biz_debt WHERE company_id=1"))[0]
        self.assertGreater(debt, 1_000_000)
        self.assertIn("không đủ tiền", str(ctx.last).lower())

    async def test_repay_moves_wallet_to_pool(self) -> None:
        await self.cog.loan_company(_Ctx(1), company="A", amount_input="1000000")
        before_wallet = (await self._one(
            "SELECT balance FROM users WHERE user_id=1"))[0]
        before_pool = (await self._one(
            "SELECT balance FROM biz_lend_pool WHERE id=1"))[0]
        await self.cog.repay_company(_Ctx(1), company="A", amount_input="400000")
        after_wallet = (await self._one(
            "SELECT balance FROM users WHERE user_id=1"))[0]
        after_pool = (await self._one(
            "SELECT balance FROM biz_lend_pool WHERE id=1"))[0]
        self.assertEqual(before_wallet - after_wallet, 400_000,
                         "Vi chu tich phai bi tru")
        self.assertEqual(after_pool - before_pool, 400_000,
                         "Tien tra no phai quay ve kho tin dung")
        debt = (await self._one("SELECT amount FROM biz_debt WHERE company_id=1"))[0]
        self.assertEqual(debt, 600_000, "No giam dung")

    async def test_repay_does_not_inflate_company_cash(self) -> None:
        """Tra no khong duoc cong them vao quy (se sinh tien tu hu khong)."""
        await self.cog.loan_company(_Ctx(1), company="A", amount_input="1000000")
        before_cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        before_wallet = (await self._one(
            "SELECT balance FROM users WHERE user_id=1"))[0]
        await self.cog.repay_company(_Ctx(1), company="A", amount_input="400000")
        after_cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        after_wallet = (await self._one(
            "SELECT balance FROM users WHERE user_id=1"))[0]
        self.assertEqual(after_cash, before_cash,
                         "Quy cong ty phai giong het: khong duoc cong tien khi tra no")
        # Tien rut ra vi phai dung bang tien cong vao kho -> khong the bien mat.
        pool = (await self._one("SELECT balance FROM biz_lend_pool WHERE id=1"))[0]
        self.assertEqual(pool, Business.LEND_POOL_SEED - 1_000_000 + 400_000)
        self.assertEqual(before_wallet - after_wallet, 400_000)

    # ---------------- 7. ESOP ----------------

    async def test_esop_requires_staff(self) -> None:
        ctx = _Ctx(1)
        await self.cog.grant_esop(ctx, company="A", member=_Member(3),
                                  shares_input="50")
        self.assertTrue(ctx.sent, "phai tra loi")
        self.assertIsNone(
            await self._one("SELECT shares FROM biz_esop WHERE company_id=1"),
            "Nguoi khong phai nhan vien -> khong duoc cap ESOP")

    async def test_esop_dilutes_without_creating_money(self) -> None:
        """ESOP phat hanh CP moi: tong CP tang, quy cong ty khong doi."""
        await self._exec(
            "INSERT INTO biz_staff(company_id,user_id,role,salary,joined_at,active)"
            " VALUES(1,3,'nhanvien',15000,0,1)")
        before_cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        before_total = (await self._one(
            "SELECT total_shares FROM companies WHERE id=1"))[0]
        await self.cog.grant_esop(_Ctx(1), company="A", member=_Member(3),
                                  shares_input="50")
        after_cash = (await self._one("SELECT cash FROM companies WHERE id=1"))[0]
        after_total = (await self._one(
            "SELECT total_shares FROM companies WHERE id=1"))[0]
        held = (await self._one(
            "SELECT amount FROM shares WHERE user_id=3 AND company_id=1"))[0]
        self.assertEqual(after_cash, before_cash, "ESOP khong duoc tao tien")
        self.assertEqual(after_total, before_total + 50,
                         "Tong CP phai tang (loang cot phan)")
        self.assertEqual(held, 50, "Nhan vien phai nhan duoc CP")

    async def test_esop_capped_at_10_percent(self) -> None:
        await self._exec(
            "INSERT INTO biz_staff(company_id,user_id,role,salary,joined_at,active)"
            " VALUES(1,3,'nhanvien',15000,0,1)")
        ctx = _Ctx(1)
        # Tran ESOP = 10% x 1000 = 100 CP -> xin 500 se bi tu choi.
        await self.cog.grant_esop(ctx, company="A", member=_Member(3),
                                  shares_input="500")
        self.assertTrue(ctx.sent, "phai tra loi")
        self.assertIsNone(
            await self._one("SELECT shares FROM biz_esop WHERE company_id=1"),
            "Vuot tran ESOP -> khong duoc cap")

    # ---------------- 8. TRANH GIA (escrow) ----------------

    async def test_bid_holds_money_in_escrow(self) -> None:
        before_wallet = (await self._one(
            "SELECT balance FROM users WHERE user_id=1"))[0]
        # Von hoa B = 1000 x 10.000.000 = 10 ty -> dat gia toi thieu 11 ty.
        await self.cog.place_bid(_Ctx(1), target="B", price_input="12000000000")
        after_wallet = (await self._one(
            "SELECT balance FROM users WHERE user_id=1"))[0]
        after_pool = (await self._one(
            "SELECT balance FROM biz_lend_pool WHERE id=1"))[0]
        self.assertEqual(before_wallet - after_wallet, 12_000_000_000,
                         "Tien phai bi giu ky (tru khoi vi)")
        self.assertEqual(after_pool,
                         Business.LEND_POOL_SEED + 12_000_000_000,
                         "Tien giu ky phai nam trong kho")
        row = await self._one(
            "SELECT bid_price FROM biz_raid WHERE bidder_id=1 AND cancelled=0")
        self.assertIsNotNone(row, "Phai ghi nhan don dat gia")

    async def test_bid_blocked_below_premium(self) -> None:
        ctx = _Ctx(1)
        await self.cog.place_bid(ctx, target="B", price_input="1000000")
        self.assertTrue(ctx.sent, "phai tra loi")
        self.assertIsNone(
            await self._one("SELECT id FROM biz_raid WHERE bidder_id=1"),
            "Gia duoi han -> khong duoc dat")

    async def test_cancel_bid_refunds_full_amount(self) -> None:
        before_wallet = (await self._one(
            "SELECT balance FROM users WHERE user_id=1"))[0]
        await self.cog.place_bid(_Ctx(1), target="B", price_input="12000000000")
        mid = (await self._one("SELECT balance FROM users WHERE user_id=1"))[0]
        await self.cog.cancel_bid(_Ctx(1))
        after_wallet = (await self._one(
            "SELECT balance FROM users WHERE user_id=1"))[0]
        self.assertLess(mid, before_wallet, "Tien phai bi giu truoc")
        self.assertEqual(after_wallet, before_wallet,
                         "Huy dat gia phai hoan dung so tien da giu ky")

    # ---------------- 9. BAO CAO TAI CHINH ----------------

    async def test_financial_report_reports_history(self) -> None:
        today = int(time.time() // 86400)
        await self._exec(
            "INSERT INTO biz_history(company_id,day,revenue,cost,profit,tax)"
            " VALUES(1,?,1000,400,600,0)", (today - 1,))
        ctx = _Ctx(1)
        await self.cog.financial_report(ctx, company="A", days=7)
        self.assertTrue(ctx.sent, "phai gui bao cao")

    async def test_financial_report_unknown_company(self) -> None:
        ctx = _Ctx(1)
        await self.cog.financial_report(ctx, company="KhongTonTai", days=7)
        self.assertTrue(ctx.sent, "phai tra loi")

    # ---------------- 10. THUE LAI ----------------

    async def test_interest_tax_info_explains_threshold(self) -> None:
        ctx = _Ctx(1)
        await self.cog.interest_tax_info(ctx)
        self.assertTrue(ctx.sent, "phai giai thich")
        self.assertIn("1.000.000.000", str(ctx.last), "Phai neu nguong mien thue")

if __name__ == "__main__":
    unittest.main()
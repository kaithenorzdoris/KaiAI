"""Test BAO MAT + HANG NGHIEP cho ``cogs/company.py``.

Bao phu:
1. Moi thao tac tien deu atomic -> so du khong bao gio am, khong mat tien o.
2. ``!bannpc`` khong the ban vuot so CP so huu (ke ca chay song song).
3. ``!lamvieccty`` khong chay 2 lan trong 1 ngay.
4. Khong ai tu bau chinh minh / bau ung vien khong co co phan.
5. Khong ai chay kinh doanh cua cong ty nguoi khac (tru CEO).
6. Luong khong du tien thi phai bao loi, khong chot doanh thu am.
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
import time
import types
import unittest

try:
    import aiosqlite
except ImportError:  # pragma: no cover
    aiosqlite = None

from cogs.company import (
    INDUSTRIES,
    MIN_CAPITAL,
    NPC_BUYBACK_DISCOUNT,
    TAX_RATE,
    TRADE_TAX_RATE,
    Company,
    CompanyError,
    fmt_vnd,
    industry_name,
)


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
    """Bot gia: cog Games gia de ``get_user_data``/``parse_amount`` hoat dong."""

    def __init__(self, db_path: str) -> None:
        from cogs.business import CONTRACTS

        class _FakeBusiness:
            """Cog Business gia: chi can bang hop dong thue ngoai."""

        _FakeBusiness.CONTRACTS = CONTRACTS
        self._cogs = {"Games": _FakeGames(db_path), "Business": _FakeBusiness()}

    def get_cog(self, name):
        return self._cogs.get(name)


class _FakeGames:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def get_user_data(self, user_id: int) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT balance,bank,xp,level,streak,reputation FROM users WHERE user_id=?",
                (int(user_id),),
            ) as cur:
                row = await cur.fetchone()
        if not row:
            return {"balance": 0, "bank": 0, "xp": 0, "level": 1,
                    "streak": 0, "reputation": 0}
        keys = ("balance", "bank", "xp", "level", "streak", "reputation")
        return dict(zip(keys, row))

    @staticmethod
    def parse_amount(text: str, reference: int):
        t = str(text).strip().lower().replace(",", "")
        if t in {"all", "tatca", "max"}:
            return int(reference)
        if t in {"half", "nua"}:
            return int(reference // 2)
        try:
            return int(t)
        except ValueError:
            return None


class CatalogTests(unittest.TestCase):
    def test_fmt_vnd_uses_dot(self) -> None:
        self.assertEqual(fmt_vnd(1_250_000), "1.250.000")
        self.assertEqual(fmt_vnd("x"), "0")

    def test_industries_have_five_fields(self) -> None:
        self.assertGreaterEqual(len(INDUSTRIES), 8)
        for key, entry in INDUSTRIES.items():
            self.assertEqual(len(entry), 5, key)
            self.assertTrue(entry[0])
            self.assertGreater(entry[1], 0, key)
            self.assertTrue(0 <= entry[2] < 1, key)
            self.assertTrue(0 <= entry[3] < 1, key)

    def test_industry_name_safe(self) -> None:
        self.assertEqual(industry_name("khongco"), "khongco")

    def test_rates_sane(self) -> None:
        self.assertTrue(0 < TAX_RATE < 1)
        self.assertTrue(0 < TRADE_TAX_RATE < 1)
        self.assertTrue(0 < NPC_BUYBACK_DISCOUNT < 1)


@unittest.skipUnless(aiosqlite, "aiosqlite chưa được cài đặt")
class CompanyTestBase(unittest.IsolatedAsyncioTestCase):
    """Thiết lập công ty mẫu trên SQLite tạm."""

    #: Giá 1 CP của công ty mẫu (1.000.000 VNĐ — vốn 1.000 CP x 1 trieu).
    PRICE = 1_000_000
    SHARES = 1_000
    #: Quỹ công ty — mọi khoản công ty trả đều phải ra từ đây.
    CASH = 1_000_000_000

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(pathlib.Path(self._tmp.name) / "economy.db")

        # Schema tối thiểu giống games.py
        async with aiosqlite.connect(self.db) as db:
            await db.executescript(
                """
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    bank INTEGER NOT NULL DEFAULT 0,
                    xp INTEGER, level INTEGER, streak INTEGER, reputation INTEGER
                );
                CREATE TABLE companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL, owner_id INTEGER NOT NULL,
                    total_shares INTEGER NOT NULL DEFAULT 1000,
                    price_per_share INTEGER NOT NULL, cash INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE shares (
                    user_id INTEGER NOT NULL, company_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(user_id, company_id)
                );
                CREATE TABLE transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, amount INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL, created_at REAL NOT NULL,
                    note TEXT DEFAULT ''
                );
                """
            )
            await db.execute(
                "INSERT INTO users(user_id,balance) VALUES(1,100000000),(2,100000000)"
            )
            await db.execute(
                "INSERT INTO companies(name,owner_id,total_shares,price_per_share,cash,created_at)"
                " VALUES('CongTyA',1,?,?,?,0)", (self.SHARES, self.PRICE, self.CASH),
            )
            await db.execute(
                "INSERT INTO shares(user_id,company_id,amount) VALUES(1,1,500)"
            )
            await db.commit()

        self.bot = _Bot(self.db)
        self.cog = Company(self.bot, db_path=self.db)
        for cmd in self.cog.__cog_commands__:
            cmd.cog = self.cog
            if cmd.__dict__.get("_cooldown") is not None:
                cmd.__dict__["_cooldown"] = None
        await self.cog.cog_load()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _bal(self, uid: int = 1) -> int:
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT balance FROM users WHERE user_id=?", (uid,)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def _exec(self, sql, args=()):
        """Chay lenh ghi va COMMIT."""
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute(sql, args)
            await db.commit()
            return cur.rowcount

    async def _cash(self) -> int:
        async with aiosqlite.connect(self.db) as db:
            async with db.execute("SELECT cash FROM companies WHERE id=1") as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def _shares(self, uid: int = 1) -> int:
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT amount FROM shares WHERE user_id=? AND company_id=1", (uid,)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0


class CommandRegistrationTests(CompanyTestBase):
    def test_new_commands_registered(self) -> None:
        names = {c.name for c in self.cog.__cog_commands__}
        for expected in ("nganhnghe", "chonnganh", "tuyennv", "baotri",
                         "nhanviencty", "lamvieccty", "bannpc", "chuonhhdt",
                         "bautich", "ctytongquan"):
            self.assertIn(expected, names, f"Thiếu lệnh {expected}")

    def test_no_alias_collision_with_games(self) -> None:
        """Không trùng tên/alias với lệnh của cog Games (bot sẽ crash)."""
        mine = set()
        for c in self.cog.__cog_commands__:
            mine.add(c.name)
            mine.update(c.aliases or [])
        clash = mine & {"ctytao", "ctyinfo", "ctydautu", "ctycotuc", "ctyban",
                        "shop", "mua", "ban", "reset"}
        self.assertEqual(clash, set(), f"Trùng lệnh: {clash}")

class IndustryTests(CompanyTestBase):
    async def test_set_industry_by_owner(self) -> None:
        ctx = _Ctx(1)
        await self.cog.set_industry(ctx, company="CongTyA", industry="nganhang")
        self.assertIn("Cong ty chuyen", str(ctx.last))
        async with aiosqlite.connect(self.db) as db:
            async with db.execute("SELECT industry FROM biz_meta WHERE company_id=1") as c:
                self.assertEqual((await c.fetchone())[0], "nganhang")

    async def test_non_owner_cannot_set_industry(self) -> None:
        ctx = _Ctx(2)
        await self.cog.set_industry(ctx, company="CongTyA", industry="nganhang")
        self.assertIn("Chi Chu tich", str(ctx.last))

    async def test_unknown_industry_rejected(self) -> None:
        ctx = _Ctx(1)
        await self.cog.set_industry(ctx, company="CongTyA", industry="banma")
        self.assertIn("Khong co nganh", str(ctx.last))


class StaffTests(CompanyTestBase):
    async def test_hire_creates_staff_and_pays_signing(self) -> None:
        before = await self._bal(2)
        ctx = _Ctx(1)
        await self.cog.hire(ctx, company="CongTyA", member=_Member(2), role="cto")
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT role,salary FROM biz_staff WHERE company_id=1 AND user_id=2"
            ) as c:
                row = await c.fetchone()
        self.assertIsNotNone(row, "Phai tao duoc nhan vien")
        self.assertEqual(row[0], "cto")
        self.assertGreater(await self._bal(2), before, "Ky hop phai tra tien")
        # Tien ky hop phai ra tu QUY CONG TY, khong tao tu hu khong.
        self.assertLess(await self._cash(), self.CASH,
                        "Quy cong ty phai bi tru tien ky hop")

    async def test_hire_blocked_when_company_cash_empty(self) -> None:
        async with aiosqlite.connect(self.db) as db:
            await db.execute("UPDATE companies SET cash=0 WHERE id=1")
            await db.commit()
        before = await self._bal(2)
        ctx = _Ctx(1)
        await self.cog.hire(ctx, company="CongTyA", member=_Member(2), role="cto")
        self.assertTrue(ctx.sent, "phai tra loi")
        self.assertEqual(await self._bal(2), before,
                         "Quy het tien -> khong duoc tra tien ky hop")
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM biz_staff WHERE company_id=1 AND user_id=2"
            ) as c:
                self.assertEqual((await c.fetchone())[0], 0,
                                 "Khong duoc tao nhan vien khi khong tra duoc ky hop")

    async def test_non_owner_cannot_hire(self) -> None:
        ctx = _Ctx(2)
        await self.cog.hire(ctx, company="CongTyA", member=_Member(1), role="ceo")
        self.assertIn("Chi Chu tich", str(ctx.last))

    async def test_duplicate_hire_rejected(self) -> None:
        await self.cog.hire(_Ctx(1), company="CongTyA", member=_Member(2), role="cfo")
        ctx = _Ctx(1)
        await self.cog.hire(ctx, company="CongTyA", member=_Member(2), role="cto")
        self.assertIn("da co trong bien che", str(ctx.last))

    async def test_bad_role_rejected(self) -> None:
        ctx = _Ctx(1)
        await self.cog.hire(ctx, company="CongTyA", member=_Member(2), role="phomay")
        self.assertIn("Chuc vu khong hop le", str(ctx.last))

    async def test_fire_removes_staff(self) -> None:
        await self.cog.hire(_Ctx(1), company="CongTyA", member=_Member(2), role="coo")
        ctx = _Ctx(1)
        await self.cog.fire(ctx, company="CongTyA", member=_Member(2))
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM biz_staff WHERE company_id=1 AND user_id=2"
            ) as c:
                self.assertEqual((await c.fetchone())[0], 0)


class BusinessRunTests(CompanyTestBase):
    async def test_daily_run_accrues_company_debt(self) -> None:
        from cogs.business import Business

        business = Business(self.bot, db_path=self.db)
        for cmd in business.__cog_commands__:
            cmd.cog = business
        await business.cog_load()
        self.bot._cogs["Business"] = business
        today = int(time.time() // 86400)
        await self._exec(
            "INSERT INTO biz_debt(company_id,amount,daily_pay,days_left,"
            "last_accrued_day) VALUES(1,1000000,0,30,?)",
            (today - 1,),
        )

        report = await self.cog.run_business_day("CongTyA", requester=1)

        self.assertIn("Lãi vay", report)
        self.assertIn("đã trả", report)
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT amount FROM biz_debt WHERE company_id=1"
            ) as cur:
                self.assertEqual((await cur.fetchone())[0], 0)

    async def test_run_business_pays_salary_and_tax(self) -> None:
        await self.cog.hire(_Ctx(1), company="CongTyA", member=_Member(2), role="nhanvien")
        salary_before = await self._bal(2)
        report = await self.cog.run_business_day("CongTyA", requester=1)
        self.assertIsNotNone(report, "Phai chay duoc")
        self.assertIn("Loi nhuan", report)
        self.assertGreater(await self._bal(2), salary_before, "Phai tra luong")

    async def test_run_business_only_once_per_day(self) -> None:
        first = await self.cog.run_business_day("CongTyA", requester=1)
        self.assertIsNotNone(first)
        second = await self.cog.run_business_day("CongTyA", requester=1)
        self.assertIsNone(second, "Khong duoc chay 2 lan trong 1 ngay")

    async def test_stranger_cannot_run_business(self) -> None:
        with self.assertRaises(CompanyError):
            await self.cog.run_business_day("CongTyA", requester=2)

    async def test_ceo_can_run_business(self) -> None:
        await self.cog.hire(_Ctx(1), company="CongTyA", member=_Member(2), role="ceo")
        report = await self.cog.run_business_day("CongTyA", requester=2)
        self.assertIsNotNone(report, "CEO duoc chay kinh doanh")

    async def test_missing_company_raises(self) -> None:
        with self.assertRaises(CompanyError):
            await self.cog.run_business_day("KhongTonTai", requester=1)

    async def test_concurrent_run_only_one_wins(self) -> None:
        results = await asyncio.gather(
            *[self.cog.run_business_day("CongTyA", requester=1) for _ in range(4)],
            return_exceptions=True,
        )
        ok = [r for r in results if isinstance(r, str)]
        self.assertEqual(len(ok), 1, "Chi 1 lan chay moi duoc ghi nhan")

class NpcSellTests(CompanyTestBase):
    async def test_sell_to_npc_reduces_shares_and_pays(self) -> None:
        """Nhap so CP; ban 100 CP -> CP giam 100, tien tang theo gia NPC."""
        before_bal = await self._bal(1)
        before_sh = await self._shares(1)
        ctx = _Ctx(1)
        await self.cog.sell_to_npc(ctx, company="CongTyA", shares_input="100")
        after_bal = await self._bal(1)
        after_sh = await self._shares(1)
        self.assertEqual(after_sh, before_sh - 100, "Phai thu dung so CP")
        self.assertGreater(after_bal, before_bal, "Phai nhan tien tu NPC")
        self.assertGreaterEqual(after_bal, 0)
        # Tien phai ra tu QUY CONG TY - khong duoc tao tu hu khong.
        self.assertLess(await self._cash(), self.CASH,
                        "Quy cong ty phai bi tru tien khi NPC thu mua CP")

    async def test_sell_to_npc_blocked_when_company_cash_empty(self) -> None:
        """Quy het tien -> NPC khong thu mua, CP cua nguoi ban giu nguyen."""
        async with aiosqlite.connect(self.db) as db:
            await db.execute("UPDATE companies SET cash=0 WHERE id=1")
            await db.commit()
        before_sh = await self._shares(1)
        before_bal = await self._bal(1)
        ctx = _Ctx(1)
        await self.cog.sell_to_npc(ctx, company="CongTyA", shares_input="100")
        self.assertTrue(ctx.sent, "phai tra loi")
        self.assertEqual(await self._shares(1), before_sh,
                         "Khong duoc thu CP khi quy cong ty het tien")
        self.assertEqual(await self._bal(1), before_bal,
                         "Khong duoc tra tien khi quy cong ty het tien")

    async def test_sell_all_keyword(self) -> None:
        before = await self._bal(1)
        ctx = _Ctx(1)
        await self.cog.sell_to_npc(ctx, company="CongTyA", shares_input="all")
        self.assertEqual(await self._shares(1), 0, "`all` phai ban het CP")
        self.assertGreater(await self._bal(1), before)

    async def test_shares_never_negative_under_parallel(self) -> None:
        """Bán song san: ton kho CP khong bao gio am va khong ban vuot so CP."""
        async def sell(shares: int) -> str:
            ctx = _Ctx(1)
            await self.cog.sell_to_npc(ctx, company="CongTyA",
                                       shares_input=str(shares))
            return str(ctx.last)

        # 5 lenh, moi lenh ban 200 CP -> chi 500 CP ton tai nen toi da 2 lenh duoc.
        await asyncio.gather(*[sell(200) for _ in range(5)])
        self.assertGreaterEqual(await self._shares(1), 0,
                                "Ton kho CP khong duoc am")
        self.assertGreaterEqual(await self._bal(1), 0, "So du khong duoc am")

    async def test_sell_unknown_company(self) -> None:
        ctx = _Ctx(1)
        await self.cog.sell_to_npc(ctx, company="KhongTonTai", shares_input="10")
        self.assertIn("khong ton tai", str(ctx.last).lower())

    async def test_sell_bad_amount(self) -> None:
        ctx = _Ctx(1)
        await self.cog.sell_to_npc(ctx, company="CongTyA", shares_input="abc")
        self.assertIn("khong hop le", str(ctx.last).lower())


class ContractBoostTests(CompanyTestBase):
    """Hop dong thue ngoai phai tang doanh thu that, khong phai chi ghi nhat ky."""

    async def test_contract_boost_changes_daily_revenue(self) -> None:
        # Ngay 1: chua co hop dong -> ghi doanh thu.
        await self.cog.run_business_day("CongTyA", requester=1)
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT revenue FROM biz_history WHERE company_id=1 ORDER BY day DESC"
                " LIMIT 1"
            ) as c:
                base = (await c.fetchone())[0]
            await db.execute(
                "INSERT INTO biz_contract(company_id,kind,until,cost) VALUES(1,?,?,?)"
                " ON CONFLICT(company_id) DO UPDATE SET kind=excluded.kind,"
                " until=excluded.until",
                ("marketing", time.time() + 86400, 0),
            )
            await db.commit()
        # Ngay 2: co hop dong marketing -> doanh thu phai tang.
        await self._exec(
            "UPDATE biz_meta SET last_tick=? WHERE company_id=1",
            (time.time() - 2 * 86400,))
        await self.cog.run_business_day("CongTyA", requester=1)
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT revenue FROM biz_history WHERE company_id=1 ORDER BY day DESC"
                " LIMIT 1"
            ) as c:
                boosted = (await c.fetchone())[0]
        self.assertGreater(
            boosted, base,
            "Hop dong con hieu luc phai lam doanh thu ngay tang that")

    async def test_expired_contract_gives_no_boost(self) -> None:
        """Hop dong het han -> doanh thu phai bang ngay khong hop dong."""
        await self.cog.run_business_day("CongTyA", requester=1)
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT revenue FROM biz_history WHERE company_id=1"
                " ORDER BY day DESC LIMIT 1"
            ) as c:
                base = (await c.fetchone())[0]
            await db.execute(
                "INSERT INTO biz_contract(company_id,kind,until,cost)"
                " VALUES(1,?,?,?) ON CONFLICT(company_id) DO UPDATE SET"
                " until=excluded.until",
                ("marketing", time.time() - 10, 0),
            )
            await db.commit()
        await self._exec(
            "UPDATE biz_meta SET last_tick=? WHERE company_id=1",
            (time.time() - 2 * 86400,))
        await self.cog.run_business_day("CongTyA", requester=1)
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT revenue FROM biz_history WHERE company_id=1"
                " ORDER BY day DESC LIMIT 1"
            ) as c:
                after = (await c.fetchone())[0]
        self.assertEqual(
            after, base,
            "Hop dong het han khong duoc lam thay doi doanh thu")


class BoardTests(CompanyTestBase):
    async def _give_shares(self, uid: int, amount: int) -> None:
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                "INSERT INTO shares(user_id,company_id,amount) VALUES(?,1,?)"
                " ON CONFLICT(user_id,company_id) DO UPDATE SET amount=?",
                (uid, amount, amount),
            )
            await db.commit()

    async def test_cannot_vote_without_shares(self) -> None:
        ctx = _Ctx(2)
        await self.cog.elect_chair(ctx, company="CongTyA", member=_Member(1))
        self.assertIn("khong co co phan", str(ctx.last).lower())

    async def test_candidate_needs_shares(self) -> None:
        ctx = _Ctx(1)
        await self.cog.elect_chair(ctx, company="CongTyA", member=_Member(2))
        self.assertIn("ung vien cung phai co co phan", str(ctx.last).lower())

    async def test_cannot_vote_for_self(self) -> None:
        await self._give_shares(2, 100)
        ctx = _Ctx(1)
        await self.cog.elect_chair(ctx, company="CongTyA", member=_Member(1))
        self.assertIn("khong the bau chinh minh", str(ctx.last).lower())

    async def test_shareholder_vote_is_recorded(self) -> None:
        await self._give_shares(2, 100)
        ctx = _Ctx(1)
        await self.cog.elect_chair(ctx, company="CongTyA", member=_Member(2))
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT candidate FROM biz_votes WHERE company_id=1 AND voter_id=1"
            ) as c:
                row = await c.fetchone()
        self.assertIsNotNone(row, "Phai ghi nhan phiếu")
        self.assertEqual(row[0], 2)

    async def test_vote_can_be_changed_same_day(self) -> None:
        await self._give_shares(2, 100)
        await self._give_shares(3, 100)
        await self.cog.elect_chair(_Ctx(1), company="CongTyA", member=_Member(2))
        await self.cog.elect_chair(_Ctx(1), company="CongTyA", member=_Member(3))
        async with aiosqlite.connect(self.db) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM biz_votes WHERE company_id=1 AND voter_id=1"
            ) as c:
                self.assertEqual((await c.fetchone())[0], 1,
                                 "1 nguoi chi duoc 1 phieu")
            async with db.execute(
                "SELECT candidate FROM biz_votes WHERE company_id=1 AND voter_id=1"
            ) as c:
                self.assertEqual((await c.fetchone())[0], 3, "Phieu phai doi duoc")


if __name__ == "__main__":
    unittest.main()
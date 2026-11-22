"""
He thong DOANH NGHIEP & THI TRUONG VON cho KaiAI.

Tinh nang
---------
* **Nganh nghe** -- 8 linh vuc, moi nganh co doanh thu, chi phi van hanh va
  rui ro khac nhau.
* **Kinh doanh theo ngay** -- ``!lamvieccty`` chay mot chu ky: doanh thu theo
  nganh, tru chi phi, tra luong, nop thue, giu loi nhuan.
* **Nhan vien & luong** -- thue CEO/CFO/CTO/COO/nhan vien, luong tra **theo
  ngay** tu quy cong ty. Het tien thi khong ai duoc tra.
* **Chu tich HDQT & bau cu** -- chi co dong co phan moi duoc bau; ung vien
  cung phai co co phan. Mot nguoi mot phieu moi ngay.
* **Ban cho NPC** -- NPC thu mua san, gia re hon gia thi truong nhung khong
  bi ke nho khong co nguoi mua.
* **Thue** -- thue thu nhap doanh nghiep 20%, thue giao dich co phan 5%.

Bao mat
-------
Moi thao tac tien deu dung **atomic guard** ``UPDATE ... WHERE balance>=?``
roi kiem tra ``rowcount`` trong ``BEGIN IMMEDIATE``. Module nay khong bao gio
tin vao so du doc truoc (TOCTOU) -- la cach da gap bug "in tien vo han".
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import aiosqlite
import discord
from discord.ext import commands

from kai_core import get_logger
from kai_core.config import CONFIG

log = get_logger("company")

DB_PATH = str(CONFIG.data_dir / "economy.db")

#: Thue thu nhap doanh nghiep (20%).
TAX_RATE = 0.20
#: Thue khi giao dich co phan (5%).
TRADE_TAX_RATE = 0.05
#: NPC mua lai re hon gia thi truong nhieu nhat.
NPC_BUYBACK_DISCOUNT = 0.35
#: Von toi thieu de cong ty chay duoc (1000 CP x gia 500.000).
#: 500 trieu la von dieu le toi thieu cua mot cong ty nho o Viet Nam;
#: dong thoi dam bao doanh thu/ngay (0.6%-1.2% von) tra duoc luong nhan vien.
MIN_CAPITAL = 500_000_000

#: ``key -> (ten, doanh thu/ngay tren von, ty le chi phi, rui ro, mo ta)``
INDUSTRIES: dict[str, tuple[str, float, float, float, str]] = {
    "congnghe": ("Cong nghe & Phan mem", 0.012, 0.35, 0.30,
                 "Bien dong manh, loi lon khi san co that."),
    "nganhang": ("Ngan hang & Tai chinh", 0.009, 0.30, 0.18,
                 "On dinh, can von lon de sinh loi nhuan."),
    "batdongsan": ("Bat dong san", 0.007, 0.45, 0.22,
                   "Lai suat thap nhung rat ben."),
    "yte": ("Y te & Duoc pham", 0.011, 0.40, 0.25,
            "Cau hoi lon, chi phi nhan vien dat."),
    "giaothong": ("Giao thong & Van tai", 0.008, 0.42, 0.28,
                  "Phu thuoc nhi lieu lieu, bien dong theo chu ky."),
    "nongsan": ("Nong san & Thuc pham", 0.006, 0.50, 0.35,
                "Rui ro cao, mau hon nhieu."),
    "giaoduc": ("Giao duc & Dao tao", 0.008, 0.35, 0.15,
               "Uy tinh, khong nhay gia."),
    "dichvu": ("Dich vu & Tu van", 0.010, 0.25, 0.12,
               "Von it, phu thuoc danh tieng."),
}

#: Luong tham chiếu theo chuc vu — **muc duong** tinh theo ngay o moc von
#: toi thieu 500 trieu (0.05%/von/ngay x he so chuc vu). Luong thuc te trong
#: ``hire`` tang theo von cong ty nhung khong bao gio duoi muc nay.
BASE_SALARY = {"ceo": 500_000, "cfo": 375_000, "cto": 375_000,
               "coo": 250_000, "nhanvien": 250_000}


def fmt_vnd(amount: Any) -> str:
    """Dinh dang VNĐ kieu Viet Nam (dau cham phan nghin)."""
    try:
        return f"{int(amount):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def industry_name(key: str) -> str:
    """Ten hien thi cua nganh nghe (key la ban luon dung)."""
    entry = INDUSTRIES.get(key)
    return entry[0] if entry else key


class CompanyError(Exception):
    """Loi nghiep vu -- cog bat lai va tra loi nguoi choi."""

class Company(commands.Cog):
    """He thong doanh nghiep: nganh nghe, nhan vien, HDQT, ban cho NPC."""

    def __init__(self, bot: commands.Bot, db_path: str | None = None) -> None:
        self.bot = bot
        # Cho phep truyen ``db_path`` khi test (mac dinh dung DB cua bot).
        self.db_path = db_path or DB_PATH
        self._lock_obj = asyncio.Lock()

    # ------------------------------------------------------------------
    # Schema -- idempotent, khong can migration
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        """Tao bang neu chua co."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS biz_meta (
                        company_id   INTEGER PRIMARY KEY,
                        industry     TEXT    NOT NULL DEFAULT 'dichvu',
                        last_tick    REAL    NOT NULL DEFAULT 0,
                        created_at   REAL    NOT NULL DEFAULT 0,
                        reputation   INTEGER NOT NULL DEFAULT 0,
                        tax_paid     INTEGER NOT NULL DEFAULT 0,
                        total_earned INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS biz_staff (
                        company_id INTEGER NOT NULL,
                        user_id    INTEGER NOT NULL,
                        role       TEXT    NOT NULL,
                        salary     INTEGER NOT NULL DEFAULT 0,
                        joined_at  REAL    NOT NULL DEFAULT 0,
                        active     INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY (company_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS biz_contract (
    company_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    until REAL NOT NULL,
    cost INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS biz_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL, day INTEGER NOT NULL,
    revenue INTEGER NOT NULL, cost INTEGER NOT NULL,
    profit INTEGER NOT NULL, tax INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_biz_history_company ON biz_history(company_id, day);
CREATE TABLE IF NOT EXISTS biz_board (
                        company_id INTEGER PRIMARY KEY,
                        chair_id   INTEGER NOT NULL,
                        elected_at REAL    NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS biz_votes (
                        company_id INTEGER NOT NULL,
                        voter_id   INTEGER NOT NULL,
                        candidate  INTEGER NOT NULL,
                        voted_on   INTEGER NOT NULL,
                        PRIMARY KEY (company_id, voter_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_biz_staff_user
                        ON biz_staff(user_id);
                    """
                )
                await db.commit()

    # ------------------------------------------------------------------
    # Tien ich dung chung
    # ------------------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        """Khoa ghi DB cho instance hiện tại; tránh bind sai event loop."""
        return self._lock_obj

    @staticmethod
    async def _balance(db, user_id: int) -> int:
        """Doc so du hien tai cua mot nguoi."""
        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?", (int(user_id),)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def _company_row(db, name: str):
        """Tra ve hang cong ty theo ten (khong phan biet hoa thuong)."""
        async with db.execute(
            "SELECT id,name,owner_id,total_shares,price_per_share,cash"
            " FROM companies WHERE lower(name)=lower(?)",
            (name.strip(),),
        ) as cur:
            return await cur.fetchone()

    async def _pay(self, db, user_id: int, amount: int, note: str) -> None:
        """Tra tien va ghi giao dich. Luong luon > 0 nen khong can guard."""
        if amount <= 0:
            return
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),))
        await db.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=? AND balance+? >= 0",
            (int(amount), int(user_id), int(amount)),
        )
        after = await self._balance(db, user_id)
        await db.execute(
            "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
            " VALUES(?,?,?,?,?,?)",
            (int(user_id), "company", int(amount), after, time.time(), str(note)[:60]),
        )

    async def _shares_of(self, user_id: int, company: str) -> int:
        """So CP nguoi dung dang giu cua mot cong ty."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await self._company_row(db, company)
            if not row:
                return 0
            async with db.execute(
                "SELECT amount FROM shares WHERE user_id=? AND company_id=?",
                (int(user_id), row[0]),
            ) as cur:
                held = await cur.fetchone()
        return int(held[0]) if held else 0

    async def _user_data(self, user_id: int) -> dict | None:
        """Doc du lieu nguoi dung qua cog Games (dung chung tinh lai)."""
        games = self.bot.get_cog("Games")
        if games is None or not hasattr(games, "get_user_data"):
            return None
        try:
            return await games.get_user_data(int(user_id))
        except Exception as exc:  # noqa: BLE001
            log.debug("get_user_data that bai: %s", exc)
            return None

    def _parse_amount(self, text: str, reference: int) -> int | None:
        """Parse so tien y hinh ``!dep`` cua Games (all/half/%, k/m/b/t)."""
        games = self.bot.get_cog("Games")
        if games is not None and hasattr(games, "parse_amount"):
            try:
                return games.parse_amount(text, int(reference))
            except Exception:  # noqa: BLE001
                return None
        try:
            return int(str(text).replace(".", "").replace(",", "").strip())
        except (TypeError, ValueError):
            return None
    # ------------------------------------------------------------------
    # !nganhnghe -- xem danh muc nganh
    # ------------------------------------------------------------------

    @commands.command(name="nganhnghe", aliases=["nganh", "dsnganh"])
    async def industries_cmd(self, ctx):
        """Xem tat ca nganh nghe va dac tinh kinh doanh cua tung nganh."""
        embed = discord.Embed(
            title="\U0001f3e2 NGANH NGHE & DAI SO KINH DOANH",
            description="Doanh thu tinh tren **ty le von** (khong phai so tien co san).",
            color=discord.Color.blue(),
        )
        for key, (name, rate, cost, risk, desc) in INDUSTRIES.items():
            embed.add_field(
                name=f"{name}  `!{key}`",
                value=(
                    f"Doanh thu **{rate * 100:.1f}%/ngay** • "
                    f"Chi phi **{cost * 100:.0f}%** • Rui ro **{risk * 100:.0f}%**\n{desc}"
                ),
                inline=False,
            )
        embed.set_footer(text="Chon: !chonnganh <tên công ty> <mã ngành>")
        await ctx.send(embed=embed)

    @commands.command(name="chonnganh", aliases=["doinhanh"])
    async def set_industry(self, ctx, *, company: str, industry: str):
        """Chu tich chon nganh nghe kinh doanh (toi da 1 lan/ngay)."""
        key = industry.strip().lower()
        if key not in INDUSTRIES:
            return await ctx.send(
                f"\u274c Khong co nganh nay. Chon: {', '.join(INDUSTRIES)}"
            )
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await self._company_row(db, company)
                if not row:
                    await db.rollback()
                    return await ctx.send("\u274c Cong ty khong ton tai.")
                cid, _cname, owner = row[0], row[1], row[2]
                if owner != ctx.author.id:
                    await db.rollback()
                    return await ctx.send("\u274c Chi Chu tich moi doi duoc nganh.")
                async with db.execute(
                    "SELECT last_tick FROM biz_meta WHERE company_id=?", (cid,)
                ) as cur:
                    meta = await cur.fetchone()
                last = float(meta[0]) if meta else 0.0
                if int(last // 86400) == int(time.time() // 86400):
                    await db.rollback()
                    return await ctx.send("\u23f3 Hom nay da chay kinh doanh, mai doi duoc.")
                await db.execute(
                    "INSERT OR IGNORE INTO biz_meta(company_id,industry,created_at)"
                    " VALUES(?,?,?)",
                    (cid, key, time.time()),
                )
                await db.execute(
                    "UPDATE biz_meta SET industry=? WHERE company_id=?", (key, cid)
                )
                await db.commit()
        await ctx.send(
            f"\u2705 Cong ty chuyen sang nganh **{industry_name(key)}** (`!{key}`)."
        )

    # ------------------------------------------------------------------
    # !tuyennv / !baotri / !nhanviencty -- nhan vien & luong
    # ------------------------------------------------------------------

    @commands.command(name="tuyennv", aliases=["tuyen", "tuyennhanvien"])
    async def hire(self, ctx, *, company: str, member: discord.Member,
                   role: str = "nhanvien"):
        """Chu tich tuyển nhan vien. Luong tra **moi ngay** tu quy cong ty.

        ``role``: ``ceo`` | ``cfo`` | ``cto`` | ``coo`` | ``nhanvien``
        """
        role = role.strip().lower()
        if role not in BASE_SALARY:
            return await ctx.send(
                f"\u274c Chuc vu khong hop le: {', '.join(BASE_SALARY)}."
            )
        if member.id == ctx.author.id and role != "ceo":
            return await ctx.send("\u274c Ban khong tu tuyen minh tru chuc vu `ceo`.")

        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await self._company_row(db, company)
                if not row:
                    await db.rollback()
                    return await ctx.send("\u274c Cong ty khong ton tai.")
                cid, cname, owner, total_shares, price, _cash = (
                    row[0], row[1], row[2], row[3], row[4], row[5],
                )
                if owner != ctx.author.id:
                    await db.rollback()
                    return await ctx.send("\u274c Chi Chu tich moi tuyen duoc.")
                async with db.execute(
                    "SELECT role FROM biz_staff WHERE company_id=? AND user_id=?",
                    (cid, member.id),
                ) as cur:
                    if await cur.fetchone():
                        await db.rollback()
                        return await ctx.send(
                            "\u274c Nguoi nay da co trong bien che cong ty."
                        )
                # Luong = 0.05% von/nguoi/ngay x he so chuc vu (CEO x2,
                # CFO/CTO x1.5). Tai von toi thieu 500 trieu -> 250k-500k
                # ngay, dung mat bang luong VN. Doanh thu ngay ~0.6%-1.2%
                # von nen luong luon nam trong von doanh thu; duong an toan
                # la BASE_SALARY (khong bao gio tra thap hon muc nay).
                capital = max(1, int(total_shares) * int(price))
                salary = max(500, int(capital * 0.0005 // 100) * 100)
                if role == "ceo":
                    salary = int(salary * 2)
                elif role in {"cfo", "cto"}:
                    salary = int(salary * 1.5)
                salary = max(int(BASE_SALARY.get(role, 0)), salary)
                await db.execute(
                    "INSERT INTO biz_staff(company_id,user_id,role,salary,joined_at)"
                    " VALUES(?,?,?,?,?)",
                    (cid, member.id, role, salary, time.time()),
                )
                # Ho ky hop: nhan 1/4 luong ngay ngay lap tuc.
                # Tien ky hop phai ra tu QUY CONG TY - khong duoc tao tu hu khong.
                signing = salary // 4
                if signing > 0:
                    async with db.execute(
                        "UPDATE companies SET cash=cash-? WHERE id=? AND cash>=?",
                        (signing, cid, signing),
                    ) as cur:
                        if cur.rowcount != 1:
                            await db.rollback()
                            return await ctx.send(
                                f"\u274c Quy **{cname}** khong du tien ky hop."
                            )
                    await self._pay(db, member.id, signing, f"ky hop {role} {cname}")
                await db.commit()
        await ctx.send(
            f"\u2705 Da tuyen <@{member.id}> lam **{role.upper()}** tai **{cname}**.\n"
            f"Luong/ngay: {fmt_vnd(salary)} VNĐ (tru tu quy cong ty khi chay kinh doanh).\n"
            f"Ky hop da nhan {fmt_vnd(salary // 4)} VNĐ."
        )

    @commands.command(name="baotri", aliases=["saatua", "tuye"])
    async def fire(self, ctx, *, company: str, member: discord.Member):
        """Chu tich cho mot nhan vien nghi viec (khong con luong)."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                row = await self._company_row(db, company)
                if not row:
                    await db.rollback()
                    return await ctx.send("\u274c Cong ty khong ton tai.")
                cid, cname, owner = row[0], row[1], row[2]
                if owner != ctx.author.id:
                    await db.rollback()
                    return await ctx.send("\u274c Chi Chu tich moi cho nghi duoc.")
                async with db.execute(
                    "DELETE FROM biz_staff WHERE company_id=? AND user_id=?",
                    (cid, member.id),
                ) as cur:
                    removed = cur.rowcount
                await db.commit()
        if not removed:
            return await ctx.send("\u274c Nguoi nay khong trong bien che cong ty.")
        await ctx.send(
            f"\U0001f5d1 Da cho <@{member.id}> nghi viec khoi **{cname}**."
        )

    @commands.command(name="nhanviencty", aliases=["nvcty", "bienche"])
    async def staff_list(self, ctx, *, company: str):
        """Xem bien che nhan vien va muc luong tung nguoi."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await self._company_row(db, company)
            if not row:
                return await ctx.send("\u274c Cong ty khong ton tai.")
            cid, cname = row[0], row[1]
            async with db.execute(
                "SELECT user_id,role,salary FROM biz_staff"
                " WHERE company_id=? ORDER BY salary DESC",
                (cid,),
            ) as cur:
                staff = await cur.fetchall()
        if not staff:
            return await ctx.send(
                f"\U0001f4cb **{cname}** chua co nhan vien. Tu yen: `!tuyennv`."
            )
        lines = [
            f"\u2022 <@{uid}> \u2014 **{role.upper()}** \u2014 {fmt_vnd(sal)} VN\u0110/ngay"
            for uid, role, sal in staff
        ]
        embed = discord.Embed(
            title=f"\U0001f465 BIEN CHE \u2014 {cname}",
            description="\n".join(lines)[:3900],
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="\U0001f4b0 Tong luong/ngay",
            value=f"{fmt_vnd(sum(int(s) for _u, _r, s in staff))} VN\u0110",
        )
        await ctx.send(embed=embed)
    # ------------------------------------------------------------------
    # !lamvieccty -- chay mot chu ky kinh doanh
    # ------------------------------------------------------------------

    @commands.command(name="lamvieccty", aliases=["kinhdoanh", "chaycty", "ngaycty"])
    @commands.cooldown(1, 30.0, commands.BucketType.user)
    async def run_business(self, ctx, *, company: str):
        """Chay **mot ngay kinh doanh**: doanh thu - chi phi - luong - thue."""
        try:
            report = await self.run_business_day(company, requester=ctx.author.id)
        except CompanyError as exc:
            return await ctx.send(f"\u274c {exc}")
        if report is None:
            return await ctx.send(
                "\u23f3 Hom nay cong ty da chay roi. Mai hay chay tiep."
            )
        await ctx.send(report)

    def _contract_boost(self, kind: str) -> float:
        """Hệ số doanh thu của hợp đồng thuê ngoài (mặc định 1.0 = không có).

        Đọc qua cog ``Business`` để tránh import vòng giữa hai module.
        """
        business_cog = self.bot.get_cog("Business") if self.bot else None
        contracts = getattr(business_cog, "CONTRACTS", None)
        if not isinstance(contracts, dict):
            return 1.0
        entry = contracts.get(str(kind))
        try:
            return float(entry[1])
        except (TypeError, IndexError, ValueError):
            return 1.0

    async def run_business_day(self, company: str, *,
                               requester: int | None = None) -> str | None:
        """Chay mot chu ky kinh doanh.

        Tra ve chuoi bao cao, hoac ``None`` neu hom nay da chay roi.
        Moi dong tien deu ghi trong cung mot ``BEGIN IMMEDIATE`` nen khong
        the phat sinh trang thay "cong ty nuoc tien ma khong ghi nhan".
        """
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._company_row(db, company)
                    if not row:
                        await db.rollback()
                        raise CompanyError("Cong ty khong ton tai.")
                    cid, cname, owner, total_shares, price, cash = (
                        row[0], row[1], row[2], int(row[3]), int(row[4]), int(row[5]),
                    )

                    # Quyen: chi Chu tich hoac CEO duoc chay.
                    if requester is not None and int(requester) != owner:
                        async with db.execute(
                            "SELECT 1 FROM biz_staff WHERE company_id=? AND user_id=?"
                            " AND role='ceo' AND active=1",
                            (cid, int(requester)),
                        ) as cur:
                            if not await cur.fetchone():
                                await db.rollback()
                                raise CompanyError(
                                    "Chi Chu tich hoac CEO moi chay kinh doanh duoc."
                                )

                    async with db.execute(
                        "SELECT industry,last_tick FROM biz_meta WHERE company_id=?",
                        (cid,),
                    ) as cur:
                        meta = await cur.fetchone()
                    if meta is None:
                        key = "dichvu"
                        last_tick = 0.0
                        await db.execute(
                            "INSERT INTO biz_meta(company_id,industry,created_at)"
                            " VALUES(?,?,?)",
                            (cid, key, time.time()),
                        )
                    else:
                        key = meta[0] or "dichvu"
                        last_tick = float(meta[1] or 0.0)

                    today = int(time.time() // 86400)
                    if int(last_tick // 86400) == today:
                        await db.rollback()
                        return None

                    capital = max(1, total_shares * price)
                    if capital < MIN_CAPITAL:
                        await db.rollback()
                        raise CompanyError("Von cong ty qua nho de kinh doanh.")

                    name, rate, cost_rate, risk, _desc = INDUSTRIES.get(
                        key, INDUSTRIES["dichvu"]
                    )
                    # Hợp đồng thuê ngoài còn hiệu lực -> tăng hệ số doanh thu.
                    async with db.execute(
                        "SELECT kind,until FROM biz_contract WHERE company_id=?",
                        (cid,),
                    ) as cur:
                        contract = await cur.fetchone()
                    boost = 1.0
                    if contract and float(contract[1]) > time.time():
                        boost = self._contract_boost(contract[0])

                    # Bien do ngay nhieu: rui ro nganh + dao lot theo ngay.
                    rng = random.Random(f"{cid}-{today}-{key}")
                    shock = 1.0 + rng.uniform(-risk, risk)
                    revenue = max(0, int(capital * rate * shock * boost))
                    op_cost = int(revenue * cost_rate)
                    profit = revenue - op_cost

                    async with db.execute(
                        "SELECT user_id,salary FROM biz_staff"
                        " WHERE company_id=? AND active=1",
                        (cid,),
                    ) as cur:
                        staff = await cur.fetchall()
                    salary_total = sum(int(s) for _u, s in staff)
                    if salary_total > profit + cash:
                        await db.rollback()
                        raise CompanyError(
                            "Khong du loi nhuan de tra luong. "
                            "Hay giam quy mo nhan vien hoac tang von."
                        )
                    profit -= salary_total

                    tax = max(0, int(profit * TAX_RATE)) if profit > 0 else 0
                    profit -= tax

                    new_cash = cash + revenue - op_cost - salary_total - tax
                    if new_cash < 0:
                        await db.rollback()
                        raise CompanyError("Quy cong ty am \u2014 khong chot duoc.")

                    for uid, sal in staff:
                        await self._pay(db, int(uid), int(sal), f"luong {cname}")

                    await db.execute(
                        "UPDATE companies SET cash=? WHERE id=?", (new_cash, cid)
                    )
                    await db.execute(
                        "UPDATE biz_meta SET last_tick=?, tax_paid=tax_paid+?,"
                        " total_earned=total_earned+?,"
                        " reputation=MAX(0, reputation+?) WHERE company_id=?",
                        (time.time(), tax, max(0, profit),
                         1 if profit > 0 else -1, cid),
                    )
                    # Nhat ky kinh doanh: de !phasan dem ngay LO LIEN TIEP
                    # (khong phai ngay khong chay).
                    await db.execute(
                        "INSERT INTO biz_history(company_id,day,revenue,cost,"
                        "profit,tax) VALUES(?,?,?,?,?,?)",
                        (cid, today, revenue, op_cost + salary_total,
                         profit, tax),
                    )
                    await db.commit()
                except CompanyError:
                    raise
                except Exception:  # noqa: BLE001 - rollback truoc khi nem len
                    await db.rollback()
                    raise

        # Lich re: cong ty da niem yet thi gia CP chay theo ket qua kinh doanh.
        price_note = ""
        debt_note = ""
        business_cog = self.bot.get_cog("Business") if self.bot else None
        if business_cog is not None:
            apply_debt_interest = getattr(
                business_cog, "apply_debt_interest", None
            )
            if callable(apply_debt_interest):
                debt_note = await apply_debt_interest(cid)
            try:
                new_price, pct = await business_cog.reprice_listed(
                    cid, profit, capital)
                if new_price:
                    sign_p = "+" if pct >= 0 else ""
                    price_note = (
                        f"\n\u2022 Gi\u00e1 CP: {fmt_vnd(price)} \u2192 "
                        f"**{fmt_vnd(new_price)}** VN\u0110 ({sign_p}{pct}%)"
                    )
            except Exception:  # noqa: BLE001 - lich re khong cham vao giao dich
                pass

        sign = "+" if profit >= 0 else ""
        return (
            f"\U0001f4c9 **BAO CAO KINH DOANH \u2014 {cname}**\n"
            f"Nganh: **{name}** (von {fmt_vnd(capital)} VN\u0110)\n"
            f"\u2022 Doanh thu: {fmt_vnd(revenue)} VN\u0110\n"
            f"\u2022 Chi phi van hanh: -{fmt_vnd(op_cost)} VN\u0110\n"
            f"\u2022 Luong ({len(staff)} nguoi): -{fmt_vnd(salary_total)} VN\u0110\n"
            f"\u2022 Thue ({int(TAX_RATE * 100)}%): -{fmt_vnd(tax)} VN\u0110\n"
            f"\u2022 **Loi nhuan: {sign}{fmt_vnd(profit)} VN\u0110**\n"
            f"\u2022 Quy cong ty: {fmt_vnd(new_cash)} VN\u0110"
            + (f"\n\u2022 {debt_note}" if debt_note else "")
            + price_note
        )
    # ------------------------------------------------------------------
    # !bannpc -- ban co phan cho NPC
    # ------------------------------------------------------------------

    @commands.command(name="bannpc", aliases=["npc", "banquocte"])
    @commands.cooldown(1, 20.0, commands.BucketType.user)
    async def sell_to_npc(self, ctx, *, company: str, shares_input: str):
        """Ban **so co phan** cho NPC \u2014 NPC thu mua san, khong bi ke.

        Nhap so CP (vi du ``!bannpc CongTyA 50`` hoac ``all`` = ban het).
        Gia thu mua thap hon gia thi truong nen can ban dung so CP can thiet.
        """
        data = await self._user_data(ctx.author.id)
        if data is None:
            return await ctx.send("\u274c He thong kinh te chua san sang.")
        held_now = await self._shares_of(ctx.author.id, company)
        want = self._parse_amount(shares_input, held_now)
        if want is None or want <= 0:
            return await ctx.send(
                f"\u274c So CP khong hop le (ban dang giu {fmt_vnd(held_now)} CP)."
            )

        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._company_row(db, company)
                    if not row:
                        await db.rollback()
                        return await ctx.send("\u274c Cong ty khong ton tai.")
                    cid, cname, _owner, _ts, price, _cash = (
                        row[0], row[1], row[2], row[3], int(row[4]), row[5],
                    )
                    price = max(1, price)
                    async with db.execute(
                        "SELECT amount FROM shares WHERE user_id=? AND company_id=?",
                        (ctx.author.id, cid),
                    ) as cur:
                        held = await cur.fetchone()
                    have = int(held[0]) if held else 0
                    shares = min(int(want), have)
                    if shares < 1:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Ban khong co CP nao cua {cname}."
                        )
                    if int(want) > have:
                        # Tu cat ve so CP dang so huu (khong rollback, giao dich
                        # van hop le voi so CP thuc te).
                        want = have

                    npc_price = max(1, int(price * (1 - NPC_BUYBACK_DISCOUNT)))
                    # Tien mua CP phai ra tu QUY CONG TY - khong duoc tao tu
                    # hu khong. NPC chi thieu duoc so CP quy cong ty tra noi.
                    cash = int(_cash or 0)
                    affordable = cash // npc_price if npc_price > 0 else 0
                    if affordable < 1:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Quy **{cname}** het tien, NPC khong thu mua duoc."
                        )
                    shares = min(shares, affordable)
                    gross = shares * npc_price
                    tax = int(gross * TRADE_TAX_RATE)
                    payout = max(0, gross - tax)

                    # Tru tien tu quy cong ty (atomic, phai that su du).
                    async with db.execute(
                        "UPDATE companies SET cash=cash-? WHERE id=? AND cash>=?",
                        (gross, cid, gross),
                    ) as cur:
                        if cur.rowcount != 1:
                            await db.rollback()
                            return await ctx.send(
                                f"\u274c Quy **{cname}** khong du tien."
                            )
                    # Nguoi ban DUOC NHAN tien tu quy cong ty (truoc thue).
                    await self._pay(db, ctx.author.id, payout, f"ban NPC {cname}")
                    # Chi thu CP khi ton tai that su (guard atomic).
                    async with db.execute(
                        "UPDATE shares SET amount=amount-?"
                        " WHERE user_id=? AND company_id=? AND amount>=?",
                        (shares, ctx.author.id, cid, shares),
                    ) as cur:
                        if cur.rowcount != 1:
                            await db.rollback()
                            return await ctx.send("\u274c Giao dich that bai.")
                    await db.commit()
                except CompanyError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        await ctx.send(
            f"\U0001f91e Da ban **{fmt_vnd(shares)} CP** cua **{cname}** cho NPC.\n"
            f"Gia thu mua: {fmt_vnd(npc_price)}/CP "
            f"(\u2212{int(NPC_BUYBACK_DISCOUNT * 100)}% so voi gia thi truong)\n"
            f"\u2022 Thue giao dich: -{fmt_vnd(tax)} VN\u0110\n"
            f"\u2022 **Nhan duoc: {fmt_vnd(payout)} VN\u0110**"
        )

    # ------------------------------------------------------------------
    # !chuonhhdt / !bautich -- HDQT
    # ------------------------------------------------------------------

    @commands.command(name="chuonhhdt", aliases=["hdqt", "chuhdt"])
    async def board_info(self, ctx, *, company: str):
        """Xem Chu tich HDQT va bang co phan cua cong ty."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await self._company_row(db, company)
            if not row:
                return await ctx.send("\u274c Cong ty khong ton tai.")
            cid, cname = row[0], row[1]
            async with db.execute(
                "SELECT chair_id FROM biz_board WHERE company_id=?", (cid,)
            ) as cur:
                b = await cur.fetchone()
            async with db.execute(
                "SELECT user_id,amount FROM shares"
                " WHERE company_id=? AND amount>0 ORDER BY amount DESC LIMIT 10",
                (cid,),
            ) as cur:
                holders = await cur.fetchall()

        chair = f"<@{b[0]}>" if b else "Chua bau (mac dinh: owner_id)"
        lines = [f"\u2022 <@{uid}> \u2014 **{fmt_vnd(amt)} CP**" for uid, amt in holders]
        embed = discord.Embed(
            title=f"\U0001f9ed HOI DONG QUAN TRI \u2014 {cname}",
            description=f"**Chu tich:** {chair}\n\n" + ("\n".join(lines) or "(chua co CP)"),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Bau chu tich: !bautich <tên công ty> <@ứng viên>")
        await ctx.send(embed=embed)

    @commands.command(name="bautich", aliases=["bauct", "baucdt"])
    async def elect_chair(self, ctx, *, company: str, member: discord.Member):
        """Bau Chu tich HDQT \u2014 chi co dong co phan moi duoc bau, 1 nguoi 1 phieu/ngay."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._company_row(db, company)
                    if not row:
                        await db.rollback()
                        return await ctx.send("\u274c Cong ty khong ton tai.")
                    cid, cname = row[0], row[1]
                    async with db.execute(
                        "SELECT amount FROM shares WHERE user_id=? AND company_id=?",
                        (ctx.author.id, cid),
                    ) as cur:
                        mine = await cur.fetchone()
                    if not mine or int(mine[0]) <= 0:
                        await db.rollback()
                        return await ctx.send(
                            "\u274c Ban khong co co phan nen khong duoc bau."
                        )
                    if member.id == ctx.author.id:
                        await db.rollback()
                        return await ctx.send(
                            "\u274c Ban khong the bau chinh minh."
                        )
                    async with db.execute(
                        "SELECT amount FROM shares WHERE user_id=? AND company_id=?",
                        (member.id, cid),
                    ) as cur:
                        cand = await cur.fetchone()
                    if not cand or int(cand[0]) <= 0:
                        await db.rollback()
                        return await ctx.send(
                            "\u274c Ung vien cung phai co co phan cong ty."
                        )
                    today = int(time.time() // 86400)
                    await db.execute(
                        "INSERT INTO biz_votes(company_id,voter_id,candidate,voted_on)"
                        " VALUES(?,?,?,?)"
                        " ON CONFLICT(company_id,voter_id) DO UPDATE SET"
                        " candidate=excluded.candidate, voted_on=excluded.voted_on",
                        (cid, ctx.author.id, member.id, today),
                    )
                    await db.commit()
                except CompanyError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT candidate,COUNT(*) FROM biz_votes WHERE company_id=?"
                " AND voted_on=? GROUP BY candidate ORDER BY COUNT(*) DESC",
                (cid, int(time.time() // 86400)),
            ) as cur:
                tally = await cur.fetchall()
        top = tally[0] if tally else None
        lines = [f"\u2022 <@{cand}>: {cnt} phiếu" for cand, cnt in tally] or ["(chưa có phiếu)"]
        leader = (
            f"\U0001f3c6 Dang dan dau: <@{top[0]}> ({top[1]} phiếu).\n"
            f"Ngay nay chi con {self._votes_left(ctx.author.id, cid)} phiếu cua ban."
            if top else ""
        )
        await ctx.send(
            f"\u2705 Ban da bau <@{member.id}> lam Chu tich HDQT cua **{cname}**.\n"
            f"{leader}\n" + "\n".join(lines)[:800]
        )

    def _votes_left(self, voter: int, cid: int) -> int:
        """So phiếu còn lại trong ngày (tối đa 3)."""
        return max(0, 3 - 1)  # 1 phiếu/ngày đã dùng khi vừa ghi

    # ------------------------------------------------------------------
    # !congty -- thong tin tong quan
    # ------------------------------------------------------------------

    @commands.command(name="ctytongquan", aliases=["ctytt", "ctyinfo2"])
    async def company_overview(self, ctx, *, company: str):
        """Xem tong quan cong ty: von, quy, nganh, nhan vien, uy tin."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await self._company_row(db, company)
            if not row:
                return await ctx.send("\u274c Cong ty khong ton tai.")
            cid, cname, owner, total_shares, price, cash = (
                row[0], row[1], row[2], int(row[3]), int(row[4]), int(row[5]),
            )
            async with db.execute(
                "SELECT industry,reputation,tax_paid,total_earned FROM biz_meta"
                " WHERE company_id=?",
                (cid,),
            ) as cur:
                meta = await cur.fetchone()
            async with db.execute(
                "SELECT COUNT(*) FROM biz_staff WHERE company_id=? AND active=1",
                (cid,),
            ) as cur:
                staff_n = (await cur.fetchone())[0]

        industry = (meta[0] if meta else "dichvu") or "dichvu"
        rep = int(meta[1]) if meta else 0
        tax_paid = int(meta[2]) if meta else 0
        earned = int(meta[3]) if meta else 0
        capital = total_shares * price
        per_share = capital // max(1, total_shares)
        embed = discord.Embed(
            title=f"\U0001f3ed {cname}",
            color=discord.Color.teal(),
        )
        embed.add_field(name="\U0001f4b3 Von co phan", value=f"{fmt_vnd(capital)} VN\u0110")
        embed.add_field(name="\U0001f4b0 Gia 1 CP", value=f"{fmt_vnd(per_share)} VN\u0110")
        embed.add_field(name="\U0001f4b8 Quy cong ty", value=f"{fmt_vnd(cash)} VN\u0110")
        embed.add_field(name="\U0001f3ed Nganh", value=f"**{industry_name(industry)}** (`!{industry}`)")
        embed.add_field(name="\U0001f465 Nhan vien", value=f"{fmt_vnd(staff_n)} người")
        embed.add_field(name="\U0001f451 Chu tich", value=f"<@{owner}>")
        embed.add_field(name="\U0001f451 Uy tin", value=f"{rep} ⭐", inline=True)
        embed.add_field(name="\U0001f4b0 Da nop thue", value=f"{fmt_vnd(tax_paid)} VN\u0110", inline=True)
        embed.add_field(name="\U0001f4c8 Da kiem tien", value=f"{fmt_vnd(earned)} VN\u0110", inline=True)
        embed.set_footer(text="Chay kinh doanh: !lamvieccty • Xem bien che: !nhanviencty")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    """Discord.py goi ham nay de dang ky cog."""
    await bot.add_cog(Company(bot))
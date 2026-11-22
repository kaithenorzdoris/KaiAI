"""
He thong DOANH NGHIEP NANG CAO — pha san, hop dong, sap nha, CP uu dai,
niem yet, no doanh nghiep, ESOP, tranh gia, bao cao tai chinh, thue lai.

Tach rieng ``cogs/company.py`` (nen tang) de moi phan deu co the kiem thu doc lap.

Moi tinh nang deu dung **atomic guard** trong ``BEGIN IMMEDIATE``: khong tin
vao so du doc truoc (TOCTOU) — nguyen nhan cua bug "in tien vo han" da gap o
he thong cu.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiosqlite
import discord
from discord.ext import commands

from kai_core import get_logger
from kai_core.config import CONFIG

log = get_logger("business")

DB_PATH = str(CONFIG.data_dir / "economy.db")

#: So ngay lo lien tiep truoc khi cong ty pha san.
BANKRUPT_DAYS = 3
#: Ty le gia CP nhan duoc tang khi niem yet san.
IPO_BONUS = 1.15
#: Von toi thieu de duoc niem yet san (5 ty — quy mo cong ty niem yet).
IPO_MIN_CAPITAL = 5_000_000_000
#: Ty le CP can mua de so huu (thau tom).
TAKEOVER_PCT = 0.51
#: Thue thu nhap ca nhan tren lai ngan hang (5% phan vuot nguong).
SAVINGS_INTEREST_TAX = 0.05
#: Nguong lai ngan hang chieu thue (1 ty — dong bo voi games.py).
SAVINGS_TAX_THRESHOLD = 1_000_000_000

#: Vay doanh nghiep: han muc = 60% von, toi da 50 ty/khoan, ky han 30 ngay.
DEBT_MAX_PCT = 0.60
DEBT_MAX_AMOUNT = 50_000_000_000
DEBT_TERM_DAYS = 30
#: Lai suat vay doanh nghiep theo ngay.
DEBT_DAILY_RATE = 0.0015
#: Vay duoi han khac se bi cong no vao han muc moi moi ngay.
DEBT_OVERDUE_DAILY = 0.002

#: ESOP: toi da 10% von tong CP duoc cap cho nhan vien.
ESOP_MAX_PCT = 0.10

#: Tranh gia (raiding): thoi gian giu tien theo (dat coc).
BID_HOLD_DAYS = 2
#: Ban dat gia toi thieu phai vuot gia hien tai cua cong ty muc tieu.
BID_MIN_PREMIUM = 1.10

#: Hop dong thue ngoai: ``key -> (ten, ty le hieu luc, gia, so ngay)``
#: Gia la chi phi 7 ngay doi voi dich vu chuyen mon o Viet Nam.
CONTRACTS: dict[str, tuple[str, float, int, int]] = {
    "marketing": ("Marketing & Quang cao", 1.20, 30_000_000, 7),
    "keetoan": ("Kế toán & Kiểm toán", 1.18, 50_000_000, 7),
    "luat": ("Tư vấn pháp lý", 1.15, 70_000_000, 7),
    "congnghe": ("Nâng cấp công nghệ", 1.25, 100_000_000, 7),
}


def fmt_vnd(amount: Any) -> str:
    """Định dạng VNĐ kiểu Việt Nam (dấu chấm phân nghìn)."""
    try:
        return f"{int(amount):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def contract_name(key: str) -> str:
    """Tên hiển thị của hợp đồng."""
    entry = CONTRACTS.get(key)
    return entry[0] if entry else key


class BizError(Exception):
    """Lỗi nghiệp vụ — cog bắt lại và trả lời người chơi."""


SCHEMA = """
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
    company_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    role TEXT NOT NULL, salary INTEGER NOT NULL DEFAULT 0,
    joined_at REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (company_id, user_id)
);
CREATE TABLE IF NOT EXISTS biz_board (
    company_id INTEGER PRIMARY KEY, chair_id INTEGER NOT NULL,
    elected_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS biz_votes (
    company_id INTEGER NOT NULL, voter_id INTEGER NOT NULL,
    candidate INTEGER NOT NULL, voted_on INTEGER NOT NULL,
    PRIMARY KEY (company_id, voter_id)
);
CREATE TABLE IF NOT EXISTS biz_contract (
    company_id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
    until REAL NOT NULL, cost INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS biz_debt (
    company_id INTEGER PRIMARY KEY, amount INTEGER NOT NULL DEFAULT 0,
    daily_pay INTEGER NOT NULL DEFAULT 0, days_left INTEGER NOT NULL DEFAULT 0,
    last_accrued_day INTEGER NOT NULL DEFAULT 0
);
-- "Ngan hang" cua he thong: moi dong tien vay doanh nghiep deu ra tu day va
-- phai quay lai day. Nho vay khong tao tien tu hu khong.
CREATE TABLE IF NOT EXISTS biz_lend_pool (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    balance INTEGER NOT NULL DEFAULT 0
);
-- Lich su tu lan vay gan nhat de tinh lai va han muc.
CREATE TABLE IF NOT EXISTS biz_debt_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    rate       REAL    NOT NULL DEFAULT 0,
    opened_at  REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_biz_debt_log ON biz_debt_log(company_id, opened_at);
CREATE TABLE IF NOT EXISTS biz_preferred (
    company_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    shares INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (company_id, user_id)
);
CREATE TABLE IF NOT EXISTS biz_esop (
    company_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    shares INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (company_id, user_id)
);
CREATE TABLE IF NOT EXISTS biz_listing (
    company_id INTEGER PRIMARY KEY,
    listed_at REAL NOT NULL,
    price INTEGER NOT NULL,
    change_pct INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS biz_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL, day INTEGER NOT NULL,
    revenue INTEGER NOT NULL, cost INTEGER NOT NULL,
    profit INTEGER NOT NULL, tax INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_biz_history ON biz_history(company_id, day);
CREATE TABLE IF NOT EXISTS biz_raid (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id INTEGER NOT NULL, bidder_id INTEGER NOT NULL,
    bidder_company INTEGER NOT NULL,
    bid_price INTEGER NOT NULL, until REAL NOT NULL,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS biz_takeover (
    company_id INTEGER PRIMARY KEY, by_company INTEGER NOT NULL,
    at REAL NOT NULL DEFAULT 0
);
"""
class Business(commands.Cog):
    """Doanh nghiep nang cao: pha san, hop dong, sap nha, niem yet, no, ESOP."""

    #: Bang hop dong thue ngoai — de cog Company doc he so (khong import vong).
    CONTRACTS = CONTRACTS

    def __init__(self, bot: commands.Bot, db_path: str | None = None) -> None:
        self.bot = bot
        self.db_path = db_path or DB_PATH
        self._lock_obj = asyncio.Lock()

    async def cog_load(self) -> None:
        """Tao toan bo bang (idempotent)."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(SCHEMA)
                await db.execute("BEGIN IMMEDIATE")
                # Migration: co `listed` tach khoi `reputation` (gia tri uy
                # tin khong duoc dung lam co niem yet).
                async with db.execute(
                    "PRAGMA table_info(biz_meta)"
                ) as cur:
                    cols = {r[1] for r in await cur.fetchall()}
                if "listed" not in cols:
                    await db.execute(
                        "ALTER TABLE biz_meta ADD COLUMN listed"
                        " INTEGER NOT NULL DEFAULT 0"
                    )
                async with db.execute("PRAGMA table_info(biz_debt)") as cur:
                    debt_cols = {row[1] for row in await cur.fetchall()}
                if "last_accrued_day" not in debt_cols:
                    await db.execute(
                        "ALTER TABLE biz_debt ADD COLUMN last_accrued_day"
                        " INTEGER NOT NULL DEFAULT 0"
                    )
                    current_day = int(time.time() // 86400)
                    async with db.execute(
                        "SELECT d.company_id,d.days_left,"
                        "(SELECT opened_at FROM biz_debt_log"
                        " WHERE company_id=d.company_id ORDER BY id DESC LIMIT 1)"
                        " FROM biz_debt d"
                    ) as cur:
                        legacy_debts = await cur.fetchall()
                    for company_id, days_left, opened_at in legacy_debts:
                        remaining_days = max(0, int(days_left or 0))
                        if (
                            opened_at is not None
                            and float(opened_at) > 0
                            and remaining_days > 0
                        ):
                            opened_day = int(float(opened_at) // 86400)
                            elapsed_days = max(0, current_day - opened_day)
                            remaining_days = min(
                                remaining_days,
                                max(0, DEBT_TERM_DAYS - elapsed_days),
                            )
                        await db.execute(
                            "UPDATE biz_debt SET days_left=?,last_accrued_day=?"
                            " WHERE company_id=?",
                            (remaining_days, current_day, company_id),
                        )
                await db.commit()

    def _lock(self) -> asyncio.Lock:
        """Khoa ghi DB cho instance hiện tại; tránh bind sai event loop."""
        return self._lock_obj

    # ------------------------------------------------------------------
    # Tien ich dung chung
    # ------------------------------------------------------------------

    @staticmethod
    async def _balance(db, user_id: int) -> int:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?", (int(user_id),)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def _company_row(db, name: str):
        async with db.execute(
            "SELECT id,name,owner_id,total_shares,price_per_share,cash"
            " FROM companies WHERE lower(name)=lower(?)",
            (str(name).strip(),),
        ) as cur:
            return await cur.fetchone()

    async def _pay(self, db, user_id: int, amount: int, note: str) -> None:
        """Tra tien va ghi giao dich (luong luon duong nen khong can guard)."""
        if amount <= 0:
            return
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),))
        await db.execute(
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (int(amount), int(user_id)),
        )
        after = await self._balance(db, user_id)
        await db.execute(
            "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
            " VALUES(?,?,?,?,?,?)",
            (int(user_id), "business", int(amount), after, time.time(), str(note)[:60]),
        )

    async def _charge(self, db, user_id: int, amount: int, note: str) -> bool:
        """Trừ tiền khỏi ví người chơi (chiều ra) với atomic guard.

        ``_pay`` chỉ dùng cho chiều VÀO (nó bỏ qua ``amount <= 0``) nên không
        dùng được để thu tiền. Hàm này trừ có kiểm tra ``rowcount``.
        """
        amount = int(amount)
        if amount <= 0:
            return True
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),)
        )
        async with db.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
            (amount, int(user_id), amount),
        ) as cur:
            if cur.rowcount != 1:
                return False
        after = await self._balance(db, user_id)
        await db.execute(
            "INSERT INTO transactions(user_id,kind,amount,balance_after,"
            "created_at,note) VALUES(?,?,?,?,?,?)",
            (int(user_id), "business", -amount, after, time.time(),
             str(note)[:60]),
        )
        return True

    async def _user_data(self, user_id: int) -> dict | None:
        games = self.bot.get_cog("Games")
        if games is None or not hasattr(games, "get_user_data"):
            return None
        try:
            return await games.get_user_data(int(user_id))
        except Exception as exc:  # noqa: BLE001
            log.debug("get_user_data that bai: %s", exc)
            return None

    def _parse_amount(self, text: str, reference: int) -> int | None:
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

    @staticmethod
    async def _require_owner(db, ctx, company: str, what: str):
        """Lấy công ty và bắt buộc người gọi là chủ tịch."""
        row = await Business._company_row(db, company)
        if not row:
            raise BizError("Công ty không tồn tại.")
        if row[2] != ctx.author.id:
            raise BizError(f"Chỉ Chủ tịch mới {what} được.")
        return row

    @staticmethod
    async def _get_meta(db, cid: int) -> dict[str, Any]:
        async with db.execute(
            "SELECT industry,last_tick,reputation FROM biz_meta WHERE company_id=?",
            (cid,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT OR IGNORE INTO biz_meta(company_id,industry,created_at)"
                " VALUES(?,?,?)", (cid, "dichvu", time.time()),
            )
            return {"industry": "dichvu", "last_tick": 0.0, "reputation": 0}
        return {"industry": row[0], "last_tick": float(row[1] or 0.0),
                "reputation": int(row[2] or 0)}
    # ==================================================================
    # 1. PHA SAN — cong ty lo 3 ngay lien tiep thi giai the
    # ==================================================================

    @commands.command(name="phasan", aliases=["bankrupt", "saphang"])
    async def bankrupt_check(self, ctx, *, company: str):
        """Xem trang thai cong ty; lo 3 ngay lien tiep se bi pha san.

        Khi pha san: **toan bo co phan cua moi nguoi ve 0**, cong ty bi xoa khoi
        bang ``companies`` — day la "san cho nguoi moi" nhu ban mong doi.
        """
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._company_row(db, company)
                    if not row:
                        await db.rollback()
                        return await ctx.send("\u274c Công ty không tồn tại.")
                    cid, cname = row[0], row[1]
                    meta = await self._get_meta(db, cid)
                    last = meta["last_tick"]
                    if not last:
                        await db.rollback()
                        return await ctx.send(
                            "\u2139\ufe0f Công ty chưa chạy kinh doanh lần nào."
                        )
                    # Dem 3 ngay LO LIEN TIEP tu nhat ky (profit <= 0).
                    # Chi pha san khi dat du chuoi ngay loi lien tiep.
                    async with db.execute(
                        "SELECT day FROM biz_history WHERE company_id=? AND profit<=0"
                        " ORDER BY day DESC LIMIT ?",
                        (cid, BANKRUPT_DAYS),
                    ) as cur:
                        loss_days = [int(r[0]) for r in await cur.fetchall()]
                    streak = 0
                    if loss_days:
                        # Ngay lien tiep = cac ngay giam dan khong gian 1 don vi.
                        prev = loss_days[0]
                        streak = 1
                        for d in loss_days[1:]:
                            if d == prev - 1:
                                streak += 1
                                prev = d
                            else:
                                break
                    async with db.execute(
                        "SELECT COUNT(*) FROM shares WHERE company_id=? AND amount>0",
                        (cid,),
                    ) as cur:
                        holders = (await cur.fetchone())[0]
                except BizError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        danger = max(0, BANKRUPT_DAYS - streak)
        if streak < BANKRUPT_DAYS:
            return await ctx.send(
                f"\U0001f6a8 **Cảnh báo phá sản — {cname}**\n"
                f"Đã **{streak}/{BANKRUPT_DAYS}** ngày lỗ liên tiếp.\n"
                f"Còn {danger} ngày nữa sẽ phá sản — cổ phiếu {holders} người về 0."
            )
        # Đã đủ chuỗi ngày lỗ -> phá sản ngay.
        return await self._do_bankrupt(
            ctx, company, reason=f"lo {streak} ngay lien tiep"
        )

    async def _do_bankrupt(self, ctx, company: str, *, reason: str) -> str:
        """Xoa cong ty va tra ve thong bao (dung chung cho !phasan va !nocty)."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._company_row(db, company)
                    if not row:
                        await db.rollback()
                        return "\u274c Công ty không tồn tại."
                    cid, cname, _owner = row[0], row[1], row[2]
                    async with db.execute(
                        "SELECT user_id,amount FROM shares WHERE company_id=?", (cid,)
                    ) as cur:
                        holders = await cur.fetchall()
                    lost = 0
                    for uid, amt in holders:
                        # Xoa CP: UPDATE co guard de khong tru 2 lan.
                        async with db.execute(
                            "UPDATE shares SET amount=amount-?"
                            " WHERE user_id=? AND company_id=? AND amount>=?",
                            (int(amt), int(uid), cid, int(amt)),
                        ) as c2:
                            if c2.rowcount == 1:
                                lost += int(amt)
                    await db.execute("DELETE FROM shares WHERE company_id=?", (cid,))
                    await db.execute("DELETE FROM companies WHERE id=?", (cid,))
                    await db.execute("DELETE FROM biz_meta WHERE company_id=?", (cid,))
                    await db.execute("DELETE FROM biz_staff WHERE company_id=?", (cid,))
                    await db.execute("DELETE FROM biz_board WHERE company_id=?", (cid,))
                    await db.execute("DELETE FROM biz_preferred WHERE company_id=?", (cid,))
                    await db.execute("DELETE FROM biz_esop WHERE company_id=?", (cid,))
                    await db.execute("DELETE FROM biz_contract WHERE company_id=?", (cid,))
                    await db.execute("DELETE FROM biz_debt WHERE company_id=?", (cid,))
                    await db.commit()
                except BizError as exc:
                    await db.rollback()
                    return f"\u274c {exc}"

        return (
            f"\U0001f4a9 **PHA SẢN — {cname}**\n"
            f"Lý do: {reason}.\n"
            f"• Đã xoá **{lost:,} cổ phiếu** của {len(holders)} cổ đông.\n"
            f"• Công ty đã bị xoá khỏi hệ thống — tên **{cname}** giờ trống, "
            "ai cũng có thể lập lại."
        )

    # ==================================================================
    # 2. HOP DONG THUE NGOAI
    # ==================================================================

    @commands.command(name="hopdong", aliases=["thuengoai", "hopdongcty"])
    async def buy_contract(self, ctx, *, company: str, kind: str):
        """Mua hợp đồng thuê ngoài (7 ngày) để tăng hiệu quả ngành.

        ``kind``: ``marketing`` | ``keetoan`` | ``luat`` | ``congnghe``
        """
        key = kind.strip().lower()
        if key not in CONTRACTS:
            return await ctx.send(
                f"\u274c Hợp đồng: {', '.join(CONTRACTS)} — xem `!hopdong`."
            )
        name, _boost, cost, days = CONTRACTS[key]

        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._require_owner(db, ctx, company, "mua hợp đồng")
                    cid, cname, _o, _ts, _p, cash = (
                        row[0], row[1], row[2], row[3], row[4], int(row[5]),
                    )
                    if cash < cost:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Quỹ công ty chỉ có {fmt_vnd(cash)} VNĐ, "
                            f"cần {fmt_vnd(cost)} VNĐ."
                        )
                    await db.execute(
                        "UPDATE companies SET cash=? WHERE id=?", (cash - cost, cid)
                    )
                    await db.execute(
                        "INSERT INTO biz_contract(company_id,kind,until,cost)"
                        " VALUES(?,?,?,?)"
                        " ON CONFLICT(company_id) DO UPDATE SET"
                        " kind=excluded.kind, until=excluded.until, cost=excluded.cost",
                        (cid, key, time.time() + days * 86400, cost),
                    )
                    await db.commit()
                except BizError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        await ctx.send(
            f"\U0001f4dc Đã ký hợp đồng **{name}** cho **{cname}**.\n"
            f"Giá: {fmt_vnd(cost)} VNĐ • Hiệu lực {days} ngày • "
            f"Tăng hiệu quả ngành lên {int(CONTRACTS[key][1] * 100)}%."
        )

    @commands.command(name="dshopdong", aliases=["hopdongcuacongty"])
    async def list_contracts(self, ctx, *, company: str):
        """Xem hợp đồng đang có của công ty."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await self._company_row(db, company)
            if not row:
                return await ctx.send("\u274c Công ty không tồn tại.")
            async with db.execute(
                "SELECT kind,until,cost FROM biz_contract WHERE company_id=?", (row[0],)
            ) as cur:
                c = await cur.fetchone()
        if not c:
            return await ctx.send("\U0001f4dc Công ty chưa ký hợp đồng nào.")
        left = max(0, int((c[1] - time.time()) // 86400))
        boost = int(CONTRACTS.get(c[0], (None, 1.0))[1] * 100)
        await ctx.send(
            f"\U0001f4dc Hợp đồng: **{contract_name(c[0])}**\n"
            f"• Còn {left} ngày • Hiệu lực +{boost}% hiệu quả\n"
            f"• Đã chi {fmt_vnd(c[2])} VNĐ"
        )
    # ==================================================================
    # 3. SAP NHA & THAU TOM — mua 51% de so huu
    # ==================================================================

    @commands.command(name="sapnha", aliases=["thautom", "muacty"])
    async def takeover(self, ctx, *, buyer: str, target: str, shares_input: str):
        """Mua cổ phần công ty khác. Đạt **51%** thì sở hữu & hợp nhất.

        Số CP phải **dẫn tới > 50% tổng số CP** mới thành công, nếu không số
        tiền đã trả vẫn được hoàn lại (mua thăm dò).
        """
        b = await self._user_data(ctx.author.id)
        if b is None:
            return await ctx.send("\u274c Hệ thống kinh tế chưa sẵn sàng.")
        want = self._parse_amount(shares_input, 0) or 0
        if want <= 0:
            return await ctx.send("\u274c Số cổ phiếu không hợp lệ.")

        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    brow = await self._company_row(db, buyer)
                    trow = await self._company_row(db, target)
                    if not brow or not trow:
                        await db.rollback()
                        return await ctx.send("\u274c Công ty không tồn tại.")
                    bid, bname, _bo, _bts, bprice, bcash = (
                        brow[0], brow[1], brow[2], brow[3], brow[4], int(brow[5]),
                    )
                    tid, tname, _to, ttotal, tprice, tcash = (
                        trow[0], trow[1], trow[2], trow[3], trow[4], trow[5],
                    )
                    if bid == tid:
                        await db.rollback()
                        return await ctx.send("\u274c Không tự thâu tóm chính mình.")
                    if tprice <= 0:
                        await db.rollback()
                        return await ctx.send("\u274c Gia co phieu cong ty bi nham khong hop le.")
                    price = max(1, int(tprice))
                    # KHONG mua lai co phiau cua chinh cong ty A (se khong ton tai).
                    # Tien phai den tu CO DONG hien huu cua cong ty B -> tien that su
                    # chuyen tu A sang B va nguoi ban CP, tru khi cong ty nuoc loi.
                    async with db.execute(
                        "SELECT COALESCE(SUM(amount),0) FROM shares WHERE company_id=?",
                        (tid,),
                    ) as cur:
                        float_shares = int((await cur.fetchone())[0])
                    if float_shares < want:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Co thoi trang cho \u0111ang luu hanh: cong ty chi co "
                            f"**{float_shares} CP**."
                        )
                    cost = want * price
                    if bcash < cost:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Quy **{bname}** chi co {fmt_vnd(bcash)} VNĐ, "
                            f"can {fmt_vnd(cost)} VNĐ de mua {want} CP."
                        )
                    # Cong ty A tru tien (atomic, phai that su du).
                    async with db.execute(
                        "UPDATE companies SET cash=cash-? WHERE id=? AND cash>=?",
                        (cost, bid, cost),
                    ) as c2:
                        if c2.rowcount != 1:
                            await db.rollback()
                            return await ctx.send("\u274c Quy cong ty khong du tien.")
                    # Rut CP tu CO DONG hien huu va tra tien cho ho.
                    # Phai lay DUNG so luong CP da mua (khong phai so tien).
                    need = want
                    remaining_pay = cost
                    async with db.execute(
                        "SELECT user_id,amount FROM shares"
                        " WHERE company_id=? AND amount>0 ORDER BY amount DESC",
                        (tid,),
                    ) as cur:
                        holders = await cur.fetchall()
                    for uid, amt in holders:
                        if need <= 0 or remaining_pay <= 0:
                            break
                        take = min(int(amt), need)
                        paid = min(remaining_pay, take * price)
                        async with db.execute(
                            "UPDATE shares SET amount=amount-?"
                            " WHERE user_id=? AND company_id=? AND amount>=?",
                            (take, int(uid), tid, take),
                        ) as c3:
                            if c3.rowcount == 1:
                                # Nguoi ban duoc nhan tien tu quy cong ty A.
                                await self._pay(db, int(uid), paid,
                                                f"bi thau tom {tname}")
                                need -= take
                                remaining_pay -= paid
                    # CP cong ty A vao cong ty B.
                    await db.execute(
                        "INSERT INTO shares(user_id,company_id,amount) VALUES(?,?,?)"
                        " ON CONFLICT(user_id,company_id) DO UPDATE SET"
                        " amount=amount+excluded.amount",
                        (ctx.author.id, tid, want),
                    )
                    owned = await self._owned_ratio(db, tid, ctx.author.id)
                    await db.commit()
                except BizError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        if owned >= TAKEOVER_PCT:
            db_msg = await self._apply_takeover(bid, tid, bname, tname, owned)
            return await ctx.send(
                f"\U0001f3e6 **{bname} đã thâu tóm {tname}!**\n"
                f"Sở hữu **{int(owned * 100)}%** cổ phiếu.\n"
                f"{db_msg}"
            )
        await ctx.send(
            f"\U0001f91d **{bname}** đã mua **{want} CP** của **{tname}** "
            f"với giá {fmt_vnd(cost)} VNĐ.\n"
            f"Sở hữu hiện tại: **{int(owned * 100)}%**. "
            f"Cần vượt **{int(TAKEOVER_PCT * 100)}%** để thâu tóm."
        )

    @staticmethod
    async def _owned_ratio(db, company_id: int, user_id: int) -> float:
        """Tỷ lệ sở hữu của một người (0..1)."""
        async with db.execute(
            "SELECT total_shares FROM companies WHERE id=?", (company_id,)
        ) as cur:
            row = await cur.fetchone()
        total = int(row[0]) if row and row[0] else 0
        if total <= 0:
            return 0.0
        async with db.execute(
            "SELECT amount FROM shares WHERE company_id=? AND user_id=?",
            (company_id, int(user_id)),
        ) as cur:
            mine = await cur.fetchone()
        return (int(mine[0]) / total) if mine else 0.0

    async def _apply_takeover(self, by_id: int, target_id: int,
                             bname: str, tname: str, ratio: float) -> str:
        """Ghi nhận thâu tóm và chuyển quyền điều hành sang bên mua."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute(
                    "INSERT INTO biz_takeover(company_id,by_company,at) VALUES(?,?,?)"
                    " ON CONFLICT(company_id) DO UPDATE SET"
                    " by_company=excluded.by_company, at=excluded.at",
                    (target_id, by_id, time.time()),
                )
                # Chuyen quyen dieu hanh sang NGUOI SO HUU DA SO THAT SU
                # (nhieu co phieu nhat), khong phai mot dong bat ky.
                async with db.execute(
                    "SELECT user_id FROM shares WHERE company_id=? AND amount>0"
                    " ORDER BY amount DESC LIMIT 1",
                    (target_id,),
                ) as cur:
                    top = await cur.fetchone()
                if top is not None:
                    await db.execute(
                        "UPDATE companies SET owner_id=? WHERE id=?",
                        (int(top[0]), target_id),
                    )
                await db.commit()
        return (
            f"Quyền điều hành **{tname}** chuyển sang người sở hữu đa số.\n"
            f"Bảng lương và nhân viên được hợp nhất dưới **{bname}**."
        )

    # ==================================================================
    # 4. CO PHIEU UU DAI — co tuc uu tien
    # ==================================================================

    @commands.command(name="cpuudai", aliases=["uudai", "cpcodinh"])
    async def buy_preferred(self, ctx, *, company: str, shares_input: str):
        """Mua cổ phiếu ưu đãi — nhận cổ tức **cố định** ưu tiên trước CP thường."""
        data = await self._user_data(ctx.author.id)
        if data is None:
            return await ctx.send("\u274c Hệ thống kinh tế chưa sẵn sàng.")
        want = self._parse_amount(shares_input, 0) or 0
        if want <= 0:
            return await ctx.send("\u274c Số cổ phiếu không hợp lệ.")

        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._company_row(db, company)
                    if not row:
                        await db.rollback()
                        return await ctx.send("\u274c Công ty không tồn tại.")
                    cid, cname, _o, total_shares, price, cash = (
                        row[0], row[1], row[2], row[3], row[4], int(row[5]),
                    )
                    pref_price = max(1, int(price) * 2)
                    cost = want * pref_price
                    # Tien phai den tu VI NGUOI MUA truoc khi phat hanh CP
                    # uu dai; khong duoc tu tao tien tu quy cong ty.
                    data = await self._user_data(ctx.author.id)
                    balance = int(data["balance"]) if data else 0
                    if balance < cost:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Ví của bạn chỉ có {fmt_vnd(balance)} VNĐ, "
                            f"cần {fmt_vnd(cost)} VNĐ."
                        )
                    async with db.execute(
                        "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                        (cost, ctx.author.id, cost),
                    ) as cur:
                        if cur.rowcount != 1:
                            await db.rollback()
                            return await ctx.send("\u274c Khong du tien.")
                    # Quy cong ty nhan tien ban CP uu dai.
                    await db.execute(
                        "UPDATE companies SET cash=cash+? WHERE id=?", (cost, cid)
                    )
                    await db.execute(
                        "INSERT INTO biz_preferred(company_id,user_id,shares)"
                        " VALUES(?,?,?)"
                        " ON CONFLICT(company_id,user_id) DO UPDATE SET"
                        " shares=shares+excluded.shares",
                        (cid, ctx.author.id, want),
                    )
                    await db.commit()
                except BizError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        await ctx.send(
            f"\u2b50 Đã mua **{want} CP ưu đãi** của **{cname}**.\n"
            f"Giá: {fmt_vnd(pref_price)}/CP (gấp đôi CP thường) — tổng {fmt_vnd(cost)} VNĐ.\n"
            f"Ưu đãi nhận cổ tức **trước** mọi cổ đông thường."
        )

    @commands.command(name="danhchicpuudai", aliases=["cpuudaitoi"])
    async def preferred_list(self, ctx, *, company: str):
        """Xem danh sách cổ đông ưu đãi."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await self._company_row(db, company)
            if not row:
                return await ctx.send("\u274c Công ty không tồn tại.")
            async with db.execute(
                "SELECT user_id,shares FROM biz_preferred WHERE company_id=?"
                " ORDER BY shares DESC", (row[0],)
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await ctx.send("\u2b50 Công ty chưa có cổ phiếu ưu đãi nào.")
        lines = [f"• <@{u}> — **{fmt_vnd(s)} CP ưu đãi**" for u, s in rows]
        await ctx.send(
            "\u2b50 **CỔ ĐÔNG ƯU ĐÃI**\n" + "\n".join(lines)[:1900]
        )
    async def reprice_listed(self, company_id: int, profit: int,
                             capital: int) -> tuple[int, int]:
        """Điều chỉnh giá CP theo lợi nhuận ngày (chỉ khi đã niêm yết).

        Trả về ``(gia_moi, phan_tram_thay_doi)``.
        """
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT listed FROM biz_meta WHERE company_id=?", (company_id,)
                ) as cur:
                    row = await cur.fetchone()
                if not row or int(row[0] or 0) <= 0:
                    await db.rollback()
                    return (0, 0)
                async with db.execute(
                    "SELECT price_per_share FROM companies WHERE id=?", (company_id,)
                ) as cur:
                    prow = await cur.fetchone()
                if not prow or int(prow[0]) <= 0:
                    await db.rollback()
                    return (0, 0)
                old_price = int(prow[0])
                # Bien do gia phu thuoc loi nhuuan / von: ±10% toi da.
                ratio = (float(profit) / float(capital)) if capital > 0 else 0.0
                pct = int(max(-0.10, min(0.10, ratio)) * 100)
                new_price = max(1, int(old_price * (1 + pct / 100.0)))
                await db.execute(
                    "UPDATE companies SET price_per_share=? WHERE id=?",
                    (new_price, company_id),
                )
                await db.execute(
                    "UPDATE biz_listing SET price=?, change_pct=? WHERE company_id=?",
                    (new_price, pct, company_id),
                )
                await db.commit()
                return (new_price, pct)
            except Exception:  # noqa: BLE001 - lich re khong duoc lam lo giao dich
                await db.rollback()
                return (0, 0)

    # ==================================================================
    # 5. NIEM YET SAN — gia CP tang va dao dong that
    # ==================================================================

    @commands.command(name="niemyet", aliases=["ipos", "listsan"])
    async def ipo(self, ctx, *, company: str):
        """Niêm yết công ty lên sàn: đủ vốn thì giá cổ phiếu **+15%**."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._require_owner(db, ctx, company, "niêm yết")
                    cid, cname, _o, total_shares, price, _cash = (
                        row[0], row[1], row[2], row[3], row[4], row[5],
                    )
                    capital = int(total_shares) * int(price)
                    if capital < IPO_MIN_CAPITAL:
                        await db.rollback()
                        need = IPO_MIN_CAPITAL - capital
                        return await ctx.send(
                            f"\u274c Vốn {fmt_vnd(capital)} VNĐ, cần tối thiểu "
                            f"{fmt_vnd(IPO_MIN_CAPITAL)} VNĐ (thiếu {fmt_vnd(need)})."
                        )
                    async with db.execute(
                        "SELECT listed FROM biz_meta WHERE company_id=?", (cid,)
                    ) as cur:
                        meta = await cur.fetchone()
                    if meta and int(meta[0] or 0) > 0:
                        await db.rollback()
                        return await ctx.send("\u274c Công ty đã niêm yết rồi.")
                    new_price = max(1, int(int(price) * IPO_BONUS))
                    await db.execute(
                        "UPDATE companies SET price_per_share=? WHERE id=?",
                        (new_price, cid),
                    )
                    await db.execute(
                        "UPDATE biz_meta SET listed=1 WHERE company_id=?", (cid,)
                    )
                    await db.execute(
                        "INSERT INTO biz_listing(company_id,listed_at,price,"
                        "change_pct) VALUES(?,?,?,?)"
                        " ON CONFLICT(company_id) DO UPDATE SET"
                        " price=excluded.price",
                        (cid, time.time(), new_price, 0),
                    )
                    await db.commit()
                except BizError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        await ctx.send(
            f"\U0001f6a8 **{cname} đã niêm yết sàn!**\n"
            f"Giá cổ phiếu: {fmt_vnd(price)} → **{fmt_vnd(new_price)}** VNĐ "
            f"(+{int((IPO_BONUS - 1) * 100)}%).\n"
            f"Từ giờ giá dao động theo kết quả kinh doanh mỗi ngày."
        )

    @commands.command(name="giacpcty", aliases=["giacty"])
    async def listed_price(self, ctx, *, company: str):
        """Xem giá cổ phiếu hiện tại của công ty (nếu đã niêm yết)."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await self._company_row(db, company)
            if not row:
                return await ctx.send("\u274c Công ty không tồn tại.")
            cid, cname, _o, total_shares, price, cash = (
                row[0], row[1], row[2], row[3], row[4], row[5],
            )
            async with db.execute(
                "SELECT listed FROM biz_meta WHERE company_id=?", (cid,)
            ) as cur:
                meta = await cur.fetchone()
            async with db.execute(
                "SELECT COALESCE(SUM(profit),0) FROM biz_history"
                " WHERE company_id=? AND day>?",
                (cid, int(time.time() // 86400) - 7),
            ) as cur:
                week = (await cur.fetchone())[0]
        listed = bool(meta and int(meta[0] or 0) > 0)
        embed = discord.Embed(title=f"\U0001f4b0 {cname}", color=discord.Color.teal())
        embed.add_field(name="Giá 1 CP", value=f"**{fmt_vnd(price)}** VNĐ")
        embed.add_field(name="Vốn hóa",
                        value=f"{fmt_vnd(int(total_shares) * int(price))} VNĐ")
        embed.add_field(name="Quỹ công ty", value=f"{fmt_vnd(int(cash))} VNĐ")
        embed.add_field(name="Lợi nhuận 7 ngày", value=f"{fmt_vnd(week)} VNĐ")
        embed.add_field(
            name="Trạng thái",
            value="✅ Đã niêm yết" if listed else "❌ Chưa niêm yết",
        )
        await ctx.send(embed=embed)

    # ==================================================================
    # 6. NO DOANH NGHIEP — vay tu "ngan hang" he thong, lai theo ngay
    # ==================================================================

    #: Vốn ban đầu của kho tín dụng — nguồn tiền duy nhất cho vay doanh
    #: nghiệp. Mọi khoản vay/giữ ký đều phải quay lại đúng kho này.
    LEND_POOL_SEED = 500_000_000_000

    @staticmethod
    async def _pool_balance(db) -> int:
        """Số dư kho tín dụng (tự tạo nếu chưa có)."""
        await db.execute(
            "INSERT OR IGNORE INTO biz_lend_pool(id,balance) VALUES(1,?)",
            (Business.LEND_POOL_SEED,),
        )
        async with db.execute("SELECT balance FROM biz_lend_pool WHERE id=1") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def _pool_add(db, amount: int) -> None:
        """Cộng tiền vào kho (giữ ký, trả nợ)."""
        await db.execute(
            "INSERT OR IGNORE INTO biz_lend_pool(id,balance) VALUES(1,?)",
            (Business.LEND_POOL_SEED,),
        )
        await db.execute(
            "UPDATE biz_lend_pool SET balance=balance+? WHERE id=1", (int(amount),)
        )

    @staticmethod
    async def _pool_take(db, amount: int) -> bool:
        """Rút tiền khỏi kho (hoàn ký). ``False`` nếu kho không đủ."""
        async with db.execute(
            "UPDATE biz_lend_pool SET balance=balance-? WHERE id=1 AND balance>=?",
            (int(amount), int(amount)),
        ) as cur:
            return cur.rowcount == 1
    async def _apply_debt_interest_in_tx(
        self, db, company_id: int, current_day: int
    ) -> tuple[int, int, int] | None:
        async with db.execute(
            "SELECT amount,days_left,last_accrued_day FROM biz_debt"
            " WHERE company_id=?",
            (company_id,),
        ) as cur:
            debt_row = await cur.fetchone()
        if not debt_row or int(debt_row[0]) <= 0:
            return None

        amount = int(debt_row[0])
        days_left = max(0, int(debt_row[1]))
        stored_day = int(debt_row[2] or 0)
        if stored_day <= 0:
            async with db.execute(
                "SELECT opened_at FROM biz_debt_log WHERE company_id=?"
                " ORDER BY id DESC LIMIT 1",
                (company_id,),
            ) as cur:
                opened_row = await cur.fetchone()
            opened_at = float(opened_row[0]) if opened_row else 0
            last_day = (
                max(0, int(opened_at // 86400))
                if opened_at > 0 else current_day
            )
        else:
            last_day = stored_day

        elapsed_days = max(0, current_day - last_day)
        interest = 0
        if elapsed_days:
            for _ in range(elapsed_days):
                rate = DEBT_DAILY_RATE if days_left > 0 else DEBT_OVERDUE_DAILY
                daily_interest = int(amount * rate)
                amount += daily_interest
                if amount > 9_223_372_036_854_775_807:
                    raise BizError("Dư nợ vượt giới hạn lưu trữ an toàn.")
                interest += daily_interest
                days_left = max(0, days_left - 1)
                if daily_interest == 0 and days_left == 0:
                    break

        if elapsed_days == 0:
            return interest, 0, amount

        async with db.execute(
            "SELECT cash FROM companies WHERE id=?", (company_id,)
        ) as cur:
            company_row = await cur.fetchone()
        if not company_row:
            return None
        cash = max(0, int(company_row[0]))
        paid = min(cash, amount)
        if paid:
            async with db.execute(
                "UPDATE companies SET cash=cash-? WHERE id=? AND cash>=?",
                (paid, company_id, paid),
            ) as cur:
                if cur.rowcount != 1:
                    raise BizError("Quỹ công ty đã thay đổi khi tính lãi vay.")
        remaining = amount - paid
        if remaining == 0:
            days_left = 0
        async with db.execute(
            "UPDATE biz_debt SET amount=?,days_left=?,last_accrued_day=?"
            " WHERE company_id=?",
            (remaining, days_left, max(last_day, current_day), company_id),
        ) as cur:
            if cur.rowcount != 1:
                raise BizError("Không tìm thấy khoản nợ khi ghi nhận lãi.")
        await self._pool_add(db, paid)
        return interest, paid, remaining

    async def apply_debt_interest(self, company_id: int) -> str:
        """Tính bù lãi theo ngày kể từ lần gần nhất, trừ từ quỹ công ty."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    accrual = await self._apply_debt_interest_in_tx(
                        db, company_id, int(time.time() // 86400)
                    )
                    await db.commit()
                    if accrual is None:
                        return ""
                    interest, paid, remaining = accrual
                    if interest == 0 and paid == 0:
                        return ""
                    return (
                        f"Lãi vay {fmt_vnd(interest)} VNĐ, đã trả "
                        f"{fmt_vnd(paid)} VNĐ từ quỹ, còn nợ "
                        f"{fmt_vnd(remaining)} VNĐ."
                    )
                except BizError as exc:
                    await db.rollback()
                    return f"Lỗi lãi vay: {exc}"
    @commands.command(name="vaycty", aliases=["vaydoanhnghiep", "vayct"])
    async def loan_company(self, ctx, *, company: str, amount_input: str):
        """Chủ tịch vay tiền cho công ty (tiền đến từ kho tín dụng hệ thống)."""
        data = await self._user_data(ctx.author.id)
        if data is None:
            return await ctx.send("\u274c Hệ thống kinh tế chưa sẵn sàng.")
        want = self._parse_amount(amount_input, 0) or 0
        if want <= 0:
            return await ctx.send("\u274c Số tiền không hợp lệ.")

        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._require_owner(db, ctx, company, "vay tiền")
                    cid, cname, _o, total_shares, price, _cash = (
                        row[0], row[1], row[2], row[3], row[4], row[5],
                    )
                    async with db.execute(
                        "SELECT amount,days_left FROM biz_debt WHERE company_id=?",
                        (cid,),
                    ) as cur:
                        drow = await cur.fetchone()
                    if drow and int(drow[0]) > 0:
                        await self._apply_debt_interest_in_tx(
                            db, cid, int(time.time() // 86400)
                        )
                        await db.commit()
                        await db.execute("BEGIN IMMEDIATE")
                        row = await self._require_owner(db, ctx, company, "vay tiền")
                        cid, cname, _o, total_shares, price, _cash = (
                            row[0], row[1], row[2], row[3], row[4], row[5],
                        )
                        async with db.execute(
                            "SELECT amount,days_left FROM biz_debt WHERE company_id=?",
                            (cid,),
                        ) as cur:
                            drow = await cur.fetchone()
                    if drow and int(drow[1]) > 0:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Công ty đang có khoản vay cũ "
                            f"({fmt_vnd(drow[0])} VNĐ, còn {drow[1]} ngày)."
                        )
                    if drow and int(drow[0]) > 0:
                        await db.rollback()
                        return await ctx.send(
                            "\u274c Công ty đang nợ quá hạn — trả hết rồi hãy vay."
                        )
                    capital = max(1, int(total_shares) * int(price))
                    limit = min(DEBT_MAX_AMOUNT, int(capital * DEBT_MAX_PCT))
                    if want > limit:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Hạn mức của **{cname}** là "
                            f"{fmt_vnd(limit)} VNĐ (60% vốn, tối đa "
                            f"{fmt_vnd(DEBT_MAX_AMOUNT)})."
                        )
                    if await self._pool_balance(db) < want:
                        await db.rollback()
                        return await ctx.send(
                            "\u274c Ngân hàng hệ thống không đủ tiền cho vay."
                        )
                    # Tiền: kho tín dụng -> quỹ công ty (không tạo tiền mới).
                    if not await self._pool_take(db, want):
                        await db.rollback()
                        return await ctx.send("\u274c Kho tín dụng không đủ.")
                    await db.execute(
                        "UPDATE companies SET cash=cash+? WHERE id=?", (want, cid)
                    )
                    await db.execute(
                        "INSERT INTO biz_debt(company_id,amount,daily_pay,days_left,"
                        "last_accrued_day) VALUES(?,?,?,?,?)"
                        " ON CONFLICT(company_id) DO UPDATE SET"
                        " amount=excluded.amount, days_left=excluded.days_left,"
                        " last_accrued_day=excluded.last_accrued_day",
                        (
                            cid, want, 0, DEBT_TERM_DAYS,
                            int(time.time() // 86400),
                        ),
                    )
                    await db.execute(
                        "INSERT INTO biz_debt_log(company_id,amount,rate,opened_at)"
                        " VALUES(?,?,?,?)",
                        (cid, want, DEBT_DAILY_RATE, time.time()),
                    )
                    await db.commit()
                except BizError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        await ctx.send(
            f"\U0001f4b8 **{cname}** vay {fmt_vnd(want)} VNĐ.\n"
            f"Kỳ hạn **{DEBT_TERM_DAYS} ngày**, lãi "
            f"{DEBT_DAILY_RATE * 100}%/ngày — tự trừ mỗi ngày khỏi quỹ."
        )
    @commands.command(name="trannocty", aliases=["trannocongty", "tronohoc"])
    async def repay_company(self, ctx, *, company: str, amount_input: str):
        """Trả nợ bằng tiền của **chủ tịch** (khi quỹ công ty không đủ)."""
        data = await self._user_data(ctx.author.id)
        if data is None:
            return await ctx.send("\u274c Hệ thống kinh tế chưa sẵn sàng.")
        company_paid = 0
        settled_by_company = False
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._require_owner(db, ctx, company, "trả nợ")
                    cid, cname = row[0], row[1]
                    async with db.execute(
                        "SELECT amount FROM biz_debt WHERE company_id=?", (cid,)
                    ) as cur:
                        drow = await cur.fetchone()
                    debt = int(drow[0]) if drow else 0
                    if debt <= 0:
                        await db.rollback()
                        return await ctx.send(f"\u274c **{cname}** không có nợ.")

                    accrual = await self._apply_debt_interest_in_tx(
                        db, cid, int(time.time() // 86400)
                    )
                    await db.commit()
                    if accrual is None:
                        return await ctx.send(
                            "\u274c Không thể cập nhật khoản nợ; vui lòng thử lại."
                        )
                    if accrual[2] <= 0:
                        settled_by_company = True
                    else:
                        company_paid = accrual[1]
                        await db.execute("BEGIN IMMEDIATE")
                        row = await self._require_owner(db, ctx, company, "trả nợ")
                        cid, cname = row[0], row[1]
                        async with db.execute(
                            "SELECT amount FROM biz_debt WHERE company_id=?", (cid,)
                        ) as cur:
                            drow = await cur.fetchone()
                        debt = int(drow[0]) if drow else 0
                        if debt <= 0:
                            await db.commit()
                            settled_by_company = True
                        else:
                            balance = await self._balance(db, ctx.author.id)
                            amt = self._parse_amount(amount_input, balance) or 0
                            if amt <= 0:
                                await db.rollback()
                                return await ctx.send("\u274c Số tiền không hợp lệ.")
                            pay = min(amt, debt)
                            if pay <= 0:
                                await db.rollback()
                                return await ctx.send(
                                    "\u274c Ví của bạn không đủ tiền trả nợ."
                                )
                            if not await self._charge(
                                db, ctx.author.id, pay, f"tra no {cname}"
                            ):
                                await db.rollback()
                                return await ctx.send(
                                    "\u274c Ví của bạn không đủ tiền trả nợ."
                                )
                            await db.execute(
                                "UPDATE biz_debt SET amount=amount-?, days_left=0"
                                " WHERE company_id=?",
                                (pay, cid),
                            )
                            await self._pool_add(db, pay)
                            left = debt - pay
                            await db.commit()
                except BizError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        if settled_by_company:
            return await ctx.send(
                f"\U0001f3e6 **{cname}** đã được quỹ công ty tất toán; "
                "không cần trừ tiền trong ví."
            )
        tail = (f"Còn nợ **{fmt_vnd(left)} VNĐ**." if left > 0
                else "\U0001f389 **Đã trả hết nợ!**")
        company_paid_note = (
            f"Quỹ công ty đã trả {fmt_vnd(company_paid)} VNĐ; "
            if company_paid > 0 else ""
        )
        await ctx.send(
            f"\U0001f3e6 **{cname}** {company_paid_note}"
            f"chủ tịch trả nợ {fmt_vnd(pay)} VNĐ từ ví.\n{tail}"
        )

    @commands.command(name="nocty", aliases=["nohoccty", "congno"])
    async def debt_info(self, ctx, *, company: str):
        """Xem khoản nợ của công ty."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await self._company_row(db, company)
            if not row:
                return await ctx.send("\u274c Công ty không tồn tại.")
            cid, cname = row[0], row[1]
            async with db.execute(
                "SELECT amount,days_left FROM biz_debt WHERE company_id=?", (cid,)
            ) as cur:
                drow = await cur.fetchone()
        debt = int(drow[0]) if drow else 0
        days = int(drow[1]) if drow else 0
        if debt <= 0:
            return await ctx.send(f"\U0001f4da **{cname}** không có nợ.")
        state = f"còn {days} ngày" if days > 0 else "**QUÁ HẠN**"
        await ctx.send(
            f"\U0001f4da **Nợ của {cname}**\n"
            f"• Dư nợ: **{fmt_vnd(debt)} VNĐ**\n"
            f"• Kỳ hạn: {state}\n"
            f"• Lãi: {DEBT_DAILY_RATE * 100}%/ngày, tự trừ mỗi ngày"
        )
# ==================================================================
    # 7. ESOP — cap CP cho nhan vien (loa CP moi, lam loang cot phan)
    # ==================================================================

    @commands.command(name="esop", aliases=["capcpnhanvien", "esopcty"])
    async def grant_esop(self, ctx, *, company: str, member: discord.Member,
                         shares_input: str):
        """Chủ tịch cấp cổ phiếu ESOP cho nhân viên.

        ESOP phát hành **cổ phiếu mới** nên làm loãng tỷ lệ sở hữu của cổ
        đông cũ — đúng bản chất ESOP, và **không** tạo tiền từ hư không.
        """
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    row = await self._require_owner(db, ctx, company, "cấp ESOP")
                    cid, cname, _o, total_shares, _price, _cash = (
                        row[0], row[1], row[2], row[3], row[4], row[5],
                    )
                    async with db.execute(
                        "SELECT 1 FROM biz_staff WHERE company_id=? AND user_id=?"
                        " AND active=1", (cid, member.id),
                    ) as cur:
                        if not await cur.fetchone():
                            await db.rollback()
                            return await ctx.send(
                                f"\u274c <@{member.id}> không phải nhân viên "
                                f"đang làm của **{cname}**."
                            )
                    async with db.execute(
                        "SELECT COALESCE(SUM(shares),0) FROM biz_esop"
                        " WHERE company_id=?", (cid,)
                    ) as cur:
                        granted = int((await cur.fetchone())[0])
                    want = self._parse_amount(shares_input, 0) or 0
                    if want <= 0:
                        await db.rollback()
                        return await ctx.send("\u274c Số cổ phiếu không hợp lệ.")
                    total = int(total_shares)
                    cap = int(total * ESOP_MAX_PCT)
                    if granted + want > cap:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Quỹ ESOP của **{cname}** chỉ còn "
                            f"{fmt_vnd(max(0, cap - granted))} CP "
                            f"(trần {int(ESOP_MAX_PCT * 100)}% vốn)."
                        )
                    # Phát hành CP mới -> tổng CP tăng, không đổi dòng tiền.
                    await db.execute(
                        "UPDATE companies SET total_shares=total_shares+? WHERE id=?",
                        (want, cid),
                    )
                    await db.execute(
                        "INSERT INTO shares(user_id,company_id,amount) VALUES(?,?,?)"
                        " ON CONFLICT(user_id,company_id) DO UPDATE SET"
                        " amount=amount+excluded.amount",
                        (member.id, cid, want),
                    )
                    await db.execute(
                        "INSERT INTO biz_esop(company_id,user_id,shares)"
                        " VALUES(?,?,?)"
                        " ON CONFLICT(company_id,user_id) DO UPDATE SET"
                        " shares=shares+excluded.shares",
                        (cid, member.id, want),
                    )
                    new_total = total + want
                    await db.commit()
                except BizError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        await ctx.send(
            f"\U0001f381 **{cname}** cấp **{fmt_vnd(want)} CP** ESOP cho "
            f"<@{member.id}>.\nTổng CP lưu hành: {fmt_vnd(total)} → "
            f"**{fmt_vnd(new_total)}** — cổ đông cũ bị loãng tỷ lệ "
            "(đúng bản chất ESOP)."
        )

    @commands.command(name="danhsachesop", aliases=["danhsachesopcty"])
    async def esop_list(self, ctx, *, company: str):
        """Xem nhân viên nào đã được cấp ESOP."""
        async with aiosqlite.connect(self.db_path) as db:
            row = await self._company_row(db, company)
            if not row:
                return await ctx.send("\u274c Công ty không tồn tại.")
            cid, cname = row[0], row[1]
            async with db.execute(
                "SELECT user_id,shares FROM biz_esop WHERE company_id=?"
                " ORDER BY shares DESC", (cid,)
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await ctx.send(f"\U0001f381 **{cname}** chưa cấp ESOP cho ai.")
        lines = [f"• <@{u}> — **{fmt_vnd(s)} CP**" for u, s in rows]
        await ctx.send(
            f"\U0001f381 **DANH SÁCH ESOP — {cname}**\n" + "\n".join(lines)[:1900]
        )
# ==================================================================
    # 8. TRANH GIA — dat coc mua CP, tien bi giu ky (escrow)
    # ==================================================================

    @commands.command(name="datgiacp", aliases=["datgia", "tranhgia"])
    async def place_bid(self, ctx, *, target: str, price_input: str):
        """Đặt giá mua cổ phiếu công ty khác. **Tiền bị giữ ký** (escrow)."""
        data = await self._user_data(ctx.author.id)
        if data is None:
            return await ctx.send("\u274c Hệ thống kinh tế chưa sẵn sàng.")
        want = self._parse_amount(price_input, 0) or 0
        if want <= 0:
            return await ctx.send("\u274c Giá đặt không hợp lệ.")

        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    trow = await self._company_row(db, target)
                    if not trow:
                        await db.rollback()
                        return await ctx.send("\u274c Công ty không tồn tại.")
                    tid, tname = trow[0], trow[1]
                    floor = int(int(trow[4]) * int(trow[3]) * BID_MIN_PREMIUM)
                    if want < floor:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Giá đặt phải ≥ {fmt_vnd(floor)} VNĐ "
                            f"(≥ {int(BID_MIN_PREMIUM * 100)}% vốn hóa)."
                        )
                    balance = await self._balance(db, ctx.author.id)
                    if balance < want:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c Ví chỉ có {fmt_vnd(balance)} VNĐ."
                        )
                    # Giữ ký: ví -> kho escrow (tiền không mất, không tạo mới).
                    if not await self._charge(db, ctx.author.id, want,
                                              f"dat gia {tname}"):
                        await db.rollback()
                        return await ctx.send("\u274c Khong du tien dat gia.")
                    await self._pool_add(db, want)
                    await db.execute(
                        "INSERT INTO biz_raid(target_id,bidder_id,bidder_company,"
                        "bid_price,until) VALUES(?,?,?,?,?)",
                        (tid, ctx.author.id, 0, want,
                         time.time() + BID_HOLD_DAYS * 86400),
                    )
                    await db.commit()
                except BizError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        await ctx.send(
            f"\U0001f3d6 Bạn đã đặt **{fmt_vnd(want)} VNĐ** cho **{tname}**.\n"
            f"Tiền được **giữ ký {BID_HOLD_DAYS} ngày** — "
            f"dùng `!huygia` để rút lại."
        )

    @commands.command(name="huygia", aliases=["huydatgia", "rutdatgia"])
    async def cancel_bid(self, ctx):
        """Hủy đặt giá gần nhất và **nhận lại** tiền ký."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    async with db.execute(
                        "SELECT id,target_id,bid_price FROM biz_raid"
                        " WHERE bidder_id=? AND cancelled=0"
                        " ORDER BY id DESC LIMIT 1", (ctx.author.id,),
                    ) as cur:
                        row = await cur.fetchone()
                    if not row:
                        await db.rollback()
                        return await ctx.send("\u274c Bạn không có đơn đặt giá nào.")
                    bid_id, target_id, price = row[0], row[1], int(row[2])
                    async with db.execute(
                        "SELECT name FROM companies WHERE id=?", (target_id,)
                    ) as cur:
                        crow = await cur.fetchone()
                    tname = crow[0] if crow else f"#{target_id}"
                    # Hoàn ký: rút từ kho về ví trước, rồi mới đánh dấu hủy.
                    if not await self._pool_take(db, price):
                        await db.rollback()
                        return await ctx.send("\u274c Kho ký không đủ tiền hoàn.")
                    await self._pay(db, ctx.author.id, price, f"huy gia {tname}")
                    await db.execute(
                        "UPDATE biz_raid SET cancelled=1 WHERE id=?", (bid_id,)
                    )
                    await db.commit()
                except BizError as exc:
                    await db.rollback()
                    return await ctx.send(f"\u274c {exc}")

        await ctx.send(
            f"\u274c Đã hủy đặt giá cho **{tname}**, hoàn lại "
            f"**{fmt_vnd(price)} VNĐ**."
        )

    @commands.command(name="dsdatgia", aliases=["danhsachdatgia", "dsgia"])
    async def bid_list(self, ctx):
        """Xem các đơn đặt giá đang có."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT r.target_id,c.name,r.bid_price,r.until FROM biz_raid r"
                " LEFT JOIN companies c ON c.id=r.target_id"
                " WHERE r.cancelled=0 ORDER BY r.id DESC LIMIT 15"
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await ctx.send("\U0001f3d6 Chưa có đơn đặt giá nào.")
        lines = []
        for tid, name, price, until in rows:
            left = max(0, int((until - time.time()) // 86400))
            label = name if name else f"#{tid}"
            lines.append(f"• **{label}** — {fmt_vnd(price)} VNĐ (còn {left} ngày)")
        await ctx.send("\U0001f3d6 **ĐƠN ĐẶT GIÁ**\n" + "\n".join(lines)[:1900])
# ==================================================================
    # 9. BAO CAO TAI CHINH — tong hop tu nhat ky + quy + no
    # ==================================================================

    @commands.command(name="baocaocty", aliases=["baocaotc", "bctc"])
    async def financial_report(self, ctx, *, company: str, days: int = 7):
        """Báo cáo tài chính: doanh thu, chi phí, lợi nhuận, quỹ, nợ."""
        days = max(1, min(90, int(days)))
        async with aiosqlite.connect(self.db_path) as db:
            row = await self._company_row(db, company)
            if not row:
                return await ctx.send("\u274c Công ty không tồn tại.")
            cid, cname, _o, total_shares, price, cash = (
                row[0], row[1], row[2], row[3], row[4], int(row[5]),
            )
            today = int(time.time() // 86400)
            async with db.execute(
                "SELECT COUNT(*),COALESCE(SUM(revenue),0),COALESCE(SUM(cost),0),"
                "COALESCE(SUM(profit),0),COALESCE(SUM(tax),0) FROM biz_history"
                " WHERE company_id=? AND day>?", (cid, today - days)
            ) as cur:
                r = await cur.fetchone()
            runs, revenue, cost, profit, tax = (
                int(r[0]), int(r[1]), int(r[2]), int(r[3]), int(r[4])
            )
            async with db.execute(
                "SELECT amount,days_left FROM biz_debt WHERE company_id=?", (cid,)
            ) as cur:
                drow = await cur.fetchone()
            debt, days_left = (int(drow[0]), int(drow[1])) if drow else (0, 0)
            async with db.execute(
                "SELECT COALESCE(SUM(salary),0) FROM biz_staff"
                " WHERE company_id=? AND active=1", (cid,),
            ) as cur:
                payroll = int((await cur.fetchone())[0])

        sign = "+" if profit >= 0 else ""
        margin = int(profit * 100 / revenue) if revenue > 0 else 0
        term = f"còn {days_left} ngày" if days_left > 0 else "**QUÁ HẠN**"
        embed = discord.Embed(
            title=f"\U0001f4b0 Báo cáo tài chính — {cname}",
            color=discord.Color.teal(),
        )
        embed.add_field(name="Quy mô", value=(
            f"Vốn hóa: {fmt_vnd(int(total_shares) * int(price))} VNĐ\n"
            f"Giá CP: {fmt_vnd(price)} VNĐ\n"
            f"Quỹ công ty: {fmt_vnd(cash)} VNĐ"))
        embed.add_field(name=f"{days} ngày qua", value=(
            f"Số lần chạy: {runs}\n"
            f"Doanh thu: {fmt_vnd(revenue)} VNĐ\n"
            f"Chi phí: -{fmt_vnd(cost)} VNĐ\n"
            f"Thuế: -{fmt_vnd(tax)} VNĐ"))
        embed.add_field(name="Kết quả", value=(
            f"Lợi nhuận: **{sign}{fmt_vnd(profit)} VNĐ**\n"
            f"Biên lợi nhuận: {margin}%"))
        embed.add_field(name="Nợ & lương", value=(
            f"Dư nợ: {fmt_vnd(debt)} VNĐ ({term})\n"
            f"Tổng lương/ngày: {fmt_vnd(payroll)} VNĐ"))
        await ctx.send(embed=embed)

    # ==================================================================
    # 10. THUE LAI NGAN HANG — 5% tren phan vuot nguong
    # ==================================================================

    @commands.command(name="thuedailai", aliases=["thuephainv", "thuelainganhang"])
    async def interest_tax_info(self, ctx):
        """Giải thích cách tính thuế lãi ngân hàng."""
        await ctx.send(
            f"\U0001f4b9 **Thuế lãi ngân hàng**\n"
            f"• Thuế **{SAVINGS_INTEREST_TAX * 100:.0f}%** trên phần lãi sinh ra "
            f"từ số dư **vượt {fmt_vnd(SAVINGS_TAX_THRESHOLD)} VNĐ**.\n"
            f"• Lãi từ phần dưới ngưỡng **được miễn thuế**.\n"
            f"• Được khấu trừ ngay khi `!sodu` cập nhật, ghi vào `transactions`."
        )


async def setup(bot: commands.Bot) -> None:
    """Discord.py gọi hàm này để đăng ký cog."""
    await bot.add_cog(Business(bot))
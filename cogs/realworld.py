# -*- coding: utf-8 -*-
"""
Cuộc sống đời thường: chợ với giá Việt Nam + sổ tiết kiệm ngân hàng
===================================================================

Vì sao tách khỏi ``cogs/games.py``
----------------------------------
``games.py`` giữ **tài sản dài hạn** (xe, nhà, đất) và các trò cờ bạc.
Module này lo **chi phí sống hằng ngày** — hai bài toán khác nhau về bản
chất, tách riêng để dữ liệu không lẫn vào ``inventory`` của games.

Tính năng
---------
* **Chợ** (``!giatham``, ``!muadi``, ``!tudon``, ``!an``, ``!banra``) --
  21 mặt hàng giá Việt Nam, giá **cố định cho cả server trong ngày**
  (``kai_core.prices``), có cờ *nhu yếu phẩm* để AI gợi ý khi thiếu.
* **Tiết kiệm** (``!moso``, ``!guitien``, ``!rutso``, ``!socuatoa``) --
  7 loại sổ theo kỳ hạn thật, lãi 4,4–8%/năm (``kai_core.savings``),
  có phí thủ tục và phạt rút sớm.

Bảo mật
--------
* Mọi khoản chi đi qua ``UPDATE ... WHERE balance >= ?`` + kiểm tra
  ``rowcount`` trong ``BEGIN IMMEDIATE`` — không bao giờ tin số dư đọc
  trước (TOCTOU), chính là lỗi đã gây "in tiền vô hạn" ở các cog cũ.
* Rút tiền chỉ đọc sổ **của chính người ra lệnh**; ``id`` của người khác
  bị từ chối (kiểm tra ownership, không tin dữ liệu người chơi gửi lên).
* Lãi là **lãi đơn theo ngày**, mỗi sổ chỉ cộng tối đa 1 lần/ngày nên
  không thể bơm tiền bằng cách spam lệnh.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiosqlite
import discord
from discord.ext import commands, tasks

from kai_core import get_logger, prices, savings
from kai_core.config import CONFIG
from kai_core.economy import validate_amount

log = get_logger("realworld")

DB_PATH = str(CONFIG.data_dir / "economy.db")

#: Tiền mua lại từ NPC (chợ xách tay) — luôn thấp hơn giá mua, nên không ai
#: lãi nếu chỉ mua đi bán lại: phải thật sự **ăn** mới có lợi.
RESELL_RATIO = 0.85
#: Một đơn vị nhu yếu phẩm giữ được bao nhiêu ngày (đồ tươi).
FRESH_DAYS = 7
#: Còn bao nhiêu ngày thì cảnh báo "sắp hết".
NEAR_EXPIRY = 2
#: Mỗi lần ăn cộng bao nhiêu uy tín.
MEAL_REP = 2
#: Tối đa số bữa mỗi ngày (chống spam ăn để farm uy tín).
MEAL_CAP = 3
#: Số món khác nhau tối đa trong tủ đồ.
MAX_PANTRY_ITEMS = 25
#: Số lượng mua tối đa trong một lệnh (chặn nhập số khổng lồ).
MAX_QTY = 999
#: Chu kỳ quét lãi tiết kiệm (giờ).
INTEREST_INTERVAL_HOURS = 6

#: Bảng mới của module này — ``IF NOT EXISTS`` nên chạy lại vô hạn lần cũng
#: an toàn, không cần migration. Cố tình **không** tạo bảng ``users`` /
#: ``transactions``: đó là schema của ``cogs/games.py``, và module này được
#: nạp sau ``games`` (xem ``PREFERRED_COGS`` trong ``main.py``).
SCHEMA = """
CREATE TABLE IF NOT EXISTS pantry (
    user_id  INTEGER NOT NULL,
    item     TEXT    NOT NULL,
    qty      INTEGER NOT NULL DEFAULT 0 CHECK(qty >= 0),
    bought_at REAL   NOT NULL DEFAULT 0,
    paid     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, item)
);
CREATE TABLE IF NOT EXISTS savings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    kind          TEXT    NOT NULL,
    balance       INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
    deposited     INTEGER NOT NULL DEFAULT 0,
    interest_paid INTEGER NOT NULL DEFAULT 0,
    opened_at     REAL    NOT NULL,
    last_interest REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_savings_user ON savings(user_id);
"""


def fmt_vnd(amount: Any) -> str:
    """Định dạng VNĐ kiểu Việt Nam (dấu chấm phân nghìn)."""
    try:
        return f"{int(amount):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def normalize_key(text: str) -> str:
    """Chuẩn hoá tên mặt hàng/loại sổ về dạng khoá tra cứu.

    Cho phép gõ tiếng Việt có dấu, cách nhau bởi dấu cách: ``"Trứng Gà"`` ->
    ``"trung"`` (khớp ``prices.GOODS``). Không tìm thấy thì trả chuỗi rút
    gọn để phía gọi tự báo lỗi "không bán mặt hàng này".
    """
    raw = str(text or "").strip().lower()
    table = {g.name.lower(): k for k, g in prices.GOODS.items()}
    table.update({a.name.lower(): k for k, a in savings.ACCOUNTS.items()})
    if raw in table:
        return table[raw]
    return raw.replace(" ", "_").replace("đ", "d")


class RealWorldError(Exception):
    """Lỗi nghiệp vụ — cog bắt lại và trả lời người chơi."""


class RealWorld(commands.Cog):
    """Chợ với giá đời thật + sổ tiết kiệm ngân hàng."""

    def __init__(self, bot: commands.Bot, db_path: str | None = None) -> None:
        self.bot = bot
        # Cho phép truyền ``db_path`` khi test (mặc định dùng DB của bot).
        self.db_path = db_path or DB_PATH
        self._lock_obj = asyncio.Lock()

    async def cog_load(self) -> None:
        """Tạo bảng nếu chưa có — idempotent, chạy lại vô hạn lần cũng an toàn."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(SCHEMA)
                await db.commit()
        self.interest_loop.start()

    async def cog_unload(self) -> None:
        """Dừng vòng lặp lãi trước khi tháo cog (tránh task treo khi reload)."""
        self.interest_loop.cancel()
        try:
            await self.interest_loop
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Tiện ích DB
    # ------------------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        """Khoá ghi DB dùng chung trong module này."""
        return self._lock_obj

    @staticmethod
    async def _ensure_user(db, user_id: int) -> None:
        """Tạo dòng ``users`` nếu chưa có (dùng DEFAULT của ``games.py``)."""
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),))

    @staticmethod
    async def _balance(db, user_id: int) -> int:
        """Đọc số dư hiện tại (chỉ để *hiển thị*, không dùng để ra quyết định)."""
        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?", (int(user_id),)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def _spend(db, user_id: int, amount: int) -> bool:
        """Trừ tiền **atomic**; trả ``False`` nếu không đủ.

        Không bao giờ đọc số dư rồi mới trừ (TOCTOU) — điều kiện ``balance>=?``
        nằm ngay trong ``UPDATE`` nên hai lệnh chạy song song chỉ một lệnh
        thành công. Đây là bảo đảm quan trọng nhất của cả hệ thống tiền.
        """
        if amount <= 0:
            return True
        cur = await db.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
            (int(amount), int(user_id), int(amount)),
        )
        return cur.rowcount == 1

    @staticmethod
    async def _credit(db, user_id: int, amount: int) -> int:
        """Cộng tiền atomic, trả số dư sau cùng."""
        if amount > 0:
            await db.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (int(amount), int(user_id)),
            )
        return await RealWorld._balance(db, user_id)

    @staticmethod
    async def _log_txn(db, user_id: int, kind: str, amount: int, balance_after: int,
                       note: str = "") -> None:
        """Ghi lịch sử giao dịch (cùng schema với ``cogs/games.py``)."""
        await db.execute(
            "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
            " VALUES(?,?,?,?,?,?)",
            (int(user_id), kind, int(amount), int(balance_after), time.time(), note[:200]),
        )

    # ------------------------------------------------------------------
    # CHO — bang gia, mua, tu do, an, ban lai
    # ------------------------------------------------------------------

    @commands.command(name="giatham", aliases=["banggia", "chotram"])
    async def market_board(self, ctx: commands.Context, group: str | None = None) -> None:
        """Xem bang gia hom nay cua tat ca mat hang (nhom tuy chon)."""
        now = time.time()
        wanted = (group or "").strip().lower()
        rows = prices.all_quotes(now)
        if wanted:
            rows = [r for r in rows if r[0].group == wanted or wanted in r[0].key]
            if not rows:
                groups = sorted({r[0].group for r in prices.all_quotes(now)})
                return await ctx.send(
                    f"❌ Khong co nhom `{wanted}`. Cac nhom: " + ", ".join(groups)
                )
        embed = discord.Embed(
            title="🛒 GIA THI TRUONG",
            description="Gia hom nay — **cung mot gia cho ca server**.\n"
                        "Bien dong so voi gia niem yet.",
            color=discord.Color.green(),
        )
        current: str | None = None
        lines: list[str] = []
        for good, price, change in rows:
            if good.group != current:
                if lines:
                    embed.add_field(name=prices.group_label(current or ""),
                                    value="\n".join(lines), inline=False)
                current, lines = good.group, []
            arrow = "▲" if change > 0 else ("▼" if change < 0 else "＝")
            flag = " ⭐" if good.essential else ""
            lines.append(
                f"{good.emoji} **{good.name}**{flag} — "
                f"`{fmt_vnd(price)}đ/{good.unit}` {arrow}{abs(change):.1f}%"
            )
        if lines:
            embed.add_field(name=prices.group_label(current or ""),
                            value="\n".join(lines), inline=False)
        embed.set_footer(text="⭐ = nhu yeu pham. Mua bang !muadi <ten> <so luong>")
        await ctx.send(embed=embed)

    @commands.command(name="muadi", aliases=["muahang"])
    async def buy_item(self, ctx: commands.Context, item: str, qty: int = 1) -> None:
        """Mua mat hang o chot voi gia hom nay."""
        key = normalize_key(item)
        good = prices.GOODS.get(key)
        if good is None:
            return await ctx.send(f"❌ Khong ban `{item}`. Xem bang gia bang `!giatham`.")
        try:
            amount = validate_amount(qty, minimum=1, maximum=MAX_QTY)
        except Exception:
            return await ctx.send(f"❌ So luong phai tu 1 den {MAX_QTY}.")

        unit = prices.quote(key)
        total = unit * amount
        uid = ctx.author.id

        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await self._ensure_user(db, uid)
                    async with db.execute(
                        "SELECT COUNT(*) FROM pantry WHERE user_id=? AND item<>?",
                        (int(uid), key),
                    ) as cur:
                        others = int((await cur.fetchone())[0])
                    if others >= MAX_PANTRY_ITEMS:
                        await db.rollback()
                        return await ctx.send(
                            f"❌ Tu do da day ({MAX_PANTRY_ITEMS} mon). Ban bo mon cu truoc."
                        )
                    if not await self._spend(db, uid, total):
                        await db.rollback()
                        return await ctx.send(
                            f"❌ Khong du tien. Can **{fmt_vnd(total)}đ** "
                            f"({amount} x {fmt_vnd(unit)}đ/{good.unit})."
                        )
                    await db.execute(
                        "INSERT INTO pantry(user_id,item,qty,bought_at,paid) VALUES(?,?,?,?,?)"
                        " ON CONFLICT(user_id,item) DO UPDATE SET"
                        " qty=qty+excluded.qty, bought_at=excluded.bought_at,"
                        " paid=paid+excluded.paid",
                        (int(uid), key, amount, time.time(), total),
                    )
                    balance = await self._balance(db, uid)
                    await self._log_txn(db, uid, "market:buy", -total, balance,
                                        f"Mua {amount} {good.unit} {good.name}")
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Mua that bai (%s)", key)
                    raise RealWorldError(f"Giao dich loi: {type(exc).__name__}") from exc

        await ctx.send(
            f"✅ Da mua **{amount} {good.unit} {good.name}** voi gia **{fmt_vnd(total)}đ**.\n"
            f"Số dư còn lại: **{fmt_vnd(balance)}đ**"
        )

    @commands.command(name="tudon", aliases=["tudonvao", "tuon"])
    async def pantry(self, ctx: commands.Context) -> None:
        """Xem tu do: mon gi, con bao lau, het han chua."""
        uid = ctx.author.id
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT item,qty,bought_at FROM pantry"
                    " WHERE user_id=? AND qty>0 ORDER BY item",
                    (int(uid),),
                ) as cur:
                    rows = await cur.fetchall()

        if not rows:
            return await ctx.send(
                "Tủ đồ trống. Mua thực phẩm bằng `!muadi <tên> <số lượng>` "
                "hoặc xem `!giatham`."
            )
        now = time.time()
        lines: list[str] = []
        total_value = 0
        expired = 0
        for item, qty, bought_at in rows:
            good = prices.GOODS.get(item)
            name = good.name if good else str(item)
            emoji = good.emoji if good else "📦"
            left = max(0, FRESH_DAYS - int((now - float(bought_at)) // 86_400))
            if left <= 0:
                mark = "⚠️ **Hết hạn**"
                expired += 1
            elif left <= NEAR_EXPIRY:
                mark = f"⏳ {left} ngày"
            else:
                mark = f"🟢 {left} ngày"
            value = prices.quote(item) * int(qty) if good else 0
            total_value += value
            lines.append(f"{emoji} **{name}** ×{qty} — {mark} (`{fmt_vnd(value)}đ`)")
        embed = discord.Embed(
            title="🧺 TỦ ĐỒ CỦA BẠN",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        footer = f"Tổng giá trị mua lại: ~{fmt_vnd(int(total_value * RESELL_RATIO))}đ"
        if expired:
            footer += f" · ⚠️ {expired} món hết hạn"
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

    @commands.command(name="an", aliases=["dung", "eat"])
    async def eat(self, ctx: commands.Context, item: str, qty: int = 1) -> None:
        """An mon trong tu do — cong uy tin, chan spam an de farm."""
        key = normalize_key(item)
        good = prices.GOODS.get(key)
        if good is None:
            return await ctx.send(f"❌ `{item}` khong phai mat hang nao.")
        if good.group not in {"thuc_pham", "do_an", "gia_vi", "nuoc_uong"}:
            return await ctx.send(
                f"❌ `{good.name}` khong phai dong an. Hay dung `!banra` de ban lai."
            )
        try:
            amount = validate_amount(qty, minimum=1, maximum=MAX_QTY)
        except Exception:
            return await ctx.send(f"❌ So luong phai tu 1 den {MAX_QTY}.")

        uid = ctx.author.id
        today = time.time() - (time.time() % 86_400)
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await self._ensure_user(db, uid)
                    async with db.execute(
                        "SELECT COUNT(*) FROM transactions"
                        " WHERE user_id=? AND kind='market:eat' AND created_at>=?",
                        (int(uid), today),
                    ) as cur:
                        meals = int((await cur.fetchone())[0])
                    if meals >= MEAL_CAP:
                        await db.rollback()
                        return await ctx.send(
                            f"❌ Hom nay da an {meals} bua. Nghi ngoi di, ngay mai an tiep!"
                        )
                    cur = await db.execute(
                        "UPDATE pantry SET qty=qty-? WHERE user_id=? AND item=? AND qty>=?",
                        (amount, int(uid), key, amount),
                    )
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send(f"❌ Khong du `{good.name}` trong tu do.")
                    rep = MEAL_REP * amount
                    # ``COALESCE`` vì cột ``reputation`` ở DB cũ có thể NULL —
                    # ``NULL + 2`` vẫn ra NULL, uy tín sẽ không bao giờ tăng.
                    await db.execute(
                        "UPDATE users SET reputation=COALESCE(reputation,0)+? WHERE user_id=?",
                        (rep, int(uid)),
                    )
                    balance = await self._balance(db, uid)
                    await self._log_txn(db, uid, "market:eat", 0, balance,
                                        f"An {amount} {good.unit} {good.name} (+{rep} uy tin)")
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("An that bai (%s)", key)
                    raise RealWorldError(f"Giao dich loi: {type(exc).__name__}") from exc

        await ctx.send(
            f"🍽️ Đã ăn **{amount} {good.unit} {good.name}**.\n"
            f"Uy tín **+{rep}** · Số dư: **{fmt_vnd(balance)}đ**"
        )

    @commands.command(name="banra", aliases=["banhang", "sell"])
    async def sell_item(self, ctx: commands.Context, item: str, qty: int = 1) -> None:
        """Ban lai cho gia thap hon gia mua (NPC mua lai, khong loi nhuoc)."""
        key = normalize_key(item)
        good = prices.GOODS.get(key)
        if good is None:
            return await ctx.send(f"❌ `{item}` khong phai mat hang nao.")
        try:
            amount = validate_amount(qty, minimum=1, maximum=MAX_QTY)
        except Exception:
            return await ctx.send(f"❌ So luong phai tu 1 den {MAX_QTY}.")

        unit = int(prices.quote(key) * RESELL_RATIO)
        total = unit * amount
        uid = ctx.author.id

        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await self._ensure_user(db, uid)
                    cur = await db.execute(
                        "UPDATE pantry SET qty=qty-? WHERE user_id=? AND item=? AND qty>=?",
                        (amount, int(uid), key, amount),
                    )
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send(f"❌ Khong du `{good.name}` trong tu do de ban.")
                    balance = await self._credit(db, uid, total)
                    await self._log_txn(db, uid, "market:sell", total, balance,
                                        f"Ban {amount} {good.unit} {good.name}")
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Ban lai that bai (%s)", key)
                    raise RealWorldError(f"Giao dich loi: {type(exc).__name__}") from exc

        await ctx.send(
            f"💰 Đã bán **{amount} {good.unit} {good.name}** được **{fmt_vnd(total)}đ**.\n"
            f"NPC mua lại rẻ hơn {int((1 - RESELL_RATIO) * 100)}% giá thị trường.\n"
            f"Số dư: **{fmt_vnd(balance)}đ**"
        )

    # ------------------------------------------------------------------
    # TIET KIEM — danh muc so, gui, rut, cong lai
    # ------------------------------------------------------------------

    @commands.command(name="moso", aliases=["danhmucso", "nganhang"])
    async def savings_catalog(self, ctx: commands.Context) -> None:
        """Xem 7 loai so tiet kiem dang co o ngan hang KaiAI."""
        embed = discord.Embed(
            title="🏦 KAI BANK — DANH MỤC SỔ TIẾT KIỆM",
            description=f"Phí mở sổ: **{fmt_vnd(savings.OPEN_FEE)}đ** · "
                        f"Tối đa **{savings.MAX_ACCOUNTS} sổ**/người · "
                        f"Gửi tối thiểu **{fmt_vnd(savings.MIN_OPEN)}đ**",
            color=discord.Color.blue(),
        )
        for acc in savings.ACCOUNTS.values():
            rate = savings.annual_for(acc.key, savings.MIN_OPEN * 10)
            term = "Không kỳ hạn" if acc.is_flexible else f"{acc.term_days} ngày"
            embed.add_field(
                name=f"{acc.emoji} `{acc.key}` — {acc.name} · {rate:.1f}%/năm",
                value=f"Kỳ hạn: {term}\n{acc.desc}",
                inline=False,
            )
        embed.set_footer(text="Mở sổ: !guitien <mã sổ> <số tiền>")
        await ctx.send(embed=embed)

    @commands.command(name="guitien", aliases=["guitienket", "mosonew"])
    async def open_savings(self, ctx: commands.Context, kind: str, amount: int) -> None:
        """Mo so tiet kiem moi hoac gui them tien vao so cua ban."""
        key = normalize_key(kind)
        acc = savings.ACCOUNTS.get(key)
        if acc is None:
            return await ctx.send(f"❌ Khong co loai so `{kind}`. Xem `!moso` de biet ma.")
        try:
            money = validate_amount(amount, minimum=savings.MIN_OPEN)
        except Exception:
            return await ctx.send(
                f"❌ So tien gui toi thieu **{fmt_vnd(savings.MIN_OPEN)}đ** "
                f"(va phai la so nguyen duong)."
            )

        uid = ctx.author.id
        now = time.time()
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await self._ensure_user(db, uid)
                    async with db.execute(
                        "SELECT id,balance FROM savings WHERE user_id=? AND kind=?",
                        (int(uid), key),
                    ) as cur:
                        row = await cur.fetchone()
                    if row is None:
                        await self._create_saving(ctx, db, uid, key, acc, money, now)
                    else:
                        await self._top_up_saving(ctx, db, uid, key, acc, money, int(row[0]))
                except RealWorldError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Mo/gui so that bai (%s)", key)
                    raise RealWorldError(f"Giao dich loi: {type(exc).__name__}") from exc

    async def _create_saving(self, ctx, db, uid: int, key: str, acc, money: int,
                             now: float) -> None:
        """Mo so moi (co thu them phi thu tuc). Loi thi nem ``RealWorldError``."""
        async with db.execute(
            "SELECT COUNT(*) FROM savings WHERE user_id=?", (int(uid),)
        ) as cur:
            count = int((await cur.fetchone())[0])
        if count >= savings.MAX_ACCOUNTS:
            await db.rollback()
            raise RealWorldError(
                f"Ban da co {count}/{savings.MAX_ACCOUNTS} so. Rut het so cu truoc."
            )
        cost = money + savings.OPEN_FEE
        if not await self._spend(db, uid, cost):
            await db.rollback()
            raise RealWorldError(
                f"Khong du tien. Can **{fmt_vnd(cost)}đ** ({fmt_vnd(money)}đ gui "
                f"+ {fmt_vnd(savings.OPEN_FEE)}đ phi mo so)."
            )
        await db.execute(
            "INSERT INTO savings(user_id,kind,balance,deposited,interest_paid,"
            "opened_at,last_interest) VALUES(?,?,?,?,0,?,?)",
            (int(uid), key, money, money, now, now),
        )
        async with db.execute("SELECT last_insert_rowid()") as cur:
            new_id = int((await cur.fetchone())[0])
        balance = await self._balance(db, uid)
        await self._log_txn(db, uid, "savings:open", -cost, balance,
                            f"Mo so {acc.name}, gui {fmt_vnd(money)}đ")
        await db.commit()
        await ctx.send(
            f"🎉 Đã mở sổ **{acc.emoji} {acc.name}** (#{new_id}) với **{fmt_vnd(money)}đ**.\n"
            f"Phí thủ tục: {fmt_vnd(savings.OPEN_FEE)}đ · "
            f"Lãi suất: {savings.annual_for(key, money):.2f}%/năm\n"
            f"Số dư còn lại: **{fmt_vnd(balance)}đ**"
        )

    async def _top_up_saving(self, ctx, db, uid: int, key: str, acc, money: int,
                             acc_id: int) -> None:
        """Nap them tien vao so da co (khong ton phi mo so)."""
        if not await self._spend(db, uid, money):
            await db.rollback()
            raise RealWorldError(f"Khong du tien. Can **{fmt_vnd(money)}đ**.")
        cur = await db.execute(
            "UPDATE savings SET balance=balance+?, deposited=deposited+?"
            " WHERE id=? AND user_id=?",
            (money, money, acc_id, int(uid)),
        )
        if cur.rowcount != 1:  # pragma: no cover - da kiem tra so o tren
            await db.rollback()
            raise RealWorldError("Khong tim thay so cua ban.")
        balance = await self._balance(db, uid)
        await self._log_txn(db, uid, "savings:deposit", -money, balance,
                            f"Nap them vao so #{acc_id}")
        await db.commit()
        async with db.execute(
            "SELECT balance FROM savings WHERE id=?", (acc_id,)
        ) as cur:
            new_balance = int((await cur.fetchone())[0])
        await ctx.send(
            f"💵 Đã gửi thêm **{fmt_vnd(money)}đ** vào sổ **#{acc_id}** ({acc.name}).\n"
            f"Số dư sổ: **{fmt_vnd(new_balance)}đ** · "
            f"Lãi suất: {savings.annual_for(key, new_balance):.2f}%/năm\n"
            f"Tiền trong ví: **{fmt_vnd(balance)}đ**"
        )

    # KHÔNG thêm alias ``rut``/``withdraw``: ``cogs/games.py`` đã dùng chúng cho
    # lệnh rút ngân hàng cũ. Trùng tên làm discord.py raise lúc add_cog -> bot
    # không khởi động được.
    @commands.command(name="rutso", aliases=["ruttien", "ruttienso"])
    async def withdraw(self, ctx: commands.Context, acc_id: int,
                       amount: str | None = None) -> None:
        """Rut tien khoi so cua ban. Rut truoc han se mat mot nua lai ky han."""
        uid = ctx.author.id
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    # Kiem tra so co thuoc ve NGUOI RA LENH — khong tin ``acc_id``
                    # do nguoi cho vao. Day la lo chiem doat so thuong gap nhat.
                    async with db.execute(
                        "SELECT kind,balance,opened_at FROM savings"
                        " WHERE id=? AND user_id=?",
                        (int(acc_id), int(uid)),
                    ) as cur:
                        row = await cur.fetchone()
                    if row is None:
                        await db.rollback()
                        return await ctx.send(
                            f"❌ Không tìm thấy sổ **#{acc_id}** của bạn."
                        )

                    key, balance, opened_at = str(row[0]), int(row[1]), float(row[2])
                    acc = savings.ACCOUNTS.get(key)
                    if acc is None:  # loai so da bi xoa khoi danh muc
                        await db.rollback()
                        return await ctx.send(f"❌ Sổ **#{acc_id}** không còn hợp lệ.")

                    money = self._parse_withdraw(amount, balance)
                    if money is None:
                        return await ctx.send(
                            f"❌ Số tiền không hợp lệ. Sổ hiện có **{fmt_vnd(balance)}đ**."
                        )
                    money = min(money, balance)
                    if money <= 0:
                        await db.rollback()
                        return await ctx.send("❌ Số tiền phải lớn hơn 0.")

                    penalty = savings.early_withdraw_penalty(key, money)
                    receive = money - penalty
                    if receive <= 0:
                        await db.rollback()
                        return await ctx.send(
                            "❌ Rút sớm mất toàn bộ lãi, không còn gì để nhận."
                        )
                    cur = await db.execute(
                        "UPDATE savings SET balance=balance-?"
                        " WHERE id=? AND user_id=? AND balance>=?",
                        (money, int(acc_id), int(uid), money),
                    )
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send("❌ Số dư sổ đã thay đổi, hãy thử lại.")
                    if money >= balance:
                        await db.execute("DELETE FROM savings WHERE id=?", (int(acc_id),))
                    wallet = await self._credit(db, uid, receive)
                    await self._log_txn(db, uid, "savings:withdraw", receive, wallet,
                                        f"Rut so #{acc_id} ({acc.name})")
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Rut so that bai (#%s)", acc_id)
                    raise RealWorldError(f"Giao dich loi: {type(exc).__name__}") from exc

        lines = [
            f"🏧 Đã rút **{fmt_vnd(money)}đ** từ sổ **#{acc_id}** ({acc.emoji} {acc.name}).",
            f"Nhận vào ví: **{fmt_vnd(receive)}đ** · Số dư: **{fmt_vnd(wallet)}đ**",
        ]
        if penalty > 0:
            left = savings.term_left(key, opened_at)
            lines.append(
                f"⚠️ Rút trước hạn — mất **{fmt_vnd(penalty)}đ** tiền lãi"
                + (f" (còn {left} ngày nữa mới đủ hạn)." if left else ".")
            )
        else:
            lines.append("✅ Sổ linh động — rút tự do, không mất lãi.")
        await ctx.send("\n".join(lines))

    @staticmethod
    def _parse_withdraw(text: str | None, balance: int) -> int | None:
        """Đọc số tiền rút; hỗ trợ ``all``/``tatca`` = rút hết."""
        if text is None:
            return balance
        raw = str(text).strip().lower().replace(".", "").replace(",", "")
        if raw in {"all", "tatca", "het", "max"}:
            return balance
        try:
            return int(raw)
        except ValueError:
            return None

    @commands.command(name="socuatoa", aliases=["listso", "taikhoan"])
    async def my_savings(self, ctx: commands.Context) -> None:
        """Xem toan bo so tiet kiem cua ban."""
        uid = ctx.author.id
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT id,kind,balance,interest_paid,opened_at"
                    " FROM savings WHERE user_id=? ORDER BY id",
                    (int(uid),),
                ) as cur:
                    rows = await cur.fetchall()

        if not rows:
            return await ctx.send(
                "Bạn chưa có sổ tiết kiệm nào. Xem `!moso` rồi mở bằng "
                "`!guitien <mã> <số tiền>`."
            )
        now = time.time()
        lines: list[str] = []
        total = 0
        total_interest = 0
        for acc_id, key, balance, interest, opened_at in rows:
            acc = savings.ACCOUNTS.get(str(key))
            name = acc.name if acc else str(key)
            emoji = acc.emoji if acc else "🏦"
            rate = savings.annual_for(str(key), int(balance))
            left = savings.term_left(str(key), float(opened_at), now)
            state = f"🔒 còn {left} ngày" if left else "🔓 tự do rút"
            lines.append(
                f"{emoji} **#{acc_id} {name}** — số dư **{fmt_vnd(balance)}đ** "
                f"({rate:.2f}%/năm) · {state}"
            )
            total += int(balance)
            total_interest += int(interest)
        embed = discord.Embed(
            title=f"🏦 SỔ TIẾT KIỆM — {ctx.author.display_name}",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        embed.set_footer(
            text=f"Tổng đang gửi: **{fmt_vnd(total)}đ** · "
                 f"Lãi đã nhận: **{fmt_vnd(total_interest)}đ** · "
                 f"Rút bằng `!rutso <id> [số tiền|all]`"
        )
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # CONG LAI — nen atomic, toi da 1 lan/ngay moi so
    # ------------------------------------------------------------------

    async def accrue_interest(self, now: float | None = None) -> int:
        """Cộng lãi cho mọi sổ đã qua ít nhất 1 ngày. Trả về số sổ được cộng.

        Bất biến quan trọng: mỗi sổ chỉ cộng **một lần cho mỗi ngày đã qua**,
        dựa vào mốc ``last_interest`` được dời tiến sau mỗi lần cộng. Nên spam
        lệnh, restart bot, hay gọi hàm này liên tục đều **không** sinh thêm
        tiền. Lãi là **lãi đơn** (``savings.interest_for``), không lãi kép.
        """
        current = time.time() if now is None else now
        credited = 0
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    async with db.execute(
                        "SELECT id,user_id,kind,balance,last_interest FROM savings"
                        " WHERE last_interest>0"
                    ) as cur:
                        rows = await cur.fetchall()
                    for acc_id, owner, key, balance, last in rows:
                        elapsed = int((current - float(last)) // 86_400)
                        if elapsed < 1 or int(balance) <= 0:
                            continue
                        advanced = float(last) + elapsed * 86_400
                        gain = savings.interest_for(str(key), int(balance), elapsed)
                        if gain > 0:
                            # Điều kiện ``balance=?`` chống cộng lãi hai lần
                            # nếu tiến trình khác sửa số dư sổ giữa chừng.
                            cur = await db.execute(
                                "UPDATE savings SET balance=balance+?,"
                                " interest_paid=interest_paid+?, last_interest=?"
                                " WHERE id=? AND balance=?",
                                (gain, gain, advanced, int(acc_id), int(balance)),
                            )
                            if cur.rowcount != 1:
                                continue
                            await self._log_txn(
                                db, int(owner), "savings:interest", gain, 0,
                                f"Lai so #{acc_id} ({key}) {elapsed} ngay",
                            )
                            credited += 1
                        else:
                            # Không có lãi (loại sổ đã bị xoá khỏi danh mục):
                            # vẫn dời mốc để không tính lại mãi mãi.
                            await db.execute(
                                "UPDATE savings SET last_interest=? WHERE id=?",
                                (advanced, int(acc_id)),
                            )
                    await db.commit()
                except Exception:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Cong lai that bai")
                    return credited
        if credited:
            log.info("Đã cộng lãi cho %d sổ tiết kiệm", credited)
        return credited

    @tasks.loop(hours=INTEREST_INTERVAL_HOURS)
    async def interest_loop(self) -> None:
        """Nền cộng lãi định kỳ — chỉ cộng khi đã thực sự qua 1 ngày."""
        try:
            await self.accrue_interest()
        except Exception:  # noqa: BLE001 - task nền không được để chết
            log.exception("interest_loop lỗi")

    @interest_loop.before_loop
    async def _before_interest(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    """Discord.py gọi hàm này để đăng ký cog."""
    await bot.add_cog(RealWorld(bot))

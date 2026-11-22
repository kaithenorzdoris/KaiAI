"""
KaiAI Ultimate Economy & Games Engine
======================================

A production-oriented virtual economy for Discord communities.

Design goals
------------
* Keep the legacy economy/company commands working.
* Make money mutations atomic so two simultaneous commands cannot mint money.
* Give the economy a large game catalog without requiring extra services.
* Keep all state in SQLite and survive bot restarts.
* Never use eval() for calculators or user supplied expressions.
* Use cooldowns on reward-generating commands.
* Keep the code dependency-light: discord.py + aiosqlite.

This economy uses virtual currency only. It is not connected to real-money
payments, cash-out systems, or financial services.
"""

from __future__ import annotations

import asyncio
import math
import random
import secrets
import statistics
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import aiosqlite
import discord
from discord.ext import commands

from kai_core.config import CONFIG
from kai_core.economy import EconomyError, validate_amount
from kai_core.idempotency import OnceGuard
from kai_core.logging_setup import get_logger

logger = get_logger("games")

#: Nằm trong KAI_DATA_DIR để volume Docker giữ được dữ liệu kinh tế.
DB_PATH = str(CONFIG.data_dir / "economy.db")
# --- Quy đổi theo mặt bằng giá Việt Nam --------------------------------------
# Mọi con số tiền dưới đây là **VNĐ thật** chứ không phải "đơn vị ảo": một lần
# ``!lamviec`` là một ngày công, ``!shop`` bán đúng giá ngoài đời, tiền khởi
# nghiệp đủ sống một tháng. Nhờ vậy thu nhập và giá cả luôn tương quan như
# đời thật (không còn cảnh "ô tô 50 triệu" rồi tích cóp vài ngày là mua được).
#: Tiền mặt ban đầu của người chơi mới (~1 tháng lương khởi điểm).
STARTING_BALANCE = 10_000_000
#: Tiền "điểm danh" một ngày — ăn sáng, gửi xe, xăng xe lặt vặt.
DAILY_BASE = 300_000
#: Thưởng cộng thêm mỗi ngày streak (tối đa 30 ngày) và mỗi level.
DAILY_STREAK_BONUS = 60_000
DAILY_LEVEL_BONUS = 25_000
#: Trần tiền điểm danh một ngày (chống lạm phát).
DAILY_MAX = 6_000_000
#: Lương một ngày công (thấp nhất — cao nhất) theo mặt bằng lao động phổ thông.
WORK_MIN = 200_000
WORK_MAX = 1_200_000

# --- Lãi suất ---------------------------------------------------------------
# Cơ sở + biên độ: lãi suất được **công bố theo ngày** (deterministic theo seed
# ngày) nên cả server thấy cùng một mức trong ngày, khác ngày khác lãi — giống
# ngân hàng công bố biểu lãi suất hằng ngày. Lãi kép tính theo từng ngày.
BANK_DAILY_RATE = 0.0025        # tiết kiệm (bank) — cơ sở 0.25%/ngày
BANK_DAILY_SPREAD = 0.0005      # ±0.05%/ngày
LOAN_DAILY_RATE = 0.05          # vay không bảo đảm — cơ sở 5%/ngày
LOAN_DAILY_SPREAD = 0.01        # ±1%/ngày
SECURED_DAILY_RATE = 0.03       # vay THẾ CHẤP — rẻ hơn (đúng nguyên tắc đời thật)
SECURED_DAILY_SPREAD = 0.005    # ±0.5%/ngày
COLLATERAL_LTV = 0.6            # cho vay tối đa 60% giá trị CP thế chấp
MIN_COLLATERAL_LOAN = 1_000_000   # khoản vay thế chấp nhỏ nhất (dưới mức này không mở hồ sơ)
MAX_INTEREST_DAYS = 3650        # trần số ngày tính lãi mỗi lần gọi (chống lũy tiến vô hạn)
# Thuế thu nhập cá nhân trên lãi tiết kiệm: đời thật Việt Nam **miễn thuế**
# lãi tiền gửi, nên ngưỡng chịu thuế đặt rất cao — chỉ đánh vào "đại gia gửi
# tiết kiệm" cho vui, phần lớn người chơi không bao giờ chạm tới.
SAVINGS_INTEREST_TAX = 0.05
SAVINGS_TAX_THRESHOLD = 1_000_000_000

# --- Cổ phiếu thị trường ----------------------------------------------------
MARKET_FEE = 0.01               # phí giao dịch 1% (mua và bán) — chống arbitrage
MARKET_TICK_COOLDOWN = 60.0     # giây giữa hai lần !market bơm/thu giá

MAX_BET = 1_000_000_000          # hạn mức một lần cược (1 tỷ)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def now() -> float:
    return time.time()


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def compact_number(value: int) -> str:
    """Human friendly number formatting while keeping exact accounting data."""
    value = int(value)
    sign = "-" if value < 0 else ""
    n = abs(value)
    if n >= 1_000_000_000_000:
        return f"{sign}{n / 1_000_000_000_000:.2f}T"
    if n >= 1_000_000_000:
        return f"{sign}{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}{n / 1_000:.2f}K"
    return f"{value:,}"


# ---------------------------------------------------------------------------
# Daily published interest rates (giống ngân hàng công bố biểu lãi theo ngày)
# ---------------------------------------------------------------------------


def day_index(ts: float | None = None) -> int:
    """Số ngày epoch — seed cho lãi suất công bố theo ngày."""
    return int((ts if ts is not None else now()) // 86_400)


def daily_rate(base: float, spread: float, day: int | None = None) -> float:
    """Lãi suất **công bố** cho một ngày cụ thể (deterministic).

    Cùng một ngày → cùng một lãi suất cho mọi user và mọi lần gọi (không có
    "roll lại" để kiếm lãi). Khác ngày → khác lãi. ``spread`` là biên độ
    dao động ± quanh ``base``. Lãi không bao giờ âm.
    """
    if day is None:
        day = day_index()
    rng = random.Random(f"kaiai-rate-{day}-{base!r}")
    return max(0.0, base + rng.uniform(-spread, spread))


def compound_daily(principal: int, base: float, spread: float, start_day: int, days: int) -> int:
    """Lãi kép tính theo **từng ngày**, mỗi ngày áp lãi suất công bố của ngày đó.

    Trả về tổng lãi tích lũy (principal + lãi đã capitalisation ở cuối mỗi ngày,
    làm tròn integer — ngân hàng thật cũng làm tròn theo ngày).
    ``days`` được chặn trần bởi caller (``MAX_INTEREST_DAYS``).
    """
    amount = int(principal)
    for offset in range(max(0, days)):
        rate = daily_rate(base, spread, start_day + offset)
        amount += int(amount * rate)  # capitalise hằng ngày, cắt lẻ mỗi ngày
    return amount - int(principal)




# ---------------------------------------------------------------------------
# Vat pham: bang gia tien, phan loai va mo ta tieng Viet
# ---------------------------------------------------------------------------

#: ``key -> (ten hien thi, gia tien, emoji, mo ta, nhom)``
#:
#: ``nhom`` quyet dinh hieu ung khi dung vat pham:
#: ``"suc_khoe"`` tang gia tri dinh ky, ``"tho"`` chi cong thuc, ``"de`` dung``.
#: Gia tien la VNĐ ao trong server, giong cach tien cua cac lenh khac.
#: Bảng giá theo **giá ngoài đời** (VNĐ): trà đá vỉa hè miễn phí, mũ bảo hiểm
#: vài trăm nghìn, xe máy vài chục triệu, ô tô vài trăm triệu, nhà phố vài tỷ.
#: Đây cũng là mốc để tính "giá trị tài sản" trong ``!taisan``.
ITEMS = {
    "oto":        ("Ô tô",                 800_000_000, ":red_car:", "Xe phổ thông đi làm hằng ngày", "de_dung"),
    "xemay":      ("Xe máy",                35_000_000, ":motorcycle:", "Phương tiện chính ở Việt Nam", "de_dung"),
    "dienthoai":  ("Điện thoại",             8_000_000, ":iphone:", "Liên lạc và giao dịch mọi nơi", "de_dung"),
    "maytinh":    ("Máy tính",              20_000_000, ":computer:", "Làm việc và học tập năng suất hơn", "suc_khoe"),
    "nha":        ("Nhà phố",            3_000_000_000, ":house:", "Tấc đất tấc vàng — tài sản lớn nhất đời người", "suc_khoe"),
    "dat":        ("Lô đất",            2_000_000_000, ":earth_africa:", "Đất nền để xây nhà hoặc chờ tăng giá", "suc_khoe"),
    "congao":     ("Bao gạo",                  350_000, ":rice:", "Bữa cơm của cả gia đình", "deo"),
    "nuoctra":    ("Nước trà đá",                    0, ":tea:", "Quán vỉa hè — miễn phí, uống cho vui", "de_dung"),
    "cap":        ("Mũ bảo hiểm",              250_000, ":shield:", "Rẻ nhất trong các loại bảo hiểm", "deo"),
    "thevip":     ("Thẻ VIP",                5_000_000, ":star:", "Giảm 20% chi phí lệnh", "deo"),
    "maytinhmo":  ("Máy tính mới nhất",     45_000_000, ":desktop_computer:", "Năng suất tối đa", "de_dung"),
    "dienthoaixa":("Điện thoại cao cấp",    32_000_000, ":telephone_receiver:", "Dòng máy đắt nhất trên thị trường", "de_dung"),
    "o_to_sang":  ("Ô tô sang",         3_500_000_000, ":blue_car:", "Xe hạng sang — tài sản dễ thấy nhất", "de_dung"),
}

#: Tang gia tri dinh ky moi ngay (tinh tren tong gia tri tai san, khong am).
DAILY_APPRECIATION = 0.0005


def item_name(key: str) -> str:
    """Tra ve ten hien thi cua vat pham; ``key`` la ban luon dung."""
    entry = ITEMS.get(key)
    return entry[0] if entry else key


def item_price(key: str) -> int:
    """Gia mua ban cua vat pham (0 neu khong ton tai)."""
    entry = ITEMS.get(key)
    return int(entry[1]) if entry else 0


def inventory_value(held: dict) -> int:
    """Tong gia tri don vi VAT pham dang so huu."""
    return sum(item_price(k) * int(n) for k, n in held.items() if int(n) > 0)


@dataclass(frozen=True)
class UserEconomy:
    user_id: int
    balance: int
    bank: int
    xp: int
    level: int
    streak: int = 0
    reputation: int = 0


# ---------------------------------------------------------------------------
# Interactive game views
# ---------------------------------------------------------------------------

class HelpPagination(discord.ui.View):
    """Paged economy menu. The author owns the interaction."""

    def __init__(self, embeds: list[discord.Embed], author_id: int):
        super().__init__(timeout=180)
        self.embeds = embeds
        self.author_id = author_id
        self.current_page = 0
        self.refresh_buttons()

    def refresh_buttons(self) -> None:
        """Update pagination buttons without overriding discord.py internals."""
        has_multiple_pages = len(self.embeds) > 1
        if len(self.children) >= 2:
            self.children[0].disabled = not has_multiple_pages
            self.children[1].disabled = not has_multiple_pages

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Menu này thuộc về người đã mở nó.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀ Trước", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.current_page = (self.current_page - 1) % len(self.embeds)
        self.refresh_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    @discord.ui.button(label="Sau ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.current_page = (self.current_page + 1) % len(self.embeds)
        self.refresh_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    @discord.ui.button(label="✖ Đóng", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class BetChoiceView(discord.ui.View):
    """Reusable choice view for two/three outcome games."""

    def __init__(self, ctx: commands.Context, cog: "Games", bet: int, choices: list[tuple[str, str]], title: str):
        super().__init__(timeout=45)
        self.ctx = ctx
        self.cog = cog
        self.bet = bet
        self.title = title
        self.resolved = False
        for index, (label, value) in enumerate(choices):
            button = discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.primary if index == 0 else discord.ButtonStyle.secondary,
                custom_id=f"kai_bet_{secrets.token_hex(5)}",
            )
            button.callback = self._callback(value)
            self.add_item(button)

    def _callback(self, value: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.ctx.author.id:
                return await interaction.response.send_message("❌ Đây không phải vé cược của bạn.", ephemeral=True)
            if self.resolved:
                return await interaction.response.send_message("⚠️ Ván này đã kết thúc.", ephemeral=True)
            self.resolved = True
            for child in self.children:
                child.disabled = True
            await self.cog.resolve_choice_bet(interaction, self.ctx.author.id, self.bet, value, self.title, self)
        return callback


class SlotsView(discord.ui.View):
    def __init__(self, ctx: commands.Context, cog: "Games", bet: int):
        super().__init__(timeout=25)
        self.ctx = ctx
        self.cog = cog
        self.bet = bet
        self.done = False
        # Chống double-click: khoá giữ nguyên suốt đời view (TTL > timeout),
        # nên kể cả 2 click cùng tick cũng chỉ một lần được xử lý (#28).
        self.guard = OnceGuard(ttl=60)

    @discord.ui.button(label="🎰 QUAY HŨ", style=discord.ButtonStyle.success)
    async def spin(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("❌ Máy này đang thuộc về người khác.", ephemeral=True)
        if self.done:
            return await interaction.response.send_message("⚠️ Máy đã quay.", ephemeral=True)
        if not await self.guard.acquire(f"slots:{interaction.user.id}:{self.bet}"):
            return await interaction.response.send_message("⚠️ Máy đang quay, chờ một chút.", ephemeral=True)
        self.done = True
        button.disabled = True
        await self.cog.resolve_slots(interaction, self.ctx.author.id, self.bet)


# ---------------------------------------------------------------------------
# Main Cog
# ---------------------------------------------------------------------------

class Games(commands.Cog):
    """KaiAI Economy: bank, jobs, games, progression and companies."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = DB_PATH
        self.logger = logger
        self._db_lock = asyncio.Lock()
        self.reward_cd = commands.CooldownMapping.from_cooldown(1, 45.0, commands.BucketType.user)
        self.work_cd = commands.CooldownMapping.from_cooldown(1, 900.0, commands.BucketType.user)
        self.daily_cd = commands.CooldownMapping.from_cooldown(1, 86_400.0, commands.BucketType.user)

    async def cog_load(self):
        await self.setup_db()

    # ------------------------------------------------------------------
    # Database schema + migration
    # ------------------------------------------------------------------

    async def setup_db(self):
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute("PRAGMA busy_timeout=5000")
                await db.executescript(
                    f"""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        balance INTEGER NOT NULL DEFAULT {STARTING_BALANCE} CHECK(balance >= 0),
                        bank INTEGER NOT NULL DEFAULT 0 CHECK(bank >= 0),
                        xp INTEGER NOT NULL DEFAULT 0 CHECK(xp >= 0),
                        level INTEGER NOT NULL DEFAULT 1 CHECK(level >= 1),
                        streak INTEGER NOT NULL DEFAULT 0 CHECK(streak >= 0),
                        reputation INTEGER NOT NULL DEFAULT 0,
                        last_daily REAL NOT NULL DEFAULT 0,
                        last_work REAL NOT NULL DEFAULT 0,
                        last_fish REAL NOT NULL DEFAULT 0,
                        last_crime REAL NOT NULL DEFAULT 0,
                        last_interest REAL NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS loans (
                        user_id INTEGER PRIMARY KEY,
                        amount INTEGER NOT NULL DEFAULT 0,
                        last_calc REAL NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS companies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE NOT NULL,
                        owner_id INTEGER NOT NULL,
                        total_shares INTEGER NOT NULL DEFAULT 1000,
                        price_per_share INTEGER NOT NULL,
                        cash INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS shares (
                        user_id INTEGER NOT NULL,
                        company_id INTEGER NOT NULL,
                        amount INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(user_id, company_id)
                    );
                    CREATE TABLE IF NOT EXISTS transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        amount INTEGER NOT NULL,
                        balance_after INTEGER NOT NULL,
                        created_at REAL NOT NULL,
                        note TEXT DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS game_stats (
                        user_id INTEGER NOT NULL,
                        game TEXT NOT NULL,
                        played INTEGER NOT NULL DEFAULT 0,
                        wins INTEGER NOT NULL DEFAULT 0,
                        losses INTEGER NOT NULL DEFAULT 0,
                        wagered INTEGER NOT NULL DEFAULT 0,
                        profit INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(user_id, game)
                    );
                    CREATE TABLE IF NOT EXISTS inventory (
                        user_id INTEGER NOT NULL,
                        item TEXT NOT NULL,
                        amount INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(user_id, item)
                    );
                    CREATE TABLE IF NOT EXISTS cooldowns (
                        user_id INTEGER NOT NULL,
                        key TEXT NOT NULL,
                        expires REAL NOT NULL,
                        PRIMARY KEY(user_id, key)
                    );
                    CREATE TABLE IF NOT EXISTS market (
                        symbol TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        price INTEGER NOT NULL,
                        change REAL NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS collateral (
                        user_id INTEGER PRIMARY KEY,
                        shares INTEGER NOT NULL DEFAULT 0 CHECK(shares >= 0),
                        company_id INTEGER NOT NULL,
                        pledged_value INTEGER NOT NULL DEFAULT 0,
                        amount INTEGER NOT NULL DEFAULT 0,
                        last_calc REAL NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS idx_transactions_user_time ON transactions(user_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_game_stats_game_profit ON game_stats(game, profit DESC);
                    CREATE INDEX IF NOT EXISTS idx_shares_company_amount ON shares(company_id, amount DESC);
                    CREATE TABLE IF NOT EXISTS market_holdings (
                        user_id INTEGER NOT NULL,
                        symbol TEXT NOT NULL,
                        shares INTEGER NOT NULL DEFAULT 0 CHECK(shares >= 0),
                        avg_cost INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(user_id, symbol)
                    );
                    """
                )
                await self._ensure_column(db, "users", "streak", "INTEGER NOT NULL DEFAULT 0")
                await self._ensure_column(db, "users", "reputation", "INTEGER NOT NULL DEFAULT 0")
                await self._ensure_column(db, "users", "last_daily", "REAL NOT NULL DEFAULT 0")
                await self._ensure_column(db, "users", "last_work", "REAL NOT NULL DEFAULT 0")
                await self._ensure_column(db, "users", "last_fish", "REAL NOT NULL DEFAULT 0")
                await self._ensure_column(db, "users", "last_crime", "REAL NOT NULL DEFAULT 0")
                await self._ensure_column(db, "users", "last_interest", "REAL NOT NULL DEFAULT 0")
                await self._seed_market(db)
                await db.commit()

    async def _ensure_column(self, db, table: str, column: str, definition: str):
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        if column not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def _seed_market(self, db):
        assets = [
            ("KAI", "KaiAI Technologies", 125_000),
            ("ROB", "Roblox Dynamics", 95_000),
            ("NOVA", "Nova Energy", 180_000),
            ("BYTE", "Byte Systems", 72_000),
            ("LUX", "Lux Entertainment", 150_000),
            ("OCEAN", "Oceanic Logistics", 64_000),
            ("ZEN", "Zen Foods", 51_000),
            ("APEX", "Apex Motors", 210_000),
        ]
        for symbol, name, price in assets:
            await db.execute(
                "INSERT OR IGNORE INTO market(symbol,name,price,change,updated_at) VALUES(?,?,?,?,?)",
                (symbol, name, price, 0.0, now()),
            )

    # ------------------------------------------------------------------
    # Economy primitives
    # ------------------------------------------------------------------

    def parse_amount(self, amount_str: str, reference_balance: int) -> Optional[int]:
        if not amount_str:
            return None
        text = str(amount_str).lower().strip().replace(",", "").replace(" ", "")
        if text in {"all", "tatca", "max"}:
            return int(reference_balance)
        if text in {"half", "nua", "50%"}:
            return int(reference_balance // 2)
        if text.endswith("%"):
            try:
                pct = float(text[:-1])
                return int(reference_balance * pct / 100)
            except ValueError:
                return None
        multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "t": 1_000_000_000_000}
        suffix = text[-1:] 
        try:
            if suffix in multipliers:
                raw = float(text[:-1]) * multipliers[suffix]
            else:
                raw = float(text)
            if not math.isfinite(raw):
                return None
            # validate_amount chặn: số âm, NaN/inf, float mất chính xác,
            # tràn số nguyên 64-bit (#27).
            return validate_amount(raw, minimum=0)
        except (TypeError, ValueError, OverflowError, EconomyError):
            return None

    def fmt_vnd(self, amount: int) -> str:
        return f"**{int(amount):,} VNĐ**"

    async def place_bet(self, user_id: int, amount: int, game: str) -> bool:
        """Tru tien cuoc **atomic** va tra ve True/False.

        **Ly do ton tai:** ``update_balance`` co the that bai (khong du tien do
        tranh chap giua hai lenh chay song song) nhung code cu van chay tiep va
        tra payout -> nguoi choi "thang" ma khong bao gio bi tru tien.

        Dung ham nay thay cho ``update_balance(-bet)`` de moi lenh cuoc deu
        dung mot duong ghi co khoa.
        """
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_user(db, user_id)
                async with db.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (amount, user_id, amount),
                ) as cur:
                    if cur.rowcount != 1:
                        await db.rollback()
                        return False
                async with db.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user_id,)
                ) as cur:
                    after = int((await cur.fetchone())[0])
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " VALUES(?,?,?,?,?,?)",
                    (user_id, f"bet:{game}", -amount, after, now(), game),
                )
                await db.commit()
        return True

    def xp_needed(self, level: int) -> int:
        return 500 + (level - 1) * 250

    async def _ensure_user(self, db, user_id: int):
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))

    async def get_user_data(self, user_id: int) -> dict:
        # Lãi ngân hàng + lãi vay tự tính khi dữ liệu được đọc (lazy, đúng chuẩn
        # ngân hàng: lãi chạy theo thời gian, không cần người dùng "khởi động").
        await self.apply_bank_interest(user_id)
        await self.apply_loan_interest(user_id)
        await self.apply_collateral_interest(user_id)
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await self._ensure_user(db, user_id)
                async with db.execute(
                    "SELECT balance,bank,xp,level,streak,reputation FROM users WHERE user_id=?",
                    (user_id,),
                ) as cur:
                    row = await cur.fetchone()
                await db.commit()
        return {
            "balance": row[0], "bank": row[1], "xp": row[2], "level": row[3],
            "streak": row[4], "reputation": row[5],
        }

    async def _log_economy(
        self,
        user_id: int,
        kind: str,
        amount: int,
        balance_before: int,
        balance_after: int,
        *,
        guild_id: int | None = None,
        reason: str = "",
    ) -> None:
        """Ghi biến động tiền vào ``economy_logs`` của Log Center (#18).

        Ghi log là việc phụ: **không được** làm hỏng giao dịch tiền. Mọi lỗi
        đều bị nuốt và chỉ ghi vào log server.
        """
        center = getattr(self.bot, "log_center", None)
        store = getattr(center, "store", None) if center is not None else None
        if store is None:
            return
        try:
            from kai_core.economy import new_transaction_id

            await store.log_economy(
                transaction_id=new_transaction_id(),
                user_id=user_id,
                kind=kind[:48],
                amount=int(amount),
                balance_before=int(balance_before),
                balance_after=int(balance_after),
                guild_id=guild_id,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Không ghi được log economy '%s': %s", kind, exc)

    async def update_balance(self, user_id: int, amount: int, kind: str = "adjustment", note: str = "") -> bool:
        """Atomically add/remove balance. Negative updates fail instead of going below zero."""
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await self._ensure_user(db, user_id)
                async with db.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user_id,)
                ) as cur:
                    before_row = await cur.fetchone()
                balance_before = int(before_row[0]) if before_row else 0

                async with db.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=? AND balance+? >= 0",
                    (int(amount), user_id, int(amount)),
                ) as cur:
                    changed = cur.rowcount == 1
                balance = balance_before
                if changed:
                    async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cur:
                        balance = (await cur.fetchone())[0]
                    await db.execute(
                        "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note) VALUES(?,?,?,?,?,?)",
                        (user_id, kind, int(amount), balance, now(), note),
                    )
                await db.commit()
        if changed:
            await self._log_economy(
                user_id, kind, amount, balance_before, balance, reason=note
            )
        return changed

    async def transfer(self, sender: int, receiver: int, amount: int) -> bool:
        if sender == receiver or amount <= 0:
            return False
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_user(db, sender)
                await self._ensure_user(db, receiver)
                # Guard atomic: chi tru khi that su du (chong 2 lenh chay
                # song song cung thay "du tien" -> in tien vo han).
                async with db.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (amount, sender, amount),
                ) as cur:
                    if cur.rowcount != 1:
                        await db.rollback()
                        return False
                async with db.execute("SELECT balance FROM users WHERE user_id=?", (sender,)) as cur:
                    row = await cur.fetchone()
                sender_before = int(row[0]) + amount if row else amount

                async with db.execute("SELECT balance FROM users WHERE user_id=?", (receiver,)) as cur:
                    receiver_row = await cur.fetchone()
                receiver_before = int(receiver_row[0]) if receiver_row else 0

                await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, receiver))
                await db.execute("INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note) SELECT ?, 'transfer_out', ?, balance, ?, ? FROM users WHERE user_id=?", (sender, -amount, now(), f"to:{receiver}", sender))
                await db.execute("INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note) SELECT ?, 'transfer_in', ?, balance, ?, ? FROM users WHERE user_id=?", (receiver, amount, now(), f"from:{sender}", receiver))
                await db.commit()

        # Ghi log **ngoài** transaction: lỗi log không được làm mất tiền.
        await self._log_economy(
            sender, "transfer_out", -amount, sender_before, sender_before - amount,
            reason=f"to:{receiver}",
        )
        await self._log_economy(
            receiver, "transfer_in", amount, receiver_before, receiver_before + amount,
            reason=f"from:{sender}",
        )
        return True

    async def add_xp(self, user_id: int, amount: int, source: str = "game") -> tuple[int, int]:
        amount = max(0, int(amount))
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await self._ensure_user(db, user_id)
                async with db.execute("SELECT xp,level FROM users WHERE user_id=?", (user_id,)) as cur:
                    xp, level = await cur.fetchone()
                xp += amount
                gained = 0
                while xp >= self.xp_needed(level):
                    xp -= self.xp_needed(level)
                    level += 1
                    gained += 1
                await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, level, user_id))
                await db.commit()
        return level, gained

    async def record_game(self, user_id: int, game: str, wagered: int, profit: int, win: bool):
        await self.add_xp(user_id, max(5, min(250, wagered // 10_000 + 10)), game)
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO game_stats(user_id,game,played,wins,losses,wagered,profit)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(user_id,game) DO UPDATE SET
                       played=played+1,wins=wins+excluded.wins,losses=losses+excluded.losses,
                       wagered=wagered+excluded.wagered,profit=profit+excluded.profit""",
                    (user_id, game, 1, 1 if win else 0, 0 if win else 1, wagered, profit),
                )
                await db.commit()

    async def validate_bet(self, ctx: commands.Context, amount_input: str, minimum: int = 100) -> Optional[int]:
        data = await self.get_user_data(ctx.author.id)
        amount = self.parse_amount(amount_input, data["balance"])
        if amount is None or amount < minimum:
            await ctx.send(f"❌ Cược phải từ **{minimum:,} VNĐ**. Ví hiện tại: {self.fmt_vnd(data['balance'])}")
            return None
        if amount > MAX_BET:
            await ctx.send(f"❌ Mức cược tối đa là {self.fmt_vnd(MAX_BET)}.")
            return None
        if amount > data["balance"]:
            await ctx.send(f"❌ Ví không đủ tiền. Bạn đang có {self.fmt_vnd(data['balance'])}.")
            return None
        return amount

    async def _cooldown_seconds(self, user_id: int, key: str, seconds: int) -> float:
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT expires FROM cooldowns WHERE user_id=? AND key=?", (user_id, key)) as cur:
                    row = await cur.fetchone()
                current = now()
                if row and row[0] > current:
                    return row[0] - current
                await db.execute(
                    "INSERT INTO cooldowns(user_id,key,expires) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET expires=excluded.expires",
                    (user_id, key, current + seconds),
                )
                await db.commit()
        return 0

    # ------------------------------------------------------------------
    # Bank, daily, work, progression
    # ------------------------------------------------------------------

    @commands.command(name="cash", aliases=["balance", "bal", "money"])
    async def cash(self, ctx, member: discord.Member = None):
        """Xem ví, ngân hàng, level và reputation."""
        target = member or ctx.author
        data = await self.get_user_data(target.id)
        embed = discord.Embed(title=f"💳 Ví điện tử — {target.display_name}", color=discord.Color.blurple())
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="💵 Ví", value=self.fmt_vnd(data["balance"]), inline=True)
        embed.add_field(name="🏦 Ngân hàng", value=self.fmt_vnd(data["bank"]), inline=True)
        embed.add_field(name="🌟 Net worth", value=self.fmt_vnd(data["balance"] + data["bank"]), inline=True)
        embed.add_field(name="⭐ Level", value=f"**{data['level']}** • {data['xp']}/{self.xp_needed(data['level'])} XP", inline=True)
        embed.add_field(name="🔥 Streak", value=f"**{data['streak']}** ngày", inline=True)
        embed.add_field(name="💎 Reputation", value=f"**{data['reputation']}**", inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="dep", aliases=["deposit"])
    async def deposit(self, ctx, amount_input: str):
        data = await self.get_user_data(ctx.author.id)
        amount = self.parse_amount(amount_input, data["balance"])
        if amount is None or amount <= 0 or amount > data["balance"]:
            return await ctx.send("❌ Số tiền gửi không hợp lệ hoặc vượt quá số dư ví.")
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "UPDATE users SET balance=balance-?, bank=bank+? WHERE user_id=? AND balance>=?",
                    (amount, amount, ctx.author.id, amount),
                ) as cur:
                    changed = cur.rowcount == 1
                if not changed:
                    # Số dư bị đổi giữa lúc đọc và lúc ghi → huỷ, không báo thành công.
                    await db.rollback()
                    return await ctx.send("❌ Gửi tiền thất bại: số dư ví đã thay đổi, hãy thử lại.")
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note) VALUES(?,?,?,?,?,?)",
                    (ctx.author.id, "deposit", -amount, data["balance"] - amount, now(), "to:bank"),
                )
                await db.commit()
        await self._log_economy(
            ctx.author.id, "deposit", -amount, data["balance"], data["balance"] - amount,
            reason="to:bank",
        )
        await ctx.send(f"🏦 Đã gửi {self.fmt_vnd(amount)} vào ngân hàng.")

    @commands.command(name="with", aliases=["withdraw", "rut"])
    async def withdraw(self, ctx, amount_input: str):
        data = await self.get_user_data(ctx.author.id)
        amount = self.parse_amount(amount_input, data["bank"])
        if amount is None or amount <= 0 or amount > data["bank"]:
            return await ctx.send("❌ Số tiền rút không hợp lệ hoặc vượt quá số dư ngân hàng.")
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "UPDATE users SET bank=bank-?, balance=balance+? WHERE user_id=? AND bank>=?",
                    (amount, amount, ctx.author.id, amount),
                ) as cur:
                    changed = cur.rowcount == 1
                if not changed:
                    await db.rollback()
                    return await ctx.send("❌ Rút tiền thất bại: số dư ngân hàng đã thay đổi, hãy thử lại.")
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note) VALUES(?,?,?,?,?,?)",
                    (ctx.author.id, "withdraw", amount, data["balance"] + amount, now(), "from:bank"),
                )
                await db.commit()
        await self._log_economy(
            ctx.author.id, "withdraw", amount, data["balance"], data["balance"] + amount,
            reason="from:bank",
        )
        await ctx.send(f"💵 Đã rút {self.fmt_vnd(amount)} về ví.")

    @commands.command(name="pay", aliases=["give", "chuyentien"])
    async def pay(self, ctx, member: discord.Member, amount_input: str):
        data = await self.get_user_data(ctx.author.id)
        amount = self.parse_amount(amount_input, data["balance"])
        if amount is None or amount <= 0:
            return await ctx.send("❌ Số tiền không hợp lệ.")
        if await self.transfer(ctx.author.id, member.id, amount):
            await ctx.send(f"💸 {ctx.author.mention} → {member.mention}: {self.fmt_vnd(amount)}")
        else:
            await ctx.send("❌ Chuyển tiền thất bại: ví không đủ hoặc người nhận không hợp lệ.")

    @commands.command(name="daily", aliases=["diemdanh"])
    async def daily(self, ctx):
        """Daily reward có streak, bonus level và reputation."""
        user_id = ctx.author.id
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await self._ensure_user(db, user_id)
                async with db.execute("SELECT last_daily,streak,level,balance FROM users WHERE user_id=?", (user_id,)) as cur:
                    last, streak, level, before_balance = await cur.fetchone()
                current = now()
                if last and current - last < 86_400:
                    remaining = 86_400 - (current - last)
                    return await ctx.send(f"⏳ Daily đã nhận. Còn khoảng **{int(remaining//3600)}h {int((remaining%3600)//60)}m**.")
                if last and current - last <= 172_800:
                    streak += 1
                else:
                    streak = 1
                reward = (DAILY_BASE + min(streak, 30) * DAILY_STREAK_BONUS
                          + level * DAILY_LEVEL_BONUS)
                reward = min(reward, DAILY_MAX)
                await db.execute("UPDATE users SET balance=balance+?,last_daily=?,streak=?,reputation=reputation+1 WHERE user_id=?", (reward, current, streak, user_id))
                await db.commit()
                after_balance = int(before_balance) + reward
        await self._log_economy(
            user_id, "daily", reward, before_balance, after_balance,
            reason=f"streak={streak} level={level}",
        )
        new_level, gained = await self.add_xp(user_id, 75 + streak * 5, "daily")
        msg = f"🎁 Daily: +{self.fmt_vnd(reward)} • 🔥 streak **{streak}**"
        if gained:
            msg += f"\n🆙 Bạn lên **Level {new_level}**!"
        await ctx.send(msg)

    @commands.command(name="work", aliases=["lamviec", "job"])
    async def work(self, ctx):
        """Làm việc 15 phút/lần, chọn nghề ngẫu nhiên."""
        left = await self._cooldown_seconds(ctx.author.id, "work", 900)
        if left:
            return await ctx.send(f"⏳ Bạn vừa làm việc. Nghỉ thêm **{int(left//60)} phút**.")
        jobs = [
            ("👨‍💻 Lập trình viên", 700_000, 2_500_000), ("🎨 Designer", 500_000, 1_800_000),
            ("🚚 Tài xế", 350_000, 900_000), ("🍜 Chủ quán ăn", 250_000, 1_500_000),
            ("🧑‍🚀 Kỹ sư không gian", 800_000, 3_000_000), ("🎮 Game Developer", 600_000, 2_200_000),
            ("📈 Trader", 300_000, 5_000_000), ("🛠️ Kỹ thuật viên", 300_000, 800_000),
        ]
        job, low, high = random.choice(jobs)
        data = await self.get_user_data(ctx.author.id)
        reward = random.randint(low, high) + data["level"] * 50_000
        await self.update_balance(ctx.author.id, reward, "work", job)
        await self.add_xp(ctx.author.id, 45, "work")
        await ctx.send(f"💼 Bạn làm nghề **{job}** và kiếm được {self.fmt_vnd(reward)}!")

    # ------------------------------------------------------------------
    # Gambling/game settlement
    # ------------------------------------------------------------------

    async def resolve_choice_bet(self, interaction: discord.Interaction, user_id: int, bet: int, choice: str, game: str, view: discord.ui.View):
        # Guard atomic: khong dat duoc cuoc -> dung lai, khong tra thuong.
        if not await self.place_bet(user_id, bet, game):
            failed = discord.Embed(
                title="\U0001f6ab Khong dat duoc cuoc",
                description="So du khong du de cuoc. Khong bi tru tien.",
                color=discord.Color.red(),
            )
            return await interaction.response.edit_message(embed=failed, view=view)
        if game == "Tài Xỉu":
            dice = [random.randint(1, 6) for _ in range(3)]
            total = sum(dice)
            result = "TÀI" if total >= 11 else "XỈU"
            win = choice.upper() == result
            multiplier = 1.92
            text = f"🎲 {dice[0]} • {dice[1]} • {dice[2]} = **{total}** → **{result}**"
        elif game == "Chẵn Lẻ":
            number = random.randint(1, 100)
            result = "CHẴN" if number % 2 == 0 else "LẺ"
            win = choice.upper() == result
            multiplier = 1.92
            text = f"🔢 Số bí mật: **{number}** → **{result}**"
        elif game == "Đồng Xu":
            result = random.choice(["NGỬA", "SẤP"])
            win = choice.upper() == result
            multiplier = 1.90
            text = f"🪙 Đồng xu rơi: **{result}**"
        elif game == "Đua Ngựa":
            result = random.choice(["horse", "tiger", "wolf", "eagle"])
            win = choice == result
            multiplier = 3.70
            text = f"🏁 Nhà vô địch: **{result.upper()}**"
        else:
            result = random.choice(["A", "DRAW", "B"])
            win = choice == result
            multiplier = 2.80 if result == "DRAW" else 1.90
            text = f"🏟️ Kết quả: **{result}**"
        payout = int(bet * multiplier) if win else 0
        profit = payout - bet
        if payout:
            await self.update_balance(user_id, payout, f"win:{game}", "payout")
        await self.record_game(user_id, game, bet, profit, win)
        embed = discord.Embed(title=f"🎮 {game.upper()}", color=discord.Color.green() if win else discord.Color.red())
        embed.description = text
        embed.add_field(name="Kết quả", value=(f"🎉 **THẮNG** +{self.fmt_vnd(profit)}" if win else f"💸 **THUA** -{self.fmt_vnd(bet)}"))
        await interaction.response.edit_message(embed=embed, view=view)

    async def resolve_slots(self, interaction: discord.Interaction, user_id: int, bet: int):
        # Guard atomic: khong dat duoc cuoc -> dung lai, khong tra thuong.
        if not await self.place_bet(user_id, bet, "slots"):
            failed = discord.Embed(
                title="\U0001f6ab Khong dat duoc cuoc",
                description="So du khong du de cuoc. Khong bi tru tien.",
                color=discord.Color.red(),
            )
            return await interaction.response.edit_message(embed=failed, view=None)
        symbols = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣", "🪙"]
        reels = [random.choice(symbols) for _ in range(3)]
        if reels[0] == reels[1] == reels[2] == "7️⃣":
            multiplier = 15
        elif reels[0] == reels[1] == reels[2]:
            multiplier = 8
        elif len(set(reels)) == 2:
            multiplier = 2
        elif "💎" in reels:
            multiplier = 1
        else:
            multiplier = 0
        payout = bet * multiplier
        if payout:
            await self.update_balance(user_id, payout, "win:slots", "slots payout")
        profit = payout - bet
        await self.record_game(user_id, "Slots", bet, profit, payout > 0)
        embed = discord.Embed(title="🎰 KAI SLOTS", color=discord.Color.gold() if payout else discord.Color.red())
        embed.description = f"# {'  '.join(reels)}"
        embed.add_field(name="Hệ số", value=f"**x{multiplier}**")
        embed.add_field(name="Kết quả", value=(f"💰 +{self.fmt_vnd(payout - bet)}" if payout else f"💸 -{self.fmt_vnd(bet)}"))
        await interaction.response.edit_message(embed=embed, view=None)

    @commands.command(name="taixiu", aliases=["tx"])
    async def play_taixiu(self, ctx, bet_input: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        embed = discord.Embed(title="🎲 BÀN TÀI XỈU", description=f"Cược: {self.fmt_vnd(bet)}\nChọn cửa:", color=discord.Color.dark_theme())
        await ctx.send(embed=embed, view=BetChoiceView(ctx, self, bet, [("🔥 TÀI", "TÀI"), ("❄️ XỈU", "XỈU")], "Tài Xỉu"))

    @commands.command(name="chanle", aliases=["cl", "oddeven"])
    async def chan_le(self, ctx, bet_input: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        await ctx.send(embed=discord.Embed(title="🔢 CHẴN / LẺ", description=f"Cược {self.fmt_vnd(bet)}", color=discord.Color.blurple()), view=BetChoiceView(ctx, self, bet, [("⚪ CHẴN", "CHẴN"), ("⚫ LẺ", "LẺ")], "Chẵn Lẻ"))

    @commands.command(name="coinflip", aliases=["cf", "dongxu"])
    async def coinflip(self, ctx, bet_input: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        await ctx.send(embed=discord.Embed(title="🪙 ĐỒNG XU", description=f"Cược {self.fmt_vnd(bet)}", color=discord.Color.gold()), view=BetChoiceView(ctx, self, bet, [("🦅 NGỬA", "NGỬA"), ("🌑 SẤP", "SẤP")], "Đồng Xu"))

    @commands.command(name="slots", aliases=["slot", "quayhu"])
    async def slots(self, ctx, bet_input: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        await ctx.send(embed=discord.Embed(title="🎰 KAI SLOTS", description=f"Cược: {self.fmt_vnd(bet)}\nBấm nút để quay.", color=discord.Color.gold()), view=SlotsView(ctx, self, bet))

    @commands.command(name="duangua", aliases=["duathu", "dn"])
    async def horse_race(self, ctx, bet_input: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        horses = [("🐎 Ngựa", "horse"), ("🐯 Hổ", "tiger"), ("🐺 Sói", "wolf"), ("🦅 Đại Bàng", "eagle")]
        await ctx.send(embed=discord.Embed(title="🏁 KAI GRAND RACE", description=f"Cược {self.fmt_vnd(bet)}\nChọn vận động viên:", color=discord.Color.green()), view=BetChoiceView(ctx, self, bet, [(a, b) for a,b in horses], "Đua Ngựa"))

    @commands.command(name="bongda", aliases=["bd", "football"])
    async def football(self, ctx, bet_input: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        teams = ["Kai United", "Nova FC", "Cyber City", "Dragon FC", "Galaxy 04", "Titan Club", "Phoenix FC", "Ocean XI", "Royal Stars", "Apex FC"]
        a, b = random.sample(teams, 2)
        await ctx.send(embed=discord.Embed(title="⚽ KAI FOOTBALL", description=f"**{a}** 🆚 **{b}**\nCược: {self.fmt_vnd(bet)}", color=discord.Color.blurple()), view=BetChoiceView(ctx, self, bet, [(f"🏠 {a}", "A"), ("🤝 HÒA", "DRAW"), (f"✈️ {b}", "B")], "Bóng Đá"))

    # ------------------------------------------------------------------
    # More mini-games: all use virtual currency and server-side settlement.
    # ------------------------------------------------------------------

    @commands.command(name="dice", aliases=["xucxac"])
    async def dice_game(self, ctx, bet_input: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        if not await self.place_bet(ctx.author.id, bet, 'dice'):
            return await ctx.send("\u274c Khong du tien de cuoc (so du vua thay doi).")
        mine, bot_roll = random.randint(1,6), random.randint(1,6)
        if mine > bot_roll:
            payout = bet * 2; profit = bet; win = True
        elif mine == bot_roll:
            payout = bet; profit = 0; win = False
        else:
            payout = 0; profit = -bet; win = False
        if payout: await self.update_balance(ctx.author.id, payout, "win:dice")
        await self.record_game(ctx.author.id, "Dice", bet, profit, win)
        await ctx.send(f"🎲 Bạn: **{mine}** | Kai: **{bot_roll}** → " + (f"🎉 +{self.fmt_vnd(profit)}" if win else (f"🤝 Hoàn cược" if payout else f"💸 -{self.fmt_vnd(bet)}")))

    @commands.command(name="highlow", aliases=["caoha"])
    async def high_low(self, ctx, bet_input: str, choice: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        choice = choice.lower()
        if choice not in {"cao", "thap", "cao hơn", "thấp"}:
            return await ctx.send("❌ Chọn `cao` hoặc `thap`.")
        if not await self.place_bet(ctx.author.id, bet, 'highlow'):
            return await ctx.send("\u274c Khong du tien de cuoc (so du vua thay doi).")
        a, b = random.randint(1,13), random.randint(1,13)
        result = "cao" if b > a else "thap" if b < a else "hoa"
        win = choice.startswith(result) if result != "hoa" else False
        payout = bet * 2 if win else (bet if result == "hoa" else 0)
        if payout: await self.update_balance(ctx.author.id, payout, "payout:highlow")
        profit = payout - bet
        await self.record_game(ctx.author.id, "HighLow", bet, profit, win)
        await ctx.send(f"🃏 Lá đầu **{a}**, lá sau **{b}** → **{result.upper()}**. " + (f"🎉 +{self.fmt_vnd(profit)}" if win else f"💸 {self.fmt_vnd(profit)}"))

    @commands.command(name="rps", aliases=["keobuabao"])
    async def rps(self, ctx, bet_input: str, choice: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        choice = choice.lower()
        mapping = {"keo":"✂️", "bua":"🪨", "bao":"📄"}
        if choice not in mapping:
            return await ctx.send("❌ Chọn `keo`, `bua` hoặc `bao`.")
        if not await self.place_bet(ctx.author.id, bet, 'rps'):
            return await ctx.send("\u274c Khong du tien de cuoc (so du vua thay doi).")
        ai = random.choice(list(mapping))
        wins = {("keo","bao"),("bua","keo"),("bao","bua")}
        if choice == ai: payout, win = bet, False
        elif (choice, ai) in wins: payout, win = bet*2, True
        else: payout, win = 0, False
        if payout: await self.update_balance(ctx.author.id, payout, "payout:rps")
        profit = payout - bet
        await self.record_game(ctx.author.id, "RPS", bet, profit, win)
        await ctx.send(f"✊ Bạn {mapping[choice]} vs Kai {mapping[ai]} → " + (f"🎉 +{self.fmt_vnd(profit)}" if win else ("🤝 Hòa" if payout else f"💸 -{self.fmt_vnd(bet)}")))

    @commands.command(name="blackjack", aliases=["bj"])
    async def blackjack(self, ctx, bet_input: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        # Lightweight single-command blackjack simulation. It intentionally has
        # no multi-message state, so restarts cannot leave a locked table.
        if not await self.place_bet(ctx.author.id, bet, 'blackjack'):
            return await ctx.send("\u274c Khong du tien de cuoc (so du vua thay doi).")
        def draw(): return random.randint(2,11)
        player = draw() + draw()
        dealer = draw() + draw()
        while player < 17:
            player += draw()
        while dealer < 17:
            dealer += draw()
        if player > 21 or (dealer <= 21 and dealer >= player):
            payout, win = (0, False)
        else:
            payout, win = (bet * 2, True)
        if payout: await self.update_balance(ctx.author.id, payout, "payout:blackjack")
        profit = payout - bet
        await self.record_game(ctx.author.id, "Blackjack", bet, profit, win)
        result = "BUST" if player > 21 else "WIN" if win else "LOSE"
        await ctx.send(f"🃏 **BLACKJACK** — Bạn **{player}**, Dealer **{dealer}** → **{result}** | " + (f"🎉 +{self.fmt_vnd(profit)}" if win else f"💸 -{self.fmt_vnd(bet)}"))

    @commands.command(name="roulette", aliases=["rl"])
    async def roulette(self, ctx, bet_input: str, choice: str):
        bet = await self.validate_bet(ctx, bet_input)
        if bet is None: return
        choice = choice.lower()
        if choice not in {"red", "black", "do", "den", "odd", "even", "le", "chan"}:
            return await ctx.send("❌ Chọn `red/black`, `do/den` hoặc `odd/even`.")
        if not await self.place_bet(ctx.author.id, bet, 'roulette'):
            return await ctx.send("\u274c Khong du tien de cuoc (so du vua thay doi).")
        number = random.randint(0,36)
        red_numbers = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
        color = "green" if number == 0 else "red" if number in red_numbers else "black"
        # Chon chinh xac bang == : startswith() sai vi "den".startswith("de") True.
        win = (
            (choice in {"red", "do"} and color == "red")
            or (choice in {"black", "den"} and color == "black")
            or (choice in {"odd", "le"} and number is not None and number % 2 == 1)
            or (choice in {"even", "chan"} and number is not None and number % 2 == 0)
        )
        payout = bet * 2 if win else 0
        if payout: await self.update_balance(ctx.author.id, payout, "payout:roulette")
        profit = payout - bet
        await self.record_game(ctx.author.id, "Roulette", bet, profit, win)
        await ctx.send(f"🎡 Roulette: **{number} / {color.upper()}** → " + (f"🎉 +{self.fmt_vnd(profit)}" if win else f"💸 -{self.fmt_vnd(bet)}"))

    # ------------------------------------------------------------------
    # Adventure/economy games
    # ------------------------------------------------------------------

    @commands.command(name="fish", aliases=["cauca"])
    async def fish(self, ctx):
        left = await self._cooldown_seconds(ctx.author.id, "fish", 180)
        if left:
            return await ctx.send(f"🎣 Hãy chờ **{int(left)}s** để thả câu tiếp.")
        catches = [
            ("🐟 Cá rô", 50_000, 120_000), ("🐠 Cá nhiệt đới", 100_000, 300_000),
            ("🦑 Mực khổng lồ", 200_000, 600_000), ("🦈 Cá mập", 400_000, 1_200_000),
            ("💎 Hòm kho báu", 1_000_000, 5_000_000), ("🗑️ Rác biển", 0, 0),
        ]
        item, low, high = random.choice(catches)
        reward = random.randint(low, high) if high else 0
        if reward:
            await self.update_balance(ctx.author.id, reward, "fish", item)
            await self.add_xp(ctx.author.id, 35, "fish")
            await ctx.send(f"🎣 Bạn câu được **{item}** và bán lấy {self.fmt_vnd(reward)}!")
        else:
            await ctx.send("🎣 Bạn kéo lên... một túi rác. Đại dương vẫn ổn chứ? 😭")

    @commands.command(name="mine", aliases=["daquy", "khaiquang"])
    async def mine(self, ctx):
        left = await self._cooldown_seconds(ctx.author.id, "mine", 240)
        if left:
            return await ctx.send(f"⛏️ Mỏ đang hồi phục. Chờ **{int(left)}s**.")
        finds = [("🪨 Đá", 50_000, 150_000), ("🪙 Vàng", 200_000, 800_000), ("💎 Kim cương", 1_000_000, 6_000_000), ("🟣 Ruby", 500_000, 3_000_000), ("🕳️ Không có gì", 0, 0)]
        item, low, high = random.choice(finds)
        reward = random.randint(low, high) if high else 0
        if reward:
            await self.update_balance(ctx.author.id, reward, "mine", item)
            await self.add_xp(ctx.author.id, 45, "mine")
            await ctx.send(f"⛏️ Đào được **{item}** → +{self.fmt_vnd(reward)}")
        else:
            await ctx.send("⛏️ Cuốc trúng đá. Không có kho báu hôm nay!")

    @commands.command(name="crime", aliases=["trotoi"])
    async def crime(self, ctx):
        """Lam viec mau: 62% thanh cong, bi bat thi nop phat.

        **Bao mat:** tien phat duoc cat bang ``UPDATE ... WHERE balance>=?``
        trong transaction. Khong cat bang ``min(fine, balance)`` doc ngoai
        transaction, vi hai lenh chay song song se dung so du cu va phat
        vuot so du -> so du am.
        """
        left = await self._cooldown_seconds(ctx.author.id, "crime", 900)
        if left:
            return await ctx.send(
                f"\ud83d\udea4 Canh sat dang de y ban. Cho **{int(left // 60)} phut**."
            )
        user_id = ctx.author.id
        if random.random() < 0.62:
            data = await self.get_user_data(user_id)
            reward = random.randint(1_000_000, 6_000_000) + self.get_level_bonus(data)
            await self.update_balance(user_id, reward, "crime", "successful heist")
            await self.add_xp(user_id, 60, "crime")
            return await ctx.send(
                f"\ud83d\udd76 Phi vu thanh cong! Ban bo tui {self.fmt_vnd(reward)}."
            )

        fine = random.randint(500_000, 3_000_000)
        paid = 0
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_user(db, user_id)
                # Cat bao nhieu nhat trong so du hien tai, khong duoc vuot.
                async with db.execute(
                    "UPDATE users SET balance=balance-?"
                    " WHERE user_id=? AND balance>=?",
                    (fine, user_id, fine),
                ) as cur:
                    if cur.rowcount == 1:
                        paid = fine
                    else:
                        # Khong du tien thi cat het so du con lai.
                        async with db.execute(
                            "UPDATE users SET balance=0 WHERE user_id=?", (user_id,)
                        ) as cur2:
                            async with db.execute(
                                "SELECT balance FROM users WHERE user_id=?", (user_id,)
                            ) as c3:
                                paid = int((await c3.fetchone())[0])
                async with db.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user_id,)
                ) as cur:
                    after = int((await cur.fetchone())[0])
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " VALUES(?,?,?,?,?,?)",
                    (user_id, "crime_fine", -paid, after, now(), "caught"),
                )
                await db.commit()

        await self._log_economy(user_id, "crime_fine", -paid, after + paid, after, reason="bi bat")
        await ctx.send(f"\ud83d\udea8 BI BAT! Tien phat: {self.fmt_vnd(paid)}.")

    def get_level_bonus(self, data: dict) -> int:
        return min(5_000_000, data.get("level", 1) * 100_000)

    @commands.command(name="quest", aliases=["nhiemvu"])
    async def quest(self, ctx):
        """Daily quest ngẫu nhiên, phần thưởng lớn hơn work."""
        left = await self._cooldown_seconds(ctx.author.id, "quest", 86_400)
        if left:
            return await ctx.send(f"📜 Nhiệm vụ hôm nay đã hoàn thành. Còn **{int(left//3600)}h**.")
        quests = [
            ("Giao hàng xuyên thành phố", 1_800_000), ("Bảo vệ server", 2_400_000),
            ("Săn kho báu", 3_000_000), ("Viết 100 dòng code", 2_200_000),
            ("Xây doanh nghiệp", 3_600_000), ("Đánh bại Kai trong game", 3_200_000),
        ]
        task, base = random.choice(quests)
        data = await self.get_user_data(ctx.author.id)
        reward = base + data["level"] * 100_000
        await self.update_balance(ctx.author.id, reward, "quest", task)
        await self.add_xp(ctx.author.id, 150, "quest")
        await ctx.send(f"📜 **Quest hoàn thành:** {task}\n💰 +{self.fmt_vnd(reward)} • ⭐ +150 XP")

    # ------------------------------------------------------------------
    # Loans
    # ------------------------------------------------------------------

    async def apply_loan_interest(self, user_id: int):
        """Áp lãi vay **lãi kép theo ngày** với lãi suất công bố từng ngày.

        Mỗi ngày trôi qua áp đúng lãi suất của ngày đó (seed theo ngày — cả
        server thấy cùng biểu lãi). Tránh tính lại nếu chưa trôi qua ngày mới.
        """
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT amount,last_calc FROM loans WHERE user_id=?", (user_id,)) as cur:
                    row = await cur.fetchone()
                if not row or row[0] <= 0:
                    return
                amount, last_calc = row
                if last_calc <= 0:
                    # Khoản vay cũ chưa có mốc — đặt mốc từ bây giờ.
                    await db.execute("UPDATE loans SET last_calc=? WHERE user_id=?", (now(), user_id))
                    await db.commit()
                    return
                elapsed_days = int((now() - last_calc) // 86_400)
                if elapsed_days <= 0:
                    return
                elapsed_days = min(elapsed_days, MAX_INTEREST_DAYS)
                interest = compound_daily(
                    amount, LOAN_DAILY_RATE, LOAN_DAILY_SPREAD,
                    day_index(last_calc), elapsed_days,
                )
                await db.execute(
                    "UPDATE loans SET amount=?,last_calc=? WHERE user_id=?",
                    (amount + interest, last_calc + elapsed_days * 86_400, user_id),
                )
                await db.commit()

    async def apply_bank_interest(self, user_id: int):
        """Tiền trong ngân hàng **sinh lãi hằng ngày** (lãi kép, lãi suất công bố theo ngày).

        Gọi ``get_user_data`` là phí bút tự vào — ``last_interest`` trên ``users``
        lưu mốc cuối cùng đã cộng lãi (lần đầu: đặt mốc = bây giờ, không cộng lùi).
        """
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await self._ensure_user(db, user_id)
                async with db.execute("SELECT bank,last_interest FROM users WHERE user_id=?", (user_id,)) as cur:
                    row = await cur.fetchone()
                if not row:
                    return
                bank, last_interest = row
                if bank <= 0:
                    # Chưa có tiền gửi → chỉ giữ mốc mới nhất.
                    if last_interest <= 0:
                        await db.execute("UPDATE users SET last_interest=? WHERE user_id=?", (now(), user_id))
                        await db.commit()
                    return
                if last_interest <= 0:
                    await db.execute("UPDATE users SET last_interest=? WHERE user_id=?", (now(), user_id))
                    await db.commit()
                    return
                elapsed_days = int((now() - last_interest) // 86_400)
                if elapsed_days <= 0:
                    return
                elapsed_days = min(elapsed_days, MAX_INTEREST_DAYS)
                interest = compound_daily(
                    bank, BANK_DAILY_RATE, BANK_DAILY_SPREAD,
                    day_index(last_interest), elapsed_days,
                )
                # Thue thu nhap ca nhan tren phan lai VUOT nguong: 5% tren
                # phan vuot. Phan duoi nguong duoc mien thue.
                taxable = max(0, bank - SAVINGS_TAX_THRESHOLD)
                taxable_interest = 0
                if taxable > 0:
                    taxable_interest = compound_daily(
                        taxable, BANK_DAILY_RATE, BANK_DAILY_SPREAD,
                        day_index(last_interest), elapsed_days,
                    )
                tax = int(taxable_interest * SAVINGS_INTEREST_TAX)
                net = interest - tax
                if net > 0:
                    await db.execute(
                        "UPDATE users SET bank=bank+? WHERE user_id=? AND bank>=0",
                        (net, user_id),
                    )
                if tax > 0:
                    await self._log_economy(
                        user_id, "savings_tax", -tax,
                        bank, bank + net,
                        reason=f"thue lai {int(SAVINGS_INTEREST_TAX * 100)}%",
                    )
                await db.execute(
                    "UPDATE users SET last_interest=? WHERE user_id=?",
                    (last_interest + elapsed_days * 86_400, user_id),
                )
                await db.commit()

    async def apply_collateral_interest(self, user_id: int):
        """Áp lãi cho khoản vay thế chấp — lãi suất secured (thấp hơn vay thường)."""
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT amount,last_calc FROM collateral WHERE user_id=?", (user_id,)
                ) as cur:
                    row = await cur.fetchone()
                if not row or row[0] <= 0:
                    return
                amount, last_calc = row
                if last_calc <= 0:
                    await db.execute("UPDATE collateral SET last_calc=? WHERE user_id=?", (now(), user_id))
                    await db.commit()
                    return
                elapsed_days = int((now() - last_calc) // 86_400)
                if elapsed_days <= 0:
                    return
                elapsed_days = min(elapsed_days, MAX_INTEREST_DAYS)
                interest = compound_daily(
                    amount, SECURED_DAILY_RATE, SECURED_DAILY_SPREAD,
                    day_index(last_calc), elapsed_days,
                )
                await db.execute(
                    "UPDATE collateral SET amount=?,last_calc=? WHERE user_id=?",
                    (amount + interest, last_calc + elapsed_days * 86_400, user_id),
                )
                await db.commit()

    @commands.command(name="vaytien", aliases=["loan"])
    async def loan(self, ctx, amount: str):
        """Vay tien theo han muc theo level. Chi duoc mot khoan vay tai mot luc.

        **Bao mat:** kiem tra "da co no chua" nam TRONG transaction va dung
        ``INSERT ... SELECT ... WHERE NOT EXISTS``. Neu kiem tra nam ngoai
        transaction, hai lenh vay chay song song se cung thay "chua co no" ->
        vay duoc nhieu lan han muc (in tien vo han).
        """
        await self.apply_loan_interest(ctx.author.id)
        data = await self.get_user_data(ctx.author.id)
        max_loan = data["level"] * 5_000_000
        amt = self.parse_amount(amount, max_loan)
        if amt is None or amt <= 0 or amt > max_loan:
            return await ctx.send(f"\u274c Han muc Level {data['level']}: {self.fmt_vnd(max_loan)}")

        user_id = ctx.author.id
        existing = 0
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_user(db, user_id)
                async with db.execute(
                    "SELECT amount FROM loans WHERE user_id=?", (user_id,)
                ) as cur:
                    row = await cur.fetchone()
                existing = int(row[0]) if row else 0
                if existing > 0:
                    await db.rollback()
                    return await ctx.send(
                        f"\u274c Ban con no {self.fmt_vnd(existing)}. Hay tra het truoc."
                    )
                # Guard atomic: chi ghi khi THUC SU chua co no.
                # Upsert: row amount=0 con sot lai sau khi tra het van tai
                # su dung duoc (tranh UNIQUE constraint failed).
                await db.execute(
                    "INSERT INTO loans(user_id,amount,last_calc) SELECT ?,?,?"
                    " WHERE NOT EXISTS (SELECT 1 FROM loans WHERE user_id=? AND amount>0)"
                    " ON CONFLICT(user_id) DO UPDATE SET amount=excluded.amount,"
                    " last_calc=excluded.last_calc",
                    (user_id, amt, now(), user_id),
                )
                await db.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=?", (amt, user_id)
                )
                async with db.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user_id,)
                ) as cur:
                    after = int((await cur.fetchone())[0])
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " VALUES(?,?,?,?,?,?)",
                    (user_id, "loan", amt, after, now(), f"han muc {self.fmt_vnd(max_loan)}"),
                )
                await db.commit()

        await self._log_economy(user_id, "loan", amt, data["balance"], after, reason="vay")
        today_rate = daily_rate(LOAN_DAILY_RATE, LOAN_DAILY_SPREAD) * 100
        await ctx.send(
            f"\ud83c\udfe6 Giai ngan {self.fmt_vnd(amt)} \u2022 lai hom nay "
            f"**{today_rate:.2f}%/ngay** (bien dong theo ngay, lai kep). "
            "Dung `!trano` de tra."
        )

    @commands.command(name="trano", aliases=["repay"])
    async def repay(self, ctx, amount: str):
        """Tra no vay. Khong the tra qua so du dang co (khong du se bi tu choi)."""
        await self.apply_loan_interest(ctx.author.id)
        user_id = ctx.author.id
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT amount FROM loans WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
        if not row or row[0] <= 0:
            return await ctx.send("\u2705 Ban khong co no.")
        debt = int(row[0])
        amt = self.parse_amount(amount, debt)
        if amt is None or amt <= 0:
            return await ctx.send("\u274c So tien tra no khong hop le.")
        amt = min(amt, debt)

        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                # Guard atomic: chi tru tien khi that su du.
                async with db.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (amt, user_id, amt),
                ) as cur:
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send(
                            f"\u274c So du khong du. Ban can them {self.fmt_vnd(amt)}."
                        )
                remaining = max(0, debt - amt)
                await db.execute(
                    "UPDATE loans SET amount=? WHERE user_id=?", (remaining, user_id)
                )
                async with db.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user_id,)
                ) as cur:
                    after = int((await cur.fetchone())[0])
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " VALUES(?,?,?,?,?,?)",
                    (user_id, "repay", -amt, after, now(), f"con no {remaining}"),
                )
                await db.commit()

        await self._log_economy(user_id, "repay", -amt, after + amt, after, reason="tra no")
        await ctx.send(
            f"\u2705 Tra {self.fmt_vnd(amt)} \u2022 con no {self.fmt_vnd(remaining)}."
        )

    # ------------------------------------------------------------------
    # Lãi suất công bố theo ngày + Thế chấp / Chuộc đồ
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Vat pham: shop, mua, ban, tu do
    # ------------------------------------------------------------------

    async def _get_inventory(self, user_id: int) -> dict:
        """Tra ve ``{item_key: so_luong}`` cua nguoi dung."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT item, amount FROM inventory WHERE user_id=? AND amount>0", (user_id,)
            ) as cur:
                return {row[0]: int(row[1]) for row in await cur.fetchall()}

    async def _add_item(self, db, user_id: int, key: str, delta: int) -> None:
        """Cong/tru so luong vat pham (dung chung ``!mua`` va ``!ban``)."""
        await db.execute(
            "INSERT INTO inventory(user_id,item,amount) VALUES(?,?,?)"
            " ON CONFLICT(user_id,item) DO UPDATE SET amount=amount+excluded.amount",
            (user_id, key, delta),
        )

    @commands.command(name="shop", aliases=["vatpham", "cuahang"])
    async def shop(self, ctx):
        """Xem cua hang vat pham va gia hien tai."""
        embed = discord.Embed(
            title="\U0001f6d2 C\u1eebA H\u00c0NG V\u1eadT PH\u1ea2M",
            description="Gi\u00e1 \u0111\u1ec3 \u0111\u1ed5 \u0111\u1ed9 \u1ed5ng v\u1edbi ti\u1ec1n b\u1ea1n \u0111ang c\u00f3 trong v\u00ed.",
            color=discord.Color.gold(),
        )
        for key, (name, price, emoji, desc, _nhom) in ITEMS.items():
            embed.add_field(
                name=f"{emoji} {name}",
                value=f"Giá: **{self.fmt_vnd(price)}**\n{desc}",
                inline=False,
            )
        embed.set_footer(text="Mua: !muado <mã> [số lượng] • Xem của mình: !tudovanpham")
        await ctx.send(embed=embed)

    @commands.command(name="muado", aliases=["muavatpham", "muadovatpham"])
    async def buy_item(self, ctx, key: str, amount_input: str = "1"):
        """Mua vat pham bang tien mat trong vi."""
        key = key.strip().lower()
        if key not in ITEMS:
            hint = ", ".join(sorted(ITEMS)[:6])
            return await ctx.send(f"\u274c Không có vật phẩm `{key}`. Ví dụ: {hint} — xem `!shop`.")

        data = await self.get_user_data(ctx.author.id)
        amount = self.parse_amount(amount_input, data["balance"])
        if amount is None or amount <= 0:
            return await ctx.send("\u274c Số lượng không hợp lệ.")

        name, price, _emoji, _desc, _nhom = ITEMS[key]
        total = price * amount
        if total > data["balance"]:
            return await ctx.send(
                f"\u274c Không đủ tiền. Cần {self.fmt_vnd(total)} nhưng bạn chỉ có "
                f"{self.fmt_vnd(data['balance'])}."
            )

        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (total, ctx.author.id, total),
                ) as cur:
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send("\u274c Giao dịch thất bại: số dư đã thay đổi.")
                await self._add_item(db, ctx.author.id, key, amount)
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " VALUES(?,?,?,?,?,?)",
                    (ctx.author.id, "buy_item", -total, data["balance"] - total, now(), key),
                )
                await db.commit()

        await self._log_economy(
            ctx.author.id, "buy_item", -total, data["balance"], data["balance"] - total,
            reason=key,
        )
        await ctx.send(
            f"\U0001f6d2 Đã mua **{amount}× {name}** với giá {self.fmt_vnd(total)}."
        )

    @commands.command(name="bando", aliases=["banvatpham", "bandovatpham"])
    @commands.cooldown(2, 15.0, commands.BucketType.user)
    async def sell_item(self, ctx, key: str, amount_input: str = "1"):
        """Ban vat pham dang so huu lay lai tien (ban 70% gia).

        **Bao mat:** kiem tra so luong nam *trong* transaction va dung atomic
        ``UPDATE ... WHERE amount>=?``. Neu kiem tra nam ngoai transaction
        (nhu ban dau) thi hai lenh chay song song se cung doc du so hang
        va cung tru -> ton kho am -> co the ban vo han vat pham ma khong co.
        """
        key = key.strip().lower()
        if key not in ITEMS:
            return await ctx.send(f"\u274c Khong co vat pham `{key}` - xem `!shop`.")

        amount = self.parse_amount(amount_input, 0) or 0
        if amount <= 0:
            return await ctx.send("\u274c So luong khong hop le.")

        name, price, _emoji, _desc, _nhom = ITEMS[key]
        refund = int(price * 0.7) * amount
        user_id = ctx.author.id

        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                # Guard atomic: chi tru khi that su dang co du so luong.
                async with db.execute(
                    "UPDATE inventory SET amount=amount-?"
                    " WHERE user_id=? AND item=? AND amount>=?",
                    (amount, user_id, key, amount),
                ) as cur:
                    sold = cur.rowcount == 1

                if not sold:
                    async with db.execute(
                        "SELECT COALESCE(amount,0) FROM inventory"
                        " WHERE user_id=? AND item=?",
                        (user_id, key),
                    ) as cur:
                        row = await cur.fetchone()
                    have = int(row[0]) if row else 0
                    await db.rollback()
                    return await ctx.send(
                        f"\u274c Ban chi co **{have} x {item_name(key)}**."
                    )

                await db.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=?",
                    (refund, user_id),
                )
                async with db.execute(
                    "SELECT balance FROM users WHERE user_id=?", (user_id,)
                ) as cur:
                    after = int((await cur.fetchone())[0])
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " VALUES(?,?,?,?,?,?)",
                    (user_id, "sell_item", refund, after, now(), key),
                )
                await db.commit()

        await self._log_economy(
            user_id, "sell_item", refund, after - refund, after, reason=key
        )
        await ctx.send(
            f"\U0001f4b3 Da ban **{amount} x {name}**, nhan lai "
            f"{self.fmt_vnd(refund)} (ban lai 70% gia)."
        )

    @commands.command(name="tudovanpham", aliases=["tudokho", "tuido", "inv"])
    async def inventory_cmd(self, ctx, member: discord.Member = None):
        """Xem t\u00fac kh\u00ed nh\u1eebt c\u1ee7a m\u1ed9t ng\u01b0\u1ed7i d\u00f9ng."""
        target = member or ctx.author
        held = await self._get_inventory(target.id)
        if not held:
            return await ctx.send(f"\U0001f6d2 {target.display_name} chưa có vật phẩm nào.")

        total_value = inventory_value(held)
        lines = []
        for key, count in sorted(held.items(), key=lambda kv: -item_price(kv[0]) * kv[1]):
            entry = ITEMS.get(key)
            emoji = entry[2] if entry else "\U0001f4e6"
            lines.append(f"{emoji} **{item_name(key)}** ×{count} — {self.fmt_vnd(item_price(key) * count)}")

        embed = discord.Embed(
            title=f"\U0001f392 T\u1ee7 KHO \u2014 {target.display_name}",
            description=chr(10).join(lines),
            color=discord.Color.purple(),
        )
        embed.add_field(name="\U0001f4b0 T\u1ed5ng gi\u00e1 trị", value=self.fmt_vnd(total_value))
        embed.set_footer(text="Bán: !bando <mã> [số lượng]")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Reset cuoc doi
    # ------------------------------------------------------------------

    #: Token xac nhan cho ``!reset`` -- gan lien voi ``user_id`` va tu huy sau 2 phut.
    #:
    #: ``!reset`` cap token ngau nhien cho CHINH nguoi goi lenh; ``!reset <token>``
    #: moi thuc hien. Token cua nguoi A khong the xac nhan cho nguoi B vi kho
    #: luu theo ``user_id``, va token chi dung duoc mot lan.
    RESET_TOKEN_TTL = 120.0

    @commands.command(name="reset", aliases=["resetcuocdoi", "vaymua"])
    @commands.cooldown(1, 60.0, commands.BucketType.user)
    async def reset_life(self, ctx, token: str = ""):
        """Xoa toan bo tien trinh va bat dau lai tu dau.

        **Bao mat:** buoc xac nhan dung bang *token ngau nhien* sinh rieng cho
        nguoi goi lenh, het han sau 2 phut va chi dung mot lan. Khong ai
        khac xac nhan thay ban duoc.
        """
        import secrets

        user_id = ctx.author.id
        token = token.strip()
        # Dict token: user_id -> (token, het_han)
        pending = getattr(self, "_reset_tokens", None)
        if pending is None:
            pending = {}
            self._reset_tokens = pending
        now_ts = time.time()
        for key_uid, (_tok, expires) in list(pending.items()):
            if expires <= now_ts:
                pending.pop(key_uid, None)

        if not token:
            code = secrets.token_hex(3)
            pending[user_id] = (code, now_ts + self.RESET_TOKEN_TTL)
            summary = await self.get_financial_summary(user_id)
            return await ctx.send(
                "\u26a0\ufe0f **Bat dau tien doi lai.**\n"
                f"\u2022 Tien mat: {self.fmt_vnd(summary['balance'])}\n"
                f"\u2022 Tiet kiem: {self.fmt_vnd(summary['bank'])}\n"
                f"\u2022 Co phieu: {self.fmt_vnd(summary['stock_value'] + summary['market_value'])}\n"
                f"\u2022 No vay: {self.fmt_vnd(summary['debt'])}\n"
                "\nMat tien, co phieu, no, vat pham, XP va level deu **mat**.\n"
                f"Ma xac nhan cua rieng ban la: `{code}` (het han sau "
                f"{int(self.RESET_TOKEN_TTL // 60)} phut, chi dung 1 lan).\n\n"
                f"\u2705 Xac nhan: `!reset {code}`"
            )

        entry = pending.pop(user_id, None)
        if not entry or entry[0] != token or entry[1] <= now_ts:
            return await ctx.send(
                "\u274c Ma xac nhan khong dung hoac da het han. "
                "Go `!reset` de lay ma moi."
            )

        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_user(db, user_id)
                await db.execute(
                    "UPDATE users SET balance=?, bank=0, xp=0, level=1, streak=0,"
                    " reputation=0, last_daily=0, last_work=0, last_fish=0,"
                    " last_crime=0, last_interest=0 WHERE user_id=?",
                    (STARTING_BALANCE, user_id),
                )
                await db.execute("DELETE FROM loans WHERE user_id=?", (user_id,))
                await db.execute("DELETE FROM collateral WHERE user_id=?", (user_id,))
                await db.execute("DELETE FROM shares WHERE user_id=?", (user_id,))
                await db.execute("DELETE FROM inventory WHERE user_id=?", (user_id,))
                await db.execute("DELETE FROM market_holdings WHERE user_id=?", (user_id,))
                await db.execute("DELETE FROM game_stats WHERE user_id=?", (user_id,))
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " VALUES(?,?,?,?,?,?)",
                    (user_id, "reset", 0, STARTING_BALANCE, now(), "vaymua"),
                )
                await db.commit()

        await self._log_economy(
            user_id, "reset", 0, 0, STARTING_BALANCE, reason="vaymua"
        )
        await ctx.send(
            f"\U0001f3c1 Da **vat moi cuoc doi**!\n"
            f"Ban bat dau voi {self.fmt_vnd(STARTING_BALANCE)} VND, "
            "level 1, so tien khong.\n"
            "Chuc mung! Chao lai tu dau nhe! \U0001f389"
        )

    @commands.command(name="laissuat", aliases=["rate", "interest"])
    async def interest_board(self, ctx):
        """Bảng lãi suất **hôm nay** — cả server thấy cùng một mức."""
        day = day_index()
        embed = discord.Embed(
            title="🏦 BIỂU LÃI SUẤT HÔM NAY",
            description=f"Lãi suất công bố theo ngày *(mã ngày {day})* — "
                        "mọi người cùng áp dụng, ngày mai sẽ khác.",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="💰 Gửi tiết kiệm (bank)",
            value=f"`!dep` → **{daily_rate(BANK_DAILY_RATE, BANK_DAILY_SPREAD, day) * 100:.3f}%/ngày** (lãi kép, tự động)",
            inline=False,
        )
        embed.add_field(
            name="📉 Vay không bảo đảm",
            value=f"`!vaytien` → **{daily_rate(LOAN_DAILY_RATE, LOAN_DAILY_SPREAD, day) * 100:.2f}%/ngày** (lãi kép)",
            inline=False,
        )
        embed.add_field(
            name="🔐 Vay thế chấp cổ phiếu",
            value=f"`!thechap` → **{daily_rate(SECURED_DAILY_RATE, SECURED_DAILY_SPREAD, day) * 100:.2f}%/ngày** "
                  f"(rẻ hơn — LTV {int(COLLATERAL_LTV * 100)}%)",
            inline=False,
        )
        embed.set_footer(text="Lãi kép tính theo từng ngày • VNĐ ảo, chỉ dùng trong server")
        await ctx.send(embed=embed)

    @commands.command(name="thechap", aliases=["pawn", "collateral"])
    async def collateral_pledge(self, ctx, company_name: str, shares_input: str):
        """Cầm cố CP công ty để vay tiền (thế chấp)."""
        await self.apply_collateral_interest(ctx.author.id)
        company_name = company_name.strip()
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                # BEGIN IMMEDIATE phải đứng ĐẦU TIÊN: mọi lệnh ghi trước đó
                # (vd. _ensure_user) đã tự mở transaction ngầm của sqlite3.
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_user(db, ctx.author.id)
                async with db.execute(
                    "SELECT id,price_per_share FROM companies WHERE lower(name)=lower(?)",
                    (company_name,),
                ) as cur:
                    company = await cur.fetchone()
                if not company:
                    await db.rollback()
                    return await ctx.send("❌ Công ty không tồn tại.")
                cid, price = company
                async with db.execute(
                    "SELECT amount FROM shares WHERE user_id=? AND company_id=?",
                    (ctx.author.id, cid),
                ) as cur:
                    held_row = await cur.fetchone()
                held = held_row[0] if held_row else 0
                shares = self.parse_amount(shares_input, held)
                if shares is None or shares <= 0 or shares > held:
                    await db.rollback()
                    return await ctx.send(f"❌ Bạn chỉ có **{held:,} CP** {company_name.upper()}.")
                async with db.execute(
                    "SELECT amount FROM collateral WHERE user_id=?", (ctx.author.id,)
                ) as cur:
                    existing = await cur.fetchone()
                if existing and existing[0] > 0:
                    await db.rollback()
                    return await ctx.send(
                        f"❌ Bạn đang có khoản thế chấp {self.fmt_vnd(existing[0])}. "
                        "Trả hết bằng `!chuoc` trước."
                    )
                # Giao dịch atomic: khóa CP → giải ngân theo LTV.
                pledged_value = shares * price
                loan_amount = int(pledged_value * COLLATERAL_LTV)
                if loan_amount < MIN_COLLATERAL_LOAN:
                    await db.rollback()
                    return await ctx.send(
                        f"❌ Giá trị thế chấp quá nhỏ — cần vay tối thiểu {self.fmt_vnd(MIN_COLLATERAL_LOAN)} "
                        f"(LTV {int(COLLATERAL_LTV * 100)}% × {self.fmt_vnd(pledged_value)})."
                    )
                await db.execute(
                    "UPDATE shares SET amount=amount-? WHERE user_id=? AND company_id=? AND amount>=?",
                    (shares, ctx.author.id, cid, shares),
                )
                await db.execute("DELETE FROM shares WHERE user_id=? AND company_id=? AND amount<=0",
                                 (ctx.author.id, cid))
                await db.execute(
                    "INSERT OR REPLACE INTO collateral(user_id,shares,company_id,pledged_value,amount,last_calc,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (ctx.author.id, shares, cid, pledged_value, loan_amount, now(), now()),
                )
                await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?",
                                 (loan_amount, ctx.author.id))
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " SELECT ?, 'collateral_loan', ?, balance, ?, ? FROM users WHERE user_id=?",
                    (ctx.author.id, loan_amount, now(), f"pledge:{cid}:{shares}cp", ctx.author.id),
                )
                await db.commit()
        secured_rate = daily_rate(SECURED_DAILY_RATE, SECURED_DAILY_SPREAD) * 100
        await ctx.send(
            f"🔐 Thế chấp **{shares:,} CP {company_name.upper()}** (giá trị {self.fmt_vnd(pledged_value)})\n"
            f"💵 Nhận ngay **{self.fmt_vnd(loan_amount)}** (LTV {int(COLLATERAL_LTV * 100)}%) "
            f"• lãi hôm nay **{secured_rate:.2f}%/ngày**\n"
            f"Để chuộc: `!chuoc {company_name}`"
        )

    @commands.command(name="chuoc", aliases=["unpawn"])
    async def collateral_redeem(self, ctx, company_name: str = ""):
        """Trả nợ thế chấp để nhận lại cổ phiếu."""
        await self.apply_collateral_interest(ctx.author.id)
        company_name = company_name.strip()
        if not company_name:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT c.shares, c.pledged_value, c.amount, co.name"
                    " FROM collateral c LEFT JOIN companies co ON c.company_id=co.id"
                    " WHERE c.user_id=?",
                    (ctx.author.id,),
                ) as cur:
                    row = await cur.fetchone()
            if not row or row[2] <= 0:
                return await ctx.send("ℹ️ Bạn không có khoản thế chấp nào. Dùng `!thechap <cty> <số CP>`.")
            shares, pledged_value, debt, cname = row
            cname = (cname or "?").upper()
            secured_rate = daily_rate(SECURED_DAILY_RATE, SECURED_DAILY_SPREAD) * 100
            embed = discord.Embed(title="🔐 KHOẢN THẾ CHẤP CỦA BẠN", color=discord.Color.orange())
            embed.add_field(name="Cổ phiếu cầm cố", value=f"**{shares:,} CP {cname}**", inline=True)
            embed.add_field(name="Giá trị khi cầm", value=self.fmt_vnd(pledged_value), inline=True)
            embed.add_field(name="Nợ hiện tại (đã gồm lãi)", value=self.fmt_vnd(debt), inline=True)
            embed.add_field(name="Lãi secured hôm nay", value=f"**{secured_rate:.2f}%/ngày**", inline=True)
            embed.add_field(
                name="Cách chuộc",
                value=f"`!chuoc {cname.lower()}` — trả {self.fmt_vnd(debt)} để nhận lại CP.",
                inline=False,
            )
            await ctx.send(embed=embed)
            return
        data = await self.get_user_data(ctx.author.id)
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute(
                    "SELECT shares,company_id,amount FROM collateral WHERE user_id=?",
                    (ctx.author.id,),
                ) as cur:
                    row = await cur.fetchone()
                if not row or row[2] <= 0:
                    await db.rollback()
                    return await ctx.send("ℹ️ Bạn không có khoản thế chấp nào.")
                shares, cid, debt = row
                async with db.execute("SELECT name FROM companies WHERE id=?", (cid,)) as cur:
                    name_row = await cur.fetchone()
                actual_name = (name_row[0] if name_row else "").lower()
                if actual_name != company_name.lower():
                    await db.rollback()
                    hint = actual_name or "?"
                    return await ctx.send(f"❌ Khoản thế chấp của bạn là **{hint.upper()}**. Dùng `!chuoc {hint}`.")
                if data["balance"] < debt:
                    await db.rollback()
                    return await ctx.send(
                        f"❌ Bạn cần {self.fmt_vnd(debt)} để chuộc (hiện có {self.fmt_vnd(data['balance'])})."
                    )
                await db.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (debt, ctx.author.id, debt),
                )
                await db.execute(
                    "INSERT INTO shares(user_id,company_id,amount) VALUES(?,?,?)"
                    " ON CONFLICT(user_id,company_id) DO UPDATE SET amount=amount+excluded.amount",
                    (ctx.author.id, cid, shares),
                )
                await db.execute("DELETE FROM collateral WHERE user_id=?", (ctx.author.id,))
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " SELECT ?, 'collateral_redeem', ?, balance, ?, ? FROM users WHERE user_id=?",
                    (ctx.author.id, -debt, now(), f"unpawn:{cid}:{shares}cp", ctx.author.id),
                )
                await db.commit()
        await ctx.send(f"✅ Đã chuộc **{shares:,} CP** — trả {self.fmt_vnd(debt)}. CP đã về tài khoản!")

    # ------------------------------------------------------------------
    # Stats, leaderboard, history
    # ------------------------------------------------------------------

    async def get_financial_summary(self, user_id: int) -> dict:
        """Tổng hợp tài sản dùng chung cho `!taisan` và cho AI (đọc tài sản user).

        Lãi (bank/vay/thế chấp) đã được áp tự động bên trong ``get_user_data``.
        ``stock_value`` = CP công ty người chơi tạo, ``market_value`` = CP trên sàn.
        """
        data = await self.get_user_data(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COALESCE(SUM(s.amount*c.price_per_share),0) FROM shares s"
                " JOIN companies c ON s.company_id=c.id WHERE s.user_id=? AND s.amount>0",
                (user_id,),
            ) as cur:
                stock_value = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COALESCE(SUM(h.shares*m.price),0) FROM market_holdings h"
                " JOIN market m ON upper(m.symbol)=upper(h.symbol) WHERE h.user_id=? AND h.shares>0",
                (user_id,),
            ) as cur:
                market_value = (await cur.fetchone())[0]
            async with db.execute("SELECT COALESCE(amount,0) FROM loans WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
                debt = row[0] if row else 0
            async with db.execute(
                "SELECT c.amount, c.shares, co.name FROM collateral c"
                " LEFT JOIN companies co ON c.company_id=co.id WHERE c.user_id=?",
                (user_id,),
            ) as cur:
                crow = await cur.fetchone()
        collateral_debt = crow[0] if crow else 0
        collateral_shares = crow[1] if crow else 0
        collateral_symbol = (crow[2] or "").upper() if crow else ""
        held = await self._get_inventory(user_id)
        item_value = inventory_value(held)
        net = (
            data["balance"] + data["bank"] + stock_value + market_value
            + item_value - debt - collateral_debt
        )
        return {
            "balance": data["balance"],
            "bank": data["bank"],
            "level": data["level"],
            "stock_value": stock_value,
            "market_value": market_value,
            "debt": debt,
            "collateral_debt": collateral_debt,
            "collateral_shares": collateral_shares,
            "collateral_symbol": collateral_symbol,
            "item_value": item_value,
            "item_count": sum(int(v) for v in held.values()),
            "net_worth": net,
        }

    def format_financial_context(self, summary: dict) -> str:
        """Chuỗi ``[TÀI SẢN]`` tiếng Việt để nhúng vào prompt AI."""
        lines = [
            f"- Ví: {summary['balance']:,} VNĐ",
            f"- Bank (tiết kiệm): {summary['bank']:,} VNĐ",
            f"- Cổ phiếu công ty: {summary['stock_value']:,} VNĐ",
            f"- Cổ phiếu sàn: {summary['market_value']:,} VNĐ",
            f"- Nợ vay: {summary['debt']:,} VNĐ",
        ]
        if summary["collateral_debt"] > 0:
            lines.append(
                f"- Nợ thế chấp: {summary['collateral_debt']:,} VNĐ "
                f"(giữ {summary['collateral_shares']:,} CP {summary['collateral_symbol']})"
            )
        if summary.get("item_value"):
            lines.append(
                f"- Vật phạm ({summary.get('item_count', 0)} món): "
                f"{summary['item_value']:,} VNĐ"
            )
        lines.append(f"- TỔNG TÀI SẢN RÒNG: {summary['net_worth']:,} VNĐ")
        lines.append(f"- Level: {summary['level']}")
        return "\n".join(lines)

    def format_rates_context(self) -> str:
        """Chuoi ``[LAI SUAT]`` tieng Viet de AI doc tu dong.

        Cung khep chung mot bien lai suat cong bo hom nay cho ca AI va lenh
        ``!laissuat``, nen AI khong bao gio tra loi lech voi hien thi chinh.
        """
        day = day_index()
        save = daily_rate(BANK_DAILY_RATE, BANK_DAILY_SPREAD, day) * 100
        loan = daily_rate(LOAN_DAILY_RATE, LOAN_DAILY_SPREAD, day) * 100
        secured = daily_rate(SECURED_DAILY_RATE, SECURED_DAILY_SPREAD, day) * 100
        return (
            f"- Tiet kiem (!dep): {save:.3f}%/ngay (lai kep, tu dong)\n"
            f"- Vay khong bao dam (!vaytien): {loan:.2f}%/ngay\n"
            f"- Vay the chap (!thechap): {secured:.2f}%/ngay (LTV {int(COLLATERAL_LTV * 100)}%)\n"
            f"- Ngay cong bo: {day}"
        )

    @commands.command(name="taisan", aliases=["assets", "ts"])
    async def assets_cmd(self, ctx, member: discord.Member = None):
        target = member or ctx.author
        s = await self.get_financial_summary(target.id)
        embed = discord.Embed(title=f"📊 TÀI SẢN — {target.display_name}", color=discord.Color.blue())
        embed.add_field(name="💵 Ví", value=self.fmt_vnd(s["balance"]))
        embed.add_field(name="🏦 Bank", value=self.fmt_vnd(s["bank"]))
        embed.add_field(name="📈 CP công ty", value=self.fmt_vnd(s["stock_value"]))
        embed.add_field(name="🏛️ CP sàn", value=self.fmt_vnd(s["market_value"]))
        embed.add_field(name="📉 Nợ vay", value=f"-{self.fmt_vnd(s['debt'])}" if s["debt"] else "0 VNĐ")
        if s["collateral_debt"]:
            embed.add_field(
                name="🔐 Nợ thế chấp",
                value=f"-{self.fmt_vnd(s['collateral_debt'])} ({s['collateral_shares']:,} CP {s['collateral_symbol']})",
            )
        embed.add_field(name="💎 NET WORTH", value=self.fmt_vnd(s["net_worth"]), inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="topmoney", aliases=["rich"])
    async def top_money(self, ctx):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT user_id,balance,bank,level FROM users ORDER BY (balance+bank) DESC LIMIT 10") as cur:
                rows = await cur.fetchall()
        lines = []
        for i, (uid, bal, bank, level) in enumerate(rows, 1):
            user = self.bot.get_user(uid)
            name = user.display_name if user else str(uid)
            lines.append(f"**{i}.** {name} — {self.fmt_vnd(bal+bank)} • Lv.{level}")
        await ctx.send(embed=discord.Embed(title="🏆 TOP KAIAI WEALTH", description="\n".join(lines) or "Chưa có dữ liệu.", color=discord.Color.gold()))

    @commands.command(name="topgame", aliases=["gameboard"])
    async def game_leaderboard(self, ctx: commands.Context) -> None:
        """Xem bảng xếp hạng thành tích game theo lợi nhuận tiền ảo."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT user_id, SUM(played), SUM(wins), SUM(profit) "
                "FROM game_stats GROUP BY user_id "
                "ORDER BY SUM(profit) DESC, SUM(wins) DESC, user_id ASC LIMIT 10"
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await ctx.send("🎮 Chưa có thành tích game. Hãy chơi một ván để lên bảng!")

        lines = []
        for rank, (user_id, played, wins, profit) in enumerate(rows, start=1):
            member = self.bot.get_user(user_id) if self.bot is not None else None
            name = member.display_name if member else f"<@{user_id}>"
            win_rate = wins / played * 100 if played else 0
            sign = "+" if profit >= 0 else ""
            lines.append(
                f"**{rank}.** {name} — **{sign}{profit:,} VNĐ** • "
                f"{wins} thắng / {played} ván ({win_rate:.0f}%)"
            )
        embed = discord.Embed(
            title="🏆 BẢNG XẾP HẠNG GAME",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Tiền ảo trong game · xếp theo tổng lợi nhuận")
        await ctx.send(embed=embed)

    @commands.command(name="gamestats", aliases=["gstats"])
    async def game_stats(self, ctx, member: discord.Member = None):
        target = member or ctx.author
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT game,played,wins,losses,wagered,profit FROM game_stats WHERE user_id=? ORDER BY profit DESC", (target.id,)) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await ctx.send("🎮 Bạn chưa chơi game nào.")
        lines = []
        for game, played, wins, losses, wagered, profit in rows:
            wr = wins / played * 100 if played else 0
            sign = "+" if profit >= 0 else ""
            lines.append(f"**{game}** • {played} ván • {wins}W/{losses}L • {wr:.0f}% • {sign}{profit:,} VNĐ")
        embed = discord.Embed(title=f"🎮 GAME STATS — {target.display_name}", description="\n".join(lines)[:3900], color=discord.Color.blurple())
        await ctx.send(embed=embed)

    @commands.command(name="history", aliases=["lichsu"])
    async def history(self, ctx):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT kind,amount,balance_after,created_at,note FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 12", (ctx.author.id,)) as cur:
                rows = await cur.fetchall()
        if not rows:
            return await ctx.send("📜 Chưa có giao dịch.")
        lines = []
        for kind, amount, balance, created, note in rows:
            sign = "+" if amount >= 0 else ""
            lines.append(f"`{time.strftime('%d/%m %H:%M', time.localtime(created))}` **{kind}** {sign}{amount:,} • dư {balance:,}\n{note}"[:180])
        await ctx.send(embed=discord.Embed(title="📜 LỊCH SỬ GIAO DỊCH", description="\n".join(lines), color=discord.Color.dark_gray()))

    # ------------------------------------------------------------------
    # Market simulation
    # ------------------------------------------------------------------

    @commands.command(name="market", aliases=["chungkhoan"])
    async def market(self, ctx):
        """Xem bảng giá. Giá chỉ **nhích** tối đa 1 lần/phút (chống spam đẩy giá)."""
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT MAX(updated_at) FROM market") as cur:
                    row = await cur.fetchone()
                last_tick = (row[0] if row and row[0] else 0.0)
                elapsed = now() - last_tick
                ticked = elapsed >= MARKET_TICK_COOLDOWN
                if ticked:
                    async with db.execute("SELECT symbol,price FROM market") as cur:
                        current = await cur.fetchall()
                    for symbol, price in current:
                        pct = random.uniform(-0.08, 0.08)
                        new_price = max(1_000, int(price * (1 + pct)))
                        await db.execute(
                            "UPDATE market SET price=?,change=?,updated_at=? WHERE symbol=?",
                            (new_price, pct, now(), symbol),
                        )
                    await db.commit()
                async with db.execute("SELECT symbol,name,price,change FROM market ORDER BY symbol") as cur:
                    rows = await cur.fetchall()
                async with db.execute(
                    "SELECT symbol,shares,avg_cost FROM market_holdings WHERE user_id=? AND shares>0",
                    (ctx.author.id,),
                ) as cur:
                    holdings = {s: (n, c) for s, n, c in await cur.fetchall()}
        lines = []
        for symbol, name, price, change in rows:
            arrow = "📈" if change >= 0 else "📉"
            line = f"{arrow} **{symbol}** {name} — {price:,} VNĐ • {'+' if change>=0 else ''}{change*100:.2f}%"
            if symbol in holdings:
                held, avg = holdings[symbol]
                pnl = (price - avg) * held
                line += f"\n　└ giữ {held:,} CP • giá vốn {avg:,} • P/L {'+' if pnl>=0 else ''}{pnl:,}"
            lines.append(line)
        embed = discord.Embed(
            title="📈 KAI MARKET",
            description="\n".join(lines),
            color=discord.Color.green(),
        )
        hint = "Mua: `!muacp <MÃ> <CP>` • Bán: `!bancp <MÃ> <CP>`"
        if ticked:
            embed.set_footer(text=f"🔄 Giá vừa cập nhật • {hint}")
        else:
            wait = int(MARKET_TICK_COOLDOWN - elapsed)
            embed.set_footer(text=f"⏳ Giá đứng yên — còn {wait}s mới nhích • {hint}")
        await ctx.send(embed=embed)

    async def _market_quote(self, db, symbol: str):
        """Lấy (symbol, name, price) từ bảng market — None nếu mã không tồn tại."""
        async with db.execute(
            "SELECT symbol,name,price FROM market WHERE upper(symbol)=upper(?)",
            (symbol,),
        ) as cur:
            return await cur.fetchone()

    @commands.command(name="muacp", aliases=["buystock"])
    async def market_buy(self, ctx, symbol: str, shares_input: str):
        """Mua cổ phiếu niêm yết trên sàn (phí 1%)."""
        data = await self.get_user_data(ctx.author.id)
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                # BEGIN IMMEDIATE trước mọi lệnh ghi (xem giải thích ở !thechap).
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_user(db, ctx.author.id)
                quote = await self._market_quote(db, symbol)
                if not quote:
                    await db.rollback()
                    return await ctx.send(f"❌ Mã `{symbol.upper()}` không có trên sàn. Xem `!market`.")
                sym, name, price = quote
                shares = self.parse_amount(shares_input, data["balance"] // max(1, price))
                if shares is None or shares <= 0:
                    await db.rollback()
                    return await ctx.send("❌ Số lượng CP không hợp lệ.")
                gross = shares * price
                fee = int(gross * MARKET_FEE)
                total = gross + fee
                # Guard atomic: chi tru khi that su du (gia co the doi giua
                # luc doc data va luc ghi; 2 lenh song song khong the cung qua).
                async with db.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (total, ctx.author.id, total),
                ) as cur:
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send(
                            "❌ Ví không đủ (số dư đã thay đổi). Hãy xem lại `!taisan` rồi thử lại."
                        )
                async with db.execute(
                    "SELECT shares,avg_cost FROM market_holdings WHERE user_id=? AND symbol=?",
                    (ctx.author.id, sym),
                ) as cur:
                    held = await cur.fetchone()
                old_shares, old_cost = held if held else (0, 0)
                new_shares = old_shares + shares
                # Giá vốn bình quân gia quyền (không tính phí — phí là chi phí riêng).
                new_cost = (old_shares * old_cost + gross) // new_shares if new_shares else 0
                await db.execute(
                    "INSERT INTO market_holdings(user_id,symbol,shares,avg_cost) VALUES(?,?,?,?)"
                    " ON CONFLICT(user_id,symbol) DO UPDATE SET shares=excluded.shares,avg_cost=excluded.avg_cost",
                    (ctx.author.id, sym, new_shares, new_cost),
                )
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " SELECT ?, 'market_buy', ?, balance, ?, ? FROM users WHERE user_id=?",
                    (ctx.author.id, -total, now(), f"buy {shares}cp {sym}@{price} fee={fee}", ctx.author.id),
                )
                await db.commit()
        await ctx.send(
            f"✅ Mua **{shares:,} CP {sym}** @ {self.fmt_vnd(price)} = {self.fmt_vnd(gross)} "
            f"+ phí {self.fmt_vnd(fee)} → tổng **{self.fmt_vnd(total)}**\n"
            f"📦 Đang giữ: **{new_shares:,} CP** • giá vốn {self.fmt_vnd(new_cost)}"
        )

    @commands.command(name="bancp", aliases=["sellstock"])
    async def market_sell(self, ctx, symbol: str, shares_input: str):
        """Bán cổ phiếu đang giữ về sàn (phí 1%)."""
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                await self._ensure_user(db, ctx.author.id)
                quote = await self._market_quote(db, symbol)
                if not quote:
                    await db.rollback()
                    return await ctx.send(f"❌ Mã `{symbol.upper()}` không có trên sàn. Xem `!market`.")
                sym, name, price = quote
                async with db.execute(
                    "SELECT shares,avg_cost FROM market_holdings WHERE user_id=? AND symbol=?",
                    (ctx.author.id, sym),
                ) as cur:
                    held = await cur.fetchone()
                owned = held[0] if held else 0
                if owned <= 0:
                    await db.rollback()
                    return await ctx.send(f"❌ Bạn không giữ CP **{sym}** nào.")
                avg_cost = held[1] if held else 0
                shares = self.parse_amount(shares_input, owned)
                if shares is None or shares <= 0 or shares > owned:
                    await db.rollback()
                    return await ctx.send(f"❌ Bạn chỉ có **{owned:,} CP {sym}**.")
                gross = shares * price
                fee = int(gross * MARKET_FEE)
                net = gross - fee
                await db.execute(
                    "UPDATE market_holdings SET shares=shares-? WHERE user_id=? AND symbol=? AND shares>=?",
                    (shares, ctx.author.id, sym, shares),
                )
                await db.execute(
                    "DELETE FROM market_holdings WHERE user_id=? AND symbol=? AND shares<=0",
                    (ctx.author.id, sym),
                )
                await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (net, ctx.author.id))
                await db.execute(
                    "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                    " SELECT ?, 'market_sell', ?, balance, ?, ? FROM users WHERE user_id=?",
                    (ctx.author.id, net, now(), f"sell {shares}cp {sym}@{price} fee={fee}", ctx.author.id),
                )
                await db.commit()
        pnl = (price - avg_cost) * shares
        await ctx.send(
            f"✅ Bán **{shares:,} CP {sym}** @ {self.fmt_vnd(price)} = {self.fmt_vnd(gross)} "
            f"− phí {self.fmt_vnd(fee)} → nhận **{self.fmt_vnd(net)}**\n"
            f"📊 Lãi/lỗ (giá vốn {self.fmt_vnd(avg_cost)}): {'+' if pnl>=0 else ''}{self.fmt_vnd(pnl)}"
        )

    # ------------------------------------------------------------------
    # Companies: legacy commands preserved + safer transactions.
    # ------------------------------------------------------------------

    async def _check_takeover(self, db, company_id: int):
        async with db.execute("SELECT total_shares,owner_id FROM companies WHERE id=?", (company_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        total, current_owner = row
        async with db.execute("SELECT user_id,amount FROM shares WHERE company_id=? ORDER BY amount DESC LIMIT 1", (company_id,)) as cur:
            top = await cur.fetchone()
        if top and top[1] > total / 2 and top[0] != current_owner:
            await db.execute("UPDATE companies SET owner_id=? WHERE id=?", (top[0], company_id))
            return top[0]
        return None

    @commands.command(name="ctytao", aliases=["createcompany"])
    async def create_company(self, ctx, *, name: str):
        name = name.strip()
        if len(name) < 3 or len(name) > 40:
            return await ctx.send("❌ Tên công ty phải dài 3–40 ký tự.")
        data = await self.get_user_data(ctx.author.id)
        daily_seed = int(time.strftime("%Y%m%d"))
        rng = random.Random(daily_seed)
        # Von dieu le thanh lap: 500 trieu - 2 ty VND (quy mo mot cong ty
        # nho o Viet Nam). Tien nap truc tiep vao QUY cong ty — khong dot.
        cost = rng.randint(500_000_000, 2_000_000_000)
        if data["balance"] < cost:
            return await ctx.send(
                f"❌ Hôm nay cần vốn điều lệ {self.fmt_vnd(cost)} để thành lập."
            )
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute("SELECT 1 FROM companies WHERE lower(name)=lower(?)", (name,)) as cur:
                    if await cur.fetchone():
                        await db.rollback(); return await ctx.send("❌ Tên công ty đã tồn tại.")
                async with db.execute(
                        "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                        (cost, ctx.author.id, cost),
                ) as cur:
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send("❌ Ví không đủ tiền để thành lập.")
                price = max(1_000, cost // 1000)
                cur = await db.execute("INSERT INTO companies(name,owner_id,total_shares,price_per_share,cash,created_at) VALUES(?,?,?,?,?,?)", (name,ctx.author.id,1000,price,cost,now()))
                cid = cur.lastrowid
                await db.execute("INSERT INTO shares(user_id,company_id,amount) VALUES(?,?,?)", (ctx.author.id,cid,1000))
                await db.commit()
        await ctx.send(
            f"🏢 Thành lập **{name}** thành công! Vốn điều lệ "
            f"{self.fmt_vnd(cost)} đã vào quỹ công ty."
        )

    @commands.command(name="ctyinfo")
    async def company_info(self, ctx, *, name: str):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT id,owner_id,total_shares,price_per_share,cash,created_at FROM companies WHERE lower(name)=lower(?)", (name.strip(),)) as cur:
                c = await cur.fetchone()
            if not c:
                return await ctx.send("❌ Công ty không tồn tại.")
            cid, owner, total, price, cash, created = c
            async with db.execute("SELECT user_id,amount FROM shares WHERE company_id=? ORDER BY amount DESC LIMIT 5", (cid,)) as cur:
                holders = await cur.fetchall()
        owner_obj = self.bot.get_user(owner)
        holder_text = []
        for uid, amount in holders:
            u = self.bot.get_user(uid)
            holder_text.append(f"{u.display_name if u else uid}: {amount} CP ({amount/total*100:.1f}%)")
        embed = discord.Embed(title=f"🏢 {name.upper()}", color=discord.Color.gold())
        embed.add_field(name="👑 Chủ tịch", value=owner_obj.mention if owner_obj else str(owner))
        embed.add_field(name="💹 Vốn hóa", value=self.fmt_vnd(total*price))
        embed.add_field(name="💰 Cash", value=self.fmt_vnd(cash))
        embed.add_field(name="📊 Cổ phiếu", value=f"{total:,} CP × {price:,} VNĐ")
        embed.add_field(name="👥 Top cổ đông", value="\n".join(holder_text) or "Chưa có", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="ctydautu", aliases=["invest"])
    async def invest_company(self, ctx, name: str, amount_input: str):
        data = await self.get_user_data(ctx.author.id)
        amount = self.parse_amount(amount_input, data["balance"])
        if amount is None or amount <= 0 or amount > data["balance"]:
            return await ctx.send("❌ Số tiền đầu tư không hợp lệ.")
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT id,owner_id,price_per_share FROM companies WHERE lower(name)=lower(?)", (name.strip(),)) as cur:
                row = await cur.fetchone()
        if not row:
            return await ctx.send("❌ Công ty không tồn tại.")
        cid, owner, price = row
        shares = amount // price
        if shares < 1:
            return await ctx.send(f"❌ Cần ít nhất {self.fmt_vnd(price)} để mua 1 CP.")
        # Investment is immediate and atomic; the old button workflow could
        # lose money if the requester changed balance while waiting.
        cost = shares * price
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                # Guard atomic: chi tru khi that su du.
                async with db.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (cost, ctx.author.id, cost),
                ) as cur:
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send("❌ Ví không còn đủ tiền.")
                await db.execute("UPDATE companies SET cash=cash+?,total_shares=total_shares+? WHERE id=?", (cost,shares,cid))
                await db.execute("INSERT INTO shares(user_id,company_id,amount) VALUES(?,?,?) ON CONFLICT(user_id,company_id) DO UPDATE SET amount=amount+excluded.amount", (ctx.author.id,cid,shares))
                new_owner = await self._check_takeover(db,cid)
                await db.commit()
        message = f"🤝 Đầu tư **{name}**: {shares:,} CP với {self.fmt_vnd(cost)}."
        if new_owner:
            message += f"\n🚨 <@{new_owner}> đã vượt 50% và trở thành Chủ tịch mới!"
        await ctx.send(message)

    @commands.command(name="ctycotuc", aliases=["dividend"])
    async def pay_dividend(self, ctx, *, name: str):
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute("SELECT id,owner_id,cash,total_shares FROM companies WHERE lower(name)=lower(?)", (name.strip(),)) as cur:
                    row = await cur.fetchone()
                if not row:
                    await db.rollback(); return await ctx.send("❌ Công ty không tồn tại.")
                cid, owner, cash, total = row
                if owner != ctx.author.id:
                    await db.rollback(); return await ctx.send("❌ Chỉ Chủ tịch mới được chia cổ tức.")
                if cash < 5_000_000:
                    await db.rollback(); return await ctx.send("❌ Quỹ công ty cần ít nhất 5 triệu để chia cổ tức.")
                async with db.execute("SELECT user_id,amount FROM shares WHERE company_id=? AND amount>0", (cid,)) as cur:
                    holders = await cur.fetchall()
                if not holders:
                    await db.rollback(); return await ctx.send("❌ Chưa có cổ đông.")
                per_share = cash // total
                distributed = 0
                for uid, amount in holders:
                    payout = amount * per_share
                    if payout:
                        await self._ensure_user(db, uid)
                        await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (payout,uid))
                        distributed += payout
                await db.execute("UPDATE companies SET cash=cash-? WHERE id=?", (distributed,cid))
                await db.commit()
        await ctx.send(f"💸 Đã chia cổ tức **{distributed:,} VNĐ** cho {len(holders)} cổ đông.")

    @commands.command(name="ctyban", aliases=["sellcompany"])
    async def sell_company(self, ctx, name: str, member: discord.Member, price_input: str):
        """Chuyển nhượng toàn bộ công ty bằng giao dịch hai chiều."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT id,owner_id FROM companies WHERE lower(name)=lower(?)", (name.strip(),)) as cur:
                row = await cur.fetchone()
        if not row or row[1] != ctx.author.id:
            return await ctx.send("❌ Bạn không phải Chủ tịch công ty này.")
        data = await self.get_user_data(member.id)
        price = self.parse_amount(price_input, data["balance"])
        if price is None or price <= 0 or price > data["balance"]:
            return await ctx.send("❌ Người mua không đủ tiền hoặc giá không hợp lệ.")
        cid = row[0]
        async with self._db_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                # Guard atomic: chi tru khi nguoi mua that su du.
                async with db.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                    (price, member.id, price),
                ) as cur:
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send("❌ Người mua không còn đủ tiền.")
                await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (price,ctx.author.id))
                await db.execute("UPDATE companies SET owner_id=? WHERE id=?", (member.id,cid))
                # Transfer founder shares as well.
                async with db.execute("SELECT amount FROM shares WHERE user_id=? AND company_id=?", (ctx.author.id,cid)) as cur:
                    share_row = await cur.fetchone()
                if share_row:
                    await db.execute("DELETE FROM shares WHERE user_id=? AND company_id=?", (ctx.author.id,cid))
                    await db.execute("INSERT INTO shares(user_id,company_id,amount) VALUES(?,?,?) ON CONFLICT(user_id,company_id) DO UPDATE SET amount=amount+excluded.amount", (member.id,cid,share_row[0]))
                await db.commit()
        await ctx.send(f"🏢 **{name}** đã được chuyển cho {member.mention} với giá {self.fmt_vnd(price)}.")

    # ------------------------------------------------------------------
    # Economy menu
    # ------------------------------------------------------------------

    @commands.command(name="games", aliases=["economy", "helpme", "econ"])
    async def games_menu(self, ctx):
        pages = []
        e = discord.Embed(title="🤖 KAI ECONOMY — CORE", description="Hệ sinh thái kinh tế ảo của KaiAI.", color=discord.Color.gold())
        e.add_field(name="💳 Tiền", value="`!cash` `!dep` `!with` `!pay` `!daily` `!work`\n`!vaytien` `!trano` `!taisan` `!history`", inline=False)
        e.add_field(name="🏦 Tín dụng", value="`!laissuat` (biểu lãi hôm nay) `!thechap <cty> <CP>` `!chuoc <cty>`\nLãi kép theo ngày • vay thế chấp rẻ hơn vay thường.", inline=False)
        e.add_field(name="⭐ Progression", value="`!topmoney` `!topgame` `!gamestats` `!quest` `!fish` `!mine` `!crime`", inline=False)
        pages.append(e)
        e = discord.Embed(title="🎰 KAI ARCADE", description="Mini-games dùng VNĐ ảo.", color=discord.Color.green())
        e.add_field(name="🎲 Dice", value="`!taixiu` `!chanle` `!coinflip` `!dice` `!highlow`", inline=False)
        e.add_field(name="🃏 Cards & Casino", value="`!blackjack` `!roulette` `!slots` `!rps`", inline=False)
        e.add_field(name="🏁 Simulation", value="`!duangua` `!bongda`", inline=False)
        pages.append(e)
        e = discord.Embed(title="🏢 KAI CORPORATION", description="Xây đế chế doanh nghiệp, cổ phần và cổ tức.", color=discord.Color.blurple())
        e.add_field(name="🏭 Doanh nghiệp", value="`!ctytao <tên>` `!ctyinfo <tên>` `!ctydautu <tên> <tiền>`", inline=False)
        e.add_field(name="📈 Ownership", value="`!ctycotuc <tên>` `!ctyban <tên> @user <giá>`\nĐầu tư thêm cổ phần có thể thay đổi Chủ tịch.", inline=False)
        pages.append(e)
        e = discord.Embed(title="📈 KAI MARKET", description="Sàn chứng khoán ảo — giá nhích tối đa **1 lần/phút** (chống spam).\nCác giá trị chỉ là tiền ảo trong server.", color=discord.Color.teal())
        e.add_field(name="🤝 Giao dịch", value="`!muacp <MÃ> <CP>` mua • `!bancp <MÃ> <CP>` bán\nPhí giao dịch **1%** (cả mua và bán).", inline=False)
        e.add_field(name="Assets", value="KAI • ROB • NOVA • BYTE • LUX • OCEAN • ZEN • APEX", inline=False)
        e.add_field(name="🛡️ An toàn", value="Mọi giao dịch chính đều kiểm tra số dư và dùng transaction SQLite.", inline=False)
        pages.append(e)
        await ctx.send(embed=pages[0], view=HelpPagination(pages, ctx.author.id))


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))

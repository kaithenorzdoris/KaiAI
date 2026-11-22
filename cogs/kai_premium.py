"""
KaiAI Premium Core
==================
A production-oriented feature pack for discord.py 2.x.

Design goals:
- Keep legacy cogs intact.
- Add persistent, per-guild configuration.
- Prefer Discord-native UI (buttons/selects/modals).
- Use SQLite WAL with short transactions.
- Avoid background tasks that grow without bounds.
- Provide graceful fallbacks when optional APIs are missing.

Prefix remains compatible with the original bot: ! / .
"""

from __future__ import annotations

import asyncio
import aiosqlite
import datetime as dt
import io
import json
import math
import os
import random
import re
import shutil
import time
import traceback
import zipfile
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands

from kai_core.config import CONFIG


DATA_DIR = CONFIG.data_dir
DB_PATH = DATA_DIR / "kai_core.db"
BOT_NAME = "KaiAI"

DEFAULT_PREFIX = "!"
MAX_MESSAGE_LENGTH = 1900
XP_COOLDOWN = 45
MAX_LEVEL = 100


def now_ts() -> int:
    return int(time.time())


def human_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def parse_duration(value: str) -> int:
    """Parse 30s, 10m, 2h, 7d, or a plain number of seconds."""
    value = value.strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw]?)", value)
    if not match:
        raise ValueError("Định dạng thời gian không hợp lệ. Ví dụ: `30s`, `10m`, `2h`, `7d`.")
    amount = float(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    seconds = int(amount * multiplier)
    if seconds < 5:
        raise ValueError("Thời lượng tối thiểu là 5 giây.")
    if seconds > 60 * 60 * 24 * 30:
        raise ValueError("Thời lượng tối đa là 30 ngày.")
    return seconds


def xp_for_level(level: int) -> int:
    level = max(0, level)
    return 100 * level * level + 250 * level


def level_from_xp(xp: int) -> int:
    xp = max(0, xp)
    level = 0
    while level < MAX_LEVEL and xp >= xp_for_level(level + 1):
        level += 1
    return level


def progress_bar(current: int, target: int, width: int = 14) -> str:
    if target <= 0:
        return "█" * width
    ratio = max(0.0, min(1.0, current / target))
    filled = int(round(width * ratio))
    return "█" * filled + "░" * (width - filled)


async def safe_delete(message: discord.Message, delay: float = 0) -> None:
    try:
        await message.delete(delay=delay)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


class Database:
    """Small async SQLite wrapper used by KaiAI Premium."""

    def __init__(self, path: Path):
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None
        self.lock = asyncio.Lock()

    async def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.initialize()

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None

    async def initialize(self):
        assert self.db
        await self.db.executescript("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            prefix TEXT DEFAULT '!',
            welcome_channel INTEGER,
            goodbye_channel INTEGER,
            log_channel INTEGER,
            levelup_channel INTEGER,
            ticket_category INTEGER,
            ticket_role INTEGER,
            ticket_counter INTEGER DEFAULT 0,
            auto_mod_enabled INTEGER DEFAULT 0,
            anti_link INTEGER DEFAULT 0,
            anti_caps INTEGER DEFAULT 0,
            anti_mention INTEGER DEFAULT 0,
            welcome_enabled INTEGER DEFAULT 0,
            level_enabled INTEGER DEFAULT 1,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_levels (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 0,
            messages INTEGER DEFAULT 0,
            last_xp INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            guild_id INTEGER,
            message TEXT NOT NULL,
            due_at INTEGER NOT NULL,
            delivered INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS afk (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT,
            since INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            guild_id INTEGER,
            text TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            votes_up INTEGER DEFAULT 0,
            votes_down INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS suggestion_votes (
            suggestion_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            vote INTEGER NOT NULL,
            PRIMARY KEY (suggestion_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS custom_tags (
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            content TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            uses INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, name)
        );

        CREATE TABLE IF NOT EXISTS command_stats (
            guild_id INTEGER,
            command_name TEXT NOT NULL,
            uses INTEGER DEFAULT 0,
            last_used INTEGER NOT NULL,
            PRIMARY KEY (guild_id, command_name)
        );

        CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            host_id INTEGER NOT NULL,
            prize TEXT NOT NULL,
            winners INTEGER NOT NULL,
            ends_at INTEGER NOT NULL,
            ended INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS giveaway_entries (
            giveaway_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (giveaway_id, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(delivered, due_at);
        CREATE INDEX IF NOT EXISTS idx_levels_xp ON user_levels(guild_id, xp DESC);
        CREATE INDEX IF NOT EXISTS idx_todos_user ON todos(user_id, guild_id, done);
        """)
        await self.db.commit()

    async def execute(self, sql: str, params=()):
        assert self.db
        async with self.lock:
            cur = await self.db.execute(sql, params)
            await self.db.commit()
            return cur

    async def fetchone(self, sql: str, params=()):
        assert self.db
        async with self.lock:
            cur = await self.db.execute(sql, params)
            return await cur.fetchone()

    async def fetchall(self, sql: str, params=()):
        assert self.db
        async with self.lock:
            cur = await self.db.execute(sql, params)
            return await cur.fetchall()


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int, timeout: float = 30):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: Optional[bool] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Nút này không dành cho bạn.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Xác nhận", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.stop()
        await interaction.response.edit_message(content="✅ Đã xác nhận.", view=None)

    @discord.ui.button(label="Hủy", emoji="✖️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.stop()
        await interaction.response.edit_message(content="🛑 Đã hủy.", view=None)


class PollView(discord.ui.View):
    def __init__(self, cog: "KaiPremium", poll_id: int, options: list[str], duration: int):
        super().__init__(timeout=duration)
        self.cog = cog
        self.poll_id = poll_id
        self.options = options
        self.votes: dict[int, int] = {}

        for i, option in enumerate(options[:5]):
            button = discord.ui.Button(
                label=f"{i+1}. {option[:70]}",
                style=discord.ButtonStyle.primary,
                custom_id=f"kai_poll:{poll_id}:{i}",
                row=i // 2,
            )
            button.callback = self._make_callback(i)
            self.add_item(button)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            self.votes[interaction.user.id] = index
            counts = Counter(self.votes.values())
            lines = []
            total = len(self.votes)
            for i, option in enumerate(self.options):
                n = counts.get(i, 0)
                pct = (n / total * 100) if total else 0
                lines.append(f"**{i+1}. {option}** — {n} vote ({pct:.0f}%)")
            embed = interaction.message.embeds[0].copy()
            embed.description = "\n".join(lines) + f"\n\n👥 Tổng lượt vote: **{total}**"
            await interaction.response.edit_message(embed=embed, view=self)
        return callback


class TicketView(discord.ui.View):
    def __init__(self, cog: "KaiPremium"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Mở Ticket", emoji="🎫", style=discord.ButtonStyle.success,
        custom_id="kai_ticket_open"
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("Chỉ dùng trong server.", ephemeral=True)

        cfg = await self.cog.get_config(guild.id)
        category = guild.get_channel(cfg["ticket_category"]) if cfg["ticket_category"] else None
        role = guild.get_role(cfg["ticket_role"]) if cfg["ticket_role"] else None

        existing = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel)
            and c.topic == f"KAI-TICKET:{interaction.user.id}",
            guild.text_channels
        )
        if existing:
            return await interaction.response.send_message(
                f"🎫 Bạn đã có ticket: {existing.mention}", ephemeral=True
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True,
                manage_messages=True, read_message_history=True
            ),
        }
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        number = int(cfg["ticket_counter"]) + 1
        await self.cog.db.execute(
            "UPDATE guild_config SET ticket_counter=?, updated_at=? WHERE guild_id=?",
            (number, now_ts(), guild.id)
        )
        channel_name = f"ticket-{number:04d}"
        try:
            channel = await guild.create_text_channel(
                channel_name, category=category, overwrites=overwrites,
                topic=f"KAI-TICKET:{interaction.user.id}"
            )
        except discord.HTTPException as exc:
            return await interaction.response.send_message(
                f"❌ Không tạo được ticket: `{exc}`", ephemeral=True
            )

        embed = discord.Embed(
            title="🎫 KaiAI Support Ticket",
            description=(
                f"Xin chào {interaction.user.mention}! Hãy mô tả vấn đề của bạn.\n"
                "Staff có thể xử lý ticket này và bấm **Đóng Ticket** khi hoàn tất."
            ),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        await channel.send(
            content=interaction.user.mention,
            embed=embed,
            view=CloseTicketView(self.cog)
        )
        await interaction.response.send_message(
            f"✅ Ticket đã tạo: {channel.mention}", ephemeral=True
        )


class CloseTicketView(discord.ui.View):
    def __init__(self, cog: "KaiPremium"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Đóng Ticket", emoji="🔒", style=discord.ButtonStyle.danger,
        custom_id="kai_ticket_close"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not channel.topic or not channel.topic.startswith("KAI-TICKET:"):
            return await interaction.response.send_message(
                "❌ Đây không phải kênh ticket của KaiAI.", ephemeral=True
            )

        owner_id = int(channel.topic.split(":", 1)[1])
        allowed = interaction.user.id == owner_id or interaction.user.guild_permissions.manage_channels
        if not allowed:
            return await interaction.response.send_message(
                "❌ Bạn không có quyền đóng ticket này.", ephemeral=True
            )

        await interaction.response.send_message("🔒 Ticket sẽ được đóng sau 3 giây.")
        await asyncio.sleep(3)
        try:
            await channel.delete(reason=f"KaiAI ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass


class GiveawayView(discord.ui.View):
    def __init__(self, cog: "KaiPremium", giveaway_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.giveaway_id = giveaway_id

    @discord.ui.button(
        label="Tham gia", emoji="🎉", style=discord.ButtonStyle.success,
        custom_id="kai_giveaway_join"
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await self.cog.db.fetchone(
            "SELECT ended FROM giveaways WHERE id=?", (self.giveaway_id,)
        )
        if not row or row["ended"]:
            return await interaction.response.send_message("❌ Giveaway đã kết thúc.", ephemeral=True)

        existing = await self.cog.db.fetchone(
            "SELECT 1 FROM giveaway_entries WHERE giveaway_id=? AND user_id=?",
            (self.giveaway_id, interaction.user.id)
        )
        if existing:
            await self.cog.db.execute(
                "DELETE FROM giveaway_entries WHERE giveaway_id=? AND user_id=?",
                (self.giveaway_id, interaction.user.id)
            )
            return await interaction.response.send_message("↩️ Đã rời giveaway.", ephemeral=True)

        await self.cog.db.execute(
            "INSERT OR IGNORE INTO giveaway_entries(giveaway_id,user_id) VALUES(?,?)",
            (self.giveaway_id, interaction.user.id)
        )
        await interaction.response.send_message("🎉 Bạn đã tham gia!", ephemeral=True)


class SuggestionView(discord.ui.View):
    def __init__(self, cog: "KaiPremium", suggestion_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.suggestion_id = suggestion_id

    async def vote(self, interaction: discord.Interaction, value: int):
        existing = await self.cog.db.fetchone(
            "SELECT vote FROM suggestion_votes WHERE suggestion_id=? AND user_id=?",
            (self.suggestion_id, interaction.user.id)
        )
        if existing and existing["vote"] == value:
            await self.cog.db.execute(
                "DELETE FROM suggestion_votes WHERE suggestion_id=? AND user_id=?",
                (self.suggestion_id, interaction.user.id)
            )
            await self.cog.db.execute(
                "UPDATE suggestions SET votes_up=votes_up-?, votes_down=votes_down-? WHERE id=?",
                (1 if value == 1 else 0, 1 if value == -1 else 0, self.suggestion_id)
            )
            text = "↩️ Đã bỏ phiếu."
        else:
            if existing:
                old_value = existing["vote"]
                if old_value == value:
                    return
                await self.cog.db.execute(
                    "UPDATE suggestion_votes SET vote=? WHERE suggestion_id=? AND user_id=?",
                    (value, self.suggestion_id, interaction.user.id)
                )
                await self.cog.db.execute(
                    "UPDATE suggestions SET votes_up=votes_up+?, votes_down=votes_down+? WHERE id=?",
                    (
                        (1 if value == 1 else 0) - (1 if old_value == 1 else 0),
                        (1 if value == -1 else 0) - (1 if old_value == -1 else 0),
                        self.suggestion_id,
                    )
                )
            else:
                await self.cog.db.execute(
                    "INSERT INTO suggestion_votes(suggestion_id,user_id,vote) VALUES(?,?,?)",
                    (self.suggestion_id, interaction.user.id, value)
                )
                await self.cog.db.execute(
                    "UPDATE suggestions SET votes_up=votes_up+?, votes_down=votes_down+? WHERE id=?",
                    (1 if value == 1 else 0, 1 if value == -1 else 0, self.suggestion_id)
                )
            text = "👍 Đã ghi nhận phiếu."
        row = await self.cog.db.fetchone(
            "SELECT votes_up,votes_down FROM suggestions WHERE id=?", (self.suggestion_id,)
        )
        await interaction.response.send_message(
            f"{text} 👍 **{row['votes_up']}** · 👎 **{row['votes_down']}**", ephemeral=True
        )

    @discord.ui.button(label="Ủng hộ", emoji="👍", style=discord.ButtonStyle.success, custom_id="kai_suggest_up")
    async def up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, 1)

    @discord.ui.button(label="Không đồng ý", emoji="👎", style=discord.ButtonStyle.danger, custom_id="kai_suggest_down")
    async def down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.vote(interaction, -1)


class KaiPremium(commands.Cog):
    """Premium feature suite for KaiAI."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Database(DB_PATH)
        self.started_at = time.time()
        self.message_times: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=8))
        self.xp_cooldown: dict[tuple[int, int], float] = {}
        self.command_counter = Counter()
        self._ready_once = False
        self.reminder_loop.start()
        self.giveaway_loop.start()

    async def cog_load(self):
        await self.db.connect()
        # Persistent buttons survive restarts.
        self.bot.add_view(TicketView(self))
        self.bot.add_view(CloseTicketView(self))
        # Restore active giveaway buttons after a process restart.
        active_giveaways = await self.db.fetchall(
            "SELECT id,message_id FROM giveaways WHERE ended=0 AND ends_at>?",
            (now_ts(),)
        )
        for row in active_giveaways:
            try:
                self.bot.add_view(GiveawayView(self, row["id"]), message_id=row["message_id"])
            except (ValueError, discord.HTTPException):
                pass

    async def cog_unload(self):
        self.reminder_loop.cancel()
        self.giveaway_loop.cancel()
        await self.db.close()

    # --------------------------- CONFIG ---------------------------

    async def get_config(self, guild_id: int):
        row = await self.db.fetchone("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
        if row:
            return row
        await self.db.execute(
            "INSERT INTO guild_config(guild_id,updated_at) VALUES(?,?)",
            (guild_id, now_ts())
        )
        return await self.db.fetchone("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))

    async def set_config(self, guild_id: int, **values):
        if not values:
            return
        await self.get_config(guild_id)
        allowed = {
            "prefix", "welcome_channel", "goodbye_channel", "log_channel",
            "levelup_channel", "ticket_category", "ticket_role", "ticket_counter",
            "auto_mod_enabled", "anti_link", "anti_caps", "anti_mention",
            "welcome_enabled", "level_enabled",
        }
        values = {k: v for k, v in values.items() if k in allowed}
        if not values:
            return
        fields = ", ".join(f"{k}=?" for k in values)
        params = list(values.values()) + [now_ts(), guild_id]
        await self.db.execute(
            f"UPDATE guild_config SET {fields}, updated_at=? WHERE guild_id=?",
            params
        )

    async def log(self, guild: discord.Guild, title: str, description: str, *, color=discord.Color.blurple()):
        cfg = await self.get_config(guild.id)
        channel = guild.get_channel(cfg["log_channel"]) if cfg["log_channel"] else None
        if not channel:
            return
        embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # --------------------------- UI / HELP ---------------------------

    @commands.command(name="kai", aliases=["kaiai", "panel", "menuplus"])
    async def kai_panel(self, ctx: commands.Context):
        """Trung tâm điều khiển KaiAI Premium."""
        embed = discord.Embed(
            title="✨ KAI AI — PREMIUM COMMAND CENTER",
            description=(
                "**KaiAI** đã sẵn sàng. Đây là bộ điều khiển tổng hợp cho hệ thống mới.\n"
                "Bạn vẫn có thể dùng toàn bộ lệnh cũ từ các Cog legacy."
            ),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(
            name="🤖 AI",
            value="`!ai <câu hỏi>` · `!mood` · `!clearai` · `!stats`",
            inline=False,
        )
        embed.add_field(
            name="🎮 Tiện ích",
            value="`!ping` `!serverinfo` `!userinfo` `!avatar` `!choose` `!roll` `!8ball`",
            inline=False,
        )
        embed.add_field(
            name="🧠 Level",
            value="`!rank` `!levels` — XP tự động theo hoạt động.",
            inline=False,
        )
        embed.add_field(
            name="📝 Productivity",
            value="`!remind` `!todo` `!afk` `!suggest`",
            inline=False,
        )
        embed.add_field(
            name="🎫 Community",
            value="`!ticketsetup` `!poll` `!giveaway` `!tag`",
            inline=False,
        )
        embed.add_field(
            name="🛡️ Server",
            value="`!kaiconfig` `!backupkai` — dành cho Admin.",
            inline=False,
        )
        embed.set_footer(text="KaiAI • Built for speed, reliability & beautiful Discord UX")
        await ctx.reply(embed=embed)

    @app_commands.command(name="kai", description="Mở trung tâm điều khiển KaiAI Premium")
    async def slash_kai(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="✨ KAI AI — PREMIUM",
            description="AI • Moderation • Leveling • Productivity • Community",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="💬 AI", value="Ping KaiAI hoặc dùng các lệnh AI hiện có.", inline=False)
        embed.add_field(name="🧠 Level", value="`!rank` · `!levels`", inline=True)
        embed.add_field(name="🎫 Community", value="`!ticketsetup` · `!poll` · `!giveaway`", inline=True)
        embed.set_footer(text="KaiAI • Premium Core")
        await interaction.response.send_message(embed=embed)

    @commands.command(name="ping")
    async def ping(self, ctx):
        before = time.perf_counter()
        msg = await ctx.reply("🏓 Đang đo độ trễ...")
        elapsed = round((time.perf_counter() - before) * 1000)
        ws = round(self.bot.latency * 1000)
        await msg.edit(content=f"🏓 **PONG!**\n⚡ WebSocket: **{ws}ms**\n📨 API round-trip: **{elapsed}ms**")

    @commands.command(name="botinfo", aliases=["aboutkai", "kaiinfo"])
    async def info_bot(self, ctx):
        embed = discord.Embed(title="🤖 KAI AI", color=discord.Color.blurple())
        embed.description = (
            "Trợ lý Discord đa năng với AI, moderation, economy legacy, "
            "leveling, productivity và community automation."
        )
        embed.add_field(name="Servers", value=f"`{len(self.bot.guilds):,}`")
        embed.add_field(name="Cogs", value=f"`{len(self.bot.cogs)}`")
        embed.add_field(name="Commands", value=f"`{len(self.bot.commands)}`")
        embed.add_field(name="Uptime", value=f"`{human_seconds(time.time()-self.started_at)}`")
        embed.add_field(name="Python", value=f"`{__import__('platform').python_version()}`")
        embed.add_field(name="discord.py", value=f"`{discord.__version__}`")
        embed.set_footer(text="KaiAI • Premium Core")
        await ctx.reply(embed=embed)

    # --------------------------- PROFILE / SERVER ---------------------------

    @commands.command(name="userinfo", aliases=["ui", "whois"])
    async def user_info(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        roles = [r.mention for r in reversed(member.roles[1:])][:15]
        embed = discord.Embed(
            title=f"👤 {member.display_name}",
            color=member.color if member.color != discord.Color.default() else discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Tên", value=f"`{member}`", inline=True)
        embed.add_field(name="ID", value=f"`{member.id}`", inline=True)
        embed.add_field(name="Bot", value="✅" if member.bot else "❌", inline=True)
        embed.add_field(name="Tham gia", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Không rõ", inline=True)
        embed.add_field(name="Tài khoản", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        embed.add_field(name="Role cao nhất", value=member.top_role.mention, inline=True)
        embed.add_field(name=f"Roles ({len(member.roles)-1})", value=" ".join(roles) or "Không có", inline=False)
        await ctx.reply(embed=embed)

    @commands.command(name="avatar", aliases=["av", "pfp"])
    async def avatar(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        embed = discord.Embed(title=f"🖼️ Avatar của {member.display_name}", color=discord.Color.blurple())
        embed.set_image(url=member.display_avatar.replace(size=1024).url)
        embed.set_footer(text=f"ID: {member.id}")
        await ctx.reply(embed=embed)

    @commands.command(name="serverinfo", aliases=["si", "server"])
    async def server_info(self, ctx):
        g = ctx.guild
        if not g:
            return await ctx.reply("❌ Lệnh này chỉ dùng trong server.")
        humans = sum(not m.bot for m in g.members)
        bots = sum(m.bot for m in g.members)
        channels = len(g.channels)
        embed = discord.Embed(title=f"🏰 {g.name}", color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="Owner", value=g.owner.mention if g.owner else f"`{g.owner_id}`", inline=True)
        embed.add_field(name="ID", value=f"`{g.id}`", inline=True)
        embed.add_field(name="Members", value=f"👤 {humans:,} · 🤖 {bots:,}", inline=True)
        embed.add_field(name="Channels", value=f"`{channels}`", inline=True)
        embed.add_field(name="Roles", value=f"`{len(g.roles)}`", inline=True)
        embed.add_field(name="Boost", value=f"Level **{g.premium_tier}** · `{g.premium_subscription_count or 0}` boosts", inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "F"), inline=False)
        await ctx.reply(embed=embed)

    # --------------------------- FUN ---------------------------

    @commands.command(name="choose", aliases=["chon"])
    async def choose(self, ctx, *, choices: str):
        items = [x.strip() for x in re.split(r"\s*(?:\||,|/|\n)\s*", choices) if x.strip()]
        if len(items) < 2:
            return await ctx.reply("❌ Hãy đưa ít nhất 2 lựa chọn, ngăn cách bằng `|`, `,` hoặc `/`.")
        choice = random.choice(items)
        embed = discord.Embed(
            title="🎯 KAI CHOICE ENGINE",
            description=f"**KaiAI đã chọn:**\n# {choice}",
            color=discord.Color.gold()
        )
        await ctx.reply(embed=embed)

    @commands.command(name="roll")
    async def roll(self, ctx, expression: str = "1d6"):
        match = re.fullmatch(r"(\d{1,3})d(\d{1,5})(?:([+-])(\d{1,6}))?", expression.lower())
        if not match:
            return await ctx.reply("❌ Cú pháp: `!roll 2d20+5`.")
        count, sides = int(match.group(1)), int(match.group(2))
        if count < 1 or count > 100 or sides < 2 or sides > 10000:
            return await ctx.reply("❌ Giới hạn: 1–100 viên, mỗi viên 2–10000 mặt.")
        values = [random.randint(1, sides) for _ in range(count)]
        modifier = int(match.group(4) or 0) * (-1 if match.group(3) == "-" else 1)
        total = sum(values) + modifier
        shown = ", ".join(map(str, values[:30])) + ("…" if len(values) > 30 else "")
        await ctx.reply(f"🎲 **{expression}** → `{shown}`\n🏆 Tổng: **{total}**")

    @commands.command(name="8ball", aliases=["8b", "boi"])
    async def eight_ball(self, ctx, *, question: str):
        answers = [
            "Chắc chắn rồi. ✨", "Khả năng rất cao. 🔮", "Có vẻ là vậy.",
            "Hãy thử thêm một lần nữa.", "Chưa thể nói chắc.",
            "Khả năng thấp.", "Không. 😅", "KaiAI cảm thấy… đừng. 💀",
            "Vũ trụ chưa tiết lộ đáp án.", "Có, nhưng bạn chưa sẵn sàng."
        ]
        embed = discord.Embed(title="🔮 KAI 8-BALL", color=discord.Color.purple())
        embed.add_field(name="Câu hỏi", value=question[:1000], inline=False)
        embed.add_field(name="Phán quyết", value=random.choice(answers), inline=False)
        await ctx.reply(embed=embed)

    @commands.command(name="coin", aliases=["flip"])
    async def coin(self, ctx):
        await ctx.reply(f"🪙 KaiAI tung đồng xu… **{random.choice(['NGỬA 🦅', 'SẤP 🪙'])}**!")

    # --------------------------- LEVELING ---------------------------

    async def add_xp(self, guild: discord.Guild, member: discord.Member):
        cfg = await self.get_config(guild.id)
        if not cfg["level_enabled"] or member.bot:
            return
        key = (guild.id, member.id)
        current = time.time()
        if current - self.xp_cooldown.get(key, 0) < XP_COOLDOWN:
            return
        self.xp_cooldown[key] = current
        gained = random.randint(12, 24)

        row = await self.db.fetchone(
            "SELECT xp,level,messages FROM user_levels WHERE guild_id=? AND user_id=?",
            (guild.id, member.id)
        )
        old_level = row["level"] if row else 0
        old_xp = row["xp"] if row else 0
        new_xp = old_xp + gained
        new_level = level_from_xp(new_xp)
        await self.db.execute(
            """INSERT INTO user_levels(guild_id,user_id,xp,level,messages,last_xp)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET
               xp=excluded.xp,level=excluded.level,messages=user_levels.messages+1,last_xp=excluded.last_xp""",
            (guild.id, member.id, new_xp, new_level, 1, now_ts())
        )

        if new_level > old_level:
            channel_id = cfg["levelup_channel"]
            if not channel_id and member.guild.system_channel:
                channel_id = member.guild.system_channel.id
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel:
                embed = discord.Embed(
                    title="🎉 LEVEL UP!",
                    description=f"{member.mention} đã lên **Level {new_level}**!",
                    color=discord.Color.gold()
                )
                embed.add_field(name="XP", value=f"`{new_xp:,}`")
                embed.set_thumbnail(url=member.display_avatar.url)
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    pass

    @commands.command(name="rank", aliases=["level", "xp"])
    async def rank(self, ctx, member: Optional[discord.Member] = None):
        member = member or ctx.author
        row = await self.db.fetchone(
            "SELECT xp,level,messages FROM user_levels WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, member.id)
        )
        xp, level, messages = (row["xp"], row["level"], row["messages"]) if row else (0, 0, 0)
        next_xp = xp_for_level(level + 1)
        previous_xp = xp_for_level(level)
        progress = max(0, xp - previous_xp)
        target = max(1, next_xp - previous_xp)
        embed = discord.Embed(title=f"🏆 RANK — {member.display_name}", color=discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=f"**{level}**", inline=True)
        embed.add_field(name="Tổng XP", value=f"**{xp:,}**", inline=True)
        embed.add_field(name="Messages", value=f"**{messages:,}**", inline=True)
        embed.add_field(name="Tiến trình", value=f"{progress_bar(progress,target)} `{progress:,}/{target:,}`", inline=False)
        await ctx.reply(embed=embed)

    @commands.command(name="levels", aliases=["leaderboard", "xptop"])
    async def levels(self, ctx):
        rows = await self.db.fetchall(
            "SELECT user_id,xp,level,messages FROM user_levels WHERE guild_id=? ORDER BY xp DESC LIMIT 10",
            (ctx.guild.id,)
        )
        if not rows:
            return await ctx.reply("📊 Chưa có dữ liệu XP.")
        lines = []
        for i, row in enumerate(rows, 1):
            member = ctx.guild.get_member(row["user_id"])
            name = member.display_name if member else f"User {row['user_id']}"
            lines.append(f"`#{i:02}` **{name}** — Lv.{row['level']} · {row['xp']:,} XP")
        embed = discord.Embed(title="🏆 KAI LEVEL LEADERBOARD", description="\n".join(lines), color=discord.Color.gold())
        await ctx.reply(embed=embed)

    # --------------------------- REMINDERS ---------------------------

    @commands.command(name="remind", aliases=["reminder", "nhac"])
    @commands.cooldown(2, 60, commands.BucketType.user)
    async def remind(self, ctx, duration: str, *, message: str):
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            return await ctx.reply(f"❌ {exc}")
        due = now_ts() + seconds
        await self.db.execute(
            "INSERT INTO reminders(user_id,channel_id,guild_id,message,due_at,created_at) VALUES(?,?,?,?,?,?)",
            (ctx.author.id, ctx.channel.id, ctx.guild.id if ctx.guild else None, message[:1000], due, now_ts())
        )
        await ctx.reply(f"⏰ Đã đặt nhắc nhở sau **{human_seconds(seconds)}** — <t:{due}:R>.")

    @tasks.loop(seconds=5)
    async def reminder_loop(self):
        if not self.db.db:
            return
        rows = await self.db.fetchall(
            "SELECT * FROM reminders WHERE delivered=0 AND due_at<=? ORDER BY due_at LIMIT 25",
            (now_ts(),)
        )
        for row in rows:
            try:
                channel = self.bot.get_channel(row["channel_id"])
                if channel:
                    await channel.send(
                        f"⏰ <@{row['user_id']}> **Nhắc nhở:** {row['message']}",
                        allowed_mentions=discord.AllowedMentions(users=True)
                    )
            except discord.HTTPException:
                pass
            finally:
                await self.db.execute("UPDATE reminders SET delivered=1 WHERE id=?", (row["id"],))

    @reminder_loop.before_loop
    async def before_reminders(self):
        await self.bot.wait_until_ready()

    # --------------------------- AFK ---------------------------

    @commands.command(name="afk")
    async def afk(self, ctx, *, reason: str = "Không có mặt"):
        if not ctx.guild:
            return await ctx.reply("❌ AFK chỉ hoạt động trong server.")
        await self.db.execute(
            """INSERT INTO afk(guild_id,user_id,reason,since) VALUES(?,?,?,?)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET reason=excluded.reason,since=excluded.since""",
            (ctx.guild.id, ctx.author.id, reason[:300], now_ts())
        )
        await ctx.reply(f"💤 Đã bật AFK: **{reason[:300]}**")

    async def clear_afk(self, member: discord.Member):
        row = await self.db.fetchone(
            "SELECT reason,since FROM afk WHERE guild_id=? AND user_id=?",
            (member.guild.id, member.id)
        )
        if not row:
            return
        await self.db.execute("DELETE FROM afk WHERE guild_id=? AND user_id=?", (member.guild.id, member.id))
        try:
            await member.guild.system_channel.send(
                f"👋 Chào mừng {member.mention} quay lại! AFK **{human_seconds(now_ts()-row['since'])}**."
            ) if member.guild.system_channel else None
        except discord.HTTPException:
            pass

    # --------------------------- TODO ---------------------------

    @commands.group(name="todo", invoke_without_command=True)
    async def todo(self, ctx, *, text: str = None):
        if text:
            await self.db.execute(
                "INSERT INTO todos(user_id,guild_id,text,created_at) VALUES(?,?,?,?)",
                (ctx.author.id, ctx.guild.id if ctx.guild else None, text[:500], now_ts())
            )
            return await ctx.reply("✅ Đã thêm việc vào Todo.")
        rows = await self.db.fetchall(
            "SELECT id,text,done FROM todos WHERE user_id=? AND guild_id IS ? ORDER BY id DESC LIMIT 15",
            (ctx.author.id, ctx.guild.id if ctx.guild else None)
        )
        if not rows:
            return await ctx.reply("📝 Todo của bạn đang trống. Dùng `!todo <việc cần làm>`.")
        lines = [f"{'✅' if r['done'] else '⬜'} `#{r['id']}` {r['text']}" for r in rows]
        await ctx.reply("📝 **TODO CỦA BẠN**\n" + "\n".join(lines))

    @todo.command(name="done")
    async def todo_done(self, ctx, item_id: int):
        row = await self.db.fetchone(
            "SELECT id FROM todos WHERE id=? AND user_id=? AND guild_id IS ?",
            (item_id, ctx.author.id, ctx.guild.id if ctx.guild else None)
        )
        if not row:
            return await ctx.reply("❌ Không tìm thấy task.")
        await self.db.execute("UPDATE todos SET done=1 WHERE id=?", (item_id,))
        await ctx.reply(f"✅ Đã hoàn thành task `#{item_id}`.")

    @todo.command(name="delete", aliases=["del", "xoa"])
    async def todo_delete(self, ctx, item_id: int):
        await self.db.execute(
            "DELETE FROM todos WHERE id=? AND user_id=? AND guild_id IS ?",
            (item_id, ctx.author.id, ctx.guild.id if ctx.guild else None)
        )
        await ctx.reply(f"🗑️ Đã xóa task `#{item_id}`.")

    # --------------------------- SUGGESTIONS ---------------------------

    @commands.command(name="suggest", aliases=["suggestion", "dexuat"])
    @commands.cooldown(2, 60, commands.BucketType.user)
    async def suggest(self, ctx, *, text: str):
        if not ctx.guild:
            return await ctx.reply("❌ Chỉ dùng trong server.")
        text = text.strip()
        if len(text) < 5:
            return await ctx.reply("❌ Đề xuất quá ngắn.")
        cur = await self.db.execute(
            "INSERT INTO suggestions(guild_id,user_id,text,created_at) VALUES(?,?,?,?)",
            (ctx.guild.id, ctx.author.id, text[:1800], now_ts())
        )
        sid = cur.lastrowid
        embed = discord.Embed(
            title=f"💡 ĐỀ XUẤT #{sid}",
            description=text[:1800],
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Đề xuất bởi {ctx.author.display_name}")
        msg = await ctx.send(embed=embed, view=SuggestionView(self, sid))
        await self.log(ctx.guild, "💡 Suggestion mới", f"{ctx.author.mention} tạo đề xuất #{sid}: {text[:500]}")
        await self.db.execute("UPDATE suggestions SET status=? WHERE id=?", ("posted", sid))
        await msg.add_reaction("💡")

    # --------------------------- POLLS ---------------------------

    @commands.command(name="poll", aliases=["vote", "binhchon"])
    @commands.has_permissions(manage_messages=True)
    async def poll(self, ctx, duration: str = "1h", *, raw: str):
        """
        Syntax: !poll 2h Câu hỏi | Lựa chọn 1 | Lựa chọn 2 | ...
        """
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        if len(parts) < 3:
            return await ctx.reply("❌ Cú pháp: `!poll 2h Câu hỏi | A | B | C`.")
        question, options = parts[0], parts[1:6]
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            return await ctx.reply(f"❌ {exc}")
        ends = now_ts() + seconds
        embed = discord.Embed(
            title="📊 KAI POLL",
            description="\n".join(f"**{i+1}. {x}** — 0 vote" for i,x in enumerate(options)),
            color=discord.Color.blurple()
        )
        embed.add_field(name="Câu hỏi", value=question[:1000], inline=False)
        embed.set_footer(text=f"Kết thúc {dt.datetime.fromtimestamp(ends, dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        await ctx.send(embed=embed, view=PollView(self, 0, options, seconds))

    # --------------------------- TICKETS ---------------------------

    @commands.command(name="ticketsetup")
    @commands.has_permissions(administrator=True)
    async def ticket_setup(self, ctx, category: Optional[discord.CategoryChannel] = None, role: Optional[discord.Role] = None):
        category = category or discord.utils.get(ctx.guild.categories, name="🎫 TICKETS")
        if not category:
            category = await ctx.guild.create_category("🎫 TICKETS")
        await self.set_config(ctx.guild.id, ticket_category=category.id, ticket_role=role.id if role else None)
        embed = discord.Embed(
            title="🎫 KAI SUPPORT CENTER",
            description="Bấm **Mở Ticket** để tạo một phòng hỗ trợ riêng tư.\nStaff sẽ được cấp quyền tự động.",
            color=discord.Color.blurple()
        )
        await ctx.send(embed=embed, view=TicketView(self))

    # --------------------------- GIVEAWAYS ---------------------------

    @commands.command(name="giveaway", aliases=["ga"])
    @commands.has_permissions(manage_guild=True)
    async def giveaway(self, ctx, duration: str, winners: int, *, prize: str):
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            return await ctx.reply(f"❌ {exc}")
        if not 1 <= winners <= 20:
            return await ctx.reply("❌ Số người thắng phải từ 1 đến 20.")
        ends = now_ts() + seconds
        embed = discord.Embed(
            title="🎉 GIVEAWAY — KAI AI",
            description=(
                f"🎁 **Phần thưởng:** {prize[:500]}\n"
                f"🏆 **Số người thắng:** {winners}\n"
                f"⏰ **Kết thúc:** <t:{ends}:R>\n\n"
                "Bấm nút bên dưới để tham gia. Bấm lần nữa để rời."
            ),
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"Host: {ctx.author.display_name}")
        msg = await ctx.send(embed=embed)
        cur = await self.db.execute(
            "INSERT INTO giveaways(guild_id,channel_id,message_id,host_id,prize,winners,ends_at) VALUES(?,?,?,?,?,?,?)",
            (ctx.guild.id, ctx.channel.id, msg.id, ctx.author.id, prize[:500], winners, ends)
        )
        gid = cur.lastrowid
        await msg.edit(view=GiveawayView(self, gid))

    @tasks.loop(seconds=10)
    async def giveaway_loop(self):
        if not self.db.db:
            return
        rows = await self.db.fetchall(
            "SELECT * FROM giveaways WHERE ended=0 AND ends_at<=? LIMIT 10", (now_ts(),)
        )
        for row in rows:
            await self.finish_giveaway(row)

    @giveaway_loop.before_loop
    async def before_giveaways(self):
        await self.bot.wait_until_ready()

    async def finish_giveaway(self, row):
        await self.db.execute("UPDATE giveaways SET ended=1 WHERE id=?", (row["id"],))
        entries = await self.db.fetchall(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (row["id"],)
        )
        channel = self.bot.get_channel(row["channel_id"])
        if not channel:
            return
        if not entries:
            return await channel.send(f"🎉 Giveaway `{row['id']}` kết thúc nhưng không có người tham gia.")
        winners = random.sample(entries, min(row["winners"], len(entries)))
        mentions = ", ".join(f"<@{x['user_id']}>" for x in winners)
        await channel.send(
            f"🎊 **GIVEAWAY KẾT THÚC!**\n🎁 {row['prize']}\n🏆 Người thắng: {mentions}"
        )

    # --------------------------- TAGS ---------------------------

    @commands.group(name="tag", invoke_without_command=True)
    async def tag(self, ctx, name: str = None, *, content: str = None):
        if not name:
            return await ctx.reply("🏷️ Dùng `!tag <tên>` để gọi tag hoặc `!tag create <tên> <nội dung>`.")
        row = await self.db.fetchone(
            "SELECT content,uses FROM custom_tags WHERE guild_id=? AND name=?",
            (ctx.guild.id, name.lower())
        )
        if not row:
            return await ctx.reply("❌ Không tìm thấy tag.")
        await self.db.execute(
            "UPDATE custom_tags SET uses=uses+1 WHERE guild_id=? AND name=?",
            (ctx.guild.id, name.lower())
        )
        await ctx.reply(row["content"])

    @tag.command(name="create", aliases=["new"])
    @commands.has_permissions(manage_guild=True)
    async def tag_create(self, ctx, name: str, *, content: str):
        name = name.lower().strip()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
            return await ctx.reply("❌ Tên tag chỉ dùng chữ, số, `_`, `-` và tối đa 32 ký tự.")
        await self.db.execute(
            """INSERT INTO custom_tags(guild_id,name,content,owner_id,created_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(guild_id,name) DO UPDATE SET content=excluded.content,owner_id=excluded.owner_id""",
            (ctx.guild.id, name, content[:1800], ctx.author.id, now_ts())
        )
        await ctx.reply(f"✅ Đã lưu tag **{name}**.")

    @tag.command(name="delete", aliases=["del", "remove"])
    @commands.has_permissions(manage_guild=True)
    async def tag_delete(self, ctx, name: str):
        await self.db.execute(
            "DELETE FROM custom_tags WHERE guild_id=? AND name=?", (ctx.guild.id, name.lower())
        )
        await ctx.reply(f"🗑️ Đã xóa tag **{name}**.")

    @tag.command(name="list", aliases=["all"])
    async def tag_list(self, ctx):
        rows = await self.db.fetchall(
            "SELECT name,uses FROM custom_tags WHERE guild_id=? ORDER BY uses DESC,name LIMIT 50",
            (ctx.guild.id,)
        )
        if not rows:
            return await ctx.reply("🏷️ Server chưa có tag.")
        await ctx.reply("🏷️ **TAGS**\n" + " · ".join(f"`{r['name']}` ({r['uses']})" for r in rows))

    # --------------------------- CONFIG / BACKUP ---------------------------

    @commands.group(name="kaiconfig", invoke_without_command=True)
    @commands.has_permissions(administrator=True)
    async def kai_config(self, ctx):
        cfg = await self.get_config(ctx.guild.id)
        embed = discord.Embed(title="⚙️ KAI AI SERVER CONFIG", color=discord.Color.dark_blue())
        def ch(cid):
            return f"<#{cid}>" if cid else "Chưa đặt"
        embed.add_field(name="Welcome", value=f"{'ON' if cfg['welcome_enabled'] else 'OFF'} · {ch(cfg['welcome_channel'])}", inline=True)
        embed.add_field(name="Level", value=f"{'ON' if cfg['level_enabled'] else 'OFF'} · {ch(cfg['levelup_channel'])}", inline=True)
        embed.add_field(name="Log", value=ch(cfg['log_channel']), inline=True)
        embed.add_field(name="Ticket", value=f"{ch(cfg['ticket_category'])} · Role `{cfg['ticket_role'] or 'none'}`", inline=False)
        embed.add_field(name="AutoMod", value=f"{'ON' if cfg['auto_mod_enabled'] else 'OFF'} · link={cfg['anti_link']} caps={cfg['anti_caps']} mention={cfg['anti_mention']}", inline=False)
        await ctx.reply(embed=embed)

    @kai_config.command(name="welcome")
    async def config_welcome(self, ctx, channel: discord.TextChannel):
        await self.set_config(ctx.guild.id, welcome_channel=channel.id, welcome_enabled=1)
        await ctx.reply(f"👋 Welcome channel: {channel.mention}")

    @kai_config.command(name="goodbye")
    async def config_goodbye(self, ctx, channel: discord.TextChannel):
        await self.set_config(ctx.guild.id, goodbye_channel=channel.id)
        await ctx.reply(f"👋 Goodbye channel: {channel.mention}")

    @kai_config.command(name="levelup")
    async def config_levelup(self, ctx, channel: discord.TextChannel):
        await self.set_config(ctx.guild.id, levelup_channel=channel.id, level_enabled=1)
        await ctx.reply(f"🏆 Level-up channel: {channel.mention}")

    @kai_config.command(name="log")
    async def config_log(self, ctx, channel: discord.TextChannel):
        await self.set_config(ctx.guild.id, log_channel=channel.id)
        await ctx.reply(f"📋 KaiAI log channel: {channel.mention}")

    @kai_config.command(name="levels")
    async def config_levels(self, ctx, enabled: str):
        value = enabled.lower() in {"on", "true", "1", "yes", "bat", "bật"}
        await self.set_config(ctx.guild.id, level_enabled=int(value))
        await ctx.reply(f"🏆 Level system: **{'ON' if value else 'OFF'}**")

    @kai_config.command(name="automod")
    async def config_automod(self, ctx, mode: str = "on", link: str = "on", caps: str = "off", mention: str = "off"):
        values = lambda x: int(x.lower() in {"on","true","1","yes","bật"})
        await self.set_config(
            ctx.guild.id,
            auto_mod_enabled=values(mode),
            anti_link=values(link),
            anti_caps=values(caps),
            anti_mention=values(mention)
        )
        await ctx.reply(
            f"🛡️ AutoMod: **{mode.upper()}** · link={link.upper()} · caps={caps.upper()} · mention={mention.upper()}"
        )

    @commands.command(name="backupkai")
    @commands.has_permissions(administrator=True)
    async def backup_kai(self, ctx):
        files = []
        for pattern in ("*.db", "*.json"):
            files.extend(DATA_DIR.glob(pattern))
        if not files:
            return await ctx.reply("❌ Chưa có file dữ liệu để backup.")
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = DATA_DIR / f"KaiAI_backup_{stamp}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                if f.name == out.name:
                    continue
                try:
                    zf.write(f, f.name)
                except OSError:
                    pass
        try:
            await ctx.author.send(
                content=f"📦 Backup KaiAI `{stamp}`",
                file=discord.File(out)
            )
            await ctx.reply("✅ Backup đã được gửi vào DM.")
        except discord.HTTPException:
            await ctx.reply("⚠️ Không gửi được DM. Hãy bật DM rồi thử lại.")
        finally:
            try:
                out.unlink()
            except OSError:
                pass

    # --------------------------- WELCOME / MODERATION ---------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = await self.get_config(member.guild.id)
        if not cfg["welcome_enabled"] or not cfg["welcome_channel"]:
            return
        channel = member.guild.get_channel(cfg["welcome_channel"])
        if not channel:
            return
        embed = discord.Embed(
            title="👋 CHÀO MỪNG ĐẾN SERVER!",
            description=f"Xin chào {member.mention}! Chúc bạn có trải nghiệm tuyệt vời cùng **{member.guild.name}**.",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Thành viên thứ", value=f"`{member.guild.member_count or '?'}`")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = await self.get_config(member.guild.id)
        if not cfg["goodbye_channel"]:
            return
        channel = member.guild.get_channel(cfg["goodbye_channel"])
        if not channel:
            return
        try:
            await channel.send(f"👋 **{member.display_name}** đã rời server. Chúc bạn một ngày tốt lành!")
        except discord.HTTPException:
            pass

    def moderation_violation(self, message: discord.Message, cfg) -> Optional[str]:
        content = message.content
        if cfg["anti_link"] and re.search(r"https?://\S+", content, re.I):
            # Do not treat Discord's own message links as spam.
            if "discord.com/channels/" not in content and "discordapp.com/channels/" not in content:
                return "link"
        if cfg["anti_caps"] and len(content) >= 12:
            letters = [c for c in content if c.isalpha()]
            if len(letters) >= 8:
                ratio = sum(c.isupper() for c in letters) / len(letters)
                if ratio >= 0.80:
                    return "caps"
        if cfg["anti_mention"] and len(message.mentions) >= 6:
            return "mention"
        return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # AFK lookup / clear
        mentioned = set(m.id for m in message.mentions)
        for uid in mentioned:
            row = await self.db.fetchone(
                "SELECT reason,since FROM afk WHERE guild_id=? AND user_id=?",
                (message.guild.id, uid)
            )
            if row:
                await message.channel.send(
                    f"💤 **{message.guild.get_member(uid).display_name if message.guild.get_member(uid) else 'User'}** đang AFK: "
                    f"{row['reason']} · <t:{row['since']}:R>"
                )
        await self.clear_afk(message.author)

        cfg = await self.get_config(message.guild.id)

        # Lightweight anti-spam rate limiter.
        times = self.message_times[message.author.id]
        now = time.time()
        times.append(now)
        recent = [t for t in times if now - t <= 5]
        if len(recent) >= 7 and cfg["auto_mod_enabled"]:
            try:
                await message.delete()
                await message.channel.send(
                    f"🛡️ {message.author.mention} chậm lại một chút nhé — KaiAI phát hiện spam.",
                    delete_after=5
                )
            except discord.HTTPException:
                pass
            return

        violation = self.moderation_violation(message, cfg)
        if violation and cfg["auto_mod_enabled"]:
            if not message.author.guild_permissions.manage_messages:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                await self.log(
                    message.guild, "🛡️ AutoMod action",
                    f"User: {message.author.mention}\nRule: `{violation}`\nChannel: {message.channel.mention}",
                    color=discord.Color.orange()
                )
                return

        await self.add_xp(message.guild, message.author)

    # --------------------------- COMMAND ANALYTICS ---------------------------

    @commands.Cog.listener()
    async def on_command(self, ctx):
        name = ctx.command.qualified_name if ctx.command else "unknown"
        self.command_counter[name] += 1
        await self.db.execute(
            """INSERT INTO command_stats(guild_id,command_name,uses,last_used)
               VALUES(?,?,1,?)
               ON CONFLICT(guild_id,command_name) DO UPDATE SET
               uses=command_stats.uses+1,last_used=excluded.last_used""",
            (ctx.guild.id if ctx.guild else None, name, now_ts())
        )

    @commands.command(name="kaistats", aliases=["usage"])
    async def kai_stats(self, ctx):
        rows = await self.db.fetchall(
            "SELECT command_name,uses FROM command_stats WHERE guild_id=? ORDER BY uses DESC LIMIT 10",
            (ctx.guild.id if ctx.guild else None,)
        )
        top = "\n".join(f"`{i+1:02}` `{r['command_name']}` — **{r['uses']:,}**" for i,r in enumerate(rows)) or "Chưa có dữ liệu."
        embed = discord.Embed(title="📈 KAI COMMAND ANALYTICS", description=top, color=discord.Color.blurple())
        embed.add_field(name="Runtime", value=f"`{human_seconds(time.time()-self.started_at)}`")
        embed.add_field(name="Memory cache", value=f"`{len(self.message_times)}` users")
        await ctx.reply(embed=embed)

    @commands.command(name="health")
    async def health(self, ctx):
        import psutil
        process = psutil.Process(os.getpid())
        mem = process.memory_info().rss / 1024 / 1024
        embed = discord.Embed(title="💚 KAI AI HEALTH", color=discord.Color.green())
        embed.add_field(name="Discord latency", value=f"`{self.bot.latency*1000:.0f} ms`")
        embed.add_field(name="RAM process", value=f"`{mem:.1f} MB`")
        embed.add_field(name="DB", value="`ONLINE`" if self.db.db else "`OFFLINE`")
        embed.add_field(name="Guilds", value=f"`{len(self.bot.guilds):,}`")
        embed.add_field(name="Tasks", value=f"`{len(asyncio.all_tasks())}`")
        await ctx.reply(embed=embed)

    # --------------------------- HELPERS ---------------------------

    async def log(self, guild: discord.Guild, title: str, description: str, *, color=discord.Color.blurple()):
        cfg = await self.get_config(guild.id)
        channel = guild.get_channel(cfg["log_channel"]) if cfg["log_channel"] else None
        if channel:
            embed = discord.Embed(title=title, description=description[:4000], color=color, timestamp=discord.utils.utcnow())
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(KaiPremium(bot))

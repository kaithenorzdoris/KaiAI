"""
KaiAI Log Center
================

Hệ thống log toàn diện, cài đặt bằng **một lệnh duy nhất** (#8):

    !setup-logs   hoặc   /setup-logs

Lệnh này **idempotent** — chạy lại không tạo trùng channel. Nó:

1. Tạo category ``📁 KAI LOGS`` nếu chưa có.
2. Tạo các channel log cần thiết (message/moderation/command/server/member/economy/ai).
3. Thiết lập permission: ``@everyone`` **deny View**, bot **allow**, staff/admin **allow** (#19).
4. Lưu ID channel vào database.

Vì sao có hàng đợi (queue) (#58)
--------------------------------
Server lớn có thể phát ra hàng trăm event mỗi phút. Gửi từng embed riêng sẽ
chạm rate limit của Discord và làm bot lag. Vì vậy event được **gom vào hàng đợi**
rồi **đẩy theo lô** (mặc định 5 event/lô, giãn 1.5s giữa các lô).

Failsafe (#57)
-------------
Nếu kênh log bị xoá hoặc bot mất quyền, hệ thống **không crash** mà ghi log
nội bộ và gợi ý chạy lại ``/setup-logs``.

Riêng tư (#21)
--------------
Nội dung message **không** lưu vào DB nếu ``LOG_MESSAGE_CONTENT=false`` (mặc
định tắt) — chỉ lưu metadata. Attachment chỉ lưu tên/kích thước/URL, **không
tải về bot** (#59).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import discord
from discord.ext import commands

from kai_core import CONFIG, get_logger
from kai_core.logstore import LogStore

log = get_logger("logs_center")

CATEGORY_NAME = "📁 KAI LOGS"

#: ``key -> (tên kênh, mô tả, có gửi embed sang Discord không)``
LOG_CHANNELS: dict[str, tuple[str, str, bool]] = {
    "message": ("logs-message", "Gửi / sửa / xoá tin nhắn", True),
    "moderation": ("logs-moderation", "Ban, kick, warn, timeout…", True),
    "command": ("logs-command", "Lịch sử dùng lệnh", False),
    "server": ("logs-server", "Thay đổi server: kênh, role, webhooks", True),
    "member": ("logs-member", "Join, leave, role, nickname", True),
    "economy": ("logs-economy", "Giao dịch kinh tế", False),
    "ai": ("logs-ai", "Metadata AI (không lưu nội dung)", False),
}

BATCH_SIZE = 5
BATCH_DELAY = 1.5


class LogCenter(commands.Cog, name="LogCenter"):
    """Cài đặt, ghi và truy vấn hệ thống log."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = log
        self.db: Any = None
        self.store: LogStore | None = None
        self.config: dict[int, dict[str, int]] = {}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._flush_task: asyncio.Task | None = None
        self._warned_missing: set[int] = set()
        self.log_message_content = CONFIG.log_message_content
        self.retention_days = CONFIG.log_retention_days

    # ------------------------------------------------------------------
    # Khởi động
    # ------------------------------------------------------------------
    async def cog_load(self) -> None:
        """Mở DB log và nạp cấu hình channel theo từng server."""
        import aiosqlite

        self.db = await aiosqlite.connect(CONFIG.log_db_path)
        self.bot.log_center = self  # để cog khác (AI/economy) ghi log trực tiếp
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.commit()

        self.store = LogStore(
            self.db,
            retention_days=self.retention_days,
            ai_content_logging=CONFIG.ai_content_logging,
        )
        await self.store.setup()
        await self._load_config()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="kai-log-flush")
        self._retention_task = asyncio.create_task(self._retention_loop(), name="kai-log-retention")
        self.logger.info(
            "Log Center sẵn sàng • retention=%d ngày • message-content=%s",
            self.retention_days,
            "bật" if self.log_message_content else "tắt",
        )

    async def cog_unload(self) -> None:
        """Dừng task nền trước khi bot tắt."""
        for task in (self._flush_task, getattr(self, "_retention_task", None)):
            if task is not None:
                task.cancel()
        if self.db is not None:
            await self.db.close()

    async def _load_config(self) -> None:
        """Nạp bản đồ ``guild_id -> {key: channel_id}`` từ DB."""
        await self.db.execute(
            """CREATE TABLE IF NOT EXISTS log_channels (
                guild_id   INTEGER NOT NULL,
                key        TEXT    NOT NULL,
                category_id INTEGER,
                channel_id INTEGER NOT NULL,
                created_at REAL    NOT NULL,
                PRIMARY KEY (guild_id, key)
            )"""
        )
        await self.db.commit()
        cursor = await self.db.execute("SELECT guild_id, key, channel_id FROM log_channels")
        rows = await cursor.fetchall()
        self.config = {}
        for guild_id, key, channel_id in rows:
            self.config.setdefault(int(guild_id), {})[str(key)] = int(channel_id)
        self.logger.info("Đã nạp cấu hình log cho %d server", len(self.config))

    async def _retention_loop(self) -> None:
        """Dọn dữ liệu log cũ mỗi 6 giờ (yêu cầu #20)."""
        while True:
            await asyncio.sleep(6 * 3600)
            if self.store is None:
                continue
            try:
                removed = await self.store.purge_expired()
                self.logger.info("Retention đã dọn: %s", removed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                self.logger.warning("Retention lỗi: %s", exc)

    # ------------------------------------------------------------------
    # Cài đặt (#8, #19, #56)
    # ------------------------------------------------------------------
    async def _overwrites(self, guild: discord.Guild, staff_role: discord.Role) -> dict:
        """Permission chuẩn: @everyone deny, staff + admin + bot allow."""
        me = guild.me
        allow = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            attach_files=True,
            read_message_history=True,
            manage_channels=True,
        )
        return {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            staff_role: allow,
            guild.premium_role: allow,
            me: allow,
        }

    async def _ensure_channel(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel,
        name: str,
        overwrites: dict,
    ) -> discord.TextChannel | None:
        """Tái sử dụng channel đã có, chỉ tạo mới khi thật sự thiếu (#8.6)."""
        for channel in guild.text_channels:
            if channel.name == name and channel.category_id == category.id:
                try:
                    await channel.edit(overwrites=overwrites, reason="KaiAI Log Center")
                except discord.HTTPException as exc:
                    self.logger.debug("Không cập nhật được quyền cho %s: %s", name, exc)
                return channel
        try:
            return await guild.create_text_channel(name, category=category, overwrites=overwrites)
        except discord.HTTPException as exc:
            self.logger.warning("Không tạo được #%s: %s", name, exc)
            return None

    @commands.command(name="setup-logs", aliases=["setuplogs"])
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def setup_logs(self, ctx: commands.Context) -> None:
        """Tạo/cập nhật toàn bộ hệ thống log của server (chạy lại được nhiều lần)."""
        guild = ctx.guild
        assert guild is not None
        status = ctx.send("⏳ **KaiAI Log Center** — đang kiểm tra và tạo kênh log…")
        message = await status

        staff_role = guild.get_role(CONFIG.log_staff_role_id) or discord.utils.get(
            guild.roles, name=CONFIG.log_staff_role_name
        )
        if staff_role is None:
            staff_role = next(
                (r for r in guild.roles if r.permissions.administrator and not r.managed),
                guild.premium_role or guild.default_role,
            )

        category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        overwrites = await self._overwrites(guild, staff_role)
        created = False
        if category is None:
            try:
                category = await guild.create_category(CATEGORY_NAME, overwrites=overwrites)
                created = True
            except discord.HTTPException as exc:
                await message.edit(content=f"❌ Không tạo được category log: `{exc}`")
                return

        resolved: dict[str, int] = {}
        lines: list[str] = []
        for key, (channel_name, _desc, _to_discord) in LOG_CHANNELS.items():
            channel = await self._ensure_channel(guild, category, channel_name, overwrites)
            if channel is None:
                lines.append(f"❌ `{channel_name}` — không tạo được")
                continue
            resolved[key] = channel.id
            lines.append(f"✅ `{channel_name}` — {('mới' if created else 'đã có')}")

        self.config[guild.id] = resolved
        await self.db.execute("DELETE FROM log_channels WHERE guild_id = ?", (guild.id,))
        for key, channel_id in resolved.items():
            await self.db.execute(
                "INSERT OR REPLACE INTO log_channels"
                " (guild_id, key, category_id, channel_id, created_at) VALUES (?,?,?,?,?)",
                (guild.id, key, category.id, channel_id, time.time()),
            )
        await self.db.commit()

        body = (
            "✅ **LOG SYSTEM ENABLED**\n"
            f"Category: {category.mention}\n"
            f"Quyền: `@everyone` bị chặn • staff/admin/bot được xem\n\n" + "\n".join(lines)
        )
        await message.edit(content=body[:1900])
        self._warned_missing.discard(guild.id)
        self.logger.info("Đã cài log center cho %s (%d kênh)", guild.name, len(resolved))

    # ------------------------------------------------------------------
    # Truy xuất channel + failsafe (#57)
    # ------------------------------------------------------------------
    def channel_for(self, guild: discord.Guild, key: str) -> discord.TextChannel | None:
        """Lấy channel log theo ``key``; ``None`` nếu chưa setup hoặc đã bị xoá."""
        channel_id = self.config.get(guild.id, {}).get(key)
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return None
        me = guild.me
        if me is not None and not channel.permissions_for(me).read_messages:
            return None
        return channel

    def is_enabled(self, guild: discord.Guild, key: str) -> bool:
        return bool(self.config.get(guild.id, {}).get(key))

    def _failsafe(self, guild: discord.Guild, key: str) -> None:
        """Báo **một lần** mỗi server rằng kênh log không dùng được."""
        if guild.id in self._warned_missing:
            return
        self._warned_missing.add(guild.id)
        self.logger.warning(
            "⚠️ Log channel '%s' không khả dụng ở %s — chạy lại /setup-logs", key, guild.name
        )

    # ------------------------------------------------------------------
    # Hàng đợi + đẩy theo lô (#58)
    # ------------------------------------------------------------------
    async def enqueue(self, key: str, guild_id: int, embed: discord.Embed | None = None) -> None:
        """Đưa event vào hàng đợi; hết chỗ thì **bỏ qua** thay vì làm chậm bot."""
        if self.store is None:
            return
        try:
            self.queue.put_nowait((key, guild_id, embed))
        except asyncio.QueueFull:
            self.logger.warning("Hàng đợi log đầy — bỏ qua event '%s'", key)

    async def _flush_loop(self) -> None:
        """Gom event và gửi theo lô để không chạm rate limit của Discord."""
        while True:
            try:
                batch: list[tuple[str, int, discord.Embed | None]] = []
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.25)
                    continue
                while len(batch) < BATCH_SIZE:
                    try:
                        batch.append(self.queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                await self._flush_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - loop không được chết
                self.logger.error("Log flush loop lỗi: %s", exc)
                await asyncio.sleep(2)

    async def _flush_batch(self, batch: list[tuple[str, int, discord.Embed | None]]) -> None:
        """Gửi một lô embed log; lỗi từng kênh được bỏ qua an toàn."""
        for key, guild_id, embed in batch:
            guild = self.bot.get_guild(guild_id)
            if guild is None or embed is None:
                continue
            if not LOG_CHANNELS.get(key, ("", "", False))[2]:
                continue
            channel = self.channel_for(guild, key)
            if channel is None:
                self._failsafe(guild, key)
                continue
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as exc:
                self._failsafe(guild, key)
                self.logger.debug("Không gửi log '%s': %s", key, exc)
        await asyncio.sleep(BATCH_DELAY)

    # ------------------------------------------------------------------
    # Ghi log sự kiện (#10 – #16)
    # ------------------------------------------------------------------
    @staticmethod
    def _clip(text: str, limit: int = 1500) -> str:
        """Rút gọn nội dung cho đúng giới hạn embed của Discord."""
        text = str(text or "")
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _attachment_meta(attachments) -> list[dict]:
        """Chỉ lưu metadata tệp đính kèm — **không** tải về bot (#59)."""
        return [
            {"name": a.filename, "size": a.size, "content_type": a.content_type, "url": a.url}
            for a in (attachments or ())
        ][:10]

    def _content(self, text: str) -> str:
        """Nội dung thật nếu bật ``LOG_MESSAGE_CONTENT``, ngược lại ẩn (#21)."""
        return text if self.log_message_content else "(ẩn — bật LOG_MESSAGE_CONTENT để lưu)"

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or self.store is None:
            return
        guild = message.guild
        if not self.is_enabled(guild, "message"):
            return
        content = self._content(message.content)
        await self.store.log_message(
            guild_id=guild.id,
            channel_id=message.channel.id,
            event="create",
            message_id=message.id,
            author_id=message.author.id,
            author_name=str(message.author),
            content=content,
            attachments=self._attachment_meta(message.attachments),
            jump_url=message.jump_url,
            reply_to=message.reference.message_id if message.reference else None,
        )
        await self.enqueue(
            "message",
            guild.id,
            discord.Embed(
                title="💬 Message",
                description=(
                    f"**{message.author}** gửi tại {message.channel.mention}\n"
                    f"{self._clip(content)}\n[Mở tin nhắn]({message.jump_url})"
                ),
                color=discord.Color.blurple(),
                timestamp=message.created_at,
            ),
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or self.store is None:
            return
        guild = message.guild
        if not self.is_enabled(guild, "message"):
            return
        content = self._content(message.content)
        await self.store.log_message(
            guild_id=guild.id, channel_id=message.channel.id, event="delete",
            message_id=message.id, author_id=message.author.id,
            author_name=str(message.author), content=content,
            attachments=self._attachment_meta(message.attachments),
        )
        await self.enqueue(
            "message",
            guild.id,
            discord.Embed(
                title="🗑️ MESSAGE DELETED",
                description=(
                    f"**User:** {message.author} (`{message.author.id}`)\n"
                    f"**Channel:** {message.channel.mention}\n"
                    f"**Message ID:** `{message.id}`\n"
                    f"**Time:** <t:{int(message.created_at.timestamp()):F}>\n"
                    f"**Content:** {self._clip(content)}\n"
                    f"**Attachments:** {len(message.attachments)}"
                ),
                color=discord.Color.red(),
            ),
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.author.bot or after.guild is None or self.store is None:
            return
        guild = after.guild
        if not self.is_enabled(guild, "message"):
            return
        await self.store.log_message(
            guild_id=guild.id, channel_id=after.channel.id, event="edit",
            message_id=after.id, author_id=after.author.id,
            author_name=str(after.author), content=self._content(after.content),
            jump_url=after.jump_url,
        )
        await self.enqueue(
            "message",
            guild.id,
            discord.Embed(
                title="✏️ MESSAGE EDITED",
                description=(
                    f"**User:** {after.author}\n**Channel:** {after.channel.mention}\n"
                    f"**Before:** {self._clip(self._content(before.content))}\n"
                    f"**After:** {self._clip(self._content(after.content))}"
                ),
                color=discord.Color.orange(),
            ),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self._member_log(member, "👥 THÀNH VIÊN MỚI", discord.Color.green())

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self._member_log(member, "🚪 THÀNH VIÊN RỜI ĐI", discord.Color.red())

    async def _member_log(
        self, member: discord.Member, title: str, color: discord.Color
    ) -> None:
        if self.store is None or member.guild is None:
            return
        guild = member.guild
        await self.store.log_audit(
            guild.id, title.split()[-1].lower(),
            target_id=member.id, target_name=str(member),
        )
        await self.enqueue(
            "member",
            guild.id,
            discord.Embed(
                title=title,
                description=(
                    f"**{member}** (`{member.id}`)\n"
                    f"Account created: <t:{int(member.created_at.timestamp()):F}>\n"
                    f"Member count: {guild.member_count}"
                ),
                color=color,
            ),
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if self.store is None or after.guild is None:
            return
        guild = after.guild
        changes: list[str] = []
        if before.nick != after.nick:
            changes.append(f"Nickname: `{before.nick}` → `{after.nick}`")
        added = [r.name for r in after.roles if r not in before.roles]
        removed = [r.name for r in before.roles if r not in after.roles]
        if added:
            changes.append(f"Role +: {', '.join(added[:10])}")
        if removed:
            changes.append(f"Role −: {', '.join(removed[:10])}")
        if before.timed_out_until != after.timed_out_until:
            state = "bị timeout" if after.timed_out_until else "được gỡ timeout"
            changes.append(f"Timeout: {state}")
        if not changes:
            return
        await self.store.log_audit(
            guild.id, "member_update", target_id=after.id, target_name=str(after),
            reason="; ".join(changes)[:500],
        )
        await self.enqueue(
            "member",
            guild.id,
            discord.Embed(
                title="🛠️ THÀNH VIÊN CẬP NHẬT",
                description=f"**{after}**\n" + "\n".join(f"• {c}" for c in changes[:10]),
                color=discord.Color.blue(),
            ),
        )

    #: Những thay đổi audit log đáng ghi ở cấp server (#14).
    INTERESTING_AUDIT = frozenset(
        {
            "channel_create", "channel_delete", "channel_update",
            "role_create", "role_delete", "role_update",
            "webhook_create", "webhook_delete", "guild_update",
        }
    )

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        if self.store is None or entry.guild is None:
            return
        guild = entry.guild
        action = str(entry.action).split(".")[-1].lower()
        if action not in self.INTERESTING_AUDIT:
            return
        target = str(getattr(entry.target, "name", entry.target) or "?")
        await self.store.log_audit(
            guild.id, action, actor_id=getattr(entry.user, "id", None),
            actor_name=str(entry.user), target_name=target, reason=entry.reason or "",
        )
        await self.enqueue(
            "server",
            guild.id,
            discord.Embed(
                title="🛠️ THAY ĐỔI SERVER",
                description=(
                    f"**Action:** `{action}`\n**Người thực hiện:** {entry.user}\n"
                    f"**Đối tượng:** {target}\n**Lý do:** {entry.reason or '—'}"
                ),
                color=discord.Color.teal(),
            ),
        )

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        """Ghi command log (#16) — tham số luôn được redact."""
        if self.store is None or ctx.guild is None or ctx.author.bot:
            return
        await self.store.log_command(
            user_id=ctx.author.id,
            command=getattr(ctx.command, "qualified_name", "?"),
            surface="prefix",
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            args=" ".join(str(a) for a in getattr(ctx, "args", []) or [])[:400],
            success=not getattr(ctx, "command_failed", False),
        )

    # ------------------------------------------------------------------
    # API cho Cog khác dùng lại (#15)
    # ------------------------------------------------------------------
    async def log_moderation(
        self,
        *,
        guild: discord.Guild,
        action: str,
        moderator: discord.abc.User,
        target: discord.abc.User | None = None,
        reason: str = "",
        duration: str = "",
        channel: discord.abc.GuildChannel | None = None,
    ) -> str:
        """Ghi hành động moderation + trả về Case ID (dùng bởi ``cogs.admin``)."""
        if self.store is None:
            return "CASE-000000"
        case_id = await self.store.log_moderation(
            guild_id=guild.id,
            action=action,
            moderator_id=getattr(moderator, "id", None),
            moderator_name=str(moderator),
            target_id=getattr(target, "id", None),
            target_name=str(target or "—"),
            reason=reason,
            duration=duration,
            channel_id=getattr(channel, "id", None),
        )
        if self.is_enabled(guild, "moderation"):
            await self.enqueue(
                "moderation",
                guild.id,
                discord.Embed(
                    title=f"🛡️ {action.upper()} • {case_id}",
                    description=(
                        f"**Moderator:** {moderator}\n**Target:** {target or '—'}\n"
                        f"**Lý do:** {reason or '—'}\n**Thời hạn:** {duration or '—'}\n"
                        f"**Channel:** {channel or '—'}\n"
                        f"**Timestamp:** <t:{int(time.time())}:F>"
                    ),
                    color=discord.Color.dark_red(),
                ),
            )
        return case_id

    async def log_economy(self, **kwargs) -> None:
        """Tiện ích cho các lệnh kinh tế (ghi vào ``economy_logs``)."""
        if self.store is not None:
            await self.store.log_economy(**kwargs)

    async def log_ai(self, **kwargs) -> None:
        """Tiện ích cho cog AI (ghi metadata vào ``ai_logs``)."""
        if self.store is not None:
            await self.store.log_ai(**kwargs)

    # ------------------------------------------------------------------
    # Lệnh tra cứu
    # ------------------------------------------------------------------
    @commands.command(name="loginfo")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def log_status(self, ctx: commands.Context) -> None:
        """Kiểm tra hệ thống log đang hoạt động thế nào."""
        guild = ctx.guild
        assert guild is not None and self.store is not None
        resolved = self.config.get(guild.id, {})
        lines = []
        for key, (name, desc, _to_discord) in LOG_CHANNELS.items():
            channel = self.channel_for(guild, key)
            if channel is not None:
                lines.append(f"✅ `{name}` — {channel.mention} • {desc}")
            elif key in resolved:
                lines.append(f"⚠️ `{name}` — kênh đã bị xoá/mất quyền, chạy lại `/setup-logs`")
            else:
                lines.append(f"➖ `{name}` — chưa bật")
        stats = await self.store.stats(guild.id)
        await ctx.send(
            content="🛡️ **KaiAI Log Center**\n"
            + "\n".join(lines)
            + f"\n\nRetention: **{self.retention_days} ngày**"
            + f" • Nội dung message: **{'có' if self.log_message_content else 'ẩn'}**"
            + f" • Tổng bản ghi: **{stats.total()}**"
        )

    @commands.command(name="case")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def case_lookup(self, ctx: commands.Context, case_id: str) -> None:
        """Tra cứu hồ sơ moderation theo Case ID (ví dụ ``!case CASE-000001``)."""
        if self.store is None:
            return await ctx.send("❌ Hệ thống log chưa sẵn sàng.")
        normalized = case_id.strip().upper()
        if not normalized.startswith("CASE-"):
            normalized = f"CASE-{normalized.zfill(6)}"
        record = await self.store.get_case(normalized)
        if record is None:
            return await ctx.send(f"❌ Không tìm thấy case `{normalized}`.")
        await ctx.send(embed=discord.Embed(
            title="📁 Hồ sơ moderation", description=record.summary(),
            color=discord.Color.orange(),
        ))

    @commands.command(name="cases")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def case_list(self, ctx: commands.Context) -> None:
        """Danh sách 10 case moderation gần nhất."""
        if self.store is None or ctx.guild is None:
            return await ctx.send("❌ Hệ thống log chưa sẵn sàng.")
        records = await self.store.recent_moderation(ctx.guild.id, 10)
        if not records:
            return await ctx.send("📭 Chưa có case moderation nào.")
        await ctx.send(embed=discord.Embed(
            title="📁 Case moderation gần nhất",
            description="\n".join(
                f"`{r.case_id}` • **{r.action.upper()}** • {r.target_name} "
                f"← {r.moderator_name}"
                for r in records
            ),
            color=discord.Color.orange(),
        ))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LogCenter(bot))

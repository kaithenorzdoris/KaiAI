import datetime
import re

import aiosqlite
import discord
from discord.ext import commands

from kai_core import get_logger
from kai_core.config import CONFIG

DB_PATH = str(CONFIG.data_dir / "aegis_system_v2.db")

class AegisLogs(commands.Cog):
    """Hệ thống Security & Logs Tối Thượng - Soi Mọi Ngóc Ngách"""

    def __init__(self, bot):
        self.bot = bot
        self.db: aiosqlite.Connection = None
        self.logger = get_logger("logs")
        # Regex tìm link discord invite
        self.invite_regex = re.compile(r"(https?://)?(www\.)?(discord\.(gg|io|me|li)|discordapp\.com/invite)/[a-zA-Z0-9]+", re.IGNORECASE)

    async def cog_load(self):
        self.db = await aiosqlite.connect(DB_PATH)
        await self.db.execute("PRAGMA journal_mode=WAL;")
        await self.db.execute("PRAGMA synchronous=NORMAL;")
        await self.setup_db()

    async def cog_unload(self):
        if self.db:
            await self.db.close()

    async def setup_db(self):
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS server_logs_config (
                guild_id INTEGER PRIMARY KEY,
                category_id INTEGER,
                msg_log_id INTEGER,
                member_log_id INTEGER,
                server_log_id INTEGER,
                voice_log_id INTEGER,
                ai_log_id INTEGER,
                spy_log_id INTEGER
            )
        ''')
        await self.db.commit()

    async def get_log_channel(self, guild: discord.Guild, log_type: str) -> discord.TextChannel | None:
        valid_types = {"msg_log_id", "member_log_id", "server_log_id", "voice_log_id", "ai_log_id", "spy_log_id"}
        if log_type not in valid_types:
            return None

        async with self.db.execute(f"SELECT {log_type} FROM server_logs_config WHERE guild_id = ?", (guild.id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return guild.get_channel(row[0])
        return None

    # ------------------------------------------------------------------
    # Chống log trùng khi chạy cả Aegis lẫn Log Center
    # ------------------------------------------------------------------
    #: Ánh xạ loại log của Aegis sang khoá cấu hình của Log Center.
    CENTER_KEYS = {
        "spy_log_id": "message",
        "msg_log_id": "message",
        "member_log_id": "member",
        "server_log_id": "server",
    }

    def _center_handles(self, guild: discord.Guild, aegis_key: str) -> bool:
        """True nếu Log Center đang ghi loại log này cho ``guild``.

        ``cogs/logs.py`` (Aegis) và ``cogs/logs_center.py`` cùng nghe **7** sự
        kiện Discord (message, edit, delete, member join/leave/update, audit).
        Khi cả hai cùng có kênh log trong một server, mỗi sự kiện sẽ bị gửi
        **hai lần** vào hai kênh khác nhau — gây nhiễu và tốn rate limit.

        Ở đây Aegis **nhường quyền**: nếu Log Center đã cấu hình kênh cho loại
        log đó thì Aegis im lặng. Nhường quyền chỉ áp dụng phần *gửi log* —
        phần chốt anti-raid vẫn luôn chạy vì Log Center không làm việc đó, và
        Aegis vẫn hoạt động độc lập nếu Log Center không được cấu hình.
        """
        center = getattr(self.bot, "log_center", None)
        if center is None:
            return False
        key = self.CENTER_KEYS.get(aegis_key)
        if key is None:
            return False
        try:
            return bool(center.is_enabled(guild, key))
        except Exception as exc:  # pragma: no cover - Log Center lỗi thì giữ Aegis chạy
            self.logger.debug("Không hỏi được trạng thái Log Center: %s", exc)
            return False

    # ====================================================
    # LỆNH SETUP - CHỐNG LẠM DỤNG (CHỈ 1 LẦN)
    # ====================================================
    @commands.command(name="setup")
    @commands.has_permissions(administrator=True)
    async def setup_logging_system(self, ctx):
        guild = ctx.guild

        # Kiểm tra xem đã setup chưa và kênh còn tồn tại không
        async with self.db.execute("SELECT category_id FROM server_logs_config WHERE guild_id = ?", (guild.id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                existing_cat = guild.get_channel(row[0])
                if existing_cat:
                    return await ctx.send("❌ **Hệ thống Aegis đã được cài đặt!** Kênh log đang hoạt động, không thể dùng lệnh này để spam. Nếu muốn setup lại, hãy xóa Category `🛡️ AEGIS SECURITY` hiện tại.")

        msg = await ctx.send("⏳ **Khởi Động Hệ Thống Theo Dõi Tối Thượng Aegis Max Pro...**")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, attach_files=True, manage_channels=True),
            ctx.author: discord.PermissionOverwrite(view_channel=True)
        }

        try:
            cat = await guild.create_category("🛡️ AEGIS SECURITY", overwrites=overwrites)
            c_spy = await guild.create_text_channel("👁️-spy-messages", category=cat, topic="Soi mọi tin nhắn, file, ảnh gửi trong server realtime")
            c_msg = await guild.create_text_channel("💬-logs-chinh-sua", category=cat, topic="Log tin nhắn bị xóa, chỉnh sửa")
            c_mem = await guild.create_text_channel("👤-logs-thanh-vien", category=cat, topic="Log join, leave, đổi tên, đổi role")
            c_srv = await guild.create_text_channel("⚙️-logs-server", category=cat, topic="Log tạo/xóa kênh, đổi cài đặt server")
            c_vc  = await guild.create_text_channel("🎙️-logs-voice", category=cat, topic="Log ra vào voice, mic, cam")
            c_ai  = await guild.create_text_channel("🤖-logs-ai-raid", category=cat, topic="Hệ thống Anti-Raid cảnh báo")

            await self.db.execute('''
                INSERT OR REPLACE INTO server_logs_config
                (guild_id, category_id, msg_log_id, member_log_id, server_log_id, voice_log_id, ai_log_id, spy_log_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (guild.id, cat.id, c_msg.id, c_mem.id, c_srv.id, c_vc.id, c_ai.id, c_spy.id))
            await self.db.commit()

            await msg.edit(content="✅ **Aegis System V2 đã kích hoạt toàn diện! Từ giây phút này, không một ai trong server có thể lọt qua tầm mắt của bot.**")
        except Exception as e:
            await msg.edit(content=f"❌ **Lỗi nghiêm trọng khi setup:** `{e}`")

    @commands.command(name="logstatus")
    @commands.has_permissions(manage_guild=True)
    async def log_status(self, ctx):
        """Xem nhanh các kênh log Aegis đang được liên kết."""
        async with self.db.execute(
            "SELECT category_id, msg_log_id, member_log_id, server_log_id, voice_log_id, ai_log_id, spy_log_id "
            "FROM server_logs_config WHERE guild_id = ?",
            (ctx.guild.id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return await ctx.send("⚠️ Hệ thống Aegis chưa được cài đặt. Dùng `!setup` để bật.")

        labels = (
            ("Category", 0),
            ("Tin nhắn", 1),
            ("Thành viên", 2),
            ("Server", 3),
            ("Voice", 4),
            ("Anti-Raid", 5),
            ("Spy", 6),
        )
        embed = discord.Embed(title="🛡️ AEGIS — TRẠNG THÁI KÊNH LOG", color=discord.Color.teal())
        for label, index in labels:
            channel_id = row[index]
            channel = ctx.guild.get_channel(channel_id) if channel_id else None
            embed.add_field(
                name=label,
                value=channel.mention if channel else "❌ Không tìm thấy",
                inline=True,
            )
        embed.set_footer(text=f"Guild ID: {ctx.guild.id}")
        await ctx.send(embed=embed)

    async def _handle_raid(self, message: discord.Message, reason: str) -> None:
        """Xoá tin nhắn vi phạm, timeout (nếu đủ quyền) và ghi cảnh báo."""
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            self.logger.debug("Không xoá được tin nhắn anti-raid: %s", exc)

        try:
            if isinstance(message.author, discord.Member):
                me = message.guild.me
                if me is not None and me.guild_permissions.moderate_members:
                    await message.author.timeout(
                        datetime.timedelta(minutes=5), reason="Aegis: Auto Anti-Raid"
                    )
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.logger.warning("Không timeout được %s: %s", message.author, exc)

        ai_log = await self.get_log_channel(message.guild, "ai_log_id")
        if ai_log is None:
            return
        embed = discord.Embed(title="🚨 HỆ THỐNG ĐÁNH CHẶN KÍCH HOẠT", color=discord.Color.red())
        embed.add_field(name="Kẻ vi phạm", value=message.author.mention, inline=True)
        embed.add_field(name="Lý do", value=reason, inline=True)
        embed.add_field(
            name="Hình phạt",
            value="Khóa mõm (Timeout) 5 phút & Xóa tin nhắn",
            inline=False,
        )
        embed.set_thumbnail(url=message.author.display_avatar.url)
        try:
            await ai_log.send(embed=embed)
        except discord.HTTPException as exc:
            self.logger.debug("Không gửi được log anti-raid: %s", exc)

    # ====================================================
    # 1. SPY SYSTEM: LƯU TRỮ MỌI TIN NHẮN (ALL MESSAGES)
    # ====================================================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        # ANTI RAID SYSTEM (Ưu tiên xử lý trước)
        is_raid, reason = False, ""
        if len(message.mentions) > 10:
            is_raid, reason = True, f"Spam Ping ({len(message.mentions)} người)"
        else:
            permissions = getattr(message.author, "guild_permissions", None)
            can_manage = bool(permissions and permissions.manage_messages)
            if self.invite_regex.search(message.content) and not can_manage:
                is_raid, reason = True, "Gửi Link Invite trái phép"

        if is_raid:
            await self._handle_raid(message, reason)
            return  # Đã xử lý anti-raid → không đẩy tiếp xuống spy log

        # Nhường quyền cho Log Center nếu nó đang gõ log tin nhắn ở server này,
        # tránh mỗi tin nhắn bị gửi vào hai kênh log.
        if self._center_handles(message.guild, "spy_log_id"):
            return

        # SPY LOG: Lưu lại mọi thứ gửi trong server
        spy_chan = await self.get_log_channel(message.guild, "spy_log_id")

        # Bỏ qua không log chính các kênh trong category log để tránh loop vô hạn
        if spy_chan and message.channel.category_id != spy_chan.category_id:
            embed = discord.Embed(description=f"{message.author.mention} gửi tin ở {message.channel.mention}:\n\n{message.content[:4000]}", color=discord.Color.light_grey())
            embed.set_author(name=message.author, icon_url=message.author.display_avatar.url)
            embed.timestamp = message.created_at

            # Xử lý File/Ảnh đính kèm
            if message.attachments:
                att_links = "\n".join([f"[{a.filename}]({a.url})" for a in message.attachments])
                embed.add_field(name="📎 Đính kèm", value=att_links[:1024], inline=False)
                if any(message.attachments[0].filename.lower().endswith(ext) for ext in ['png', 'jpg', 'jpeg', 'gif', 'webp']):
                    embed.set_image(url=message.attachments[0].url)

            # Xử lý Sticker
            if message.stickers:
                embed.add_field(name="✨ Sticker", value=message.stickers[0].name)
                embed.set_image(url=message.stickers[0].url)

            # Link tới tin nhắn gốc (nút bấm phải nằm trong View gửi kèm message).
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Tới tin nhắn", url=message.jump_url))

            await spy_chan.send(embed=embed, view=view)

    # ====================================================
    # 2. THEO DÕI SỬA/XÓA TIN NHẮN
    # ====================================================
    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        if self._center_handles(message.guild, "msg_log_id"):
            return
        log_chan = await self.get_log_channel(message.guild, "msg_log_id")
        if not log_chan:
            return

        embed = discord.Embed(title="🗑️ TIN NHẮN BỊ XÓA", color=discord.Color.red())
        embed.set_author(name=f"{message.author} ({message.author.id})", icon_url=message.author.display_avatar.url)
        embed.add_field(name="Kênh", value=message.channel.mention, inline=True)
        embed.add_field(name="Thời gian gửi", value=f"<t:{int(message.created_at.timestamp())}:R>", inline=True)

        if message.content:
            embed.add_field(name="Nội dung", value=f"```\n{message.content[:1000]}\n```", inline=False)

        if message.attachments:
            att_links = "\n".join([f"[Tải {a.filename}]({a.url})" for a in message.attachments])
            embed.add_field(name="File Đính Kèm", value=att_links[:1024], inline=False)
            if any(message.attachments[0].filename.lower().endswith(ext) for ext in ['png', 'jpg', 'jpeg', 'gif']):
                embed.set_image(url=message.attachments[0].url)

        if message.stickers:
            embed.set_image(url=message.stickers[0].url)

        await log_chan.send(embed=embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot or before.content == after.content:
            return
        if self._center_handles(before.guild, "msg_log_id"):
            return
        log_chan = await self.get_log_channel(before.guild, "msg_log_id")
        if not log_chan:
            return

        embed = discord.Embed(title="✏️ TIN NHẮN ĐÃ SỬA", color=discord.Color.orange())
        embed.set_author(name=f"{before.author} ({before.author.id})", icon_url=before.author.display_avatar.url)
        embed.add_field(name="Kênh", value=before.channel.mention, inline=False)
        embed.add_field(name="Trước khi sửa", value=f"```\n{before.content[:1000] or 'Trống'}\n```", inline=False)
        embed.add_field(name="Sau khi sửa", value=f"```\n{after.content[:1000] or 'Trống'}\n```", inline=False)

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Tới tin nhắn", url=after.jump_url))
        await log_chan.send(embed=embed, view=view)

    # ====================================================
    # 3. AUDIT LOG - BẮT TẬN TAY KẺ PHÁ HOẠI SERVER (Tối Thượng)
    # ====================================================
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        # Không log thao tác của chính bot để tránh spam
        if entry.user.id == self.bot.user.id:
            return

        guild = entry.guild
        # Nhường quyền audit cho Log Center (tránh gửi hai embed cho một thay đổi).
        if self._center_handles(guild, "server_log_id"):
            return
        log_chan = await self.get_log_channel(guild, "server_log_id")
        if not log_chan:
            return

        embed = discord.Embed(timestamp=discord.utils.utcnow())
        embed.set_footer(text=f"ID Người thực hiện: {entry.user.id}")
        embed.set_author(name=str(entry.user), icon_url=entry.user.display_avatar.url)

        # Bắt thao tác Xóa Kênh
        if entry.action == discord.AuditLogAction.channel_delete:
            embed.title, embed.color = "🧨 KÊNH BỊ XÓA", discord.Color.dark_red()
            embed.description = f"**{entry.user.mention}** vừa xóa kênh **#{entry.target.name}**"

        # Bắt thao tác Tạo Kênh
        elif entry.action == discord.AuditLogAction.channel_create:
            embed.title, embed.color = "🆕 KÊNH ĐƯỢC TẠO", discord.Color.teal()
            embed.description = f"**{entry.user.mention}** vừa tạo kênh {entry.target.mention}"

        # Bắt thao tác Kick Người
        elif entry.action == discord.AuditLogAction.kick:
            embed.title, embed.color = "🥾 THÀNH VIÊN BỊ KICK", discord.Color.red()
            embed.description = f"**{entry.user.mention}** đã Kick **{entry.target}**\nLý do: `{entry.reason or 'Không có'}`"

        # Bắt thao tác Ban Người
        elif entry.action == discord.AuditLogAction.ban:
            embed.title, embed.color = "🔨 THÀNH VIÊN BỊ BAN", discord.Color.dark_red()
            embed.description = f"**{entry.user.mention}** đã Ban **{entry.target}**\nLý do: `{entry.reason or 'Không có'}`"

        # Gửi log nếu có sự kiện khớp
        if embed.title:
            await log_chan.send(embed=embed)

    # ====================================================
    # 4. NHẬT KÝ THÀNH VIÊN (JOIN/LEAVE/UPDATE)
    # ====================================================
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if self._center_handles(member.guild, "member_log_id"):
            return
        log_chan = await self.get_log_channel(member.guild, "member_log_id")
        if not log_chan:
            return

        created_time = int(member.created_at.timestamp())
        embed = discord.Embed(title="📥 THÀNH VIÊN MỚI", description=f"{member.mention} đã tham gia server.", color=discord.Color.green())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Tạo tài khoản lúc", value=f"<t:{created_time}:F> (<t:{created_time}:R>)", inline=False)

        # Cảnh báo clone
        if (discord.utils.utcnow() - member.created_at).days < 3:
            embed.description += "\n\n⚠️ **CẢNH BÁO TÀI KHOẢN CLONE: TÀI KHOẢN MỚI TẠO DƯỚI 3 NGÀY**"
            embed.color = discord.Color.yellow()

        await log_chan.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if self._center_handles(member.guild, "member_log_id"):
            return
        log_chan = await self.get_log_channel(member.guild, "member_log_id")
        if not log_chan:
            return

        joined_time = int(member.joined_at.timestamp()) if member.joined_at else 0
        embed = discord.Embed(title="📤 THÀNH VIÊN RỜI ĐI", description=f"**{member}** ({member.id}) đã rời server.", color=discord.Color.dark_red())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Vào server lúc", value=f"<t:{joined_time}:F> (<t:{joined_time}:R>)" if joined_time else "Không xác định", inline=False)
        roles = [r.mention for r in member.roles[1:]]
        embed.add_field(name="Các Role đã sở hữu", value=", ".join(roles) if roles else "Không có", inline=False)

        await log_chan.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if self._center_handles(before.guild, "member_log_id"):
            return
        log_chan = await self.get_log_channel(before.guild, "member_log_id")
        if not log_chan:
            return

        embed = discord.Embed(color=discord.Color.blue(), timestamp=discord.utils.utcnow())
        embed.set_author(name=f"{after} ({after.id})", icon_url=after.display_avatar.url)

        # Check đổi tên (Nickname)
        if before.nick != after.nick:
            embed.title = "📝 THAY ĐỔI BIỆT DANH (NICKNAME)"
            embed.add_field(name="Tên cũ", value=before.nick or before.name, inline=True)
            embed.add_field(name="Tên mới", value=after.nick or after.name, inline=True)
            await log_chan.send(embed=embed)

        # Check Timeout
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                embed.title, embed.color = "🔇 BỊ KHÓA MÕM (TIMEOUT)", discord.Color.red()
                embed.description = f"{after.mention} bị timeout đến <t:{int(after.timed_out_until.timestamp())}:F>"
            else:
                embed.title, embed.color = "🔊 GỠ KHÓA MÕM (UN-TIMEOUT)", discord.Color.green()
                embed.description = f"{after.mention} đã được gỡ timeout."
            await log_chan.send(embed=embed)

    # ====================================================
    # 5. VOICE LOGS CỰC CHI TIẾT
    # ====================================================
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        log_chan = await self.get_log_channel(member.guild, "voice_log_id")
        if not log_chan:
            return

        embed = discord.Embed(timestamp=discord.utils.utcnow())
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)

        if not before.channel and after.channel:
            embed.title, embed.color = "🎙️ THAM GIA VOICE", discord.Color.green()
            embed.description = f"{member.mention} tham gia {after.channel.mention}"
        elif before.channel and not after.channel:
            embed.title, embed.color = "🔇 RỜI KHỎI VOICE", discord.Color.red()
            embed.description = f"{member.mention} rời khỏi {before.channel.mention}"
        elif before.channel and after.channel and before.channel != after.channel:
            embed.title, embed.color = "🔄 CHUYỂN VOICE", discord.Color.blue()
            embed.description = f"{member.mention} chuyển: {before.channel.mention} ➔ {after.channel.mention}"
        elif not before.self_video and after.self_video:
            embed.title, embed.color = "📷 BẬT CAMERA", discord.Color.purple()
            embed.description = f"{member.mention} bật camera ở {after.channel.mention}"
        elif not before.self_stream and after.self_stream:
            embed.title, embed.color = "🖥️ BẬT LIVESTREAM", discord.Color.purple()
            embed.description = f"{member.mention} bắt đầu stream màn hình ở {after.channel.mention}"
        else:
            return

        await log_chan.send(embed=embed)

async def setup(bot):
    await bot.add_cog(AegisLogs(bot))

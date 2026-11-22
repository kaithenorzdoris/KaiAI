import asyncio
import datetime
import os
import time

import aiosqlite
import discord
from discord.ext import commands

from kai_core import get_logger
from kai_core.config import CONFIG

# --- CẤU HÌNH CƠ BẢN ---
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "0"))
# ⚠️ Phải khớp đúng với ``cogs/games.py`` — cùng một file economy.db.
# Nếu hai cog lệch nhau, lệnh admin sẽ đọc nhầm database và "không thấy" tiền.
DB_PATH = str(CONFIG.data_dir / "economy.db")

class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.start_time = time.time()
        self.logger = get_logger("admin")

    async def cog_load(self):
        """Khởi tạo Bảng phân quyền và Cảnh cáo cho hệ thống"""
        async with aiosqlite.connect(DB_PATH) as db:
            # Bảng hệ thống phân quyền mới (1: Mod, 2: Admin)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS role_perms (
                    guild_id INTEGER,
                    role_id INTEGER,
                    level INTEGER,
                    PRIMARY KEY(guild_id, role_id)
                )
            """)
            # Bảng hệ thống Cảnh cáo (Warns)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS warnings (
                    warn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    user_id INTEGER,
                    mod_id INTEGER,
                    reason TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    # ==========================================
    # HÀM KIỂM TRA BẢO MẬT & QUYỀN HẠN
    # ==========================================
    async def get_user_level(self, member: discord.Member):
        """Lấy cấp bậc cao nhất của user trong server"""
        if await self.bot.is_owner(member):
            return 999
        if member.guild_permissions.administrator:
            return 2 # Admin mặc định level 2

        highest_level = 0
        async with aiosqlite.connect(DB_PATH) as db:
            role_ids = [role.id for role in member.roles]
            if role_ids:
                placeholders = ','.join('?' * len(role_ids))
                cursor = await db.execute(
                    f"SELECT MAX(level) FROM role_perms WHERE guild_id = ? AND role_id IN ({placeholders})",
                    (member.guild.id, *role_ids)
                )
                result = await cursor.fetchone()
                if result and result[0]:
                    highest_level = result[0]
        return highest_level

    async def check_perm(self, ctx, required_level: int):
        """Kiểm tra quyền của người dùng"""
        user_level = await self.get_user_level(ctx.author)
        if user_level >= required_level:
            return True
        raise commands.CheckFailure(f"❌ Bạn ở cấp bậc **Level {user_level}**. Lệnh này yêu cầu **Level {required_level}**.")

    async def check_hierarchy(self, ctx, member: discord.Member):
        """Kiểm tra xem Bot và Mod có quyền tác động lên Member không"""
        if await self.bot.is_owner(ctx.author):
            return True
        # Không ai được tự xử lý chính mình.
        if ctx.author.id == member.id:
            raise commands.CheckFailure("❌ Bạn không thể tự tác động lên chính mình.")
        if ctx.author.top_role <= member.top_role:
            raise commands.CheckFailure("❌ Bạn không thể thao tác lên người có Role bằng hoặc cao hơn bạn!")
        if ctx.guild.me.top_role <= member.top_role:
            raise commands.CheckFailure("❌ Bot không có quyền! Role của Bot đang thấp hơn hoặc bằng Role của người này.")
        return True

    async def log_moderation(
        self, ctx, action: str, *, member=None, target_id=None, reason: str = "", duration: str = ""
    ) -> str:
        """Ghi hành động moderation vào Log Center, trả về Case ID (rỗng nếu chưa bật).

        Mọi hành động kiểm duyệt đều có hồ sơ tra cứu được bằng ``!case <id>``.
        Ghi log **không được** làm hỏng lệnh moderation — mọi lỗi đều bị nuốt
        và chỉ ghi vào log server.
        """
        center = getattr(self.bot, "log_center", None)
        store = getattr(center, "store", None) if center is not None else None
        if store is None:
            return ""
        try:
            return await store.log_moderation(
                guild_id=ctx.guild.id,
                action=action,
                moderator_id=ctx.author.id,
                moderator_name=str(ctx.author),
                target_id=member.id if member is not None else target_id,
                target_name=str(member) if member is not None else "",
                reason=str(reason or "")[:300],
                duration=duration,
                channel_id=ctx.channel.id if ctx.channel is not None else None,
            )
        except Exception as exc:  # noqa: BLE001 - log lỗi không được chặn moderation
            self.logger.warning("Không ghi được log moderation '%s': %s", action, exc)
            return ""

    @staticmethod
    def _case_suffix(case_id: str) -> str:
        """Chuỗi Case ID gắn thêm vào phản hồi cho người kiểm duyệt."""
        return f"\n`📁 Hồ sơ: {case_id}`" if case_id else ""

    # ==========================================
    # LỆNH BẢNG ĐIỀU KHIỂN (MENU ADMIN)
    # ==========================================
    @commands.command(name="admin", aliases=["adminhelp", "modhelp"])
    async def admin_panel(self, ctx):
        """[Mod+] Hiển thị toàn bộ lệnh Quản trị xịn xò"""
        await self.check_perm(ctx, 1) # Chỉ Mod trở lên mới xem được

        embed = discord.Embed(
            title="🛡️ BẢNG ĐIỀU KHIỂN QUẢN TRỊ (MOD/ADMIN)",
            description="Dưới đây là danh sách các quyền hạn được phân chia hệ thống.",
            color=0x2b2d31 # Màu đen nhám Discord xịn xò
        )
        embed.set_thumbnail(url=ctx.guild.icon.url if ctx.guild.icon else None)

        embed.add_field(name="👤 Quản lý Thành Viên (Level 1+)", value="""
        `!warn @user [lý do]` - Cảnh cáo thành viên
        `!warnings @user` - Xem lịch sử cảnh cáo
        `!delwarn <warn_id>` - Xóa cảnh cáo
        `!mute @user <phút> [lý do]` - Timeout (Khóa mõm)
        `!unmute @user` - Gỡ khóa mõm
        `!kick @user [lý do]` - Đuổi khỏi server
        `!ban @user [lý do]` - Cấm vĩnh viễn
        `!unban <ID User>` - Gỡ cấm (Ban)
        """, inline=False)

        embed.add_field(name="💬 Quản lý Kênh Chat (Level 1+)", value="""
        `!xoa <số lượng>` - Xóa tin nhắn hàng loạt
        `!lock` - Khóa kênh chat hiện tại
        `!unlock` - Mở khóa kênh chat hiện tại
        `!slowmode <giây>` - Bật chat chậm
        """, inline=False)

        embed.add_field(name="⚙️ Cài đặt & Hệ thống (Level 2+)", value="""
        `!role @user @role` - Cấp/Thu hồi role
        `!nuke` - Tái tạo (xóa trắng) kênh hiện tại
        `!lockdown` - Khóa **toàn bộ** server
        `!unlockdown` - Mở khóa **toàn bộ** server
        `!setmod @role` - Cấp quyền Moderator (Cấp 1) cho role
        `!setadmin @role` - Cấp quyền Admin (Cấp 2) cho role
        `!delperm @role` - Thu hồi toàn bộ quyền của role
        """, inline=False)

        embed.add_field(
            name="📢 Thông báo cập nhật bot",
            value=(
                "`!setupdatechannel [#kênh]` — Server Admin chọn kênh nhận thông báo.\n"
                "`!clearupdatechannel` — Dùng lại System Channel của Discord.\n"
                "`!broadcastupdate <nội dung>` — Bot Owner phát tới mọi server.\n"
                "`!broadcastupdate --ping <nội dung>` — Owner xác nhận rồi ping @everyone "
                "ở server có quyền Mention Everyone."
            ),
            inline=False,
        )

        embed.set_footer(text=f"Yêu cầu bởi {ctx.author.name} • Hệ thống bảo mật cao", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed)

    # ==========================================
    # HỆ THỐNG CẢNH CÁO (WARN SYSTEM) - MỚI 🌟
    # ==========================================
    @commands.command(name="warn")
    async def warn_user(self, ctx, member: discord.Member, *, reason="Vi phạm luật server"):
        """[Mod+] Cảnh cáo thành viên"""
        await self.check_perm(ctx, 1)
        await self.check_hierarchy(ctx, member)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO warnings (guild_id, user_id, mod_id, reason) VALUES (?, ?, ?, ?)",
                (ctx.guild.id, member.id, ctx.author.id, reason)
            )
            await db.commit()

            cursor = await db.execute("SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id))
            warn_count = (await cursor.fetchone())[0]

        case = await self.log_moderation(ctx, "warn", member=member, reason=reason)
        embed = discord.Embed(title="⚠️ CẢNH CÁO", color=discord.Color.orange())
        embed.description = f"**{member.mention}** đã bị cảnh cáo bởi {ctx.author.mention}.\n**Lý do:** `{reason}`\n**Số lần cảnh cáo:** `{warn_count}`"
        if case:
            embed.description += f"\n`📁 Hồ sơ: {case}`"
        await ctx.send(embed=embed)

        # Gửi DM cho nạn nhân (bỏ qua nếu họ chặn tin nhắn riêng)
        try:
            await member.send(f"⚠️ Bạn đã bị cảnh cáo tại server **{ctx.guild.name}**. Lý do: `{reason}`. Hãy cẩn thận!")
        except (discord.Forbidden, discord.HTTPException):
            self.logger.debug("Không gửi được DM cảnh cáo cho %s", member)

    @commands.command(name="warnings", aliases=["warns"])
    async def check_warnings(self, ctx, member: discord.Member):
        """[Mod+] Xem danh sách cảnh cáo"""
        await self.check_perm(ctx, 1)

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT warn_id, mod_id, reason, timestamp FROM warnings WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id))
            warns = await cursor.fetchall()

        if not warns:
            return await ctx.send(f"✅ **{member.display_name}** là một công dân tốt, chưa có cảnh cáo nào.")

        embed = discord.Embed(title=f"📋 Lịch sử cảnh cáo của {member.display_name}", color=discord.Color.yellow())
        for w in warns[-10:]: # Chỉ lấy 10 cảnh cáo gần nhất tránh bị spam màn hình
            embed.add_field(
                name=f"ID: {w[0]} | Ngày: {w[3][:10]}",
                value=f"Bởi: <@{w[1]}>\nLý do: `{w[2]}`",
                inline=False
            )
        await ctx.send(embed=embed)

    @commands.command(name="delwarn")
    async def delete_warning(self, ctx, warn_id: int):
        """[Mod+] Xóa 1 cảnh cáo bằng ID"""
        await self.check_perm(ctx, 1)

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT * FROM warnings WHERE warn_id = ? AND guild_id = ?", (warn_id, ctx.guild.id))
            data = await cursor.fetchone()
            if not data:
                return await ctx.send("❌ Không tìm thấy mã cảnh cáo này.")

            await db.execute("DELETE FROM warnings WHERE warn_id = ?", (warn_id,))
            await db.commit()

        await ctx.send(f"✅ Đã xóa thành công cảnh cáo mang ID **{warn_id}**.")

    # ==========================================
    # CÁC LỆNH KICK/BAN/MUTE (ĐÃ NÂNG CẤP)
    # ==========================================
    @commands.command(name="ban")
    @commands.bot_has_permissions(ban_members=True)
    async def ban_user(self, ctx, member: discord.Member, *, reason="Không có lý do"):
        await self.check_perm(ctx, 1)
        await self.check_hierarchy(ctx, member)

        try:
            await member.send(f"🔨 Bạn đã bị cấm khỏi **{ctx.guild.name}**. Lý do: `{reason}`")
        except (discord.Forbidden, discord.HTTPException):
            self.logger.debug("Không gửi được DM ban cho %s", member)

        await member.ban(reason=f"{ctx.author}: {reason}")
        case = await self.log_moderation(ctx, "ban", member=member, reason=reason)
        await ctx.send(f"🔨 Đã tiễn {member.mention} ra đảo. Lý do: `{reason}`{self._case_suffix(case)}")

    @commands.command(name="unban")
    @commands.bot_has_permissions(ban_members=True)
    async def unban_user(self, ctx, user_id: int):
        """[Mod+] Gỡ ban bằng ID"""
        await self.check_perm(ctx, 1)
        try:
            user = await self.bot.fetch_user(user_id)
            await ctx.guild.unban(user, reason=f"Unban bởi {ctx.author}")
            await ctx.send(f"🕊️ Đã ân xá thành công cho **{user.name}** (`{user_id}`).")
        except discord.NotFound:
            await ctx.send("❌ Không tìm thấy user này, hoặc user không bị ban.")
        except discord.Forbidden:
            await ctx.send("❌ Bot thiếu quyền `Ban Members` để gỡ ban.")
        except discord.HTTPException as exc:
            await ctx.send(f"❌ Discord từ chối yêu cầu: `{exc}`")

    @commands.command(name="kick")
    @commands.bot_has_permissions(kick_members=True)
    async def kick_user(self, ctx, member: discord.Member, *, reason="Không có lý do"):
        await self.check_perm(ctx, 1)
        await self.check_hierarchy(ctx, member)

        await member.kick(reason=f"{ctx.author}: {reason}")
        case = await self.log_moderation(ctx, "kick", member=member, reason=reason)
        await ctx.send(f"👢 Đã sút {member.mention} khỏi server. Lý do: `{reason}`{self._case_suffix(case)}")

    @commands.command(name="mute")
    @commands.bot_has_permissions(moderate_members=True)
    async def mute_user(self, ctx, member: discord.Member, minutes: int, *, reason="Không có lý do"):
        await self.check_perm(ctx, 1)
        await self.check_hierarchy(ctx, member)

        if not 1 <= minutes <= 40320:
            return await ctx.send("❌ Số phút phải nằm trong khoảng **1 – 40320** (tối đa 28 ngày).")

        duration = datetime.timedelta(minutes=minutes)
        await member.timeout(duration, reason=f"{ctx.author}: {reason}")
        case = await self.log_moderation(
            ctx, "timeout", member=member, reason=reason, duration=f"{minutes} phút"
        )
        await ctx.send(
            f"🤫 Đã khóa mõm {member.mention} trong **{minutes} phút**. "
            f"Lý do: `{reason}`{self._case_suffix(case)}"
        )

    @commands.command(name="unmute")
    @commands.bot_has_permissions(moderate_members=True)
    async def unmute_user(self, ctx, member: discord.Member):
        await self.check_perm(ctx, 1)
        await self.check_hierarchy(ctx, member)

        await member.timeout(None, reason=f"Unmute bởi {ctx.author}")
        case = await self.log_moderation(ctx, "untimeout", member=member, reason="Gỡ timeout")
        await ctx.send(f"🔊 Đã mở khóa mõm cho {member.mention}.{self._case_suffix(case)}")

    # ==========================================
    # QUẢN LÝ KÊNH CHAT (CHANNEL MANAGEMENT)
    # ==========================================
    @commands.command(name="xoa", aliases=["purge", "clear"])
    @commands.bot_has_permissions(manage_messages=True)
    async def xoa_tin_nhan(self, ctx, amount: int):
        await self.check_perm(ctx, 1)
        if amount <= 0 or amount > 1000:
            return await ctx.send("❌ Số lượng phải từ 1 đến 1000!")

        await ctx.message.delete()
        deleted = await ctx.channel.purge(limit=amount)
        await ctx.send(f"🧹 Đã quét sạch **{len(deleted)}** tin nhắn bởi {ctx.author.mention}.", delete_after=5)

    @commands.command(name="lock")
    @commands.bot_has_permissions(manage_channels=True)
    async def lock_channel(self, ctx):
        """[Mod+] Khóa kênh hiện tại"""
        await self.check_perm(ctx, 1)
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Bị khóa bởi {ctx.author}")
        await ctx.send("🔒 **Kênh này đã bị khóa!** Chỉ Mod/Admin mới có thể nhắn tin.")

    @commands.command(name="unlock")
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock_channel(self, ctx):
        """[Mod+] Mở khóa kênh hiện tại"""
        await self.check_perm(ctx, 1)
        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Mở khóa bởi {ctx.author}")
        await ctx.send("🔓 **Kênh đã được mở khóa!** Mọi người có thể trò chuyện bình thường.")

    @commands.command(name="slowmode")
    @commands.bot_has_permissions(manage_channels=True)
    async def set_slowmode(self, ctx, seconds: int):
        """[Mod+] Cài đặt chat chậm"""
        await self.check_perm(ctx, 1)
        if seconds < 0 or seconds > 21600:
            return await ctx.send("❌ Thời gian phải từ 0 đến 21600 giây (6 tiếng).")
        await ctx.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await ctx.send("✅ Đã tắt chế độ Slowmode.")
        else:
            await ctx.send(f"🐢 Đã bật chế độ Slowmode: **{seconds} giây** / tin nhắn.")

    @commands.command(name="nuke")
    @commands.bot_has_permissions(manage_channels=True)
    async def nuke_channel(self, ctx):
        """[Admin+] Tái tạo kênh (Xóa sạch sẽ)"""
        await self.check_perm(ctx, 2)

        # Tạo View xác nhận để tránh bấm nhầm
        class ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=15)
                self.value = None

            @discord.ui.button(label="HỦY DIỆT", style=discord.ButtonStyle.danger)
            async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user != ctx.author:
                    return await interaction.response.send_message(
                        "❌ Chỉ người ra lệnh mới xác nhận được.", ephemeral=True
                    )
                self.value = True
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(
                    content="☢️ **Đã xác nhận!** Đang tiến hành Nuke…", view=self
                )
                self.stop()

            @discord.ui.button(label="Hủy thao tác", style=discord.ButtonStyle.secondary)
            async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
                if interaction.user != ctx.author:
                    return await interaction.response.send_message(
                        "❌ Chỉ người ra lệnh mới huỷ được.", ephemeral=True
                    )
                self.value = False
                for child in self.children:
                    child.disabled = True
                await interaction.response.edit_message(
                    content="✅ Đã hủy thao tác Nuke.", view=self
                )
                self.stop()

        view = ConfirmView()
        msg = await ctx.send("⚠️ **CẢNH BÁO:** Bạn sắp Nuke kênh này. Toàn bộ lịch sử chat sẽ bốc hơi vĩnh viễn! Bạn chắc chứ?", view=view)
        await view.wait()

        if view.value is None:
            return await msg.edit(content="⌛ Hết thời gian xác nhận — đã hủy Nuke.", view=None)
        if view.value is False:
            return

        # Tiến hành Nuke
        position = ctx.channel.position
        new_channel = await ctx.channel.clone(reason=f"Nuke kênh bởi {ctx.author}")
        await ctx.channel.delete(reason="Nuke kênh")
        await new_channel.edit(position=position)

        embed = discord.Embed(title="☢️ NUKE THÀNH CÔNG", description=f"Kênh này đã được tái tạo bởi {ctx.author.mention}", color=discord.Color.red())
        embed.set_image(url="https://media.giphy.com/media/HhTXt43pk1I1W/giphy.gif") # Hình nuke cool ngầu
        await new_channel.send(embed=embed)

    @commands.command(name="lockdown")
    @commands.bot_has_permissions(manage_channels=True)
    async def server_lockdown(self, ctx):
        await self.check_perm(ctx, 2)
        msg = await ctx.send("🚨 **Đang tiến hành phong tỏa server...** (Có thể mất chút thời gian)")
        count, failed = 0, 0
        for channel in ctx.guild.text_channels:
            try:
                overwrite = channel.overwrites_for(ctx.guild.default_role)
                if overwrite.send_messages is not False:
                    overwrite.send_messages = False
                    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
                    count += 1
                    await asyncio.sleep(0.5)  # Tránh rate limit của Discord
            except discord.Forbidden:
                failed += 1
                self.logger.warning("Không đủ quyền khoá kênh #%s", channel.name)
            except discord.HTTPException as exc:
                failed += 1
                self.logger.warning("Lỗi HTTP khi khoá #%s: %s", channel.name, exc)
        text = f"🔒 **ĐÃ PHONG TỎA TOÀN BỘ SERVER!** (Khóa {count} kênh)"
        if failed:
            text += f" — ⚠️ {failed} kênh thất bại."
        await msg.edit(content=text)

    @commands.command(name="unlockdown")
    @commands.bot_has_permissions(manage_channels=True)
    async def server_unlockdown(self, ctx):
        await self.check_perm(ctx, 2)
        msg = await ctx.send("🟢 **Đang gỡ phong tỏa server...**")
        count, failed = 0, 0
        for channel in ctx.guild.text_channels:
            try:
                overwrite = channel.overwrites_for(ctx.guild.default_role)
                if overwrite.send_messages is False:
                    overwrite.send_messages = None
                    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
                    count += 1
                    await asyncio.sleep(0.5)
            except discord.Forbidden:
                failed += 1
                self.logger.warning("Không đủ quyền mở khoá kênh #%s", channel.name)
            except discord.HTTPException as exc:
                failed += 1
                self.logger.warning("Lỗi HTTP khi mở khoá #%s: %s", channel.name, exc)
        text = f"🔓 **ĐÃ GỠ PHONG TỎA TOÀN BỘ SERVER!** (Mở {count} kênh)"
        if failed:
            text += f" — ⚠️ {failed} kênh thất bại."
        await msg.edit(content=text)

    # ==========================================
    # CÀI ĐẶT SERVER & QUYỀN HẠN
    # ==========================================
    @commands.command(name="role")
    @commands.bot_has_permissions(manage_roles=True)
    async def add_remove_role(self, ctx, member: discord.Member, role: discord.Role):
        """[Admin+] Cấp hoặc lấy lại role của user"""
        await self.check_perm(ctx, 2)

        if ctx.guild.me.top_role <= role:
            return await ctx.send("❌ Bot không thể cấp Role cao hơn hoặc bằng Role cao nhất của Bot!")
        if ctx.author.top_role <= role and not await self.bot.is_owner(ctx.author):
            return await ctx.send("❌ Bạn không thể thao tác với Role cao hơn hoặc bằng bạn!")

        if role in member.roles:
            await member.remove_roles(role, reason=f"Bởi {ctx.author}")
            await ctx.send(f"➖ Đã **thu hồi** role `{role.name}` khỏi {member.mention}.")
        else:
            await member.add_roles(role, reason=f"Bởi {ctx.author}")
            await ctx.send(f"➕ Đã **cấp** role `{role.name}` cho {member.mention}.")

    @commands.command(name="setmod")
    @commands.has_permissions(administrator=True)
    async def set_mod_role(self, ctx, role: discord.Role):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO role_perms (guild_id, role_id, level) VALUES (?, ?, 1)", (ctx.guild.id, role.id))
            await db.commit()
        await ctx.send(f"🛡️ Đã set role {role.mention} làm **Moderator (Level 1)**.")

    @commands.command(name="setadmin")
    @commands.has_permissions(administrator=True)
    async def set_admin_role(self, ctx, role: discord.Role):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO role_perms (guild_id, role_id, level) VALUES (?, ?, 2)", (ctx.guild.id, role.id))
            await db.commit()
        await ctx.send(f"👑 Đã set role {role.mention} làm **Administrator (Level 2)**.")

    @commands.command(name="delperm")
    @commands.has_permissions(administrator=True)
    async def remove_perm_role(self, ctx, role: discord.Role):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM role_perms WHERE guild_id = ? AND role_id = ?", (ctx.guild.id, role.id))
            await db.commit()
        await ctx.send(f"🗑️ Đã thu hồi quyền quản trị của role {role.mention} trên hệ thống bot.")

    # ==========================================
    # XỬ LÝ LỖI
    # ==========================================
    # Thông báo lỗi được TẬP TRUNG tại ``main.py`` (Bot.on_command_error) nên
    # ``cog_command_error`` cục bộ đã được lược bỏ để tránh gửi 2 tin nhắn/lỗi.
    # Riêng CheckFailure (thiếu cấp bậc) được xử lý ở handler toàn cục.

async def setup(bot):
    await bot.add_cog(Admin(bot))

"""
KaiAI System Core
=================

Cog hạ tầng bổ sung những thứ mà bot còn thiếu sau khi ``help_command=None``:

* ``!help``        — menu lệnh toàn cục, tự động sinh từ mọi Cog (có phân trang).
* ``!help <lệnh>`` — tra cứu chi tiết một lệnh.
* ``!sysinfo``     — chẩn đoán CPU/RAM/latency/uptime (psutil là tuỳ chọn).
* ``!uptime``      — thời gian hoạt động.
* ``/ping``, ``/status`` — bản Slash Command tương ứng.
* ``!reload`` / ``!loadcog`` / ``!unloadcog`` / ``!sync`` / ``!shutdown`` — chỉ owner.
* ``!invite``      — link mời bot kèm quyền hợp lý.

Mọi lệnh owner-only đều kiểm tra qua ``CONFIG.is_owner`` nên không phụ thuộc
vào tính năng ``owner_ids`` của discord.py.
"""

from __future__ import annotations

import platform
import time

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from kai_core import CONFIG, format_duration, get_logger, human_number, utcnow
from kai_core.ui import ConfirmView, Paginator
from keep_alive import INVITE_URL

try:  # psutil là tuỳ chọn — bot vẫn chạy tốt nếu thiếu.
    import psutil
except Exception:  # pragma: no cover - dependency missing
    psutil = None  # type: ignore[assignment]

HELP_FIELDS_PER_PAGE = 18




def owner_only():
    """Decorator restricting a command to configured bot owners."""

    async def predicate(ctx: commands.Context) -> bool:
        if CONFIG.is_owner(ctx.author.id):
            return True
        try:
            if await ctx.bot.is_owner(ctx.author):
                return True
        except Exception:  # pragma: no cover - phòng hờ
            pass
        raise commands.CheckFailure("❌ Lệnh này chỉ dành cho chủ bot (owner).")

    return commands.check(predicate)


class SystemCore(commands.Cog, name="System"):
    """Chẩn đoán hệ thống, help toàn cục và công cụ bảo trì cho owner."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.logger = get_logger("system")
        self.started_at = time.time()
        self.settings_db_path = CONFIG.data_dir / "system_settings.db"

    async def _ensure_settings_table(self) -> None:
        async with aiosqlite.connect(self.settings_db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS update_channels (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    updated_by INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()

    async def _saved_update_channels(self) -> dict[int, int]:
        await self._ensure_settings_table()
        async with aiosqlite.connect(self.settings_db_path) as db:
            cursor = await db.execute("SELECT guild_id, channel_id FROM update_channels")
            rows = await cursor.fetchall()
        return {int(guild_id): int(channel_id) for guild_id, channel_id in rows}

    async def _update_channel_for(
        self, guild: discord.Guild, saved_channels: dict[int, int]
    ) -> discord.abc.Messageable | None:
        channel_id = saved_channels.get(guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if not callable(getattr(channel, "send", None)):
            channel = guild.system_channel
        if not callable(getattr(channel, "send", None)):
            return None
        bot_member = guild.me
        if bot_member is None:
            return None
        permissions = channel.permissions_for(bot_member)
        if not permissions.view_channel or not permissions.send_messages:
            return None
        return channel

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _uptime_seconds(self) -> float:
        started = getattr(self.bot, "started_at", self.started_at)
        return max(0.0, time.time() - started)

    def _iter_commands(self) -> list[commands.Command]:
        seen: set[str] = set()
        result: list[commands.Command] = []
        for cog in self.bot.cogs.values():
            for command in cog.get_commands():
                if command.hidden or command.name in seen:
                    continue
                seen.add(command.name)
                result.append(command)
        for command in self.bot.commands:
            if command.hidden or command.name in seen:
                continue
            seen.add(command.name)
            result.append(command)
        return result

    def _command_line(self, prefix: str, command: commands.Command) -> tuple[str, str]:
        signature = f" {command.signature}" if command.signature else ""
        usage = f"{prefix}{command.name}{signature}".strip()
        description = (command.help or command.brief or "Không có mô tả.").strip().splitlines()[0]
        return f"`{usage}`"[:250], description[:200] or "—"

    def _help_pages(self, prefix: str) -> list[discord.Embed]:
        pages: list[discord.Embed] = []

        overview = discord.Embed(
            title="📖 KAI AI — TRUNG TÂM LỆNH",
            description=(
                f"Prefix: **{prefix}**  •  Dùng `{prefix}help <lệnh>` để xem chi tiết.\n"
                f"Tổng cộng **{len(self._iter_commands())}** lệnh trên **{len(self.bot.cogs)}** module."
            ),
            color=discord.Color.blurple(),
        )
        overview.add_field(
            name="🎯 Nhóm lệnh nhanh",
            value=(
                f"🎮 `{prefix}games` — Kinh tế & trò chơi\n"
                f"🤖 `ai ` / `{prefix}menu` — Trò chuyện với AI\n"
                f"🛡️ `{prefix}admin` — Quản trị server\n"
                f"💖 `{prefix}love` — Đời sống & tình yêu\n"
                f"⚙️ `{prefix}kai` — Bảng điều khiển Premium"
            ),
            inline=False,
        )
        overview.set_footer(text="KaiAI • AI • Games • Community")
        pages.append(overview)

        for cog_name, cog in sorted(self.bot.cogs.items(), key=lambda item: item[0].lower()):
            entries = sorted(
                (self._command_line(prefix, c) for c in cog.get_commands() if not c.hidden),
                key=lambda item: item[0].lower(),
            )
            if not entries:
                continue
            for index in range(0, len(entries), HELP_FIELDS_PER_PAGE):
                chunk = entries[index : index + HELP_FIELDS_PER_PAGE]
                embed = discord.Embed(title=f"🔹 {cog_name}", color=discord.Color.dark_teal())
                for name, value in chunk:
                    embed.add_field(name=name, value=value, inline=False)
                total = len(entries)
                embed.set_footer(
                    text=f"KaiAI • {cog_name} • {total} lệnh"
                    + (
                        f" (phần {index // HELP_FIELDS_PER_PAGE + 1})"
                        if total > HELP_FIELDS_PER_PAGE
                        else ""
                    )
                )
                pages.append(embed)
        return pages


    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------
    @commands.command(name="help", aliases=["h", "commands", "cmds"])
    async def help_command(self, ctx: commands.Context, *, query: str | None = None) -> None:
        """Xem toàn bộ danh sách lệnh, hoặc chi tiết một lệnh cụ thể."""
        prefix = ctx.clean_prefix
        if query:
            raw = query.strip()
            for candidate in (prefix, *CONFIG.prefixes):
                if candidate and raw.startswith(candidate):
                    raw = raw[len(candidate) :]
                    break
            command = self.bot.get_command(raw.strip())
            if command is None or command.hidden:
                return await ctx.send(
                    f"❌ Không tìm thấy lệnh `{query}`. Dùng `{prefix}help` để xem tất cả."
                )
            signature = f" {command.signature}" if command.signature else ""
            embed = discord.Embed(
                title=f"📌 {prefix}{command.name}{signature}",
                description=(command.help or "Không có mô tả.").strip()[:2000],
                color=discord.Color.blurple(),
            )
            if command.aliases:
                embed.add_field(
                    name="🔁 Alias",
                    value=", ".join(f"`{alias}`" for alias in command.aliases)[:1024],
                    inline=False,
                )
            if isinstance(command, commands.Group) and command.commands:
                subs = ", ".join(f"`{sub.name}`" for sub in command.commands)
                embed.add_field(name="🧩 Lệnh con", value=subs[:1024], inline=False)
            embed.set_footer(
                text=f"Module: {command.cog.qualified_name if command.cog else 'System'}"
            )
            return await ctx.send(embed=embed)

        pages = self._help_pages(prefix)
        if len(pages) == 1:
            return await ctx.send(embed=pages[0])
        view = Paginator(pages, author_id=ctx.author.id)
        await ctx.send(embed=pages[0], view=view)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    @commands.command(name="sysinfo", aliases=["status", "sys", "info"])
    async def sysinfo(self, ctx: commands.Context) -> None:
        """Xem trạng thái hệ thống: latency, uptime, tài nguyên, phiên bản."""
        latency = round(self.bot.latency * 1000) if self.bot.latency else 0
        embed = discord.Embed(
            title="🖥️ KAI AI — SYSTEM STATUS",
            color=discord.Color.green(),
            timestamp=utcnow(),
        )
        embed.add_field(name="⚡ Latency", value=f"`{latency} ms`")
        embed.add_field(name="⏱️ Uptime", value=f"`{format_duration(self._uptime_seconds(), long=True)}`")
        embed.add_field(name="🌐 Servers", value=f"`{len(self.bot.guilds):,}`")
        embed.add_field(
            name="👥 Users",
            value=f"`{sum(guild.member_count or 0 for guild in self.bot.guilds):,}`",
        )
        embed.add_field(name="🧩 Cogs", value=f"`{len(self.bot.cogs)}`")
        embed.add_field(name="📜 Commands", value=f"`{len(self.bot.commands):,}`")

        if psutil is not None:
            try:
                memory = psutil.virtual_memory()
                process = psutil.Process()
                embed.add_field(
                    name="🧠 RAM hệ thống",
                    value=f"`{memory.percent:.1f}%` · {human_number(memory.used)}/{human_number(memory.total)}",
                )
                embed.add_field(name="🔥 CPU", value=f"`{psutil.cpu_percent(interval=None):.1f}%`")
                embed.add_field(name="📦 RAM của bot", value=f"`{human_number(process.memory_info().rss)}`")
            except Exception as exc:  # pragma: no cover - psutil hiếm khi lỗi
                self.logger.debug("Không đọc được thông số psutil: %s", exc)

        embed.add_field(name="🐍 Python", value=f"`{platform.python_version()}`")
        embed.add_field(name="🤖 discord.py", value=f"`{discord.__version__}`")
        embed.add_field(name="💻 Hệ điều hành", value=f"`{platform.system()} {platform.release()}`")
        embed.set_footer(text="KaiAI System Core • gõ !help để xem mọi lệnh")
        await ctx.send(embed=embed)

    @commands.command(name="uptime")
    async def uptime(self, ctx: commands.Context) -> None:
        """Xem thời gian bot đã hoạt động liên tục."""
        seconds = self._uptime_seconds()
        await ctx.send(
            f"⏱️ **KaiAI** đã online **{format_duration(seconds, long=True)}** "
            f"(`{format_duration(seconds)}`)."
        )

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------
    @app_commands.command(name="ping", description="Kiểm tra độ trễ của KaiAI")
    async def slash_ping(self, interaction: discord.Interaction) -> None:
        latency = round(self.bot.latency * 1000) if self.bot.latency else 0
        await interaction.response.send_message(
            f"🏓 Pong! WebSocket: **{latency} ms** · Uptime: **{format_duration(self._uptime_seconds())}**"
        )

    @app_commands.command(name="status", description="Trạng thái hệ thống KaiAI")
    async def slash_status(self, interaction: discord.Interaction) -> None:
        latency = round(self.bot.latency * 1000) if self.bot.latency else 0
        lines = [
            f"⚡ Latency: **{latency} ms**",
            f"⏱️ Uptime: **{format_duration(self._uptime_seconds(), long=True)}**",
            f"🌐 Servers: **{len(self.bot.guilds):,}**",
            f"🧩 Cogs: **{len(self.bot.cogs)}** · 📜 Commands: **{len(self.bot.commands):,}**",
        ]
        await interaction.response.send_message("\n".join(lines))


    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    @commands.command(name="invite", aliases=["addbot"])
    async def invite(self, ctx: commands.Context) -> None:
        """Tạo link mời KaiAI với các quyền cơ bản, không gồm Administrator."""
        if self.bot.user is None:
            return await ctx.send("⚠️ Bot chưa sẵn sàng.")
        embed = discord.Embed(
            title="🔗 Mời KaiAI về server của bạn",
            description=(
                f"[**Bấm vào đây để mời bot**]({INVITE_URL})\n"
                "Link chỉ xin quyền xem/gửi tin nhắn, dùng thread, đọc lịch sử, "
                "embed, gửi file và reaction; không xin Administrator. "
                "Lệnh quản trị hoặc voice cần cấp thêm quyền tương ứng trong Discord.\n\n"
                f"Yêu cầu bởi {ctx.author.mention}"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="KaiAI • AI • Games • Community")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Bot-wide update announcements
    # ------------------------------------------------------------------
    @commands.command(name="setupdatechannel", aliases=["setupdate", "kenhupdate"])
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def set_update_channel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel | None = None,
    ) -> None:
        """[Server Admin] Chọn kênh nhận thông báo cập nhật toàn bot."""
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            return await ctx.reply(
                "❌ Hãy chọn một kênh văn bản, ví dụ `!setupdatechannel #thong-bao`.",
                mention_author=False,
            )
        bot_member = ctx.guild.me
        if bot_member is None:
            return await ctx.reply("⚠️ Chưa đọc được quyền của bot trong server.", mention_author=False)
        permissions = target.permissions_for(bot_member)
        if not permissions.view_channel or not permissions.send_messages:
            return await ctx.reply(
                "❌ KaiAI cần quyền **View Channel** và **Send Messages** ở kênh đó.",
                mention_author=False,
            )

        await self._ensure_settings_table()
        async with aiosqlite.connect(self.settings_db_path) as db:
            await db.execute(
                """
                INSERT INTO update_channels (guild_id, channel_id, updated_by)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    updated_by = excluded.updated_by,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (ctx.guild.id, target.id, ctx.author.id),
            )
            await db.commit()
        await ctx.reply(
            f"✅ Kênh nhận thông báo cập nhật của server này là {target.mention}.",
            mention_author=False,
        )

    @commands.command(name="clearupdatechannel", aliases=["resetupdatechannel"])
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def clear_update_channel(self, ctx: commands.Context) -> None:
        """[Server Admin] Xoá kênh cập nhật riêng, dùng lại System Channel của Discord."""
        await self._ensure_settings_table()
        async with aiosqlite.connect(self.settings_db_path) as db:
            await db.execute("DELETE FROM update_channels WHERE guild_id = ?", (ctx.guild.id,))
            await db.commit()
        await ctx.reply(
            "✅ Đã xoá kênh cập nhật riêng. KaiAI sẽ dùng System Channel của Discord "
            "nếu bot có quyền gửi tin.",
            mention_author=False,
        )

    @commands.command(
        name="broadcastupdate",
        aliases=["globalupdate", "thongbaoupdate"],
    )
    @owner_only()
    @commands.cooldown(1, 300, commands.BucketType.user)
    async def broadcast_update(self, ctx: commands.Context, *, raw: str) -> None:
        """[Bot Owner] Phát thông báo tới các server; thêm --ping để ping @everyone."""
        raw = raw.strip()
        parts = raw.split(maxsplit=1)
        ping_everyone = bool(parts and parts[0].casefold() == "--ping")
        body = parts[1].strip() if ping_everyone and len(parts) > 1 else (
            "" if ping_everyone else raw
        )
        if not body or len(body) > 1800:
            return await ctx.reply(
                "❌ Nội dung cần có từ 1 đến 1800 ký tự.\n"
                f"Cú pháp: `{ctx.clean_prefix}broadcastupdate <nội dung>` "
                f"hoặc `{ctx.clean_prefix}broadcastupdate --ping <nội dung>`.",
                mention_author=False,
            )

        saved_channels = await self._saved_update_channels()
        eligible: list[tuple[discord.Guild, discord.abc.Messageable]] = []
        missing_channel = 0
        missing_ping_permission = 0
        for guild in self.bot.guilds:
            channel = await self._update_channel_for(guild, saved_channels)
            if channel is None:
                missing_channel += 1
                continue
            if ping_everyone:
                bot_member = guild.me
                permissions = channel.permissions_for(bot_member)
                if not permissions.mention_everyone and not permissions.administrator:
                    missing_ping_permission += 1
                    continue
            eligible.append((guild, channel))

        if not eligible:
            return await ctx.reply(
                "⚠️ Không có server nào có kênh nhận thông báo và đủ quyền để gửi. "
                "Quản trị viên server có thể đặt kênh bằng `!setupdatechannel #kênh`.",
                mention_author=False,
            )

        preview_text = (
            f"⚠️ Sắp gửi thông báo tới **{len(eligible)}/{len(self.bot.guilds)} server**.\n"
            f"{'🔔 Có ping @everyone tại các server đủ quyền.' if ping_everyone else 'Không ping thành viên.'}\n\n"
            f"> {body[:500]}"
        )
        view = ConfirmView(
            author_id=ctx.author.id,
            timeout=45.0,
            confirm_label="Gửi toàn bộ",
            cancel_label="Huỷ",
        )
        preview = await ctx.send(preview_text, view=view, allowed_mentions=discord.AllowedMentions.none())
        await view.wait()
        if view.value is not True:
            return await preview.edit(
                content="✅ Đã huỷ thông báo toàn server; chưa gửi tin nào.",
                view=None,
            )

        safe_body = body.replace("@everyone", "@\u200beveryone").replace(
            "@here", "@\u200bhere"
        )
        announcement = f"📢 **CẬP NHẬT KAIAI**\n{safe_body}"
        allowed_mentions = discord.AllowedMentions(
            everyone=ping_everyone,
            users=False,
            roles=False,
            replied_user=False,
        )
        if ping_everyone:
            announcement = f"@everyone\n{announcement}"

        sent = 0
        failed = 0
        for guild, channel in eligible:
            try:
                await channel.send(announcement, allowed_mentions=allowed_mentions)
            except discord.HTTPException as exc:
                failed += 1
                self.logger.warning(
                    "Không gửi được thông báo cập nhật tới guild %s (%s): %s",
                    guild.id,
                    type(exc).__name__,
                    exc,
                )
            else:
                sent += 1

        skipped = missing_channel + missing_ping_permission
        result = (
            f"📣 Đã gửi tới **{sent} server**. "
            f"Thất bại khi gửi: **{failed}**; bỏ qua: **{skipped}**."
        )
        if missing_channel:
            result += f" ({missing_channel} server chưa đặt kênh/thiếu quyền gửi)"
        if missing_ping_permission:
            result += f" ({missing_ping_permission} server thiếu quyền Mention Everyone)"
        await preview.edit(content=result, view=None)
        self.logger.info(
            "Owner %s (%s) phát thông báo update: sent=%s failed=%s skipped=%s ping=%s",
            ctx.author,
            ctx.author.id,
            sent,
            failed,
            skipped,
            ping_everyone,
        )

    # ------------------------------------------------------------------
    # Owner-only maintenance
    # ------------------------------------------------------------------
    @commands.command(name="reload")
    @owner_only()
    async def reload_cog(self, ctx: commands.Context, *, extension: str) -> None:
        """Nạp lại một Cog (ví dụ: ``!reload system``)."""
        target = extension.strip()
        if not target.startswith("cogs."):
            target = f"cogs.{target}"
        try:
            await self.bot.reload_extension(target)
        except commands.ExtensionNotLoaded:
            try:
                await self.bot.load_extension(target)
            except Exception as exc:
                return await ctx.send(f"❌ Không nạp được `{target}`: `{exc}`")
            return await ctx.send(f"✅ Đã **nạp mới** `{target}`.")
        except Exception as exc:
            return await ctx.send(f"❌ Không reload được `{target}`: `{exc}`")
        await ctx.send(f"♻️ Đã **nạp lại** `{target}` thành công.")

    @commands.command(name="loadcog")
    @owner_only()
    async def load_cog(self, ctx: commands.Context, *, extension: str) -> None:
        """Nạp thêm một Cog đang tồn tại trong thư mục cogs/."""
        target = extension.strip()
        if not target.startswith("cogs."):
            target = f"cogs.{target}"
        try:
            await self.bot.load_extension(target)
        except commands.ExtensionAlreadyLoaded:
            return await ctx.send(f"ℹ️ `{target}` đã được nạp từ trước.")
        except Exception as exc:
            return await ctx.send(f"❌ Không nạp được `{target}`: `{exc}`")
        await ctx.send(f"✅ Đã nạp `{target}`.")

    @commands.command(name="unloadcog")
    @owner_only()
    async def unload_cog(self, ctx: commands.Context, *, extension: str) -> None:
        """Gỡ một Cog ra khỏi bot (không xoá file)."""
        target = extension.strip()
        if not target.startswith("cogs."):
            target = f"cogs.{target}"
        if target == "cogs.system":
            return await ctx.send("⚠️ Không thể gỡ chính cog System.")
        try:
            await self.bot.unload_extension(target)
        except Exception as exc:
            return await ctx.send(f"❌ Không gỡ được `{target}`: `{exc}`")
        await ctx.send(f"🗑️ Đã gỡ `{target}`.")

    @commands.command(name="sync")
    @owner_only()
    async def sync_tree(self, ctx: commands.Context, guild_id: int | None = None) -> None:
        """Đồng bộ Slash Command (toàn cầu hoặc cho một guild)."""
        sync = getattr(self.bot, "sync_commands", None)
        if sync is None:
            return await ctx.send("⚠️ Bot hiện tại không hỗ trợ sync động.")
        async with ctx.typing():
            try:
                count = await sync(guild_id=guild_id)
            except Exception as exc:
                return await ctx.send(f"❌ Sync thất bại: `{exc}`")
        scope = f"guild `{guild_id}`" if guild_id else "toàn cầu"
        await ctx.send(f"🔄 Đã đồng bộ **{count}** slash command ({scope}).")

    @commands.command(name="shutdown")
    @owner_only()
    async def shutdown(self, ctx: commands.Context) -> None:
        """Tắt bot an toàn (có xác nhận để tránh bấm nhầm)."""
        view = ConfirmView(
            author_id=ctx.author.id,
            timeout=20.0,
            confirm_label="Tắt ngay",
            cancel_label="Huỷ",
        )
        message = await ctx.send("⚠️ Bạn sắp **tắt KaiAI**. Xác nhận?", view=view)
        await view.wait()
        if not view.value:
            return await message.edit(content="✅ Đã huỷ thao tác tắt bot.", view=None)
        await message.edit(content="👋 KaiAI đang tắt… Hẹn gặp lại!", view=None)
        self.logger.warning("Owner %s (%s) yêu cầu tắt bot.", ctx.author, ctx.author.id)
        await self.bot.close()

    async def cog_unload(self) -> None:
        """Log khi cog bị gỡ để dễ theo dõi khi hot-reload."""
        self.logger.info("SystemCore đã được gỡ.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SystemCore(bot))



# __PART2__

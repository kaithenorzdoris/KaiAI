"""
KaiAI Utility Pro
=================
A second, intentionally modular utility layer. It keeps the Premium Core focused
while adding polished admin and power-user commands without touching legacy cogs.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import math
import operator
import random
import re

import discord
from discord import app_commands
from discord.ext import commands


class SafeCalculator(ast.NodeVisitor):
    """Tiny AST calculator: no eval(), no imports, no attributes."""

    BINOPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }
    UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
    FUNCS = {
        "abs": abs,
        "round": round,
        "sqrt": math.sqrt,
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "floor": math.floor,
        "ceil": math.ceil,
    }

    def visit_Constant(self, node):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            if abs(node.value) > 10**100:
                raise ValueError("Số quá lớn.")
            return node.value
        raise ValueError("Chỉ cho phép số.")

    def visit_BinOp(self, node):
        if type(node.op) not in self.BINOPS:
            raise ValueError("Phép toán không được hỗ trợ.")
        left, right = self.visit(node.left), self.visit(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("Số mũ quá lớn.")
        result = self.BINOPS[type(node.op)](left, right)
        if isinstance(result, (int, float)) and abs(result) > 10**100:
            raise ValueError("Kết quả quá lớn.")
        return result

    def visit_UnaryOp(self, node):
        if type(node.op) not in self.UNARY:
            raise ValueError("Phép toán không được hỗ trợ.")
        return self.UNARY[type(node.op)](self.visit(node.operand))

    def visit_Call(self, node):
        if not isinstance(node.func, ast.Name) or node.func.id not in self.FUNCS:
            raise ValueError("Hàm không được phép.")
        if len(node.args) > 3 or node.keywords:
            raise ValueError("Quá nhiều tham số.")
        return self.FUNCS[node.func.id](*(self.visit(x) for x in node.args))

    def generic_visit(self, node):
        raise ValueError("Biểu thức chứa thành phần không an toàn.")


def calculate(expression: str):
    expression = expression.replace("^", "**").strip()
    if len(expression) > 300:
        raise ValueError("Biểu thức quá dài.")
    tree = ast.parse(expression, mode="eval")
    return SafeCalculator().visit(tree.body)


def parse_color(value: str) -> discord.Color:
    value = value.strip().lower().replace("#", "")
    if not re.fullmatch(r"[0-9a-f]{6}", value):
        raise ValueError("Màu phải ở dạng HEX, ví dụ `#5865F2`.")
    return discord.Color(int(value, 16))


class AnnouncementModal(discord.ui.Modal, title="📢 Tạo thông báo KaiAI"):
    title_input = discord.ui.TextInput(label="Tiêu đề", max_length=120, required=True)
    body = discord.ui.TextInput(
        label="Nội dung", style=discord.TextStyle.paragraph, max_length=1800, required=True
    )
    color = discord.ui.TextInput(label="Màu HEX", default="#5865F2", max_length=7, required=False)

    def __init__(self, cog, channel: discord.TextChannel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            color = parse_color(self.color.value or "#5865F2")
        except ValueError as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

        embed = discord.Embed(
            title=self.title_input.value,
            description=self.body.value,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"📢 {self.cog.bot.user.name} • Thông báo chính thức")
        try:
            await self.channel.send(embed=embed)
        except discord.HTTPException as exc:
            return await interaction.response.send_message(f"❌ Không gửi được: `{exc}`", ephemeral=True)
        await interaction.response.send_message(f"✅ Đã gửi thông báo vào {self.channel.mention}.", ephemeral=True)


class EmbedBuilderView(discord.ui.View):
    def __init__(self, author_id: int, channel: discord.TextChannel):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.channel = channel
        self.title_value = "KaiAI Embed"
        self.description_value = "Chỉnh sửa embed bằng các nút bên dưới."
        self.color_value = discord.Color.blurple()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Builder này không phải của bạn.", ephemeral=True)
            return False
        return True

    def preview(self):
        return discord.Embed(
            title=self.title_value,
            description=self.description_value,
            color=self.color_value,
            timestamp=discord.utils.utcnow()
        )

    @discord.ui.button(label="Tiêu đề", emoji="📝", style=discord.ButtonStyle.primary)
    async def title_button(self, interaction, button):
        modal = SimpleInputModal("Đặt tiêu đề", "Tiêu đề", self.title_value, 120)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.value is not None:
            self.title_value = modal.value
            await interaction.edit_original_response(embed=self.preview(), view=self)

    @discord.ui.button(label="Nội dung", emoji="📄", style=discord.ButtonStyle.primary)
    async def body_button(self, interaction, button):
        modal = SimpleInputModal("Đặt nội dung", "Nội dung", self.description_value, 1800, True)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.value is not None:
            self.description_value = modal.value
            await interaction.edit_original_response(embed=self.preview(), view=self)

    @discord.ui.button(label="Màu", emoji="🎨", style=discord.ButtonStyle.secondary)
    async def color_button(self, interaction, button):
        modal = SimpleInputModal("Đặt màu HEX", "HEX", "#5865F2", 7)
        await interaction.response.send_modal(modal)
        await modal.wait()
        if modal.value is None:
            return
        try:
            self.color_value = parse_color(modal.value)
        except ValueError:
            return await interaction.followup.send("❌ HEX không hợp lệ.", ephemeral=True)
        await interaction.edit_original_response(embed=self.preview(), view=self)

    @discord.ui.button(label="Gửi", emoji="🚀", style=discord.ButtonStyle.success)
    async def send_button(self, interaction, button):
        try:
            await self.channel.send(embed=self.preview())
            await interaction.response.edit_message(content="🚀 **Đã gửi embed!**", embed=None, view=None)
            self.stop()
        except discord.HTTPException:
            await interaction.response.send_message("❌ Không gửi được embed.", ephemeral=True)


class SimpleInputModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, default: str, max_length: int, paragraph: bool = False):
        super().__init__(title=title)
        self.value = None
        self.input = discord.ui.TextInput(
            label=label,
            default=default[:max_length],
            max_length=max_length,
            style=discord.TextStyle.paragraph if paragraph else discord.TextStyle.short,
            required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction):
        self.value = self.input.value
        await interaction.response.defer()


class KaiUtilityPro(commands.Cog):
    """Polished utilities, diagnostics, formatting and admin publishing tools."""

    def __init__(self, bot):
        self.bot = bot
        self.start = dt.datetime.now(dt.timezone.utc)

    @commands.command(name="math", aliases=["calc", "calculate"])
    async def math_command(self, ctx, *, expression: str):
        try:
            result = calculate(expression)
        except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
            return await ctx.reply(f"❌ Không tính được: **{exc}**")
        if isinstance(result, float):
            text = f"{result:.12g}"
        else:
            text = str(result)
        await ctx.reply(f"🧮 **KaiAI Calculator**\n`{expression}` = **{text}**")

    @commands.command(name="timestamp", aliases=["timecode"])
    async def timestamp(self, ctx, *, when: str = ""):
        if not when:
            unix = int(dt.datetime.now(dt.timezone.utc).timestamp())
            return await ctx.reply(
                f"🕒 Discord timestamp hiện tại: `{unix}`\n"
                f"<t:{unix}:F> · <t:{unix}:R>"
            )
        try:
            parsed = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            unix = int(parsed.timestamp())
        except ValueError:
            return await ctx.reply("❌ Dùng ISO 8601, ví dụ `2026-08-31T20:30:00+07:00`.")
        await ctx.reply(f"🕒 `{unix}`\n<t:{unix}:F>\n<t:{unix}:R>")

    @commands.command(name="color", aliases=["hex"])
    async def color(self, ctx, value: str):
        try:
            color = parse_color(value)
        except ValueError as exc:
            return await ctx.reply(f"❌ {exc}")
        embed = discord.Embed(
            title="🎨 COLOR LAB",
            description=f"HEX: **#{color.value:06X}**\nDecimal: `{color.value}`",
            color=color
        )
        await ctx.reply(embed=embed)

    @commands.command(name="roleinfo", aliases=["ri"])
    async def role_info(self, ctx, role: discord.Role):
        embed = discord.Embed(title=f"🎭 Role — {role.name}", color=role.color)
        embed.add_field(name="ID", value=f"`{role.id}`", inline=True)
        embed.add_field(name="Position", value=f"`{role.position}`", inline=True)
        embed.add_field(name="Members", value=f"`{len(role.members):,}`", inline=True)
        embed.add_field(name="Mentionable", value="✅" if role.mentionable else "❌", inline=True)
        embed.add_field(name="Hoisted", value="✅" if role.hoist else "❌", inline=True)
        embed.add_field(name="Managed", value="✅" if role.managed else "❌", inline=True)
        await ctx.reply(embed=embed)

    @commands.command(name="channelinfo", aliases=["ci"])
    async def channel_info(self, ctx, channel: discord.TextChannel | None = None):
        channel = channel or ctx.channel
        embed = discord.Embed(title=f"📺 Channel — {channel.name}", color=discord.Color.blurple())
        embed.add_field(name="ID", value=f"`{channel.id}`", inline=True)
        embed.add_field(name="Type", value=f"`{channel.type}`", inline=True)
        embed.add_field(name="Category", value=channel.category.mention if getattr(channel, "category", None) else "None", inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(channel.created_at, "R"), inline=True)
        if isinstance(channel, discord.TextChannel):
            embed.add_field(name="Slowmode", value=f"`{channel.slowmode_delay}s`", inline=True)
            embed.add_field(name="NSFW", value="🔞" if channel.nsfw else "SFW", inline=True)
        await ctx.reply(embed=embed)

    @commands.command(name="perms", aliases=["permissions", "quyen"])
    async def permissions(self, ctx, member: discord.Member | None = None):
        member = member or ctx.author
        if not ctx.guild:
            return await ctx.reply("❌ Server only.")
        perms = member.guild_permissions
        important = {
            "Administrator": perms.administrator,
            "Manage Guild": perms.manage_guild,
            "Manage Channels": perms.manage_channels,
            "Manage Roles": perms.manage_roles,
            "Manage Messages": perms.manage_messages,
            "Kick Members": perms.kick_members,
            "Ban Members": perms.ban_members,
            "Moderate Members": perms.moderate_members,
            "Mention Everyone": perms.mention_everyone,
        }
        lines = [f"{'🟢' if v else '⚪'} **{k}**" for k, v in important.items()]
        await ctx.reply(f"🛡️ **QUYỀN CỦA {member.display_name}**\n" + "\n".join(lines))

    @app_commands.command(name="announce", description="Mở trình tạo thông báo chuyên nghiệp")
    @app_commands.default_permissions(manage_guild=True)
    async def slash_announce(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Kênh không hợp lệ.", ephemeral=True)
        await interaction.response.send_modal(AnnouncementModal(self, channel))

    @commands.command(name="announce", aliases=["thongbao"])
    @commands.has_permissions(manage_guild=True)
    async def announce(self, ctx, channel: discord.TextChannel | None = None):
        """Hướng dẫn tạo thông báo (modal chỉ mở được từ Slash Command)."""
        target = channel or ctx.channel
        await ctx.reply(
            "📢 **KaiAI Announcement Builder**\n"
            f"• Slash: `/announce` (chọn {target.mention}) để mở trình tạo trực quan.\n"
            f"• Prefix: `!announcequick {target.mention} | Tiêu đề | Nội dung` để gửi nhanh.",
            mention_author=False,
        )

    @commands.command(name="announcequick", aliases=["aq"])
    @commands.has_permissions(manage_guild=True)
    async def announce_quick(self, ctx, *, raw: str):
        parts = [p.strip() for p in raw.split("|", 2)]
        if len(parts) != 3:
            return await ctx.reply("❌ Cú pháp: `!announcequick #channel | Tiêu đề | Nội dung`.")
        channel = ctx.message.channel_mentions[0] if ctx.message.channel_mentions else ctx.channel
        title, body = parts[1], parts[2]
        embed = discord.Embed(title=title[:120], description=body[:1800], color=discord.Color.blurple())
        embed.set_footer(text=f"📢 {ctx.guild.name} • KaiAI")
        await channel.send(embed=embed)
        await ctx.reply(f"✅ Đã gửi vào {channel.mention}.", delete_after=5)

    @commands.command(name="serverroles", aliases=["roles"])
    async def server_roles(self, ctx):
        roles = sorted(ctx.guild.roles[1:], key=lambda r: r.position, reverse=True)
        lines = []
        for role in roles[:50]:
            lines.append(f"`{role.position:>3}` {role.mention} · {len(role.members)} members")
        embed = discord.Embed(title="🎭 SERVER ROLES", description="\n".join(lines) or "Không có role.", color=discord.Color.blurple())
        embed.set_footer(text=f"{len(roles)} roles total")
        await ctx.reply(embed=embed)

    @commands.command(name="emojiinfo", aliases=["ei"])
    async def emoji_info(self, ctx, emoji: discord.Emoji):
        embed = discord.Embed(title=f"😀 Emoji — {emoji.name}", color=discord.Color.blurple())
        embed.set_thumbnail(url=emoji.url)
        embed.add_field(name="ID", value=f"`{emoji.id}`", inline=True)
        embed.add_field(name="Animated", value="✅" if emoji.animated else "❌", inline=True)
        embed.add_field(name="Managed", value="✅" if emoji.managed else "❌", inline=True)
        embed.add_field(name="Usage", value=f"`<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>`", inline=False)
        await ctx.reply(embed=embed)

    @commands.command(name="random")
    async def random_choice(self, ctx, start: int = 1, end: int = 100):
        if start > end:
            start, end = end, start
        if end - start > 1_000_000:
            return await ctx.reply("❌ Khoảng quá lớn.")
        await ctx.reply(f"🎲 KaiAI chọn số **{random.randint(start, end)}** trong `{start}..{end}`.")

    @commands.command(name="countdown")
    async def countdown(self, ctx, seconds: int = 5):
        seconds = max(1, min(seconds, 30))
        msg = await ctx.reply(f"⏳ **{seconds}**")
        for left in range(seconds - 1, 0, -1):
            await asyncio.sleep(1)
            await msg.edit(content=f"⏳ **{left}**")
        await asyncio.sleep(1)
        await msg.edit(content="🚀 **GO!**")

    @commands.command(name="id")
    async def id_command(self, ctx, member: discord.Member | None = None):
        member = member or ctx.author
        await ctx.reply(f"🆔 **{member.display_name}** → `{member.id}`")

    @commands.command(name="inviteinfo")
    async def invite_info(self, ctx, *, code: str):
        # Local parser only; no network call.
        match = re.search(r"(?:discord(?:\.gg|\.com/invite)/)?([A-Za-z0-9-]{2,64})", code.strip())
        if not match:
            return await ctx.reply("❌ Invite code không hợp lệ.")
        await ctx.reply(
            f"🔗 Invite code: `{match.group(1)}`\n"
            "ℹ️ KaiAI không tự join/follow invite. Hãy dùng Discord để mở link."
        )

    @commands.command(name="say")
    @commands.has_permissions(manage_messages=True)
    async def say(self, ctx, *, text: str):
        """Gửi lại nội dung dưới danh nghĩa bot (không ping everyone)."""
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="embed")
    @commands.has_permissions(manage_messages=True)
    async def embed_command(self, ctx, *, text: str):
        """Quick embed: !embed Tiêu đề | Nội dung | #HEX"""
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2:
            return await ctx.reply("❌ `!embed Tiêu đề | Nội dung | #5865F2`.")
        color = discord.Color.blurple()
        if len(parts) >= 3:
            try:
                color = parse_color(parts[2])
            except ValueError as exc:
                return await ctx.reply(f"❌ {exc}")
        embed = discord.Embed(title=parts[0][:120], description=parts[1][:1800], color=color)
        await ctx.send(embed=embed)

    @commands.command(name="embedbuilder", aliases=["eb", "builder"])
    @commands.has_permissions(manage_messages=True)
    async def embed_builder(self, ctx, channel: discord.TextChannel | None = None):
        """Mở trình tạo embed trực quan bằng nút bấm + modal."""
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            return await ctx.reply("❌ Chỉ hỗ trợ kênh văn bản.")
        view = EmbedBuilderView(ctx.author.id, target)
        await ctx.send(embed=view.preview(), view=view)

    @commands.command(name="clean")
    @commands.has_permissions(manage_messages=True)
    async def clean(self, ctx, amount: int = 10):
        """Dọn nhanh tin nhắn trong kênh hiện tại (1–100)."""
        amount = max(1, min(amount, 100))
        deleted = await ctx.channel.purge(limit=amount + 1)
        await ctx.send(f"🧹 Đã dọn **{max(0, len(deleted) - 1)}** tin nhắn.", delete_after=5)

    @commands.command(name="support")
    async def support(self, ctx):
        embed = discord.Embed(
            title="🆘 KAI AI SUPPORT",
            description=(
                "Nếu một tính năng lỗi, hãy gửi cho Admin:\n"
                "• Tên lệnh\n• Thời điểm lỗi\n• Nội dung lỗi\n\n"
                "Không gửi token Discord hoặc API key vào chat."
            ),
            color=discord.Color.orange()
        )
        await ctx.reply(embed=embed)


async def setup(bot):
    await bot.add_cog(KaiUtilityPro(bot))

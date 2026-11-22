"""
KaiAI Diagnostics
=================

Công cụ vận hành cho owner/admin, phục vụ trực tiếp cho yêu cầu #3 và #61:

* ``!diagnostics errors``   — các lỗi gần nhất **kèm traceback đầy đủ**.
  Member thường **không bao giờ** thấy traceback (xem ``kai_core.errors``).
* ``!diagnostics commands`` — bảng audit: lệnh prefix nào đã có slash, cái nào
  chưa, vì sao. Dùng để chứng minh "không mất lệnh cũ, không có lệnh chết".
* ``!diagnostics parity``   — báo cáo phát sinh slash mirror lúc khởi động.
* ``!diagnostics health``   — trạng thái nhanh (Discord/latency/uptime/version).

Các lệnh này tự động có bản slash tương ứng nhờ ``kai_core.parity``.
"""

from __future__ import annotations

import time

import discord
from discord.ext import commands

from kai_core import CONFIG, get_logger
from kai_core.errors import ERRORS
from kai_core.pagination import build_embed, build_field_pages, send_paginated
from kai_core.version import APP_NAME, VERSION

log = get_logger("diagnostics")


class Diagnostics(commands.Cog, name="Diagnostics"):
    """Tra cứu lỗi, audit command và trạng thái hệ thống."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _may_run(ctx: commands.Context) -> bool:
        """Chỉ owner hoặc quản trị server được xem thông tin này."""
        if CONFIG.is_owner(ctx.author.id):
            return True
        permissions = getattr(ctx.author, "guild_permissions", None)
        return bool(permissions and permissions.administrator)

    async def cog_check(self, ctx: commands.Context) -> bool:
        if self._may_run(ctx):
            return True
        await ctx.send("⛔ Lệnh này chỉ dành cho quản trị viên server hoặc chủ bot.")
        return False

    def _prefix_inventory(self) -> list[tuple[str, str, str, str]]:
        """Thu thập ``(lệnh, module, trạng thái slash, ghi chú)``."""
        report = getattr(self.bot, "parity_report", None)
        mirrored = set(report.created) if report is not None else set()
        guild_mirrored = set(report.guild_scoped) if report is not None else set()
        skipped = report.skipped if report is not None else {}
        overflow = set(report.overflow) if report is not None else set()

        rows: list[tuple[str, str, str, str]] = []

        def walk(command_list, prefix: str = "") -> None:
            for cmd in sorted(command_list, key=lambda c: c.qualified_name):
                full = f"{prefix}{cmd.name}"
                module = getattr(cmd.cog, "qualified_name", "?")
                if isinstance(cmd, commands.Group) and cmd.commands:
                    walk(list(cmd.commands), f"{full} ")
                if full in mirrored:
                    state = "🏠 guild" if full in guild_mirrored else "🌐 toàn cục"
                elif full in overflow:
                    state = "⚠️ chưa có"
                elif full in skipped:
                    state = "⚠️ bỏ qua"
                else:
                    state = "➕ có sẵn"
                rows.append((f"!{full}", module, state, skipped.get(full, "")))

        walk(list(self.bot.commands))
        return rows

    @staticmethod
    def _safe_tree_count(bot: commands.Bot) -> int:
        """Đếm command slash an toàn khi bot hoặc tree chưa sẵn sàng."""
        tree = getattr(bot, "tree", None)
        if tree is None:
            return 0
        try:
            return len(tree.get_commands())
        except Exception:
            return 0

    # -- commands --------------------------------------------------------
    @commands.group(name="diagnostics", invoke_without_command=True)
    async def diagnostics(self, ctx: commands.Context) -> None:
        """Menu chẩn đoán hệ thống (errors / commands / parity / health)."""
        if ctx.invoked_subcommand is not None:
            return
        embed = build_embed(
            "🩺 KaiAI — Diagnostics",
            "• `!diagnostics errors` — 10 lỗi gần nhất kèm traceback\n"
            "• `!diagnostics types` — thống kê lỗi theo loại\n"
            "• `!diagnostics commands` — audit prefix ↔ slash\n"
            "• `!diagnostics parity` — báo cáo sinh slash mirror\n"
            "• `!diagnostics health` — trạng thái hệ thống",
            color=discord.Color.blurple(),
            footer=f"{APP_NAME} v{VERSION}",
        )
        await ctx.send(embed=embed)

    @diagnostics.command(name="errors")
    async def errors(self, ctx: commands.Context, count: int = 10) -> None:
        """Xem các lỗi gần nhất kèm traceback (chỉ owner/admin)."""
        count = max(1, min(count, 25))
        stats = ERRORS.stats()
        records = ERRORS.recent(count)
        if not records:
            return await ctx.send("✅ Không có lỗi nào được ghi nhận từ lần khởi động này.")

        pages = [
            build_embed(
                f"🩺 Lỗi gần nhất — {index + 1}/{len(records)}",
                record.detail(),
                color=discord.Color.red(),
                footer=f"Tổng {stats['total']} lỗi · {stats['types']} loại",
            )
            for index, record in enumerate(records)
        ]
        await send_paginated(ctx, pages, author_id=ctx.author.id, use_select=True)

    @diagnostics.command(name="types")
    async def error_types(self, ctx: commands.Context) -> None:
        """Thống kê lỗi theo loại exception."""
        top = ERRORS.top_types(10)
        if not top:
            return await ctx.send("✅ Chưa ghi nhận lỗi nào.")
        lines = "\n".join(f"`{name}` × **{count}**" for name, count in top)
        await ctx.send(embed=build_embed("📊 Thống kê lỗi", lines, color=discord.Color.orange()))

    @diagnostics.command(name="commands")
    async def commands(self, ctx: commands.Context, search: str | None = None) -> None:
        """Audit toàn bộ lệnh: prefix ↔ slash, module và trạng thái."""
        rows = self._prefix_inventory()
        if search:
            needle = search.lower()
            rows = [row for row in rows if needle in row[0].lower() or needle in row[1].lower()]
        if not rows:
            return await ctx.send(f"Không tìm thấy lệnh nào khớp `{search}`.")

        pages = build_field_pages(
            "📋 Command Audit — Prefix ↔ Slash",
            [
                (f"`{name}`\n`{module}`", f"{state}{(' — ' + note) if note else ''}")
                for name, module, state, note in rows
            ],
            per_page=6,
            footer=f"Tổng {len(rows)} lệnh prefix",
        )
        await send_paginated(ctx, pages, author_id=ctx.author.id, use_select=True)

    @diagnostics.command(name="parity")
    async def parity(self, ctx: commands.Context) -> None:
        """Báo cáo sinh slash mirror lúc khởi động."""
        report = getattr(self.bot, "parity_report", None)
        if report is None:
            return await ctx.send("⚠️ Báo cáo parity chưa sẵn sàng.")
        await ctx.send(
            embed=build_embed(
                "🪞 Parity Engine",
                f"```\n{report.summary()}\n```\n"
                f"🏠 Gắn theo guild: `{len(report.guild_scoped)}`\n"
                f"⚠️ Vượt hạn mức: `{len(report.overflow)}`",
                color=discord.Color.teal(),
                footer="Discord giới hạn 100 application command cấp cao nhất / phạm vi",
            )
        )

    @diagnostics.command(name="health")
    async def health(self, ctx: commands.Context) -> None:
        """Trạng thái nhanh của bot (không lộ đường dẫn hay secret)."""
        latency = round(self.bot.latency * 1000) if self.bot.latency else 0
        uptime = time.time() - getattr(self.bot, "started_at", time.time())

        report = getattr(self.bot, "parity_report", None)
        parity_summary = report.summary() if report is not None else "Parity chưa sẵn sàng"
        slash_count = self._safe_tree_count(self.bot)

        lines = [
            f"🟢 **Discord**: ONLINE • `{latency} ms`",
            f"⏱️ **Uptime**: {int(uptime // 3600)}h {int((uptime % 3600) // 60)}m",
            f"🌐 **Servers**: {len(self.bot.guilds)}",
            f"🧩 **Cogs**: {len(self.bot.cogs)}",
            f"📜 **Prefix commands**: {len(self.bot.commands)}",
            f"🪞 **Slash commands**: {slash_count}",
            f"📊 **Parity**: {parity_summary}",
            f"🐍 **Version**: {APP_NAME} v{VERSION}",
            f"🔧 **Prefix**: {' '.join(CONFIG.prefixes)}",
            f"🔒 **AI**: {'đã bật' if CONFIG.openai_api_key else 'tắt'}",
            f"📊 **Lỗi đã ghi**: {ERRORS.stats()['total']}",
        ]
        await ctx.send(embed=build_embed("💓 Health", "\n".join(lines), color=discord.Color.green()))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Diagnostics(bot))

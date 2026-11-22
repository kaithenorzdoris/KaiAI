"""
KaiAI — Điểm khởi chạy (entry point)
====================================

Thay thế ``main.py`` cũ bằng một runtime gọn gàng, an toàn, dễ debug:

* Ghi log ra console + file xoay vòng (kai.log) thay vì ``print``.
* Xác thực biến môi trường trước khi kết nối, in báo cáo khởi động rõ ràng.
* Tự động phát hiện Cog có hàm ``setup(bot)`` (preflight bằng ``ast``) và chỉ
  nạp file hợp lệ — không bao giờ nạp ``main.py`` / ``keep_alive.py`` như Cog.
* Đồng bộ Slash Command thông minh (guild sync để cập nhật tức thì).
* Xoay vòng trạng thái (presence) và tắt máy an toàn (graceful shutdown).
* Xử lý lỗi tập trung cho cả lệnh prefix lẫn slash command.

Chạy::

    python main.py
"""

from __future__ import annotations

import ast
import asyncio
import signal
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402
from discord import app_commands  # noqa: E402
from discord.ext import commands, tasks  # noqa: E402

from kai_core import (  # noqa: E402
    CONFIG,
    get_logger,
    install_global_excepthook,
    setup_logging,
)
from kai_core.backup import (
    collect_files as collect_backup_files,
    load_settings as load_backup_settings,
    pull as pull_backup,
    push as push_backup_now,
)
from kai_core.errors import report_app_error, report_command_error, safe_send  # noqa: E402
from kai_core.parity import install_parity  # noqa: E402
from kai_core.version import APP_SUMMARY, VERSION  # noqa: E402

BOT_NAME = "KaiAI"
COG_DIR = ROOT / "cogs"

#: Cog "chính chủ" — luôn được ưu tiên nạp theo đúng thứ tự này.
PREFERRED_COGS: Sequence[str] = (
    "admin",
    "ai_chat",
    "database",
    "games",
    "realworld",
    # ``career`` chỉ tạo bảng ``study`` / ``jobs`` và đọc ``users`` /
    # ``transactions`` do ``games`` tạo — nên nạp SAU ``games`` (xem cogs/career.py).
    "career",
    "kai_premium",
    "logs",
    "logs_center",
    "love",
    "system",
    "diagnostics",
    "utility_pro",
)

#: File hạ tầng tuyệt đối không được coi là Cog.
INFRA_FILES = {"main", "keep_alive", "core", "config", "utils"}

setup_logging(CONFIG.log_level, log_dir=CONFIG.data_dir)
install_global_excepthook()
log = get_logger("main")


def _has_setup_function(path: Path) -> bool:
    """Return True when ``path`` defines a module level ``setup`` callable."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        log.error("Không đọc/parse được %s: %s", path.name, exc)
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "setup"
        for node in tree.body
    )


def discover_cogs(cog_dir: Path = COG_DIR) -> list[str]:
    """Return the ordered list of importable cog module names."""
    if not cog_dir.is_dir():
        log.error("Không tìm thấy thư mục Cog: %s", cog_dir)
        return []

    discovered: list[str] = []
    for module in PREFERRED_COGS:
        path = cog_dir / f"{module}.py"
        if not path.is_file():
            log.warning("Bỏ qua Cog bị thiếu: cogs/%s.py", module)
            continue
        if not _has_setup_function(path):
            log.error("Bỏ qua cogs/%s.py: thiếu hàm setup(bot).", module)
            continue
        discovered.append(module)

    for path in sorted(cog_dir.glob("*.py")):
        stem = path.stem
        if stem.startswith("_"):
            continue
        if stem in INFRA_FILES:
            log.debug("Bỏ qua file hạ tầng trong cogs/: %s", path.name)
            continue
        if stem in PREFERRED_COGS:
            continue  # đã nạp ở vòng lặp trên
        if not _has_setup_function(path):
            log.info("Không nạp file không phải Cog: cogs/%s.py", stem)
            continue
        log.info("Phát hiện Cog mở rộng: cogs/%s.py", stem)
        discovered.append(stem)

    return discovered


async def load_cogs(bot: commands.Bot, modules: Iterable[str]) -> list[str]:
    """Load every cog, isolating failures so one bad cog breaks nothing."""
    loaded: list[str] = []
    for module in modules:
        target = f"cogs.{module}"
        try:
            await bot.load_extension(target)
        except commands.ExtensionAlreadyLoaded:
            log.info("Cog đã được nạp trước đó: %s", target)
        except commands.NoEntryPointError:
            log.error("%s không có setup(bot) hợp lệ — bỏ qua.", target)
        except commands.ExtensionFailed as exc:
            log.error("Cog %s lỗi khi nạp: %s", target, exc.original, exc_info=exc.original)
        except Exception as exc:  # pragma: no cover - phòng hờ
            log.exception("Lỗi không xác định khi nạp %s: %s", target, exc)
        else:
            loaded.append(module)
            log.info("✅ Đã nạp Cog: %s", target)
    return loaded


class KaiAI(commands.Bot):
    """Bot subclass keeping all runtime logic in one predictable place."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True

        super().__init__(
            command_prefix=commands.when_mentioned_or(*CONFIG.prefixes),
            intents=intents,
            case_insensitive=True,
            help_command=None,
            owner_ids=CONFIG.owner_ids or None,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True),
        )
        self.version = VERSION
        self.started_at = time.time()
        self.synced = False
        self.loaded_cogs: list[str] = []
        self.logger = get_logger("bot")

    # -- lifecycle ------------------------------------------------------
    async def setup_hook(self) -> None:
        """Nạp Cog, bật health server và khởi động presence rotation."""
        # PHẢI kéo dữ liệu về TRƯỚC khi nạp cog: các cog mở SQLite ngay
        # trong cog_load, nên nếu khôi phục sau thì chúng đã tạo file rỗng
        # và ghi đè mất bản backup.
        await self.restore_backup()

        self.loaded_cogs = await load_cogs(self, discover_cogs())
        self.logger.info("Tổng số Cog đã nạp: %d", len(self.loaded_cogs))

        # Sinh slash mirror cho mọi lệnh prefix còn thiếu (req #1/#2).
        # Phần dư gắn theo guild qua SYNC_GUILD_ID vì Discord giới hạn 100
        # application command cấp cao nhất cho mỗi phạm vi.
        self.parity_report = install_parity(self, guild_id=CONFIG.sync_guild_id or None)
        self.logger.info(self.parity_report.summary())

        if CONFIG.enable_keep_alive:
            try:
                from keep_alive import set_dashboard_bot, set_ready_probe, start_health_server

                set_dashboard_bot(self, CONFIG.data_dir / "economy.db")
                set_ready_probe(lambda: self.is_ready())
                start_health_server(CONFIG.host, CONFIG.port)
            except Exception as exc:  # pragma: no cover - health server tuỳ chọn
                self.logger.warning("Không khởi động được health server: %s", exc)

        if not self.rotate_presence.is_running():
            self.rotate_presence.start()

    # -- backup: KHÔNG ĐỂ MẤT DỮ LIỆU KHI DEPLOY ------------------------
    async def restore_backup(self) -> None:
        """Tải dữ liệu từ kho lưu trữ ra ngoài về ``KAI_DATA_DIR``.

        Render mỗi lần deploy đều xoá container nên ổ cứng trống; nếu
        ``KAI_BACKUP_URL`` + ``KAI_BACKUP_TOKEN`` được cấu hình thì dữ liệu
        được kéo về tự động. Không có cấu hình -> bỏ qua, bot vẫn chạy bình
        thường (chỉ là không có bản sao).
        """
        settings = load_backup_settings()
        if not settings.enabled:
            self.logger.info(
                "Backup du lieu: TAT (thieu KAI_BACKUP_URL / "
                "KAI_BACKUP_ACCESS_KEY_ID / KAI_BACKUP_SECRET_ACCESS_KEY)."
            )
            return
        # Luon bat vong lap backup — ke ca lan deploy dau tien (chua co ban
        # nao de khoi phuc) — de du lieu duoc day len ngay tu dau.
        self._start_backup_loop(settings)
        try:
            restored = await pull_backup(settings, CONFIG.data_dir)
        except Exception as exc:  # noqa: BLE001 - hong backup khong chan bot
            self.logger.warning("Khoi phuc du lieu loi: %s", exc)
            return
        if restored:
            self.logger.info(
                "♻️ Đã khôi phục %d file từ backup: %s",
                len(restored), ", ".join(restored),
            )
        else:
            self.logger.info("Không có bản backup nào để khôi phục (khởi động lần đầu).")

    def _start_backup_loop(self, settings) -> None:
        """Bật vòng lặp backup theo đúng chu kỳ người dùng cấu hình.

        ``self._backup_body`` đã là một :class:`discord.ext.tasks.Loop`
        (không phải ``asyncio.Task`` nên không có ``.done()``/``.loop()``).
        Đổi chu kỳ bằng ``change_interval`` rồi ``start()``.
        """
        loop = self._backup_body
        try:
            if loop.is_running():
                try:
                    if loop.minutes != float(settings.interval_min):
                        loop.change_interval(minutes=settings.interval_min)
                except Exception:  # noqa: BLE001 - giu vong lap cu chay
                    pass
                return
        except Exception:  # noqa: BLE001 - coi nhu chua chay
            pass
        try:
            loop.change_interval(minutes=settings.interval_min)
        except Exception:  # noqa: BLE001 - giu mac dinh 30 phut
            pass
        try:
            loop.start()
        except RuntimeError:
            # Da chay (dua start 2 lan) — bo qua.
            pass

    @tasks.loop(minutes=30)
    async def _backup_body(self) -> None:
        """Một nhịp backup: đẩy dữ liệu lên kho lưu trữ."""
        await self.push_backup()

    @_backup_body.before_loop
    async def _before_backup(self) -> None:
        await self.wait_until_ready()
        # Chờ ngắn để các cog tạo bảng xong rồi mới chụp ảnh đầu tiên.
        await asyncio.sleep(30)

    async def push_backup(self) -> bool:
        """Đóng gói rồi tải dữ liệu lên kho. ``False`` nếu không bật/hỏng."""
        settings = load_backup_settings()
        if not settings.enabled:
            return False
        try:
            return await push_backup_now(settings, CONFIG.data_dir)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Backup dữ liệu lỗi: %s", exc)
            return False

    async def on_ready(self) -> None:
        self.logger.info(
            "⚡ %s %s (ID: %s) đã online | guilds=%d | users=%d",
            BOT_NAME,
            self.user,
            getattr(self.user, "id", "?"),
            len(self.guilds),
            sum(g.member_count or 0 for g in self.guilds),
        )
        if not self.synced:
            await self.sync_commands()

    async def close(self) -> None:
        """Shut down background tasks cleanly before closing the connection."""
        self.logger.info("Đang tắt bot một cách an toàn…")
        if self.rotate_presence.is_running():
            self.rotate_presence.cancel()
        try:
            if self._backup_body.is_running():
                self._backup_body.cancel()
        except Exception:  # noqa: BLE001 - tat bot khong duoc vuong loop
            pass
        # Render hay deploy (xoá container) hơn là tắt bot: đẩy một bản
        # cuối trước khi thoát để dữ liệu vài giây cuối không mất.
        try:
            await self.push_backup()
        except Exception as exc:  # noqa: BLE001 - không được chặn việc tắt bot
            self.logger.warning("Backup cuối lỗi: %s", exc)
        await super().close()

    # -- presence -------------------------------------------------------
    @tasks.loop(seconds=45)
    async def rotate_presence(self) -> None:
        """Cycle a few informative, non-spammy statuses."""
        statuses = [
            ("playing", "!help • KaiAI Multi-System"),
            ("listening", "thành viên của server"),
            ("watching", f"{len(self.guilds)} server ⚡"),
            ("playing", "AI • Games • Community"),
        ]
        kind, text = statuses[self.rotate_presence.current_loop % len(statuses)]
        activity_type = {
            "playing": discord.ActivityType.playing,
            "listening": discord.ActivityType.listening,
            "watching": discord.ActivityType.watching,
        }[kind]
        try:
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=activity_type, name=text[:128]),
            )
        except discord.HTTPException as exc:  # pragma: no cover - hiếm khi xảy ra
            self.logger.debug("Không đổi được presence: %s", exc)

    @rotate_presence.before_loop
    async def _before_presence(self) -> None:
        await self.wait_until_ready()

    # -- slash command sync --------------------------------------------
    @commands.command(name="backup", aliases=["!backup", "luudata"])
    @commands.is_owner()
    async def backup_now(self, ctx: commands.Context) -> None:
        """Đẩy dữ liệu lên kho lưu trữ ngay lập tức (Render sẽ xoá ổ mỗi lần deploy)."""
        settings = load_backup_settings()
        if not settings.enabled:
            return await ctx.reply(
                "⚠️ **Backup đang TẮT.**\n"
                "Thiếu biến môi trường `KAI_BACKUP_URL`, "
                "`KAI_BACKUP_ACCESS_KEY_ID` hoặc `KAI_BACKUP_SECRET_ACCESS_KEY`.\n"
                "Xem `docs/DEPLOYMENT.md` mục *Backup dữ liệu* để cấu hình."
            )
        async with ctx.typing():
            ok = await self.push_backup()
        if ok:
            files = collect_backup_files(CONFIG.data_dir)
            await ctx.reply(
                f"✅ Đã backup **{len(files)} file** lên kho lưu trữ.\n"
                f"Tự động định kỳ: mỗi **{settings.interval_min} phút**."
            )
        else:
            await ctx.reply("❌ Backup thất bại — xem log để biết lý do.")

    async def sync_commands(self, *, guild_id: int | None = None) -> int:
        """Sync the app command tree; returns the number of synced commands."""
        target = guild_id if guild_id is not None else CONFIG.sync_guild_id
        try:
            if target:
                guild = discord.Object(id=target)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            else:
                synced = await self.tree.sync()
        except discord.HTTPException as exc:
            self.logger.warning("Đồng bộ Slash Commands thất bại: %s", exc)
            return 0
        self.synced = True
        self.logger.info("Đã đồng bộ %d lệnh Slash Commands.", len(synced))
        return len(synced)
    # -- error handling -------------------------------------------------
    @staticmethod
    def unwrap_error(error: BaseException) -> BaseException:
        """Peel CommandInvokeError/HybridCommandError wrappers to the cause."""
        for _ in range(6):
            original = getattr(error, "original", None)
            if original is None:
                break
            error = original
        return error

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        """Centralised, user-friendly error reporting for prefix commands."""
        # Cog đã tự trả lời người dùng rồi — đừng gửi tin nhắn thứ hai.
        if getattr(ctx, "kai_error_handled", False):
            return
        if isinstance(error, commands.CommandNotFound):
            return await safe_send(
                ctx,
                f"❓ Mình chưa nhận ra lệnh này. Dùng `{ctx.clean_prefix}help` để xem danh sách lệnh.",
            )
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Chậm lại chút nhé, thử lại sau **{error.retry_after:.1f}s**.")
            return

        error = self.unwrap_error(error)  # type: ignore[assignment]

        if isinstance(error, commands.MissingRequiredArgument):
            signature = f"{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}"
            return await ctx.send(f"⚠️ Thiếu thông tin! Cú pháp: `{signature.strip()}`")
        if isinstance(error, (commands.BadArgument, commands.MemberNotFound, commands.UserNotFound)):
            return await ctx.send("❌ Không tìm thấy người dùng/đối tượng hợp lệ trong server.")
        if isinstance(error, commands.RoleNotFound):
            return await ctx.send("❌ Không tìm thấy Role này trong server.")
        if isinstance(error, commands.ChannelNotFound):
            return await ctx.send("❌ Không tìm thấy kênh này trong server.")
        if isinstance(error, commands.MissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            return await ctx.send(f"❌ Bạn không đủ quyền: {perms}")
        if isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            return await ctx.send(f"❌ Bot thiếu quyền: cần cấp {perms}")
        if isinstance(error, commands.NotOwner):
            return await ctx.send("❌ Lệnh này chỉ dành cho chủ bot.")
        if isinstance(error, commands.CheckFailure):
            return await ctx.send(f"⛔ {error}")

        self.logger.error(
            "Lỗi tại lệnh '%s' (guild=%s, user=%s): %s",
            ctx.command,
            getattr(ctx.guild, "id", "DM"),
            ctx.author.id,
            error,
            exc_info=error,
        )
        await safe_send(ctx, report_command_error(ctx, error))

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Centralised error reporting for slash commands."""
        if isinstance(error, app_commands.CommandOnCooldown):
            return await self._respond(
                interaction, f"⏳ Chậm lại nhé, thử lại sau **{error.retry_after:.1f}s**."
            )
        error = self.unwrap_error(error)  # type: ignore[assignment]
        if isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            return await self._respond(interaction, f"❌ Bạn không đủ quyền: {perms}")
        if isinstance(error, app_commands.BotMissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            return await self._respond(interaction, f"❌ Bot thiếu quyền: cần {perms}")
        if isinstance(error, app_commands.CheckFailure):
            return await self._respond(interaction, f"⛔ {error}")
        await self._respond(interaction, report_app_error(interaction, error))

    @staticmethod
    async def _respond(interaction: discord.Interaction, message: str, *, ephemeral: bool = True) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(message, ephemeral=ephemeral)
        except discord.HTTPException:  # pragma: no cover - interaction hết hạn
            pass

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        """Log unhandled exceptions raised inside event listeners."""
        self.logger.exception("Lỗi không xử lý trong event listener: %s", event_method)


def _print_startup_report() -> None:
    """Log a compact, human readable startup summary."""
    line = "─" * 64
    log.info(line)
    log.info("  %s v%s  •  %s", BOT_NAME, VERSION, APP_SUMMARY)
    log.info(line)
    log.info("  Prefix      : %s", "  ".join(CONFIG.prefixes))
    log.info("  Data dir    : %s", CONFIG.data_dir.resolve())
    log.info("  Environment : %s (log=%s)", CONFIG.environment, CONFIG.log_level)
    log.info("  Owners      : %s", ", ".join(str(i) for i in sorted(CONFIG.owner_ids)) or "(chưa cấu hình)")
    log.info("  AI model    : %s | concurrency=%d", CONFIG.openai_model, CONFIG.ai_concurrency)
    log.info("  Health check: %s", f"http://{CONFIG.host}:{CONFIG.port}/health" if CONFIG.enable_keep_alive else "tắt")
    log.info(line)

    problems = CONFIG.validate()
    fatal = [p for p in problems if "DISCORD_TOKEN" in p]
    for problem in problems:
        (log.error if problem in fatal else log.warning)("⚠ %s", problem)


def _is_cloudflare_rate_limit(error: discord.HTTPException) -> bool:
    """Identify Cloudflare's 1015 access-denied page returned as HTTP 429."""
    response_text = error.text.lower()
    return error.status == 429 and "cloudflare" in response_text and "1015" in response_text


async def run_bot() -> int:
    """Create the bot, install signal handlers and run until closed."""
    _print_startup_report()
    if not CONFIG.is_configured:
        log.critical("Thiếu DISCORD_TOKEN — không thể khởi động. Hãy cấu hình rồi chạy lại.")
        return 1

    bot = KaiAI()

    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(bot.close()))
        except (NotImplementedError, RuntimeError, ValueError, AttributeError):
            # Windows không hỗ trợ add_signal_handler → dùng KeyboardInterrupt.
            pass

    try:
        await bot.start(CONFIG.token)  # type: ignore[arg-type]
    except discord.LoginFailure:
        log.critical("DISCORD_TOKEN không hợp lệ. Kiểm tra lại token trong Secrets.")
        return 2
    except discord.PrivilegedIntentsRequired:
        log.critical(
            "Bot cần bật 'Message Content Intent' và 'Server Members Intent' "
            "trong Discord Developer Portal → Bot → Privileged Gateway Intents."
        )
        return 3
    except discord.HTTPException as exc:
        if _is_cloudflare_rate_limit(exc):
            log.critical(
                "Discord/Cloudflare đang giới hạn IP outbound của dịch vụ (Error 1015). "
                "Đây không phải lỗi token. Tránh restart liên tục; chờ lệnh giới hạn hết hạn "
                "rồi thử lại. Nếu vẫn lặp lại, liên hệ nhà cung cấp hosting hoặc dùng môi "
                "trường có IP outbound được Discord chấp nhận."
            )
        else:
            log.critical(
                "Discord API từ chối yêu cầu đăng nhập (HTTP %s, mã lỗi %s).",
                exc.status,
                exc.code,
            )
        return 4
    except asyncio.CancelledError:
        log.info("Bot đã dừng theo yêu cầu.")
    finally:
        if not bot.is_closed():
            await bot.close()
    return 0


def main() -> int:
    """Synchronous entry point used by ``python main.py``."""
    try:
        return asyncio.run(run_bot())
    except KeyboardInterrupt:
        log.info("Đã nhận Ctrl+C — KaiAI tạm biệt!")
        return 0
    except Exception:  # pragma: no cover - lỗi khởi động ngoài dự kiến
        log.critical("Không thể khởi động KaiAI", exc_info=True)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

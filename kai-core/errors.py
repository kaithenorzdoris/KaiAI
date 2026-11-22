"""Hệ thống lỗi tập trung — không bao giờ lộ traceback cho người dùng (yêu cầu #3).

Nguyên tắc
----------
1. Member **luôn** chỉ thấy thông báo thân thiện, không bao giờ thấy traceback
   Python, stack của discord.py hay thông báo lỗi SQLite.
2. Chi tiết kỹ thuật đầy đủ (exception, traceback, lệnh, user, guild) được ghi
   vào log nội bộ **và** vào bộ đệm vòng trong bộ nhớ.
3. Admin tra cứa bằng ``/diagnostics errors`` (hoặc ``!diagnostics errors``).

Bộ đệm vòng giới hạn số phần tử nên không thể làm phình RAM khi bot chạy 24/7.
"""

from __future__ import annotations

import re
import time
import traceback
from collections import deque
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

from .logging_setup import get_logger

log = get_logger("errors")

#: Số lỗi gần nhất được giữ lại để admin tra cứu.
ERROR_BUFFER_SIZE = 200

#: Thông báo chung — tuyệt đối không chứa chi tiết kỹ thuật.
GENERIC_MESSAGE = (
    "❌ Đã xảy ra lỗi khi thực hiện lệnh. "
    "Hệ thống đã ghi nhận lỗi, quản trị viên có thể tra cứu bằng `/diagnostics errors`."
)

#: Các mẫu thông báo thân thiện theo loại lỗi người dùng hay gặp.
FRIENDLY: tuple[tuple[type, str], ...] = (
    (commands.MissingRequiredArgument, "⚠️ Thiếu thông tin. Kiểm tra lại cú pháp của lệnh."),
    (commands.MissingPermissions, "⛔ Bạn không có đủ quyền để dùng lệnh này."),
    (commands.BotMissingPermissions, "⛔ Bot thiếu quyền, không thể thực hiện lệnh."),
    (commands.NotOwner, "⛔ Lệnh này chỉ dành cho chủ bot."),
    (commands.MaxConcurrencyReached, "⏳ Lệnh đang bị giới hạn tốc độ, vui lòng thử lại sau."),
    (commands.BadArgument, "❌ Giá trị bạn nhập không hợp lệ."),
    (commands.MemberNotFound, "❌ Không tìm thấy thành viên này trong server."),
    (commands.UserNotFound, "❌ Không tìm thấy người dùng này."),
    (commands.RoleNotFound, "❌ Không tìm thấy role này trong server."),
    (commands.ChannelNotFound, "❌ Không tìm thấy kênh này trong server."),
    (commands.CheckFailure, "⛔ Bạn không đủ điều kiện để dùng lệnh này."),
)

#: Mẫu bí mật cần che trước khi ghi log (yêu cầu #16/#33).

def _mask_named_secret(match: re.Match[str]) -> str:
    """Ghi đè chuỗi có tên như ``token=...`` hoặc ``Authorization: Bearer ...``."""
    key = match.group(1)
    return f"{key}=***"


_SECRET_PATTERNS: tuple[tuple[str, str | object], ...] = (
    (r"(?is)(token|api[_-]?key|secret|password|authorization)\s*[:=]\s*(?:bearer\s+)?[A-Za-z0-9_\-./+=]{6,}", _mask_named_secret),
    (r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}\b", "***JWT***"),
    (r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***"),
    (r"\bMTA[A-Za-z0-9_-]{20,}\b", "***TOKEN***"),
)


def redact(text: str) -> str:
    """Che các mẫu bí mật để không rò rỉ vào log/chat (yêu cầu #16/#33)."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text


@dataclass(slots=True)
class ErrorRecord:
    """Một lỗi đã được ghi nhận (dùng cho ``/diagnostics errors``)."""

    timestamp: float
    source: str
    command: str
    user_id: int
    guild_id: int
    exception_type: str
    message: str
    traceback_text: str

    @property
    def short(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        return f"`{stamp}` [{self.source}] {self.command or '?'}: {self.exception_type}: {self.message}"

    def detail(self, width: int = 900) -> str:
        head = (
            f"**{self.exception_type}** — {self.command or '(event)'}\n"
            f"Source: `{self.source}` • User: `{self.user_id}` • Guild: `{self.guild_id}`\n"
            f"Time: <t:{int(self.timestamp)}:F>\n"
            f"Message: `{self.message[:300]}`\n"
        )
        if self.traceback_text:
            head += f"```py\n{self.traceback_text[-width:]}\n```"
        return head
@dataclass
class ErrorRecorder:
    """Bộ đệm vòng lỗi dùng chung cho toàn bot."""

    records: deque[ErrorRecord] = field(default_factory=lambda: deque(maxlen=ERROR_BUFFER_SIZE))
    counters: dict[str, int] = field(default_factory=dict)

    def record(
        self,
        exc: BaseException,
        *,
        source: str,
        command: str = "",
        user_id: int = 0,
        guild_id: int = 0,
    ) -> ErrorRecord:
        """Ghi nhận lỗi (không bao giờ ném ra ngoài)."""
        record = ErrorRecord(
            timestamp=time.time(),
            source=source,
            command=command,
            user_id=user_id,
            guild_id=guild_id,
            exception_type=type(exc).__name__,
            message=redact(str(exc))[:500],
            traceback_text=redact(
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            ),
        )
        self.records.append(record)
        self.counters[record.exception_type] = self.counters.get(record.exception_type, 0) + 1
        log.error("Lỗi [%s] %s: %s", source, command or "-", record.message)
        return record

    def recent(self, limit: int = 10) -> list[ErrorRecord]:
        """Trả về ``limit`` lỗi gần nhất, mới nhất trước."""
        items = list(self.records)
        return items[-limit:][::-1]

    def top_types(self, limit: int = 5) -> list[tuple[str, int]]:
        return sorted(self.counters.items(), key=lambda item: item[1], reverse=True)[:limit]

    def stats(self) -> dict[str, int]:
        return {"total": len(self.records), "types": len(self.counters)}


#: Bộ đệm lỗi toàn cục (tồn tại suốt vòng đời tiến trình).
ERRORS = ErrorRecorder()


def _unwrap(exc: BaseException) -> BaseException:
    current = exc
    for _ in range(6):
        original = getattr(current, "original", None)
        if original is None:
            break
        current = original
    return current


def friendly_message(exc: BaseException) -> str:
    """Trả về thông báo an toàn cho người dùng theo loại lỗi."""
    current = _unwrap(exc)

    if isinstance(current, commands.CommandOnCooldown):
        return f"⏳ Lệnh đang hồi chiêu, thử lại sau **{current.retry_after:.1f}s**."
    if isinstance(current, app_commands.CommandOnCooldown):
        # Slash command dùng app_commands.Cooldown — phải xử lý riêng.
        return f"⏳ Lệnh đang hồi chiêu, thử lại sau **{current.retry_after:.1f}s**."
    if isinstance(current, commands.CommandInvokeError) and current.original is not None:
        current = _unwrap(current.original)

    for error_type, message in FRIENDLY:
        if isinstance(current, error_type):
            return message
    if isinstance(current, commands.UserInputError):
        return "❌ Dữ liệu bạn nhập không hợp lệ. Hãy kiểm tra lại cú pháp."
    if isinstance(current, commands.CommandNotFound):
        return "❌ Không tìm thấy lệnh này."
    if isinstance(current, discord.HTTPException):
        return "⚠️ Discord đang tạm thời không phản hồi. Vui lòng thử lại sau ít phút."
    if isinstance(current, (TimeoutError, OSError)):
        return "⚠️ Hệ thống đang bận, vui lòng thử lại sau."
    return GENERIC_MESSAGE


def report_command_error(
    ctx: commands.Context,
    exc: Exception,
    *,
    recorder: ErrorRecorder = ERRORS,
) -> str:
    """Ghi nhận lỗi của lệnh prefix và trả về thông báo an toàn để gửi user."""
    recorder.record(
        exc,
        source="prefix",
        command=getattr(ctx.command, "qualified_name", "") if ctx.command else "",
        user_id=getattr(ctx.author, "id", 0),
        guild_id=getattr(getattr(ctx, "guild", None), "id", 0),
    )
    return friendly_message(exc)


def report_app_error(
    interaction: discord.Interaction,
    exc: Exception,
    *,
    recorder: ErrorRecorder = ERRORS,
) -> str:
    """Ghi nhận lỗi của slash command và trả về thông báo an toàn."""
    recorder.record(
        exc,
        source="slash",
        command=getattr(interaction.command, "qualified_name", "") if interaction.command else "",
        user_id=getattr(interaction.user, "id", 0),
        guild_id=getattr(getattr(interaction, "guild", None), "id", 0),
    )
    return friendly_message(exc)


async def safe_send(target, message: str, **kwargs) -> object | None:
    """Gửi tin nhắn, nuốt lỗi HTTP để không gây chuỗi lỗi dây chuyền."""
    try:
        return await target.send(message, **kwargs)
    except discord.HTTPException as exc:
        log.debug("Không gửi được tin nhắn: %s", exc)
        return None


__all__ = (
    "ERRORS",
    "ErrorRecord",
    "ErrorRecorder",
    "GENERIC_MESSAGE",
    "FRIENDLY",
    "friendly_message",
    "redact",
    "report_command_error",
    "report_app_error",
    "safe_send",
)

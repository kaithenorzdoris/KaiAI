"""Bảo mật & phân quyền KaiAI Ultimate V7 (#30, #31, #32, Security Center).

Bốn nguyên tắc an ninh cốt lõi:
--------------------------------
1. **Multi-tier RBAC**: Phân cấp quyền hạn nghiêm ngặt:
   - Owner (cấp 4): Toàn quyền kiểm soát bot.
   - Admin (cấp 3): Quản trị viên server Discord.
   - Moderator (cấp 2): Điều hành viên / Staff của server.
   - User (cấp 1): Thành viên thông thường.
   - Guest (cấp 0): Khách truy cập web chưa xác thực.
2. **Owner-only (#31)**: `shutdown`, `reload`, `sync`, `database`, `debug`… phải đúng
   là chủ bot (theo `BOT_OWNER_ID`), **không** dựa vào `administrator`. Người điều hành
   server cũng không vượt qua được.
3. **Hierarchy (#30)**: không ai tác động lên người có role ngang hoặc cao hơn
   mình; bot cũng không tác động lên người cao hơn bot.
4. **Không eval/exec/shell từ input (#32)**: :func:`is_dangerous_call` chặn các
   token nguy hiểm. KaiAI **không** cung cấp lệnh eval cho thành viên.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import discord

from .config import CONFIG
from .logging_setup import get_logger

log = get_logger("security")


class RoleLevel(IntEnum):
    """Cấp độ phân quyền trong hệ sinh thái KaiAI V7."""

    GUEST = 0
    USER = 1
    MODERATOR = 2
    ADMIN = 3
    OWNER = 4


#: Các token lệnh nguy hiểm tuyệt đối (#32).
FORBIDDEN_TOKENS: frozenset[str] = frozenset(
    {
        "rm", "del", "drop", "shutdown", "reboot", "halt", "poweroff",
        "mkfs", "format", "chmod", "chown", "curl", "wget",
        "eval", "exec", "system", "popen", "spawn", "subprocess",
        "os", "sh", "bash", "cmd", "powershell",
    }
)

#: Các lệnh nguy hiểm của chính KaiAI (#31).
OWNER_DANGEROUS_COMMANDS = frozenset(
    {"shutdown", "restart", "reload", "sync", "database", "debug", "eval", "shell"}
)

#: Ký tự phân tách cần cắt thêm vì shell không tách chúng thành token riêng
_FRAGMENT_SPLIT = re.compile(r"[.()\[\]{};,|&<>]+")


def is_dangerous_call(text: str) -> bool:
    """Kiểm tra chuỗi lệnh shell có token nguy hiểm hay không (#32).

    Chỉ dùng để **từ chối** — KaiAI không thực thi shell từ input người dùng.
    """
    try:
        parts = shlex.split(text)
    except ValueError:
        return True  # không parse được → coi như nguy hiểm
    for part in parts:
        for fragment in _FRAGMENT_SPLIT.split(part.lower()):
            if fragment in FORBIDDEN_TOKENS:
                return True
    return False


def is_owner(user_id: int) -> bool:
    """True nếu user là chủ bot (theo ``BOT_OWNER_ID``)."""
    return CONFIG.is_owner(user_id)


def is_bot_admin(member: discord.Member) -> bool:
    """True nếu member có quyền Administrator **hoặc** là chủ server."""
    permissions = getattr(member, "guild_permissions", None)
    if permissions is not None and permissions.administrator:
        return True
    guild = getattr(member, "guild", None)
    return bool(guild is not None and getattr(guild, "owner_id", None) == member.id)


def is_bot_moderator(member: discord.Member) -> bool:
    """True nếu member có quyền ban/kick/manage_messages hoặc vai trò Staff."""
    if is_bot_admin(member):
        return True
    permissions = getattr(member, "guild_permissions", None)
    if permissions is not None and (
        permissions.ban_members
        or permissions.kick_members
        or permissions.manage_messages
        or permissions.moderate_members
    ):
        return True
    # Kiểm tra theo tên hoặc ID vai trò Staff
    staff_name = CONFIG.log_staff_role_name.casefold()
    staff_id = CONFIG.log_staff_role_id
    for role in getattr(member, "roles", ()):
        if (staff_id and role.id == staff_id) or role.name.casefold() == staff_name:
            return True
    return False


def get_member_role_level(member: discord.Member | None, user_id: int | None = None) -> RoleLevel:
    """Xác định cấp độ vai trò chính xác của người dùng."""
    uid = member.id if member is not None else (user_id or 0)
    if is_owner(uid):
        return RoleLevel.OWNER
    if member is None:
        return RoleLevel.USER if uid > 0 else RoleLevel.GUEST
    if is_bot_admin(member):
        return RoleLevel.ADMIN
    if is_bot_moderator(member):
        return RoleLevel.MODERATOR
    return RoleLevel.USER


@dataclass(slots=True)
class GuardResult:
    """Kết quả kiểm tra quyền, dùng chung cho prefix, slash và views."""

    allowed: bool
    message: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def require_owner(user_id: int) -> GuardResult:
    """Kiểm tra owner-only cho lệnh nguy hiểm (#31)."""
    if is_owner(user_id):
        return GuardResult(allowed=True)
    return GuardResult(allowed=False, message="⛔ Lệnh này chỉ dành cho chủ bot.")


def require_admin(member: discord.Member) -> GuardResult:
    """Kiểm tra quyền quản trị (owner hoặc Administrator)."""
    if is_bot_admin(member):
        return GuardResult(allowed=True)
    return GuardResult(allowed=False, message="⛔ Cần quyền Administrator để dùng lệnh này.")


def require_role_level(member: discord.Member | None, min_level: RoleLevel) -> GuardResult:
    """Yêu cầu người dùng đạt cấp độ phân quyền tối thiểu."""
    level = get_member_role_level(member)
    if level >= min_level:
        return GuardResult(allowed=True)
    names = {
        RoleLevel.OWNER: "Chủ bot",
        RoleLevel.ADMIN: "Administrator",
        RoleLevel.MODERATOR: "Moderator / Staff",
        RoleLevel.USER: "Thành viên",
    }
    required_name = names.get(min_level, "Cấp cao")
    return GuardResult(
        allowed=False,
        message=f"⛔ Thao tác này yêu cầu quyền hạn tối thiểu: **{required_name}**.",
    )


def can_interact_view(
    actor_id: int,
    owner_id: int,
    *,
    allow_owner_bypass: bool = True,
) -> GuardResult:
    """Kiểm tra quyền tương tác với Button View / Select Menu."""
    if actor_id == owner_id:
        return GuardResult(allowed=True)
    if allow_owner_bypass and is_owner(actor_id):
        return GuardResult(allowed=True)
    return GuardResult(
        allowed=False,
        message="❌ Giao diện này không thuộc về bạn.",
    )


def check_hierarchy(
    actor: discord.Member,
    target: discord.Member,
    *,
    guild: discord.Guild | None = None,
) -> GuardResult:
    """Chặn tác động lên người ngang hoặc cao hơn mình — và cao hơn bot (#30)."""
    guild = guild or actor.guild
    if actor.id == target.id:
        return GuardResult(allowed=False, message="❌ Không thể tự tác động lên chính mình.")
    if is_owner(actor.id):
        return GuardResult(allowed=True)
    if actor.top_role <= target.top_role:
        return GuardResult(
            allowed=False, message="❌ Bạn không thể tác động lên người có role ngang hoặc cao hơn."
        )
    me = guild.me if guild is not None else None
    if me is not None and me.top_role <= target.top_role:
        return GuardResult(
            allowed=False,
            message="❌ Role của bot đang thấp hơn người này — không thể tác động.",
        )
    return GuardResult(allowed=True)


def check_role_hierarchy(actor: discord.Member, role: discord.Role) -> GuardResult:
    """Bot và người thực hiện phải cao hơn role được cấp/thu hồi (#30)."""
    guild = actor.guild
    me = guild.me if guild is not None else None
    if me is not None and me.top_role <= role:
        return GuardResult(allowed=False, message="❌ Role đó cao hơn hoặc ngang role của bot.")
    if not is_owner(actor.id) and actor.top_role <= role:
        return GuardResult(
            allowed=False, message="❌ Bạn không thể quản lý role cao hơn hoặc ngang bạn."
        )
    return GuardResult(allowed=True)


def bot_has(guild: discord.Guild, **permissions: bool) -> GuardResult:
    """Kiểm tra bot có đủ quyền Discord không trước khi thao tác (#30)."""
    me = guild.me
    if me is None:
        return GuardResult(allowed=False, message="❌ Không xác định được bot trong server.")
    missing = [
        name for name, needed in permissions.items()
        if not getattr(me.guild_permissions, name, False)
    ]
    if missing:
        pretty = ", ".join(f"`{m}`" for m in missing)
        return GuardResult(allowed=False, message=f"❌ Bot thiếu quyền: {pretty}.")
    return GuardResult(allowed=True)


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?is)(token|api[_-]?key|secret|password|authorization)\s*[:=]\s*(?:bearer\s+)?[A-Za-z0-9_\-./+=]{6,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bMTA[A-Za-z0-9_-]{20,}\b"),
)


def contains_secret(text: str) -> bool:
    """Phát hiện chuỗi có vẻ chứa secret (dùng cho ``/diagnostics``)."""
    return any(pattern.search(text or "") for pattern in _SECRET_PATTERNS)


def safe_env_keys() -> list[str]:
    """Danh sách **tên** biến môi trường đang đặt (không lộ giá trị)."""
    return sorted(k for k in os.environ if k.endswith(("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")))


@dataclass(slots=True)
class SecurityEvent:
    """Sự kiện an ninh cần ghi nhật ký giám sát."""

    event_type: str
    actor_id: int
    severity: str = "INFO"
    details: str = ""
    timestamp: float = field(default_factory=time.time)


def log_security_event(event_type: str, actor_id: int, details: str, severity: str = "WARNING") -> None:
    """Ghi nhật ký sự kiện bảo mật vào hệ thống log trung tâm."""
    log.warning(
        "[SECURITY EVENT] [%s] [%s] actor=%s: %s",
        severity.upper(),
        event_type,
        actor_id,
        details,
    )


__all__ = (
    "FORBIDDEN_TOKENS",
    "GuardResult",
    "OWNER_DANGEROUS_COMMANDS",
    "RoleLevel",
    "SecurityEvent",
    "bot_has",
    "can_interact_view",
    "check_hierarchy",
    "check_role_hierarchy",
    "contains_secret",
    "get_member_role_level",
    "is_bot_admin",
    "is_bot_moderator",
    "is_dangerous_call",
    "is_owner",
    "log_security_event",
    "require_admin",
    "require_owner",
    "require_role_level",
    "safe_env_keys",
)

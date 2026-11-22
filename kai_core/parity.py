"""Parity Engine — tự sinh Slash Command cho **mọi** Prefix Command sẵn có.

Vấn đề
------
KaiAI có hơn 160 lệnh dạng prefix (``!cash``, ``!daily``, ``!taixiu`` …) nhưng
gần như không có bản slash tương ứng. Viết tay ~160 slash command sẽ tạo ra
hàng trăm dòng trùng lặp và chắc chắn lệch logic với prefix.

Cách tiếp cận (OLD SYSTEM + NEW SYSTEM)
---------------------------------------
Thay vì đụng vào các Cog cũ, engine này **đọc** danh sách lệnh prefix đã đăng ký
rồi **sinh** một ``app_commands.Command`` tương ứng cho từng lệnh:

1. ``plan_command()`` phân tích ``Command.params`` của lệnh prefix, suy ra kiểu
   dữ liệu slash hợp lệ cho từng tham số.
2. ``build_callback()`` **sinh động** một hàm callback có đúng chữ ký (signature)
   mà ``discord.app_commands`` cần, nhờ ``exec`` trên mã nguồn được sinh ra.
3. Callback đó **không** parse chuỗi lại: nó dựng
   ``Context`` từ ``Interaction`` rồi gọi lại đúng hàm callback gốc của lệnh
   prefix, bằng đúng bộ giá trị mà slash đã chuyển đổi.
4. Toàn bộ pipeline của prefix được tái sử dụng nguyên vẹn: ``@has_permissions``,
   ``@cooldown``, ``@max_concurrency``, before/after hooks, ``cog_command_error``
   và ``on_command_error`` — nhờ đi qua ``Command.prepare``/``dispatch_error`` của
   chính discord.py.

Nhờ vậy: lệnh cũ **không bị sửa một dòng nào**, không có logic kinh doanh nào bị
nhân bản, và mọi lệnh mới thêm vào sau này cũng tự động có slash.

Bỏ qua
------
Lệnh không thể ánh xạ an toàn sẽ được **bỏ qua kèm lý do** (ghi log), thay vì tạo
ra slash hỏng. Xem :func:`install_parity` để biết chi tiết.
"""

from __future__ import annotations

import asyncio
import re
import types as _types
import typing
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.enums import AppCommandType
from discord.ext import commands
from discord.ext.commands import converter as conv_mod

from .logging_setup import get_logger

log = get_logger("parity")

#: Slash command chỉ nhận tối đa 25 tham số (giới hạn của Discord).
MAX_SLASH_OPTIONS = 24

#: Độ dài tối đa của description theo giới hạn Discord.
MAX_DESC = 100

#: Tên tham số slash chỉ được chứa chữ thường, số, ``-`` và ``_``.
_IMPORT_RE = str.maketrans({"-": "_", ".": "_"})


class ParitySkip(Exception):
    """Lệnh prefix không thể sinh slash an toàn; mang theo lý do cụ thể."""


def sanitize_param_name(name: str) -> str:
    """Đổi tên tham số hợp lệ theo chuẩn slash (giữ nguyên nếu đã hợp lệ)."""
    cleaned = name.translate(_IMPORT_RE)
    cleaned = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in cleaned)
    cleaned = cleaned.strip("_") or "arg"
    if not cleaned[0].isalpha():
        cleaned = f"a_{cleaned}"
    return cleaned.lower()


def unwrap_optional(annotation: object) -> tuple[object, bool]:
    """Tách ``Optional[X]`` / ``X | None`` thành ``(X, True)``.

    Trả về ``(annotation, False)`` nếu annotation không phải Optional.
    """
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is getattr(_types, "UnionType", None):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]  # noqa: E721
        if len(args) == 1:
            return args[0], True
    return annotation, False


@dataclass(frozen=True)
class TypePlan:
    """Kết quả ánh xạ một converter của prefix sang kiểu slash."""

    slash_type: object
    pass_through: bool
    note: str = ""


#: Ánh xạ trực tiếp: kiểu prefix -> kiểu slash tương đương (giá trị đã đúng sẵn).
DIRECT_TYPES: dict[type, object] = {
    str: str,
    int: int,
    float: float,
    bool: bool,
    discord.Member: discord.Member,
    discord.User: discord.User,
    discord.Role: discord.Role,
    discord.TextChannel: discord.TextChannel,
    discord.VoiceChannel: discord.VoiceChannel,
    discord.StageChannel: discord.StageChannel,
    discord.CategoryChannel: discord.CategoryChannel,
    discord.ForumChannel: discord.ForumChannel,
    discord.Thread: discord.Thread,
    discord.abc.GuildChannel: discord.abc.GuildChannel,
    discord.Attachment: discord.Attachment,
    commands.MemberConverter: discord.Member,
    commands.UserConverter: discord.User,
    commands.RoleConverter: discord.Role,
    commands.TextChannelConverter: discord.TextChannel,
    commands.Greedy: str,
}

#: Emoji không được slash chấp nhận trực tiếp → nhận ``str`` rồi chạy lại converter.
STRING_CONVERTERS = (discord.PartialEmoji, discord.Emoji)


def resolve_type(annotation: object) -> TypePlan:
    """Ánh xạ một annotation/converter của prefix sang kiểu dùng được cho slash.

    * Kiểu tra tiếng Anh đã đúng sẵn (Member, Role, int, …) → trả về nguyên vẹn
      (``pass_through=True``), callback nhận đúng đối tượng như lệnh prefix.
    * ``Range[X]`` / ``Greedy[X]`` → bóc lấy ``X``.
    * Converter tuỳ chỉnh → khai báo ``str`` cho slash và ``pass_through=False``;
      giá trị sẽ được chạy lại qua chính converter đó nên cho ra **cùng kết quả**
      với lệnh prefix.
    """
    annotation, is_optional = unwrap_optional(annotation)

    if isinstance(annotation, conv_mod.Greedy):
        return resolve_type(annotation.converter)

    if isinstance(annotation, commands.Range):
        return resolve_type(annotation.inner_type)

    if isinstance(annotation, commands.Converter):
        return TypePlan(slash_type=str, pass_through=False, note="custom-converter")

    if isinstance(annotation, type):
        if annotation is discord.Message:
            raise ParitySkip("tham số kiểu discord.Message không dùng được với slash")
        if annotation in STRING_CONVERTERS:
            return TypePlan(slash_type=str, pass_through=False, note="emoji")
        target = DIRECT_TYPES.get(annotation)
        if target is not None:
            note = "optional" if is_optional else ""
            return TypePlan(slash_type=target, pass_through=True, note=note)
        raise ParitySkip(f"converter không hỗ trợ: {getattr(annotation, '__name__', annotation)}")

    raise ParitySkip(f"annotation không xác định được: {annotation!r}")


def describe_param(name: str, explicit: str | None) -> str:
    """Sinh description hợp lệ (1–100 ký tự) cho tham số slash."""
    text = (explicit or "").strip()
    if not text:
        text = f"Giá trị cho {name}"
    text = " ".join(text.split())
    if len(text) > MAX_DESC:
        text = text[: MAX_DESC - 1].rstrip() + "…"
    return text or f"Tham số {name}"


@dataclass(slots=True)
class ParamPlan:
    """Kế hoạch ánh xạ cho một tham số của lệnh prefix."""

    source_name: str
    slash_name: str
    slash_type: object
    required: bool
    default: object
    description: str
    kind: str  # "positional" | "keyword"
    pass_through: bool
    converter: object


@dataclass(slots=True)
class CommandPlan:
    """Kế hoạch sinh slash cho một lệnh prefix (hoặc group)."""

    name: str
    description: str
    params: list[ParamPlan] = field(default_factory=list)
    is_group: bool = False
    subcommands: list[CommandPlan] = field(default_factory=list)


def _default_for(param: commands.Parameter) -> object:
    """Giá trị mặc định dùng cho slash (``None`` nếu bắt buộc)."""
    if param.required:
        return None
    default = param.default
    if default is commands.Parameter.empty or default is None:
        return None
    return default


def plan_command(cmd: commands.Command) -> CommandPlan:
    """Phân tích lệnh prefix và trả về :class:`CommandPlan`.

    Ném :class:`ParitySkip` nếu lệnh không thể sinh slash an toàn.
    """
    if cmd.hidden:
        raise ParitySkip("lệnh bị ẩn (hidden)")

    description = (cmd.help or cmd.brief or "").strip()
    if not description:
        description = f"Lệnh {cmd.qualified_name} (chạy bằng {cmd.name})"
    description = " ".join(description.split())[:MAX_DESC] or f"Lệnh {cmd.qualified_name}"

    params: list[ParamPlan] = []
    used: set[str] = set()

    for param in cmd.params.values():
        plan = resolve_type(param.converter)
        slash_name = sanitize_param_name(param.name)
        if slash_name in used:
            raise ParitySkip(f"trùng tên tham số sau khi chuẩn hoá: {param.name}")
        used.add(slash_name)

        default = _default_for(param)
        # ``Optional[X]`` không có default vẫn phải là tuỳ chọn trên slash.
        required = param.required and plan.note != "optional"
        kind = "keyword" if param.kind == param.KEYWORD_ONLY else "positional"
        params.append(
            ParamPlan(
                source_name=param.name,
                slash_name=slash_name,
                slash_type=plan.slash_type,
                required=required,
                default=default,
                description=describe_param(param.name, param.description),
                kind=kind,
                pass_through=plan.pass_through,
                converter=param.converter,
            )
        )

    if len(params) > MAX_SLASH_OPTIONS:
        raise ParitySkip(f"quá nhiều tham số cho slash ({len(params)})")

    keyword_params = [p for p in params if p.kind == "keyword"]
    if len(keyword_params) > 1:
        # discord.py chỉ parse được tham số keyword-only đầu tiên (consume rest).
        raise ParitySkip("nhiều hơn một tham số keyword-only")

    return CommandPlan(name=cmd.name, description=description, params=params)


# ---------------------------------------------------------------------------
# Sinh callback động
# ---------------------------------------------------------------------------

_CALLBACK_NAME = "_kai_parity_callback"
_DISPATCH_NAME = "_kai_parity_dispatch"


def _literal_default(value: object, *, required: bool, param_name: str) -> str:
    """Biểu diễn giá trị mặc định thành literal Python hợp lệ cho signature."""
    if value is None:
        return "None"
    if isinstance(value, (str, int, float, bool)):
        return repr(value)
    if required:
        raise ParitySkip(f"tham số {param_name} bắt buộc có default không thể biểu diễn")
    return "None"


def _build_source(plans: list[ParamPlan]) -> tuple[str, dict[str, object]]:
    """Sinh mã nguồn callback + namespace chứa các kiểu dùng trong chữ ký."""
    namespace: dict[str, object] = {"discord": discord}
    type_alias: dict[int, str] = {}
    arguments: list[str] = []
    passed: list[str] = []
    star_added = False

    for plan in plans:
        key = id(plan.slash_type)
        alias = type_alias.get(key)
        if alias is None:
            alias = f"_type{len(type_alias)}"
            type_alias[key] = alias
            namespace[alias] = plan.slash_type

        if plan.kind == "keyword" and not star_added:
            arguments.append("*")
            star_added = True

        if plan.required:
            arguments.append(f"{plan.slash_name}: {alias}")
        else:
            default = _literal_default(plan.default, required=False, param_name=plan.slash_name)
            arguments.append(f"{plan.slash_name}: {alias} = {default}")
        passed.append(f"{plan.slash_name!r}: {plan.slash_name}")

    signature = ", ".join(["interaction: discord.Interaction", *arguments])
    body = f"    return await {_DISPATCH_NAME}(interaction, {{{', '.join(passed)}}})"
    source = f"async def {_CALLBACK_NAME}({signature}):\n{body}\n"
    return source, namespace


# ---------------------------------------------------------------------------
# Gọi lại lệnh prefix từ slash
# ---------------------------------------------------------------------------


class _SyntheticMessage:
    """Bọc message ảo của interaction để ``ctx.message.delete()`` trở thành no-op.

    ``Context.from_interaction`` tạo một message **ảo** (id không tồn tại trên
    Discord). Nhiều lệnh cũ gọi ``await ctx.message.delete()`` — nếu để nguyên,
    lệnh sẽ ném ``NotFound``. Với slash, thao tác xoá "tin nhắn lệnh" không có ý
    nghĩa, nên ta vô hiệu hoá đúng thao tác đó và giữ nguyên mọi thuộc tính khác.
    """

    __slots__ = ("_message",)

    def __init__(self, message: discord.Message) -> None:
        object.__setattr__(self, "_message", message)

    def __getattr__(self, item: str):
        return getattr(object.__getattribute__(self, "_message"), item)

    def __setattr__(self, key: str, value) -> None:
        setattr(object.__getattribute__(self, "_message"), key, value)

    async def delete(self, *args, **kwargs) -> None:
        """Message ảo không tồn tại nên coi như đã xoá."""
        return None

    async def purge(self, *args, **kwargs):  # pragma: no cover - phòng thủ
        raise AttributeError("purge() không khả dụng trên message ảo")


async def _run_prefix_command(
    interaction: discord.Interaction,
    prefix_cmd: commands.Command,
    values: dict[str, object],
) -> None:
    """Dựng ``Context`` từ interaction rồi chạy lại đúng lệnh prefix đã đăng ký.

    Việc gọi lại pipeline gốc của discord.py (``can_run`` → cooldown → hook →
    callback → ``dispatch_error``) đảm bảo prefix và slash có **cùng** hành vi:
    cùng quyền, cùng cooldown, cùng cách báo lỗi.
    """
    from discord.ext.commands.core import hooked_wrapped_callback

    bot: commands.Bot = interaction.client  # type: ignore[assignment]

    ctx = await commands.Context.from_interaction(interaction)
    ctx.message = _SyntheticMessage(ctx.message)  # type: ignore[assignment]
    ctx.command = prefix_cmd
    ctx.invoked_with = prefix_cmd.name
    ctx.subcommand_passed = None
    ctx.invoked_subcommand = None
    interaction._baton = ctx

    args: list[object] = [prefix_cmd.cog, ctx] if prefix_cmd.cog is not None else [ctx]
    kwargs: dict[str, object] = {}

    for param in prefix_cmd.params.values():
        plan_value = values.get(sanitize_param_name(param.name), None)
        if plan_value is None and not param.required:
            kwargs[param.name] = (
                None if param.default is commands.Parameter.empty else param.default
            )
            continue
        if not _passes_through(param.converter):
            try:
                plan_value = await conv_mod.run_converters(ctx, param.converter, plan_value, param)
            except commands.CommandError:
                raise
            except Exception as exc:
                raise commands.BadArgument(f"Giá trị không hợp lệ cho `{param.name}`") from exc
        if param.kind == param.KEYWORD_ONLY:
            kwargs[param.name] = plan_value
        else:
            args.append(plan_value)

    bot.dispatch("command", ctx)
    callback_completed = False
    limiter = getattr(prefix_cmd, "_max_concurrency", None)
    try:
        if not await prefix_cmd.can_run(ctx):
            raise commands.CheckFailure(
                f"Kiểm tra quyền của lệnh `{prefix_cmd.qualified_name}` thất bại."
            )
        if limiter is not None:
            await limiter.acquire(ctx)
        prepare_cooldowns = getattr(prefix_cmd, "_prepare_cooldowns", None)
        if prepare_cooldowns is not None:
            prepare_cooldowns(ctx)
        await prefix_cmd.call_before_hooks(ctx)

        injected = hooked_wrapped_callback(prefix_cmd, ctx, prefix_cmd.callback)  # type: ignore[arg-type]
        await injected(*args, **kwargs)
        callback_completed = True
    except app_commands.AppCommandError as exc:
        await _safe_respond(interaction, f"❌ {exc}")
        ctx.command_failed = True
    except commands.CommandError as exc:
        await _safe_dispatch_error(prefix_cmd, ctx, exc)
    except asyncio.CancelledError:  # pragma: no cover - không nuốt cancel
        raise
    except Exception as exc:
        log.exception("Lỗi khi chạy lệnh prefix qua slash: %s", prefix_cmd.qualified_name)
        await _safe_dispatch_error(prefix_cmd, ctx, commands.CommandInvokeError(exc))
    finally:
        if limiter is not None:
            await limiter.release(ctx.message)
        if callback_completed:
            await prefix_cmd.call_after_hooks(ctx)

    if not ctx.command_failed:
        bot.dispatch("command_completion", ctx)
    interaction.command_failed = ctx.command_failed


def _passes_through(converter: object) -> bool:
    """Giá trị slash đã đúng kiểu gốc thì không cần chạy lại converter."""
    try:
        return resolve_type(converter).pass_through
    except ParitySkip:
        return False


async def _safe_dispatch_error(cmd: commands.Command, ctx, error: commands.CommandError) -> None:
    """Đưa lỗi qua hệ thống xử lý chuẩn; tuyệt đối không lộ traceback cho user."""
    try:
        await cmd.dispatch_error(ctx, error)
    except asyncio.CancelledError:  # pragma: no cover
        raise
    except Exception:
        log.exception("Không xử lý được lỗi cho lệnh %s", cmd.qualified_name)


async def _safe_respond(interaction: discord.Interaction, message: str) -> None:
    """Trả lời interaction một cách an toàn (không phụ thuộc state response)."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.debug("Không gửi được phản hồi slash (interaction hết hạn)")


# ---------------------------------------------------------------------------
# Cài đặt lên cây slash
# ---------------------------------------------------------------------------

_COMMAND_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


def sanitize_command_name(name: str) -> str | None:
    """Chuẩn hoá tên lệnh theo chuẩn slash; ``None`` nếu không hợp lệ."""
    lowered = name.strip().lower().replace(" ", "-")
    lowered = "".join(ch if (ch.isalnum() or ch in "_-") else "" for ch in lowered)
    return lowered if _COMMAND_NAME_RE.match(lowered) else None


@dataclass(slots=True)
class ParityReport:
    """Kết quả cài parity — dùng cho startup log, ``/diagnostics`` và test."""

    created: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    already_slash: list[str] = field(default_factory=list)
    overflow: list[str] = field(default_factory=list)
    guild_scoped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.skipped) + len(self.overflow)

    @property
    def coverage(self) -> float:
        return (len(self.created) / self.total) if self.total else 1.0

    def summary(self) -> str:
        pct = self.coverage * 100
        return (
            f"slash mirror: {len(self.created)}/{self.total} lệnh ({pct:.1f}%)"
            f" • {len(self.already_slash)} đã có sẵn"
            f" • {len(self.guild_scoped)} gắn theo guild"
            f" • {len(self.overflow)} vượt hạn mức Discord"
        )


def _existing_slash_names(bot: commands.Bot) -> set[str]:
    """Tập tên slash đã đăng ký sẵn (kể cả subcommand của group)."""
    names: set[str] = set()

    def collect(items):
        for item in items:
            names.add(item.name)
            if isinstance(item, app_commands.Group):
                collect(item.commands)

    try:
        collect(bot.tree.get_commands())
    except Exception:  # pragma: no cover - tree chưa sẵn sàng
        pass
    return names


def _make_app_command(prefix_cmd: commands.Command, plan: CommandPlan) -> app_commands.Command:
    """Dựng ``app_commands.Command`` cho một lệnh prefix đã lên kế hoạch."""
    source, namespace = _build_source(plan.params)

    async def _dispatch(interaction: discord.Interaction, values: dict[str, object]) -> None:
        await _run_prefix_command(interaction, prefix_cmd, values)

    namespace[_DISPATCH_NAME] = _dispatch
    exec(source, namespace, namespace)  # noqa: S102 - sinh động chữ ký hợp lệ
    callback = namespace[_CALLBACK_NAME]

    descriptions = {item.slash_name: item.description for item in plan.params}
    if descriptions:
        callback = app_commands.describe(**descriptions)(callback)
    if getattr(prefix_cmd, "guild_only", False):
        callback = app_commands.guild_only()(callback)

    return app_commands.Command(
        name=plan.name,
        description=plan.description,
        callback=callback,  # type: ignore[arg-type]
        nsfw=bool(getattr(prefix_cmd, "nsfw", False)),
    )


def _build_group(
    prefix_group: commands.Group,
    report: ParityReport,
    taken: set[str],
) -> app_commands.Group | None:
    """Dựng ``app_commands.Group`` từ một ``commands.Group`` của prefix."""
    group_name = sanitize_command_name(prefix_group.name)
    if group_name is None or group_name in taken:
        report.skipped[prefix_group.qualified_name] = "tên group không hợp lệ hoặc trùng"
        return None

    description = (prefix_group.help or prefix_group.brief or "").strip()
    description = " ".join(description.split())[:MAX_DESC] or f"Nhóm lệnh {prefix_group.name}"

    group = app_commands.Group(
        name=group_name,
        description=description,
        guild_only=bool(getattr(prefix_group, "guild_only", False)),
        nsfw=bool(getattr(prefix_group, "nsfw", False)),
    )

    sub_names: set[str] = set()
    added = 0
    for sub in sorted(prefix_group.commands, key=lambda c: c.name):
        sub_name = sanitize_command_name(sub.name)
        if sub_name is None or sub_name in sub_names:
            report.skipped[sub.qualified_name] = "tên subcommand không hợp lệ hoặc trùng"
            continue
        try:
            plan = plan_command(sub)
            plan.name = sub_name
            group.add_command(_make_app_command(sub, plan))
        except ParitySkip as exc:
            report.skipped[sub.qualified_name] = str(exc)
            continue
        except Exception as exc:  # pragma: no cover - phòng thủ
            report.skipped[sub.qualified_name] = f"{type(exc).__name__}: {exc}"
            continue
        sub_names.add(sub_name)
        report.created.append(f"{group_name} {sub_name}")
        added += 1

    if added == 0:
        report.skipped[prefix_group.qualified_name] = "không có subcommand nào sinh được slash"
        return None

    if getattr(prefix_group, "invoke_without_command", False):
        try:
            base_plan = plan_command(prefix_group)
            base_plan.name = group_name
            group.callback = _make_app_command(prefix_group, base_plan).callback
            report.created.append(f"{group_name} (mặc định)")
        except ParitySkip as exc:
            log.debug("Group %s không dựng được callback mặc định: %s", prefix_group.qualified_name, exc)

    taken.add(group_name)
    return group


#: Thứ tự ưu tiên khi phải chọn lệnh nào lấp đầy hạn mức 100 của Discord.
#: Đặt "syst./mod/economy" lên đầu vì đây là lệnh người dùng gõ nhiều nhất.
COG_PRIORITY: dict[str, int] = {
    "system": 0,
    "admin": 1,
    "games": 2,
    "database": 3,
    "kai_premium": 4,
    "ai_chat": 5,
    "utility_pro": 6,
    "love": 7,
    "logs": 8,
}

#: Discord chỉ cho phép tối đa 100 application command cấp cao nhất cho mỗi phạm vi.
SLASH_LIMIT = 100

#: Lệnh "hạt nhân" luôn được ưu tiên đặt ở phạm vi toàn cục.
#: Đây là các lệnh người dùng mong đợi gõ dạng slash nhất — kể cả khi chúng
#: nằm cuối thứ tự chữ cái trong cog của mình.
STAR_COMMANDS: frozenset[str] = frozenset(
    {
        # AI
        "ai", "menu", "mood", "stats", "clearai",
        # Economy / games
        "cash", "dep", "with", "pay", "daily", "work", "quest",
        "taixiu", "chanle", "coinflip", "bongda", "dice", "slots",
        "blackjack", "roulette", "fish", "mine", "rps", "highlow",
        "vaytien", "trano", "taisan", "topmoney", "market", "games",
        # Hồ sơ / tiến trình
        "profile", "top", "levels", "rank", "bot",
        # Hệ thống
        "help", "sysinfo", "uptime", "invite", "ping", "botinfo", "health",
        # Moderation
        "admin", "warn", "ban", "unban", "kick", "mute", "unmute",
        "xoa", "lock", "unlock", "slowmode", "nuke",
        "setup", "logstatus", "backup",
    }
)


def _priority_of(cmd: commands.Command) -> tuple[int, int, str]:
    """Sắp xếp lệnh để lấp đầy hạn mức 100 slot toàn cục một cách hợp lý."""
    cog_name = getattr(cmd.cog, "qualified_name", "") or ""
    is_star = 0 if cmd.name in STAR_COMMANDS else 1
    return (is_star, COG_PRIORITY.get(cog_name, 99), cmd.qualified_name)


def _tree_size(tree) -> int:
    """Đếm số command **cấp cao nhất** toàn cục (đúng cách Discord giới hạn).

    Discord chỉ giới hạn 100 application command cấp cao nhất; subcommand nằm
    trong group và **không** tính vào hạn mức đó.

    ⚠️ Ba cái bẫy đã từng phá vỡ hạn mức này — đừng sửa lại:

    1. ``tree.get_commands()`` (không truyền ``type``) trả về danh sách **phẳng**,
       đã mở group ra lấy cả subcommand ⇒ đếm quá.
    2. Enum đúng là :class:`discord.enums.AppCommandType`, **không** phải
       ``discord.app_commands.CommandType`` (không tồn tại).
    3. Nếu nuốt lỗi trong im lặng rồi trả ``0``, hạn mức 100 sẽ **không bao giờ
       được kiểm tra** và bot cố đẩy hết lệnh lên toàn cục.

    Vì vậy lỗi ở đây trả về ``SLASH_LIMIT`` (coi như đã đầy) để fail-closed và
    luôn ghi log.
    """
    try:
        return len(tree.get_commands(type=AppCommandType.chat_input))
    except Exception:  # noqa: BLE001
        log.exception(
            "Không đếm được cây lệnh — coi như đã đầy hạn mức %d để an toàn.", SLASH_LIMIT
        )
        return SLASH_LIMIT


def _guild_size(tree, guild_id: int | None) -> int:
    """Đếm số command đã gắn riêng cho guild (dùng hạn mức 100 độc lập)."""
    if not guild_id:
        return 0
    try:
        return len(tree._guild_commands.get(guild_id, {}))  # noqa: SLF001
    except Exception:  # pragma: no cover
        return 0


def install_parity(
    bot: commands.Bot,
    limit: int = SLASH_LIMIT,
    guild_id: int | None = None,
) -> ParityReport:
    """Sinh slash mirror cho lệnh prefix còn thiếu, tôn trọng hạn mức Discord.

    Discord cho phép tối đa **100 application command cấp cao nhất** cho mỗi
    phạm vi (subcommand không tính vào 100). KaiAI có ~160 lệnh prefix nên không
    thể để tất cả ở dạng phẳng toàn cục. Chiến lược hai tầng:

    1. **Phẳng toàn cục** — ưu tiên theo :data:`COG_PRIORITY` (hệ thống →
       moderation → economy → AI → tiện ích). Giữ nguyên trải nghiệm quen thuộc
       ``/cash``, ``/work``, ``/vaytien``…
    2. **Phần dư gắn theo guild** (``guild_id``) — mỗi guild có hạn mức 100 *riêng*,
       nên phần lệnh còn lại vẫn tồn tại dạng slash trong server đó.

    Lệnh không ánh xạ được ghi vào :attr:`ParityReport.skipped` **kèm lý do**;
    lệnh bị trượt ghi vào :attr:`ParityReport.overflow`. Cả hai đều tra được bằng
    ``/diagnostics commands`` — không có gì bị giấu.

    Idempotent: chạy lại không tạo trùng.
    """
    report = ParityReport()
    taken = _existing_slash_names(bot)
    guild_scope = discord.Object(id=guild_id) if guild_id else None

    for prefix_cmd in sorted(bot.commands, key=_priority_of):
        qualified = prefix_cmd.qualified_name

        if isinstance(prefix_cmd, commands.Group) and prefix_cmd.commands:
            sub_names = [
                sanitize_command_name(sub.name)
                for sub in sorted(prefix_cmd.commands, key=lambda c: c.name)
            ]
            usable = [n for n in sub_names if n and n not in taken]
            if not usable:
                report.already_slash.append(qualified)
                continue
            if _tree_size(bot.tree) + len(usable) + 1 > limit:
                if guild_scope is not None and _guild_size(bot.tree, guild_id) + len(usable) + 1 <= limit:
                    group_target = guild_scope
                else:
                    report.overflow.append(qualified)
                    continue
            else:
                group_target = None
            group = _build_group(prefix_cmd, report, taken)
            if group is not None:
                try:
                    bot.tree.add_command(group, override=True, guild=group_target)
                except app_commands.CommandAlreadyRegistered:
                    report.already_slash.append(qualified)
                    continue
                except app_commands.CommandLimitReached:
                    # Nhánh group cũng phải bắt lỗi hạn mức — thiếu đoạn này
                    # khiến lỗi nổi lên làm hỏng cả quá trình khởi động bot.
                    report.overflow.append(qualified)
                    continue
                except Exception as exc:  # pragma: no cover - phòng thủ
                    report.skipped[qualified] = f"đăng ký group lỗi: {exc}"
                    continue
                if group_target is not None:
                    report.guild_scoped.append(qualified)
            continue

        name = sanitize_command_name(prefix_cmd.name)
        if name is None:
            report.skipped[qualified] = "tên lệnh không hợp lệ cho slash"
            continue
        if name in taken:
            report.already_slash.append(qualified)
            continue

        if _tree_size(bot.tree) + 1 <= limit:
            target_guild = None
        elif guild_scope is not None and _guild_size(bot.tree, guild_id) + 1 <= limit:
            target_guild = guild_scope
        else:
            report.overflow.append(qualified)
            continue

        try:
            plan = plan_command(prefix_cmd)
            plan.name = name
            app_command = _make_app_command(prefix_cmd, plan)
        except ParitySkip as exc:
            report.skipped[qualified] = str(exc)
            continue
        except Exception as exc:  # pragma: no cover - phòng thủ
            report.skipped[qualified] = f"{type(exc).__name__}: {exc}"
            continue

        try:
            bot.tree.add_command(app_command, override=True, guild=target_guild)
        except app_commands.CommandAlreadyRegistered:
            report.already_slash.append(qualified)
            continue
        except app_commands.CommandLimitReached:
            report.overflow.append(qualified)
            continue
        except Exception as exc:  # pragma: no cover - phòng thủ
            report.skipped[qualified] = f"đăng ký lỗi: {exc}"
            continue

        taken.add(name)
        report.created.append(name)
        if target_guild is not None:
            report.guild_scoped.append(name)

    log.info("Parity: %s", report.summary())
    if report.overflow:
        log.warning(
            "Còn %d lệnh chưa có slash do giới hạn %d của Discord (xem /diagnostics commands).",
            len(report.overflow),
            limit,
        )
    for qualified, reason in report.skipped.items():
        log.warning("Bỏ qua slash cho '%s': %s", qualified, reason)
    return report


__all__ = (
    "install_parity",
    "ParityReport",
    "ParitySkip",
    "CommandPlan",
    "ParamPlan",
    "TypePlan",
    "plan_command",
    "resolve_type",
    "sanitize_param_name",
    "sanitize_command_name",
    "describe_param",
    "unwrap_optional",
)

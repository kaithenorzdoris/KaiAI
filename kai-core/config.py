"""Centralised, validated configuration for KaiAI.

Every environment variable the bot reads is declared here, parsed defensively and
exposed through :data:`CONFIG`.  Importing this module never raises: invalid
values fall back to safe defaults and are reported through
:meth:`BotConfig.validate` so ``main.py`` can print a helpful startup report.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

try:  # python-dotenv is optional at runtime.
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dependency missing
    load_dotenv = None  # type: ignore[assignment]

DEFAULT_PREFIXES: Sequence[str] = ("!", ".", "?")
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_IMAGE_MODEL = "gpt-image-1"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"

_TRUE = {"1", "true", "t", "yes", "y", "on", "enable", "enabled"}
_FALSE = {"0", "false", "f", "no", "n", "off", "disable", "disabled"}


def _load_env_file() -> None:
    """Load a local ``.env`` file when python-dotenv is installed."""
    if load_dotenv is None:
        return
    try:
        load_dotenv(override=False)
    except Exception:  # pragma: no cover - never fail because of dotenv
        pass


def get_env(name: str, default: str | None = None) -> str | None:
    """Return a stripped environment value, treating blanks as missing."""
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def get_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = get_env(name)
    if raw is None:
        return default
    # ⚠️ Phải thử ``int(raw)`` TRƯỚC. ``int(float(raw))`` làm mất chính xác với
    # Discord snowflake: float64 chỉ giữ chính xác tới ~9.0e15, nên
    # ``123456789012345678`` thành ``123456789012345680`` — âm thầm trỏ sang
    # guild/channel/role **không tồn tại**. Chỉ chuỗi kiểu "123.0" mới cần float.
    try:
        value = int(raw)
    except ValueError:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def get_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = get_env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    return value


def get_bool(name: str, default: bool = False) -> bool:
    raw = get_env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return default


def get_id_set(name: str, default: Sequence[int] = ()) -> set[int]:
    """Parse a comma/space separated list of Discord snowflake IDs."""
    raw = get_env(name)
    if raw is None:
        return set(default)
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    return ids or set(default)


def get_prefixes(name: str = "COMMAND_PREFIXES") -> Sequence[str]:
    raw = get_env(name)
    if not raw:
        return DEFAULT_PREFIXES
    prefixes = tuple(p for p in (part.strip() for part in raw.split(",")) if p)
    return prefixes or DEFAULT_PREFIXES


def _resolve_in_data_dir(raw: str, data_dir: Path, default_name: str) -> str:
    """Đưa đường dẫn database tương đối vào ``KAI_DATA_DIR``.

    Nếu không có, mọi file ``.db`` sinh ra nằm rải rác ở thư mục chạy và sẽ
    **mất sạch** khi container được thay thế — đúng thứ volume ``/app/data``
    trong Dockerfile cố gắng bảo vệ. Đường dẫn tuyệt đối giữ nguyên.
    """
    candidate = Path(raw).expanduser() if raw else Path(data_dir) / default_name
    if candidate.is_absolute():
        return str(candidate)
    return str(data_dir / candidate)


@dataclass(slots=True)
class BotConfig:
    """Snapshot of the process environment with sane defaults."""

    token: str | None = None
    owner_ids: set[int] = field(default_factory=set)
    log_channel_id: int = 0
    log_db_path: str = "kai_logs.db"
    log_retention_days: int = 30
    log_message_content: bool = False
    log_staff_role_id: int = 0
    log_staff_role_name: str = "Staff"
    data_dir: Path = field(default_factory=lambda: Path("."))
    prefixes: Sequence[str] = DEFAULT_PREFIXES

    openai_api_key: str | None = None
    openai_model: str = DEFAULT_MODEL
    openai_image_model: str = DEFAULT_IMAGE_MODEL
    gemini_api_key: str | None = None
    gemini_model: str = DEFAULT_GEMINI_MODEL
    ai_concurrency: int = 3
    ai_user_token_budget: int = 40_000
    ai_global_token_budget: int = 4_000_000
    ai_budget_window: float = 3600.0
    ai_content_logging: bool = False
    ai_max_prompt_chars: int = 6000
    ai_max_attachments: int = 3
    ai_max_attachment_bytes: int = 8 * 1024 * 1024

    host: str = "0.0.0.0"
    port: int = 8080
    enable_keep_alive: bool = True

    sync_guild_id: int = 0
    log_level: str = "INFO"
    environment: str = "production"

    @classmethod
    def from_env(cls) -> BotConfig:
        """Build a configuration object from environment variables."""
        _load_env_file()
        data_dir = Path(get_env("KAI_DATA_DIR", ".") or ".").expanduser()
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            data_dir = Path(".")

        owners = get_id_set("BOT_OWNER_ID")
        owners |= get_id_set("BOT_OWNER_IDS")

        return cls(
            token=get_env("DISCORD_TOKEN") or get_env("TOKEN"),
            owner_ids=owners,
            log_channel_id=get_int("LOG_CHANNEL_ID", 0, minimum=0),
            log_db_path=_resolve_in_data_dir(
                get_env("LOG_DB_PATH", "") or "", data_dir, "kai_logs.db"
            ),
            log_retention_days=get_int("LOG_RETENTION_DAYS", 30, minimum=1, maximum=3650),
            log_message_content=get_bool("LOG_MESSAGE_CONTENT", False),
            log_staff_role_id=get_int("LOG_STAFF_ROLE_ID", 0, minimum=0),
            log_staff_role_name=get_env("LOG_STAFF_ROLE_NAME", "Staff") or "Staff",
            data_dir=data_dir,
            prefixes=get_prefixes(),
            openai_api_key=get_env("OPENAI_API_KEY"),
            openai_model=get_env("OPENAI_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
            openai_image_model=get_env("OPENAI_IMAGE_MODEL", DEFAULT_IMAGE_MODEL) or DEFAULT_IMAGE_MODEL,
            gemini_api_key=get_env("GEMINI_API_KEY"),
            gemini_model=get_env("GEMINI_MODEL", DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL,
            ai_concurrency=get_int("AI_CONCURRENCY", 3, minimum=1, maximum=8),
            ai_user_token_budget=get_int("AI_USER_TOKEN_BUDGET", 40_000, minimum=1000),
            ai_global_token_budget=get_int("AI_GLOBAL_TOKEN_BUDGET", 4_000_000, minimum=10_000),
            ai_budget_window=get_float("AI_BUDGET_WINDOW", 3600.0, minimum=60.0),
            ai_content_logging=get_bool("AI_CONTENT_LOGGING", False),
            ai_max_prompt_chars=get_int("AI_MAX_PROMPT_CHARS", 6000, minimum=200),
            ai_max_attachments=get_int("AI_MAX_ATTACHMENTS", 3, minimum=0, maximum=10),
            ai_max_attachment_bytes=get_int(
                "AI_MAX_ATTACHMENT_BYTES", 8 * 1024 * 1024, minimum=64 * 1024
            ),
            host=get_env("HOST", "0.0.0.0") or "0.0.0.0",
            port=get_int("PORT", 8080, minimum=1, maximum=65535),
            enable_keep_alive=get_bool("ENABLE_KEEP_ALIVE", True),
            sync_guild_id=get_int("SYNC_GUILD_ID", 0, minimum=0),
            log_level=(get_env("LOG_LEVEL", "INFO") or "INFO").upper(),
            environment=get_env("ENVIRONMENT", "production") or "production",
        )

    @property
    def is_configured(self) -> bool:
        """True when the minimum required secrets are present."""
        return bool(self.token)

    def is_owner(self, user_id: int) -> bool:
        """Return True when ``user_id`` is a configured bot owner."""
        return user_id in self.owner_ids

    def validate(self) -> list[str]:
        """Return a list of human readable problems (empty list means all good)."""
        problems: list[str] = []
        if not self.token:
            problems.append("DISCORD_TOKEN chưa cấu hình (Secrets / Environment Variables).")
        if not self.openai_api_key:
            problems.append("OPENAI_API_KEY chưa cấu hình → các lệnh AI sẽ bị vô hiệu hoá.")
        if not self.owner_ids:
            problems.append(
                "BOT_OWNER_ID/BOT_OWNER_IDS chưa cấu hình → Discord application owner "
                "được dùng làm owner; hãy cấu hình rõ nếu cần thêm owner."
            )
        if self.environment == "production" and self.log_level == "DEBUG":
            problems.append("LOG_LEVEL=DEBUG trong production có thể làm log rất ồn (cảnh báo).")
        return problems


#: Process-wide configuration singleton.
CONFIG = BotConfig.from_env()

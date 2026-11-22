"""
KaiAI Core
==========

Shared, dependency-light foundation used by every KaiAI cog.

The package deliberately contains *no* discord.py ``Cog`` objects so it can never
be mistaken for a loadable extension by the cog loader.  It only provides:

* :mod:`kai_core.config`        - typed, validated environment configuration.
* :mod:`kai_core.logging_setup` - consistent console + rotating file logging.
* :mod:`kai_core.utils`         - text/number/UI helpers (no side effects).
* :mod:`kai_core.ui`            - reusable Discord views (buttons, confirm).
* :mod:`kai_core.database`      - a small async SQLite helper with WAL + retries.
* :mod:`kai_core.errors`        - safe error handling (never leak tracebacks).
* :mod:`kai_core.pagination`    - pagination engine + smart message splitter.
* :mod:`kai_core.parity`        - auto-generates slash mirrors for prefix commands.
* :mod:`kai_core.version`       - single source of truth for the version number.

Import order is intentionally flat so that ``import kai_core`` stays cheap.
"""

from __future__ import annotations

from .config import CONFIG, BotConfig, get_env, get_id_set
from .database import Database, DatabaseError
from .errors import (
    ERRORS,
    ErrorRecord,
    ErrorRecorder,
    friendly_message,
    redact,
)
from .logging_setup import get_logger, install_global_excepthook, setup_logging
from .pagination import PaginationView, build_embed, paginate_sequence, split_text
from .utils import (
    chunk_text,
    clamp,
    format_duration,
    human_number,
    paginate,
    truncate,
    utcnow,
)
from .version import APP_NAME, APP_SUMMARY, VERSION, version_string

__all__ = (
    "APP_NAME",
    "APP_SUMMARY",
    "VERSION",
    "version_string",
    "CONFIG",
    "BotConfig",
    "get_env",
    "get_id_set",
    "Database",
    "DatabaseError",
    "ERRORS",
    "ErrorRecord",
    "ErrorRecorder",
    "friendly_message",
    "redact",
    "get_logger",
    "setup_logging",
    "install_global_excepthook",
    "PaginationView",
    "build_embed",
    "paginate_sequence",
    "split_text",
    "chunk_text",
    "clamp",
    "format_duration",
    "human_number",
    "paginate",
    "truncate",
    "utcnow",
)

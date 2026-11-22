"""Consistent logging for KaiAI: coloured console output + rotating file log."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

_PREFIX = "kai"
_CONFIGURED = False
_DEFAULT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-24s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_LEVEL_COLORS = {
    "DEBUG": "\x1b[38;5;244m",
    "INFO": "\x1b[38;5;39m",
    "WARNING": "\x1b[38;5;214m",
    "ERROR": "\x1b[38;5;196m",
    "CRITICAL": "\x1b[48;5;196m\x1b[38;5;15m",
}
_RESET = "\x1b[0m"


class _ColorFormatter(logging.Formatter):
    """Formatter that adds ANSI colours to the level name when supported."""

    def __init__(self, fmt: str, datefmt: str, use_color: bool) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102 - inherited
        if not self.use_color:
            return super().format(record)
        color = _LEVEL_COLORS.get(record.levelname, "")
        original = record.levelname
        record.levelname = f"{color}{original:<8}{_RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original


def _supports_color(stream) -> bool:
    """Best-effort detection of ANSI support without extra dependencies."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.name != "nt":
        return hasattr(stream, "isatty") and stream.isatty()
    if "ANSICON" in os.environ or "WT_SESSION" in os.environ:
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def _ensure_utf8_stdio() -> None:
    """Ép stdout/stderr sang UTF-8 nếu đang dùng mã hoá khác (ví dụ cp1252 trên Windows).

    Nếu không làm vậy, mọi thông báo tiếng Việt in ra console sẽ làm handler của
    ``logging`` ném ``UnicodeEncodeError`` và làm mất log.  Hàm này "im lặng"
    thất bại: nó chỉ là lớp bảo vệ best-effort.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        encoding = (getattr(stream, "encoding", None) or "").lower()
        normalized = encoding.replace("-", "").replace("_", "")
        if normalized in {"utf8", "utf"}:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):  # pragma: no cover - stream chỉ đọc
            pass


def setup_logging(
    level: str = "INFO",
    log_dir: str | Path = ".",
    filename: str = "kai.log",
    max_bytes: int = 2_000_000,
    backups: int = 3,
) -> logging.Logger:
    """Configure the ``kai`` logger tree once and return the root KaiAI logger.

    Calling this function multiple times is safe: handlers are only attached the
    first time.  The file handler is best-effort — if the directory is not
    writable we simply keep console logging.
    """
    global _CONFIGURED
    _ensure_utf8_stdio()
    logger = logging.getLogger(_PREFIX)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    if _CONFIGURED:
        return logger

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(
        _ColorFormatter(_DEFAULT_FORMAT, _DATE_FORMAT, _supports_color(sys.stdout))
    )
    console.setLevel(logging.DEBUG)
    logger.addHandler(console)

    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            directory / filename,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT, _DATE_FORMAT))
        file_handler.setLevel(logging.INFO)
        logger.addHandler(file_handler)
    except OSError:
        logger.debug("Không tạo được file log trong %s; chỉ dùng console.", log_dir)

    # discord.py is chatty on DEBUG; keep it useful but not overwhelming.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.INFO)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    logger.propagate = False
    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a namespaced child logger (``kai.<name>``)."""
    if not name:
        return logging.getLogger(_PREFIX)
    if name.startswith(_PREFIX + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_PREFIX}.{name}")


def install_global_excepthook(logger: logging.Logger | None = None) -> None:
    """Forward otherwise-unhandled exceptions to the logger."""
    log = logger or get_logger("fatal")

    def _hook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        try:
            log.critical("Lỗi không được xử lý", exc_info=(exc_type, exc_value, exc_tb))
        except Exception:  # pragma: no cover - không bao giờ được che mất lỗi gốc
            sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook

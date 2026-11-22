"""Pure-python helpers shared across KaiAI.

This module intentionally has **no third-party imports** so it can be unit
tested without discord.py installed.  UI helpers live in :mod:`kai_core.ui`.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Iterable, Iterator, Sequence
from typing import TypeVar

T = TypeVar("T")

_WHITESPACE_RE = re.compile(r"[ \t]+")
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def utcnow() -> dt.datetime:
    """Timezone-aware "now" in UTC (replaces the deprecated ``utcnow``)."""
    return dt.datetime.now(dt.timezone.utc)


def clamp(value: int | float, low: int | float, high: int | float) -> int | float:
    """Clamp ``value`` into the inclusive ``[low, high]`` range."""
    return max(low, min(high, value))


def truncate(text: str, limit: int = 1024, suffix: str = "…") -> str:
    """Trim ``text`` so that ``len(result) <= limit`` including the suffix."""
    text = "" if text is None else str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if len(suffix) >= limit:
        return text[:limit]
    return text[: limit - len(suffix)] + suffix


def chunk_text(text: str, limit: int = 1900, *, preserve_newlines: bool = True) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters.

    Newlines are respected first so code blocks and bullet lists stay readable.
    Text without newlines is hard-split as a fallback.  An empty or blank string
    yields an empty list.
    """
    text = "" if text is None else str(text)
    if not text.strip():
        return []
    if limit <= 0:
        raise ValueError("limit must be positive")

    chunks: list[str] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            chunks.append(buffer)
        buffer = ""

    segments = text.split("\n") if preserve_newlines else [text]
    for segment in segments:
        candidate = f"{buffer}\n{segment}" if buffer else segment
        if len(candidate) <= limit:
            buffer = candidate
            continue
        flush()
        while len(segment) > limit:
            chunks.append(segment[:limit])
            segment = segment[limit:]
        buffer = segment
    flush()
    return chunks or [truncate(text, limit)]


def paginate(items: Sequence[T], per_page: int) -> list[list[T]]:
    """Split ``items`` into consecutive pages of ``per_page`` elements."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    return [list(items[i : i + per_page]) for i in range(0, len(items), per_page)] or [[]]


def iter_pages(items: Iterable[T], per_page: int) -> Iterator[list[T]]:
    """Lazy variant of :func:`paginate`."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    page: list[T] = []
    for item in items:
        page.append(item)
        if len(page) == per_page:
            yield page
            page = []
    if page:
        yield page


def human_number(value: int | float) -> str:
    """Format large integers compactly (``1_250_000 -> 1.25M``)."""
    number = float(value)
    sign = "-" if number < 0 else ""
    number = abs(number)
    for threshold, suffix in (
        (1_000_000_000_000, "T"),
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ):
        if number >= threshold:
            scaled = number / threshold
            decimals = 0 if scaled >= 100 else 1 if scaled >= 10 else 2
            return f"{sign}{scaled:.{decimals}f}{suffix}"
    return f"{sign}{int(number):,}"


def format_duration(seconds: float, *, long: bool = False) -> str:
    """Turn a number of seconds into a compact human readable string."""
    seconds = int(max(0, seconds))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)

    if long:
        parts: list[str] = []
        if days:
            parts.append(f"{days} ngày")
        if hours:
            parts.append(f"{hours} giờ")
        if minutes:
            parts.append(f"{minutes} phút")
        if seconds or not parts:
            parts.append(f"{seconds} giây")
        return " ".join(parts[:3])

    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def format_timestamp(moment: dt.datetime, style: str = "R") -> str:
    """Return a Discord timestamp markdown for ``moment``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return f"<t:{int(moment.timestamp())}:{style}>"


def parse_hex_color(value: str) -> int:
    """Validate and convert ``#RRGGBB`` into a decimal integer.

    Raises :class:`ValueError` with a user-friendly Vietnamese message.
    """
    match = _HEX_RE.match((value or "").strip())
    if not match:
        raise ValueError("Màu phải ở dạng HEX 6 ký tự, ví dụ `#5865F2`.")
    return int(match.group(1), 16)


def normalize_whitespace(text: str) -> str:
    """Collapse runs of spaces/tabs and trim each line."""
    return "\n".join(
        _WHITESPACE_RE.sub(" ", line).strip() for line in str(text).splitlines()
    ).strip()


def unique(iterable: Iterable[T]) -> list[T]:
    """Return a de-duplicated list preserving first-seen order."""
    seen = set()
    result: list[T] = []
    for item in iterable:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """Return ``"1 item"`` / ``"3 items"`` style pluralisation."""
    word = singular if abs(count) == 1 else (plural or f"{singular}s")
    return f"{count} {word}"


def percentage(part: float, whole: float, digits: int = 1) -> str:
    """Safe percentage string (never raises on a zero denominator)."""
    if not whole:
        return f"0.{'0' * digits}%"
    return f"{(100.0 * part / whole):.{digits}f}%"


def is_effectively_int(value: float) -> bool:
    """True when a float carries no fractional part (used for calculator output)."""
    return math.isfinite(value) and float(value).is_integer()


def bar_gauge(part: float, whole: float, width: int = 12) -> str:
    """Return a ``████░░░░`` style gauge string for progress display."""
    width = max(1, int(width))
    ratio = 0.0 if not whole else max(0.0, min(1.0, float(part) / float(whole)))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


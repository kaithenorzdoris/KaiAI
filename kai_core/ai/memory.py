"""Bộ nhớ AI lưu trong database — không mất khi bot restart (yêu cầu #38).

Ba tầng nhớ
------------
* **Short-term** — N lượt gần nhất, giữ trong bảng ``ai_sessions``.
* **Long-term** — tóm tắt dài hạn của cuộc trò chuyện, nối vào mọi prompt sau.
* **Nén lịch sử** — khi lịch sử dài, tóm tắt bằng chính AI rồi giữ lại vài lượt
  gần nhất để tiết kiệm token.

Bảng ``ai_sessions`` được tạo bởi migration ``002_ai_memory``; hàm
:meth:`ConversationMemory.setup` vẫn tự tạo nếu migration chưa chạy.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger
from .provider import ChatMessage, normalize_history

log = get_logger("ai.memory")

DEFAULT_TURNS = 10
DEFAULT_SUMMARY_TRIGGER = 24
SUMMARY_KEEP_RECENT = 6


@dataclass(slots=True)
class MemorySnapshot:
    """Ảnh chụp bộ nhớ của một người dùng tại thời điểm đọc."""

    history: list[ChatMessage]
    summary: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    updated_at: float = 0.0


class ConversationMemory:
    """Bộ nhớ hội thoại theo ``(user_id, guild_id)``, lưu SQLite."""

    def __init__(
        self,
        db: Any,
        *,
        max_turns: int = DEFAULT_TURNS,
        summary_trigger: int = DEFAULT_SUMMARY_TRIGGER,
    ) -> None:
        self.db = db
        self.max_turns = int(max_turns)
        self.summary_trigger = int(summary_trigger)

    async def setup(self) -> None:
        """Đảm bảo bảng tồn tại (an toàn nếu migration chưa chạy)."""
        await self.db.execute(
            """CREATE TABLE IF NOT EXISTS ai_sessions (
                user_id      INTEGER NOT NULL,
                guild_id     INTEGER NOT NULL,
                messages     TEXT    NOT NULL DEFAULT '[]',
                tokens_in    INTEGER NOT NULL DEFAULT 0,
                tokens_out   INTEGER NOT NULL DEFAULT 0,
                summary      TEXT,
                updated_at   REAL    NOT NULL,
                PRIMARY KEY (user_id, guild_id)
            )"""
        )
        await self.db.commit()

    async def load(self, user_id: int, guild_id: int = 0) -> MemorySnapshot:
        """Đọc lịch sử + tóm tắt (không ném lỗi ra ngoài)."""
        await self.setup()
        try:
            cursor = await self.db.execute(
                "SELECT messages, summary, tokens_in, tokens_out, updated_at"
                " FROM ai_sessions WHERE user_id = ? AND guild_id = ?",
                (user_id, guild_id),
            )
            row = await cursor.fetchone()
        except Exception as exc:  # pragma: no cover - DB hỏng
            log.error("Không đọc được bộ nhớ AI: %s", exc)
            return MemorySnapshot(history=[])

        if row is None:
            return MemorySnapshot(history=[])
        try:
            raw = json.loads(row[0] or "[]")
        except (TypeError, ValueError):
            raw = []
        history = [
            ChatMessage(role=str(item.get("role", "user")), content=str(item.get("content", "")))
            for item in raw
            if isinstance(item, dict) and item.get("content")
        ]
        return MemorySnapshot(
            history=history,
            summary=row[1] or "",
            tokens_in=int(row[2] or 0),
            tokens_out=int(row[3] or 0),
            updated_at=float(row[4] or 0.0),
        )

    async def recent(self, user_id: int, guild_id: int = 0) -> list[ChatMessage]:
        """Chỉ lấy N lượt gần nhất để đưa vào prompt."""
        snapshot = await self.load(user_id, guild_id)
        return normalize_history(snapshot.history, self.max_turns)

    async def summary(self, user_id: int, guild_id: int = 0) -> str:
        """Lấy tóm tắt dài hạn (rỗng nếu chưa có)."""
        return (await self.load(user_id, guild_id)).summary

    async def _write(
        self,
        user_id: int,
        guild_id: int,
        snapshot: MemorySnapshot,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        payload = json.dumps([m.to_dict() for m in snapshot.history], ensure_ascii=False)
        await self.db.execute(
            """INSERT INTO ai_sessions
                   (user_id, guild_id, messages, tokens_in, tokens_out, summary, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, guild_id) DO UPDATE SET
                   messages   = excluded.messages,
                   tokens_in  = ai_sessions.tokens_in + excluded.tokens_in,
                   tokens_out = ai_sessions.tokens_out + excluded.tokens_out,
                   summary    = COALESCE(excluded.summary, ai_sessions.summary),
                   updated_at = excluded.updated_at""",
            (
                user_id,
                guild_id,
                payload,
                tokens_in,
                tokens_out,
                snapshot.summary or None,
                time.time(),
            ),
        )
        await self.db.commit()

    async def append(
        self,
        user_id: int,
        guild_id: int,
        role: str,
        content: str,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Thêm một lượt vào lịch sử (giữ tối đa ``max_turns * 3`` để nén sau)."""
        await self.setup()
        snapshot = await self.load(user_id, guild_id)
        snapshot.history.append(ChatMessage(role=role, content=content))
        snapshot.history = normalize_history(snapshot.history, self.max_turns * 3)
        await self._write(user_id, guild_id, snapshot, tokens_in, tokens_out)

    async def remember_summary(self, user_id: int, guild_id: int, summary: str) -> None:
        """Ghi tóm tắt dài hạn mà không đụng lịch sử."""
        await self.setup()
        await self.db.execute(
            """INSERT INTO ai_sessions (user_id, guild_id, messages, summary, updated_at)
               VALUES (?, ?, '[]', ?, ?)
               ON CONFLICT(user_id, guild_id) DO UPDATE SET
                   summary    = excluded.summary,
                   updated_at = excluded.updated_at""",
            (user_id, guild_id, summary, time.time()),
        )
        await self.db.commit()

    async def compact(
        self,
        user_id: int,
        guild_id: int,
        summarizer: Callable[[str], Any],
    ) -> str | None:
        """Nén lịch sử dài bằng coroutine ``summarizer(str) -> str``.

        Trả về tóm tắt mới, hoặc ``None`` nếu chưa đủ ngưỡng nén.
        """
        snapshot = await self.load(user_id, guild_id)
        if len(snapshot.history) < self.summary_trigger:
            return None
        try:
            summary = await summarizer(snapshot.summary)
        except Exception as exc:  # noqa: BLE001 - callback bên ngoài
            log.warning("Không tóm tắt được bộ nhớ: %s", exc)
            return None
        if not summary:
            return None
        snapshot.history = normalize_history(snapshot.history, SUMMARY_KEEP_RECENT)
        snapshot.summary = summary
        await self._write(user_id, guild_id, snapshot)
        return summary

    async def clear(self, user_id: int, guild_id: int = 0) -> None:
        """Xoá toàn bộ bộ nhớ của một người dùng (lệnh ``!clearai``)."""
        await self.setup()
        await self.db.execute(
            "DELETE FROM ai_sessions WHERE user_id = ? AND guild_id = ?", (user_id, guild_id)
        )
        await self.db.commit()

    async def stats(self, user_id: int, guild_id: int = 0) -> dict[str, int]:
        """Thống kê nhẹ cho ``/diagnostics``."""
        snapshot = await self.load(user_id, guild_id)
        return {
            "messages": len(snapshot.history),
            "tokens_in": snapshot.tokens_in,
            "tokens_out": snapshot.tokens_out,
        }


__all__ = ("ConversationMemory", "MemorySnapshot", "DEFAULT_TURNS", "DEFAULT_SUMMARY_TRIGGER")

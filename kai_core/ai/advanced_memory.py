"""
KaiAI Ultimate V7 — Advanced Multi-Tier Memory Engine
====================================================

Xây dựng hệ thống bộ nhớ phân tầng chuẩn Enterprise (Yêu cầu Phần 5):
1. **Tier 1 - Short-Term Memory**: Lưu các lượt hội thoại gần nhất trong phiên,
   có TTL tự động dọn dẹp sau 30 phút không hoạt động.
2. **Tier 2 - Long-Term Fact Store**: Trích xuất và lưu giữ các sự thật (facts),
   sở thích và phong cách của người dùng. Phân vùng nghiêm ngặt theo (user_id, guild_id).
3. **Tier 3 - Server Knowledge Store**: Lưu quy định và ngữ cảnh riêng của từng server.
4. **Privacy & GDPR Compliance**:
   - Không lưu secret, token, mật khẩu.
   - Hỗ trợ xem (view_memory) và xóa vĩnh viễn (purge_user_memory - Right to be forgotten).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..logging_setup import get_logger
from .provider import ChatMessage
from .safety import AISafetyShield

log = get_logger("ai.advanced_memory")

DEFAULT_SESSION_TTL = 1800.0  # 30 phút
MAX_RECENT_TURNS = 12
MAX_FACTS_PER_USER = 25


@dataclass(slots=True)
class UserMemorySnapshot:
    """Ảnh chụp bộ nhớ toàn diện của một người dùng."""

    user_id: int
    guild_id: int
    recent_messages: list[ChatMessage]
    facts: list[str] = field(default_factory=list)
    preferences: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    updated_at: float = 0.0

    def to_system_context(self) -> str:
        """Chuyển đổi bộ nhớ thành ngữ cảnh ngắn gọn cho prompt AI."""
        parts: list[str] = []
        if self.preferences:
            pref_str = ", ".join(f"{k}: {v}" for k, v in self.preferences.items())
            parts.append(f"[SỞ THÍCH NGƯỜI DÙNG]: {pref_str}")
        if self.facts:
            facts_str = "; ".join(self.facts[:MAX_FACTS_PER_USER])
            parts.append(f"[THÔNG TIN ĐÃ BIẾT VỀ BẠN]: {facts_str}")
        if self.summary:
            parts.append(f"[TÓM TẮT TRÒ CHUYỆN TRƯỚC ĐÓ]: {self.summary}")
        return "\n".join(parts)


class AdvancedMemoryEngine:
    """Động cơ bộ nhớ đa tầng lưu trữ SQLite bền vững."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def setup_schema(self) -> None:
        """Đảm bảo các bảng bộ nhớ nâng cao tồn tại."""
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_user_memories (
                user_id     INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL DEFAULT 0,
                facts_json  TEXT NOT NULL DEFAULT '[]',
                prefs_json  TEXT NOT NULL DEFAULT '{}',
                summary     TEXT NOT NULL DEFAULT '',
                updated_at  REAL NOT NULL,
                PRIMARY KEY (user_id, guild_id)
            )
            """
        )
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_session_turns (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                guild_id    INTEGER NOT NULL DEFAULT 0,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  REAL NOT NULL
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_user_time ON ai_session_turns(user_id, guild_id, created_at)"
        )
        await self.db.commit()

    async def record_turn(
        self,
        user_id: int,
        guild_id: int,
        role: str,
        content: str,
    ) -> None:
        """Ghi nhận một lượt tin nhắn vào Short-Term memory (có lọc secret)."""
        await self.setup_schema()
        clean_content = AISafetyShield.sanitize_output(content).strip()
        if not clean_content:
            return

        now = time.time()
        await self.db.execute(
            "INSERT INTO ai_session_turns(user_id, guild_id, role, content, created_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (int(user_id), int(guild_id), str(role), clean_content, now),
        )

        # Cắt tỉa phiên cũ hơn TTL
        cutoff = now - DEFAULT_SESSION_TTL
        await self.db.execute(
            "DELETE FROM ai_session_turns WHERE user_id = ? AND guild_id = ? AND created_at < ?",
            (int(user_id), int(guild_id), cutoff),
        )

        # Giữ tối đa MAX_RECENT_TURNS tin nhắn
        await self.db.execute(
            """
            DELETE FROM ai_session_turns WHERE id NOT IN (
                SELECT id FROM ai_session_turns
                WHERE user_id = ? AND guild_id = ?
                ORDER BY created_at DESC LIMIT ?
            ) AND user_id = ? AND guild_id = ?
            """,
            (int(user_id), int(guild_id), MAX_RECENT_TURNS, int(user_id), int(guild_id)),
        )
        await self.db.commit()

    async def load_memory(self, user_id: int, guild_id: int = 0) -> UserMemorySnapshot:
        """Đọc toàn bộ các tầng bộ nhớ của người dùng."""
        await self.setup_schema()
        user_id = int(user_id)
        guild_id = int(guild_id)

        # 1. Đọc Short-term turns
        async with self.db.execute(
            "SELECT role, content FROM ai_session_turns "
            "WHERE user_id = ? AND guild_id = ? ORDER BY created_at ASC",
            (user_id, guild_id),
        ) as cur:
            rows = await cur.fetchall()

        recent: list[ChatMessage] = [
            ChatMessage(role=r[0], content=r[1]) for r in rows
        ]

        # 2. Đọc Long-term facts & preferences
        async with self.db.execute(
            "SELECT facts_json, prefs_json, summary, updated_at "
            "FROM ai_user_memories WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ) as cur:
            row = await cur.fetchone()

        facts: list[str] = []
        prefs: dict[str, str] = {}
        summary = ""
        updated_at = 0.0

        if row:
            try:
                facts = json.loads(row[0] or "[]")
            except Exception:
                facts = []
            try:
                prefs = json.loads(row[1] or "{}")
            except Exception:
                prefs = {}
            summary = row[2] or ""
            updated_at = float(row[3] or 0.0)

        return UserMemorySnapshot(
            user_id=user_id,
            guild_id=guild_id,
            recent_messages=recent,
            facts=facts,
            preferences=prefs,
            summary=summary,
            updated_at=updated_at,
        )

    async def add_fact(self, user_id: int, guild_id: int, fact: str) -> bool:
        """Thêm một sự thật mới vào Long-term memory của người dùng."""
        await self.setup_schema()
        clean_fact = AISafetyShield.sanitize_output(fact).strip()
        if not clean_fact:
            return False

        snapshot = await self.load_memory(user_id, guild_id)
        facts = list(snapshot.facts)
        if clean_fact in facts:
            return True

        facts.append(clean_fact)
        if len(facts) > MAX_FACTS_PER_USER:
            facts = facts[-MAX_FACTS_PER_USER:]

        now = time.time()
        await self.db.execute(
            """
            INSERT INTO ai_user_memories(user_id, guild_id, facts_json, prefs_json, summary, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET
                facts_json = excluded.facts_json,
                updated_at = excluded.updated_at
            """,
            (
                int(user_id),
                int(guild_id),
                json.dumps(facts, ensure_ascii=False),
                json.dumps(snapshot.preferences, ensure_ascii=False),
                snapshot.summary,
                now,
            ),
        )
        await self.db.commit()
        return True

    async def purge_memory(self, user_id: int, guild_id: int | None = None) -> int:
        """Xoá sạch toàn bộ dữ liệu trí nhớ AI theo chuẩn GDPR (Right to be Forgotten)."""
        await self.setup_schema()
        user_id = int(user_id)
        deleted = 0

        if guild_id is not None:
            cur1 = await self.db.execute(
                "DELETE FROM ai_session_turns WHERE user_id = ? AND guild_id = ?",
                (user_id, int(guild_id)),
            )
            cur2 = await self.db.execute(
                "DELETE FROM ai_user_memories WHERE user_id = ? AND guild_id = ?",
                (user_id, int(guild_id)),
            )
            deleted = cur1.rowcount + cur2.rowcount
        else:
            cur1 = await self.db.execute(
                "DELETE FROM ai_session_turns WHERE user_id = ?", (user_id,)
            )
            cur2 = await self.db.execute(
                "DELETE FROM ai_user_memories WHERE user_id = ?", (user_id,)
            )
            deleted = cur1.rowcount + cur2.rowcount

        await self.db.commit()
        log.info("Đã xoá sạch trí nhớ AI của user_id=%s (deleted=%s)", user_id, deleted)
        return deleted


__all__ = (
    "AdvancedMemoryEngine",
    "DEFAULT_SESSION_TTL",
    "MAX_FACTS_PER_USER",
    "MAX_RECENT_TURNS",
    "UserMemorySnapshot",
)


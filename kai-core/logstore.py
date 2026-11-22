"""Log Store — schema database riêng cho hệ thống log (#16, #17, #18, #20, #22, #23).

Vì sao không nhét tất cả vào một bảng
------------------------------------
Mỗi loại log có vòng đời, giữ dữ liệu và chính sách riêng:

==================== ==================== =================================
Bảng                 Giữ                 Ghi chú
==================== ==================== =================================
``audit_logs``       audit log Discord   không bao giờ xoá tự động
``message_logs``     create/edit/delete  theo ``LOG_RETENTION_DAYS``
``moderation_logs``  ban/kick/warn…     có **Case ID** tra cứu được
``command_logs``     mỗi lần dùng lệnh  có redact secret
``economy_logs``     mọi giao dịch tiền  balance trước/sau + reason
``ai_logs``          metadata AI         **không** lưu nội dung hội thoại
==================== ==================== =================================

Bảo mật & riêng tư (#21)
-------------------------
* Mọi nội dung đi qua :func:`kai_core.errors.redact` trước khi ghi.
* ``ai_logs`` chỉ lưu metadata; nội dung hội thoại chỉ lưu khi
  ``AI_CONTENT_LOGGING=true``.

Case ID (#23)
-------------
Mỗi hành động moderation sinh mã ``CASE-000001`` (đếm riêng theo server),
tra cứu bằng ``!case <id>`` / ``/case id:<id>``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import redact
from .logging_setup import get_logger

log = get_logger("logstore")

DEFAULT_RETENTION_DAYS = 30

#: Schema chuẩn của hệ thống log.
LOG_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS audit_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id    INTEGER NOT NULL,
        action      TEXT    NOT NULL,
        actor_id    INTEGER,
        actor_name  TEXT,
        target_id   INTEGER,
        target_name TEXT,
        reason      TEXT,
        extra       TEXT,
        created_at  REAL    NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_audit_guild_time
       ON audit_logs(guild_id, created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS message_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id    INTEGER NOT NULL,
        channel_id  INTEGER NOT NULL,
        message_id  INTEGER,
        author_id   INTEGER,
        author_name TEXT,
        event       TEXT    NOT NULL,
        content     TEXT,
        attachments TEXT,
        jump_url    TEXT,
        reply_to    INTEGER,
        created_at  REAL    NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_msglog_guild_time
       ON message_logs(guild_id, created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_msglog_channel_time
       ON message_logs(channel_id, created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_msglog_message
       ON message_logs(message_id)""",
    """CREATE TABLE IF NOT EXISTS moderation_logs (
        case_id        TEXT    PRIMARY KEY,
        guild_id       INTEGER NOT NULL,
        channel_id     INTEGER,
        moderator_id   INTEGER,
        moderator_name TEXT,
        target_id      INTEGER,
        target_name    TEXT,
        action         TEXT    NOT NULL,
        reason         TEXT,
        duration       TEXT,
        created_at     REAL    NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_modlog_guild_time
       ON moderation_logs(guild_id, created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_modlog_target
       ON moderation_logs(guild_id, target_id)""",
    """CREATE TABLE IF NOT EXISTS case_counters (
        guild_id   INTEGER PRIMARY KEY,
        last_value INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS command_logs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id      INTEGER,
        channel_id    INTEGER,
        user_id       INTEGER NOT NULL,
        command       TEXT    NOT NULL,
        surface       TEXT    NOT NULL,
        args_redacted TEXT,
        success       INTEGER NOT NULL DEFAULT 1,
        duration_ms   INTEGER,
        created_at    REAL    NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_cmdlog_user_time
       ON command_logs(user_id, created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_cmdlog_guild_time
       ON command_logs(guild_id, created_at DESC)""",
    """CREATE TABLE IF NOT EXISTS economy_logs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        transaction_id TEXT    NOT NULL,
        guild_id       INTEGER,
        user_id        INTEGER NOT NULL,
        kind           TEXT    NOT NULL,
        amount         INTEGER NOT NULL,
        balance_before INTEGER NOT NULL,
        balance_after  INTEGER NOT NULL,
        reason         TEXT,
        created_at     REAL    NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_econlog_user_time
       ON economy_logs(user_id, created_at DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_econlog_txn
       ON economy_logs(transaction_id)""",
    """CREATE TABLE IF NOT EXISTS ai_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        guild_id   INTEGER,
        command    TEXT,
        provider   TEXT,
        model      TEXT,
        surface    TEXT,
        latency_ms INTEGER,
        tokens_in  INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        success    INTEGER NOT NULL DEFAULT 1,
        error      TEXT,
        created_at REAL    NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_ailog_user_time
       ON ai_logs(user_id, created_at DESC)""",
)

#: Các bảng bị xoá theo chính sách retention (audit log loại trừ).
RETENTION_TABLES = ("message_logs", "moderation_logs", "command_logs", "economy_logs", "ai_logs")


@dataclass(slots=True)
class CaseRecord:
    """Một hồ sơ moderation tra cứu được bằng Case ID."""

    case_id: str
    action: str
    guild_id: int
    moderator_name: str = ""
    target_name: str = ""
    reason: str = ""
    duration: str = ""
    created_at: float = 0.0

    def summary(self) -> str:
        stamp = time.strftime("%d/%m/%Y %H:%M", time.localtime(self.created_at or time.time()))
        lines = [
            f"**Case** `{self.case_id}` — **{self.action.upper()}**",
            f"👮 Moderator: {self.moderator_name or '—'}",
            f"🎯 Đối tượng: {self.target_name or '—'}",
            f"⏱️ Thời gian: {stamp}",
        ]
        if self.reason:
            lines.append(f"📄 Lý do: {self.reason}")
        if self.duration:
            lines.append(f"⏳ Thời hạn: {self.duration}")
        return "\n".join(lines)


@dataclass(slots=True)
class LogStats:
    """Thống kê nhanh cho ``/diagnostics logs``."""

    counts: dict[str, int] = field(default_factory=dict)

    def total(self) -> int:
        return sum(self.counts.values())


class LogStore:
    """Ghi/đọc/xoá mọi loại log; mọi thao tác đều **không ném lỗi ra ngoài**."""

    def __init__(self, db: Any, *, retention_days: int = DEFAULT_RETENTION_DAYS,
                 ai_content_logging: bool = False) -> None:
        self.db = db
        self.retention_days = max(1, int(retention_days))
        self.ai_content_logging = bool(ai_content_logging)

    # -- schema -----------------------------------------------------------
    async def setup(self) -> None:
        """Tạo bảng + index nếu chưa có (an toàn khi migration chưa chạy)."""
        for statement in LOG_SCHEMA:
            await self.db.execute(statement)
        await self.db.commit()

    async def _run(self, sql: str, params: tuple = ()) -> Any | None:
        """Thực thi SQL, nuốt lỗi để log không bao giờ làm sập bot."""
        try:
            cursor = await self.db.execute(sql, params)
            await self.db.commit()
            return cursor
        except Exception as exc:  # noqa: BLE001 - lỗi log không được lan truyền
            log.error("LogStore lỗi: %s", exc)
            return None

    # -- audit log (#14, #23) ---------------------------------------------
    async def log_audit(
        self,
        guild_id: int,
        action: str,
        *,
        actor_id: int | None = None,
        actor_name: str = "",
        target_id: int | None = None,
        target_name: str = "",
        reason: str = "",
        extra: dict | None = None,
    ) -> None:
        """Ghi một dòng audit log (không bao giờ tự xoá)."""
        await self._run(
            "INSERT INTO audit_logs (guild_id, action, actor_id, actor_name, target_id,"
            " target_name, reason, extra, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                guild_id, action, actor_id, str(actor_name)[:120], target_id,
                str(target_name)[:120], redact(reason)[:500],
                json.dumps(extra, ensure_ascii=False)[:2000] if extra else None,
                time.time(),
            ),
        )

    # -- message log (#10, #11, #12) --------------------------------------
    async def log_message(
        self,
        *,
        guild_id: int,
        channel_id: int,
        event: str,
        message_id: int | None = None,
        author_id: int | None = None,
        author_name: str = "",
        content: str = "",
        attachments: list | None = None,
        jump_url: str = "",
        reply_to: int | None = None,
    ) -> None:
        """Ghi message create / edit / delete."""
        await self._run(
            "INSERT INTO message_logs (guild_id, channel_id, message_id, author_id, author_name,"
            " event, content, attachments, jump_url, reply_to, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                guild_id, channel_id, message_id, author_id, str(author_name)[:120],
                event, redact(content)[:4000],
                json.dumps(attachments, ensure_ascii=False)[:4000] if attachments else None,
                str(jump_url)[:500], reply_to, time.time(),
            ),
        )

    # -- moderation + case id (#15, #23) ----------------------------------
    async def next_case_id(self, guild_id: int) -> str:
        """Sinh Case ID dạng ``CASE-000001``, đếm riêng theo server.

        ⚠️ Phải là **một** câu lệnh nguyên tử. Trước đây INSERT → UPDATE →
        SELECT chạy ở 3 câu lệnh rời nhau; hai hành động moderation chạy song
        song sẽ cùng đọc ``last_value`` giống nhau và sinh **trùng Case ID**.

        Vì sao không dùng ``BEGIN IMMEDIATE``: ``sqlite3`` mặc định tự mở
        transaction ngầm ở câu DML đầu tiên, nên ``BEGIN IMMEDIATE`` tiếp theo sẽ
        ném ``cannot start a transaction within a transaction``. Câu UPSERT +
        ``RETURNING`` dưới đây vừa nguyên tử ở mức statement, vừa không cần
        quản lý transaction thủ công.
        """
        try:
            cursor = await self.db.execute(
                "INSERT INTO case_counters (guild_id, last_value) VALUES (?, 1)"
                " ON CONFLICT(guild_id) DO UPDATE SET last_value = last_value + 1"
                " RETURNING last_value",
                (guild_id,),
            )
            row = await cursor.fetchone()
            await self.db.commit()
        except Exception:
            try:
                await self.db.rollback()
            except Exception:  # pragma: no cover
                pass
            log.exception("Không sinh được Case ID cho guild %s", guild_id)
            raise
        return f"CASE-{int(row[0]) if row else 1:06d}"

    async def log_moderation(
        self,
        *,
        guild_id: int,
        action: str,
        moderator_id: int | None = None,
        moderator_name: str = "",
        target_id: int | None = None,
        target_name: str = "",
        reason: str = "",
        duration: str = "",
        channel_id: int | None = None,
        case_id: str | None = None,
    ) -> str:
        """Ghi hành động moderation và trả về Case ID để hiển thị."""
        case = case_id or await self.next_case_id(guild_id)
        await self._run(
            "INSERT OR REPLACE INTO moderation_logs (case_id, guild_id, channel_id, moderator_id,"
            " moderator_name, target_id, target_name, action, reason, duration, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                case, guild_id, channel_id, moderator_id, str(moderator_name)[:120],
                target_id, str(target_name)[:120], action, redact(reason)[:500],
                str(duration)[:64], time.time(),
            ),
        )
        return case

    async def get_case(self, case_id: str) -> CaseRecord | None:
        """Tra cứu hồ sơ moderation theo Case ID."""
        try:
            cursor = await self.db.execute(
                "SELECT case_id, action, guild_id, moderator_name, target_name, reason,"
                " duration, created_at FROM moderation_logs WHERE case_id = ?",
                (case_id,),
            )
            row = await cursor.fetchone()
        except Exception as exc:  # pragma: no cover
            log.error("Tra cứu case lỗi: %s", exc)
            return None
        if row is None:
            return None
        return CaseRecord(
            case_id=row[0], action=row[1], guild_id=int(row[2] or 0),
            moderator_name=row[3] or "", target_name=row[4] or "",
            reason=row[5] or "", duration=row[6] or "", created_at=float(row[7] or 0.0),
        )

    # -- command log (#16) --------------------------------------------------
    async def log_command(
        self,
        *,
        user_id: int,
        command: str,
        surface: str = "prefix",
        guild_id: int | None = None,
        channel_id: int | None = None,
        args: str = "",
        success: bool = True,
        duration_ms: int | None = None,
    ) -> None:
        """Ghi mỗi lần dùng lệnh; ``args`` luôn được redact trước khi lưu."""
        await self._run(
            "INSERT INTO command_logs (guild_id, channel_id, user_id, command, surface,"
            " args_redacted, success, duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                guild_id, channel_id, user_id, str(command)[:80], surface,
                redact(str(args))[:800], 1 if success else 0, duration_ms, time.time(),
            ),
        )

    # -- economy log (#18) --------------------------------------------------
    async def log_economy(
        self,
        *,
        transaction_id: str,
        user_id: int,
        kind: str,
        amount: int,
        balance_before: int,
        balance_after: int,
        guild_id: int | None = None,
        reason: str = "",
    ) -> None:
        """Ghi mọi biến động tiền (transfer/deposit/withdraw/payout…)."""
        await self._run(
            "INSERT INTO economy_logs (transaction_id, guild_id, user_id, kind, amount,"
            " balance_before, balance_after, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(transaction_id)[:64], guild_id, user_id, kind, int(amount),
                int(balance_before), int(balance_after), redact(reason)[:300], time.time(),
            ),
        )

    # -- AI log (#17) -------------------------------------------------------
    async def log_ai(
        self,
        *,
        user_id: int,
        provider: str = "",
        model: str = "",
        command: str = "",
        surface: str = "chat",
        guild_id: int | None = None,
        latency_ms: int | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        success: bool = True,
        error: str = "",
    ) -> None:
        """Ghi **metadata** AI — không lưu nội dung hội thoại (riêng tư #21)."""
        await self._run(
            "INSERT INTO ai_logs (user_id, guild_id, command, provider, model, surface,"
            " latency_ms, tokens_in, tokens_out, success, error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, guild_id, str(command)[:80], str(provider)[:32], str(model)[:64],
                surface, latency_ms, int(tokens_in), int(tokens_out),
                1 if success else 0, redact(error)[:300], time.time(),
            ),
        )

    # -- retention + thống kê (#20) ---------------------------------------
    async def purge_expired(self, days: int | None = None) -> dict[str, int]:
        """Xoá bản ghi cũ hơn ``days``. **Không** đụng ``audit_logs``."""
        window = self.retention_days if days is None else max(1, int(days))
        cutoff = time.time() - window * 86_400
        removed: dict[str, int] = {}
        for table in RETENTION_TABLES:
            try:
                cursor = await self.db.execute(
                    f"DELETE FROM {table} WHERE created_at < ?", (cutoff,)  # noqa: S608
                )
                removed[table] = cursor.rowcount or 0
            except Exception as exc:  # noqa: BLE001
                log.warning("Không dọn được %s: %s", table, exc)
                removed[table] = 0
        await self.db.commit()
        return removed

    async def stats(self, guild_id: int | None = None) -> LogStats:
        """Đếm số bản ghi của từng bảng (toàn bot nếu ``guild_id`` rỗng)."""
        stats = LogStats()
        for table in ("audit_logs", *RETENTION_TABLES):
            try:
                if guild_id:
                    cursor = await self.db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE guild_id = ?", (guild_id,)  # noqa: S608
                    )
                else:
                    cursor = await self.db.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
                row = await cursor.fetchone()
                stats.counts[table] = int(row[0]) if row else 0
            except Exception as exc:  # noqa: BLE001
                log.debug("Không đếm được %s: %s", table, exc)
                stats.counts[table] = 0
        return stats

    async def recent_moderation(self, guild_id: int, limit: int = 10) -> list[CaseRecord]:
        """Danh sách case moderation gần nhất của một server."""
        try:
            cursor = await self.db.execute(
                "SELECT case_id, action, guild_id, moderator_name, target_name, reason,"
                " duration, created_at FROM moderation_logs WHERE guild_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (guild_id, max(1, int(limit))),
            )
            rows = await cursor.fetchall()
        except Exception as exc:  # noqa: BLE001
            log.error("Không đọc được moderation log: %s", exc)
            return []
        return [
            CaseRecord(
                case_id=r[0], action=r[1], guild_id=int(r[2] or 0), moderator_name=r[3] or "",
                target_name=r[4] or "", reason=r[5] or "", duration=r[6] or "",
                created_at=float(r[7] or 0.0),
            )
            for r in rows
        ]


__all__ = (
    "CaseRecord",
    "DEFAULT_RETENTION_DAYS",
    "LOG_SCHEMA",
    "LogStats",
    "LogStore",
    "RETENTION_TABLES",
)
# __PART2__

"""Hệ thống migration cho database KaiAI (yêu cầu #53).

Vấn đề
------
KaiAI có **nhiều** file SQLite độc lập (``economy.db``, ``kai_core.db``,
``group_data.db``, ``aegis_system_v2.db`` …). Nếu mỗi lần thêm bảng lại sửa tay
``CREATE TABLE IF NOT EXISTS`` rải rác, rất dễ xảy ra tình trạng bot chạy trên
bản cũ thiếu bảng mới và ném lỗi giữa chừng.

Cách giải quyết
---------------
Mọi thay đổi schema được khai báo **một lần** dưới dạng :class:`Migration` có
số version tăng dần. :class:`Migrator` đọc bảng ``schema_migrations`` để biết
phiên bản hiện tại rồi chỉ chạy những migration còn thiếu, **theo thứ tự** và
**giao dịch (transaction)** từng bước.

Tính chất an toàn
-----------------
* **Không phá dữ liệu cũ**: migration chỉ tạo bảng/index mới hoặc thêm cột.
* **Idempotent**: chạy lại nhiều lần vẫn an toàn (kiểm tra version trước).
* **Giao dịch**: nếu một migration lỗi, toàn bộ thay đổi của nó được rollback.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .logging_setup import get_logger

log = get_logger("migrations")

VERSION_TABLE = "schema_migrations"


class SupportsExecute(Protocol):
    """Giao diện tối thiểu của ``aiosqlite.Connection`` mà Migrator cần."""

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> Any: ...

    async def commit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Migration:
    """Một bước thay đổi schema, đánh số tăng dần."""

    version: int
    name: str
    statements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version của migration phải >= 1")

    @property
    def label(self) -> str:
        return f"{self.version:03d}_{self.name}"

    def __str__(self) -> str:  # pragma: no cover - chỉ để log
        return self.label


@dataclass(slots=True)
class MigrationResult:
    """Kết quả chạy migration (dùng cho log khởi động và test)."""

    from_version: int
    to_version: int
    applied: list[Migration] = field(default_factory=list)
    failed: list[Migration] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        return (
            f"schema v{self.from_version} → v{self.to_version}: "
            f"{len(self.applied)} applied, {len(self.failed)} failed"
        )
# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

#: Danh sách migration chuẩn của KaiAI. Thêm bảng mới ⇒ thêm entry mới ở cuối,
#: tuyệt đối không sửa lại entry đã phát hành.
CORE_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="baseline",
        statements=(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                name        TEXT    NOT NULL,
                applied_at  REAL    NOT NULL
            )""",
        ),
    ),
    Migration(
        version=2,
        name="ai_memory",
        statements=(
            """CREATE TABLE IF NOT EXISTS ai_sessions (
                user_id      INTEGER NOT NULL,
                guild_id     INTEGER NOT NULL,
                messages     TEXT    NOT NULL DEFAULT '[]',
                tokens_in    INTEGER NOT NULL DEFAULT 0,
                tokens_out   INTEGER NOT NULL DEFAULT 0,
                summary      TEXT,
                updated_at   REAL    NOT NULL,
                PRIMARY KEY (user_id, guild_id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_ai_sessions_updated
               ON ai_sessions(updated_at)""",
        ),
    ),
    Migration(
        version=3,
        name="idempotency",
        statements=(
            """CREATE TABLE IF NOT EXISTS idempotency_keys (
                scope       TEXT    NOT NULL,
                request_id  TEXT    NOT NULL,
                user_id     INTEGER NOT NULL DEFAULT 0,
                status      TEXT    NOT NULL DEFAULT 'in_progress',
                result      TEXT,
                created_at  REAL    NOT NULL,
                expires_at  REAL    NOT NULL,
                PRIMARY KEY (scope, request_id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_idempotency_expiry
               ON idempotency_keys(expires_at)""",
        ),
    ),
    Migration(
        version=4,
        name="v7_economy_and_plans",
        statements=(
            """CREATE TABLE IF NOT EXISTS ai_plans (
                id           TEXT    PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                guild_id     INTEGER NOT NULL DEFAULT 0,
                status       TEXT    NOT NULL DEFAULT 'pending',
                steps        TEXT    NOT NULL DEFAULT '[]',
                total_amount INTEGER NOT NULL DEFAULT 0,
                created_at   REAL    NOT NULL,
                updated_at   REAL    NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_ai_plans_user
               ON ai_plans(user_id, status)""",
        ),
    ),
    Migration(
        version=5,
        name="v7_security_audit_events",
        statements=(
            """CREATE TABLE IF NOT EXISTS security_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT    NOT NULL,
                actor_id    INTEGER NOT NULL,
                severity    TEXT    NOT NULL DEFAULT 'WARNING',
                details     TEXT    NOT NULL,
                created_at  REAL    NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_security_actor_time
               ON security_events(actor_id, created_at)""",
        ),
    ),
)


class Migrator:
    """Áp dụng :data:`CORE_MIGRATIONS` (hoặc danh sách tuỳ chỉnh) lên một DB."""

    def __init__(
        self,
        db: SupportsExecute,
        migrations: Iterable[Migration] = CORE_MIGRATIONS,
        *,
        table: str = VERSION_TABLE,
    ) -> None:
        self.db = db
        self.table = table
        self.migrations = tuple(sorted(migrations, key=lambda m: m.version))

    async def _ensure_table(self) -> None:
        await self.db.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table} ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)"
        )
        await self.db.commit()

    async def current_version(self) -> int:
        """Phiên bản schema hiện tại (0 nếu chưa có bảng version)."""
        try:
            await self._ensure_table()
            cursor = await self.db.execute(f"SELECT MAX(version) FROM {self.table}")
            row = await cursor.fetchone()
        except Exception as exc:  # pragma: no cover - DB hỏng
            log.error("Không đọc được phiên bản schema: %s", exc)
            return 0
        return int(row[0]) if row and row[0] is not None else 0

    def pending(self, current: int) -> list[Migration]:
        """Các migration còn thiếu theo thứ tự version."""
        return [m for m in self.migrations if m.version > current]

    async def apply(self, *, dry_run: bool = False) -> MigrationResult:
        """Chạy các migration còn thiếu. Trả về :class:`MigrationResult`."""
        start = await self.current_version()
        result = MigrationResult(from_version=start, to_version=start)

        for migration in self.pending(start):
            if dry_run:
                result.applied.append(migration)
                result.to_version = migration.version
                continue
            if await self._run(migration):
                result.applied.append(migration)
                result.to_version = migration.version
            else:
                result.failed.append(migration)
                log.error("Migration %s thất bại — dừng tại đây.", migration.label)
                break

        if not dry_run and result.ok and result.applied:
            log.info("Migration: %s", result.summary())
        return result

    async def _run(self, migration: Migration) -> bool:
        """Chạy một migration trong transaction; trả về True nếu thành công."""
        try:
            for statement in migration.statements:
                await self.db.execute(statement)
            await self.db.execute(
                f"INSERT OR REPLACE INTO {self.table} (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, time.time()),
            )
            await self.db.commit()
        except Exception as exc:
            log.error("Lỗi migration %s: %s", migration.label, exc)
            try:
                await self.db.rollback()
            except Exception:  # pragma: no cover
                pass
            return False
        log.info("Đã áp dụng migration %s", migration.label)
        return True


async def apply_to(
    db: SupportsExecute,
    migrations: Iterable[Migration] = CORE_MIGRATIONS,
) -> MigrationResult:
    """Hàm tiện lợi: tạo :class:`Migrator` rồi chạy ngay."""
    return await Migrator(db, migrations).apply()


__all__ = ("Migration", "MigrationResult", "Migrator", "CORE_MIGRATIONS", "apply_to", "VERSION_TABLE")

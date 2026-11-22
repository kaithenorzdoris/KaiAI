"""A small, dependable async SQLite helper for KaiAI.

Features
--------
* One long-lived connection per process (SQLite is fastest this way).
* WAL journal mode, foreign keys enabled, busy timeout configured.
* Bounded automatic retries when the database is momentarily locked.
* ``async with db.transaction():`` for atomic multi-statement writes.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

__all__ = ("Database", "DatabaseError")

_LOCK_WORDS = ("locked", "busy")


class DatabaseError(RuntimeError):
    """Raised when a database operation fails after all retries."""


class Database:
    """Thin async wrapper around :mod:`aiosqlite` with retry + transactions."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 30.0,
        retries: int = 5,
        base_delay: float = 0.05,
        wal: bool = True,
    ) -> None:
        self.path = str(path)
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.base_delay = max(0.0, float(base_delay))
        self.wal = wal
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("Database chưa được connect().")
        return self._conn

    async def connect(self) -> Database:
        """Open the connection and apply pragmas. Safe to call twice."""
        async with self._lock:
            if self._conn is not None:
                return self
            parent = Path(self.path).parent
            if str(parent) not in ("", "."):
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
            self._conn = await aiosqlite.connect(self.path, timeout=self.timeout)
            self._conn.row_factory = aiosqlite.Row
            if self.wal:
                await self._conn.execute("PRAGMA journal_mode=WAL;")
                await self._conn.execute("PRAGMA synchronous=NORMAL;")
            await self._conn.execute("PRAGMA foreign_keys=ON;")
            await self._conn.execute("PRAGMA busy_timeout=5000;")
            await self._conn.commit()
        return self

    async def close(self) -> None:
        """Close the connection (idempotent)."""
        async with self._lock:
            if self._conn is not None:
                try:
                    await self._conn.commit()
                except sqlite3.Error:
                    pass
                await self._conn.close()
                self._conn = None

    async def __aenter__(self) -> Database:
        return await self.connect()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()


    # ------------------------------------------------------------------
    async def _with_retry(self, operation):
        """Run ``operation`` (a zero-arg coroutine factory) with lock retries."""
        attempt = 0
        while True:
            try:
                return await operation()
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if attempt >= self.retries or not any(word in message for word in _LOCK_WORDS):
                    raise DatabaseError(str(exc)) from exc
                await asyncio.sleep(self.base_delay * (2 ** attempt))
                attempt += 1
            except sqlite3.Error as exc:  # pragma: no cover - unexpected sqlite failure
                raise DatabaseError(str(exc)) from exc

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> aiosqlite.Cursor:
        """Execute a statement. The caller is responsible for committing."""
        return await self._with_retry(lambda: self.connection.execute(sql, tuple(parameters)))

    async def executemany(self, sql: str, seq_of_parameters) -> aiosqlite.Cursor:
        return await self._with_retry(
            lambda: self.connection.executemany(sql, seq_of_parameters)
        )

    async def executescript(self, script: str) -> None:
        async def _run() -> None:
            await self.connection.executescript(script)

        await self._with_retry(_run)

    async def fetchone(self, sql: str, parameters: Sequence[Any] = ()) -> aiosqlite.Row | None:
        cursor = await self.execute(sql, parameters)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def fetchall(self, sql: str, parameters: Sequence[Any] = ()) -> list:
        cursor = await self.execute(sql, parameters)
        try:
            return list(await cursor.fetchall())
        finally:
            await cursor.close()

    async def fetchval(self, sql: str, parameters: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, parameters)
        if row is None:
            return default
        try:
            return row[0]
        except (IndexError, KeyError, TypeError):
            return default

    async def commit(self) -> None:
        if self._conn is not None:
            await self._conn.commit()

    async def rollback(self) -> None:
        if self._conn is not None:
            await self._conn.rollback()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Database]:
        """Atomic block: commits on success, rolls back on any exception."""
        if self._conn is None:
            await self.connect()
        try:
            yield self
        except BaseException:
            await self.rollback()
            raise
        else:
            await self.commit()

    async def table_exists(self, name: str) -> bool:
        row = await self.fetchone(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
        )
        return row is not None

    async def pragma(self, statement: str) -> Any:
        """Run ``PRAGMA <statement>`` and return the first value if any."""
        cursor = await self.connection.execute(f"PRAGMA {statement}")
        try:
            row = await cursor.fetchone()
            return row[0] if row is not None else None
        finally:
            await cursor.close()


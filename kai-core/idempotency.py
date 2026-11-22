"""Chống double-click và idempotency cho hành động có thể retry (#28, #29).

Hai tình huống thực tế
----------------------
**Double-click**: người dùng bấm nút "Claim"/"Buy"/"Daily" 5 lần trong 200ms.
Discord gửi 5 interaction riêng biệt; nếu xử lý tuần tự mà không khoá, họ có thể
nhận thưởng 5 lần. :class:`OnceGuard` đảm bảo **một interaction chỉ được xử lý
đúng một lần** theo khoá ``(custom_id, user_id)``.

**Retry / replay**: người dùng (hoặc hạ tầng) gửi lại cùng một yêu cầu — ví dụ
double-click, mạng lag, webhook retry. :class:`IdempotencyStore` gắn một
``request_id`` vào mỗi giao dịch; lần gửi lại sẽ trả về kết quả đã lưu thay vì
thực hiện lại lần nữa.

Cả hai đều **bất đồng bộ** và an toàn khi bot restart vì ``IdempotencyStore``
lưu xuống SQLite.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from .logging_setup import get_logger

log = get_logger("idempotency")

#: Thời gian giữ khoá trong bộ nhớ (giây) cho nút bấm.
DEFAULT_LOCK_TTL = 30.0

#: Thời gian lưu kết quả idempotency trong DB (giây) = 24 giờ.
DEFAULT_RECORD_TTL = 86_400.0


class OnceGuard:
    """Khoá "chỉ chạy một lần" cho interaction, tự mở khoá sau TTL.

    Dùng ``asyncio.Lock`` theo scope để các lần bấm song song thực sự chờ nhau
    thay vì cùng nhảy vào xử lý — đây là chốt chặn race condition thật sự.
    """

    def __init__(self, ttl: float = DEFAULT_LOCK_TTL) -> None:
        self.ttl = float(ttl)
        self._locks: dict[str, asyncio.Lock] = {}
        self._stamps: dict[str, float] = {}
        self._guard = asyncio.Lock()

    async def acquire(self, key: str) -> bool:
        """Thử giành khoá. ``True`` nghĩa là bạn được phép xử lý."""
        async with self._guard:
            self._expire()
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            if lock.locked():
                log.debug("Bỏ qua xử lý trùng: %s", key)
                return False
            await lock.acquire()
            self._stamps[key] = time.monotonic()
            return True

    async def release(self, key: str) -> None:
        """Nhả khoá sau khi xử lý xong (bắt buộc trong ``finally``)."""
        async with self._guard:
            lock = self._locks.get(key)
            if lock is not None and lock.locked():
                lock.release()

    def _expire(self) -> int:
        """Xoá khoá quá hạn (bộ nhớ không phình)."""
        now = time.monotonic()
        stale = [key for key, stamp in self._stamps.items() if now - stamp > self.ttl]
        for key in stale:
            self._stamps.pop(key, None)
            lock = self._locks.get(key)
            if lock is not None and not lock.locked():
                self._locks.pop(key, None)
        return len(stale)

    def sweep(self, force: bool = False) -> int:
        """Dọn khoá quá hạn để bộ nhớ không phình."""
        return self._expire()

    @property
    def tracked(self) -> int:
        return len(self._locks)


@dataclass(slots=True)
class Claim:
    """Kết quả của một lần claim idempotency."""

    claimed: bool
    result: Any = None
    replayed: bool = False


class IdempotencyStore:
    """Khoá idempotency lưu trong SQLite — sống sót qua restart."""

    def __init__(self, db: Any, *, ttl: float = DEFAULT_RECORD_TTL) -> None:
        self.db = db
        self.ttl = float(ttl)

    async def setup(self) -> None:
        """Đảm bảo bảng tồn tại (an toàn nếu migration chưa chạy)."""
        await self.db.execute(
            """CREATE TABLE IF NOT EXISTS idempotency_keys (
                scope       TEXT    NOT NULL,
                request_id  TEXT    NOT NULL,
                user_id     INTEGER NOT NULL DEFAULT 0,
                status      TEXT    NOT NULL DEFAULT 'in_progress',
                result      TEXT,
                created_at  REAL    NOT NULL,
                expires_at  REAL    NOT NULL,
                PRIMARY KEY (scope, request_id)
            )"""
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_expiry ON idempotency_keys(expires_at)"
        )
        await self.db.commit()

    async def claim(self, scope: str, request_id: str, user_id: int = 0) -> Claim:
        """Cố giành ``request_id``.

        * Thành công → ``Claim(claimed=True)``, bạn được phép xử lý.
        * Đã xử lý xong → ``Claim(claimed=False, replayed=True, result=…)``.
        * Đang xử lý → ``Claim(claimed=False, replayed=False)``.
        """
        await self.setup()
        now = time.time()
        await self.db.execute("DELETE FROM idempotency_keys WHERE expires_at < ?", (now,))

        # ⚠️ Phải INSERT trước rồi mới đọc — không được SELECT trước rồi INSERT.
        # Mẫu SELECT-then-INSERT có TOCTOU: hai request đến cùng lúc cùng thấy
        # "chưa có bản ghi", cùng INSERT thành công và cùng nhận claimed=True
        # ⇒ phát thưởng hai lần. ``INSERT OR IGNORE`` + ``rowcount`` là một
        # câu lệnh nguyên tử nên chỉ đúng một bên thắng.
        cursor = await self.db.execute(
            "INSERT OR IGNORE INTO idempotency_keys"
            " (scope, request_id, user_id, status, result, created_at, expires_at)"
            " VALUES (?, ?, ?, 'in_progress', NULL, ?, ?)",
            (scope, request_id, user_id, now, now + self.ttl),
        )
        await self.db.commit()
        if (cursor.rowcount or 0) > 0:
            return Claim(claimed=True)

        # Đã có bản ghi → báo lại trạng thái đã lưu (xong hay đang chạy).
        existing = await self.db.execute(
            "SELECT status, result FROM idempotency_keys WHERE scope = ? AND request_id = ?",
            (scope, request_id),
        )
        row = await existing.fetchone()
        if row is None:
            # Bản ghi vừa bị xoá (hết hạn giữa chừng) → coi như mình thắng.
            return Claim(claimed=True)
        status, payload = row[0], row[1]
        return Claim(
            claimed=False,
            result=json.loads(payload) if payload else None,
            replayed=status == "done",
        )

    async def complete(self, scope: str, request_id: str, result: Any = None) -> None:
        """Đánh dấu đã xử lý xong và lưu kết quả để lần replay trả về."""
        await self.db.execute(
            "UPDATE idempotency_keys SET status = 'done', result = ?"
            " WHERE scope = ? AND request_id = ?",
            (json.dumps(result, ensure_ascii=False), scope, request_id),
        )
        await self.db.commit()

    async def release(self, scope: str, request_id: str) -> None:
        """Xoá khoá khi xử lý thất bại (cho phép retry lại)."""
        await self.db.execute(
            "DELETE FROM idempotency_keys"
            " WHERE scope = ? AND request_id = ? AND status = 'in_progress'",
            (scope, request_id),
        )
        await self.db.commit()

    async def purge_expired(self) -> int:
        """Xoá bản ghi hết hạn. Trả về số dòng đã xoá."""
        cursor = await self.db.execute(
            "DELETE FROM idempotency_keys WHERE expires_at < ?", (time.time(),)
        )
        await self.db.commit()
        return cursor.rowcount or 0


__all__ = (
    "Claim",
    "IdempotencyStore",
    "OnceGuard",
    "DEFAULT_LOCK_TTL",
    "DEFAULT_RECORD_TTL",
)

"""
KaiAI Ultimate V7 — Economy Coordinator & Shared Transaction Manager
===================================================================

Giải quyết triệt để lỗi phân tán Lock (BUG-01) và thiếu Idempotency (BUG-02):
1. **Shared Process Lock**: Cung cấp một asyncio.Lock duy nhất cho toàn bộ tiến trình
   được chia sẻ giữa tất cả các Cog (Games, Company, Business, Career, RealWorld).
2. **Idempotency Protection**: Tự động phát hiện và ngăn chặn double-spending / replay attack
   thông qua bộ nhớ đệm kết quả giao dịch theo token (TTL 120s).
3. **Atomic Execution**: Đảm bảo mọi giao dịch đều chạy dưới BEGIN IMMEDIATE, kiểm tra
   số dư nghiêm ngặt (balance >= 0), rollback toàn diện khi có lỗi.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from .errors import redact
from .logging_setup import get_logger

log = get_logger("economy.coordinator")

#: SQLite / Discord 64-bit signed integer max
MAX_AMOUNT = 9_000_000_000_000_000_000

#: Shared lock singleton across all cogs in the same event loop.
#: Creating asyncio.Lock() at import-time binds it to the wrong loop when tests
#: or workers spin up multiple event loops in the same process.
_SHARED_ECONOMY_LOCKS: dict[int, asyncio.Lock] = {}
_LOCK_INIT_LOCK = threading.Lock()


def get_economy_lock() -> asyncio.Lock:
    """Trả về lock bất đồng bộ dùng chung trong event loop hiện tại."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.Lock()

    loop_id = id(loop)
    with _LOCK_INIT_LOCK:
        lock = _SHARED_ECONOMY_LOCKS.get(loop_id)
        if lock is None:
            lock = asyncio.Lock()
            _SHARED_ECONOMY_LOCKS[loop_id] = lock
        return lock


@dataclass(slots=True)
class TransactionRecord:
    """Bản ghi kết quả giao dịch dùng để cache chống double-spending."""

    ok: bool
    tx_id: str
    amount: int
    sender_id: int
    receiver_id: int | None
    created_at: float
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class IdempotencyCache:
    """Bộ nhớ đệm lưu vết giao dịch gần nhất theo idempotency_key."""

    def __init__(self, ttl_seconds: float = 120.0, max_entries: int = 10_000) -> None:
        self.ttl = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._cache: dict[str, TransactionRecord] = {}

    def get(self, key: str) -> TransactionRecord | None:
        if not key:
            return None
        self._prune()
        return self._cache.get(key)

    def set(self, key: str, record: TransactionRecord) -> None:
        if not key:
            return
        self._prune()
        if len(self._cache) >= self.max_entries:
            # Xoá bớt mục cũ nhất nếu đầy
            oldest = min(self._cache.keys(), key=lambda k: self._cache[k].created_at)
            self._cache.pop(oldest, None)
        self._cache[key] = record

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            k for k, rec in self._cache.items() if now - rec.created_at > self.ttl
        ]
        for k in expired:
            self._cache.pop(k, None)


#: Global idempotency cache singleton
IDEMPOTENCY_CACHE = IdempotencyCache()


def generate_tx_id(prefix: str = "TXN") -> str:
    """Tạo mã giao dịch duy nhất có tiền tố và tính ngẫu nhiên cao."""
    return f"{prefix}-{int(time.time())}-{secrets.token_hex(6).upper()}"


class EconomyCoordinator:
    """Bộ điều phối giao dịch trung tâm (Transaction Coordinator) cho KaiAI V7."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._lock = get_economy_lock()

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    async def execute_atomic(
        self,
        handler: Callable[[Any], Coroutine[Any, Any, Any]],
        *,
        timeout_seconds: float = 15.0,
    ) -> Any:
        """Thực thi một khối giao dịch an toàn trong shared lock với busy timeout."""
        import aiosqlite

        async with self._lock:
            async with aiosqlite.connect(self.db_path, timeout=timeout_seconds) as db:
                await db.execute("PRAGMA journal_mode=WAL")
                await db.execute("PRAGMA synchronous=NORMAL")
                await db.execute("PRAGMA foreign_keys=ON")
                await db.execute(f"PRAGMA busy_timeout={int(timeout_seconds * 1000)}")
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    result = await handler(db)
                    await db.commit()
                    return result
                except Exception as exc:
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.warning("Giao dịch atomic bị huỷ và rollback: %s", exc)
                    raise

    async def atomic_transfer(
        self,
        sender_id: int,
        receiver_id: int,
        amount: int,
        *,
        idempotency_key: str | None = None,
        note: str = "",
    ) -> TransactionRecord:
        """Chuyển tiền atomic giữa 2 tài khoản có bảo vệ Idempotency và non-negative."""
        sender_id = int(sender_id)
        receiver_id = int(receiver_id)
        amount = int(amount)

        if sender_id == receiver_id:
            return TransactionRecord(
                ok=False,
                tx_id="",
                amount=amount,
                sender_id=sender_id,
                receiver_id=receiver_id,
                created_at=time.time(),
                error="Không thể tự chuyển tiền cho chính mình.",
            )

        if amount <= 0:
            return TransactionRecord(
                ok=False,
                tx_id="",
                amount=amount,
                sender_id=sender_id,
                receiver_id=receiver_id,
                created_at=time.time(),
                error="Số tiền chuyển phải là số nguyên dương.",
            )

        if idempotency_key:
            cached = IDEMPOTENCY_CACHE.get(idempotency_key)
            if cached is not None:
                log.info(
                    "Phát hiện giao dịch trùng lặp qua key %s — trả về kết quả đã cache",
                    idempotency_key,
                )
                return cached

        tx_id = generate_tx_id("PAY")

        async def _work(db) -> TransactionRecord:
            # Đảm bảo cả 2 tài khoản tồn tại
            await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (sender_id,))
            await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (receiver_id,))

            # Trừ tiền người gửi với điều kiện số dư đủ
            async with db.execute(
                "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                (amount, sender_id, amount),
            ) as cur:
                if cur.rowcount != 1:
                    return TransactionRecord(
                        ok=False,
                        tx_id="",
                        amount=amount,
                        sender_id=sender_id,
                        receiver_id=receiver_id,
                        created_at=time.time(),
                        error="Số dư không đủ để thực hiện giao dịch.",
                    )

            # Cộng tiền người nhận
            await db.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (amount, receiver_id),
            )

            # Lấy số dư mới sau giao dịch
            async with db.execute("SELECT balance FROM users WHERE user_id=?", (sender_id,)) as cur:
                s_bal = (await cur.fetchone())[0]
            async with db.execute("SELECT balance FROM users WHERE user_id=?", (receiver_id,)) as cur:
                r_bal = (await cur.fetchone())[0]

            now_ts = time.time()
            clean_note = redact(note)[:100]

            # Ghi sổ cái transactions
            await db.execute(
                "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note) "
                "VALUES(?,?,?,?,?,?)",
                (
                    sender_id,
                    "transfer_out",
                    -amount,
                    s_bal,
                    now_ts,
                    f"{tx_id} to:{receiver_id} {clean_note}".strip(),
                ),
            )
            await db.execute(
                "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note) "
                "VALUES(?,?,?,?,?,?)",
                (
                    receiver_id,
                    "transfer_in",
                    amount,
                    r_bal,
                    now_ts,
                    f"{tx_id} from:{sender_id} {clean_note}".strip(),
                ),
            )

            return TransactionRecord(
                ok=True,
                tx_id=tx_id,
                amount=amount,
                sender_id=sender_id,
                receiver_id=receiver_id,
                created_at=now_ts,
                data={
                    "sender_balance": s_bal,
                    "receiver_balance": r_bal,
                    "note": clean_note,
                },
            )

        record = await self.execute_atomic(_work)
        if record.ok and idempotency_key:
            IDEMPOTENCY_CACHE.set(idempotency_key, record)
        return record

    async def atomic_adjust_balance(
        self,
        user_id: int,
        amount: int,
        kind: str = "adjustment",
        *,
        note: str = "",
        allow_negative: bool = False,
    ) -> tuple[bool, int]:
        """Cộng/trừ số dư an toàn, mặc định không bao giờ cho phép âm (balance >= 0)."""
        user_id = int(user_id)
        amount = int(amount)

        async def _work(db) -> tuple[bool, int]:
            await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))

            if amount < 0 and not allow_negative:
                async with db.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=? AND balance+? >= 0",
                    (amount, user_id, amount),
                ) as cur:
                    if cur.rowcount != 1:
                        return False, 0
            else:
                await db.execute(
                    "UPDATE users SET balance=balance+? WHERE user_id=?",
                    (amount, user_id),
                )

            async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cur:
                new_bal = (await cur.fetchone())[0]

            now_ts = time.time()
            clean_note = redact(note)[:100]
            await db.execute(
                "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note) "
                "VALUES(?,?,?,?,?,?)",
                (user_id, kind, amount, new_bal, now_ts, clean_note),
            )
            return True, new_bal

        return await self.execute_atomic(_work)


__all__ = (
    "EconomyCoordinator",
    "IDEMPOTENCY_CACHE",
    "IdempotencyCache",
    "MAX_AMOUNT",
    "TransactionRecord",
    "generate_tx_id",
    "get_economy_lock",
)


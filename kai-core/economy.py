"""Lớp kinh tế an toàn — atomic, chống khai thác, RNG mạnh (#26, #27, #42, #44).

Vì sao cần một lớp riêng
-------------------------
``cogs/games.py`` đã có ``transfer``/``update_balance`` atomic bằng
``BEGIN IMMEDIATE``. Module này **bổ sung** những bảo đảm còn thiếu mà động cơ
cũ chưa có, và gom chúng vào một chỗ để các cog dùng lại:

Kiểm tra đầu vào (#27)
    ``validate_amount`` chặn số âm, ``NaN``, ``±inf``, tràn số nguyên 64-bit,
    sai kiểu và giá trị vượt trần. ``/pay -100000`` bị từ chối **trước khi**
    chạm database.

Giao dịch atomic (#26)
    :class:`EconomyService` mở ``BEGIN IMMEDIATE``, kiểm tra số dư, ghi hai bên,
    ghi ``transactions`` rồi ``commit``. Bất kỳ lỗi nào → ``rollback``; không
    bao giờ có trường hợp "trừ tiền xong thì crash".

Server là authority (#44)
    Mọi số tiền do bot tính từ dữ liệu trong DB. Giá trị từ interaction chỉ là
    **ý định** và luôn đi qua lại validation ở đây.

RNG mạnh (#42)
    :func:`rng_int` dùng :mod:`secrets` (CSPRNG), không seed được từ phía người
    chơi — không thể đoán trước kết quả.
"""

from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass

from .errors import redact
from .logging_setup import get_logger

log = get_logger("economy")

#: SQLite/Discord dùng INTEGER 64-bit có dấu.
MAX_AMOUNT = 9_000_000_000_000_000_000


class EconomyError(Exception):
    """Lỗi nghiệp vụ kinh tế — an toàn để hiển thị cho người dùng."""


@dataclass(slots=True)
class TransferResult:
    """Kết quả một giao dịch chuyển tiền."""

    ok: bool
    transaction_id: str = ""
    amount: int = 0
    sender_balance: int = 0
    receiver_balance: int = 0
    error: str = ""

    @classmethod
    def failed(cls, reason: str) -> TransferResult:
        return cls(ok=False, error=reason)


@dataclass(slots=True)
class BalanceSnapshot:
    """Số dư tại một thời điểm."""

    balance: int
    bank: int = 0


def validate_amount(amount: object, *, minimum: int = 1, maximum: int = MAX_AMOUNT) -> int:
    """Kiểm tra số tiền đầu vào, trả về ``int`` hợp lệ.

    Chặn: âm, ``NaN``, ``±inf``, float mất chính xác, tràn 64-bit, sai kiểu.
    """
    if isinstance(amount, bool):
        raise EconomyError("Số tiền không hợp lệ.")
    if not isinstance(amount, (int, float, str)):
        raise EconomyError("Số tiền phải là số.")
    try:
        value = float(amount) if isinstance(amount, str) else amount
    except (TypeError, ValueError) as exc:
        raise EconomyError("Số tiền không phải là số hợp lệ.") from exc
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EconomyError("Số tiền không hợp lệ (NaN/Infinity).")
        if not value.is_integer():
            raise EconomyError("Số tiền phải là số nguyên.")
    result = int(value)
    if result < minimum:
        raise EconomyError(f"Số tiền tối thiểu là {minimum:,}.")
    if result > maximum or result > MAX_AMOUNT:
        raise EconomyError("Số tiền vượt giới hạn cho phép.")
    return result


def rng_int(low: int, high: int) -> int:
    """Số nguyên ngẫu nhiên **mã hoá** trong ``[low, high]`` (CSPRNG)."""
    if high < low:
        low, high = high, low
    return secrets.randbelow(high - low + 1) + low


def rng_float() -> float:
    """Số thực ngẫu nhiên trong ``[0.0, 1.0)`` từ CSPRNG."""
    return secrets.randbelow(10**9) / 10**9


def new_transaction_id() -> str:
    """Mã giao dịch ngẫu nhiên, khó đoán và không trùng."""
    return f"TXN-{int(time.time())}-{secrets.token_hex(4)}"


class EconomyService:
    """Bao bọc thao tác tiền trên database economy **đã có sẵn**.

    Service **không** tự tạo schema — nó dùng đúng bảng ``users`` và
    ``transactions`` mà ``cogs/games.py`` đã tạo, nên dữ liệu cũ không bị ảnh hưởng.
    """

    def __init__(self, db_path: str, *, lock=None) -> None:
        self.db_path = db_path
        self._lock = lock

    async def _guarded(self, coro_factory):
        """Chạy coroutine trong ``self._lock`` nếu có (chống race trong 1 process)."""
        if self._lock is None:
            return await coro_factory()
        async with self._lock:
            return await coro_factory()

    @staticmethod
    async def _balance(db, user_id: int) -> int:
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
        async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def transfer(
        self,
        sender: int,
        receiver: int,
        amount: object,
        *,
        note: str = "",
    ) -> TransferResult:
        """Chuyển tiền atomic (#26). Mọi lỗi → rollback, không mất tiền."""
        if sender == receiver:
            return TransferResult.failed("Không thể tự chuyển tiền cho chính mình.")
        try:
            value = validate_amount(amount)
        except EconomyError as exc:
            return TransferResult.failed(str(exc))

        async def _run() -> TransferResult:
            import aiosqlite

            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    # Guard atomic: chi tru khi that su du (2 lenh song
                    # song deu co the doc "du tien" neu chi SELECT truoc).
                    async with db.execute(
                        "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
                        (value, sender, value),
                    ) as cur:
                        if cur.rowcount != 1:
                            await db.rollback()
                            return TransferResult.failed("Số dư không đủ.")
                    sender_balance = await self._balance(db, sender)
                    await db.execute(
                        "UPDATE users SET balance=balance+? WHERE user_id=?", (value, receiver)
                    )
                    receiver_balance = await self._balance(db, receiver)
                    txn = new_transaction_id()
                    stamp = time.time()
                    await db.execute(
                        "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                        " VALUES(?,?,?,?,?,?)",
                        (sender, "transfer_out", -value, sender_balance - value, stamp,
                         redact(f"{txn} to:{receiver} {note}")[:200]),
                    )
                    await db.execute(
                        "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                        " VALUES(?,?,?,?,?,?)",
                        (receiver, "transfer_in", value, receiver_balance, stamp,
                         redact(f"{txn} from:{sender} {note}")[:200]),
                    )
                    await db.commit()
                    return TransferResult(
                        ok=True,
                        transaction_id=txn,
                        amount=value,
                        sender_balance=sender_balance - value,
                        receiver_balance=receiver_balance,
                    )
                except Exception as exc:  # noqa: BLE001 - luôn rollback trước khi ném
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Chuyển tiền thất bại (%s→%s)", sender, receiver)
                    return TransferResult.failed(f"Giao dịch lỗi: {type(exc).__name__}")

        return await self._guarded(_run)

    async def adjust(
        self,
        user_id: int,
        amount: object,
        kind: str = "adjustment",
        note: str = "",
    ) -> BalanceSnapshot:
        """Cộng/trừ tiền atomic; số dư **không bao giờ** âm."""
        if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
            raise EconomyError("Số tiền không hợp lệ.")
        try:
            numeric = float(amount)
            # minimum=0: cộng/trừ 0 là no-op hợp lệ (ví dụ khởi tạo số dư).
            magnitude = validate_amount(abs(numeric), minimum=0)
        except (EconomyError, TypeError, ValueError) as exc:
            raise EconomyError("Số tiền không hợp lệ.") from exc
        value = -magnitude if numeric < 0 else magnitude

        async def _run() -> BalanceSnapshot:
            import aiosqlite

            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
                    if value < 0:
                        cursor = await db.execute(
                            "UPDATE users SET balance=balance+? WHERE user_id=? AND balance+? >= 0",
                            (value, user_id, value),
                        )
                        if cursor.rowcount != 1:
                            await db.rollback()
                            raise EconomyError("Số dư không đủ cho giao dịch này.")
                    else:
                        await db.execute(
                            "UPDATE users SET balance=balance+? WHERE user_id=?", (value, user_id)
                        )
                    balance = await self._balance(db, user_id)
                    await db.execute(
                        "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
                        " VALUES(?,?,?,?,?,?)",
                        (user_id, kind, value, balance, time.time(), redact(note)[:200]),
                    )
                    await db.commit()
                    return BalanceSnapshot(balance=balance)
                except EconomyError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("adjust() thất bại cho %s", user_id)
                    raise EconomyError(f"Giao dịch lỗi: {type(exc).__name__}") from exc

        return await self._guarded(_run)


__all__ = (
    "BalanceSnapshot",
    "EconomyError",
    "EconomyService",
    "MAX_AMOUNT",
    "TransferResult",
    "new_transaction_id",
    "rng_float",
    "rng_int",
    "validate_amount",
)

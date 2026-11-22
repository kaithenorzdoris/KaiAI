"""Rate limiting nhiều tầng — chống spam và bảo vệ bot khi chạy 24/7 (#24).

Vì sao cần
----------
Một user (hoặc bot đệm) spam 20 lệnh/giây sẽ khiến bot vướng rate limit của
Discord, spam log và có thể làm nghẽn cả server. Module này cung cấp **token
bucket** — thuật toán chuẩn, cho phép "burst" ngắn nhưng giới hạn trung bình dài
hạn, với các tầng độc lập: user, guild, channel, command và nhóm tính năng.

Bộ nhớ được chặn trên
---------------------
Key đã hồi đầy sẽ bị quét định kỳ (mặc định 5 phút), nên rate limiter **không
bao giờ** phình vô hạn dù bot chạy hàng tháng.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from .logging_setup import get_logger

log = get_logger("ratelimit")

DEFAULT_PERIOD = 60.0
DEFAULT_SWEEP_INTERVAL = 300.0


@dataclass(slots=True)
class Decision:
    """Kết quả kiểm tra rate limit."""

    allowed: bool
    retry_after: float = 0.0
    scope: str = ""
    key: Hashable = ""

    @property
    def message(self) -> str:
        if self.allowed:
            return ""
        return f"⏳ Bạn đang thao tác quá nhanh. Thử lại sau **{self.retry_after:.1f}s**."


@dataclass(slots=True)
class Bucket:
    """Một token bucket đơn giản.

    ``clock`` được truyền vào để test được xác định (deterministic) — trước đây
    hàm này gọi thẳng :func:`time.monotonic` nên mọi test dùng clock giả đều sai.
    """

    capacity: float
    refill_per_second: float
    clock: Callable[[], float] | None = None
    tokens: float | None = None
    updated: float = 0.0

    def __post_init__(self) -> None:
        if self.clock is None:
            self.clock = time.monotonic
        if self.tokens is None:
            self.tokens = self.capacity
        self.updated = self.clock()

    def _now(self) -> float:
        return self.clock() if self.clock else time.monotonic()

    def refill(self, now: float | None = None) -> float:
        """Cộng token theo thời gian đã trôi qua và trả về số token hiện tại."""
        now = self._now() if now is None else now
        elapsed = max(0.0, now - self.updated)
        self.updated = now
        self.tokens = min(self.capacity, float(self.tokens) + elapsed * self.refill_per_second)
        return float(self.tokens)

    def take(self, cost: float = 1.0) -> tuple[bool, float]:
        """Trừ ``cost`` token. Trả về ``(thành công, số giây phải chờ)``."""
        self.refill()
        if float(self.tokens) >= cost:
            self.tokens = float(self.tokens) - cost
            return True, 0.0
        return False, max(0.0, (cost - float(self.tokens)) / self.refill_per_second)


class RateLimiter:
    """Token bucket có tự quét key cũ để không phình bộ nhớ."""

    def __init__(
        self,
        *,
        capacity: int = 20,
        period: float = DEFAULT_PERIOD,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if capacity < 1 or period <= 0:
            raise ValueError("capacity >= 1 và period > 0")
        self.capacity = float(capacity)
        self.period = float(period)
        self.sweep_interval = float(sweep_interval)
        self._clock = clock or time.monotonic
        self._buckets: dict[tuple[str, Hashable], Bucket] = {}
        self._last_sweep = self._clock()
# -- cốt lõi ---------------------------------------------------------
    def _bucket(self, key: tuple[str, Hashable]) -> Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = Bucket(
                capacity=self.capacity,
                refill_per_second=self.capacity / self.period,
                clock=self._clock,
            )
            self._buckets[key] = bucket
        return bucket

    def check(self, scope: str, identifier: Hashable, cost: float = 1.0) -> Decision:
        """Kiểm tra (và **tiêu** token) cho ``scope:identifier``."""
        self.sweep()
        key = (scope, identifier)
        allowed, retry_after = self._bucket(key).take(cost)
        if not allowed:
            log.debug("Chặn %s:%s, chờ %.1fs", scope, identifier, retry_after)
        return Decision(allowed=allowed, retry_after=retry_after, scope=scope, key=identifier)

    def peek(self, scope: str, identifier: Hashable) -> Decision:
        """Kiểm tra **không** tiêu token (dùng để hiển thị)."""
        bucket = self._bucket((scope, identifier))
        return Decision(allowed=bucket.refill() >= 1.0, scope=scope, key=identifier)

    def reset(self, scope: str | None = None, identifier: Hashable | None = None) -> None:
        """Xoá bucket theo scope/identifier (bỏ trống = xoá tất cả)."""
        if scope is None:
            self._buckets.clear()
            return
        stale = [
            key
            for key in self._buckets
            if key[0] == scope and (identifier is None or key[1] == identifier)
        ]
        for key in stale:
            self._buckets.pop(key, None)

    def sweep(self, force: bool = False) -> int:
        """Dọn các bucket đã refill đầy (tức là không còn gì để nhớ).

        Phải ``refill()`` trước khi so sánh — nếu không thì bucket chưa từng được
        truy cập lại sẽ giữ số token cũ và **không bao giờ** được dọn.
        """
        now = self._clock()
        if not force and now - self._last_sweep < self.sweep_interval:
            return 0
        self._last_sweep = now
        stale = [key for key, bucket in self._buckets.items() if bucket.refill(now) >= self.capacity]
        for key in stale:
            self._buckets.pop(key, None)
        return len(stale)

    @property
    def tracked(self) -> int:
        return len(self._buckets)


class MultiRateLimiter:
    """Kiểm tra **nhiều tầng cùng lúc** — tất cả phải cho qua mới chạy."""

    def __init__(
        self,
        user: RateLimiter,
        guild: RateLimiter,
        channel: RateLimiter,
        command: RateLimiter,
        feature: RateLimiter | None = None,
    ) -> None:
        self.user = user
        self.guild = guild
        self.channel = channel
        self.command = command
        self.feature = feature

    def layered_check(
        self,
        *,
        user_id: int,
        guild_id: int = 0,
        channel_id: int = 0,
        command_name: str = "",
        feature: str = "",
        cost: float = 1.0,
    ) -> Decision:
        """Trả về ``Decision`` của tầng đầu tiên bị chặn, ngược lại là cho phép."""
        layers: list[tuple[str, Hashable, RateLimiter | None]] = [
            ("user", user_id, self.user),
            ("guild", guild_id, self.guild if guild_id else None),
            ("channel", channel_id, self.channel if channel_id else None),
            ("command", command_name, self.command if command_name else None),
            ("feature", feature, self.feature if feature else None),
        ]
        for scope, identifier, limiter in layers:
            if limiter is None:
                continue
            decision = limiter.check(scope, identifier, cost)
            if not decision.allowed:
                return decision
        return Decision(allowed=True)

    def reset_user(self, user_id: int) -> None:
        self.user.reset("user", user_id)


def build_default_limiter() -> MultiRateLimiter:
    """Bộ giới hạn mặc định — đủ dùng cho bot cỡ vừa, an toàn khi chạy 24/7."""
    return MultiRateLimiter(
        user=RateLimiter(capacity=20, period=60.0),      # 20 lệnh/phút mỗi user
        guild=RateLimiter(capacity=120, period=60.0),    # 120 lệnh/phút mỗi server
        channel=RateLimiter(capacity=40, period=60.0),   # 40 lệnh/phút mỗi kênh
        command=RateLimiter(capacity=10, period=30.0),   # 10 lần/30s mỗi lệnh
        feature=RateLimiter(capacity=6, period=60.0),    # 6 lần/phút mỗi nhóm tính năng
    )


__all__ = ("Bucket", "Decision", "RateLimiter", "MultiRateLimiter", "build_default_limiter")

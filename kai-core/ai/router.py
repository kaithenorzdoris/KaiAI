"""Router AI: chọn provider, retry, fallback, giới hạn token và đồng thời (#37, #25).

Cơ chế bảo vệ (đủ để chạy 24/7)
--------------------------------
* **Concurrency toàn cục** — semaphore chặn số request AI đồng thời.
* **Token budget** — theo user và toàn bot; vượt ngưỡng thì chặn trước khi tốn tiền.
* **Retry + exponential backoff** — chỉ retry lỗi *tạm thời*; có jitter để tránh
  nhiều request cùng đổ về một thời điểm.
* **Circuit breaker** — provider lỗi liên tiếp bị tạm ngắt, request chuyển sang
  provider dự phòng thay vì chờ timeout.
* **Fallback** — OpenAI lỗi → Gemini → thông báo thân thiện. Không retry vô hạn.
* **Đo lường** — số lần gọi, lỗi, fallback, token, độ trễ (phục vụ #17).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..logging_setup import get_logger
from .provider import AIRequest, AIResponse, Provider, ProviderError

log = get_logger("ai.router")

MAX_ATTEMPTS = 2
BASE_BACKOFF = 0.5
MAX_BACKOFF = 8.0


@dataclass(slots=True)
class UsageStats:
    """Bộ đếm quan sát được, phục vụ log AI và ``/diagnostics``."""

    requests: int = 0
    successes: int = 0
    failures: int = 0
    fallbacks: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    total_latency: float = 0.0
    by_provider: dict[str, int] = field(default_factory=dict)

    @property
    def avg_latency(self) -> float:
        return self.total_latency / self.successes if self.successes else 0.0

    def snapshot(self) -> dict[str, float | int]:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "fallbacks": self.fallbacks,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "avg_latency": round(self.avg_latency, 3),
        }


class TokenBudget:
    """Hạn mức token theo user và toàn bot, cửa sổ trượt."""

    def __init__(
        self,
        *,
        per_user: int = 40_000,
        global_limit: int = 4_000_000,
        window: float = 3600.0,
    ) -> None:
        self.per_user = int(per_user)
        self.global_limit = int(global_limit)
        self.window = float(window)
        self._user: dict[int, tuple[int, float]] = {}
        self._global: tuple[int, float] = (0, time.monotonic())
        self._lock = asyncio.Lock()

    def _prune(self) -> None:
        cutoff = time.monotonic() - self.window
        for user_id, (_spent, stamp) in list(self._user.items()):
            if stamp < cutoff:
                self._user.pop(user_id, None)
        total, stamp = self._global
        if stamp < cutoff:
            self._global = (0, time.monotonic())

    async def check(self, user_id: int, estimated: int) -> str | None:
        """Trả về lý do bị chặn, hoặc ``None`` nếu còn hạn mức."""
        async with self._lock:
            self._prune()
            user_tokens = self._user.get(user_id, (0, time.monotonic()))[0]
            global_tokens = self._global[0]
            if user_tokens + estimated > self.per_user:
                return f"bạn đã dùng hết hạn mức AI ({self.per_user:,} token/giờ)"
            if global_tokens + estimated > self.global_limit:
                return "bot đã dùng hết hạn mức AI toàn cục"
            return None

    async def consume(self, user_id: int, tokens: int) -> None:
        async with self._lock:
            now = time.monotonic()
            spent, stamp = self._user.get(user_id, (0, now))
            if stamp < now - self.window:
                spent = 0
            self._user[user_id] = (spent + max(0, tokens), now)
            total, stamp = self._global
            if stamp < now - self.window:
                total = 0
            self._global = (total + max(0, tokens), now)

    @property
    def tracked_users(self) -> int:
        return len(self._user)


class AIRouter:
    """Điểm vào duy nhất cho mọi lời gọi AI trong KaiAI."""

    def __init__(
        self,
        providers: Sequence[Provider],
        *,
        concurrency: int = 3,
        per_user_token_budget: int = 40_000,
        global_token_budget: int = 4_000_000,
        budget_window: float = 3600.0,
        estimate_tokens_per_char: int = 4,
    ) -> None:
        self.providers: list[Provider] = list(providers)
        self.concurrency = asyncio.Semaphore(max(1, concurrency))
        self.budget = TokenBudget(
            per_user=per_user_token_budget,
            global_limit=global_token_budget,
            window=budget_window,
        )
        self.estimate_tokens_per_char = max(1, estimate_tokens_per_char)
        self.stats = UsageStats()

    def available_providers(self) -> list[Provider]:
        """Các provider sẵn sàng, theo đúng thứ tự ưu tiên đã cấu hình."""
        return [p for p in self.providers if p.available]

    def describe(self) -> str:
        """Mô tả ngắn chuỗi provider (cho log khởi động và diagnostics)."""
        if not self.providers:
            return "không có provider nào được cấu hình"
        return " → ".join(
            f"{p.name}:{'sẵn sàng' if p.available else 'tạm chưa dùng'}" for p in self.providers
        )

    async def complete(self, request: AIRequest) -> AIResponse:
        """Gọi AI với fallback + retry + budget. **Không bao giờ ném ra ngoài.**"""
        self.stats.requests += 1

        estimated = max(1, request.estimated_input_chars() // self.estimate_tokens_per_char)
        blocked = await self.budget.check(request.user_id, estimated)
        if blocked:
            self.stats.failures += 1
            return AIResponse(text="", ok=False, error=f"budget: {blocked}")

        candidates = self.available_providers()
        if not candidates:
            self.stats.failures += 1
            reason = (
                "Chưa cấu hình AI hoặc mọi provider đang tạm ngắt. "
                "Hãy kiểm tra OPENAI_API_KEY / GEMINI_API_KEY."
            )
            log.warning("AI không khả dụng: %s", reason)
            return AIResponse(text="", ok=False, error=reason)

        last_error = ""
        for index, provider in enumerate(candidates):
            if index > 0:
                self.stats.fallbacks += 1
            response = await self._call_with_retry(provider, request)
            if response.ok:
                self.stats.successes += 1
                self.stats.tokens_in += response.tokens_in
                self.stats.tokens_out += response.tokens_out
                self.stats.total_latency += response.latency
                name = response.provider or provider.name
                self.stats.by_provider[name] = self.stats.by_provider.get(name, 0) + 1
                await self.budget.consume(request.user_id, response.total_tokens or estimated)
                response.fallback_used = index > 0
                return response
            last_error = response.error
            log.warning("Provider %s thất bại: %s", provider.name, last_error)

        self.stats.failures += 1
        return AIResponse(text="", ok=False, error=last_error or "AI không phản hồi.")

    async def _call_with_retry(self, provider: Provider, request: AIRequest) -> AIResponse:
        """Retry lỗi tạm thời với exponential backoff + jitter."""
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self.concurrency:
                    response = await provider.complete(request)
            except ProviderError as exc:
                provider.breaker.record_failure()
                if exc.retryable and attempt < MAX_ATTEMPTS and provider.breaker.available:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                return AIResponse(text="", ok=False, error=str(exc), provider=provider.name)
            except asyncio.CancelledError:  # pragma: no cover - không nuốt cancel
                raise
            except Exception as exc:  # noqa: BLE001 - provider lạ có thể ném bất cứ gì
                provider.breaker.record_failure()
                return AIResponse(
                    text="", ok=False, error=f"{type(exc).__name__}: {exc}", provider=provider.name
                )

            provider.breaker.record_success()
            return response

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff có jitter để tránh thundering herd."""
        delay = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
        return delay * (0.75 + random.random() * 0.5)

    async def close(self) -> None:
        """Đóng tất cả provider."""
        for provider in self.providers:
            try:
                await provider.close()
            except Exception as exc:  # pragma: no cover
                log.debug("Lỗi khi đóng provider %s: %s", provider.name, exc)


def build_router_from_env(config) -> AIRouter:
    """Dựng router từ :class:`kai_core.config.BotConfig`."""
    from .gemini_provider import GeminiProvider
    from .openai_provider import OpenAIProvider

    providers: list[Provider] = []
    if config.openai_api_key:
        providers.append(OpenAIProvider(config.openai_api_key, config.openai_model, max_history=10))
    if config.gemini_api_key:
        providers.append(GeminiProvider(config.gemini_api_key, config.gemini_model, max_history=10))
    return AIRouter(providers, concurrency=config.ai_concurrency)


__all__ = ("AIRouter", "TokenBudget", "UsageStats", "build_router_from_env", "MAX_ATTEMPTS")

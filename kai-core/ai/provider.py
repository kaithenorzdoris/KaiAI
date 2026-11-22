"""Hợp đồng chung cho mọi AI provider (yêu cầu #36).

Các Cog **không** được chứa logic provider. Chúng chỉ nói "tôi cần AI trả lời
câu này"; :mod:`kai_core.ai.router` sẽ chọn provider, retry, fallback và tính
token. Thêm Gemini sau này không đụng một dòng code Cog nào.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


class ProviderError(RuntimeError):
    """Lỗi từ một provider cụ thể.

    ``retryable`` cho biết router có nên thử provider khác hay báo lỗi luôn.
    """

    def __init__(self, provider: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


@dataclass(slots=True)
class ChatMessage:
    """Một lượt trong hội thoại."""

    role: str  # "user" | "assistant" | "system"
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class AIRequest:
    """Yêu cầu AI đã chuẩn hoá, độc lập với provider."""

    prompt: str
    system: str = ""
    history: list[ChatMessage] = field(default_factory=list)
    max_output_tokens: int = 2000
    temperature: float = 0.8
    user_id: int = 0
    guild_id: int = 0
    kind: str = "chat"  # "chat" | "image" | "summarize"
    metadata: dict[str, Any] = field(default_factory=dict)

    def estimated_input_chars(self) -> int:
        return len(self.prompt) + len(self.system) + sum(len(m.content) for m in self.history)

    def to_openai_messages(self) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if self.system:
            messages.append({"role": "system", "content": self.system})
        messages.extend(m.to_dict() for m in self.history)
        messages.append({"role": "user", "content": self.prompt})
        return messages


@dataclass(slots=True)
class AIResponse:
    """Kết quả chuẩn hoá từ mọi provider."""

    text: str
    provider: str = ""
    model: str = ""
    latency: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    ok: bool = True
    error: str = ""
    fallback_used: bool = False

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def failed(self, reason: str) -> AIResponse:
        return AIResponse(text="", ok=False, error=reason, provider=self.provider)


class CircuitBreaker:
    """Ngắt mạch sau N lỗi liên tiếp để không spam một provider đã chết.

    ``OPEN`` trong ``cooldown`` giây → mọi request bị từ chối ngay, rồi thử
    ``half_open`` (cho phép 1 thử) để dò xem provider đã hồi chưa.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, *, threshold: int = 3, cooldown: float = 30.0, name: str = "") -> None:
        if threshold < 1 or cooldown <= 0:
            raise ValueError("threshold >= 1 và cooldown > 0")
        self.threshold = int(threshold)
        self.cooldown = float(cooldown)
        self.name = name
        self._failures = 0
        self._opened_at = 0.0
        self._state = self.CLOSED

    @property
    def state(self) -> str:
        if self._state == self.OPEN and time.monotonic() - self._opened_at >= self.cooldown:
            self._state = self.HALF_OPEN
        return self._state

    @property
    def available(self) -> bool:
        return self.state != self.OPEN

    def record_success(self) -> None:
        self._failures = 0
        self._state = self.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._state = self.OPEN
            self._opened_at = time.monotonic()


class Provider(ABC):
    """Giao diện bắt buộc cho một provider (OpenAI, Gemini, …)."""

    #: Tên hiển thị, dùng trong log và báo cáo.
    name: str = "provider"

    def __init__(self, *, breaker: CircuitBreaker | None = None) -> None:
        self.breaker = breaker or CircuitBreaker(name=self.name)

    @property
    def available(self) -> bool:
        """Provider có dùng được không (có key + mạch không bị ngắt)."""
        return bool(self.configured) and self.breaker.available

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Provider đã có đủ cấu hình (API key…) chưa."""

    @property
    @abstractmethod
    def model(self) -> str:
        """Tên model đang dùng."""

    @abstractmethod
    async def complete(self, request: AIRequest) -> AIResponse:
        """Thực hiện một lượt hoàn thành; ném :class:`ProviderError` khi lỗi."""

    async def close(self) -> None:  # pragma: no cover - tuỳ chọn, mặc định không cần
        """Giải phóng tài nguyên (mặc định không làm gì)."""
        return None


def normalize_history(history: Sequence[ChatMessage] | None, limit: int) -> list[ChatMessage]:
    """Giới hạn số lượt hội thoại được gửi đi (giảm token + rủi ro context tràn)."""
    items = list(history or [])
    return items[-limit:] if limit > 0 else items


__all__ = (
    "AIRequest",
    "AIResponse",
    "ChatMessage",
    "CircuitBreaker",
    "Provider",
    "ProviderError",
    "normalize_history",
)

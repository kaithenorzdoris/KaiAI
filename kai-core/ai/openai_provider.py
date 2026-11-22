"""Provider OpenAI — hiện thực :class:`~kai_core.ai.provider.Provider`.

Cố tình dùng ``responses.create`` khi có (model mới) và tự động lùi về
``chat.completions.create`` cho model cũ, để không bị phụ thuộc vào một API
cụ thể. Retry/backoff được đặt ở :mod:`kai_core.ai.router` (tránh retry hai lần).
"""

from __future__ import annotations

import time
from typing import Any

from .provider import AIRequest, AIResponse, CircuitBreaker, Provider, ProviderError

try:  # openai là phụ thuộc bắt buộc của dự án
    import openai
except Exception:  # pragma: no cover - thiếu dependency
    openai = None  # type: ignore[assignment]

#: Số status code coi là lỗi tạm thời (đáng retry/fallback).
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class OpenAIProvider(Provider):
    """Gọi OpenAI (chat + responses) qua SDK chính thức."""

    name = "openai"

    def __init__(
        self,
        api_key: str | None,
        model: str = "gpt-4o-mini",
        *,
        timeout: float = 30.0,
        max_history: int = 10,
        breaker: CircuitBreaker | None = None,
        client: Any = None,
    ) -> None:
        super().__init__(breaker=breaker)
        self.api_key = api_key
        self._model = model
        self.timeout = float(timeout)
        self.max_history = int(max_history)
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self):
        if self._client is not None:
            return self._client
        if openai is None:
            raise ProviderError(self.name, "Thiếu thư viện `openai`.", retryable=False)
        self._client = openai.AsyncOpenAI(
            api_key=self.api_key,
            timeout=self.timeout,
            max_retries=0,  # retry do router quản lý
        )
        return self._client

    # ------------------------------------------------------------------
    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return status in RETRYABLE_STATUS
        return isinstance(exc, (TimeoutError, ConnectionError, OSError))

    async def complete(self, request: AIRequest) -> AIResponse:
        """Gửi request và trả về :class:`AIResponse` đã chuẩn hoá."""
        client = self._get_client()
        started = time.perf_counter()
        messages = request.to_openai_messages()

        try:
            response = await self._invoke(client, request, messages)
            text = self._extract_text(response)
            usage = self._extract_usage(response)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - chuẩn hoá mọi lỗi SDK
            raise ProviderError(
                self.name, f"{type(exc).__name__}: {exc}", retryable=self._is_retryable(exc)
            ) from exc

        return AIResponse(
            text=text,
            provider=self.name,
            model=self._model,
            latency=time.perf_counter() - started,
            tokens_in=usage[0],
            tokens_out=usage[1],
        )

    async def _invoke(self, client, request: AIRequest, messages: list[dict[str, str]]):
        """Ưu tiên Responses API, lùi về Chat Completions nếu model không hỗ trợ."""
        if hasattr(client, "responses"):
            try:
                return await client.responses.create(
                    model=self._model,
                    instructions=request.system or None,
                    input=messages,
                    max_output_tokens=request.max_output_tokens,
                )
            except Exception as exc:
                if not self._is_retryable(exc) and "responses" in type(exc).__name__.lower():
                    raise
                if not hasattr(client, "chat"):
                    raise
        return await client.chat.completions.create(
            model=self._model,
            messages=messages,
            max_tokens=request.max_output_tokens,
            temperature=request.temperature,
        )

    @staticmethod
    def _extract_text(response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if output_text:
            return str(output_text).strip()
        choices = getattr(response, "choices", None)
        if choices:
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None)
            if content:
                return str(content).strip()
        raise ProviderError("openai", "Phản hồi AI không có nội dung.", retryable=False)

    @staticmethod
    def _extract_usage(response: Any) -> tuple[int, int]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0, 0
        tokens_in = getattr(usage, "input_tokens", None)
        if tokens_in is None:
            tokens_in = getattr(usage, "prompt_tokens", 0)
        tokens_out = getattr(usage, "output_tokens", None)
        if tokens_out is None:
            tokens_out = getattr(usage, "completion_tokens", 0)
        return int(tokens_in or 0), int(tokens_out or 0)


__all__ = ("OpenAIProvider", "RETRYABLE_STATUS")

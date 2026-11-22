"""Provider Google Gemini — hiện thực :class:`~kai_core.ai.provider.Provider`.

Dùng REST API qua ``aiohttp`` (bot vốn đã phụ thuộc aiohttp) nên **không cần
thêm thư viện**. Đây là provider dự phòng khi OpenAI timeout/rate-limit
(yêu cầu #37).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp

from .provider import AIRequest, AIResponse, CircuitBreaker, Provider, ProviderError, normalize_history

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class GeminiProvider(Provider):
    """Gọi Gemini ``generateContent`` và chuẩn hoá về :class:`AIResponse`."""

    name = "gemini"

    def __init__(
        self,
        api_key: str | None,
        model: str = "gemini-2.0-flash",
        *,
        timeout: float = 30.0,
        max_history: int = 10,
        breaker: CircuitBreaker | None = None,
        session: Any = None,
    ) -> None:
        super().__init__(breaker=breaker)
        self.api_key = api_key
        self._model = model
        self.timeout = float(timeout)
        self.max_history = int(max_history)
        self._session = session

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def model(self) -> str:
        return self._model

    # ------------------------------------------------------------------
    def _build_payload(self, request: AIRequest) -> dict[str, Any]:
        """Gemini dùng ``contents`` (role user/model) thay vì messages."""
        contents: list[dict[str, Any]] = []
        for message in normalize_history(request.history, self.max_history):
            role = "model" if message.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message.content}]})
        contents.append({"role": "user", "parts": [{"text": request.prompt}]})

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": request.max_output_tokens,
                "temperature": request.temperature,
            },
        }
        if request.system:
            payload["systemInstruction"] = {"parts": [{"text": request.system}]}
        return payload

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderError("gemini", "Gemini không trả về nội dung.", retryable=False)
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts).strip()
        if not text:
            raise ProviderError("gemini", "Nội dung Gemini rỗng.", retryable=False)
        return text

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> tuple[int, int]:
        usage = data.get("usageMetadata") or {}
        return int(usage.get("promptTokenCount", 0)), int(usage.get("candidatesTokenCount", 0))

    async def complete(self, request: AIRequest) -> AIResponse:
        """Gửi request tới Gemini và trả về kết quả chuẩn hoá."""
        started = time.perf_counter()
        url = f"{BASE_URL}/{self._model}:generateContent"
        params = {"key": self.api_key or ""}
        payload = self._build_payload(request)

        try:
            if self._session is not None:
                data = await self._post(self._session, url, params, payload)
            else:
                timeout = aiohttp.ClientTimeout(total=self.timeout)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    data = await self._post(session, url, params, payload)
            text = self._extract_text(data)
            tokens_in, tokens_out = self._extract_usage(data)
        except ProviderError:
            raise
        except aiohttp.ClientError as exc:
            raise ProviderError(self.name, f"aiohttp: {exc}", retryable=True) from exc
        except asyncio.TimeoutError as exc:  # noqa: UP041 - tương thích 3.10
            raise ProviderError(self.name, "Gemini timeout.", retryable=True) from exc
        except (ValueError, KeyError) as exc:
            raise ProviderError(self.name, f"Dữ liệu Gemini lỗi: {exc}", retryable=False) from exc

        return AIResponse(
            text=text,
            provider=self.name,
            model=self._model,
            latency=time.perf_counter() - started,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    @staticmethod
    async def _post(session, url: str, params: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        async with session.post(url, params=params, json=payload) as response:
            text = await response.text()
            if response.status >= 400:
                retryable = response.status in RETRYABLE_STATUS
                raise ProviderError(
                    "gemini", f"HTTP {response.status}: {text[:200]}", retryable=retryable
                )
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProviderError("gemini", "Phản hồi Gemini không phải JSON.", retryable=True) from exc


__all__ = ("GeminiProvider", "BASE_URL")

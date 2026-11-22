from __future__ import annotations

from types import SimpleNamespace

import discord

from main import _is_cloudflare_rate_limit


def _http_error(status: int, text: str) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="Too Many Requests")
    return discord.HTTPException(response, text)


def test_detects_cloudflare_error_1015() -> None:
    error = _http_error(429, "<html><title>Cloudflare</title><h1>Error 1015</h1></html>")

    assert _is_cloudflare_rate_limit(error)


def test_does_not_classify_other_http_errors_as_cloudflare_rate_limit() -> None:
    assert not _is_cloudflare_rate_limit(_http_error(429, "rate limited"))
    assert not _is_cloudflare_rate_limit(_http_error(403, "Cloudflare Error 1015"))

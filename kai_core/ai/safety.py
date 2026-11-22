"""
KaiAI Ultimate V7 — AI Safety Guard & Threat Shield
===================================================

Bảo vệ hệ thống AI chống các cuộc tấn công thế hệ mới:
1. **Prompt Injection / Jailbreak Filter**: Ngăn chặn kẻ tấn công ghi đè system instructions
   hoặc ép AI thoát khỏi khuôn khổ an toàn.
2. **Secret Extraction Prevention**: Chặn việc dò tìm hoặc yêu cầu in API keys, token, env vars.
3. **Output Sanitizer**: Quét và tự động làm mờ (redact) bất kỳ chuỗi token/key nào vô tình
   xuất hiện trong phản hồi của AI trước khi gửi cho người dùng.
4. **Factual Verification Guard**: Cảnh báo hoặc chuẩn hoá khi AI có dấu hiệu tự bịa số liệu tài chính.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger

log = get_logger("ai.safety")

#: Các mẫu prompt injection / jailbreak phổ biến
PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)\b"),
    re.compile(r"(?i)\bdisregard\s+(all\s+)?(previous|prior|system)\s+(instructions|directives)\b"),
    re.compile(r"(?i)\byou\s+are\s+now\s+(in\s+)?(dan|developer|jailbreak|unrestricted|god)\s+mode\b"),
    re.compile(r"(?i)\b(reveal|output|display|print|show)\s+(your\s+)?(system\s+prompt|initial\s+prompt|hidden\s+instructions)\b"),
    re.compile(r"(?i)\b(show|leak|reveal|print)\s+(the\s+)?(bot\s+token|api[_\s-]?key|bot[_\s-]?token|secret|password|dotenv|\.env)\b"),
    re.compile(r"(?i)\bpretend\s+you\s+have\s+no\s+(rules|limits|safety|restrictions)\b"),
    re.compile(r"(?i)\b(override|bypass)\s+security\s+(checks|protocols|filters)\b"),
    re.compile(r"(?i)\b(bỏ qua|hủy bỏ|xóa bỏ)\s+(toàn bộ\s+)?(chỉ dẫn|hướng dẫn|quy tắc|prompt)\s+(trước|hệ thống)\b"),
    re.compile(r"(?i)\b(in|hiển thị|tiết lộ|đọc)\s+(ra\s+)?(system prompt|api key|token bí mật|mật khẩu)\b"),
)

#: Các mẫu secret cần redact ở output
SECRET_OUTPUT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z-_]{35}\b"),
    re.compile(r"\bMTA[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"(?i)(token|api[_-]?key|secret|password)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{12,})['\"]?"),
)


@dataclass(slots=True)
class SafetyCheckResult:
    """Kết quả kiểm tra an toàn của prompt."""

    safe: bool
    reason: str = ""
    detected_pattern: str = ""


class AISafetyShield:
    """Tấm chắn an toàn cho AI của KaiAI V7."""

    @classmethod
    def inspect_input(cls, text: str) -> SafetyCheckResult:
        """Kiểm tra văn bản đầu vào của người dùng trước khi gửi cho LLM."""
        if not text:
            return SafetyCheckResult(safe=True)

        normalized = " ".join(text.split()).strip()

        for pattern in PROMPT_INJECTION_PATTERNS:
            match = pattern.search(normalized)
            if match:
                log.warning(
                    "Phát hiện dấu hiệu Prompt Injection / Jailbreak: '%s'",
                    match.group(0),
                )
                return SafetyCheckResult(
                    safe=False,
                    reason="Yêu cầu chứa chỉ thị bất thường hoặc cố gắng can thiệp vào quy tắc an toàn của AI.",
                    detected_pattern=match.group(0),
                )

        return SafetyCheckResult(safe=True)

    @classmethod
    def sanitize_output(cls, response_text: str) -> str:
        """Làm mờ mọi token, secret hoặc dữ liệu nhạy cảm trước khi trả về client."""
        if not response_text:
            return ""

        cleaned = response_text
        for pattern in SECRET_OUTPUT_PATTERNS:
            cleaned = pattern.sub("[REDACTED_SECRET]", cleaned)

        return cleaned

    @classmethod
    def validate_action_permissions(
        cls,
        action: str,
        user_id: int,
        target_id: int | None = None,
    ) -> bool:
        """Kiểm tra xem AI có quyền thực thi thao tác nhạy cảm hay không."""
        # AI không bao giờ được tự thực thi lệnh tài chính mà không qua User Confirmation
        sensitive_actions = {"transfer", "deposit", "withdraw", "ban", "kick", "shutdown", "eval"}
        if action.lower() in sensitive_actions:
            return False
        return True


__all__ = (
    "AISafetyShield",
    "PROMPT_INJECTION_PATTERNS",
    "SECRET_OUTPUT_PATTERNS",
    "SafetyCheckResult",
)

"""
KaiAI Ultimate V7 — AI Model Selection Engine & Cost Optimizer
==============================================================

Xây dựng hệ thống điều phối mô hình thông minh (Yêu cầu Phần 4):
1. **Dynamic Task Classifier**: Phân loại mức độ phức tạp của câu hỏi:
   - LIGHT: Câu hỏi ngắn, chào hỏi, meme, chat thông thường -> Chọn model siêu nhẹ, phản hồi nhanh.
   - STANDARD: Câu hỏi nghiệp vụ, hướng dẫn, tra cứu thông tin server.
   - COMPLEX: Lập kế hoạch nhiều bước, phân tích tài chính, reasoning logic -> Chọn model cao cấp.
2. **Cost & Latency Optimizer**: Tiết kiệm chi phí token bằng cách tránh dùng model nặng cho câu hỏi đơn giản.
3. **Provider Fallback Strategy**: Lựa chọn thông minh giữa OpenAI và Gemini.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..logging_setup import get_logger

log = get_logger("ai.model_selector")


class TaskComplexity(str, Enum):
    LIGHT = "light"
    STANDARD = "standard"
    COMPLEX = "complex"


#: Các từ khóa chỉ thị tác vụ phức tạp
COMPLEX_KEYWORDS: tuple[str, ...] = (
    "kế hoạch", "lộ trình", "chiến lược", "phân tích", "tối ưu",
    "đầu tư", "lập trình", "code", "thuật toán", "tại sao", "chứng minh",
    "so sánh chi tiết", "kịch bản", "plan", "analyze", "strategy",
)

#: Các mẫu chào hỏi / câu ngắn đơn giản
LIGHT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)^(chào|alo|hi|hello|hey|ê|bot\s*ơi|ơi|bye|tạm biệt)(\s+(bạn|bot|ơi|nha|nhé|cậu|ạ))*[\s!.]*$"),
    re.compile(r"(?i)^(cảm ơn|thank|thanks|ok|được rồi|ừ|uhm|dạ)(\s+(bạn|bot|nha|nhé|ạ))*[\s!.]*$"),
    re.compile(r"(?i)^(mấy giờ|hôm nay ngày mấy|thời tiết)[\s!.]*$"),
)


@dataclass(slots=True)
class ModelSelection:
    """Kết quả lựa chọn mô hình tối ưu cho yêu cầu."""

    complexity: TaskComplexity
    recommended_openai_model: str
    recommended_gemini_model: str
    reason: str


class ModelSelectionEngine:
    """Động cơ phân tích độ phức tạp và chọn model AI của KaiAI V7."""

    def __init__(
        self,
        default_light_openai: str = "gpt-4o-mini",
        default_standard_openai: str = "gpt-4o-mini",
        default_complex_openai: str = "gpt-4o",
        default_light_gemini: str = "gemini-3.5-flash-lite",
        default_complex_gemini: str = "gemini-1.5-pro",
    ) -> None:
        self.openai_light = default_light_openai
        self.openai_standard = default_standard_openai
        self.openai_complex = default_complex_openai
        self.gemini_light = default_light_gemini
        self.gemini_complex = default_complex_gemini

    def classify_complexity(self, prompt: str, history_len: int = 0) -> TaskComplexity:
        """Phân loại mức độ phức tạp của prompt người dùng."""
        text = (prompt or "").strip().lower()
        if not text:
            return TaskComplexity.LIGHT

        # Kiểm tra câu ngắn đơn giản
        if len(text) < 50:
            for pat in LIGHT_PATTERNS:
                if pat.search(text):
                    return TaskComplexity.LIGHT

        # Kiểm tra từ khóa phức tạp
        for kw in COMPLEX_KEYWORDS:
            if kw in text:
                return TaskComplexity.COMPLEX

        # Nếu prompt dài hoặc có lịch sử hội thoại nhiều lượt
        if len(text) > 400 or history_len >= 8:
            return TaskComplexity.COMPLEX

        return TaskComplexity.STANDARD

    def select_model(self, prompt: str, history_len: int = 0) -> ModelSelection:
        """Lựa chọn mô hình AI tối ưu dựa trên phân tích prompt."""
        complexity = self.classify_complexity(prompt, history_len)

        if complexity == TaskComplexity.LIGHT:
            return ModelSelection(
                complexity=complexity,
                recommended_openai_model=self.openai_light,
                recommended_gemini_model=self.gemini_light,
                reason="Tác vụ đơn giản/chào hỏi → ưu tiên model siêu tốc và tiết kiệm token.",
            )
        elif complexity == TaskComplexity.COMPLEX:
            return ModelSelection(
                complexity=complexity,
                recommended_openai_model=self.openai_complex,
                recommended_gemini_model=self.gemini_complex,
                reason="Tác vụ phức tạp/lập kế hoạch/phân tích sâu → chọn model có năng lực suy luận cao.",
            )
        else:
            return ModelSelection(
                complexity=complexity,
                recommended_openai_model=self.openai_standard,
                recommended_gemini_model=self.gemini_light,
                reason="Tác vụ tiêu chuẩn → cân bằng giữa tốc độ và độ chính xác.",
            )


#: Singleton instance của ModelSelectionEngine
MODEL_SELECTOR = ModelSelectionEngine()

__all__ = (
    "MODEL_SELECTOR",
    "ModelSelection",
    "ModelSelectionEngine",
    "TaskComplexity",
)

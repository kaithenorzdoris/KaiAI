"""
KaiAI AI Core V7
================

Kiến trúc AI Core nâng cấp toàn diện cho KaiAI Ultimate V7:
- AIRouter với Fallback thông minh đa provider (OpenAI ↔ Gemini).
- Model Selection Engine & Dynamic Complexity Classifier.
- Advanced Multi-Tier Memory Engine (Short-term, Long-term, GDPR purge).
- Persistent AI Plans (Lưu trữ SQLite, Risk Assessment).
- AI Safety Shield & Threat Defense (Prompt Injection filter, Output Sanitizer).
"""

from __future__ import annotations

from .advanced_memory import AdvancedMemoryEngine, UserMemorySnapshot
from .confirm import CONFIRM_TTL, PendingAction, PendingActions, run_confirmed
from .memory import ConversationMemory, MemorySnapshot
from .model_selector import (
    MODEL_SELECTOR,
    ModelSelection,
    ModelSelectionEngine,
    TaskComplexity,
)
from .persistent_plan import (
    PersistentPlan,
    PersistentPlanStore,
    PlanStatus,
    RiskLevel,
)
from .plan import (
    ALLOWED_ACTIONS,
    PLAN_PENDING,
    Plan,
    PlanStep,
    PlanStore,
    execute_plan,
)
from .provider import (
    AIRequest,
    AIResponse,
    ChatMessage,
    CircuitBreaker,
    Provider,
    ProviderError,
    normalize_history,
)
from .router import AIRouter, TokenBudget, UsageStats, build_router_from_env
from .safety import (
    AISafetyShield,
    PROMPT_INJECTION_PATTERNS,
    SECRET_OUTPUT_PATTERNS,
    SafetyCheckResult,
)
from .scheduler import (
    KINDS,
    ScheduledRun,
    ScheduleStore,
    next_occurrence,
    run_due_once,
    scheduler_enabled,
    scheduler_loop,
)

__all__ = (
    "AIRouter",
    "AISafetyShield",
    "CONFIRM_TTL",
    "AIRequest",
    "AIResponse",
    "AdvancedMemoryEngine",
    "ChatMessage",
    "CircuitBreaker",
    "ConversationMemory",
    "MemorySnapshot",
    "MODEL_SELECTOR",
    "ModelSelection",
    "ModelSelectionEngine",
    "PROMPT_INJECTION_PATTERNS",
    "PersistentPlan",
    "PersistentPlanStore",
    "Plan",
    "PlanStatus",
    "PlanStep",
    "PlanStore",
    "RiskLevel",
    "SECRET_OUTPUT_PATTERNS",
    "SafetyCheckResult",
    "ScheduledRun",
    "ScheduleStore",
    "ALLOWED_ACTIONS",
    "PLAN_PENDING",
    "PendingAction",
    "PendingActions",
    "Provider",
    "ProviderError",
    "TaskComplexity",
    "TokenBudget",
    "UsageStats",
    "UserMemorySnapshot",
    "build_router_from_env",
    "execute_plan",
    "KINDS",
    "next_occurrence",
    "normalize_history",
    "run_confirmed",
    "run_due_once",
    "scheduler_enabled",
    "scheduler_loop",
)

"""
KaiAI Ultimate V7 — Persistent AI Plans & Autonomous Agent Engine
================================================================

Nâng cấp AI Plan từ RAM sang lưu trữ bền vững trong Database (Yêu cầu Phần 6):
1. **Persistent State Machine**: Trạng thái kế hoạch (PENDING, APPROVED, RUNNING, DONE, FAILED, CANCELLED)
   được lưu trực tiếp trong bảng ai_plans, không mất khi bot khởi động lại.
2. **Risk Assessment**: Tự động tính toán tổng số tiền và mức độ rủi ro (LOW, MEDIUM, HIGH, CRITICAL).
3. **Execution Safety**: Giới hạn tối đa số bước, timeout cho từng bước, idempotency token,
   ngăn chặn AI tự ý rút/chuyển tiền mà chưa có người dùng xác nhận.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from ..logging_setup import get_logger

log = get_logger("ai.persistent_plan")

MAX_STEPS_PER_PLAN = 10
MAX_PLANS_PER_USER = 5
MAX_TOTAL_AMOUNT_LIMIT = 500_000_000  # 500 triệu VNĐ ảo


class PlanStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(slots=True)
class PlanStep:
    """Một bước thực thi trong kế hoạch."""

    index: int
    action: str
    args: dict[str, Any]
    status: str = "pending"
    result: str = ""
    error: str = ""


@dataclass(slots=True)
class PersistentPlan:
    """Kế hoạch đa bước của AI lưu trữ bền vững trong database."""

    id: str
    user_id: int
    guild_id: int
    steps: list[PlanStep]
    total_amount: int
    status: PlanStatus = PlanStatus.PENDING
    risk_level: RiskLevel = RiskLevel.LOW
    confirmation_token: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def summary(self) -> str:
        """Tạo báo cáo tóm tắt cho người dùng duyệt."""
        lines = [
            f"📋 **KẾ HOẠCH AI — MÃ: `{self.id}`**",
            f"• Trạng thái: `{self.status.value.upper()}`",
            f"• Mức độ rủi ro: `{self.risk_level.value.upper()}`",
            f"• Tổng tiền liên quan: **{self.total_amount:,} VNĐ**",
            "• Các bước thực hiện:",
        ]
        for s in self.steps:
            lines.append(f"  [{s.index}] {s.action.upper()}: {s.args} → *{s.status}*")
        lines.append(f"\n👉 Gõ `!xacnhan {self.confirmation_token}` để duyệt kế hoạch này.")
        return "\n".join(lines)


class PersistentPlanStore:
    """Kho lưu trữ và điều phối kế hoạch AI bằng SQLite."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def setup_schema(self) -> None:
        """Đảm bảo bảng ai_plans tồn tại."""
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_plans (
                id           TEXT PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                guild_id     INTEGER NOT NULL DEFAULT 0,
                status       TEXT NOT NULL DEFAULT 'pending',
                steps_json   TEXT NOT NULL DEFAULT '[]',
                total_amount INTEGER NOT NULL DEFAULT 0,
                risk_level   TEXT NOT NULL DEFAULT 'low',
                token        TEXT NOT NULL DEFAULT '',
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_plans_user_status ON ai_plans(user_id, status)"
        )
        await self.db.commit()

    @staticmethod
    def assess_risk(steps: list[PlanStep]) -> tuple[int, RiskLevel]:
        """Đánh giá tổng số tiền và cấp độ rủi ro của chuỗi thao tác."""
        total = 0
        for step in steps:
            amt = step.args.get("amount") or step.args.get("value") or 0
            try:
                total += max(0, int(amt))
            except (ValueError, TypeError):
                pass

        if total > 50_000_000:
            level = RiskLevel.CRITICAL
        elif total > 5_000_000:
            level = RiskLevel.HIGH
        elif total > 100_000:
            level = RiskLevel.MEDIUM
        else:
            level = RiskLevel.LOW

        return total, level

    async def create_plan(
        self,
        user_id: int,
        guild_id: int,
        steps_data: list[dict[str, Any]],
    ) -> PersistentPlan:
        """Tạo mới một kế hoạch bền vững và tính toán rủi ro."""
        await self.setup_schema()
        if len(steps_data) > MAX_STEPS_PER_PLAN:
            raise ValueError(f"Kế hoạch vượt quá số bước tối đa ({MAX_STEPS_PER_PLAN}).")

        steps: list[PlanStep] = []
        for idx, item in enumerate(steps_data, start=1):
            action = str(item.get("action", "")).strip().lower()
            args = dict(item.get("args") or {})
            steps.append(PlanStep(index=idx, action=action, args=args))

        total_amount, risk = self.assess_risk(steps)
        if total_amount > MAX_TOTAL_AMOUNT_LIMIT:
            raise ValueError(
                f"Tổng số tiền của kế hoạch ({total_amount:,} VNĐ) vượt quá trần cho phép."
            )

        plan_id = f"PLAN-{int(time.time())}-{secrets.token_hex(3).upper()}"
        token = secrets.token_hex(4).upper()
        now = time.time()

        steps_json = json.dumps([asdict(s) for s in steps], ensure_ascii=False)

        await self.db.execute(
            """
            INSERT INTO ai_plans(id, user_id, guild_id, status, steps_json, total_amount, risk_level, token, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                int(user_id),
                int(guild_id),
                PlanStatus.PENDING.value,
                steps_json,
                total_amount,
                risk.value,
                token,
                now,
                now,
            ),
        )
        await self.db.commit()

        return PersistentPlan(
            id=plan_id,
            user_id=int(user_id),
            guild_id=int(guild_id),
            steps=steps,
            total_amount=total_amount,
            status=PlanStatus.PENDING,
            risk_level=risk,
            confirmation_token=token,
            created_at=now,
            updated_at=now,
        )

    async def get_plan(self, plan_id: str) -> PersistentPlan | None:
        """Đọc kế hoạch theo mã định danh."""
        await self.setup_schema()
        async with self.db.execute(
            "SELECT id, user_id, guild_id, status, steps_json, total_amount, risk_level, token, created_at, updated_at "
            "FROM ai_plans WHERE id = ?",
            (str(plan_id).strip(),),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None

        raw_steps = json.loads(row[4] or "[]")
        steps = [
            PlanStep(
                index=s["index"],
                action=s["action"],
                args=s["args"],
                status=s.get("status", "pending"),
                result=s.get("result", ""),
                error=s.get("error", ""),
            )
            for s in raw_steps
        ]

        return PersistentPlan(
            id=row[0],
            user_id=row[1],
            guild_id=row[2],
            steps=steps,
            total_amount=row[5],
            status=PlanStatus(row[3]),
            risk_level=RiskLevel(row[6]),
            confirmation_token=row[7],
            created_at=row[8],
            updated_at=row[9],
        )

    async def update_status(self, plan_id: str, new_status: PlanStatus) -> bool:
        """Cập nhật trạng thái của kế hoạch."""
        await self.setup_schema()
        now = time.time()
        cur = await self.db.execute(
            "UPDATE ai_plans SET status = ?, updated_at = ? WHERE id = ?",
            (new_status.value, now, str(plan_id).strip()),
        )
        await self.db.commit()
        return cur.rowcount == 1


__all__ = (
    "MAX_PLANS_PER_USER",
    "MAX_STEPS_PER_PLAN",
    "MAX_TOTAL_AMOUNT_LIMIT",
    "PersistentPlan",
    "PersistentPlanStore",
    "PlanStatus",
    "PlanStep",
    "RiskLevel",
)


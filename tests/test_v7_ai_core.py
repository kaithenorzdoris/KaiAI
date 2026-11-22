"""
Unit & Integration Tests for KaiAI Ultimate V7 AI Core:
- Advanced Multi-Tier Memory Engine & GDPR Purge
- Persistent AI Plans & Risk Assessment
- Model Selection Engine & Dynamic Complexity Classifier
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

import aiosqlite

from kai_core.ai.advanced_memory import AdvancedMemoryEngine
from kai_core.ai.model_selector import (
    MODEL_SELECTOR,
    ModelSelectionEngine,
    TaskComplexity,
)
from kai_core.ai.persistent_plan import (
    PersistentPlanStore,
    PlanStatus,
    RiskLevel,
)


class AdvancedMemoryEngineTests(unittest.IsolatedAsyncioTestCase):
    """Kiểm thử bộ nhớ đa tầng và quyền riêng tư."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self._tmp.name) / "test_mem.db")
        self.db = await aiosqlite.connect(self.db_path)
        self.engine = AdvancedMemoryEngine(self.db)
        await self.engine.setup_schema()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_record_turn_and_load(self) -> None:
        await self.engine.record_turn(100, 10, "user", "Chào bạn, tôi là Nam")
        await self.engine.record_turn(100, 10, "assistant", "Chào bạn Nam!")

        snap = await self.engine.load_memory(100, 10)
        self.assertEqual(len(snap.recent_messages), 2)
        self.assertEqual(snap.recent_messages[0].role, "user")
        self.assertEqual(snap.recent_messages[0].content, "Chào bạn, tôi là Nam")

    async def test_add_fact_and_to_context(self) -> None:
        await self.engine.add_fact(100, 10, "Thích chơi game câu cá")
        await self.engine.add_fact(100, 10, "Là giám đốc công ty VinaTech")

        snap = await self.engine.load_memory(100, 10)
        self.assertEqual(len(snap.facts), 2)
        ctx = snap.to_system_context()
        self.assertIn("Thích chơi game câu cá", ctx)
        self.assertIn("VinaTech", ctx)

    async def test_purge_memory_gdpr(self) -> None:
        await self.engine.record_turn(100, 10, "user", "Tin nhắn bí mật")
        await self.engine.add_fact(100, 10, "Fact nhạy cảm")

        deleted = await self.engine.purge_memory(100)
        self.assertGreater(deleted, 0)

        snap = await self.engine.load_memory(100, 10)
        self.assertEqual(len(snap.recent_messages), 0)
        self.assertEqual(len(snap.facts), 0)


class PersistentPlanStoreTests(unittest.IsolatedAsyncioTestCase):
    """Kiểm thử lưu trữ kế hoạch AI bền vững và đánh giá rủi ro."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(pathlib.Path(self._tmp.name) / "test_plan.db")
        self.db = await aiosqlite.connect(self.db_path)
        self.store = PersistentPlanStore(self.db)
        await self.store.setup_schema()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_create_plan_with_risk_assessment(self) -> None:
        steps = [
            {"action": "deposit", "args": {"user_id": 1, "amount": 2_000_000}},
            {"action": "transfer", "args": {"sender": 1, "receiver": 2, "amount": 4_000_000}},
        ]
        plan = await self.store.create_plan(1, 10, steps)
        self.assertIsNotNone(plan.id)
        self.assertEqual(plan.total_amount, 6_000_000)
        self.assertEqual(plan.risk_level, RiskLevel.HIGH)
        self.assertEqual(plan.status, PlanStatus.PENDING)

        loaded = await self.store.get_plan(plan.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.total_amount, 6_000_000)
        self.assertEqual(len(loaded.steps), 2)

    async def test_update_status(self) -> None:
        steps = [{"action": "withdraw", "args": {"user_id": 1, "amount": 50_000}}]
        plan = await self.store.create_plan(1, 10, steps)
        self.assertEqual(plan.risk_level, RiskLevel.LOW)

        ok = await self.store.update_status(plan.id, PlanStatus.DONE)
        self.assertTrue(ok)
        loaded = await self.store.get_plan(plan.id)
        self.assertEqual(loaded.status, PlanStatus.DONE)


class ModelSelectionEngineTests(unittest.TestCase):
    """Kiểm thử phân loại độ phức tạp và chọn model."""

    def setUp(self) -> None:
        self.selector = ModelSelectionEngine()

    def test_classify_light_greeting(self) -> None:
        comp = self.selector.classify_complexity("Chào bot ơi!")
        self.assertEqual(comp, TaskComplexity.LIGHT)
        sel = self.selector.select_model("Chào bot ơi!")
        self.assertEqual(sel.complexity, TaskComplexity.LIGHT)
        self.assertEqual(sel.recommended_openai_model, "gpt-4o-mini")

    def test_classify_complex_analysis(self) -> None:
        prompt = "Hãy lập cho tôi một kế hoạch và chiến lược đầu tư cổ phiếu chi tiết nhất."
        comp = self.selector.classify_complexity(prompt)
        self.assertEqual(comp, TaskComplexity.COMPLEX)
        sel = self.selector.select_model(prompt)
        self.assertEqual(sel.complexity, TaskComplexity.COMPLEX)
        self.assertEqual(sel.recommended_openai_model, "gpt-4o")


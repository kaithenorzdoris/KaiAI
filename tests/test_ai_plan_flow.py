"""Tích hợp lộ trình tự động của AI trên DB thật.

Khác :mod:`test_ai_plan` (kiểm tra engine với runner giả), file này chạy
lộ trình qua **đúng lệnh của bot** (``!chaykehoach``…) với ``Games`` thật,
nên tiền được chuyển thật và phải khớp từng bước.
"""

from __future__ import annotations

import pathlib
import tempfile
import unittest

import aiosqlite

from cogs.ai_chat import AIChat
from cogs.games import Games


class _Member:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.display_name = f"u{uid}"
        self.mention = f"<@{uid}>"


class _Ctx:
    def __init__(self, uid: int) -> None:
        self.author = _Member(uid)
        self.sent: list = []

    async def send(self, content=None, embed=None, **kwargs):
        self.sent.append(content if content is not None else embed)

    async def reply(self, content=None, embed=None, **kwargs):
        return await self.send(content, embed, **kwargs)

    @property
    def last(self) -> str:
        return self.sent[-1]


class _Bot:
    def __init__(self, cogs: dict) -> None:
        self._cogs = cogs

    def get_cog(self, name):
        return self._cogs.get(name)


class PlanFlowTests(unittest.IsolatedAsyncioTestCase):
    """Lo trinh nhieu buoc: xem → chay thu → duyet → kiem tra tien."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.games = Games(bot=None)
        self.games.db_path = str(pathlib.Path(self._tmp.name) / "e.db")
        await self.games.setup_db()
        async with aiosqlite.connect(self.games.db_path) as db:
            await db.execute(
                "INSERT INTO users(user_id,balance,bank) VALUES(1,5000000,0)")
            await db.execute(
                "INSERT INTO users(user_id,balance,bank) VALUES(2,0,0)")
            await db.commit()
        self.cog = AIChat(_Bot({"Games": self.games}))
        for cmd in self.cog.__cog_commands__:
            cmd.cog = self.cog
            if cmd.__dict__.get("_cooldown") is not None:
                cmd.__dict__["_cooldown"] = None
        task = getattr(self.cog, "_cleanup_task", None)
        if task is not None:
            task.cancel()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _balance(self, uid: int = 1) -> int:
        async with aiosqlite.connect(self.games.db_path) as db:
            async with db.execute(
                "SELECT balance FROM users WHERE user_id=?", (uid,)
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _bank(self, uid: int = 1) -> int:
        async with aiosqlite.connect(self.games.db_path) as db:
            async with db.execute(
                "SELECT bank FROM users WHERE user_id=?", (uid,)
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _plan(self, steps, title="Tiết kiệm + hỗ trợ"):
        return await self.cog.propose_plan(
            user_id=1, title=title, steps=steps)

    async def test_propose_plan_does_not_move_money(self) -> None:
        plan = await self._plan([
            {"action": "deposit", "args": {"user_id": 1, "amount": 500_000}},
        ])
        self.assertIsNotNone(plan)
        self.assertEqual(await self._balance(1), 5_000_000,
                         "De xuat khong duoc tru tien")
        self.assertEqual(await self._bank(1), 0)

    async def test_invalid_step_rejected_with_reason(self) -> None:
        plan = await self._plan([
            {"action": "transfer",
             "args": {"sender": 1, "receiver": 1, "amount": 100}},
        ])
        self.assertIsNone(plan, "Chuyen cho chinh minh phai bi tu choi")
        self.assertIn("chính mình", self.cog.plans.last_error)

    async def test_list_and_show_plan(self) -> None:
        plan = await self._plan([
            {"action": "deposit", "args": {"user_id": 1, "amount": 500_000}},
        ])
        ctx = _Ctx(1)
        await self.cog.list_plans(ctx)
        self.assertIn(plan.token, str(ctx.last))
        ctx = _Ctx(1)
        await self.cog.show_plan(ctx, plan.token)
        self.assertIn("deposit", str(ctx.last))
        self.assertIn("500.000", str(ctx.last))

    async def test_show_plan_of_other_user_rejected(self) -> None:
        plan = await self._plan([
            {"action": "deposit", "args": {"user_id": 1, "amount": 1000}}])
        ctx = _Ctx(999)
        await self.cog.show_plan(ctx, plan.token)
        self.assertIn("❌", str(ctx.last))

    async def test_dry_run_then_approve_moves_money(self) -> None:
        plan = await self._plan([
            {"action": "deposit", "args": {"user_id": 1, "amount": 200_000}},
            {"action": "transfer",
             "args": {"sender": 1, "receiver": 2, "amount": 100_000}},
        ])
        ctx = _Ctx(1)
        await self.cog.run_plan(ctx, plan.token, dry_run=True)
        self.assertIn("không đụng tiền", str(ctx.last).lower())
        self.assertEqual(await self._balance(1), 5_000_000)
        self.assertEqual(await self._balance(2), 0)

        ctx = _Ctx(1)
        await self.cog.run_plan(ctx, plan.token)
        self.assertIn("✅", str(ctx.last))
        self.assertEqual(await self._balance(1), 4_700_000,
                         "Gui 200k vao ngan hang + chuyen 100k => rut 300k")
        self.assertEqual(await self._balance(2), 100_000)
        self.assertEqual(await self._bank(1), 200_000)

    async def test_plan_cannot_run_twice(self) -> None:
        plan = await self._plan([
            {"action": "transfer",
             "args": {"sender": 1, "receiver": 2, "amount": 100_000}}])
        await self.cog.run_plan(_Ctx(1), plan.token)
        ctx = _Ctx(1)
        await self.cog.run_plan(ctx, plan.token)
        self.assertIn("❌", str(ctx.last))
        self.assertEqual(await self._balance(1), 4_900_000,
                         "Khong tru tien hai lan")

    async def test_plan_stops_when_budget_insufficient(self) -> None:
        plan = await self._plan([
            {"action": "transfer",
             "args": {"sender": 1, "receiver": 2, "amount": 50_000_000}},
            {"action": "deposit", "args": {"user_id": 1, "amount": 100_000}},
        ])
        ctx = _Ctx(1)
        await self.cog.run_plan(ctx, plan.token)
        self.assertIn("❌", str(ctx.last))
        self.assertEqual(await self._balance(1), 5_000_000,
                         "Buoc loi khong duoc rut tien")
        self.assertEqual(await self._bank(1), 0,
                         "Buoc sau phai bi bo qua")

    async def test_other_user_cannot_run_plan(self) -> None:
        plan = await self._plan([
            {"action": "transfer",
             "args": {"sender": 1, "receiver": 2, "amount": 100_000}}])
        ctx = _Ctx(999)
        await self.cog.run_plan(ctx, plan.token)
        self.assertIn("❌", str(ctx.last))
        self.assertEqual(await self._balance(1), 5_000_000)

    async def test_cancel_plan(self) -> None:
        plan = await self._plan([
            {"action": "deposit", "args": {"user_id": 1, "amount": 100_000}}])
        ctx = _Ctx(1)
        await self.cog.cancel_plan(ctx, plan.token)
        self.assertIn("hủy", str(ctx.last).lower())
        ctx = _Ctx(1)
        await self.cog.run_plan(ctx, plan.token)
        self.assertIn("❌", str(ctx.last))
        self.assertEqual(await self._bank(1), 0)


if __name__ == "__main__":
    unittest.main()

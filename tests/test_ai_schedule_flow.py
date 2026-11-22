"""Tích hợp lịch tự động: bot tự chạy mà không cần người dùng gõ lệnh.

Khác :mod:`test_ai_scheduler` (runner giả), file này dùng ``Games`` thật và
gọi đúng ``!datlich`` của bot — tiền thật bị chuyển, và phải **đúng một lần**
dù có quét lịch bao nhiêu lần.
"""

from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

import aiosqlite

from cogs.ai_chat import AIChat
from cogs.games import Games
from kai_core.ai.scheduler import run_due_once


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

    def get_user(self, _uid):
        return None


class ScheduleFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.games = Games(bot=None)
        self.games.db_path = str(root / "e.db")
        await self.games.setup_db()
        async with aiosqlite.connect(self.games.db_path) as db:
            await db.execute(
                "INSERT INTO users(user_id,balance,bank) VALUES(1,5000000,0)")
            await db.commit()
        self.cog = AIChat(_Bot({"Games": self.games}))
        self.cog.schedules.db_path = str(root / "sched.db")
        await self.cog.schedules.setup()
        for cmd in self.cog.__cog_commands__:
            cmd.cog = self.cog
            if cmd.__dict__.get("_cooldown") is not None:
                cmd.__dict__["_cooldown"] = None
        task = getattr(self.cog, "_cleanup_task", None)
        if task is not None:
            task.cancel()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _balance(self) -> int:
        async with aiosqlite.connect(self.games.db_path) as db:
            async with db.execute(
                "SELECT balance FROM users WHERE user_id=1") as cur:
                return int((await cur.fetchone())[0])

    async def _bank(self) -> int:
        async with aiosqlite.connect(self.games.db_path) as db:
            async with db.execute(
                "SELECT bank FROM users WHERE user_id=1") as cur:
                return int((await cur.fetchone())[0])

    async def _plan(self):
        return await self.cog.propose_plan(
            user_id=1, title="Tiet kiem hang thang",
            steps=[{"action": "deposit",
                    "args": {"user_id": 1, "amount": 200_000}}])

    async def _force_due(self, schedule_id: int) -> None:
        async with aiosqlite.connect(self.cog.schedules.db_path) as db:
            await db.execute(
                "UPDATE ai_schedules SET next_run_at=? WHERE id=?",
                (time.time() - 1, int(schedule_id)))
            await db.commit()

    async def test_set_schedule_requires_plan(self) -> None:
        ctx = _Ctx(1)
        await self.cog.set_schedule(ctx, "monthly", 9, 0)
        self.assertIn("chưa có lộ trình", str(ctx.last).lower())
        self.assertEqual(await self.cog.schedules.list_for(1), [])

    async def test_set_schedule_creates_one(self) -> None:
        await self._plan()
        ctx = _Ctx(1)
        await self.cog.set_schedule(ctx, "monthly", 9, 30)
        self.assertIn("lịch", str(ctx.last).lower())
        runs = await self.cog.schedules.list_for(1)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].kind, "monthly")
        self.assertEqual(runs[0].total, 200_000)
        self.assertEqual(await self._balance(), 5_000_000,
                         "Dat lich khong duoc dung tien")

    async def test_viet_alias(self) -> None:
        await self._plan()
        ctx = _Ctx(1)
        await self.cog.set_schedule(ctx, "thang", 9, 0)
        self.assertEqual(len(await self.cog.schedules.list_for(1)), 1)

    async def test_schedule_runs_and_moves_money_once(self) -> None:
        await self._plan()
        await self.cog.set_schedule(_Ctx(1), "monthly", 9, 0)
        run = (await self.cog.schedules.list_for(1))[0]
        await self._force_due(run.id)
        results = await run_due_once(
            self.cog.schedules, self.cog._plan_runner)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][1])
        self.assertEqual(await self._balance(), 4_800_000)
        self.assertEqual(await self._bank(), 200_000)
        for _ in range(3):
            await run_due_once(self.cog.schedules, self.cog._plan_runner)
        self.assertEqual(await self._balance(), 4_800_000,
                         "Khong duoc chay trung lich")
        self.assertEqual(await self._bank(), 200_000)

    async def test_cancel_schedule(self) -> None:
        await self._plan()
        await self.cog.set_schedule(_Ctx(1), "daily", 9, 0)
        run = (await self.cog.schedules.list_for(1))[0]
        ctx = _Ctx(1)
        await self.cog.cancel_schedule(ctx, run.id)
        self.assertIn("hủy", str(ctx.last).lower())
        self.assertEqual(await self.cog.schedules.list_for(1), [])

    async def test_other_user_cannot_cancel(self) -> None:
        await self._plan()
        await self.cog.set_schedule(_Ctx(1), "daily", 9, 0)
        run = (await self.cog.schedules.list_for(1))[0]
        ctx = _Ctx(999)
        await self.cog.cancel_schedule(ctx, run.id)
        self.assertIn("\u274c", str(ctx.last))
        self.assertEqual(len(await self.cog.schedules.list_for(1)), 1)

    async def test_list_schedules_shows_amount(self) -> None:
        await self._plan()
        await self.cog.set_schedule(_Ctx(1), "daily", 7, 15)
        ctx = _Ctx(1)
        await self.cog.list_schedules(ctx)
        self.assertIn("200.000", str(ctx.last))

    async def test_survives_store_reopen(self) -> None:
        from kai_core.ai.scheduler import ScheduleStore

        await self._plan()
        await self.cog.set_schedule(_Ctx(1), "monthly", 9, 0)
        fresh = ScheduleStore(self.cog.schedules.db_path)
        self.assertEqual(len(await fresh.list_for(1)), 1,
                         "Lich phai con sau khi khoi dong lai bot")


if __name__ == "__main__":
    unittest.main()

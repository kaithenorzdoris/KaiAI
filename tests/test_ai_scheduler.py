"""Kiểm thử bộ chạy lộ trình tự động theo lịch.

Trọng tâm là ba chốt chống tràn tiền: **claim nguyên tử** (không chạy
trùng), **không bù nợ** (bot offline lâu chỉ chạy một kỳ), và **tự tắt**
lịch khi hỏng nhiều lần.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import tempfile
import time
import unittest

from kai_core.ai.scheduler import (
    MAX_CONSECUTIVE_FAILURES,
    ScheduleStore,
    next_occurrence,
    run_due_once,
    scheduler_enabled,
)


def _ok(text: str = "OK"):
    class _R:
        ok = True
        message = text
    return _R()


def _bad(text: str = "khong du tien"):
    class _R:
        ok = False
        message = text
    return _R()


class _Recorder:
    def __init__(self, script=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.script = script or {}

    async def __call__(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        out = self.script.get(action)
        if callable(out):
            return out(kwargs)
        return out if out is not None else _ok()


class NextOccurrenceTests(unittest.TestCase):
    def test_daily_moves_one_day(self) -> None:
        now = dt.datetime(2026, 3, 10, 12, 0)
        nxt = dt.datetime.fromtimestamp(
            next_occurrence("daily", 9, 0, now=now))
        self.assertEqual((nxt.year, nxt.month, nxt.day), (2026, 3, 11))
        self.assertEqual((nxt.hour, nxt.minute), (9, 0))

    def test_weekly_moves_seven_days(self) -> None:
        now = dt.datetime(2026, 3, 10, 12, 0)
        nxt = dt.datetime.fromtimestamp(
            next_occurrence("weekly", 9, 0, now=now))
        self.assertEqual(nxt.day, 17)

    def test_monthly_clamps_to_last_day(self) -> None:
        """Ngày 31 sang tháng 2 phải lùi về ngày cuối, không lỗi."""
        now = dt.datetime(2026, 1, 31, 12, 0)
        nxt = dt.datetime.fromtimestamp(
            next_occurrence("monthly", 9, 0, now=now))
        self.assertEqual((nxt.year, nxt.month), (2026, 2))
        self.assertEqual(nxt.day, 28)

    def test_late_bot_catches_up_only_one_period(self) -> None:
        """Bot offline 5 kỳ ngày -> chạy đúng 1 lần, không dồn 5 lần."""
        now = dt.datetime(2026, 3, 10, 12, 0)
        after = time.time() - 5 * 86400
        nxt = next_occurrence("daily", 9, 0, after=after, now=now)
        self.assertLessEqual(nxt, now.timestamp() + 86400,
                             "Chi bu mot ky, khong cong don")
class RunDueOnceTests(unittest.IsolatedAsyncioTestCase):
    """Vong chay: lay lich den han -> thuc thi -> ghi ket qua."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(
            str(pathlib.Path(self._tmp.name) / "sched.db"))
        await self.store.setup()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _steps(self):
        return [{"action": "deposit",
                 "args": {"user_id": 1, "amount": 200_000}}]

    async def _due_now(self, **kw) -> int:
        import aiosqlite
        run = await self.store.create(
            user_id=kw.pop("user_id", 1), kind="daily",
            steps=self._steps(), **kw)
        async with aiosqlite.connect(self.store.db_path) as db:
            await db.execute(
                "UPDATE ai_schedules SET next_run_at=? WHERE id=?",
                (time.time() - 1, run.id))
            await db.commit()
        return run.id

    async def test_no_due_schedule_runs_nothing(self) -> None:
        await self.store.create(
            user_id=1, kind="daily", steps=self._steps())
        runner = _Recorder()
        self.assertEqual(await run_due_once(self.store, runner), [])
        self.assertEqual(runner.calls, [])

    async def test_runs_due_schedule(self) -> None:
        await self._due_now()
        runner = _Recorder()
        results = await run_due_once(self.store, runner)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][1])
        self.assertEqual([c[0] for c in runner.calls], ["deposit"])
        self.assertIn("✅", results[0][2])

    async def test_failed_step_stops_chain(self) -> None:
        import aiosqlite
        steps = [
            {"action": "deposit", "args": {"user_id": 1, "amount": 100}},
            {"action": "withdraw", "args": {"user_id": 1, "amount": 200}},
        ]
        run = await self.store.create(
            user_id=1, kind="daily", steps=steps)
        async with aiosqlite.connect(self.store.db_path) as db:
            await db.execute(
                "UPDATE ai_schedules SET next_run_at=? WHERE id=?",
                (time.time() - 1, run.id))
            await db.commit()
        runner = _Recorder({"deposit": _bad("het tien")})
        results = await run_due_once(self.store, runner)
        self.assertFalse(results[0][1])
        self.assertEqual(len(runner.calls), 1,
                         "Buoc sau phai bi bo qua khi buoc truoc loi")

    async def test_notifier_receives_report(self) -> None:
        await self._due_now()
        seen: list[tuple] = []

        async def notifier(user_id, ok, report):
            seen.append((user_id, ok))

        await run_due_once(self.store, _Recorder(), notifier=notifier)
        self.assertEqual(seen, [(1, True)])

    async def test_notifier_error_does_not_break_loop(self) -> None:
        await self._due_now()

        async def bad_notifier(*_a):
            raise RuntimeError("het thong")

        results = await run_due_once(
            self.store, _Recorder(), notifier=bad_notifier)
        self.assertEqual(len(results), 1, "Bao cao loi khong duoc lam hong")

    async def test_second_run_does_not_repeat(self) -> None:
        await self._due_now()
        runner = _Recorder()
        await run_due_once(self.store, runner)
        await run_due_once(self.store, runner)
        self.assertEqual(len(runner.calls), 1,
                         "Khong chay trung khi vong lap quet lai")


class ScheduleStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(
            str(pathlib.Path(self._tmp.name) / "sched.db"))
        await self.store.setup()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _steps(self):
        return [{"action": "deposit",
                 "args": {"user_id": 1, "amount": 200_000}}]

    async def _force_due(self, schedule_id: int) -> None:
        import aiosqlite
        async with aiosqlite.connect(self.store.db_path) as db:
            await db.execute(
                "UPDATE ai_schedules SET next_run_at=? WHERE id=?",
                (time.time() - 5, int(schedule_id)))
            await db.commit()

    async def test_create_and_list(self) -> None:
        run = await self.store.create(
            user_id=1, kind="daily", steps=self._steps(),
            title="Tiết kiệm", hour=8, minute=30)
        self.assertIsNotNone(run)
        self.assertEqual(run.kind, "daily")
        self.assertEqual(run.total, 200_000)
        self.assertIn("hằng ngày", run.when_text())
        self.assertEqual(len(await self.store.list_for(1)), 1)

    async def test_rejects_bad_kind_and_clock(self) -> None:
        self.assertIsNone(await self.store.create(
            user_id=1, kind="hourly", steps=self._steps()))
        self.assertIsNone(await self.store.create(
            user_id=1, kind="daily", steps=self._steps(), hour=99))

    async def test_rejects_empty_steps(self) -> None:
        self.assertIsNone(
            await self.store.create(user_id=1, kind="daily", steps=[]))

    async def test_limits_schedules_per_user(self) -> None:
        for _ in range(3):
            self.assertIsNotNone(await self.store.create(
                user_id=1, kind="daily", steps=self._steps()))
        self.assertIsNone(await self.store.create(
            user_id=1, kind="daily", steps=self._steps()))
        self.assertIn("tối đa", self.store.last_error)

    async def test_other_user_sees_nothing(self) -> None:
        await self.store.create(
            user_id=1, kind="daily", steps=self._steps())
        self.assertEqual(await self.store.list_for(999), [])

    async def test_cancel_only_owner(self) -> None:
        run = await self.store.create(
            user_id=1, kind="daily", steps=self._steps())
        self.assertFalse(
            await self.store.cancel(user_id=999, schedule_id=run.id))
        self.assertTrue(
            await self.store.cancel(user_id=1, schedule_id=run.id))
        self.assertEqual(await self.store.list_for(1), [])

    async def test_claim_due_is_atomic(self) -> None:
        """Gọi claim hai lần liên tiếp -> chỉ nhận được một lần."""
        run = await self.store.create(
            user_id=1, kind="daily", steps=self._steps())
        await self._force_due(run.id)
        first = await self.store.claim_due()
        second = await self.store.claim_due()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "Lanh phai chay trung")

    async def test_mark_result_disables_after_repeated_failures(self) -> None:
        run = await self.store.create(
            user_id=1, kind="daily", steps=self._steps())
        await self._force_due(run.id)
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            await self.store.mark_result(run.id, ok=False)
        self.assertEqual(await self.store.list_for(1), [],
                         "Lich hong lien tiep phai tu tat")

    async def test_success_resets_failure_counter(self) -> None:
        run = await self.store.create(
            user_id=1, kind="daily", steps=self._steps())
        await self.store.mark_result(run.id, ok=False)
        await self.store.mark_result(run.id, ok=True)
        self.assertEqual(len(await self.store.list_for(1)), 1)


if __name__ == "__main__":
    unittest.main()
    def test_unknown_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            next_occurrence("hourly", 9, 0)


class EnvToggleTests(unittest.TestCase):
    def test_enabled_by_default(self) -> None:
        self.assertTrue(scheduler_enabled())

    def test_disabled_by_env(self) -> None:
        import os
        old = os.environ.get("AI_SCHEDULER")
        os.environ["AI_SCHEDULER"] = "off"
        try:
            self.assertFalse(scheduler_enabled())
        finally:
            if old is None:
                os.environ.pop("AI_SCHEDULER", None)
            else:
                os.environ["AI_SCHEDULER"] = old
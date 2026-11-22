"""Kiem thu engine lo trinh tu dong (``kai_core.ai.plan``).

Vi AI co the tu chay **nhieu** giao dich, cac test o day tap trung vao cac
chot an toan: danh sach trang hanh dong, tham so phai hop le, han muc,
chay-mot-lan, dung khi buoc loi, va quyen so huu ke hoach.
"""

from __future__ import annotations

import time
import unittest

from kai_core.ai.plan import (
    PLAN_DONE,
    PLAN_FAILED,
    PLAN_PENDING,
    STEP_DONE,
    STEP_FAILED,
    STEP_SKIPPED,
    PlanStore,
    draft_plan_from_text,
    execute_plan,
    mentions_action,
    mentions_plan,
    parse_money,
)


def _ok_result(text: str = "OK"):
    class _R:
        ok = True
        message = text
    return _R()


def _bad_result(text: str = "khong du tien"):
    class _R:
        ok = False
        message = text
    return _R()


class PlanCreationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PlanStore()

    def _step(self, action="deposit", **args):
        base = {"user_id": 1, "amount": 100_000}
        base.update(args)
        return {"action": action, "args": base}



    def test_create_valid_plan(self) -> None:
        plan = self.store.create(
            user_id=1, title="Gửi tiết kiệm",
            steps=[self._step(), self._step("transfer", sender=1, receiver=2)],
        )
        self.assertIsNotNone(plan)
        self.assertTrue(plan.token)
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.total, 200_000)
        self.assertEqual(plan.status, PLAN_PENDING)
        self.assertTrue(plan.is_runnable)


    def test_token_is_random_not_sequential(self) -> None:
        tokens = {
            self.store.create(user_id=1, title="t", steps=[self._step()]).token
            for _ in range(20)
        }
        self.assertEqual(len(tokens), 20)


    def test_unknown_action_rejected(self) -> None:
        plan = self.store.create(
            user_id=1, title="x", steps=[self._step(action="rm_rf")])
        self.assertIsNone(plan, "Hanh dong ngoai danh sach bi tu choi")
        self.assertIn("không được phép", self.store.last_error)


    def test_missing_amount_rejected(self) -> None:
        plan = self.store.create(
            user_id=1, title="x",
            steps=[{"action": "deposit", "args": {"user_id": 1}}])
        self.assertIsNone(plan)
        self.assertIn("số tiền", self.store.last_error)


    def test_negative_and_zero_amount_rejected(self) -> None:
        for bad in (0, -5):
            plan = self.store.create(
                user_id=1, title="x", steps=[self._step(amount=bad)])
            self.assertIsNone(plan, f"amount={bad} phai bi tu choi")


    def test_non_integer_amount_rejected(self) -> None:
        plan = self.store.create(
            user_id=1, title="x", steps=[self._step(amount="abc")])
        self.assertIsNone(plan)


    def test_transfer_to_self_rejected(self) -> None:
        plan = self.store.create(
            user_id=1, title="x",
            steps=[self._step("transfer", sender=1, receiver=1)])


    def test_transfer_missing_receiver_rejected(self) -> None:
        plan = self.store.create(
            user_id=1, title="x",
            steps=[{"action": "transfer",
                    "args": {"sender": 1, "amount": 100}}])
        self.assertIsNone(plan)


    def test_empty_plan_rejected(self) -> None:
        self.assertIsNone(self.store.create(user_id=1, title="x", steps=[]))


    def test_too_many_steps_rejected(self) -> None:
        store = PlanStore(max_steps=3)
        plan = store.create(user_id=1, title="x", steps=[self._step()] * 4)
        self.assertIsNone(plan)
        self.assertIn("tối đa", store.last_error)


    def test_total_over_cap_rejected(self) -> None:
        store = PlanStore(max_total=1_000_000, max_steps=50)
        plan = store.create(user_id=1, title="x", steps=[self._step()] * 20)
        self.assertIsNone(plan)
        self.assertIn("vượt trần", store.last_error)


    def test_plan_limit_per_user_drops_oldest(self) -> None:
        store = PlanStore(max_per_user=2)
        plans = [store.create(user_id=1, title=f"t{i}", steps=[self._step()])
                 for i in range(4)]
        self.assertEqual(store.size, 2, "chi giu toi da 2 ke hoach")
        self.assertIsNone(store.get(plans[0].token), "ke hoach cu bi lo")
        self.assertIsNotNone(store.get(plans[3].token))


class PlanAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PlanStore()

    def _make(self, user_id=1):
        return self.store.create(
            user_id=user_id, title="t",
            steps=[{"action": "deposit",
                    "args": {"user_id": user_id, "amount": 100}}])



    def test_other_user_cannot_see_or_cancel(self) -> None:
        plan = self._make(1)
        self.assertIsNone(self.store.get(plan.token, user_id=999))
        self.assertFalse(self.store.cancel(user_id=999, token=plan.token))
        self.assertEqual(self.store.list_for(999), [])
        self.assertIsNotNone(self.store.get(plan.token, user_id=1))


    def test_cancel_pending_plan(self) -> None:
        plan = self._make()
        self.assertTrue(self.store.cancel(user_id=1, token=plan.token))
        self.assertFalse(self.store.cancel(user_id=1, token=plan.token),
                         "Huy hai lan phai that bai")


    def test_expired_plan_is_dropped(self) -> None:
        store = PlanStore(ttl=1.0)
        plan = store.create(
            user_id=1, title="t",
            steps=[{"action": "deposit",
                    "args": {"user_id": 1, "amount": 100}}])
        plan.created_at = time.time() - 3600
        store.purge()
        self.assertIsNone(store.get(plan.token))


class _Recorder:
    """Runner gia: ghi lai loi goi va tra ket qua theo kich ban."""

    def __init__(self, script=None) -> None:
        self.calls = []
        self.script = script or {}

    async def __call__(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        outcome = self.script.get(action)
        if callable(outcome):
            return outcome(kwargs)
        return outcome if outcome is not None else _ok_result()


class PlanExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = PlanStore()

    async def _plan(self, steps):
        return self.store.create(user_id=1, title="ke hoach", steps=steps)



    async def test_dry_run_moves_no_money(self) -> None:
        runner = _Recorder()
        plan = await self._plan(
            [{"action": "deposit", "args": {"user_id": 1, "amount": 100_000}}])
        ok, msg = await execute_plan(
            self.store, user_id=1, token=plan.token, runner=runner,
            dry_run=True)
        self.assertTrue(ok)
        self.assertIn("không đụng tiền", msg.lower())
        self.assertEqual(runner.calls, [], "Chay thu khong duoc goi runner")
        self.assertEqual(plan.status, PLAN_PENDING,
                         "Chay thu khong duoc doi trang thai")


    async def test_runs_all_steps_in_order(self) -> None:
        runner = _Recorder()
        plan = await self._plan([
            {"action": "deposit", "args": {"user_id": 1, "amount": 100_000}},
            {"action": "transfer",
             "args": {"sender": 1, "receiver": 2, "amount": 50_000}},
        ])
        ok, _ = await execute_plan(
            self.store, user_id=1, token=plan.token, runner=runner)
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in runner.calls], ["deposit", "transfer"])
        self.assertEqual(plan.status, PLAN_DONE)
        self.assertTrue(all(s.status == STEP_DONE for s in plan.steps))


    async def test_stops_at_first_failure(self) -> None:
        runner = _Recorder({"transfer": _bad_result("khong du tien")})
        plan = await self._plan([
            {"action": "deposit", "args": {"user_id": 1, "amount": 100}},
            {"action": "transfer",
             "args": {"sender": 1, "receiver": 2, "amount": 200}},
            {"action": "withdraw", "args": {"user_id": 1, "amount": 300}},
        ])
        ok, msg = await execute_plan(
            self.store, user_id=1, token=plan.token, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(plan.status, PLAN_FAILED)
        self.assertEqual(plan.steps[1].status, STEP_FAILED)
        self.assertEqual(plan.steps[2].status, STEP_SKIPPED,
                         "Buoc sau loi phai bi bo qua")
        self.assertEqual(len(runner.calls), 2, "Khong chay buoc thu 3")
        self.assertIn("Dừng", msg)


    async def test_continue_on_error_when_asked(self) -> None:
        runner = _Recorder({"deposit": _bad_result("het tien")})
        plan = await self._plan([
            {"action": "deposit", "args": {"user_id": 1, "amount": 100}},
            {"action": "withdraw", "args": {"user_id": 1, "amount": 200}},
        ])
        ok, _ = await execute_plan(
            self.store, user_id=1, token=plan.token, runner=runner,
            stop_on_error=False)
        self.assertFalse(ok)
        self.assertEqual(len(runner.calls), 2, "Van chay buoc con lai")
        self.assertEqual(plan.steps[1].status, STEP_DONE)


    async def test_runner_exception_stops_plan(self) -> None:
        def boom(_kwargs):
            raise RuntimeError("DB down")

        runner = _Recorder({"deposit": boom})
        plan = await self._plan([
            {"action": "deposit", "args": {"user_id": 1, "amount": 100}},
            {"action": "withdraw", "args": {"user_id": 1, "amount": 200}},
        ])
        ok, msg = await execute_plan(
            self.store, user_id=1, token=plan.token, runner=runner)
        self.assertFalse(ok)
        self.assertIn("DB down", msg)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(plan.steps[1].status, STEP_SKIPPED)


    async def test_cannot_run_twice(self) -> None:
        runner = _Recorder()
        plan = await self._plan(
            [{"action": "deposit", "args": {"user_id": 1, "amount": 100}}])
        await execute_plan(self.store, user_id=1, token=plan.token,
                           runner=runner)
        ok, _ = await execute_plan(self.store, user_id=1, token=plan.token,
                                   runner=runner)
        self.assertFalse(ok, "Khong duoc chay lai ke hoach da chay")
        self.assertEqual(len(runner.calls), 1, "Khong trừ tiền hai lần")


    async def test_other_user_cannot_run(self) -> None:
        runner = _Recorder()
        plan = await self._plan(
            [{"action": "deposit", "args": {"user_id": 1, "amount": 100}}])
        ok, _ = await execute_plan(self.store, user_id=999,
                                   token=plan.token, runner=runner)
        self.assertFalse(ok)
        self.assertEqual(runner.calls, [])


    async def test_cancelled_plan_cannot_run(self) -> None:
        runner = _Recorder()
        plan = await self._plan(
            [{"action": "deposit", "args": {"user_id": 1, "amount": 100}}])
        self.store.cancel(user_id=1, token=plan.token)
        ok, _ = await execute_plan(self.store, user_id=1, token=plan.token,
                                   runner=runner)
        self.assertFalse(ok)
        self.assertEqual(runner.calls, [])


    async def test_sync_runner_supported(self) -> None:
        seen: list[str] = []

        def sync_runner(action: str, **kwargs):
            seen.append(action)
            return _ok_result()

        plan = await self._plan(
            [{"action": "deposit", "args": {"user_id": 1, "amount": 100}}])
        ok, _ = await execute_plan(self.store, user_id=1, token=plan.token,
                                   runner=sync_runner)
        self.assertTrue(ok)
        self.assertEqual(seen, ["deposit"])


class DraftFromTextTests(unittest.TestCase):
    """Phan tich cau chat -> du khat lo trinh (offline, khong goi LLM)."""

    def test_parse_money_variants(self) -> None:
        self.assertEqual(parse_money("gui 500000"), 500_000)
        self.assertEqual(parse_money("gửi 500.000 đ"), 500_000)
        self.assertEqual(parse_money("gửi 2 triệu"), 2_000_000)
        self.assertEqual(parse_money("nap 1,5 triệu"), 1_500_000)
        self.assertEqual(parse_money("gửi 20k"), 20_000)
        self.assertEqual(parse_money("gửi 300 nghìn"), 300_000)

    def test_parse_money_rejects_junk(self) -> None:
        self.assertIsNone(parse_money("xin chào"))
        self.assertIsNone(parse_money("gửi 0 đ"))
        self.assertIsNone(parse_money("gửi -5"))

    def test_mentions_helpers(self) -> None:
        self.assertTrue(mentions_action("gửi tiền cho bạn"))
        self.assertFalse(mentions_action("bạn khoẻ không"))
        self.assertTrue(mentions_plan("lập lộ trình tháng này"))
        self.assertFalse(mentions_plan("hôm nay thời tiết sao"))

    def test_draft_transfer_with_mention(self) -> None:
        title, steps = draft_plan_from_text(
            "chuyển 500k cho <@42> giúp mình", user_id=1, mentioned=(42,))
        self.assertEqual(steps[0]["action"], "transfer")
        self.assertEqual(steps[0]["args"]["receiver"], 42)
        self.assertEqual(steps[0]["args"]["amount"], 500_000)
        self.assertIn("500.000", title)

    def test_draft_deposit_by_default(self) -> None:
        _title, steps = draft_plan_from_text(
            "gửi 2 triệu vào ngân hàng", user_id=1)
        self.assertEqual(steps[0]["action"], "deposit")
        self.assertEqual(steps[0]["args"]["amount"], 2_000_000)

    def test_draft_withdraw(self) -> None:
        _title, steps = draft_plan_from_text(
            "rút 1 triệu về ví", user_id=1)
        self.assertEqual(steps[0]["action"], "withdraw")

    def test_draft_ignores_own_mention(self) -> None:
        _title, steps = draft_plan_from_text(
            "gửi 300k cho <@1> nhé", user_id=1, mentioned=(1,))
        self.assertNotEqual(steps[0]["action"], "transfer",
                            "Khong chuyen cho chinh minh")

    def test_draft_ignores_non_money_sentence(self) -> None:
        self.assertIsNone(draft_plan_from_text("hôm nay ăn gì", user_id=1))
        self.assertIsNone(draft_plan_from_text("gửi tiền nhé", user_id=1),
                          "Khong co so tien -> khong tao du khat")


    def test_draft_splits_multiple_intents(self) -> None:
        """Câu nhiều ý định -> nhiều bước, giữ đúng thứ tự."""
        _title, steps = draft_plan_from_text(
            "gửi 2 triệu vào ngân hàng và chuyển 500k cho <@42>",
            user_id=1, mentioned=(42,))
        self.assertEqual(len(steps), 2, f"phai tach 2 buoc, nhan duoc {steps}")
        self.assertEqual(steps[0]["action"], "deposit")
        self.assertEqual(steps[0]["args"]["amount"], 2_000_000)
        self.assertEqual(steps[1]["action"], "transfer")
        self.assertEqual(steps[1]["args"]["receiver"], 42)
        self.assertEqual(steps[1]["args"]["amount"], 500_000)

    def test_draft_splits_on_comma(self) -> None:
        _title, steps = draft_plan_from_text(
            "gửi 1 triệu vào ngân hàng, rút 300 nghìn ra ví",
            user_id=1)
        self.assertEqual([s["action"] for s in steps], ["deposit", "withdraw"])

    def test_draft_ignores_noise_clause(self) -> None:
        """Mệnh đề không có tiền bị bỏ qua, không tạo bước rác."""
        _title, steps = draft_plan_from_text(
            "gửi 1 triệu vào ngân hàng, tôi cảm ơn bạn nhiều nhé", user_id=1)
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["action"], "deposit")

    def test_draft_all_clause_noise_returns_none(self) -> None:
        self.assertIsNone(draft_plan_from_text(
            "mình khỏe, cảm ơn bạn", user_id=1))


if __name__ == "__main__":
    unittest.main()

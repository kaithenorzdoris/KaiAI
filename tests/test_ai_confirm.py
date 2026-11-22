"""Kiểm thử cơ chế **xác nhận** thao tác giao dịch của AI.

AI không bao giờ tự chuyển tiền. Nó chỉ tạo *yêu cầu chờ*; người dùng phải
xác nhận bằng token trong thời hạn. Các test ở đây khẳng định đúng các bất
biến đó: token ngẫu nhiên, hạn tuyệt đối, dùng-một-lần, và phân quyền
đúng người yêu cầu.
"""

from __future__ import annotations

import time
import unittest

from kai_core.ai.confirm import (
    CONFIRM_TTL,
    PendingActions,
    run_confirmed,
)


class PendingActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pending = PendingActions(ttl=CONFIRM_TTL)

    def test_create_returns_usable_token(self) -> None:
        item = self.pending.create(
            user_id=1, action="transfer", args={"to": 2, "amount": 1000})
        self.assertTrue(item.token, "phai sinh token")
        self.assertEqual(item.user_id, 1)
        self.assertEqual(item.args["amount"], 1000)
        self.assertEqual(self.pending.size, 1)

    def test_tokens_are_unique_and_not_sequential(self) -> None:
        tokens = {
            self.pending.create(user_id=1, action="transfer").token
            for _ in range(30)
        }
        self.assertEqual(len(tokens), 30, "token phai khac nhau")

    def test_get_does_not_consume(self) -> None:
        item = self.pending.create(user_id=1, action="transfer")
        self.assertIsNotNone(self.pending.get(item.token))
        self.assertIsNotNone(self.pending.get(item.token),
                            "get khong duoc xoa yeu cau")

    def test_take_consumes_once(self) -> None:
        item = self.pending.create(user_id=1, action="transfer")
        self.assertIsNotNone(self.pending.take(user_id=1, token=item.token))
        self.assertIsNone(self.pending.take(user_id=1, token=item.token),
                         "Dung mot lan: lan hai phai tra None")
        self.assertEqual(self.pending.size, 0)

    def test_other_user_cannot_take(self) -> None:
        item = self.pending.create(user_id=1, action="transfer")
        self.assertIsNone(self.pending.take(user_id=999, token=item.token),
                         "Nguoi khac khong duoc lay yeu cau")
        self.assertEqual(self.pending.size, 1, "Yeu cau van phai con")
        self.assertIsNotNone(self.pending.take(user_id=1, token=item.token))

    def test_unknown_token_rejected(self) -> None:
        self.assertIsNone(self.pending.take(user_id=1, token="khong-ton-tai"))

    def test_expired_item_unusable(self) -> None:
        item = self.pending.create(user_id=1, action="transfer")
        item.expires_at = time.time() - 1
        self.assertIsNone(self.pending.get(item.token), "het han -> None")
        self.assertIsNone(self.pending.take(user_id=1, token=item.token))

    def test_purge_removes_expired(self) -> None:
        keep = self.pending.create(user_id=1, action="a")
        gone = self.pending.create(user_id=1, action="b")
        gone.expires_at = time.time() - 1
        self.assertEqual(self.pending.purge(), 1)
        self.assertEqual(self.pending.size, 1)
        self.assertIsNotNone(self.pending.get(keep.token))

    def test_cancel_all_only_affects_owner(self) -> None:
        a = self.pending.create(user_id=1, action="a")
        b = self.pending.create(user_id=2, action="b")
        self.assertEqual(self.pending.cancel_all(1), 1)
        self.assertIsNone(self.pending.get(a.token))
        self.assertIsNotNone(self.pending.get(b.token, user_id=2))

    def test_list_for_sorted_newest_first(self) -> None:
        first = self.pending.create(user_id=1, action="a")
        first.created_at -= 10
        second = self.pending.create(user_id=1, action="b")
        listed = self.pending.list_for(1)
        self.assertEqual([i.token for i in listed],
                         [second.token, first.token])
class RunConfirmedTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_with_args_on_confirm(self) -> None:
        pending = PendingActions(ttl=60)
        calls: list[tuple] = []

        def runner(*, to: int, amount: int) -> bool:
            calls.append((to, amount))
            return True

        item = pending.create(user_id=1, action="transfer",
                              args={"to": 2, "amount": 500})
        ok, msg = await run_confirmed(pending, user_id=1,
                                      token=item.token, runner=runner)
        self.assertTrue(ok, msg)
        self.assertEqual(calls, [(2, 500)])

    async def test_awaitable_runner_supported(self) -> None:
        pending = PendingActions(ttl=60)
        seen: list[int] = []

        async def runner(*, amount: int) -> None:
            seen.append(amount)

        item = pending.create(user_id=1, action="dep", args={"amount": 7})
        ok, _ = await run_confirmed(pending, user_id=1, token=item.token,
                                    runner=runner)
        self.assertTrue(ok)
        self.assertEqual(seen, [7])

    async def test_bad_token_runs_nothing(self) -> None:
        pending = PendingActions(ttl=60)
        called = False

        def runner(**_kwargs) -> bool:
            nonlocal called
            called = True
            return True

        ok, msg = await run_confirmed(pending, user_id=1, token="sai",
                                      runner=runner)
        self.assertFalse(ok)
        self.assertFalse(called, "Token sai -> khong chay gi")
        self.assertIn("không hợp lệ", msg.lower())

    async def test_runner_error_is_reported_not_raised(self) -> None:
        pending = PendingActions(ttl=60)

        def runner(**_kwargs) -> bool:
            raise RuntimeError("DB down")

        item = pending.create(user_id=1, action="transfer")
        ok, msg = await run_confirmed(pending, user_id=1, token=item.token,
                                      runner=runner)
        self.assertFalse(ok)
        self.assertIn("DB down", msg)

    async def test_token_consumed_even_when_runner_fails(self) -> None:
        """Fail thi khong duoc 'xac nhan lai' bang cung token."""
        pending = PendingActions(ttl=60)
        item = pending.create(user_id=1, action="transfer")

        def runner(**_kwargs) -> bool:
            raise RuntimeError("x")

        await run_confirmed(pending, user_id=1, token=item.token,
                            runner=runner)
        self.assertIsNone(pending.get(item.token),
                          "Yeu cau da xoa du runner that bai")


if __name__ == "__main__":
    unittest.main()

    def test_max_per_user_drops_oldest(self) -> None:
        pending = PendingActions(ttl=60, max_per_user=2)
        tokens = [pending.create(user_id=1, action="a").token
                  for _ in range(4)]
        self.assertEqual(pending.size, 2, "chi giu toi da 2 yeu cau")
        self.assertIsNone(pending.get(tokens[0]), "yeu cau cu bi lo")
        self.assertIsNotNone(pending.get(tokens[3]))
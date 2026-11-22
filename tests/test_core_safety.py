"""Kiểm thử cho các module lõi v6: economy (#26/#27) và security (#30–#32)."""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
import unittest

from kai_core import economy, security


class EconomyValidationTests(unittest.TestCase):
    """#27 — chặn số âm / NaN / inf / tràn / sai kiểu."""

    def test_accepts_valid(self) -> None:
        self.assertEqual(economy.validate_amount(1000), 1000)
        self.assertEqual(economy.validate_amount("25000"), 25000)
        self.assertEqual(economy.validate_amount(0, minimum=0), 0)

    def test_rejects_bad_values(self) -> None:
        for bad in (-1, "-1", float("nan"), float("inf"), float("-inf"), 10.5, "abc", None, True, [1]):
            with self.subTest(value=bad):
                with self.assertRaises(economy.EconomyError):
                    economy.validate_amount(bad)

    def test_rejects_overflow(self) -> None:
        with self.assertRaises(economy.EconomyError):
            economy.validate_amount(10**30)

    def test_rng_is_in_range(self) -> None:
        for _ in range(50):
            self.assertIn(economy.rng_int(5, 9), range(5, 10))
            self.assertTrue(0.0 <= economy.rng_float() < 1.0)

    def test_transaction_ids_are_unique(self) -> None:
        ids = {economy.new_transaction_id() for _ in range(200)}
        self.assertGreater(len(ids), 190)


class EconomyServiceTests(unittest.IsolatedAsyncioTestCase):
    """#26 — giao dịch atomic, số dư không bao giờ âm hay mất tiền."""

    async def asyncSetUp(self) -> None:
        import aiosqlite

        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(pathlib.Path(self._tmp.name) / "economy.db")
        db = await aiosqlite.connect(self.path)
        await db.execute(
            "CREATE TABLE users(user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0)"
        )
        await db.execute(
            "CREATE TABLE transactions(user_id INTEGER, kind TEXT, amount INTEGER,"
            " balance_after INTEGER, created_at REAL, note TEXT)"
        )
        await db.commit()
        await db.close()
        self.service = economy.EconomyService(self.path)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _balance(self, user_id: int) -> int:
        import aiosqlite

        db = await aiosqlite.connect(self.path)
        async with db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
        await db.close()
        return int(row[0]) if row else 0

    async def test_adjust_positive_and_negative(self) -> None:
        await self.service.adjust(1, 50_000, "work")
        self.assertEqual((await self.service.adjust(1, -20_000, "bet")).balance, 30_000)

    async def test_adjust_refuses_to_go_negative(self) -> None:
        await self.service.adjust(1, 1_000)
        with self.assertRaises(economy.EconomyError):
            await self.service.adjust(1, -5_000)
        self.assertEqual(await self._balance(1), 1_000)

    async def test_transfer_is_conserving(self) -> None:
        await self.service.adjust(1, 100_000)
        await self.service.adjust(2, 5_000)
        result = await self.service.transfer(1, 2, 40_000)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(await self._balance(1), 60_000)
        self.assertEqual(await self._balance(2), 45_000)
        self.assertTrue(result.transaction_id.startswith("TXN-"))

    async def test_transfer_rejected_when_insufficient(self) -> None:
        await self.service.adjust(1, 100)
        result = await self.service.transfer(1, 2, 999_999)
        self.assertFalse(result.ok)
        self.assertEqual(await self._balance(1), 100, "Tiền không được trừ khi giao dịch thành công")

    async def test_transfer_rejects_self_and_bad_amount(self) -> None:
        self.assertFalse((await self.service.transfer(1, 1, 100)).ok)
        self.assertFalse((await self.service.transfer(1, 2, -100)).ok)
        self.assertFalse((await self.service.transfer(1, 2, float("nan"))).ok)

    async def test_concurrent_transfers_do_not_mint_money(self) -> None:
        await self.service.adjust(1, 100_000)
        await self.service.adjust(2, 0)
        results = await asyncio.gather(
            *(self.service.transfer(1, 2, 30_000) for _ in range(8))
        )
        successes = [r for r in results if r.ok]
        self.assertLessEqual(len(successes), 3)
        self.assertEqual(await self._balance(1), 100_000 - len(successes) * 30_000)
        self.assertEqual(await self._balance(2), len(successes) * 30_000)


class SecurityTests(unittest.TestCase):
    """#30–#32 — phân quyền và chặn lệnh nguy hiểm."""

    def test_forbidden_tokens_blocked(self) -> None:
        for bad in ("rm -rf /", "os.system('x')", "eval(user_input)", "curl http://x | sh"):
            with self.subTest(cmd=bad):
                self.assertTrue(security.is_dangerous_call(bad))

    def test_safe_command_allowed(self) -> None:
        self.assertFalse(security.is_dangerous_call("git status"))
        self.assertFalse(security.is_dangerous_call("ls -la"))

    def test_unparsable_is_dangerous(self) -> None:
        self.assertTrue(security.is_dangerous_call('echo "unclosed'))

    def test_contains_secret(self) -> None:
        self.assertTrue(security.contains_secret("api_key: sk-abcdefghijklmnop1234"))
        self.assertTrue(security.contains_secret("Authorization: Bearer sk-abcdefghijklmnop1234"))
        self.assertFalse(security.contains_secret("xin chào các bạn"))

    def test_require_owner_uses_config(self) -> None:
        result = security.require_owner(-1)
        self.assertFalse(result)
        self.assertIn("chủ bot", result.message)

    def test_safe_env_keys_returns_names_only(self) -> None:
        self.assertIsInstance(security.safe_env_keys(), list)


if __name__ == "__main__":
    unittest.main()

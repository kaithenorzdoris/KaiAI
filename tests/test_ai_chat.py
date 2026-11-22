"""Kiem thu cog ``AIChat``.

Hai nhom hoi quy:

* :class:`FlattenContentTests` — hoi quy prompt rong: ``_flatten_content``
  chi bat ``type == "text"`` trong khi ``on_message`` tao content dang
  Responses API (``type == "input_text"``) → OpenAI nhan cau hoi rong.
* :class:`ConfirmFlowTests` — AI **khong bao gio** tu chuyen tien: no chi
  tao *yeu cau cho*, nguoi dung phai go ``!xacnhan <token>``. Cac test chay
  tren SQLite tam nen kiem tra tien that, khong chi logic.
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


class FlattenContentTests(unittest.TestCase):
    """Hoi quy: prompt khong duoc rong voi moi format content da ho tro."""

    def test_input_text_responses_format_is_extracted(self) -> None:
        content = [
            {"type": "input_text", "text": "Hom nay thoi tiet the nao?"},
            {"type": "input_image", "image_url": "data:image/png;base64,eA==",
             "detail": "auto"},
        ]
        text, has_image = AIChat._flatten_content(content)
        self.assertEqual(text, "Hom nay thoi tiet the nao?")
        self.assertTrue(has_image)

    def test_chat_completions_text_format_still_works(self) -> None:
        text, has_image = AIChat._flatten_content(
            [{"type": "text", "text": "xin chao"}])
        self.assertEqual(text, "xin chao")
        self.assertFalse(has_image)

    def test_plain_string_passthrough(self) -> None:
        text, has_image = AIChat._flatten_content("chao ban")
        self.assertEqual(text, "chao ban")
        self.assertFalse(has_image)

    def test_multiple_text_parts_joined(self) -> None:
        content = [
            {"type": "input_text", "text": "dong 1"},
            {"type": "text", "text": "dong 2"},
        ]
        self.assertEqual(AIChat._flatten_content(content)[0], "dong 1\ndong 2")

    def test_unknown_type_and_empty(self) -> None:
        self.assertEqual(AIChat._flatten_content([{"type": "unknown"}]), ("", False))
        self.assertEqual(AIChat._flatten_content(""), ("", False))
        self.assertEqual(AIChat._flatten_content(None), ("", False))

class ConfirmFlowTests(unittest.IsolatedAsyncioTestCase):
    """Vong doi yeu cau giao dich cua AI: de xuat → xac nhan → tien di."""

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
        # Cog chua duoc Discord nap nen phai gan `cog` cho tung lenh de
        # callback duoc bind vao dung instance.
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

    async def _ticket(self, amount: int = 100_000):
        return await self.cog.propose_action(
            user_id=1, action="transfer",
            args={"sender": 1, "receiver": 2, "amount": amount})

    async def _deposit_ticket(self, amount: int = 50_000):
        return await self.cog.propose_action(
            user_id=1, action="deposit",
            args={"user_id": 1, "amount": amount})

    async def test_propose_action_does_not_move_money(self) -> None:
        item = await self._ticket()
        self.assertIsNotNone(item)
        self.assertTrue(item.token)
        self.assertEqual(await self._balance(1), 5_000_000,
                         "De xuat khong duoc tru tien")
        self.assertEqual(await self._balance(2), 0)

    async def test_unknown_action_is_refused(self) -> None:
        item = await self.cog.propose_action(
            user_id=1, action="rm_rf", args={})
        self.assertIsNone(item, "Hanh dong ngoai danh sach bi tu choi")

    async def test_list_pending_shows_nothing_initially(self) -> None:
        ctx = _Ctx(1)
        await self.cog.list_pending(ctx)
        self.assertIn("không có yêu cầu", str(ctx.last).lower())

    async def test_list_pending_shows_token(self) -> None:
        item = await self._deposit_ticket()
        ctx = _Ctx(1)
        await self.cog.list_pending(ctx)
        self.assertIn(item.token, str(ctx.last))

    async def test_list_pending_is_private(self) -> None:
        await self._deposit_ticket()
        ctx = _Ctx(999)
        await self.cog.list_pending(ctx)
        self.assertIn("không có yêu cầu", str(ctx.last).lower(),
                      "Khong thay yeu cau cua nguoi khac")

    async def test_confirm_transfer_moves_money(self) -> None:
        item = await self._ticket()
        ctx = _Ctx(1)
        await self.cog.confirm_action(ctx, item.token)
        self.assertIn("✅", str(ctx.last))
        self.assertEqual(await self._balance(1), 4_900_000)
        self.assertEqual(await self._balance(2), 100_000)

    async def test_confirm_deposit_moves_to_bank(self) -> None:
        item = await self._deposit_ticket(200_000)
        ctx = _Ctx(1)
        await self.cog.confirm_action(ctx, item.token)
        self.assertEqual(await self._balance(1), 4_800_000)

    async def test_token_works_only_once(self) -> None:
        item = await self._ticket()
        await self.cog.confirm_action(_Ctx(1), item.token)
        ctx = _Ctx(1)
        await self.cog.confirm_action(ctx, item.token)
        self.assertIn("❌", str(ctx.last), "Token dung lai bi tu choi")
        self.assertEqual(await self._balance(1), 4_900_000,
                         "Khong chuyen tien them lan nua")

    async def test_other_user_cannot_confirm(self) -> None:
        item = await self._ticket()
        ctx = _Ctx(999)
        await self.cog.confirm_action(ctx, item.token)
        self.assertIn("❌", str(ctx.last))
        self.assertEqual(await self._balance(1), 5_000_000,
                         "Nguoi khac xac nhan -> khong chuyen tien")

    async def test_expired_token_rejected(self) -> None:
        item = await self._ticket()
        item.expires_at = 0.0  # het han
        ctx = _Ctx(1)
        await self.cog.confirm_action(ctx, item.token)
        self.assertIn("❌", str(ctx.last))
        self.assertEqual(await self._balance(1), 5_000_000)

    async def test_insufficient_balance_fails_without_loss(self) -> None:
        item = await self._ticket(amount=99_000_000)
        ctx = _Ctx(1)
        await self.cog.confirm_action(ctx, item.token)
        self.assertIn("❌", str(ctx.last))
        self.assertEqual(await self._balance(1), 5_000_000,
                         "That bai thi khong mat tien")

    async def test_cancel_single_token(self) -> None:
        item = await self._ticket()
        ctx = _Ctx(1)
        await self.cog.cancel_action(ctx, item.token)
        self.assertIn("hủy", str(ctx.last).lower())
        self.assertIsNone(self.cog.pending.get(item.token))

    async def test_cancel_unknown_token(self) -> None:
        ctx = _Ctx(1)
        await self.cog.cancel_action(ctx, "khong-ton-tai")
        self.assertIn("❌", str(ctx.last))

    async def test_cancel_all(self) -> None:
        for _ in range(3):
            await self._deposit_ticket(1000)
        ctx = _Ctx(1)
        await self.cog.cancel_action(ctx)
        self.assertIn("3", str(ctx.last))
        self.assertEqual(self.cog.pending.size, 0)


if __name__ == "__main__":
    unittest.main()
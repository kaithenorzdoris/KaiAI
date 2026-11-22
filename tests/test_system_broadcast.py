from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import discord
from discord.ext import commands

from cogs.system import SystemCore, owner_only


class FakeChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        can_send: bool = True,
        can_ping: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.can_send = can_send
        self.can_ping = can_ping
        self.error = error
        self.sent: list[tuple[str, discord.AllowedMentions]] = []

    def permissions_for(self, _member):
        return SimpleNamespace(
            view_channel=self.can_send,
            send_messages=self.can_send,
            mention_everyone=self.can_ping,
            administrator=False,
        )

    async def send(self, content: str, *, allowed_mentions: discord.AllowedMentions):
        if self.error:
            raise self.error
        self.sent.append((content, allowed_mentions))


class FakeGuild:
    def __init__(self, guild_id: int, channel: FakeChannel | None) -> None:
        self.id = guild_id
        self.name = f"Guild {guild_id}"
        self.system_channel = channel
        self.me = object()
        self.channels = {channel.id: channel} if channel else {}

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)


class AutoConfirmView:
    def __init__(self, **_kwargs) -> None:
        self.value = True

    async def wait(self) -> None:
        return None


class CancelView(AutoConfirmView):
    def __init__(self, **_kwargs) -> None:
        self.value = False


class FakeMessage:
    def __init__(self) -> None:
        self.edits: list[dict[str, object]] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeContext:
    clean_prefix = "!"

    def __init__(self, author_id: int = 50) -> None:
        self.author = SimpleNamespace(id=author_id, __str__=lambda _self: f"user-{author_id}")
        self.messages: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.message = FakeMessage()

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        return self.message

    async def reply(self, content: str, **kwargs):
        self.messages.append(((content,), kwargs))
        return self.message


class SystemBroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.channel = FakeChannel(100)
        self.guild = FakeGuild(1, self.channel)
        self.bot = SimpleNamespace(guilds=[self.guild])
        self.cog = SystemCore(self.bot)
        self.cog.settings_db_path = Path(self.temp_dir.name) / "system_settings.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_server_admin_can_save_its_update_channel(self) -> None:
        context = FakeContext()
        context.guild = self.guild
        context.channel = self.channel

        with patch("cogs.system.discord.TextChannel", FakeChannel):
            await SystemCore.set_update_channel.callback(self.cog, context, self.channel)

        async with aiosqlite.connect(self.cog.settings_db_path) as db:
            cursor = await db.execute("SELECT guild_id, channel_id, updated_by FROM update_channels")
            self.assertEqual(await cursor.fetchone(), (1, 100, 50))
        self.assertIn(self.channel.mention, context.messages[-1][0][0])

    async def test_global_broadcast_requires_owner_not_server_admin(self) -> None:
        context = FakeContext()
        context.bot = SimpleNamespace(is_owner=AsyncMock(return_value=False))
        with patch("cogs.system.CONFIG", SimpleNamespace(is_owner=lambda _user_id: False)):
            with self.assertRaises(commands.CheckFailure):
                await owner_only().predicate(context)

    async def test_ping_broadcast_is_confirmed_and_skips_servers_without_permission(self) -> None:
        custom_channel = FakeChannel(200, can_ping=True)
        failed_channel = FakeChannel(
            301,
            error=discord.HTTPException(
                SimpleNamespace(status=500, reason="Internal Server Error"),
                "simulated send failure",
            ),
        )
        no_ping_channel = FakeChannel(300, can_ping=False)
        self.guild.channels[200] = custom_channel
        guild_with_send_failure = FakeGuild(2, failed_channel)
        guild_without_ping = FakeGuild(3, no_ping_channel)
        guild_without_channel = FakeGuild(4, None)
        self.bot.guilds = [
            self.guild,
            guild_with_send_failure,
            guild_without_ping,
            guild_without_channel,
        ]
        with closing(sqlite3.connect(self.cog.settings_db_path)) as db:
            db.execute(
                """
                CREATE TABLE update_channels (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    updated_by INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute("INSERT INTO update_channels VALUES (1, 200, 50, CURRENT_TIMESTAMP)")
            db.commit()

        context = FakeContext()
        with patch("cogs.system.ConfirmView", AutoConfirmView):
            await SystemCore.broadcast_update.callback(
                self.cog, context, raw="--ping @everyone bản cập nhật mới"
            )

        self.assertEqual(len(custom_channel.sent), 1)
        content, allowed_mentions = custom_channel.sent[0]
        self.assertTrue(content.startswith("@everyone\n"))
        self.assertIn("@\u200beveryone bản cập nhật mới", content)
        self.assertTrue(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(no_ping_channel.sent)
        self.assertIn("Thất bại khi gửi: **1**", context.message.edits[-1]["content"])
        self.assertIn("gửi tới **1 server**", context.message.edits[-1]["content"])
        self.assertIn("bỏ qua: **2**", context.message.edits[-1]["content"])

    async def test_broadcast_cancelled_by_owner_sends_nothing(self) -> None:
        context = FakeContext()
        with patch("cogs.system.ConfirmView", CancelView):
            await SystemCore.broadcast_update.callback(
                self.cog, context, raw="@everyone bảo trì hoàn tất"
            )

        self.assertFalse(self.channel.sent)
        self.assertIn("Đã huỷ", context.message.edits[-1]["content"])


if __name__ == "__main__":
    unittest.main()

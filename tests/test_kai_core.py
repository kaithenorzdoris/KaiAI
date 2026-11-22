"""Unit test cho package ``kai_core`` (không cần discord.py)."""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest import mock

from kai_core import config as config_module
from kai_core.utils import (
    bar_gauge,
    chunk_text,
    clamp,
    format_duration,
    human_number,
    paginate,
    parse_hex_color,
    percentage,
    truncate,
    unique,
)

try:
    import aiosqlite  # noqa: F401
except Exception:  # pragma: no cover
    aiosqlite = None


class UtilsTests(unittest.TestCase):
    def test_clamp(self) -> None:
        self.assertEqual(clamp(5, 0, 10), 5)
        self.assertEqual(clamp(-3, 0, 10), 0)
        self.assertEqual(clamp(99, 0, 10), 10)

    def test_truncate_never_exceeds_limit(self) -> None:
        for limit in (0, 1, 5, 20):
            self.assertLessEqual(len(truncate("x" * 100, limit)), max(limit, 0))

    def test_chunk_text_respects_limit(self) -> None:
        text = "\n".join(f"dòng số {i}" for i in range(400))
        chunks = chunk_text(text, limit=120)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 120)
        self.assertIn("dòng số 0", chunks[0])

    def test_chunk_text_empty(self) -> None:
        self.assertEqual(chunk_text(""), [])
        self.assertEqual(chunk_text("   "), [])

    def test_chunk_text_hard_split(self) -> None:
        chunks = chunk_text("A" * 250, limit=100)
        self.assertEqual([len(c) for c in chunks], [100, 100, 50])

    def test_paginate(self) -> None:
        self.assertEqual(paginate([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])
        self.assertEqual(paginate([], 3), [[]])
        with self.assertRaises(ValueError):
            paginate([1], 0)

    def test_human_number(self) -> None:
        self.assertEqual(human_number(999), "999")
        self.assertEqual(human_number(1_500), "1.50K")
        self.assertEqual(human_number(-2_000_000), "-2.00M")

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(90), "1m 30s")
        self.assertEqual(format_duration(3661), "1h 1m")
        self.assertIn("giây", format_duration(45, long=True))

    def test_parse_hex_color(self) -> None:
        self.assertEqual(parse_hex_color("#5865F2"), 0x5865F2)
        self.assertEqual(parse_hex_color("5865f2"), 0x5865F2)
        with self.assertRaises(ValueError):
            parse_hex_color("#12345")
        with self.assertRaises(ValueError):
            parse_hex_color("not-a-color")

    def test_percentage_and_gauge(self) -> None:
        self.assertEqual(percentage(1, 4), "25.0%")
        self.assertEqual(percentage(1, 0), "0.0%")
        self.assertEqual(len(bar_gauge(1, 2, width=10)), 10)

    def test_unique(self) -> None:
        self.assertEqual(unique([1, 1, 2, 3, 2]), [1, 2, 3])


class ConfigTests(unittest.TestCase):
    def test_get_int_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"KAI_TEST_INT": "abc"}, clear=False):
            self.assertEqual(config_module.get_int("KAI_TEST_INT", 7), 7)
        with mock.patch.dict(os.environ, {"KAI_TEST_INT": "42"}, clear=False):
            self.assertEqual(config_module.get_int("KAI_TEST_INT", 7), 42)

    def test_get_int_clamps_range(self) -> None:
        with mock.patch.dict(os.environ, {"KAI_TEST_INT": "999"}, clear=False):
            self.assertEqual(config_module.get_int("KAI_TEST_INT", 1, minimum=1, maximum=8), 8)

    def test_get_bool(self) -> None:
        for raw, expected in (("true", True), ("ON", True), ("no", False), ("0", False)):
            with mock.patch.dict(os.environ, {"KAI_TEST_BOOL": raw}, clear=False):
                self.assertIs(config_module.get_bool("KAI_TEST_BOOL", not expected), expected)

    def test_get_id_set(self) -> None:
        with mock.patch.dict(os.environ, {"KAI_TEST_IDS": "1, 2;3 4"}, clear=False):
            self.assertEqual(config_module.get_id_set("KAI_TEST_IDS"), {1, 2, 3, 4})

    def test_from_env_builds_config(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DISCORD_TOKEN": "token-abc",
                "BOT_OWNER_ID": "123",
                "OPENAI_MODEL": "gpt-4o-mini",
                "AI_CONCURRENCY": "9",
                "ENABLE_KEEP_ALIVE": "false",
                "COMMAND_PREFIXES": "!,?,~",
            },
            clear=False,
        ):
            cfg = config_module.BotConfig.from_env()
        self.assertEqual(cfg.token, "token-abc")
        self.assertIn(123, cfg.owner_ids)
        self.assertEqual(cfg.ai_concurrency, 8)  # bị clamp về 8
        self.assertFalse(cfg.enable_keep_alive)
        self.assertEqual(cfg.prefixes, ("!", "?", "~"))
        self.assertTrue(cfg.is_configured)

    def test_from_env_has_no_baked_in_owner_id(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"BOT_OWNER_ID": "", "BOT_OWNER_IDS": ""},
            clear=False,
        ):
            cfg = config_module.BotConfig.from_env()
        self.assertEqual(cfg.owner_ids, set())

    def test_validate_reports_missing_token(self) -> None:
        cfg = config_module.BotConfig(token=None, openai_api_key="x")
        self.assertTrue(any("DISCORD_TOKEN" in p for p in cfg.validate()))


@unittest.skipUnless(aiosqlite, "aiosqlite chưa được cài đặt")
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from kai_core.database import Database

        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self._tmp.name) / "test.db")
        await self.db.connect()
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)"
        )
        await self.db.commit()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_insert_and_fetch(self) -> None:
        await self.db.execute("INSERT INTO items (name) VALUES (?)", ("alpha",))
        await self.db.commit()
        row = await self.db.fetchone("SELECT name FROM items WHERE name = ?", ("alpha",))
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "alpha")

    async def test_fetchval_and_row_count(self) -> None:
        await self.db.executemany("INSERT INTO items (name) VALUES (?)", [("a",), ("b",)])
        await self.db.commit()
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM items"), 2)

    async def test_transaction_commits(self) -> None:
        async with self.db.transaction():
            await self.db.execute("INSERT INTO items (name) VALUES (?)", ("ok",))
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM items"), 1)

    async def test_transaction_rolls_back(self) -> None:
        with self.assertRaises(RuntimeError):
            async with self.db.transaction():
                await self.db.execute("INSERT INTO items (name) VALUES (?)", ("nope",))
                raise RuntimeError("boom")
        self.assertEqual(await self.db.fetchval("SELECT COUNT(*) FROM items"), 0)

    async def test_table_exists_and_pragma(self) -> None:
        self.assertTrue(await self.db.table_exists("items"))
        self.assertFalse(await self.db.table_exists("khong_ton_tai"))
        self.assertIn(
            str(await self.db.pragma("journal_mode")).lower(),
            {"wal", "memory", "delete"},
        )


if __name__ == "__main__":
    unittest.main()

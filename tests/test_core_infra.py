"""Kiểm thử Phase A/B: ratelimit (#24), idempotency (#28/#29), migrations (#53)
và LogStore (#16–#23)."""

from __future__ import annotations

import asyncio
import logging
import pathlib
import tempfile
import types
import unittest

from kai_core import idempotency, migrations, ratelimit
from kai_core.logstore import LogStore


class RateLimitTests(unittest.TestCase):
    """#24 — token bucket nhiều tầng."""

    def test_blocks_after_capacity(self) -> None:
        limiter = ratelimit.RateLimiter(capacity=3, period=60)
        self.assertTrue(all(limiter.check("user", 1).allowed for _ in range(3)))
        blocked = limiter.check("user", 1)
        self.assertFalse(blocked.allowed)
        self.assertGreater(blocked.retry_after, 0)
        self.assertIn("quá nhanh", blocked.message)

    def test_separate_keys_independent(self) -> None:
        limiter = ratelimit.RateLimiter(capacity=1, period=60)
        self.assertTrue(limiter.check("user", 1).allowed)
        self.assertTrue(limiter.check("user", 2).allowed)
        self.assertFalse(limiter.check("user", 1).allowed)

    def test_sweep_only_drops_refilled_buckets(self) -> None:
        clock = {"now": 0.0}
        limiter = ratelimit.RateLimiter(
            capacity=1, period=10, clock=lambda: clock["now"]
        )
        limiter.check("user", 1)
        limiter.check("user", 2)
        self.assertEqual(limiter.tracked, 2)
        # Bucket còn token đang bị giữ → không được xoá sớm (giữ đúng trạng thái).
        self.assertEqual(limiter.sweep(force=True), 0)
        self.assertEqual(limiter.tracked, 2)

        clock["now"] = 20.0  # refill đầy
        self.assertEqual(limiter.sweep(force=True), 2)
        self.assertEqual(limiter.tracked, 0)

    def test_sweep_respects_interval(self) -> None:
        clock = {"now": 0.0}
        limiter = ratelimit.RateLimiter(
            capacity=1, period=10, sweep_interval=60, clock=lambda: clock["now"]
        )
        clock["now"] = 100.0
        limiter.check("user", 1)
        self.assertEqual(limiter.sweep(), 0)
        self.assertEqual(limiter.sweep(force=True), 0)  # vẫn chưa refill

    def test_layered_check_allows_normal_usage(self) -> None:
        multi = ratelimit.build_default_limiter()
        self.assertTrue(
            multi.layered_check(user_id=1, guild_id=2, channel_id=3, command_name="cash").allowed
        )


class IdempotencyTests(unittest.IsolatedAsyncioTestCase):
    """#28/#29 — chống double-click và replay."""

    async def test_once_guard_blocks_second_claim(self) -> None:
        guard = idempotency.OnceGuard(ttl=30)
        self.assertTrue(await guard.acquire("k"))
        self.assertFalse(await guard.acquire("k"))
        await guard.release("k")
        self.assertTrue(await guard.acquire("k"))

    async def test_store_replay_returns_cached_result(self) -> None:
        import aiosqlite

        with tempfile.TemporaryDirectory() as tmp:
            db = await aiosqlite.connect(str(pathlib.Path(tmp) / "i.db"))
            store = idempotency.IdempotencyStore(db)
            first = await store.claim("daily", "2026-10-01", 1)
            self.assertTrue(first.claimed)
            await store.complete("daily", "2026-10-01", {"reward": 25000})

            replay = await store.claim("daily", "2026-10-01", 1)
            self.assertFalse(replay.claimed)
            self.assertTrue(replay.replayed)
            self.assertEqual(replay.result, {"reward": 25000})
            await db.close()


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    """#53 — migration theo version, idempotent, rollback khi lỗi."""

    async def test_apply_then_rerun_is_idempotent(self) -> None:
        import aiosqlite

        with tempfile.TemporaryDirectory() as tmp:
            db = await aiosqlite.connect(str(pathlib.Path(tmp) / "m.db"))
            migrator = migrations.Migrator(db)
            first = await migrator.apply()
            self.assertTrue(first.ok)
            self.assertEqual(first.to_version, migrations.CORE_MIGRATIONS[-1].version)

            second = await migrator.apply()
            self.assertTrue(second.ok)
            self.assertEqual(len(second.applied), 0)

            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_sessions'"
            )
            self.assertIsNotNone(await cursor.fetchone())
            await db.close()

    async def test_failed_migration_rolls_back(self) -> None:
        import aiosqlite

        with tempfile.TemporaryDirectory() as tmp:
            db = await aiosqlite.connect(str(pathlib.Path(tmp) / "bad.db"))
            broken = migrations.Migration(version=1, name="broken", statements=("NONSENSE SQL",))
            result = await migrations.Migrator(db, [broken]).apply()
            self.assertFalse(result.ok)
            self.assertEqual(result.to_version, 0)
            await db.close()


class LogStoreTests(unittest.IsolatedAsyncioTestCase):
    """#16–#23 — log DB, Case ID, redaction và retention."""

    async def asyncSetUp(self) -> None:
        import aiosqlite

        self._tmp = tempfile.TemporaryDirectory()
        self.db = await aiosqlite.connect(str(pathlib.Path(self._tmp.name) / "logs.db"))
        self.store = LogStore(self.db, retention_days=1)
        await self.store.setup()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_case_ids_increment(self) -> None:
        first = await self.store.log_moderation(guild_id=1, action="ban", moderator_name="mod")
        second = await self.store.log_moderation(guild_id=1, action="kick", moderator_name="mod")
        self.assertEqual(first, "CASE-000001")
        self.assertEqual(second, "CASE-000002")

    async def test_case_lookup(self) -> None:
        case = await self.store.log_moderation(
            guild_id=1, action="warn", moderator_name="mod", target_name="user", reason="spam"
        )
        record = await self.store.get_case(case)
        self.assertIsNotNone(record)
        self.assertIn(case, record.summary())
        self.assertIsNone(await self.store.get_case("CASE-999999"))

    async def test_command_log_redacts_secrets(self) -> None:
        await self.store.log_command(user_id=7, command="token", args="api_key=sk-abcdefghijklmnop1234")
        cursor = await self.db.execute("SELECT args_redacted FROM command_logs")
        self.assertNotIn("sk-abcdefghijklmnop1234", (await cursor.fetchone())[0])

    async def test_retention_purges_old_rows(self) -> None:
        await self.db.execute(
            "INSERT INTO message_logs (guild_id, channel_id, event, created_at) VALUES (1, 1, 'create', ?)",
            (0.0,),
        )
        await self.db.commit()
        await self.store.log_message(guild_id=1, channel_id=1, event="create")
        self.assertGreaterEqual((await self.store.purge_expired(days=1))["message_logs"], 1)

    async def test_stats_counts_rows(self) -> None:
        await self.store.log_message(guild_id=1, channel_id=1, event="create")
        self.assertEqual((await self.store.stats(1)).counts["message_logs"], 1)


class ParityLimitTests(unittest.TestCase):
    """Hồi quy: hạn mứn 100 của Discord chỉ tính lệnh **top-level**.

    Trước đây ``_tree_size`` dùng ``tree.get_commands()`` — trên discord.py cũ
    hàm này trả về danh sách phẳng đã mở group, khiến KaiAI tưởng đã đầy hạn
    mức sớm và rớt 32 lệnh. Từ discord.py 2.7.x, ``get_commands()`` đã trả về
    đúng top-level (group giữ nguyên), nên test này chấp nhận cả hai hành vi
    nhưng ``_tree_size`` luôn phải bằng 4.
    """

    def test_tree_size_counts_only_top_level(self) -> None:
        import discord
        from discord import app_commands
        from discord.ext import commands as dpy_commands

        from kai_core.parity import _tree_size

        # ``Bot`` đã tự tạo ``.tree``; tạo tree mới sẽ gây ClientException.
        bot = dpy_commands.Bot(command_prefix="!", intents=discord.Intents.none())
        tree = bot.tree
        group = app_commands.Group(name="admin", description="g")

        async def _noop(interaction) -> None:
            return None

        for sub in ("ban", "kick", "mute"):
            group.add_command(
                app_commands.Command(
                    name=sub, description=sub, callback=_noop
                )
            )
        tree.add_command(group)

        for name in ("cash", "daily", "work"):
            tree.add_command(app_commands.Command(name=name, description=name, callback=_noop))

        # 1 group + 3 lệnh đơn = 4 top-level.
        # discord.py cũ: get_commands() phẳng = 7; bản >=2.7: = 4.
        flat_count = len(tree.get_commands())
        self.assertIn(flat_count, (4, 7))
        self.assertEqual(_tree_size(tree), 4)
        # walk_commands luôn thấy toàn bộ cây: 1 group + 3 sub + 3 đơn = 7.
        self.assertEqual(len(list(tree.walk_commands())), 7)


class ConcurrencyRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Hồi quy cho 3 race/path bug đã sửa."""

    async def asyncSetUp(self) -> None:
        import aiosqlite

        self._tmp = tempfile.TemporaryDirectory()
        self.db = await aiosqlite.connect(str(pathlib.Path(self._tmp.name) / "l.db"))
        self.store = LogStore(self.db, retention_days=30)
        await self.store.setup()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_case_ids_are_unique_under_concurrency(self) -> None:
        """Trước đây INSERT→UPDATE→SELECT rời nhau sinh ra Case ID trùng."""
        ids = await asyncio.gather(
            *(self.store.next_case_id(42) for _ in range(40))
        )
        self.assertEqual(len(set(ids)), 40, "Case ID phải là duy nhất")
        self.assertEqual(sorted(ids), [f"CASE-{i:06d}" for i in range(1, 41)])

    async def test_idempotency_claim_is_atomic(self) -> None:
        """SELECT-then-INSERT cũ cho phép 2 request cùng claimed=True."""
        store = idempotency.IdempotencyStore(self.db)
        await store.setup()
        claims = await asyncio.gather(
            *(store.claim("daily", "2026-10-02", 1) for _ in range(20))
        )
        winners = [c for c in claims if c.claimed]
        self.assertEqual(len(winners), 1, "Chỉ một request được phép xử lý")
        self.assertFalse(any(c.replayed for c in claims))


class DataDirTests(unittest.TestCase):
    """#53 — mọi file .db phải nằm trong KAI_DATA_DIR để volume Docker giữ được."""

    def test_relative_path_resolves_under_data_dir(self) -> None:
        from kai_core.config import _resolve_in_data_dir

        base = pathlib.Path("/data")
        self.assertEqual(
            _resolve_in_data_dir("", base, "kai_logs.db"),
            str(base / "kai_logs.db"),
        )
        self.assertEqual(
            _resolve_in_data_dir("logs.db", base, "kai_logs.db"),
            str(base / "logs.db"),
        )

    def test_absolute_path_kept_as_is(self) -> None:
        from kai_core.config import _resolve_in_data_dir

        absolute = str(pathlib.Path("var") / "lib" / "kai" / "l.db")
        absolute = str(pathlib.Path(absolute).absolute())
        self.assertEqual(_resolve_in_data_dir(absolute, pathlib.Path("/data"), "x.db"), absolute)

    def test_economy_db_path_is_shared_by_games_and_admin(self) -> None:
        """Hai cog phải trỏ cùng một file, nếu lệch thì admin 'không thấy' tiền."""
        import importlib

        from kai_core.config import CONFIG

        games = importlib.import_module("cogs.games")
        admin = importlib.import_module("cogs.admin")
        self.assertEqual(games.DB_PATH, admin.DB_PATH)
        self.assertEqual(games.DB_PATH, str(CONFIG.data_dir / "economy.db"))


class SnowflakePrecisionTests(unittest.TestCase):
    """Hồi quy: get_int phải giữ nguyên 18-19 chữ số của Discord snowflake.

    ``int(float("123456789012345678")) == 123456789012345680`` — sai 2 đơn vị,
    đủ để trỏ nhầm guild/channel và làm slash guild-scoped không hoạt động.
    """

    def test_snowflake_is_not_mangled(self) -> None:
        import os

        from kai_core.config import get_int

        ids = ["123456789012345678", "987654321098765432", "9007199254740993"]
        original = dict(os.environ)
        try:
            for snowflake in ids:
                os.environ["KAI_TEST_ID"] = snowflake
                self.assertEqual(get_int("KAI_TEST_ID", 0), int(snowflake), snowflake)
                self.assertNotEqual(int(float(snowflake)), int(snowflake))
        finally:
            os.environ.clear()
            os.environ.update(original)

    def test_float_style_still_accepted(self) -> None:
        import os

        from kai_core.config import get_int

        os.environ["KAI_TEST_ID"] = "42.0"
        try:
            self.assertEqual(get_int("KAI_TEST_ID", 0), 42)
        finally:
            os.environ.pop("KAI_TEST_ID", None)

    def test_garbage_falls_back_to_default(self) -> None:
        import os

        from kai_core.config import get_int

        os.environ["KAI_TEST_ID"] = "khong-phai-so"
        try:
            self.assertEqual(get_int("KAI_TEST_ID", 7), 7)
        finally:
            os.environ.pop("KAI_TEST_ID", None)


class LogDedupeTests(unittest.TestCase):
    """#63 — Aegis phải nhường quyền khi Log Center đang ghi cùng loại log.

    Cả hai cog đều nghe 7 sự kiện Discord. Nếu không có cổng ủy quyền thì mỗi
    tin nhắn/join/audit sẽ bị gửi **hai lần** vào hai kênh log khác nhau.
    """

    @staticmethod
    def _aegis(center):
        from cogs.logs import AegisLogs

        cog = AegisLogs.__new__(AegisLogs)  # bypass __init__ (cần DB thật)
        cog.bot = types.SimpleNamespace(log_center=center)
        cog.logger = logging.getLogger("test")
        return cog

    def test_no_log_center_means_aegis_runs(self) -> None:
        cog = self._aegis(None)
        self.assertFalse(cog._center_handles(object(), "spy_log_id"))

    def test_defers_when_center_enabled_for_that_key(self) -> None:
        center = types.SimpleNamespace(is_enabled=lambda guild, key: key == "message")
        cog = self._aegis(center)
        self.assertTrue(cog._center_handles(object(), "spy_log_id"))
        self.assertTrue(cog._center_handles(object(), "msg_log_id"))
        self.assertFalse(cog._center_handles(object(), "member_log_id"))

    def test_broken_center_keeps_aegis_running(self) -> None:
        def boom(guild, key):
            raise RuntimeError("Log Center chết")

        cog = self._aegis(types.SimpleNamespace(is_enabled=boom))
        self.assertFalse(cog._center_handles(object(), "spy_log_id"))

    def test_key_mapping_covers_all_overlapping_events(self) -> None:
        from cogs.logs import AegisLogs

        self.assertEqual(
            AegisLogs.CENTER_KEYS,
            {
                "spy_log_id": "message",
                "msg_log_id": "message",
                "member_log_id": "member",
                "server_log_id": "server",
            },
        )


class ModerationLoggingTests(unittest.IsolatedAsyncioTestCase):
    """#14 — hành động kiểm duyệt phải sinh Case ID tra cứu được."""

    @staticmethod
    def _admin(store):
        from cogs.admin import Admin

        cog = Admin.__new__(Admin)
        cog.bot = types.SimpleNamespace(log_center=types.SimpleNamespace(store=store))
        cog.logger = logging.getLogger("test")
        return cog

    @staticmethod
    def _ctx():
        return types.SimpleNamespace(
            guild=types.SimpleNamespace(id=1),
            channel=types.SimpleNamespace(id=2),
            author=types.SimpleNamespace(id=3, __str__=lambda s: "Mod#0001"),
        )

    async def test_returns_empty_without_log_center(self) -> None:
        from cogs.admin import Admin

        cog = Admin.__new__(Admin)
        cog.bot = types.SimpleNamespace()
        cog.logger = logging.getLogger("test")
        self.assertEqual(await cog.log_moderation(self._ctx(), "ban"), "")

    async def test_generates_case_id(self) -> None:
        import aiosqlite

        from kai_core.logstore import LogStore

        with tempfile.TemporaryDirectory() as tmp:
            db = await aiosqlite.connect(str(pathlib.Path(tmp) / "l.db"))
            store = LogStore(db)
            await store.setup()
            case = await self._admin(store).log_moderation(
                self._ctx(), "ban", member=types.SimpleNamespace(id=9), reason="spam"
            )
            self.assertRegex(case, r"^CASE-\d{6}$")
            record = await store.get_case(case)
            self.assertIsNotNone(record)
            self.assertIn(case, record.summary())
            self.assertIn("spam", record.summary())
            await db.close()

    async def test_store_failure_does_not_break_moderation(self) -> None:
        class Broken:
            async def log_moderation(self, **kwargs):
                raise RuntimeError("DB log hỏng")

        self.assertEqual(await self._admin(Broken()).log_moderation(self._ctx(), "kick"), "")


if __name__ == "__main__":
    unittest.main()

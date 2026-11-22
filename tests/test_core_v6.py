"""Kiểm thử cho các module lõi mới ở v6: parity, pagination, errors, version."""

from __future__ import annotations

import unittest

import discord

from kai_core import errors, pagination, version
from kai_core.parity import (
    ParitySkip,
    describe_param,
    plan_command,
    resolve_type,
    sanitize_command_name,
    sanitize_param_name,
    unwrap_optional,
)


class VersionTests(unittest.TestCase):
    def test_version_is_semver(self) -> None:
        parts = version.VERSION.split(".")
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(part.isdigit() for part in parts))

    def test_version_file_matches(self) -> None:
        self.assertEqual(version.read_version_file(), version.VERSION)

    def test_version_string(self) -> None:
        self.assertIn(version.VERSION, version.version_string())


class ParityPureTests(unittest.TestCase):
    """Các hàm thuần của parity — không cần kết nối Discord."""

    def test_sanitize_param_name(self) -> None:
        self.assertEqual(sanitize_param_name("member"), "member")
        self.assertEqual(sanitize_param_name("Text-Content"), "text_content")
        self.assertEqual(sanitize_param_name("_private"), "private")
        self.assertEqual(sanitize_param_name("2fa"), "a_2fa")

    def test_sanitize_command_name(self) -> None:
        self.assertEqual(sanitize_command_name("Cash"), "cash")
        self.assertEqual(sanitize_command_name("8ball"), "8ball")
        self.assertIsNone(sanitize_command_name("!!!"))

    def test_unwrap_optional(self) -> None:
        inner, optional = unwrap_optional(int | None)
        self.assertTrue(optional)
        self.assertIs(inner, int)
        plain, not_optional = unwrap_optional(str)
        self.assertFalse(not_optional)
        self.assertIs(plain, str)

    def test_resolve_type_known_and_unknown(self) -> None:
        import discord

        self.assertTrue(resolve_type(int).pass_through)
        self.assertTrue(resolve_type(discord.Member).pass_through)
        self.assertFalse(resolve_type(discord.PartialEmoji).pass_through)
        with self.assertRaises(ParitySkip):
            resolve_type(discord.Message)

    def test_describe_param_limits(self) -> None:
        self.assertLessEqual(len(describe_param("a", "x" * 500)), 100)
        self.assertTrue(describe_param("amount", None))

    def test_plan_command_on_real_command(self) -> None:
        from discord.ext import commands

        class Demo(commands.Cog):
            @commands.command(name="demo")
            async def demo(self, ctx, member: discord.Member, amount: int, *, note: str = "hi"):
                """Demo command."""

        plan = plan_command(Demo(None).demo)
        self.assertEqual(plan.name, "demo")
        self.assertEqual([p.slash_name for p in plan.params], ["member", "amount", "note"])
        self.assertTrue(plan.params[0].required)
        self.assertFalse(plan.params[2].required)
        self.assertEqual(plan.params[2].kind, "keyword")

    def test_plan_command_skips_hidden(self) -> None:
        from discord.ext import commands

        class Hidden(commands.Cog):
            @commands.command(name="secret", hidden=True)
            async def secret(self, ctx):
                """Hidden."""

        with self.assertRaises(ParitySkip):
            plan_command(Hidden(None).secret)
class PaginationTests(unittest.TestCase):
    def test_split_short_text(self) -> None:
        self.assertEqual(pagination.split_text("short", 100), ["short"])

    def test_split_respects_limit(self) -> None:
        pages = pagination.split_text("word " * 500, 200)
        self.assertTrue(pages)
        self.assertTrue(all(len(page) <= 200 for page in pages))

    def test_split_never_breaks_words(self) -> None:
        text = " ".join(f"token{i}" for i in range(300))
        pages = pagination.split_text(text, 150)
        self.assertEqual(" ".join(pages).split(), text.split())

    def test_split_keeps_code_fences_valid(self) -> None:
        code = "```py\n" + "\n".join(f"x{i} = {i}" for i in range(120)) + "\n```"
        pages = pagination.split_text(code, 240)
        self.assertTrue(all(len(page) <= 240 for page in pages))
        for page in pages:
            stripped = page.strip()
            self.assertTrue(stripped.startswith("```"), stripped[:40])
            self.assertTrue(stripped.endswith("```"), stripped[-40:])

    def test_split_does_not_break_links(self) -> None:
        link = "https://discord.com/channels/123456789/987654321/1122334455"
        pages = pagination.split_text(f"open {link} now", 20)
        self.assertTrue(any(link in page for page in pages))

    def test_paginate_sequence_keeps_all_items(self) -> None:
        items = list(range(95))
        pages = pagination.paginate_sequence(items, 10)
        self.assertEqual([x for page in pages for x in page], items)
        self.assertEqual(len(pages), 10)

    def test_paginate_sequence_rejects_bad_size(self) -> None:
        with self.assertRaises(ValueError):
            pagination.paginate_sequence([1], 0)

    def test_build_field_pages_respects_discord_limits(self) -> None:
        rows = [(f"item{i}", "v" * 3000) for i in range(60)]
        pages = pagination.build_field_pages("Title", rows, per_page=8)
        self.assertGreater(len(pages), 1)
        for page in pages:
            self.assertLessEqual(len(page.fields), pagination.MAX_FIELDS_PER_PAGE)
            for field in page.fields:
                self.assertLessEqual(len(field.value), pagination.MAX_EMBED_FIELD_VALUE)

    def test_build_embed_truncates(self) -> None:
        embed = pagination.build_embed("T", "x" * 9000)
        self.assertLessEqual(len(embed.description), pagination.MAX_EMBED_DESCRIPTION)

    def test_pagination_view_navigation(self) -> None:
        pages = [pagination.build_embed(f"P{i}") for i in range(5)]
        view = pagination.PaginationView(pages)
        self.assertEqual(view.index, 0)
        self.assertEqual(view.total, 5)
        view.index = (view.index - 1) % view.total
        self.assertEqual(view.index, 4)


class ErrorTests(unittest.TestCase):
    def test_redact_masks_secrets(self) -> None:
        masked = errors.redact(
            "token=MTIzNDU2Nzg5MDEyMzQ1Ng== Authorization: Bearer sk-abcdefghijklmnop1234"
        )
        self.assertNotIn("MTIzNDU2Nzg5MDEyMzQ1Ng==", masked)
        self.assertNotIn("sk-abcdefghijklmnop1234", masked)
        self.assertIn("***", masked)

    def test_friendly_message_never_leaks_traceback(self) -> None:
        from discord.ext import commands

        message = errors.friendly_message(commands.CommandInvokeError(ValueError("boom")))
        self.assertIn("❌", message)
        self.assertNotIn("Traceback", message)

        for exc in (
            commands.MissingPermissions(["manage_guild"]),
            commands.BadArgument("giá trị sai"),
            commands.UserInputError("x"),
            commands.NoPrivateMessage(),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertNotIn("Traceback", errors.friendly_message(exc))

    def test_cooldown_message_is_specific(self) -> None:
        from discord import app_commands

        message = errors.friendly_message(
            app_commands.CommandOnCooldown(
                app_commands.Cooldown(1.5, per=1), retry_after=1.5
            )
        )
        self.assertIn("1.5", message)
        self.assertNotIn("Traceback", message)

    def test_recorder_is_bounded_and_counted(self) -> None:
        recorder = errors.ErrorRecorder()
        for i in range(errors.ERROR_BUFFER_SIZE + 30):
            recorder.record(ValueError(f"e{i}"), source="test", command="demo")
        stats = recorder.stats()
        self.assertLessEqual(stats["total"], errors.ERROR_BUFFER_SIZE)
        self.assertEqual(len(recorder.recent(5)), 5)
        self.assertTrue(recorder.top_types())

    def test_recorder_record_never_raises(self) -> None:
        recorder = errors.ErrorRecorder()
        try:
            raise RuntimeError("kaboom")
        except RuntimeError as exc:
            record = recorder.record(exc, source="test", command="x")
        self.assertEqual(record.exception_type, "RuntimeError")


if __name__ == "__main__":
    unittest.main()

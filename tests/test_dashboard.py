from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import discord

import keep_alive


class FakeCommand:
    def __init__(self, name: str, *, category: str = "Tools", description: str = "A bot command."):
        self.qualified_name = name
        self.signature = "target"
        self.help = description
        self.brief = ""
        self.cog_name = category
        self.hidden = False
        self.description = description
        self.aliases = ["p"] if name == "ping" else []


class FakeGuild:
    name = "Kai community"
    member_count = 42
    icon = None


class FakeBot:
    guilds = [FakeGuild()]
    cogs = {"Tools": object()}
    user = type("User", (), {"id": 123456789})()
    latency = 0.042
    shard_count = 1

    def walk_commands(self):
        return iter([FakeCommand("ping")])

    @property
    def tree(self):
        return type("Tree", (), {"walk_commands": lambda _self: iter([FakeCommand("ping")])})()

    def get_user(self, _user_id):
        return None




class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "economy.db"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE users (user_id INTEGER, balance INTEGER, bank INTEGER)")
            db.executemany(
                "INSERT INTO users VALUES (?, ?, ?)",
                [(42, 1200, 300), (9, 500, 100)],
            )
            db.commit()
        keep_alive.set_dashboard_bot(FakeBot(), self.database)
        keep_alive.set_ready_probe(lambda: True)
        self.app = keep_alive.build_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()


    def tearDown(self) -> None:
        keep_alive.set_dashboard_bot(None, self.database)
        keep_alive.set_ready_probe(None)
        self.temp_dir.cleanup()

    def test_homepage_renders_dashboard_and_assets(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"KaithenorzDoris", response.data)
        self.assertIn(b"COMMAND CENTER", response.data)
        self.assertIn(b"permissions=274878024768&amp;integration_type=0", response.data)
        self.assertNotIn(b"permissions=8&amp;integration_type=0", response.data)
        self.assertIn(b"Kh\xc3\xb4ng y\xc3\xaau c\xe1\xba\xa7u Administrator", response.data)
        self.assertNotIn(b"login-link", response.data)
        self.assertIn(b"Nh\xe1\xbb\xafng tay ch\xc6\xa1i n\xe1\xbb\x95i b\xe1\xba\xadt", response.data)

    def test_api_exposes_live_commands_servers_and_leaderboard(self) -> None:
        commands = self.client.get("/api/commands").get_json()["commands"]
        self.assertEqual(
            {command["name"] for command in commands},
            {"!ping", "!p", "/ping"},
        )
        servers = self.client.get("/api/servers").get_json()
        self.assertEqual(servers["total"], 1)
        self.assertEqual(servers["servers"][0]["name"], "Kai community")
        leaderboard = self.client.get("/api/leaderboard").get_json()
        self.assertTrue(leaderboard["available"])
        self.assertEqual(leaderboard["players"][0]["wealth"], 1500)
        games_db = sqlite3.connect(self.database)
        games_db.execute(
            "CREATE TABLE game_stats "
            "(user_id INTEGER, game TEXT, played INTEGER, wins INTEGER, profit INTEGER)"
        )
        games_db.executemany(
            "INSERT INTO game_stats VALUES (?, ?, ?, ?, ?)",
            [(42, "slots", 8, 5, 1200), (42, "dice", 4, 2, -200), (9, "slots", 3, 1, 500)],
        )
        games_db.commit()
        games_db.close()
        game_leaderboard = self.client.get("/api/games/leaderboard").get_json()
        self.assertTrue(game_leaderboard["available"])
        self.assertEqual(game_leaderboard["players"][0]["id"], "42")
        self.assertEqual(game_leaderboard["players"][0]["wins"], 7)
        self.assertEqual(game_leaderboard["players"][0]["played"], 12)
        self.assertEqual(game_leaderboard["players"][0]["profit"], 1000)
        status = self.client.get("/api/status").get_json()
        self.assertEqual(status["latency_ms"], 42)
        self.assertEqual(status["shard_count"], 1)
        self.assertEqual(status["members"], 42)

    def test_bot_invite_requests_only_starter_permissions(self) -> None:
        query = keep_alive.INVITE_URL.partition("?")[2]
        invite_permissions = int(
            next(item.split("=", 1)[1] for item in query.split("&") if item.startswith("permissions="))
        )
        permissions = discord.Permissions(invite_permissions)
        self.assertTrue(permissions.view_channel)
        self.assertTrue(permissions.send_messages)
        self.assertTrue(permissions.send_messages_in_threads)
        self.assertTrue(permissions.read_message_history)
        self.assertTrue(permissions.embed_links)
        self.assertTrue(permissions.attach_files)
        self.assertTrue(permissions.add_reactions)
        self.assertFalse(permissions.administrator)
        self.assertFalse(permissions.manage_guild)
        self.assertFalse(permissions.manage_roles)
        self.assertFalse(permissions.ban_members)
        self.assertFalse(permissions.kick_members)

    def test_server_directory_searches_and_paginates_all_guilds(self) -> None:
        bot = FakeBot()
        bot.guilds = [
            type(
                "Guild",
                (),
                {"name": f"Community {index:02d}", "member_count": index, "icon": None},
            )()
            for index in range(19)
        ]
        keep_alive.set_dashboard_bot(bot, self.database)
        first_page = self.client.get("/api/servers?per_page=8&page=1").get_json()
        self.assertEqual(first_page["total"], 19)
        self.assertEqual(first_page["total_pages"], 3)
        self.assertEqual(len(first_page["servers"]), 8)
        final_page = self.client.get("/api/servers?per_page=8&page=3").get_json()
        self.assertEqual(final_page["page"], 3)
        self.assertEqual(len(final_page["servers"]), 3)
        filtered = self.client.get("/api/servers?q=community%2007").get_json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["servers"][0]["name"], "Community 07")
        self.assertEqual(self.client.get("/api/servers?page=bad").status_code, 400)

    def test_status_does_not_report_online_without_a_registered_bot(self) -> None:
        keep_alive.set_dashboard_bot(None, self.database)
        keep_alive.set_ready_probe(None)
        status = self.client.get("/api/status").get_json()
        self.assertFalse(status["ready"])
        self.assertEqual(status["servers"], 0)
        self.assertIsNone(status["latency_ms"])

    def test_website_guide_uses_openai_without_persisting_conversation(self) -> None:
        with patch.dict(
            os.environ,
            {
                "WEBSITE_ASSISTANT_API_KEY": "website-test-key",
                "WEBSITE_ASSISTANT_MODEL": "gpt-4o-mini",
            },
        ):
            session_info = self.client.get("/api/session").get_json()
            self.assertTrue(session_info["guide_ai_enabled"])
            response = SimpleNamespace(output_text="Chào bạn, mình là Kai!")
            with patch("openai.OpenAI") as openai_client:
                openai_client.return_value.responses.create.return_value = response
                result = self.client.post(
                    "/api/guide",
                    json={"question": "Kai có thể giúp server thế nào?"},
                    headers={"X-CSRF-Token": session_info["csrf_token"]},
                )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["answer"], "Chào bạn, mình là Kai!")
        call = openai_client.return_value.responses.create.call_args.kwargs
        self.assertEqual(call["input"], "Kai có thể giúp server thế nào?")
        self.assertFalse(call["store"])
        self.assertEqual(call["max_output_tokens"], 280)
        openai_client.assert_called_once_with(
            api_key="website-test-key",
            timeout=12.0,
            max_retries=0,
        )

    def test_website_guide_checks_csrf_input_and_rate_limits(self) -> None:
        with patch.dict(os.environ, {"WEBSITE_ASSISTANT_API_KEY": "website-test-key"}):
            csrf = self.client.get("/api/session").get_json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf}
            self.assertEqual(
                self.client.post("/api/guide", json={"question": "hello"}).status_code,
                403,
            )
            self.assertEqual(
                self.client.post(
                    "/api/guide", json={"question": " "}, headers=headers
                ).status_code,
                400,
            )
            self.assertEqual(
                self.client.post(
                    "/api/guide", json={"question": "x" * 501}, headers=headers
                ).status_code,
                400,
            )
            with patch("openai.OpenAI") as openai_client:
                openai_client.return_value.responses.create.return_value = SimpleNamespace(
                    output_text="Đây là câu trả lời."
                )
                for _ in range(5):
                    result = self.client.post(
                        "/api/guide", json={"question": "hello"}, headers=headers
                    )
                    self.assertEqual(result.status_code, 200)
                limited = self.client.post(
                    "/api/guide", json={"question": "sixth"}, headers=headers
                )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(openai_client.return_value.responses.create.call_count, 5)

    def test_website_guide_falls_back_when_no_provider_key_is_set(self) -> None:
        with patch.dict(
            os.environ,
            {"WEBSITE_ASSISTANT_API_KEY": "", "OPENAI_API_KEY": ""},
        ):
            session_info = self.client.get("/api/session").get_json()
            self.assertFalse(session_info["guide_ai_enabled"])
            response = self.client.post(
                "/api/guide",
                json={"question": "Kai"},
                headers={"X-CSRF-Token": session_info["csrf_token"]},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "guide_ai_not_configured")

    def test_website_has_no_login_or_oauth_flow(self) -> None:
        self.assertEqual(self.client.get("/auth/discord").status_code, 404)
        self.assertEqual(self.client.get("/auth/discord/callback").status_code, 404)
        self.assertEqual(self.client.post("/auth/logout").status_code, 404)
        website_session = self.client.get("/api/session").get_json()
        self.assertNotIn("oauth_enabled", website_session)
        self.assertNotIn("user", website_session)
        self.assertNotIn("authenticated", website_session)

    def test_community_chat_is_public_but_requires_csrf(self) -> None:
        anonymous = self.client.get("/api/chat").get_json()
        self.assertEqual(anonymous["messages"], [])
        self.assertEqual(
            self.client.post("/api/chat", json={"content": "hello"}).status_code,
            403,
        )
        csrf = self.client.get("/api/session").get_json()["csrf_token"]
        posted = self.client.post(
            "/api/chat",
            json={"content": "<script>alert(1)</script>", "username": "Khách thử"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(posted.status_code, 201)
        messages = self.client.get("/api/chat").get_json()["messages"]
        self.assertEqual(messages[0]["content"], "<script>alert(1)</script>")
        self.assertEqual(messages[0]["username"], "Khách thử")
        refreshed_client = keep_alive.build_app().test_client()
        persisted = refreshed_client.get("/api/chat").get_json()["messages"]
        self.assertEqual(persisted[0]["id"], messages[0]["id"])

    def test_community_chat_validates_and_rate_limits_messages(self) -> None:
        csrf = self.client.get("/api/session").get_json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        self.assertEqual(
            self.client.post("/api/chat", json={"content": " "}, headers=headers).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/chat", json={"content": "x" * 501, "username": "Người thử"}, headers=headers
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                "/api/chat", json={"content": "hello", "username": "x"}, headers=headers
            ).status_code,
            400,
        )
        for _ in range(5):
            self.assertEqual(
                self.client.post(
                    "/api/chat", json={"content": "hello", "username": "Người thử"}, headers=headers
                ).status_code,
                201,
            )
        self.assertEqual(
            self.client.post(
                "/api/chat",
                json={"content": "sixth", "username": "Người thử"},
                headers=headers,
            ).status_code,
            429,
        )

    def test_existing_health_endpoints_remain_available(self) -> None:
        self.assertEqual(self.client.get("/health").get_json()["status"], "ok")
        self.assertEqual(self.client.get("/ready").status_code, 200)
        self.assertEqual(self.client.get("/api/status").get_json()["servers"], 1)
        self.assertEqual(
            self.client.get("/api/status").get_json()["invite_url"],
            keep_alive.INVITE_URL,
        )
        self.assertEqual(self.client.get("/assets/style.css").status_code, 200)


if __name__ == "__main__":
    unittest.main()

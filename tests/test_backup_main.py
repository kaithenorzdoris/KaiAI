# -*- coding: utf-8 -*-
"""Test tích hợp phần backup trong ``main.py`` (Render deploy không mất dữ liệu)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kai_core import backup as backup_mod


class RestoreBackupTests(unittest.IsolatedAsyncioTestCase):
    """``restore_backup`` phải kéo dữ liệu về trước khi cog nạp."""

    async def test_disabled_when_env_missing(self) -> None:
        env = {"KAI_BACKUP_URL": "", "KAI_BACKUP_ACCESS_KEY_ID": "", "KAI_BACKUP_SECRET_ACCESS_KEY": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            settings = backup_mod.load_settings()
        self.assertFalse(settings.enabled, "Không cấu hình thì phải tắt, không gọi mạng")

    async def test_pull_reports_restored_files(self) -> None:
        """``pull`` trả về danh sách file đã khôi phục (rỗng nếu không có)."""
        settings = backup_mod.BackupSettings(
            url="https://acc.r2.cloudflarestorage.com/bkt",
            access_key="ak", secret_key="sk",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "economy.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO users(user_id) VALUES(1)")
            conn.commit()
            conn.close()
            blob = backup_mod.build_archive(Path(tmp), [db])

            class _Resp:
                status = 200

                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *exc):
                    return False

                async def read(self_inner):
                    return blob

            class _Session:
                def get(self_inner, *a, **kw):
                    return _Resp()

            restored = await backup_mod.pull(settings, Path(tmp), session=_Session())
        self.assertIn("economy.db", restored)

    async def test_push_returns_false_when_disabled(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"KAI_BACKUP_URL": "", "KAI_BACKUP_ACCESS_KEY_ID": "", "KAI_BACKUP_SECRET_ACCESS_KEY": ""},
            clear=False,
        ):
            result = await backup_mod.push(
                backup_mod.load_settings(), Path(".")
            )
        self.assertFalse(result, "Tắt thì push phải trả về False, không ném lỗi")

    async def test_push_with_no_files_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = backup_mod.BackupSettings(
                url="https://x", access_key="a", secret_key="s"
            )
            result = await backup_mod.push(settings, Path(tmp))
        self.assertFalse(result, "Thư mục rỗng thì không có gì để backup")


class EndToEndWipeTests(unittest.TestCase):
    """Kịch bản thật: dữ liệu bị xoá sạch rồi khôi phục lại được."""

    def test_data_survives_full_container_wipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live"
            fresh = root / "fresh"          # giống container Render mới
            live.mkdir()
            fresh.mkdir()

            db = live / "economy.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE users(user_id INTEGER PRIMARY KEY, money INTEGER)")
            for i in range(50):
                conn.execute(
                    "INSERT INTO users(user_id,money) VALUES(?,?)", (i, 999)
                )
            conn.commit()
            conn.close()
            (live / "settings.json").write_text('{"a":1}')

            # Bot đẩy lên kho.
            blob = backup_mod.build_archive(live, backup_mod.collect_files(live))
            self.assertGreater(len(blob), 0)

            # Render deploy: container cũ biến mất, container mới trống.
            self.assertEqual(list(fresh.glob("*.db")), [])

            # Container mới tự kéo về.
            restored = backup_mod.extract_archive(blob, fresh)
            self.assertIn("economy.db", restored)
            self.assertIn("settings.json", restored)

            conn = sqlite3.connect(fresh / "economy.db")
            total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            money = conn.execute("SELECT SUM(money) FROM users").fetchone()[0]
            conn.close()
            self.assertEqual(total, 50, "Phải khôi phục đủ 50 tài khoản")
            self.assertEqual(money, 50 * 999)


if __name__ == "__main__":
    unittest.main()
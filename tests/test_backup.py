# -*- coding: utf-8 -*-
"""
Test cho ``kai_core/backup.py`` — cứu dữ liệu khi Render deploy lại container.

Kịch bản quan trọng nhất: ``test_roundtrip_preserves_sqlite_content`` mô phỏng
đúng tình huống của bạn — dữ liệu bị xoá sạch rồi khôi phục lại từ kho.
"""
from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from kai_core.backup import (
    BackupSettings,
    _endpoint,
    build_archive,
    collect_files,
    detect_region,
    extract_archive,
    load_settings,
    sigv4_headers,
)


class SigV4Tests(unittest.TestCase):
    """Chữ ký phải khớp vector chính thức của AWS.

    R2 dùng API tương thích S3 nên bắt buộc AWS Signature V4 — sai là
    403 ngay. Test dưới đây dùng bộ test vector "get-vanilla" của AWS để
    chứng minh thuật toán đúng, không phải đoán mò.
    """

    ACCESS = "AKIDEXAMPLE"
    SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"

    def test_matches_aws_get_vanilla_vector(self) -> None:
        headers = sigv4_headers(
            "GET",
            "https://example.amazonaws.com/",
            b"",
            self.ACCESS,
            self.SECRET,
            timestamp="20150830T123600Z",
        )
        self.assertEqual(headers["host"], "example.amazonaws.com")
        self.assertEqual(
            headers["x-amz-content-sha256"],
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "SHA-256 của payload rỗng",
        )
        self.assertIn("SignedHeaders=host;x-amz-content-sha256;x-amz-date", headers["authorization"])
        self.assertIn("Credential=AKIDEXAMPLE/20150830/", headers["authorization"])

    def test_signature_is_deterministic(self) -> None:
        a = sigv4_headers(
            "PUT", "https://x.r2.cloudflarestorage.com/b/k.zip", b"data",
            self.ACCESS, self.SECRET, timestamp="20240101T000000Z",
        )
        b = sigv4_headers(
            "PUT", "https://x.r2.cloudflarestorage.com/b/k.zip", b"data",
            self.ACCESS, self.SECRET, timestamp="20240101T000000Z",
        )
        self.assertEqual(a["authorization"], b["authorization"])

    def test_different_payload_changes_signature(self) -> None:
        a = sigv4_headers(
            "PUT", "https://x/b/k.zip", b"one",
            self.ACCESS, self.SECRET, timestamp="20240101T000000Z",
        )
        b = sigv4_headers(
            "PUT", "https://x/b/k.zip", b"two",
            self.ACCESS, self.SECRET, timestamp="20240101T000000Z",
        )
        self.assertNotEqual(a["authorization"], b["authorization"])

    def test_r2_uses_auto_region(self) -> None:
        headers = sigv4_headers(
            "GET", "https://acc.r2.cloudflarestorage.com/b/l.zip", b"",
            self.ACCESS, self.SECRET, timestamp="20240101T000000Z",
        )
        self.assertIn("/auto/s3/aws4_request", headers["authorization"])

    def test_detect_region_r2_is_auto(self) -> None:
        self.assertEqual(
            detect_region("https://acc.r2.cloudflarestorage.com/bkt"), "auto"
        )

    def test_detect_region_b2_uses_real_region(self) -> None:
        """B2 bat buoc region that trong SigV4 — dung "auto" se bi 403."""
        self.assertEqual(
            detect_region("https://s3.us-west-004.backblazeb2.com/kaiai-backup"),
            "us-west-004",
        )
        self.assertEqual(
            detect_region("https://s3.eu-central-003.backblazeb2.com/kaiai-backup/"),
            "eu-central-003",
        )

    def test_detect_region_aws_s3(self) -> None:
        self.assertEqual(
            detect_region("https://mybucket.s3.us-east-1.amazonaws.com/k.zip"),
            "us-east-1",
        )

    def test_detect_region_unknown_falls_back_to_auto(self) -> None:
        # Tuong thich nguoc: host la ma khong nhan ra thi dung "auto" nhu R2.
        self.assertEqual(detect_region("https://minio.local:9000/bkt"), "auto")
        self.assertEqual(detect_region(""), "auto")

    def test_b2_signature_embeds_real_region(self) -> None:
        headers = sigv4_headers(
            "GET", "https://s3.us-west-004.backblazeb2.com/bkt/latest.zip", b"",
            self.ACCESS, self.SECRET, timestamp="20240101T000000Z",
            region="us-west-004",
        )
        self.assertIn("/us-west-004/s3/aws4_request", headers["authorization"])

    def test_settings_resolve_region_from_url(self) -> None:
        s = BackupSettings(
            url="https://s3.us-west-004.backblazeb2.com/kaiai-backup",
            access_key="ak", secret_key="sk",
        )
        self.assertEqual(s.resolved_region, "us-west-004")
        r2 = BackupSettings(
            url="https://acc.r2.cloudflarestorage.com/bkt",
            access_key="ak", secret_key="sk",
        )
        self.assertEqual(r2.resolved_region, "auto")

    def test_settings_region_env_overrides_url(self) -> None:
        env = {
            "KAI_BACKUP_URL": "https://s3.us-west-004.backblazeb2.com/kaiai-backup",
            "KAI_BACKUP_ACCESS_KEY_ID": "ak",
            "KAI_BACKUP_SECRET_ACCESS_KEY": "sk",
            "KAI_BACKUP_REGION": "US-WEST-004",  # phai chuan hoa chu thuong
        }
        with mock.patch.dict(os.environ, env, clear=False):
            s = load_settings()
        self.assertEqual(s.region, "us-west-004")
        self.assertEqual(s.resolved_region, "us-west-004")

    def test_signature_key_derivation_matches_aws(self) -> None:
        """Khoá ký = HMAC 4 bậc; vector khoá chính thức của AWS (service=iam)."""
        from kai_core.backup import _signing_key

        derived = _signing_key(self.SECRET, "20150830", "us-east-1", "iam")
        self.assertEqual(
            derived.hex(),
            "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9",
            "Suy khoá ký phải khớp vector AWS",
        )

    def test_full_signature_matches_aws_reference(self) -> None:
        """Ví dụ đầy đủ của AWS: chữ ký phải ra đúng giá trị đã công bố.

        Đây là bằng chứng mạnh nhất rằng thuật toán đúng — nếu canonical
        request / string-to-sign sai chỗ nào thì chữ ký sẽ lệch.
        Tham chiếu: AWS "auth-sigv4-examples-calculation" (service="service").
        """
        headers = sigv4_headers(
            "GET",
            "https://example.amazonaws.com/",
            b"",
            self.ACCESS,
            self.SECRET,
            timestamp="20150830T123600Z",
            region="us-east-1",
            service="service",
            include_content_hash=False,
        )
        self.assertEqual(
            headers["authorization"].split("Signature=")[1],
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
            "Chữ ký phải khớp ví dụ của AWS",
        )
        self.assertIn("SignedHeaders=host;x-amz-date", headers["authorization"])


def _make_db(path: Path, rows: int = 3) -> None:
    """Tạo một SQLite tối giản với ``rows`` bản ghi."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, money INTEGER)"
    )
    for i in range(rows):
        conn.execute(
            "INSERT OR REPLACE INTO users(user_id,money) VALUES(?,?)",
            (i + 1, 1000 * i),
        )
    conn.commit()
    conn.close()


class SettingsTests(unittest.TestCase):
    """Cấu hình đọc từ biến môi trường."""

    def test_disabled_without_url_or_token(self) -> None:
        self.assertFalse(BackupSettings().enabled)
        self.assertFalse(BackupSettings(url="https://x", access_key="a").enabled)
        self.assertFalse(BackupSettings(url="https://x", secret_key="s").enabled)
        self.assertFalse(BackupSettings(access_key="a", secret_key="s").enabled)
        self.assertTrue(
            BackupSettings(url="https://x", access_key="a", secret_key="s").enabled
        )

    def test_load_settings_from_env(self) -> None:
        env = {
            "KAI_BACKUP_URL": "https://acc.r2.cloudflarestorage.com/bkt",
            "KAI_BACKUP_ACCESS_KEY_ID": "ak",
            "KAI_BACKUP_SECRET_ACCESS_KEY": "sk",
            "KAI_BACKUP_PREFIX": "bot",
            "KAI_BACKUP_INTERVAL_MIN": "15",
            "KAI_BACKUP_KEEP": "3",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            s = load_settings()
        self.assertEqual(s.url, env["KAI_BACKUP_URL"])
        self.assertEqual(s.access_key, "ak")
        self.assertEqual(s.secret_key, "sk")
        self.assertEqual(s.prefix, "bot")
        self.assertEqual(s.interval_min, 15)
        self.assertEqual(s.keep, 3)
        self.assertEqual(s.key, "latest.zip")
        self.assertEqual(_endpoint(s, s.key),
                         "https://acc.r2.cloudflarestorage.com/bkt/bot/latest.zip")

    def test_bad_env_numbers_fall_back(self) -> None:
        with mock.patch.dict(os.environ, {"KAI_BACKUP_INTERVAL_MIN": "abc"}, clear=False):
            self.assertEqual(load_settings().interval_min, 30)

    def test_interval_is_clamped_to_at_least_one_minute(self) -> None:
        with mock.patch.dict(os.environ, {"KAI_BACKUP_INTERVAL_MIN": "0"}, clear=False):
            self.assertGreaterEqual(load_settings().interval_min, 1)


class CollectFilesTests(unittest.TestCase):
    """Chỉ gom ``.db``/``.json``, bỏ log và bản backup cũ."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "economy.db").write_bytes(b"x")
        (self.dir / "data.json").write_text("{}")
        (self.dir / "kai.log").write_text("noise")
        (self.dir / "KaiAI_backup_20260101.zip").write_bytes(b"z")
        (self.dir / "notes.txt").write_text("skip")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_only_db_and_json(self) -> None:
        names = {p.name for p in collect_files(self.dir)}
        self.assertEqual(names, {"economy.db", "data.json"})


class ArchiveTests(unittest.TestCase):
    """Đóng gói / giải nén và khôi phục dữ liệu."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip_preserves_sqlite_content(self) -> None:
        _make_db(self.dir / "economy.db", rows=5)
        blob = build_archive(self.dir, collect_files(self.dir))
        # Xoá sạch — giống hệt Render deploy lại container.
        (self.dir / "economy.db").unlink()
        restored = extract_archive(blob, self.dir)
        self.assertIn("economy.db", restored)
        conn = sqlite3.connect(self.dir / "economy.db")
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        conn.close()
        self.assertEqual(total, 5, "Sau khi mất container, dữ liệu phải về đủ")

    def test_snapshot_is_consistent_while_writing(self) -> None:
        """Bản backup phải đọc được ngay cả khi DB đang ở chế độ WAL."""
        db = self.dir / "economy.db"
        _make_db(db, rows=2)
        conn = sqlite3.connect(db)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO users(user_id,money) VALUES(99, 777)")
        conn.commit()          # dữ liệu còn nằm trong WAL, chưa vào file chính
        blob = build_archive(self.dir, [db])
        conn.close()
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            out = self.dir / "restored.db"
            out.write_bytes(zf.read("economy.db"))
        c2 = sqlite3.connect(out)
        rows = c2.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        c2.close()
        self.assertEqual(rows, 3, "Phải gộp cả WAL, không mất giao dịch chưa flush")

    def test_corrupt_zip_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            extract_archive(b"khong phai zip", self.dir)

    def test_truncated_zip_raises(self) -> None:
        _make_db(self.dir / "economy.db", rows=1)
        blob = build_archive(self.dir, collect_files(self.dir))
        with self.assertRaises(Exception):
            extract_archive(blob[: len(blob) // 2], self.dir)

    def test_path_traversal_is_blocked(self) -> None:
        """Archive độc hại không được ghi ra ngoài thư mục dữ liệu."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../escaped.txt", "bad")
            zf.writestr("ok.txt", "good")
        extract_archive(buf.getvalue(), self.dir)
        self.assertFalse((self.dir.parent / "escaped.txt").exists())
        self.assertTrue((self.dir / "ok.txt").exists())

    def test_dotfiles_are_skipped(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(".env", "SECRET=1")
        restored = extract_archive(buf.getvalue(), self.dir)
        self.assertEqual(restored, [], "Không được ghi file ẩn từ archive ngoài")


if __name__ == "__main__":
    unittest.main()
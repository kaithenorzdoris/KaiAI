"""Smoke test: import toàn bộ Cog + entrypoint để bắt lỗi ở mức module.

Các lỗi như ``NameError``, ``ImportError`` chỉ xuất hiện khi import module —
AST test không thể bắt được.  Bộ test này nằm ở cuối nên nó cũng đóng vai trò
"chốt chặn" trước khi giao code.
"""

from __future__ import annotations

import importlib
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
COGS_DIR = ROOT / "cogs"


def cog_modules() -> list[str]:
    """Tên module cog dạng ``cogs.<stem>`` (bỏ qua ``__init__``)."""
    return sorted(
        f"cogs.{path.stem}" for path in COGS_DIR.glob("*.py") if not path.stem.startswith("_")
    )


class ImportSmokeTests(unittest.TestCase):
    def _import_all(self, names: list[str]) -> list[str]:
        failures: list[str] = []
        for name in names:
            try:
                importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001 - muốn ghi lại mọi lỗi import
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
        return failures

    def test_all_cogs_import_cleanly(self) -> None:
        names = cog_modules()
        self.assertGreaterEqual(len(names), 9, "Không tìm thấy đủ Cog trong cogs/")
        self.assertEqual(self._import_all(names), [])

    def test_entrypoints_import_cleanly(self) -> None:
        self.assertEqual(self._import_all(["kai_core", "keep_alive", "main"]), [])

    def test_cogs_package_is_importable(self) -> None:
        self.assertIsNotNone(importlib.import_module("cogs"))


if __name__ == "__main__":
    unittest.main()

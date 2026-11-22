"""Kiểm thử toàn vẹn mã nguồn KaiAI.

Các test trong file này **không cần** discord.py: chúng phân tích cú pháp (AST)
và kiểm tra cấu trúc thư mục nên chạy được ở mọi môi trường.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from collections.abc import Iterable

ROOT = pathlib.Path(__file__).resolve().parents[1]
COGS = ROOT / "cogs"
CORE = ROOT / "kai_core"

SKIP_DIRS = {
    "_backup_pre_v5",
    "archive_old",
    ".git",
    "__pycache__",
    ".tmp_read",
    ".venv",
    "venv",
}

LEGACY_COGS = {"admin.py", "ai_chat.py", "database.py", "games.py", "logs.py", "love.py"}
EXTRA_COGS = {"kai_premium.py", "utility_pro.py", "system.py", "diagnostics.py", "logs_center.py"}
CORE_MODULES = {
    "__init__.py",
    "config.py",
    "logging_setup.py",
    "utils.py",
    "database.py",
    "ui.py",
    "version.py",
    "errors.py",
    "pagination.py",
    "parity.py",
    "ratelimit.py",
    "idempotency.py",
    "migrations.py",
    "logstore.py",
    "economy.py",
    "security.py",
    "ai/__init__.py",
    "ai/provider.py",
    "ai/openai_provider.py",
    "ai/gemini_provider.py",
    "ai/router.py",
    "ai/memory.py",
}


def iter_python_files() -> Iterable[pathlib.Path]:
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class SourceIntegrityTests(unittest.TestCase):
    """Mọi file Python phải parse được và có cấu trúc đúng."""

    def test_all_python_files_parse(self) -> None:
        for path in iter_python_files():
            with self.subTest(path=str(path.relative_to(ROOT))):
                parse(path)

    def test_legacy_cogs_are_present(self) -> None:
        actual = {p.name for p in COGS.glob("*.py")}
        self.assertTrue(LEGACY_COGS.issubset(actual), f"Thiếu: {LEGACY_COGS - actual}")

    def test_premium_and_system_cogs_exist(self) -> None:
        for name in sorted(EXTRA_COGS):
            self.assertTrue((COGS / name).is_file(), f"Thiếu cogs/{name}")

    def test_kai_core_package_is_complete(self) -> None:
        self.assertTrue(CORE.is_dir(), "Thiếu package kai_core/")
        for name in sorted(CORE_MODULES):
            self.assertTrue((CORE / name).is_file(), f"Thiếu kai_core/{name}")

    def test_no_infrastructure_files_inside_cogs(self) -> None:
        for name in ("main.py", "keep_alive.py"):
            self.assertFalse(
                (COGS / name).exists(),
                f"{name} không được đặt trong cogs/ (xem FIX_REPORT.md).",
            )

    def test_every_cog_exposes_setup_function(self) -> None:
        for path in sorted(COGS.glob("*.py")):
            if path.name == "__init__.py":
                continue
            with self.subTest(cog=path.name):
                tree = parse(path)
                has_setup = any(
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "setup"
                    for node in tree.body
                )
                self.assertTrue(has_setup, f"{path.name} thiếu hàm setup(bot)")


class CodeQualityTests(unittest.TestCase):
    """Các quy tắc chất lượng đã được thống nhất cho KaiAI."""

    def test_no_bare_except(self) -> None:
        offenders: list[str] = []
        for path in iter_python_files():
            for node in ast.walk(parse(path)):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], f"Còn 'except:' trống tại: {offenders}")

    def test_no_deprecated_utcnow_attribute(self) -> None:
        """``discord.utils.utcnow``/``datetime.utcnow`` không nên dùng cho code mới."""
        offenders: list[str] = []
        for path in iter_files_below(COGS, CORE):
            text = path.read_text(encoding="utf-8")
            if "datetime.utcnow()" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"Còn dùng datetime.utcnow(): {offenders}")

    def test_no_duplicate_command_names(self) -> None:
        registry: dict[tuple[str, str], list[str]] = {}
        for path in sorted(COGS.glob("*.py")):
            tree = parse(path)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in node.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    kind = _decorator_kind(decorator)
                    if kind is None:
                        continue
                    for keyword in decorator.keywords:
                        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                            registry.setdefault((kind, str(keyword.value.value)), []).append(path.name)
                        elif keyword.arg == "aliases" and isinstance(keyword.value, ast.List):
                            for element in keyword.value.elts:
                                if isinstance(element, ast.Constant):
                                    registry.setdefault((kind, str(element.value)), []).append(path.name)
        duplicates = {key: value for key, value in registry.items() if len(value) > 1}
        self.assertEqual(duplicates, {}, f"Trùng tên/alias lệnh: {duplicates}")


def iter_files_below(*directories: pathlib.Path) -> Iterable[pathlib.Path]:
    for directory in directories:
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.py")):
            if path.name == "__init__.py":
                continue
            yield path


def _decorator_kind(decorator: ast.Call) -> str | None:
    func = decorator.func
    if not isinstance(func, ast.Attribute):
        return None
    owner = func.value
    if not isinstance(owner, ast.Name):
        return None
    if owner.id == "commands" and func.attr in {"command", "group"}:
        return "prefix"
    if owner.id == "app_commands" and func.attr == "command":
        return "slash"
    return None


if __name__ == "__main__":
    unittest.main()

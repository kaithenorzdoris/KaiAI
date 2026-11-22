#!/usr/bin/env python3
"""Kiểm tra tính toàn vẹn mã nguồn KaiAI.

Cách dùng::

    python scripts/validate.py

Script này KHÔNG cần cài discord.py. Nó kiểm tra:

* Cú pháp toàn bộ file ``.py`` (compile).
* Mỗi Cog trong ``cogs/`` đều có hàm ``setup(bot)``.
* Không có ``except:`` trống trong ``cogs/`` và ``kai_core/``.
* Không trùng tên / alias lệnh giữa các Cog.
* ``cogs/`` không chứa file hạ tầng (``main.py``, ``keep_alive.py``).
* Package ``kai_core`` có đầy đủ module.

Mã thoát: ``0`` = đạt, ``1`` = có vấn đề.
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
COGS_DIR = ROOT / "cogs"
CORE_DIR = ROOT / "kai_core"

SKIP_PARTS = {
    "_backup_pre_v5",
    "archive_old",
    ".git",
    "__pycache__",
    ".tmp_read",
    ".venv",
    "venv",
}

REQUIRED_COGS = (
    "admin.py",
    "ai_chat.py",
    "database.py",
    "diagnostics.py",
    "games.py",
    "kai_premium.py",
    "logs.py",
    "logs_center.py",
    "love.py",
    "system.py",
    "utility_pro.py",
)
REQUIRED_CORE = ("__init__.py", "config.py", "logging_setup.py", "utils.py", "database.py", "ui.py",
                 "errors.py", "pagination.py", "parity.py", "version.py", "ratelimit.py",
                 "idempotency.py", "migrations.py", "logstore.py", "economy.py", "security.py")

CommandKey = tuple[str, str]


def _enable_utf8_output() -> None:
    """Tránh ``UnicodeEncodeError`` khi console dùng mã hoá khác (cp1252 trên Windows).

    Script chạy độc lập nên không thể dùng ``kai_core`` — tự bảo vệ tại đây.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        if encoding in {"utf8", "utf"}:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            pass


def python_files() -> list[pathlib.Path]:
    return [
        path
        for path in sorted(ROOT.rglob("*.py"))
        if not any(part in SKIP_PARTS for part in path.parts)
    ]


def check_syntax() -> list[str]:
    problems: list[str] = []
    for path in python_files():
        rel = path.relative_to(ROOT)
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            problems.append(f"{rel}: không phải UTF-8 ({exc})")
            continue
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            problems.append(f"{rel}:{exc.lineno}: {exc.msg}")
    return problems


def check_cog_contract() -> list[str]:
    problems: list[str] = []
    for name in REQUIRED_COGS:
        path = COGS_DIR / name
        if not path.is_file():
            problems.append(f"Thiếu cog bắt buộc: cogs/{name}")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "setup"
            for node in tree.body
        )
        if not found:
            problems.append(f"cogs/{name}: thiếu hàm setup(bot)")
    for name in ("main.py", "keep_alive.py"):
        if (COGS_DIR / name).exists():
            problems.append(f"cogs/{name} không được phép tồn tại (file hạ tầng).")
    return problems


def check_core_package() -> list[str]:
    problems: list[str] = []
    if not CORE_DIR.is_dir():
        return ["Thiếu package kai_core/"]
    for name in REQUIRED_CORE:
        if not (CORE_DIR / name).is_file():
            problems.append(f"Thiếu kai_core/{name}")
    return problems


def _decorator_kind(decorator: ast.Call) -> str | None:
    func = decorator.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None
    if func.value.id == "commands" and func.attr in {"command", "group"}:
        return "prefix"
    if func.value.id == "app_commands" and func.attr == "command":
        return "slash"
    return None


def collect_commands() -> dict[CommandKey, list[str]]:
    registry: dict[CommandKey, list[str]] = {}
    for path in sorted(COGS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
    return registry


def check_command_conflicts() -> list[str]:
    registry = collect_commands()
    return [
        f"Trùng {'lệnh' if kind == 'prefix' else 'slash'} '{name}': {', '.join(sorted(set(owners)))}"
        for (kind, name), owners in registry.items()
        if len(set(owners)) > 1
    ]


def check_bare_except() -> list[str]:
    problems: list[str] = []
    for directory in (COGS_DIR, CORE_DIR):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    rel = path.relative_to(ROOT)
                    problems.append(f"{rel}:{node.lineno}: dùng 'except:' trống")
    return problems


CHECKS = (
    ("Cú pháp", check_syntax),
    ("Hợp đồng Cog", check_cog_contract),
    ("Package kai_core", check_core_package),
    ("Trùng tên lệnh", check_command_conflicts),
    ("except trống", check_bare_except),
)


def main() -> int:
    _enable_utf8_output()
    print("=" * 66)
    print(" KaiAI — Kiểm tra toàn vẹn mã nguồn")
    print("=" * 66)
    total = 0
    for title, check in CHECKS:
        problems = check()
        total += len(problems)
        if problems:
            print(f"[FAIL] {title}: {len(problems)} vấn đề")
            for problem in problems:
                print(f"        - {problem}")
        else:
            print(f"[ OK ] {title}")
    registry = collect_commands()
    print("-" * 66)
    print(f" Đã quét {len(python_files())} file Python · {len(registry)} tên lệnh/alias.")
    print("=" * 66)
    if total:
        print(f" KẾT QUẢ: {total} vấn đề cần xử lý.")
        return 1
    print(" KẾT QUẢ: không phát hiện vấn đề nào. ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())


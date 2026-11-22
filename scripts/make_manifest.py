#!/usr/bin/env python3
"""Sinh lại ``SOURCE_MANIFEST.json`` — danh mục file nguồn kèm sha256.

Cách dùng::

    python scripts/make_manifest.py --check   # chỉ đối chiếu, không ghi
    python scripts/make_manifest.py           # ghi lại manifest

Manifest là dấu vân tay của mã nguồn: nếu file nào bị sửa ngoài ý muốn,
``--check`` sẽ báo lệch.  File manifest tự thân không được liệt kê (tránh
vòng lặp tham chiếu).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "SOURCE_MANIFEST.json"

#: Thư mục/vùng không phải mã nguồn.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp_read",
    "_backup_pre_v5",
    "archive_old",
    "build",
    "dist",
}

#: Mở rộng file được phép đưa vào manifest.
SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".json",
    ".example",
    ".lock",
    ".css",
    ".js",
    ".html",
    "",
}

#: Tên file riêng lẻ được phép (kèm suffix rỗng).
NAMES = {"Dockerfile", "LICENSE", "VERSION", ".gitignore"}

#: Không liệt kê file sinh ra runtime.
EXCLUDE_FILES = {"SOURCE_MANIFEST.json", "kai.log"}
EXCLUDE_PREFIXES = ("kai.log", "art_", "KaiAI_backup_")


def _enable_utf8_output() -> None:
    """Tránh ``UnicodeEncodeError`` khi console dùng mã hoá khác (cp1252 trên Windows)."""
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


def iter_files() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.name in EXCLUDE_FILES or rel.name.startswith(EXCLUDE_PREFIXES):
            continue
        if path.suffix.lower() in SUFFIXES or path.name in NAMES:
            found.append(path)
    return found


def build_manifest() -> dict:
    files = []
    for path in iter_files():
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return {"generated_at": None, "files": files}


def main() -> int:
    _enable_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="chỉ đối chiếu, không ghi file")
    args = parser.parse_args()

    current = build_manifest()
    if not OUTPUT.exists():
        print(f"Thiếu {OUTPUT.name}")
        return 1

    existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
    old = {item["path"]: item for item in existing.get("files", [])}
    new = {item["path"]: item for item in current["files"]}

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(new) & set(old) if new[p]["sha256"] != old[p]["sha256"])

    if args.check:
        for label, items in (("thêm", added), ("xóa", removed), ("đổi", changed)):
            for item in items:
                print(f"  [{label}] {item}")
        if added or removed or changed:
            print(
                f"SOURCE_MANIFEST.json LỆCH: +{len(added)} -{len(removed)} ~{len(changed)}. "
                "Chạy `python scripts/make_manifest.py` để cập nhật."
            )
            return 1
        print(f"SOURCE_MANIFEST.json khớp ({len(new)} file).")
        return 0

    payload = {"generated_at": time.time(), "files": current["files"]}
    OUTPUT.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Đã ghi {OUTPUT.name} với {len(new)} file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

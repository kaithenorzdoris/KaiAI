"""Cấu hình pytest: đảm bảo project root luôn nằm trong ``sys.path``.

Nhờ vậy các test có thể ``import main`` và ``import kai_core`` dù chúng nằm
trong thư mục ``tests/``.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

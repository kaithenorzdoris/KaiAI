"""
Xac nhan thao tac giao dich cua AI (pending confirmation).

Vay sao
--------
AI **khong bao gio** tu chuyen tien. Khi AI hieu yeu cau giao dich, no chi
tao mot *yeu cau cho* — cong lai tra ve ``need_confirm=True`` va dung id
cua yeu cau do. Nguoi dung phai go ``!xacnhan <token>`` trong khoang
:data:`CONFIRM_TTL` giay moi thuc hien.

Tin dung
--------
* **Token ngau nhien** (``secrets.token_urlsafe``) — khong phai ID tu tang.
* **Han tuyet doi** (mon ton tai, mat khi het han).
* **Dung mot lan** — xac nhan xong hoac huy xong, yeu cau bi xoa ngay.
* **Phan quyen** — chi chinh chu yeu cau moi xac nhan duoc.
* **Audit log** — moi thao tac da xac nhan deu ghi lai de doi chieu.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..logging_setup import get_logger

log = get_logger("ai.confirm")

#: Yeu cau xac nhan het han sau 60 giay.
CONFIRM_TTL = 60.0

#: Do dai token (ky tu URL-safe).
TOKEN_BYTES = 9


@dataclass(slots=True)
class PendingAction:
    """Mot yeu cau giao dich cho, **chua** duoc thuc hien."""

    token: str
    user_id: int
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    expires_at: float = 0.0

    def is_expired(self, now: float | None = None) -> bool:
        """Token đã hết hạn chưa."""
        return (now if now is not None else time.time()) >= self.expires_at

    def describe(self) -> str:
        """Mô tả ngắn để hiển thị cho người dùng."""
        return f"{self.action}({self.args})"


class PendingActions:
    """Kho yeu cau cho, khoang :data:`CONFIRM_TTL` giay cho moi yeu cau."""

    def __init__(self, ttl: float = CONFIRM_TTL, max_per_user: int = 3) -> None:
        self.ttl = float(ttl)
        self.max_per_user = int(max_per_user)
        self._items: dict[str, PendingAction] = {}

    def create(
        self,
        *,
        user_id: int,
        action: str,
        args: dict[str, Any] | None = None,
    ) -> PendingAction:
        """Tạo yeu cau mới và trả về nó (tự dọn yeu cau cũ của nguoi dung)."""
        now = time.time()
        self.purge()
        self._drop_oldest(user_id)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        item = PendingAction(
            token=token,
            user_id=int(user_id),
            action=str(action),
            args=dict(args or {}),
            created_at=now,
            expires_at=now + self.ttl,
        )
        self._items[token] = item
        return item

    def get(self, token: str, *, user_id: int | None = None) -> PendingAction | None:
        """Lấy yeu cau theo token (không xoa). ``None`` neu khong hợp lệ."""
        item = self._items.get(str(token))
        if item is None or item.is_expired():
            return None
        if user_id is not None and item.user_id != int(user_id):
            return None
        return item

    def take(self, *, user_id: int, token: str) -> PendingAction | None:
        """**Lấy và xoá** yeu cau trong một lần goi (dung-mot-lan)."""
        item = self.get(token, user_id=user_id)
        if item is None:
            return None
        self._items.pop(item.token, None)
        return item

    def cancel(self, *, user_id: int, token: str) -> bool:
        """Huy yeu cau. ``True`` neu thuc su co xoa."""
        return self.take(user_id=user_id, token=token) is not None

    def cancel_all(self, user_id: int) -> int:
        """Huy **tat ca** yeu cau cua mot nguoi. Tra ve so luong da xoa."""
        self.purge()
        tokens = [t for t, it in self._items.items() if it.user_id == int(user_id)]
        for t in tokens:
            self._items.pop(t, None)
        return len(tokens)

    def list_for(self, user_id: int) -> list[PendingAction]:
        """Danh sach yeu cau con hieu luc cua mot nguoi (moi nhat truoc)."""
        self.purge()
        items = [it for it in self._items.values() if it.user_id == int(user_id)]
        items.sort(key=lambda it: it.created_at, reverse=True)
        return items

    def purge(self) -> int:
        """Xoa moi yeu cau het han. Tra ve so luong da xoa."""
        now = time.time()
        dead = [t for t, it in self._items.items() if it.is_expired(now)]
        for t in dead:
            self._items.pop(t, None)
        return len(dead)

    def _drop_oldest(self, user_id: int) -> None:
        """Giu toi da ``max_per_user`` yeu cau cho cho mot nguoi."""
        mine = [it for it in self._items.values() if it.user_id == int(user_id)]
        overflow = len(mine) - self.max_per_user + 1
        if overflow <= 0:
            return
        mine.sort(key=lambda it: it.created_at)
        for it in mine[:overflow]:
            self._items.pop(it.token, None)

    @property
    def size(self) -> int:
        """So yeu cau dang cho (chua het han)."""
        self.purge()
        return len(self._items)


async def run_confirmed(
    pending: PendingActions,
    *,
    user_id: int,
    token: str,
    runner: Callable[..., Any],
) -> tuple[bool, str]:
    """Xac nhan roi chay ``runner(**item.args)``.

    ``runner`` la ham giao dich **that** cua cog (vi du ``games.transfer``) —
    ham nay giu nguyen atomic/rollback cua bot. Tra ve ``(ok, thong_bao)``.
    """
    item = pending.take(user_id=user_id, token=token)
    if item is None:
        return False, (
            "Yêu cầu không hợp lệ, đã hết hạn hoặc không phải của bạn. "
            f"Hãy nhờ AI tạo lại (hạn {int(pending.ttl)}s)."
        )
    try:
        result = runner(**item.args)
        if hasattr(result, "__await__"):
            result = await result
    except TypeError as exc:
        log.warning("Xac nhan that bai (%s): %s", item.action, exc)
        return False, f"Không thực hiện được `{item.action}`: {exc}"
    except Exception as exc:  # noqa: BLE001 - lỗi giao dich phải báo lại
        log.warning("Loi khi chay %s: %s", item.action, exc)
        return False, f"Giao dịch lỗi: {exc}"
    log.info(
        "AI action confirmed: user=%s action=%s args=%s",
        user_id, item.action, item.args,
    )
    # ``runner`` có thể trả về ``ToolResult`` — phải tôn trọng ``ok`` của nó
    # để "đã xác nhận" không đồng nghĩa "đã thành công".
    if getattr(result, "ok", True) is False:
        msg = getattr(result, "message", "") or f"`{item.action}` thất bại."
        return False, msg
    return True, f"Đã thực hiện `{item.action}`."
    def is_expired(self, now: float | None = None) -> bool:
        """Token đã hết hạn chưa."""
        return (now if now is not None else time.time()) >= self.expires_at

    def describe(self) -> str:
        """Mô tả ngắn để hiển thị cho người dùng."""
        return f"{self.action}({self.args})"
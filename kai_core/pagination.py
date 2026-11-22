"""Pagination Engine + Smart Message Splitter (yêu cầu #4, #5, #6).

Vấn đề giải quyết
------------------
Discord giới hạn cứng: 2000 ký tự/tin nhắn, 4096 ký tự mô tả embed, 25 field,
6000 ký tự tổng mọi field. Bot 24/7 chắc chắn sẽ gặp các danh sách dài
(leaderboard, market, transactions, logs, help, inventory…) nếu không có lớp
bảo vệ. Module này cung cấp **một** lớp bảo vệ dùng chung cho mọi Cog.

Tính năng
---------
* :func:`split_text` — cắt thông minh: không cắt giữa từ, không phá vỡ link,
  và **tự đóng/mở lại** khối code ```` ``` ```` để mỗi trang vẫn là code block hợp lệ.
* :class:`PaginationView` — nút ⏮ ◀ ▶ ⏭ ✖ kèm bộ đếm trang, khoá theo người mở.
* :class:`PaginationSelect` — phân trang bằng **select menu** cho danh sách dài.
* :func:`paginate_sequence` — chia danh sách thành trang.
* :func:`build_field_pages` — dựng trang embed từ danh sách, tự chia khi > 25 field.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import TypeVar

import discord

__all__ = (
    "MAX_MESSAGE",
    "MAX_EMBED_DESCRIPTION",
    "MAX_EMBED_FIELD_VALUE",
    "MAX_FIELDS_PER_PAGE",
    "Fence",
    "split_text",
    "paginate_sequence",
    "build_field_pages",
    "build_embed",
    "PaginationView",
    "PaginationSelect",
    "send_paginated",
    "safe_edit",
)

MAX_MESSAGE = 2000
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_FIELD_VALUE = 1024
MAX_FIELDS_PER_PAGE = 25

T = TypeVar("T")

_FENCE = "```"


# ---------------------------------------------------------------------------
# Smart splitter
# ---------------------------------------------------------------------------


class Fence:
    """Theo dõi trạng thái khối code để cắt mà vẫn tạo ra code block hợp lệ."""

    __slots__ = ("lang",)

    def __init__(self) -> None:
        self.lang: str | None = None

    @property
    def open(self) -> bool:
        return self.lang is not None

    def toggle(self, line: str) -> str | None:
        """Xử lý một dòng, trả về ngôn ngữ nếu vừa đóng khối."""
        stripped = line.lstrip()
        if not stripped.startswith(_FENCE):
            return None
        if not self.open:
            self.lang = stripped[3:].strip() or ""
            return None
        self.lang = None
        return None


_URL_RE = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)


def _safe_split_line(line: str, limit: int) -> list[str]:
    """Cắt một dòng dài theo ranh giới từ.

    Token giống URL được coi là **nguyên tử** và không bao giờ bị cắt (yêu cầu
    #6). Trường hợp URL dài hơn ``limit`` thì nó chiếm trọn một trang — thay vì
    gửi ra một liên kết hỏng.
    """
    words = line.split(" ")
    chunks: list[str] = []
    buffer = ""
    for word in words:
        candidate = f"{buffer} {word}" if buffer else word
        if len(candidate) <= limit or _URL_RE.match(word):
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
        # Từ đơn lẻ dài hơn limit và không phải URL → cắt cứng theo ký tự.
        while len(word) > limit:
            chunks.append(word[:limit])
            word = word[limit:]
        buffer = word
    if buffer:
        chunks.append(buffer)
    return chunks or [""]


def split_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Chia nội dung dài thành các trang không vượt quá ``limit``.

    Bảo đảm:
    * Không cắt giữa từ (cắt theo khoảng trắng).
    * Link/URL không bị cắt (URL không chứa khoảng trắng).
    * Khối code ```` ``` ```` luôn **đóng rồi mở lại** ở trang kế tiếp nên mỗi
      trang vẫn là một code block markdown hợp lệ.
    """
    if limit <= 0:
        raise ValueError("limit phải lớn hơn 0")
    text = "" if text is None else str(text)
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    fence = Fence()
    pages: list[str] = []
    buffer: list[str] = []
    length = 0

    def flush() -> None:
        """Đóng trang hiện tại, tự đóng fence nếu đang mở."""
        nonlocal length
        body = "\n".join(buffer)
        buffer.clear()
        length = 0
        if fence.open:
            body += f"\n{_FENCE}"
        if body.strip():
            pages.append(body)

    for line in text.split("\n"):
        fence.toggle(line)
        pieces = _safe_split_line(line, limit)
        for piece in pieces:
            extra = len(piece) + (1 if buffer else 0)
            closing = len(_FENCE) + 1 if fence.open else 0
            if buffer and length + extra + closing > limit:
                flush()
                if fence.open:
                    buffer.append(f"{_FENCE}{fence.lang}")
                    length += len(_FENCE) + len(fence.lang) + 1
            buffer.append(piece)
            length += len(piece) + (1 if len(buffer) > 1 else 0)

    if buffer:
        body = "\n".join(buffer)
        if fence.open:
            body += f"\n{_FENCE}"
        if body.strip():
            pages.append(body)

    # Bảo đảm không trang nào vượt limit (phòng thủ) — chia lại theo ranh giới từ
    # để **không phá vỡ link** ở lượt cắt phòng thủ này.
    final: list[str] = []
    for page in pages:
        if len(page) <= limit:
            final.append(page)
            continue
        buffer: list[str] = []
        for line in page.split("\n"):
            for piece in _safe_split_line(line, limit):
                candidate = "\n".join([*buffer, piece])
                if buffer and len(candidate) > limit:
                    final.append("\n".join(buffer))
                    buffer = [piece]
                else:
                    buffer.append(piece)
        if buffer:
            final.append("\n".join(buffer))
    return final or [""]


# ---------------------------------------------------------------------------
# Chia trang & dựng embed
# ---------------------------------------------------------------------------


def paginate_sequence(items: Sequence[T], per_page: int) -> list[list[T]]:
    """Chia danh sách thành các trang, không mất phần tử nào."""
    if per_page <= 0:
        raise ValueError("per_page phải lớn hơn 0")
    items = list(items)
    if not items:
        return [[]]
    return [items[i : i + per_page] for i in range(0, len(items), per_page)]


def build_field_pages(
    title: str,
    rows: Iterable[tuple[str, str]],
    *,
    color: discord.Color = discord.Color.blurple(),
    per_page: int = 8,
    footer: str = "",
) -> list[discord.Embed]:
    """Dựng nhiều embed từ danh sách ``(tên, giá trị)``, tự chia khi vượt giới hạn.

    ``discord.Embed`` giới hạn 25 field/trang và 1024 ký tự mỗi value — hàm này
    tự cắt trang và rút gọn value quá dài để **không bao giờ** sinh ra embed
    mà Discord từ chối.
    """
    per_page = max(1, min(per_page, MAX_FIELDS_PER_PAGE))
    safe_rows = [(str(name)[:256], str(value)[:MAX_EMBED_FIELD_VALUE]) for name, value in rows]
    chunks = paginate_sequence(safe_rows, per_page)
    pages: list[discord.Embed] = []
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(title=title[:256], color=color)
        for name, value in chunk:
            embed.add_field(name=name, value=value or "—", inline=False)
        label = f"Trang {index}/{total}"
        embed.set_footer(text=f"{footer} • {label}"[:2048] if footer else label)
        pages.append(embed)
    return pages or [build_embed(title, "(trống)", color=color)]


def build_embed(
    title: str,
    description: str = "",
    *,
    color: discord.Color = discord.Color.blurple(),
    footer: str = "",
) -> discord.Embed:
    """Tạo embed an toàn (mô tả tự cắt vừa giới hạn Discord)."""
    embed = discord.Embed(title=title[:256], color=color)
    if description:
        embed.description = description[:MAX_EMBED_DESCRIPTION]
    if footer:
        embed.set_footer(text=footer[:2048])
    return embed


async def safe_edit(message: discord.Message, **kwargs) -> bool:
    """Sửa message, bỏ qua lỗi do interaction đã hết hạn."""
    try:
        await message.edit(**kwargs)
        return True
    except discord.HTTPException:
        return False


class PaginationView(discord.ui.View):
    """Phân trang bằng nút bấm, khoá theo người mở menu."""

    def __init__(
        self,
        pages: Sequence[discord.Embed | str],
        *,
        author_id: int | None = None,
        timeout: float = 180.0,
        start: int = 0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.pages: list[discord.Embed | str] = list(pages) or [build_embed("Không có dữ liệu")]
        self.author_id = author_id
        self.index = max(0, min(start, len(self.pages) - 1))
        self.refresh()

    @property
    def total(self) -> int:
        return len(self.pages)

    @property
    def current(self) -> discord.Embed | str:
        return self.pages[self.index]

    def _stamp(self, page: discord.Embed | str) -> discord.Embed | str:
        """Ghi số trang lên footer của embed."""
        if isinstance(page, discord.Embed):
            marker = f"Trang {self.index + 1}/{self.total}"
            base = (page.footer.text or "") if page.footer else ""
            base = base.split(" • Trang ")[0].strip()
            page.set_footer(text=f"{base} • {marker}"[:2048] if base else marker)
        return page

    def refresh(self) -> None:
        single = self.total <= 1
        self.first.disabled = single
        self.prev.disabled = single
        self.next.disabled = single
        self.last.disabled = single
        self.counter.label = f"{self.index + 1}/{self.total}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Menu này thuộc về người đã mở nó.", ephemeral=True
            )
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        self.refresh()
        page = self._stamp(self.current)
        await safe_edit(
            interaction.message,
            content=page if isinstance(page, str) else None,
            embed=page if isinstance(page, discord.Embed) else None,
            view=self,
        )

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        self.index = (self.index - 1) % self.total
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:  # pragma: no cover
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        self.index = (self.index + 1) % self.total
        await self._show(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        self.index = self.total - 1
        await self._show(interaction)

    @discord.ui.button(label="✖", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(view=self)


class _SelectMenu(discord.ui.Select):
    """Select menu động cho :class:`PaginationSelect`."""

    def __init__(self, owner: PaginationSelect) -> None:
        super().__init__(placeholder="Chọn trang…", min_values=1, max_values=1)
        self.owner = owner
        self.sync_options()

    def sync_options(self) -> None:
        self.options = [
            discord.SelectOption(label=f"Trang {i + 1}/{len(self.owner.pages)}", value=str(i))
            for i in range(len(self.owner.pages))
        ]

    async def callback(self, interaction: discord.Interaction) -> None:
        self.owner.index = int(interaction.data.get("values", ["0"])[0])
        self.sync_options()
        await interaction.response.edit_message(
            embed=self.owner.pages[self.owner.index], view=self.owner
        )


class PaginationSelect(discord.ui.View):
    """Phân trang bằng **select menu** — hợp với danh sách rất dài."""

    def __init__(
        self,
        pages: Sequence[discord.Embed],
        *,
        author_id: int | None = None,
        timeout: float = 180.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.pages = list(pages) or [build_embed("Không có dữ liệu")]
        self.author_id = author_id
        self.index = 0
        self.add_item(_SelectMenu(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Menu này không phải của bạn.", ephemeral=True)
            return False
        return True


async def send_paginated(
    target,
    pages: Sequence[discord.Embed | str],
    *,
    author_id: int | None = None,
    use_select: bool = False,
    **kwargs,
) -> discord.Message:
    """Gửi nội dung nhiều trang tới channel/interaction và gắn bộ phân trang.

    Dùng chung cho cả prefix (``ctx.send``) và slash (``interaction.response``)
    vì chỉ yêu cầu target có phương thức ``send``.
    """
    pages = list(pages) or [build_embed("Không có dữ liệu")]
    if len(pages) == 1:
        page = pages[0]
        if isinstance(page, discord.Embed):
            return await target.send(embed=page, **kwargs)
        return await target.send(page[:MAX_MESSAGE], **kwargs)

    if use_select and all(isinstance(page, discord.Embed) for page in pages):
        view: discord.ui.View = PaginationSelect(pages, author_id=author_id)
    else:
        view = PaginationView(pages, author_id=author_id)
    first = pages[0]
    if isinstance(first, discord.Embed):
        return await target.send(embed=first, view=view, **kwargs)
    return await target.send(first[:MAX_MESSAGE], view=view, **kwargs)

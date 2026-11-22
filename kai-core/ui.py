"""Reusable discord.py UI components (pagination, confirmation, chunked output)."""

from __future__ import annotations

from collections.abc import Sequence

import discord

__all__ = ("Paginator", "ConfirmView", "send_chunked")


class Paginator(discord.ui.View):
    """A reusable, owner-locked paginator for embeds or plain text pages."""

    def __init__(
        self,
        pages: Sequence[discord.Embed | str],
        *,
        author_id: int | None = None,
        timeout: float = 180.0,
        start: int = 0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.pages: list[discord.Embed | str] = list(pages) or [
            discord.Embed(description="📭 Không có dữ liệu để hiển thị.")
        ]
        self.author_id = author_id
        self.index = max(0, min(start, len(self.pages) - 1))
        self._refresh_buttons()

    # -- helpers ---------------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.pages)

    @property
    def current(self):
        return self.pages[self.index]

    def _refresh_buttons(self) -> None:
        single = self.total <= 1
        self.first_button.disabled = single
        self.prev_button.disabled = single
        self.next_button.disabled = single
        self.last_button.disabled = single
        self.counter.label = f"{self.index + 1}/{self.total}"

    def _decorate(self, page):
        if isinstance(page, discord.Embed):
            footer = page.footer.text or ""
            marker = f"Trang {self.index + 1}/{self.total}"
            page.set_footer(text=f"{marker} • {footer}" if footer else marker)
        return page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # noqa: D102
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Menu này thuộc về người đã mở nó.", ephemeral=True
            )
            return False
        return True

    async def _show(self, interaction: discord.Interaction) -> None:
        self._refresh_buttons()
        page = self._decorate(self.current)
        if isinstance(page, discord.Embed):
            await interaction.response.edit_message(embed=page, content=None, view=self)
        else:
            await interaction.response.edit_message(
                content=str(page)[:2000], embed=None, view=self
            )

    # -- buttons ---------------------------------------------------------
    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def first_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def prev_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = (self.index - 1) % self.total
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # pragma: no cover
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = (self.index + 1) % self.total
        await self._show(interaction)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def last_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = self.total - 1
        await self._show(interaction)

    @discord.ui.button(label="✖", style=discord.ButtonStyle.danger)
    async def close_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(view=self)


class ConfirmView(discord.ui.View):
    """A small yes/no prompt that records the outcome in :attr:`value`."""

    def __init__(
        self,
        *,
        author_id: int | None = None,
        timeout: float = 30.0,
        confirm_label: str = "✅ Xác nhận",
        cancel_label: str = "✖ Huỷ",
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: bool | None = None
        self.confirm_button.label = confirm_label
        self.cancel_button.label = cancel_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # noqa: D102
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Bạn không phải người ra lệnh.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="✅ Xác nhận", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="✖ Huỷ", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


async def send_chunked(destination, text: str, *, limit: int = 1900, **kwargs) -> list[discord.Message]:
    """Send ``text`` to a channel/message target, splitting it when needed."""
    from .utils import chunk_text

    sent: list[discord.Message] = []
    for piece in chunk_text(text, limit=limit) or ["(trống)"]:
        sent.append(await destination.send(piece, **kwargs))
    return sent


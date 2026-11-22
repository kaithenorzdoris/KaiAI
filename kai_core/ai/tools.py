"""
Cong cu AI doc va thao tac du lieu toan he thong.

Muc dich
--------
Cho phep AI tra loi cau hoi ve **bat ky ai** trong server (vi, tiet kiem, co
phieu, no vay, level, streak...) va **thuc hien giao dich** khi nguoi dung
yeu cau ro rang.

Thiet ke
--------
* **Read-only** -- ``look_up_user``, ``server_stats``, ``leaderboard``,
  ``market_prices``, ``interest_rates``. Chi doc, khong bao gio doi du lieu.
* **Giao dich** -- ``do_transfer``, ``do_deposit``, ``do_withdraw``,
  ``do_market_buy``, ``do_market_sell``. Moi thao tac deu **can xac nhan**:
  goi khong co ``confirm=True`` chi tra ve yeu cau xac nhan, khong chay.
* **Ket qua co cau truc** -- :class:`ToolResult` de AI trich dan dung so lieu
  thay vi doan.
* **An toan** -- moi thao tac goi lai ham giao dich chinh thuc cu ``Games``,
  nen giu nguyen atomic + rollback cua bot.

Vi du
-----
::

    tools = AITools(games_cog)
    r = await tools.do_transfer(1, 2, 50_000)
    if r.need_confirm:
        # nguoi dung xac nhan -> goi lai
        r = await tools.do_transfer(1, 2, 50_000, confirm=True)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger

log = get_logger("ai.tools")


@dataclass(slots=True)
class ToolResult:
    """Ket qua cua mot cong cu AI.

    ``ok=False`` kem ``need_confirm=True`` nghia la AI da hieu yeu cau nhung
    **can nguoi dung xac nhan** truoc khi thao tac tien that.
    """

    ok: bool
    message: str = ""
    need_confirm: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - tiện lợi khi log
        return self.message


def fmt_money(amount: Any) -> str:
    """Định dạng tiền VNĐ có dấu chấm phân tách nghìn, ví dụ ``1.250.000``."""
    try:
        return f"{int(amount):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def format_user_report(summary: dict) -> str:
    """Bao cao tai chinh cua mot nguoi, dinh dang lai chu VN de AI doc de.

    Khong dung ``Games.format_financial_context`` vi ham do in dau phay theo
    chuan tieng Anh; cong cu AI can dau cham cho nhat quan voi phan con lai
    cua he thong.
    """
    lines = [
        f"- Vi (tien mat): {fmt_money(summary.get('balance', 0))} VND",
        f"- Ngan hang (tiet kiem): {fmt_money(summary.get('bank', 0))} VND",
        f"- Co phieu cong ty: {fmt_money(summary.get('stock_value', 0))} VND",
        f"- Co phieu san: {fmt_money(summary.get('market_value', 0))} VND",
        f"- No vay: {fmt_money(summary.get('debt', 0))} VND",
    ]
    if summary.get("collateral_debt"):
        lines.append(
            f"- No the chap: {fmt_money(summary['collateral_debt'])} VND "
            f"(giu {fmt_money(summary.get('collateral_shares', 0))} CP "
            f"{summary.get('collateral_symbol', '')})"
        )
    lines.append(f"- TONG TAI SAN RONG: {fmt_money(summary.get('net_worth', 0))} VND")
    lines.append(f"- Level: {summary.get('level', 1)}")
    return chr(10).join(lines)


class AITools:
    """Bộ công cụ để AI đọc và điều khiển hệ thống kinh tế của bot.

    ``games`` là instance của cog :class:`~cogs.games.Games`. Mọi hàm đều bọc
    ``try/except`` nên lỗi ở cog nào cũng không làm sập luồng chat AI.
    """

    #: Ngưỡng số dòng bảng xếp hạng mặc định.
    DEFAULT_TOP = 10

    def __init__(self, games: Any) -> None:
        self.games = games

    # ------------------------------------------------------------------
    # Kiểm tra sẵn sàng
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        """Cog ``Games`` đã load xong và có đủ API cần dùng chưa."""
        return bool(self.games) and hasattr(self.games, "get_financial_summary")

    # ------------------------------------------------------------------
    # Đọc dữ liệu (read-only) -- không bao giờ thay đổi dữ liệu
    # ------------------------------------------------------------------

    async def look_up_user(self, user_id: int) -> ToolResult:
        """Trả về toàn bộ thông tin tài chính của **một** người dùng theo id.

        Đây là hàm AI dùng để trả lời câu hỏi kiểu "tôi có bao nhiêu tiền".
        """
        if not self.is_ready():
            return ToolResult(ok=False, message="Hệ thống kinh tế chưa sẵn sàng.")
        try:
            uid = int(user_id)
            summary = await self.games.get_financial_summary(uid)
            profile = await self.games.get_user_data(uid)
        except Exception as exc:  # noqa: BLE001 - lỗi DB không được làm hỏng chat
            log.debug("look_up_user thất bại: %s", exc)
            return ToolResult(ok=False, message=f"Không đọc được dữ liệu: {exc}")

        return ToolResult(
            ok=True,
            message=format_user_report(summary),
            data={"summary": summary, "profile": profile, "user_id": uid},
        )

    async def server_stats(self) -> ToolResult:
        """Thống kê tổng quan của server: số người chơi, tổng tiền, tổng nợ."""
        if not self.is_ready():
            return ToolResult(ok=False, message="Hệ thống kinh tế chưa sẵn sàng.")
        try:
            rows = await self._fetch_all(
                "SELECT COUNT(*) AS players,"
                " COALESCE(SUM(balance),0) AS wallet,"
                " COALESCE(SUM(bank),0) AS savings"
                " FROM users"
            )
            debt_row = await self._fetch_all("SELECT COALESCE(SUM(amount),0) FROM loans")
            stats = rows[0] if rows else {}
            total_debt = int(next(iter(debt_row[0].values()))) if debt_row else 0
        except Exception as exc:  # noqa: BLE001
            log.debug("server_stats thất bại: %s", exc)
            return ToolResult(ok=False, message=f"Không đọc được thống kê: {exc}")

        return ToolResult(
            ok=True,
            message=(
                f"- Số người chơi: {int(stats.get('players', 0)):,}\n"
                f"- Tổng tiền trong ví: {fmt_money(stats.get('wallet', 0))} VNĐ\n"
                f"- Tổng tiền tiết kiệm: {fmt_money(stats.get('savings', 0))} VNĐ\n"
                f"- Tổng nợ vay: {fmt_money(total_debt)} VNĐ"
            ),
            data={"players": int(stats.get("players", 0)), "debt": total_debt},
        )

    async def bot_identity(self) -> ToolResult:
        """Ban la ai, dang o dau (so server THAT tu Discord)."""
        try:
            bot = getattr(self.games, "bot", None)
            guilds = list(getattr(bot, "guilds", []) or [])
            names = [str(getattr(g, "name", "?")) for g in guilds]
            total_members = sum(int(getattr(g, "member_count", 0) or 0) for g in guilds)
            latency_ms = round(float(getattr(bot, "latency", 0) or 0) * 1000)
            shown = ", ".join(names[:12]) if names else "?"
            more = f" (+{len(names) - 12} server nua)" if len(names) > 12 else ""
            commands = sorted({c.name for c in getattr(bot, "commands", []) or []})
        except Exception as exc:  # noqa: BLE001
            log.debug("bot_identity that bai: %s", exc)
            return ToolResult(ok=False, message=f"Khong doc duoc thong tin bot: {exc}")
        return ToolResult(
            ok=True,
            message=(
                f"Ban la KaiAI — tro ly AI tren Discord, tao boi KaithenorzDoris. "
                f"Dang online tai {len(guilds)} server ({shown}{more}), "
                f"phuc vu khoang {total_members} nguoi, ping {latency_ms}ms. "
                f"Ban co {len(commands)} lenh prefix (!). "
                f"Khi ai hoi 'may o may server', hay tra loi DUNG con so {len(guilds)} nay."
            ),
            data={
                "guild_count": len(guilds),
                "guild_names": names,
                "total_members": total_members,
                "latency_ms": latency_ms,
                "command_count": len(commands),
            },
        )

        """Tra ve toan bo thong tin tai chinh."""

    async def leaderboard(self, limit: int = DEFAULT_TOP) -> ToolResult:
        """Bảng xếp hạng người giữ nhiều tiền nhất (ví + tiết kiệm)."""
        if not self.is_ready():
            return ToolResult(ok=False, message="Hệ thống kinh tế chưa sẵn sàng.")
        try:
            rows = await self._fetch_all(
                "SELECT user_id, balance + bank AS total, balance, bank, level"
                " FROM users ORDER BY total DESC LIMIT ?",
                (max(1, min(int(limit), 50)),),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("leaderboard thất bại: %s", exc)
            return ToolResult(ok=False, message=f"Không đọc được bảng xếp hạng: {exc}")

        if not rows:
            return ToolResult(ok=True, message="Chưa có ai chơi để lên bảng xếp hạng.")

        lines = ["Hạng | Thành viên | Tổng tài sản | Level"]
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"{index}. <@{row['user_id']}> — "
                f"{fmt_money(row['total'])} VNĐ (level {row['level']})"
            )
        return ToolResult(ok=True, message="\n".join(lines), data={"rows": rows})

    async def market_prices(self) -> ToolResult:
        """Bảng giá cổ phiếu sàn hiện tại."""
        if not self.is_ready():
            return ToolResult(ok=False, message="Hệ thống kinh tế chưa sẵn sàng.")
        try:
            rows = await self._fetch_all(
                "SELECT symbol, name, price, change FROM market ORDER BY symbol"
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("market_prices thất bại: %s", exc)
            return ToolResult(ok=False, message=f"Không đọc được bảng giá: {exc}")

        if not rows:
            return ToolResult(ok=True, message="Sàn chứng khoán hiện chưa có mã nào.")

        lines = [
            f"- {row['symbol']} ({row['name']}): "
            f"{fmt_money(row['price'])} VNĐ [{row['change']:+.2f}%]"
            for row in rows
        ]
        return ToolResult(ok=True, message="\n".join(lines), data={"rows": rows})

    async def interest_rates(self) -> ToolResult:
        """Biểu lãi suất công bố hôm nay (tiết kiệm + hai loại vay)."""
        if not self.is_ready():
            return ToolResult(ok=False, message="Hệ thống kinh tế chưa sẵn sàng.")
        try:
            text = self.games.format_rates_context()
        except Exception as exc:  # noqa: BLE001
            log.debug("interest_rates thất bại: %s", exc)
            return ToolResult(ok=False, message=f"Không đọc được lãi suất: {exc}")
        return ToolResult(ok=True, message=f"Biểu lãi suất công bố hôm nay:\n{text}")

    # ------------------------------------------------------------------
    # Giao dịch — bắt buộc xác nhận trước khi thao tác tiền thật
    # ------------------------------------------------------------------

    async def do_transfer(
        self, sender: int, receiver: int, amount: int, *, confirm: bool = False
    ) -> ToolResult:
        """Chuyển tiền từ ví người gửi sang người nhận."""
        try:
            src, dst, value = int(sender), int(receiver), int(amount)
        except (TypeError, ValueError):
            return ToolResult(ok=False, message="Thông tin giao dịch không hợp lệ.")

        if src == dst:
            return ToolResult(ok=False, message="Không thể chuyển tiền cho chính mình.")
        if value <= 0:
            return ToolResult(ok=False, message="Số tiền phải lớn hơn 0.")

        if not confirm:
            return ToolResult(
                ok=False,
                need_confirm=True,
                message=(
                    f"Sẽ chuyển {fmt_money(value)} VNĐ từ <@{src}> sang <@{dst}>. "
                    "Người dùng xác nhận để thực hiện."
                ),
                data={"sender": src, "receiver": dst, "amount": value},
            )

        if not self.is_ready() or not hasattr(self.games, "transfer"):
            return ToolResult(ok=False, message="Hệ thống kinh tế chưa sẵn sàng.")
        try:
            ok = await self.games.transfer(src, dst, value)
        except Exception as exc:  # noqa: BLE001
            log.warning("AI chuyển tiền lỗi: %s", exc)
            return ToolResult(ok=False, message=f"Giao dịch thất bại: {exc}")

        if not ok:
            return ToolResult(
                ok=False,
                message="Không chuyển được: số dư không đủ hoặc người nhận không hợp lệ.",
            )
        return ToolResult(
            ok=True,
            message=f"Đã chuyển {fmt_money(value)} VNĐ từ <@{src}> sang <@{dst}>.",
            data={"sender": src, "receiver": dst, "amount": value},
        )

    async def do_deposit(self, user_id: int, amount: int, *, confirm: bool = False) -> ToolResult:
        """Gửi tiền từ ví vào ngân hàng (bắt đầu hưởng lãi kép)."""
        try:
            uid, value = int(user_id), int(amount)
        except (TypeError, ValueError):
            return ToolResult(ok=False, message="Thông tin không hợp lệ.")
        if value <= 0:
            return ToolResult(ok=False, message="Số tiền phải lớn hơn 0.")

        if not confirm:
            return ToolResult(
                ok=False,
                need_confirm=True,
                message=(
                    f"Sẽ gửi {fmt_money(value)} VNĐ của <@{uid}> vào ngân hàng. "
                    "Người dùng xác nhận để thực hiện."
                ),
                data={"user_id": uid, "amount": value},
            )
        return await self._run_bank_move(uid, value, deposit=True)

    async def do_withdraw(self, user_id: int, amount: int, *, confirm: bool = False) -> ToolResult:
        """Rút tiền từ ngân hàng về ví."""
        try:
            uid, value = int(user_id), int(amount)
        except (TypeError, ValueError):
            return ToolResult(ok=False, message="Thông tin không hợp lệ.")
        if value <= 0:
            return ToolResult(ok=False, message="Số tiền phải lớn hơn 0.")

        if not confirm:
            return ToolResult(
                ok=False,
                need_confirm=True,
                message=(
                    f"Sẽ rút {fmt_money(value)} VNĐ của <@{uid}> từ ngân hàng về ví. "
                    "Người dùng xác nhận để thực hiện."
                ),
                data={"user_id": uid, "amount": value},
            )
        return await self._run_bank_move(uid, value, deposit=False)

    async def _run_bank_move(self, user_id: int, amount: int, *, deposit: bool) -> ToolResult:
        """Gọi lệnh gửi/rút thật của ``Games`` rồi trả về kết quả cho AI."""
        name = "deposit" if deposit else "withdraw"
        handler = getattr(self.games, name, None)
        if handler is None:
            return ToolResult(ok=False, message=f"Lệnh {name} chưa sẵn sàng.")
        # ``games.deposit`` là ``commands.Command``; nếu chưa được Discord nap
        # thì callback chưa gắn instance -> ``self`` sẽ bị bind nhầm vào ctx.
        if getattr(handler, "cog", None) is None:
            try:
                handler.cog = self.games
            except AttributeError:
                pass
        try:
            fake_ctx = _SilentCtx(int(user_id))
            await handler(fake_ctx, str(int(amount)))
        except Exception as exc:  # noqa: BLE001
            log.warning("AI %s lỗi: %s", name, exc)
            return ToolResult(ok=False, message=f"Thao tác thất bại: {exc}")

        if fake_ctx.sent and fake_ctx.sent[-1].startswith("❌"):
            return ToolResult(ok=False, message=fake_ctx.sent[-1])
        verb = "gửi vào ngân hàng" if deposit else "rút về ví"
        return ToolResult(
            ok=True,
            message=f"Đã {verb} {fmt_money(amount)} VNĐ cho <@{user_id}>.",
            data={"user_id": user_id, "amount": amount},
        )

    # ------------------------------------------------------------------
    # Nội bộ
    # ------------------------------------------------------------------

    async def _fetch_all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        """Chạy một câu ``SELECT`` và trả về list dict (dùng chung cho các tool)."""
        import aiosqlite

        async with aiosqlite.connect(self.games.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, args) as cur:
                # Lay ten cot tu ``description`` chu khong phai row.keys() --
                # mot so ban aiosqlite tra ve iterator da bi tieu thu.
                columns = [d[0] for d in cur.description or ()]
                rows = await cur.fetchall()
        return [dict(zip(columns, row)) for row in rows]


class _SilentCtx:
    """Context giả cho lệnh Games — chỉ dùng ``author.id`` và ``send``.

    Cho phép AI gọi lại đúng logic ``!dep``/``!with`` của bot thay vì viết lại.
    """

    __slots__ = ("author", "sent")

    def __init__(self, user_id: int) -> None:
        self.author = _SilentMember(user_id)
        self.sent: list[str] = []

    async def send(self, content: str | None = None, **_kwargs: object) -> None:
        self.sent.append(str(content or ""))


class _SilentMember:
    """Member tối giản cho :class:`_SilentCtx`."""

    __slots__ = ("id", "mention")

    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.mention = f"<@{user_id}>"


__all__ = ("AITools", "ToolResult", "fmt_money", "format_user_report")
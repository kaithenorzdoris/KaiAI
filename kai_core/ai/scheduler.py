"""
Chạy lộ trình AI **tự động theo lịch**.

Vì sao cần phần này
-------------------
:mod:`kai_core.ai.plan` chạy chuỗi bước ngay khi người dùng bấm
``!chaykehoach``. Nhưng "mỗi tháng gửi 2 triệu vào ngân hàng" là việc phải
làm **đều đặn**, không phải mỗi lần ngồi gõ lệnh.

Module này lưu lịch vào **SQLite** để sống sót qua restart (bot trên
Render hay bị restart khi hết bộ nhớ), và có vòng lặp nền tự chạy các lịch
đã đến giờ.

Nguyên tắc an toàn
------------------
* **Chỉ lịch đã được người dùng duyệt mới chạy** — ``!datlich`` là hành
  động duyệt. Sau đó bot tự chạy, không hỏi lại từng lần.
* **Không bù nợ** — bot hợp xuôi nhiều lần thì chỉ chạy **một** lần cho
  kỳ vừa qua rồi bỏ qua các kỳ cũ (chống tràn tiền khi bot offline).
* **Claim nguyên tử** — ``claim_due`` đánh dấu trong *cùng transaction*,
  nên hai vòng lặp chạy song song cũng không chạy trùng.
* **Giới hạn số lịch** mỗi người, và tự tắt lịch liên tiếp thất bại.
* **Tắt được** bằng biến môi trường ``AI_SCHEDULER`` (mặc định bật).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..logging_setup import get_logger
from .plan import step_owner_error

log = get_logger("ai.scheduler")

#: Các kiểu lịch được phép.
KINDS = ("daily", "weekly", "monthly")

#: Số lịch tối đa mỗi người.
MAX_SCHEDULES_PER_USER = 3
#: Số lần thất bại liên tiếp thì tự tắt lịch (tránh spam lỗi).
MAX_CONSECUTIVE_FAILURES = 5

#: Múi giờ người dùng — **Việt Nam (UTC+7)**. Lịch hiển thị và chạy theo
#: giờ VN kể cả khi máy chủ (Render/Docker) đang đặt UTC. VN không có
#: DST nên dùng offset cố định, không cần gói ``tzdata``.
TZ_VN = dt.timezone(dt.timedelta(hours=7), "+07:00")

#: Cột chọn khi đọc một lịch (dùng chung cho mọi truy vấn).
_COLUMNS = (
    "id,user_id,kind,hour,minute,title,steps,next_run_at,"
    "enabled,failures,last_run_at,anchor_day"
)


def wallclock(now: dt.datetime | None = None) -> dt.datetime:
    """Đưa ``now`` về khung giờ Việt Nam.

    * ``None`` — thời điểm hiện tại theo giờ VN (luôn dùng khi chạy thật).
    * naive (không kèm tz) — coi là giờ VN (giữ tương thích lịch cũ/test).
    * aware — quy đổi về giờ VN.
    """
    if now is None:
        return dt.datetime.now(TZ_VN)
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ_VN)
    return now.astimezone(TZ_VN)


def validate_schedule(
    kind: str, hour: Any, minute: Any, steps: Any,
) -> str | None:
    """Validator **duy nhất** cho lịch tự động.

    Dùng cả khi ``ScheduleStore.create`` lưu lịch lẫn khi ``claim_due`` /
    ``run_schedule_now`` sắp thực thi (revalidate — phòng khi dữ liệu cũ
    hỏng hoặc bị sửa ngoài luồng).

    Trả về thông báo lỗi tiếng Việt, ``None`` nếu hợp lệ.
    """
    if kind not in KINDS:
        return f"Loại lịch lạ: {kind}."
    if not isinstance(steps, list) or not steps:
        return "Lịch phải có ít nhất 1 bước."
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            return f"Bước {index} không hợp lệ."
        if not str(step.get("action") or "").strip():
            return f"Bước {index} thiếu hành động."
        args = step.get("args")
        if args is not None and not isinstance(args, dict):
            return f"Bước {index} có tham số không hợp lệ."
    try:
        hour_i, minute_i = int(hour), int(minute)
    except (TypeError, ValueError):
        return "Giờ không hợp lệ (0–23h, 0–59p)."
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        return "Giờ không hợp lệ (0–23h, 0–59p)."
    return None


def scheduler_enabled() -> bool:
    """Lịch có được phép chạy không (tắt qua biến môi trường ``AI_SCHEDULER``)."""
    raw = os.environ.get("AI_SCHEDULER", "1").strip().lower()
    return raw not in {"0", "false", "no", "off", "disable", "disabled"}


@dataclass(slots=True)
class ScheduledRun:
    """Một lịch đã lưu."""

    id: int
    user_id: int
    kind: str
    hour: int
    minute: int
    title: str
    steps: list[dict[str, Any]]
    next_run_at: float
    enabled: int = 1
    failures: int = 0
    last_run_at: float = 0.0
    #: Ngày trong tháng làm mốc cho lịch ``monthly`` (vd 31) — để các kỳ
    #: sau vẫn quay về đúng ngày đó sau khi đã lùi về ngày cuối tháng.
    anchor_day: int = 0

    @property
    def total(self) -> int:
        """Tổng tiền một lần chạy."""
        return sum(
            int((s.get("args") or {}).get("amount") or 0) for s in self.steps
        )

    def when_text(self) -> str:
        """Mô tả lịch cho người dùng đọc."""
        clock = f"{self.hour:02d}:{self.minute:02d}"
        label = {"daily": "hằng ngày", "weekly": "hằng tuần",
                 "monthly": "hằng tháng"}.get(self.kind, self.kind)
        return f"{label} lúc {clock}"

def next_occurrence(
    kind: str,
    hour: int,
    minute: int,
    *,
    after: float | None = None,
    now: dt.datetime | None = None,
    anchor_day: int | None = None,
) -> float:
    """Tính mốc chạy kế tiếp sau ``after`` (mặc định: hiện tại, **giờ VN**).

    Trả về timestamp. Nếu mốc đó **đã qua** (bot offline), trả về thời điểm
    ngay trước đó — nghĩa là chỉ bù **một** kỳ, không dồn dập.

    ``anchor_day`` giữ **ngày trong tháng** của lịch tháng (vd 31) để các
    kỳ sau vẫn về đúng ngày đó; tháng ngắn hơn thì tự lùi về ngày cuối.
    """
    ref = wallclock(now)
    base = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if base <= ref:
        base = _advance(base, kind, anchor_day=anchor_day)
    if after is not None and after >= base.timestamp():
        # Đã trễ: bù đúng một kỳ rồi đặt mốc kế tiếp từ hiện tại.
        return base.timestamp()
    return base.timestamp()


def _advance(
    moment: dt.datetime, kind: str, *, anchor_day: int | None = None,
) -> dt.datetime:
    """Cộng một kỳ theo loại lịch."""
    if kind == "daily":
        return moment + dt.timedelta(days=1)
    if kind == "weekly":
        return moment + dt.timedelta(days=7)
    if kind == "monthly":
        # Ngày 31 không tồn tại ở tháng 2 -> lùi về ngày cuối tháng đó,
        # nhưng **anchor giữ nguyên**: tháng sau lại về ngày 31.
        year = moment.year + (1 if moment.month == 12 else 0)
        month = 1 if moment.month == 12 else moment.month + 1
        last = _last_day_of_month(year, month)
        day = int(anchor_day or moment.day)
        day = min(max(1, day), last)
        return moment.replace(year=year, month=month, day=day)
    raise ValueError(f"loại lịch lạ: {kind}")


def _last_day_of_month(year: int, month: int) -> int:
    """Số ngày của tháng (28/29/30/31)."""
    if month == 12:
        return 31
    return (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    kind         TEXT    NOT NULL,
    hour         INTEGER NOT NULL DEFAULT 9,
    minute       INTEGER NOT NULL DEFAULT 0,
    title        TEXT    NOT NULL DEFAULT '',
    steps        TEXT    NOT NULL,
    next_run_at  REAL    NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    failures     INTEGER NOT NULL DEFAULT 0,
    last_run_at  REAL    NOT NULL DEFAULT 0,
    created_at   REAL    NOT NULL DEFAULT 0,
    anchor_day   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ai_schedules_due ON ai_schedules(enabled, next_run_at);
"""

#: Migration cho kho dữ liệu đã tạo trước khi có ``anchor_day``.
_MIGRATIONS = (
    "ALTER TABLE ai_schedules ADD COLUMN anchor_day INTEGER NOT NULL DEFAULT 0",
)


class ScheduleStore:
    """Kho lịch trên SQLite — sống sót qua restart."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.last_error = ""

    async def setup(self) -> None:
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            # Cột mới cho kho cũ (bảng mới thì đã có sẵn -> bỏ qua lỗi).
            async with db.execute(
                "PRAGMA table_info(ai_schedules)"
            ) as cur:
                cols = {row[1] for row in await cur.fetchall()}
            for stmt in _MIGRATIONS:
                if "anchor_day" in stmt and "anchor_day" in cols:
                    continue
                try:
                    await db.execute(stmt)
                except Exception:  # noqa: BLE001 - đã áp dụng rồi
                    await db.rollback()
            await db.commit()

    @staticmethod
    def _row(row: tuple) -> ScheduledRun:
        import json
        try:
            steps = json.loads(row[6])
        except (TypeError, ValueError):
            steps = []
        if not isinstance(steps, list):
            steps = []
        try:
            anchor_day = int(row[11])
        except (IndexError, TypeError, ValueError):
            anchor_day = 0
        return ScheduledRun(
            id=int(row[0]), user_id=int(row[1]), kind=str(row[2]),
            hour=int(row[3]), minute=int(row[4]), title=str(row[5]),
            steps=steps, next_run_at=float(row[7]), enabled=int(row[8]),
            failures=int(row[9]), last_run_at=float(row[10]),
            anchor_day=anchor_day,
        )

    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        user_id: int,
        kind: str,
        steps: list[dict[str, Any]],
        title: str = "",
        hour: int = 9,
        minute: int = 0,
    ) -> ScheduledRun | None:
        """Tạo lịch mới. ``None`` nếu không hợp lệ hoặc vượt giới hạn."""
        import json

        import aiosqlite

        self.last_error = ""
        # Validator chung — cùng một bộ luật với lúc chạy (revalidate).
        error = validate_schedule(kind, hour, minute, steps)
        if error is not None:
            self.last_error = error
            return None
        hour, minute = int(hour), int(minute)

        await self.setup()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT COUNT(*) FROM ai_schedules WHERE user_id=? AND enabled=1",
                    (int(user_id),),
                ) as cur:
                    count = int((await cur.fetchone())[0])
                if count >= MAX_SCHEDULES_PER_USER:
                    await db.rollback()
                    self.last_error = (
                        f"Bạn đã có {count} lịch (tối đa "
                        f"{MAX_SCHEDULES_PER_USER})."
                    )
                    return None
                nxt = next_occurrence(kind, hour, minute)
                # Mốc ngày trong tháng lấy theo **hôm nay** (giờ VN): nếu
                # hôm nay là 31 mà tháng sau không đủ ngày thì các kỳ sau
                # vẫn quay lại ngày 31 thay vì kẹt ở ngày 28/30.
                anchor = wallclock().day if kind == "monthly" else 0
                await db.execute(
                    "INSERT INTO ai_schedules(user_id,kind,hour,minute,title,"
                    "steps,next_run_at,created_at,anchor_day)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (int(user_id), kind, hour, minute, str(title)[:120],
                     json.dumps(steps), nxt, time.time(), anchor),
                )
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                self.last_error = f"Không tạo được lịch: {exc}"
                return None

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"SELECT {_COLUMNS} FROM ai_schedules"
                " WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (int(user_id),),
            ) as cur:
                row = await cur.fetchone()
        log.info("Tao lich #%s cho user=%s (%s)", row[0], user_id, kind)
        return self._row(row)

    async def claim_due(self, *, limit: int = 5) -> list[ScheduledRun]:
        """Lấy các lịch đã đến giờ và **đánh dấu đã nhận** ngay.

        Việc đánh dấu nằm trong cùng transaction với ``SELECT`` nên nếu có
        hai vòng lặp chạy song song thì mỗi lịch chỉ được một vòng lặp
        nhận — không chạy trùng, không trừ tiền hai lần.
        """
        import aiosqlite

        await self.setup()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    f"SELECT {_COLUMNS} FROM ai_schedules"
                    " WHERE enabled=1 AND next_run_at<=?"
                    " ORDER BY next_run_at LIMIT ?",
                    (time.time(), int(limit)),
                ) as cur:
                    rows = await cur.fetchall()
                claimed: list[ScheduledRun] = []
                for row in rows:
                    run = self._row(row)
                    # Revalidate trước khi thi hành: dữ liệu cũ có thể hỏng
                    # hoặc bị sửa ngoài luồng -> tự tắt, không bao giờ chạy.
                    error = validate_schedule(
                        run.kind, run.hour, run.minute, run.steps)
                    if error is not None:
                        await db.execute(
                            "UPDATE ai_schedules SET enabled=0 WHERE id=?",
                            (run.id,),
                        )
                        log.warning("Tu tat lich #%s loi: %s", run.id, error)
                        continue
                    # Đẩy mốc sang kỳ kế tiếp ngay khi nhận: một lần trễ
                    # chỉ bù đúng một lần (anchor giữ ngày/thứ ban đầu).
                    await db.execute(
                        "UPDATE ai_schedules SET next_run_at=?"
                        " WHERE id=?",
                        (next_occurrence(
                            run.kind, run.hour, run.minute,
                            after=run.next_run_at,
                            anchor_day=run.anchor_day or None), run.id),
                    )
                    claimed.append(run)
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                log.warning("claim_due that bai: %s", exc)
                return []
        return claimed

    async def mark_result(self, schedule_id: int, *, ok: bool) -> None:
        """Ghi kết quả; lỗi liên tiếp quá ngưỡng thì tự tắt lịch."""
        import aiosqlite

        await self.setup()
        async with aiosqlite.connect(self.db_path) as db:
            if ok:
                await db.execute(
                    "UPDATE ai_schedules SET failures=0, last_run_at=?"
                    " WHERE id=?", (time.time(), int(schedule_id)))
            else:
                await db.execute(
                    "UPDATE ai_schedules SET failures=failures+1, last_run_at=?,"
                    " enabled=CASE WHEN failures+1>=? THEN 0 ELSE enabled END"
                    " WHERE id=?",
                    (time.time(), MAX_CONSECUTIVE_FAILURES, int(schedule_id)),
                )
            await db.commit()

    async def cancel(self, *, user_id: int, schedule_id: int) -> bool:
        """Huỷ lịch của người dùng. ``True`` nếu thực sự huỷ."""
        import aiosqlite

        await self.setup()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "DELETE FROM ai_schedules WHERE id=? AND user_id=?",
                    (int(schedule_id), int(user_id)))
                await db.commit()
                return cur.rowcount == 1
            except Exception:  # noqa: BLE001
                await db.rollback()
                return False

    async def list_for(self, user_id: int) -> list[ScheduledRun]:
        """Danh sách lịch còn bật của một người."""
        import aiosqlite

        await self.setup()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"SELECT {_COLUMNS} FROM ai_schedules"
                " WHERE user_id=? AND enabled=1 ORDER BY id", (int(user_id),)
            ) as cur:
                rows = await cur.fetchall()
        return [self._row(r) for r in rows]

    async def get(
        self, *, user_id: int, schedule_id: int,
    ) -> ScheduledRun | None:
        """Lấy đúng một lịch **của người dùng** (chỉ chủ sở hữu thấy được).

        Trả ``None`` khi sai mã, sai chủ sở hữu hoặc lịch đã bị tắt.
        """
        import aiosqlite

        await self.setup()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                f"SELECT {_COLUMNS} FROM ai_schedules"
                " WHERE id=? AND user_id=? AND enabled=1",
                (int(schedule_id), int(user_id)),
            ) as cur:
                row = await cur.fetchone()
        return self._row(row) if row else None


async def _execute_run(
    run: ScheduledRun, runner: Callable[..., Any],
) -> tuple[bool, str]:
    """Thực thi tuần tự các bước của một lịch.

    Trả về ``(tat_ca_ok, báo_cáo)``. Không bao giờ ném lỗi ra ngoài —
    một bước hỏng chỉ làm dừng chuỗi, không làm sập vòng lặp.
    """
    lines: list[str] = []
    ok_all = True
    for index, step in enumerate(run.steps, 1):
        action = str(step.get("action") or "")
        args = dict(step.get("args") or {})
        # SỞ HỮU (lớp 3/3): bước phải thuộc về chủ lịch — dữ liệu cũ bị
        # sửa ngoài luồng cũng không thể chạm vào ví của người khác.
        owner_error = step_owner_error(action, args, run.user_id)
        if owner_error is not None:
            ok_all = False
            lines.append(f"{index}. ❌ `{action}` — {owner_error}")
            log.warning(
                "Lich #%s buoc %d vi pham so huu: %s",
                run.id, index, owner_error,
            )
            break
        try:
            outcome = runner(action, **args)
            if hasattr(outcome, "__await__"):
                outcome = await outcome
        except Exception as exc:  # noqa: BLE001 - 1 bước lỗi không
            ok_all = False          # được làm hỏng cả lịch.
            lines.append(f"{index}. ❌ `{action}` — {exc}")
            log.warning("Lich #%s buoc %d loi: %s", run.id, index, exc)
            break
        failed = getattr(outcome, "ok", True) is False
        text = str(getattr(outcome, "message", "") or outcome)
        lines.append(f"{index}. {'❌' if failed else '✅'} `{action}` — {text}")
        if failed:
            ok_all = False
            break
    report = (
        f"{'✅' if ok_all else '⚠️'} **Lịch tự động: {run.title}**\n"
        + "\n".join(lines)
    )
    return ok_all, report


async def run_schedule_now(
    store: ScheduleStore,
    runner: Callable[..., Any],
    *,
    user_id: int,
    schedule_id: int,
) -> tuple[bool, str] | None:
    """Chạy **ngay một lịch cụ thể** mà không thay đổi lịch đó.

    Khác ``run_due_once``: không ``claim`` (``next_run_at`` giữ nguyên)
    và không ``mark_result`` — lịch chạy tay **không tính là một kỳ**,
    không làm vênh mốc tự động và không cộng dồn bộ đếm lỗi.

    Trả ``None`` nếu không tìm thấy lịch (sai mã, sai chủ sở hữu, lịch
    đã tắt) hoặc lịch không vượt qua validator.
    """
    run = await store.get(user_id=user_id, schedule_id=schedule_id)
    if run is None:
        return None
    error = validate_schedule(run.kind, run.hour, run.minute, run.steps)
    if error is not None:
        store.last_error = error
        log.warning("Chay lich #%s loi validator: %s", run.id, error)
        return None
    return await _execute_run(run, runner)


async def run_due_once(
    store: ScheduleStore,
    runner: Callable[..., Any],
    *,
    notifier: Callable[[int, bool, str], Any] | None = None,
) -> list[tuple[int, bool, str]]:
    """Chạy một vòng: nhận lịch đến hạn, thực thi, ghi kết quả.

    ``runner(action, **kwargs)`` là hàm giao dịch thật (trả về object có
    ``ok``). Trả về danh sách ``(schedule_id, ok, thông_bao)`` để cog báo
    cho người dùng. Không bao giờ ném lỗi ra ngoài.
    """
    results: list[tuple[int, bool, str]] = []
    for run in await store.claim_due():
        ok_all, report = await _execute_run(run, runner)
        await store.mark_result(run.id, ok=ok_all)
        results.append((run.id, ok_all, report))
        if notifier is not None:
            try:
                maybe = notifier(run.user_id, ok_all, report)
                if hasattr(maybe, "__await__"):
                    await maybe
            except Exception:  # noqa: BLE001 - báo cáo lỗi không được
                log.warning("Bao cao lich #%s that bai", run.id)
    return results


async def scheduler_loop(
    store: ScheduleStore,
    runner: Callable[..., Any],
    *,
    interval: float = 60.0,
    notifier: Callable[[int, bool, str], Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Vòng lặp nền: kiểm tra lịch mỗi ``interval`` giây.

    ``should_stop`` cho phép dừng vòng lặp khi cog được gỡ (test, tắt bot).
    """
    while True:
        if should_stop is not None and should_stop():
            log.info("Vong lap lich da dung.")
            return
        try:
            await run_due_once(store, runner, notifier=notifier)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - lỗi lặp không được
            log.warning("Vong lap lich gap loi: %s", exc)
        await asyncio.sleep(max(5.0, interval))

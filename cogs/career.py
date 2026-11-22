# -*- coding: utf-8 -*-
"""
Giáo dục & sự nghiệp cho KaiAI — học để có việc, việc để có lương
===============================================================

Vòng lặp kinh tế
----------------
::

    !trongtruong -> !nhaphoc -> (chờ đủ ngày) -> !tophoc
        -> !vieclam -> !ungtuyen -> !dilam (hằng ngày) -> !thangchuc
        -> dùng lương để !muadi trong cogs/realworld.py

Toán học nằm ở :mod:`kai_core.career` (thuần, dễ test); module này chỉ lo
phần Discord + ghi DB.

Bảo mật
--------
* Học phí trừ qua ``UPDATE ... WHERE balance>=?`` + ``rowcount`` trong
  ``BEGIN IMMEDIATE`` — không đọc số dư rồi mới trừ (TOCTOU).
* **Không nhảy cấp học**: chỉ nhập học được đúng bậc kế tiếp
  (``career.can_enroll``), học phải trả trước toàn bộ.
* **Không vượt bằng khi xin việc**: ``career.can_apply`` chặn nghề vượt bằng.
* **Làm việc 1 lần/ngày**: dựa trên ``last_work``, không phải cooldown cứng.
* **Thăng chức cần ngày thật**: bậc không bao giờ vượt
  ``career.seniority_for(days_worked)`` nên không có cách nào tự tăng chức.
* **Đổi việc/nghỉ việc reset kinh nghiệm** + có thời gian chờ
  (``career.REHIRE_COOLDOWN``) — chặn việc chuyển vòng để lấy lại bậc cao.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiosqlite
import discord
from discord.ext import commands

from kai_core import career, get_logger
from kai_core.config import CONFIG

log = get_logger("career")

DB_PATH = str(CONFIG.data_dir / "economy.db")

#: Thưởng uy tín khi tốt nghiệp mỗi bậc.
GRADUATE_REP = 50
#: Hệ số uy tín khi tốt nghiệp, nhân theo cấp (bậc càng cao càng nhiều).
GRADUATE_REP_SCALE = (0, 1, 2, 4, 8)

#: Bảng mới — ``IF NOT EXISTS`` nên chạy lại vô hạn lần cũng an toàn.
#: Cố tình **không** tạo bảng ``users`` / ``transactions``: đó là schema của
#: ``cogs/games.py``, và module này được nạp sau ``games``.
SCHEMA = """
CREATE TABLE IF NOT EXISTS study (
    user_id    INTEGER PRIMARY KEY,
    level      TEXT NOT NULL DEFAULT 'chua_hoc',
    enrolled   TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL DEFAULT 0,
    paid       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    user_id     INTEGER PRIMARY KEY,
    job         TEXT    NOT NULL DEFAULT '',
    level       INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 0,
    hired_at    REAL    NOT NULL DEFAULT 0,
    last_work   REAL    NOT NULL DEFAULT 0,
    days_worked INTEGER NOT NULL DEFAULT 0,
    streak      INTEGER NOT NULL DEFAULT 0,
    last_switch REAL    NOT NULL DEFAULT 0
);
"""


def fmt_vnd(amount: Any) -> str:
    """Định dạng VNĐ kiểu Việt Nam (dấu chấm phân nghìn)."""
    try:
        return f"{int(amount):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def normalize(text: str) -> str:
    """Chuẩn hoá tên bậc học / nghề về khoá tra cứu (không phân biệt hoa thường)."""
    raw = str(text or "").strip().lower()
    table = {s.name.lower(): k for k, s in career.SCHOOLS.items()}
    table.update({j.name.lower(): k for k, j in career.JOBS.items()})
    return table.get(raw, raw.replace(" ", "_").replace("đ", "d"))


class CareerError(Exception):
    """Lỗi nghiệp vụ — cog bắt lại và trả lời người chơi."""


class Career(commands.Cog):
    """Học vấn, xin việc, đi làm và thăng chức."""

    def __init__(self, bot: commands.Bot, db_path: str | None = None) -> None:
        self.bot = bot
        self.db_path = db_path or DB_PATH
        self._lock_obj = asyncio.Lock()

    async def cog_load(self) -> None:
        """Tạo bảng nếu chưa có — idempotent, không cần migration."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                await db.executescript(SCHEMA)
                await db.commit()

    # ------------------------------------------------------------------
    # Tiện ích DB
    # ------------------------------------------------------------------

    def _lock(self) -> asyncio.Lock:
        return self._lock_obj

    @staticmethod
    async def _ensure_user(db, user_id: int) -> None:
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (int(user_id),))

    @staticmethod
    async def _balance(db, user_id: int) -> int:
        async with db.execute(
            "SELECT balance FROM users WHERE user_id=?", (int(user_id),)
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    async def _spend(db, user_id: int, amount: int) -> bool:
        """Trừ tiền **atomic**; ``False`` nếu không đủ. Chống TOCTOU."""
        if amount <= 0:
            return True
        cur = await db.execute(
            "UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?",
            (int(amount), int(user_id), int(amount)),
        )
        return cur.rowcount == 1

    @staticmethod
    async def _credit(db, user_id: int, amount: int) -> int:
        if amount > 0:
            await db.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (int(amount), int(user_id)),
            )
        return await Career._balance(db, user_id)

    @staticmethod
    async def _add_rep(db, user_id: int, amount: int) -> None:
        """Cộng uy tín — ``COALESCE`` vì cột có thể NULL ở DB cũ."""
        if amount > 0:
            await db.execute(
                "UPDATE users SET reputation=COALESCE(reputation,0)+? WHERE user_id=?",
                (int(amount), int(user_id)),
            )

    @staticmethod
    async def _log_txn(db, user_id: int, kind: str, amount: int, balance: int,
                       note: str = "") -> None:
        await db.execute(
            "INSERT INTO transactions(user_id,kind,amount,balance_after,created_at,note)"
            " VALUES(?,?,?,?,?,?)",
            (int(user_id), kind, int(amount), int(balance), time.time(), note[:200]),
        )

    @staticmethod
    async def _study_row(db, user_id: int) -> tuple[str, str, float, int]:
        """``(bằng đã có, đang học, mốc bắt đầu, đã trả học phí)``."""
        async with db.execute(
            "SELECT level,enrolled,started_at,paid FROM study WHERE user_id=?", (int(user_id),)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return "chua_hoc", "", 0.0, 0
        return str(row[0]), str(row[1]), float(row[2]), int(row[3])

    # ------------------------------------------------------------------
    # GIAO DUC — xem, nhap hoc, tot nghiep
    # ------------------------------------------------------------------

    @commands.command(name="trongtruong", aliases=["hocvien", "truonghoc"])
    async def school_status(self, ctx: commands.Context) -> None:
        """Xem học vấn hiện tại và bước học tiếp theo."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                level, enrolled, started, _paid = await self._study_row(db, ctx.author.id)

        have = career.SCHOOLS.get(level, career.SCHOOLS["chua_hoc"])
        embed = discord.Embed(
            title=f"🎓 HỌC VẤN — {ctx.author.display_name}",
            description=f"{have.emoji} **Bằng hiện có:** {have.name}",
            color=discord.Color.blue(),
        )
        if enrolled:
            school = career.SCHOOLS.get(enrolled)
            if school is not None:
                left = career.study_left(school, started)
                state = "✅ **Đã tốt nghiệp — dùng `!tophoc` để nhận bằng!**" if left == 0 \
                    else f"⏳ Còn **{left}**/{school.days} ngày"
                embed.add_field(
                    name=f"Đang học: {school.emoji} {school.name}",
                    value=state, inline=False,
                )
        else:
            nxt = career.next_school(level)
            if nxt is None:
                embed.add_field(name="Bước tiếp theo", value="🏆 Bạn đã học bằng cao nhất rồi!",
                                inline=False)
            else:
                embed.add_field(
                    name=f"Bước tiếp theo: {nxt.emoji} {nxt.name}",
                    value=f"Học phí: **{fmt_vnd(nxt.tuition)}đ** · Thời gian: **{nxt.days} ngày**\n"
                          f"Nhập học bằng `!nhaphoc`",
                    inline=False,
                )
        embed.add_field(
            name="Nghề đang mở khoá",
            value=", ".join(j.name for j in career.jobs_for(level)) or "Chưa có nghề nào",
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.command(name="nhaphoc", aliases=["dangkynhom", "enroll"])
    async def enroll(self, ctx: commands.Context, school: str | None = None) -> None:
        """Nhap hoc bai tiep theo (tra hoc phi truoc)."""
        uid = ctx.author.id
        now = time.time()
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await self._ensure_user(db, uid)
                    level, enrolled, _started, _paid = await self._study_row(db, uid)
                    if enrolled:
                        await db.rollback()
                        return await ctx.send(
                            f"❌ Bạn đang học **{career.SCHOOLS[enrolled].name}**. "
                            f"Phải tốt nghiệp trước đã."
                        )
                    nxt = career.next_school(level)
                    if nxt is None:
                        await db.rollback()
                        return await ctx.send("🏆 Bạn đã có bằng cao nhất rồi.")
                    if school is not None:
                        target = normalize(school)
                        if target != nxt.key:
                            await db.rollback()
                            return await ctx.send(
                                f"❌ Chỉ được nhập học đúng bước kế tiếp: "
                                f"**{nxt.name}**. Xem `!trongtruong`."
                            )
                    if not await self._spend(db, uid, nxt.tuition):
                        await db.rollback()
                        return await ctx.send(
                            f"❌ Không đủ học phí **{fmt_vnd(nxt.tuition)}đ**."
                        )
                    await db.execute(
                        "INSERT INTO study(user_id,level,enrolled,started_at,paid)"
                        " VALUES(?,?,?,?,?)"
                        " ON CONFLICT(user_id) DO UPDATE SET"
                        " enrolled=excluded.enrolled, started_at=excluded.started_at,"
                        " paid=study.paid+excluded.paid",
                        (int(uid), level, nxt.key, now, nxt.tuition),
                    )
                    balance = await self._balance(db, uid)
                    await self._log_txn(db, uid, "career:enroll", -nxt.tuition, balance,
                                        f"Nhap hoc {nxt.name}")
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Nhap hoc that bai")
                    raise CareerError(f"Giao dich loi: {type(exc).__name__}") from exc

        lines = [f"🎒 Đã nhập học **{nxt.emoji} {nxt.name}**."]
        if nxt.tuition:
            lines.append(f"Học phí: **{fmt_vnd(nxt.tuition)}đ** · Số dư: **{fmt_vnd(balance)}đ**")
        lines.append(f"Thời gian học: **{nxt.days} ngày**. Xong thì dùng `!tophoc` để nhận bằng.")
        await ctx.send("\n".join(lines))

    @commands.command(name="tophoc", aliases=["nghenhoc", "graduated"])
    async def graduate(self, ctx: commands.Context) -> None:
        """Nhận bằng khi đã học đủ thời gian."""
        uid = ctx.author.id
        now = time.time()
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await self._ensure_user(db, uid)
                    level, enrolled, started, _paid = await self._study_row(db, uid)
                    if not enrolled:
                        await db.rollback()
                        return await ctx.send("❌ Bạn chưa nhập học bài nào. Dùng `!nhaphoc`.")
                    school = career.SCHOOLS.get(enrolled)
                    if school is None:
                        await db.rollback()
                        return await ctx.send("❌ Bậc học không còn hợp lệ.")
                    left = career.study_left(school, started, now)
                    if left > 0:
                        await db.rollback()
                        return await ctx.send(
                            f"⏳ Còn **{left} ngày** nữa mới tốt nghiệp được."
                        )
                    # Chống nhận trùng: chỉ vào khi đang thật sự học bậc này.
                    if level == enrolled:
                        await db.rollback()
                        return await ctx.send("❌ Bằng này bạn đã có rồi.")
                    rep = GRADUATE_REP * GRADUATE_REP_SCALE[career.school_rank(enrolled)]
                    await self._add_rep(db, uid, rep)
                    await db.execute(
                        "UPDATE study SET level=?, enrolled='', started_at=0 WHERE user_id=?",
                        (enrolled, int(uid)),
                    )
                    balance = await self._balance(db, uid)
                    await self._log_txn(db, uid, "career:graduate", 0, balance,
                                        f"Tot nghiep {school.name} (+{rep} uy tin)")
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Tot nghiep that bai")
                    raise CareerError(f"Giao dich loi: {type(exc).__name__}") from exc

        unlocked = [j for j in career.jobs_for(enrolled)
                    if not career.can_apply(level, j.key)]
        text = (f"🎉 Chúc mừng! Bạn đã tốt nghiệp **{school.emoji} {school.name}**.\n"
                f"Uy tín **+{rep}**")
        if unlocked:
            text += "\n\n🔓 Nghề mới mở khoá: " + ", ".join(j.name for j in unlocked)
        else:
            text += "\n\nXem nghề đang mở khoá bằng `!vieclam`."
        await ctx.send(text)

    @commands.command(name="vieclam", aliases=["nghe", "jobs"])
    async def job_board(self, ctx: commands.Context) -> None:
        """Xem các nghề bằng học vấn hiện tại mở khoá."""
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                level, _enrolled, _started, _paid = await self._study_row(db, ctx.author.id)
        have = career.SCHOOLS.get(level, career.SCHOOLS["chua_hoc"])
        open_jobs = career.jobs_for(level)
        embed = discord.Embed(
            title="💼 VIỆC LÀM",
            description=f"Bằng của bạn: {have.emoji} **{have.name}**",
            color=discord.Color.green(),
        )
        if not open_jobs:
            embed.description += "\nChưa có nghề nào mở. Hãy học thêm (`!nhaphoc`)."
        for job in sorted(open_jobs, key=lambda j: -j.daily_salary):
            embed.add_field(
                name=f"{job.emoji} `{job.key}` — {job.name} · {fmt_vnd(job.daily_salary)}đ/ngày",
                value=f"{job.desc}\nỨng tuyển: `!ungtuyen {job.key}`",
                inline=False,
            )
        embed.set_footer(text="Lương thực tế ở Việt Nam chia theo ngày. Thăng chức để tăng lương.")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # VIEC LAM — ung tuyen, di lam viec, thang chuc, nghi viec
    # ------------------------------------------------------------------

    @staticmethod
    async def _job_row(db, user_id: int) -> dict:
        async with db.execute(
            "SELECT job,level,active,hired_at,last_work,days_worked,streak,last_switch"
            " FROM jobs WHERE user_id=?", (int(user_id),)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return {"job": "", "level": 0, "active": 0, "hired_at": 0.0, "last_work": 0.0,
                    "days": 0, "streak": 0, "last_switch": 0.0}
        return {"job": str(row[0]), "level": int(row[1]), "active": int(row[2]),
                "hired_at": float(row[3]), "last_work": float(row[4]),
                "days": int(row[5]), "streak": int(row[6]), "last_switch": float(row[7])}

    @commands.command(name="ungtuyen", aliases=["xinviec", "apply"])
    async def apply_job(self, ctx: commands.Context, job: str) -> None:
        """Ung tuyen mot nghe. Doi viec se reset kinh nghiem."""
        uid = ctx.author.id
        now = time.time()
        key = normalize(job)
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await self._ensure_user(db, uid)
                    level, enrolled, _s, _p = await self._study_row(db, uid)
                    # Dang hoc thi chua co bang de xin viec cao hon.
                    if enrolled:
                        await db.rollback()
                        return await ctx.send(
                            f"❌ Bạn đang học **{career.SCHOOLS[enrolled].name}**. "
                            f"Tốt nghiệp (`!tophoc`) rồi hãy xin việc."
                        )
                    entry = career.JOBS.get(key)
                    if entry is None:
                        await db.rollback()
                        return await ctx.send(f"❌ Không có nghề `{job}`. Xem `!vieclam`.")
                    if not career.can_apply(level, key):
                        need = career.SCHOOLS[entry.min_school]
                        await db.rollback()
                        return await ctx.send(
                            f"❌ Cần tối thiểu **{need.name}** để xin **{entry.name}**."
                        )
                    state = await self._job_row(db, uid)
                    if state["active"]:
                        cur_job = career.JOBS.get(state["job"])
                        name = cur_job.name if cur_job else state["job"]
                        await db.rollback()
                        return await ctx.send(
                            f"❌ Bạn đang làm **{name}**. Dùng `!nghiviec` nếu muốn nghỉ."
                        )
                    # Thoi gian cho: chong chuyen vong de reset kinh nghiem.
                    if state["last_switch"] > 0:
                        gap = int((now - state["last_switch"]) // career.DAY)
                        if gap < career.REHIRE_COOLDOWN:
                            await db.rollback()
                            return await ctx.send(
                                f"⏳ Cần chờ **{career.REHIRE_COOLDOWN - gap} ngày** nữa "
                                f"mới ứng tuyển lại được."
                            )
                    await db.execute(
                        "INSERT INTO jobs(user_id,job,level,active,hired_at,last_work,"
                        "days_worked,streak,last_switch) VALUES(?,?,0,1,?,0,0,0,?)"
                        " ON CONFLICT(user_id) DO UPDATE SET job=excluded.job, level=0,"
                        " active=1, hired_at=excluded.hired_at, last_work=0,"
                        " days_worked=0, streak=0, last_switch=excluded.last_switch",
                        (int(uid), key, now, now),
                    )
                    balance = await self._balance(db, uid)
                    await self._log_txn(db, uid, "career:hire", 0, balance,
                                        f"Ung tuyen {entry.name}")
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Ung tuyen that bai")
                    raise CareerError(f"Giao dich loi: {type(exc).__name__}") from exc

        await ctx.send(
            f"🎉 Chúc mừng, bạn đã được nhận vào **{entry.emoji} {entry.name}**!\n"
            f"Lương khởi điểm: **{fmt_vnd(career.salary_for(key, 0))}đ/ngày**.\n"
            f"Đi làm mỗi ngày bằng `!dilam`. Số dư: **{fmt_vnd(balance)}đ**"
        )

    # Tên ``lamviec`` đã thuộc alias của ``!work`` trong ``cogs/games.py`` —
    # đăng ký trùng làm ``add_cog`` raise và bot chết, nên đổi thành ``!dilam``.
    @commands.command(name="dilam", aliases=["dilamviec"])
    async def work(self, ctx: commands.Context) -> None:
        """Đi làm một ngày, nhận lương (mỗi ngày một lần)."""
        uid = ctx.author.id
        now = time.time()
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await self._ensure_user(db, uid)
                    state = await self._job_row(db, uid)
                    if not state["active"] or not state["job"]:
                        await db.rollback()
                        return await ctx.send("❌ Bạn chưa có việc. Dùng `!vieclam` rồi `!ungtuyen`.")
                    # Chốt ngày làm việc bằng so sánh ngày, không dùng mốc 24h —
                    # nếu dùng mốc thời gian thì chờ nửa đêm là lấy lương 2 lần.
                    if state["last_work"] > 0:
                        done_today = int(state["last_work"] // career.DAY) == int(now // career.DAY)
                        if done_today:
                            await db.rollback()
                            return await ctx.send("⏳ Hôm nay bạn đã đi làm rồi. Mai quay lại!")
                    entry = career.JOBS.get(state["job"])
                    if entry is None:
                        await db.rollback()
                        return await ctx.send("❌ Nghề của bạn không còn hợp lệ.")

                    # Chuỗi ngày liên tục: ngắt quãng thì về 0.
                    gap_days = int((now - state["last_work"]) // career.DAY) if state["last_work"] else 0
                    streak = state["streak"] + 1 if gap_days <= 1 else 1
                    level = state["level"]
                    pay = career.work_pay(state["job"], level, streak)
                    # days_worked chỉ tăng khi thật sự đi làm (1 lần/ngày).
                    days = state["days"] + 1
                    cur = await db.execute(
                        "UPDATE jobs SET last_work=?, days_worked=?, streak=?"
                        " WHERE user_id=? AND active=1 AND last_work=?",
                        (now, days, streak, int(uid), state["last_work"]),
                    )
                    if cur.rowcount != 1:
                        # Co ngay chay song song voi len khac da ghi last_work.
                        await db.rollback()
                        return await ctx.send("⏳ Hôm nay bạn đã đi làm rồi.")
                    balance = await self._credit(db, uid, pay)
                    await self._log_txn(db, uid, "career:work", pay, balance,
                                        f"Di lam viec {entry.name} (ngay {days})")
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Di lam viec that bai")
                    raise CareerError(f"Giao dich loi: {type(exc).__name__}") from exc

        lines = [
            f"💼 Bạn đi làm hôm nay ở **{entry.emoji} {entry.name}** "
            f"({career.seniority_title(level)}).",
            f"Nhận lương **{fmt_vnd(pay)}đ** · Số dư: **{fmt_vnd(balance)}đ**",
            f"Chuỗi ngày liên tục: **{streak}** ngày (tối đa thưởng "
            f"{career.STREAK_MAX} ngày). Tổng đã đi làm: **{days} ngày**.",
        ]
        nxt = career.next_promotion(days)
        if nxt is not None and level < career.seniority_for(days):
            lines.append(f"⭐ Bạn đã đủ điều kiện lên **{nxt[1]}** — dùng `!thangchuc`!")
        await ctx.send("\n".join(lines))

    @commands.command(name="thangchuc", aliases=["promote"])
    async def promote(self, ctx: commands.Context) -> None:
        """Đề nghị thăng chức khi đã đủ số ngày đi làm."""
        uid = ctx.author.id
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    state = await self._job_row(db, uid)
                    if not state["active"] or not state["job"]:
                        await db.rollback()
                        return await ctx.send("❌ Bạn chưa có việc để thăng chức.")
                    # Trần bậc do NGÀY ĐI LÀM quyết định — không có cách nào
                    # tự tăng chức nếu không thật sự đi làm đủ ngày.
                    ceiling = career.seniority_for(state["days"])
                    if state["level"] >= ceiling:
                        await db.rollback()
                        nxt = career.next_promotion(state["days"])
                        if nxt is None:
                            return await ctx.send("🏆 Bạn đã lên bậc cao nhất rồi!")
                        return await ctx.send(
                            f"⏳ Cần đi làm thêm **{nxt[0]} ngày** nữa để lên "
                            f"**{nxt[1]}** (hiện đã đi làm {state['days']} ngày)."
                        )
                    new_level = state["level"] + 1
                    cur = await db.execute(
                        "UPDATE jobs SET level=? WHERE user_id=? AND level=?",
                        (new_level, int(uid), state["level"]),
                    )
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send("⏳ Có thay đổi, thử lại.")
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Thang chuc that bai")
                    raise CareerError(f"Giao dich loi: {type(exc).__name__}") from exc

        entry = career.JOBS.get(state["job"])
        if entry is None:
            return await ctx.send("❌ Nghề của bạn không còn hợp lệ.")
        await ctx.send(
            f"🎊 Chúc mừng! Bạn được thăng lên **{career.seniority_title(new_level)}** "
            f"tại **{entry.name}**.\n"
            f"Lương/ngày mới: **{fmt_vnd(career.salary_for(state['job'], new_level))}đ** "
            f"(cũ: {fmt_vnd(career.salary_for(state['job'], new_level - 1))}đ)"
        )

    @commands.command(name="nghiviec", aliases=["resign", "saoviec"])
    async def resign(self, ctx: commands.Context) -> None:
        """Nghi viec. Kinh nghiem se bi reset va phai cho mot thoi gian."""
        uid = ctx.author.id
        now = time.time()
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    state = await self._job_row(db, uid)
                    if not state["active"] or not state["job"]:
                        await db.rollback()
                        return await ctx.send("❌ Bạn đang không làm việc nào.")
                    cur = await db.execute(
                        "UPDATE jobs SET active=0, level=0, days_worked=0, streak=0,"
                        " last_switch=? WHERE user_id=? AND active=1",
                        (now, int(uid)),
                    )
                    if cur.rowcount != 1:
                        await db.rollback()
                        return await ctx.send("⏳ Có thay đổi, thử lại.")
                    await db.commit()
                except Exception as exc:  # noqa: BLE001
                    try:
                        await db.rollback()
                    except Exception:  # pragma: no cover
                        pass
                    log.exception("Nghi viec that bai")
                    raise CareerError(f"Giao dich loi: {type(exc).__name__}") from exc

        entry = career.JOBS.get(state["job"])
        name = entry.name if entry else state["job"]
        await ctx.send(
            f"👋 Bạn đã nghỉ việc **{name}** sau {state['days']} ngày.\n"
            f"Kinh nghiệm đã về 0. Phải chờ **{career.REHIRE_COOLDOWN} ngày** mới "
            f"ứng tuyển lại được."
        )

    # ------------------------------------------------------------------
    # HO SO — xem tong quan su nghiep
    # ------------------------------------------------------------------

    # ``profile`` đã là lệnh của ``cogs/database.py`` — không được dùng làm alias.
    @commands.command(name="hoso", aliases=["cv", "hoscongviec"])
    async def profile(self, ctx: commands.Context) -> None:
        """Xem hồ sơ sự nghiệp: học vấn, nghề, thâm niên, lương."""
        uid = ctx.author.id
        async with self._lock():
            async with aiosqlite.connect(self.db_path) as db:
                level, enrolled, started, paid = await self._study_row(db, uid)
                state = await self._job_row(db, uid)
                balance = await self._balance(db, uid)
                async with db.execute(
                    "SELECT COALESCE(reputation,0) FROM users WHERE user_id=?", (int(uid),)
                ) as cur:
                    row = await cur.fetchone()
                rep = int(row[0]) if row else 0

        school = career.SCHOOLS.get(level, career.SCHOOLS["chua_hoc"])
        embed = discord.Embed(
            title=f"📋 HỒ SƠ SỰ NGHIỆP — {ctx.author.display_name}",
            description=f"{school.emoji} **{school.name}** · Uy tín: **{rep}**",
            color=discord.Color.gold(),
        )
        if enrolled:
            sc = career.SCHOOLS[enrolled]
            left = career.study_left(sc, started)
            embed.add_field(
                name="🎓 Đang học",
                value=(f"{sc.emoji} {sc.name} — "
                       + ("✅ Sẵn sàng `!tophoc`" if left == 0 else f"còn {left} ngày")),
                inline=False,
            )
        if state["active"] and state["job"]:
            entry = career.JOBS.get(state["job"])
            if entry is not None:
                title = career.seniority_title(state["level"])
                embed.add_field(
                    name="💼 Công việc hiện tại",
                    value=(f"{entry.emoji} **{entry.name}** — {title}\n"
                           f"Lương: **{fmt_vnd(career.salary_for(state['job'], state['level']))}đ**/ngày · "
                           f"Đã làm **{state['days']} ngày** · Chuỗi **{state['streak']}** ngày"),
                    inline=False,
                )
                nxt = career.next_promotion(state["days"])
                if nxt is None:
                    embed.add_field(name="⭐ Thăng chức",
                                    value="🏆 Bậc cao nhất", inline=False)
                else:
                    embed.add_field(
                        name="⭐ Thăng chức",
                        value=(f"Trần bậc hiện tại: **{career.seniority_title(career.seniority_for(state['days']))}**\n"
                               f"Lên **{nxt[1]}** sau **{nxt[0]} ngày** đi làm (`!thangchuc`)"),
                        inline=False,
                    )
        else:
            embed.add_field(name="💼 Công việc", value="Chưa có việc — dùng `!vieclam`",
                            inline=False)
        embed.add_field(name="💰 Số dư",
                        value=f"**{fmt_vnd(balance)}đ** · Học phí đã trả **{fmt_vnd(paid)}đ**",
                        inline=False)
        await ctx.send(embed=embed)

    # Lưu ý: discord.py vẫn dispatch ``command_error`` sau khi chạy handler cục bộ,
    # nên ta đánh dấu ``ctx.kai_error_handled`` để handler tập trung ở ``main.py``
    # không gửi tin nhắn lần thứ hai cho cùng một lỗi (xem cogs/love.py).
    @school_status.error
    @enroll.error
    @graduate.error
    @job_board.error
    @apply_job.error
    @work.error
    @promote.error
    @resign.error
    @profile.error
    async def career_error(self, ctx: commands.Context, error: Exception) -> None:
        original = error
        while hasattr(original, "original"):
            original = original.original
        if isinstance(original, CareerError):
            await ctx.send(f"⚠️ {original}")
            ctx.kai_error_handled = True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Career(bot))

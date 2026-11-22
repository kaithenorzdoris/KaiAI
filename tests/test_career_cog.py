# -*- coding: utf-8 -*-
"""
Test tích hợp cho ``cogs/career.py`` — Giáo dục & sự nghiệp.

File này chạy **các lệnh thật** trên SQLite tạm để chứng minh:

1. Schema ``study`` / ``jobs`` tạo idempotent.
2. Không nhảy cấp học; học phí trừ đúng một lần (không sinh tiền).
3. ``!tophoc`` chặn khi chưa đủ ngày; đủ ngày thì nhận bằng + uy tín.
4. Không xin được nghề vượt bằng; đi làm tối đa 1 lần/ngày, tiền công
   khớp ``career.work_pay`` (không tự sinh tiền).
5. Thăng chức chỉ lên được bậc do **ngày đi làm** quyết định.
6. Nghỉ việc reset kinh nghiệm + phải chờ ``REHIRE_COOLDOWN`` ngày.
7. Không trùng tên/alias lệnh với Cog khác (đã từng làm bot chết).
"""

from __future__ import annotations

import pathlib
import tempfile
import time
import unittest

try:
    import aiosqlite
except ImportError:  # pragma: no cover
    aiosqlite = None

from discord.ext import commands

from cogs.career import SCHEMA, Career, CareerError, fmt_vnd, normalize
from kai_core import career

DAY = 86_400


class _Member:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.display_name = f"u{uid}"
        self.mention = f"<@{uid}>"


class _Ctx:
    """Context giả: chỉ cần ``author`` và ``send``."""

    def __init__(self, uid: int) -> None:
        self.author = _Member(uid)
        self.sent: list = []

    async def send(self, content=None, embed=None, **kwargs):
        self.sent.append(content if content is not None else embed)

    @property
    def last(self):
        return self.sent[-1]


class _Bot:
    """Cog không gọi bot trong các lệnh này — giữ hình dạng tối thiểu."""


class HelperTests(unittest.TestCase):
    """Hàm thuần — không cần DB."""

    def test_fmt_vnd_uses_dots(self) -> None:
        self.assertEqual(fmt_vnd(45_000_000), "45.000.000")
        self.assertEqual(fmt_vnd("x"), "0")

    def test_normalize_school_and_job_names(self) -> None:
        self.assertEqual(normalize("Đại học (Cử nhân)"), "dai_hoc")
        self.assertEqual(normalize("KỸ SƯ IT"), "ky_su_it")
        self.assertEqual(normalize("cong_nhan"), "cong_nhan")

    def test_normalize_unknown_becomes_slug(self) -> None:
        self.assertEqual(normalize("Xa May"), "xa_may")
        self.assertEqual(normalize("dai hoc"), "dai_hoc")


@unittest.skipUnless(aiosqlite, "aiosqlite chưa được cài đặt")
class CareerDBTests(unittest.IsolatedAsyncioTestCase):
    """Chạy lệnh thật trên SQLite tạm."""

    START_BALANCE = 300_000_000

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = str(pathlib.Path(self._tmp.name) / "economy.db")
        async with aiosqlite.connect(self.db) as conn:
            # Schema tối thiểu của ``cogs/games.py`` — cog career chỉ ĐỌC
            # ``users``/``transactions``, không tự tạo chúng.
            await conn.executescript(
                """
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0,
                    bank INTEGER NOT NULL DEFAULT 0,
                    xp INTEGER, level INTEGER, streak INTEGER, reputation INTEGER
                );
                CREATE TABLE transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, amount INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL, created_at REAL NOT NULL,
                    note TEXT DEFAULT ''
                );
                """
            )
            await conn.execute(
                "INSERT INTO users(user_id,balance) VALUES(1,?)", (self.START_BALANCE,)
            )
            await conn.execute(
                "INSERT INTO users(user_id,balance) VALUES(2,?)", (self.START_BALANCE,)
            )
            await conn.commit()
        self.cog = Career(_Bot(), db_path=self.db)
        # Gắn cog + bỏ cooldown để gọi lệnh trực tiếp (như test_realworld_cog.py).
        # KHÔNG gọi ``cog_load`` vì hàm đó bật task nội bộ -> rò event loop.
        for cmd in self.cog.__cog_commands__:
            cmd.cog = self.cog
            if cmd.__dict__.get("_cooldown") is not None:
                cmd.__dict__["_cooldown"] = None
        async with aiosqlite.connect(self.db) as conn:
            await conn.executescript(SCHEMA)
            await conn.commit()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    # -- tiện ích DB ----------------------------------------------------

    async def _bal(self, uid: int) -> int:
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT balance FROM users WHERE user_id=?", (int(uid),)
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _rep(self, uid: int) -> int:
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT COALESCE(reputation,0) FROM users WHERE user_id=?", (int(uid),)
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _study(self, uid: int) -> tuple:
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT level,enrolled,started_at,paid FROM study WHERE user_id=?",
                (int(uid),),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return ("chua_hoc", "", 0.0, 0)
        return (str(row[0]), str(row[1]), float(row[2]), int(row[3]))

    async def _job(self, uid: int) -> tuple:
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT job,level,active,days_worked,streak,last_work,last_switch"
                " FROM jobs WHERE user_id=?",
                (int(uid),),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return ("", 0, 0, 0, 0, 0.0, 0.0)
        return (str(row[0]), int(row[1]), int(row[2]), int(row[3]),
                int(row[4]), float(row[5]), float(row[6]))

    async def _exec(self, sql: str, *params) -> None:
        async with aiosqlite.connect(self.db) as conn:
            await conn.execute(sql, params)
            await conn.commit()

    # -- schema ----------------------------------------------------------

    async def test_schema_is_idempotent(self) -> None:
        for _ in range(3):
            async with aiosqlite.connect(self.db) as conn:
                await conn.executescript(SCHEMA)
                await conn.commit()
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) as cur:
                names = {r[0] for r in await cur.fetchall()}
        self.assertIn("study", names)
        self.assertIn("jobs", names)
        self.assertEqual(await self._bal(1), self.START_BALANCE)

    # -- giáo dục --------------------------------------------------------

    async def test_hocvien_shows_next_step(self) -> None:
        ctx = _Ctx(1)
        await self.cog.school_status(ctx)
        embed = ctx.last
        self.assertIn("HỌC VẤN", embed.title)
        joined = " ".join(f.name + f.value for f in embed.fields)
        self.assertIn("Trung học phổ thông", joined)

    async def test_nhaphoc_thpt_is_free_and_enrolls(self) -> None:
        ctx = _Ctx(1)
        before = await self._bal(1)
        await self.cog.enroll(ctx)
        self.assertIn("nhập học", str(ctx.last).lower())
        level, enrolled, started, paid = await self._study(1)
        self.assertEqual(level, "chua_hoc")
        self.assertEqual(enrolled, "thpt")
        self.assertGreater(started, 0)
        self.assertEqual(paid, 0)
        self.assertEqual(await self._bal(1), before, "THPT miễn phí không được trừ tiền")

    async def test_khong_nhay_cap_hoc(self) -> None:
        """Không thể nhảy từ chưa học lên Đại học — phải đi đúng bước kế tiếp."""
        ctx = _Ctx(1)
        await self.cog.enroll(ctx, "Đại học (Cử nhân)")
        self.assertIn("bước kế tiếp", str(ctx.last).lower())
        self.assertEqual((await self._study(1))[1], "", "Không được phép nhảy cấp")

    async def test_nhaphoc_khi_dang_hoc_bi_chan(self) -> None:
        await self.cog.enroll(_Ctx(1))
        ctx = _Ctx(1)
        await self.cog.enroll(ctx)
        self.assertIn("đang học", str(ctx.last).lower())

    async def test_hoc_phi_du_duoc_tru_mot_lan(self) -> None:
        """Nhập Đại học trừ đúng học phí một lần, ghi log giao dịch."""
        await self._exec("INSERT INTO study(user_id,level) VALUES(1,'thpt')")
        await self._exec("UPDATE users SET balance=?", 45_000_000)
        ctx = _Ctx(1)
        await self.cog.enroll(ctx)
        self.assertEqual(await self._bal(1), 0)
        level, enrolled, started, paid = await self._study(1)
        self.assertEqual(level, "thpt")
        self.assertEqual(enrolled, "dai_hoc")
        self.assertEqual(paid, 45_000_000)
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT amount FROM transactions WHERE user_id=1 AND kind='career:enroll'"
            ) as cur:
                rows = await cur.fetchall()
        self.assertEqual(len(rows), 1, "Chỉ được trừ học phí đúng 1 lần")
        self.assertEqual(int(rows[0][0]), -45_000_000)

    async def test_hoc_phi_khong_du_khong_bi_tru(self) -> None:
        await self._exec("INSERT INTO study(user_id,level) VALUES(1,'thpt')")
        await self._exec("UPDATE users SET balance=?", 44_999_999)
        ctx = _Ctx(1)
        await self.cog.enroll(ctx)
        self.assertIn("Không đủ học phí", str(ctx.last))
        self.assertEqual(await self._bal(1), 44_999_999, "Số dư không được thay đổi")
        self.assertEqual((await self._study(1))[1], "", "Không được ghi enrolled khi fail")

    async def test_tophoc_truoc_thoi_han_bi_chan(self) -> None:
        await self.cog.enroll(_Ctx(1))  # THPT: 14 ngày
        ctx = _Ctx(1)
        await self.cog.graduate(ctx)
        self.assertIn("14 ngày", str(ctx.last))
        self.assertEqual((await self._study(1))[1], "thpt", "Vẫn phải chờ")

    async def test_tot_nghiep_nhan_bang_uy_tin(self) -> None:
        await self.cog.enroll(_Ctx(1))
        await self._exec(
            "UPDATE study SET started_at=? WHERE user_id=1", time.time() - 14 * DAY - 5
        )
        ctx = _Ctx(1)
        await self.cog.graduate(ctx)
        self.assertIn("Chúc mừng", str(ctx.last))
        self.assertIn("Kỹ sư IT", str(ctx.last), "Nghề mới phải được mở khoá")
        level, enrolled, _s, _p = await self._study(1)
        self.assertEqual(level, "thpt")
        self.assertEqual(enrolled, "")
        self.assertEqual(await self._rep(1), 50, "Tốt nghiệp THPT = 50 uy tín")

    async def test_tot_nghiep_dai_hoc_uy_tin_ca_hon(self) -> None:
        """Bậc học cao hơn phải cho nhiều uy tín hơn (hệ số 50 * scale)."""
        await self.cog.enroll(_Ctx(1))
        await self._exec(
            "UPDATE study SET started_at=? WHERE user_id=1", time.time() - 14 * DAY - 5
        )
        await self.cog.graduate(_Ctx(1))  # thpt: +50
        await self.cog.enroll(_Ctx(1))    # dai_hoc: -45.000.000
        await self._exec(
            "UPDATE study SET started_at=? WHERE user_id=1", time.time() - 45 * DAY - 5
        )
        await self.cog.graduate(_Ctx(1))
        level, _e, _s, _p = await self._study(1)
        self.assertEqual(level, "dai_hoc")
        self.assertEqual(await self._rep(1), 150, "Cử nhân = 50 uy tín (50*2)")

    # -- xin việc & đi làm ------------------------------------------------

    async def test_vieclam_theo_bang(self) -> None:
        ctx = _Ctx(1)
        await self.cog.job_board(ctx)
        joined = " ".join(f.name for f in ctx.last.fields)
        self.assertIn("cong_nhan", joined)
        self.assertNotIn("ky_su_it", joined, "Chưa THPT thì không thấy nghề kỹ thuật")
        await self._exec(
            "INSERT INTO study(user_id,level) VALUES(1,'thpt')"
            " ON CONFLICT(user_id) DO UPDATE SET level='thpt'"
        )
        ctx2 = _Ctx(1)
        await self.cog.job_board(ctx2)
        self.assertIn("ky_su_it", " ".join(f.name for f in ctx2.last.fields))

    async def test_ungtuyen_vuot_bang_bi_chan(self) -> None:
        ctx = _Ctx(1)
        await self.cog.apply_job(ctx, "ky_su_it")
        self.assertIn("Cần tối thiểu", str(ctx.last))
        _job, _lv, active, *_ = await self._job(1)
        self.assertEqual(active, 0, "Không được nhận việc khi thiếu bằng")

    async def test_ungtuyen_khi_dang_hoc_bi_chan(self) -> None:
        await self._exec(
            "INSERT INTO study(user_id,level,enrolled) VALUES(1,'thpt','dai_hoc')"
        )
        ctx = _Ctx(1)
        await self.cog.apply_job(ctx, "cong_nhan")
        self.assertIn("Tốt nghiệp", str(ctx.last))
        self.assertEqual((await self._job(1))[2], 0)

    async def test_ungtuyen_khi_dang_lam_bi_chan(self) -> None:
        await self.cog.apply_job(_Ctx(1), "cong_nhan")
        ctx = _Ctx(1)
        await self.cog.apply_job(ctx, "van_phong")
        self.assertIn("đang làm", str(ctx.last).lower())
        self.assertEqual((await self._job(1))[0], "cong_nhan")

    async def test_ungtuyen_va_mot_lan_lam_moi_ngay(self) -> None:
        ctx = _Ctx(1)
        await self.cog.apply_job(ctx, "cong_nhan")
        self.assertIn("Chúc mừng", str(ctx.last))
        job, level, active, days, *_ = await self._job(1)
        self.assertEqual((job, level, active, days), ("cong_nhan", 0, 1, 0))

        before = await self._bal(1)
        ctx2 = _Ctx(1)
        await self.cog.work(ctx2)
        pay = career.work_pay("cong_nhan", 0, 1)
        self.assertEqual(await self._bal(1), before + pay)
        self.assertIn("Nhận lương", str(ctx2.last))

        ctx3 = _Ctx(1)
        await self.cog.work(ctx3)
        self.assertIn("Hôm nay", str(ctx3.last))
        self.assertEqual(await self._bal(1), before + pay, "Không có lương lần 2")
        _j, _l, _a, days, streak, *_ = await self._job(1)
        self.assertEqual((days, streak), (1, 1))

    async def test_lam_viec_sang_ngay_moi_va_streak(self) -> None:
        await self.cog.apply_job(_Ctx(1), "cong_nhan")
        await self.cog.work(_Ctx(1))  # ngày 1: streak = 1
        await self._exec("UPDATE jobs SET last_work=? WHERE user_id=1", time.time() - DAY)
        before = await self._bal(1)
        await self.cog.work(_Ctx(1))  # ngày kế: streak = 2 -> +5%
        pay = career.work_pay("cong_nhan", 0, 2)
        self.assertEqual(await self._bal(1), before + pay)
        _j, _l, _a, days, streak, *_ = await self._job(1)
        self.assertEqual((days, streak), (2, 2))

    async def test_streak_reset_khi_ngat_quang(self) -> None:
        await self.cog.apply_job(_Ctx(1), "cong_nhan")
        await self._exec("UPDATE jobs SET last_work=? WHERE user_id=1",
                         time.time() - 3 * DAY)
        before = await self._bal(1)
        await self.cog.work(_Ctx(1))
        self.assertEqual(await self._bal(1), before + career.work_pay("cong_nhan", 0, 1))
        _j, _l, _a, _d, streak, *_ = await self._job(1)
        self.assertEqual(streak, 1, "Ngắt 3 ngày phải về streak 1")

    async def test_tien_cong_khong_tu_sinh(self) -> None:
        """Tổng giao dịch ``career:work`` phải khớp đúng số dư đã tăng."""
        await self.cog.apply_job(_Ctx(1), "cong_nhan")
        for offset in (DAY, 2 * DAY, 3 * DAY):
            await self._exec("UPDATE jobs SET last_work=? WHERE user_id=1",
                             time.time() - offset)
            await self.cog.work(_Ctx(1))
        async with aiosqlite.connect(self.db) as conn:
            async with conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM transactions"
                " WHERE kind='career:work'"
            ) as cur:
                paid = int((await cur.fetchone())[0])
        self.assertGreater(paid, 0)
        self.assertEqual(await self._bal(1), self.START_BALANCE + paid)

    async def test_lamviec_khi_chua_co_viec(self) -> None:
        ctx = _Ctx(2)
        await self.cog.work(ctx)
        self.assertIn("chưa có việc", str(ctx.last).lower())

    # -- thăng chức --------------------------------------------------------

    async def test_thangchuc_can_du_ngay(self) -> None:
        await self.cog.apply_job(_Ctx(1), "cong_nhan")
        await self._exec("UPDATE jobs SET days_worked=29 WHERE user_id=1")
        ctx = _Ctx(1)
        await self.cog.promote(ctx)
        self.assertIn("1 ngày", str(ctx.last))
        self.assertEqual((await self._job(1))[1], 0, "Chưa đủ ngày không được lên")

        await self._exec("UPDATE jobs SET days_worked=30 WHERE user_id=1")
        ctx2 = _Ctx(1)
        await self.cog.promote(ctx2)
        self.assertIn("Chuyên viên", str(ctx2.last))
        self.assertEqual((await self._job(1))[1], 1)

        ctx3 = _Ctx(1)
        await self.cog.promote(ctx3)
        self.assertIn("Cần đi làm thêm", str(ctx3.last))
        self.assertEqual((await self._job(1))[1], 1, "Gọi lại không được tăng tiếp")

    async def test_thangchuc_len_bac_cao_nhat(self) -> None:
        await self.cog.apply_job(_Ctx(1), "cong_nhan")
        await self._exec("UPDATE jobs SET days_worked=1800 WHERE user_id=1")
        for _ in range(3):  # Chuyên viên -> Trưởng phòng -> Giám đốc
            await self.cog.promote(_Ctx(1))
        self.assertEqual((await self._job(1))[1], career.MAX_SENIORITY)
        ctx = _Ctx(1)
        await self.cog.promote(ctx)
        self.assertIn("cao nhất", str(ctx.last))
        self.assertEqual((await self._job(1))[1], career.MAX_SENIORITY)

    async def test_khong_tu_tang_bac_khi_khong_du_ngay(self) -> None:
        """Bậc không bao giờ vượt ``seniority_for(days_worked)``."""
        await self.cog.apply_job(_Ctx(1), "cong_nhan")
        for _ in range(5):  # thăng nhiều lần nhưng days_worked vẫn = 0
            await self.cog.promote(_Ctx(1))
        job, level, _a, days, *_ = await self._job(1)
        self.assertEqual(level, career.seniority_for(days))
        self.assertEqual((job, days), ("cong_nhan", 0))

    async def test_thangchuc_khi_chua_co_viec(self) -> None:
        ctx = _Ctx(2)
        await self.cog.promote(ctx)
        self.assertIn("chưa có việc", str(ctx.last).lower())

    # -- nghỉ việc ---------------------------------------------------------

    async def test_nghiviec_reset_va_cho_lai(self) -> None:
        await self.cog.apply_job(_Ctx(1), "cong_nhan")
        await self._exec("UPDATE jobs SET days_worked=45, level=1 WHERE user_id=1")
        ctx = _Ctx(1)
        await self.cog.resign(ctx)
        self.assertIn("nghỉ việc", str(ctx.last).lower())
        _j, level, active, days, streak, _lw, last_switch = await self._job(1)
        self.assertEqual((active, level, days, streak), (0, 0, 0, 0))
        self.assertGreater(last_switch, 0)

        ctx2 = _Ctx(1)
        await self.cog.apply_job(ctx2, "van_phong")
        self.assertIn("Cần chờ", str(ctx2.last))
        self.assertEqual((await self._job(1))[0], "cong_nhan", "Chưa đổi việc khi chờ")

        await self._exec(
            "UPDATE jobs SET last_switch=?",
            time.time() - career.REHIRE_COOLDOWN * DAY,
        )
        ctx3 = _Ctx(1)
        await self.cog.apply_job(ctx3, "van_phong")
        self.assertIn("Chúc mừng", str(ctx3.last))
        job, level, active, days, *_ = await self._job(1)
        self.assertEqual((job, level, active, days), ("van_phong", 0, 1, 0))

    async def test_nghiviec_khi_chua_co_viec(self) -> None:
        ctx = _Ctx(2)
        await self.cog.resign(ctx)
        self.assertIn("không làm việc nào", str(ctx.last))

    # -- hồ sơ & xử lý lỗi ---------------------------------------------------

    async def test_hoso_tong_quan(self) -> None:
        await self.cog.apply_job(_Ctx(1), "cong_nhan")
        await self._exec("UPDATE jobs SET days_worked=5, streak=5 WHERE user_id=1")
        await self.cog.enroll(_Ctx(1))  # đang học THPT
        ctx = _Ctx(1)
        await self.cog.profile(ctx)
        embed = ctx.last
        self.assertIn("HỒ SƠ", embed.title)
        values = " ".join(f.name + " " + f.value for f in embed.fields)
        self.assertIn("Công nhân", values)
        self.assertIn("Nhân viên", values)  # chức danh bậc 0
        self.assertIn("300.000", values)    # lương bậc 0
        # Bằng hiện có vẫn là "chưa tốt nghiệp"; THPT đang học nằm ở field.
        self.assertIn("Chưa tốt nghiệp", embed.description)
        self.assertIn("Trung học phổ thông", values)

    async def test_error_handler_gui_canh_bao(self) -> None:
        ctx = _Ctx(1)
        err = commands.CommandInvokeError(CareerError("boom"))
        await self.cog.career_error(ctx, err)
        self.assertEqual(str(ctx.last), "⚠️ boom")
        self.assertTrue(getattr(ctx, "kai_error_handled", False),
                        "Phải đánh dấu đã xử lý để main.py không gửi lần 2")

    async def test_error_handler_bo_qua_loi_khac(self) -> None:
        ctx = _Ctx(1)
        err = commands.CommandInvokeError(ValueError("x"))
        await self.cog.career_error(ctx, err)
        self.assertEqual(ctx.sent, [], "Lỗi khác phải để handler tập trung xử lý")

    # -- đăng ký lệnh --------------------------------------------------------

    async def test_all_commands_registered(self) -> None:
        names = {c.name for c in self.cog.__cog_commands__}
        for expected in ("trongtruong", "nhaphoc", "tophoc", "vieclam", "ungtuyen",
                         "dilam", "thangchuc", "nghiviec", "hoso"):
            self.assertIn(expected, names)

    def test_no_name_or_alias_collision_with_other_cogs(self) -> None:
        """Trùng tên/alias làm discord.py raise lúc ``add_cog`` -> bot chết."""
        from cogs.games import Games
        from cogs.company import Company
        from cogs.business import Business
        from cogs.realworld import RealWorld

        def names_of(cls) -> set:
            out = set()
            for attr in vars(cls).values():
                name = getattr(attr, "name", None)
                if name and hasattr(attr, "callback"):
                    out.add(name)
                    out.update(getattr(attr, "aliases", None) or [])
            return out

        mine = names_of(type(self.cog))
        self.assertIn("trongtruong", mine)
        for other, label in ((Games, "Games"), (Company, "Company"),
                             (Business, "Business"), (RealWorld, "RealWorld")):
            clash = mine & names_of(other)
            self.assertFalse(clash, f"Trùng lệnh với {label}: {sorted(clash)}")


if __name__ == "__main__":
    unittest.main()



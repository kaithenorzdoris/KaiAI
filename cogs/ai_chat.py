import asyncio
import base64
import datetime
import io
import os
import time

import aiohttp
import discord
import openai
import psutil
import pytz
from discord.ext import commands
from openai import AsyncOpenAI

from kai_core import get_logger
from kai_core.ai import AIRequest, ChatMessage, build_router_from_env
from kai_core.ai.confirm import PendingAction, PendingActions, run_confirmed
from kai_core.ai.scheduler import (
    ScheduleStore,
    run_due_once,
    run_schedule_now as run_one_schedule,
    scheduler_enabled,
    scheduler_loop,
)
from kai_core.ai.plan import (
    STEP_DONE,
    PlanStore,
    draft_plan_from_text,
    execute_plan,
    step_owner_error,
)
from kai_core.ai.tools import AITools
from kai_core.config import CONFIG

log = get_logger("ai_chat")

# ==========================================
# 🔧 CẤU HÌNH HỆ THỐNG — OPENAI
# ==========================================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL_NAME = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
IMAGE_MODEL_NAME = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")

openai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    timeout=30.0,
    max_retries=0,  # We implement bounded async retries below.
) if OPENAI_API_KEY else None

BOT_START_TIME = time.time()
SESSION_TIMEOUT = 1800
MAX_HISTORY_PAIRS = 10

# Abuse/cost controls.
MAX_PROMPT_CHARS = 6000
MAX_OUTPUT_TOKENS = 2000
USER_COOLDOWN_SECONDS = 4.0
MAX_IMAGE_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_GENERATED_IMAGE_BYTES = 20 * 1024 * 1024
MAX_API_RETRIES = 3
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
ALLOWED_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}

# A bounded semaphore protects Render from a burst of simultaneous API work.
AI_CONCURRENCY = max(1, min(int(os.environ.get("AI_CONCURRENCY", "3")), 8))

# ==========================================
# 🎭 BỘ SƯU TẬP TÍNH CÁCH (MOOD PRESETS)
# ==========================================
EMOTION_PRESETS = {
    "mặc định": "Bạn là AI thông minh, tinh tế, am hiểu rộng và trả lời cực kỳ thân thiện.",
    "vui vẻ": "Bạn là AI siêu cấp đáng yêu, dùng nhiều emoji vui nhộn, luôn lạc quan và tràn đầy năng lượng.",
    "hài hước": "Bạn là AI cực kỳ hài hước, thích meme text, châm biếm nhẹ nhàng, trả lời mặn mòi.",
    "thả thính": "Bạn là AI nói chuyện ngọt ngào, quan tâm người dùng và giữ ranh giới phù hợp.",
    "nghiêm túc": "Bạn là chuyên gia cấp cao. Ngắn gọn, súc tích, đi thẳng vào vấn đề, phân tích logic.",
    "wibu": "Bạn là AI phong cách anime, có thể dùng từ kawaii, baka và biểu tượng UwU, OwO."
}

# Goi y lenh tieng Viet cho AI, kem chu thich de AI khong nham sai.
AI_COMMAND_HINT = (
    "\n[LENH TIENG VIET TRONG SERVER - dung so lieu tren de tra loi]\n"
    "- !taisan <@nguoi>  : xem tai san rong cua mot nguoi\n"
    "- !dep <so tien>    : gui tien vao ngan hang (huong lai kep)\n"
    "- !with <so tien>   : rut tien tu ngan hang ve vi\n"
    "- !pay <@nguoi> <so tien> : chuyen tien cho nguoi khac\n"
    "- !vaytien / !tranon  : vay va tra no\n"
    "- !thechap <cong ty> <so cp> : the chap co phieu de vay re hon\n"
    "- !chuoc            : chuoc tai san the chap\n"
    "- !market           : bang gia co phieu san\n"
    "- !muacp / !bancp   : mua / ban co phieu tren san\n"
    "- !laissuat         : bieu lai suat cong bo hom nay\n"
    "- !topmoney         : bang xep hang nguoi giu tien nhieu nhat\n"
    "- !kehoach / !xemkehoach <ma> / !chaykehoach <ma> : lo trinh nhieu buoc\n"
    "- !chaykehoach <ma> thu  : chay thu, khong dung tien\n"
    "- !huykehoach <ma>     : huy lo trinh\n"
    "- !datlich daily 9 0  : dat lich TU DONG (ngay/tuan/thang), bot tu chay\n"
    "- !danhsachlich      : xem cac lich tu dong\n"
    "- !huylich <so>       : huy lich tu dong\n"
    "- !duyety           : xem yeu cau giao dich AI dang cho ban xac nhan\n"
    "- !xacnhan <ma>     : xac nhan yeu cau do (chi dung 1 lan, co han)\n"
    "- !huyxacnhan       : huy yeu cau AI\n"
    "Don vi la VNĐ ảo trong server, khong phai tien that. "
    "AI KHONG BAO GIO tu chuyen tien hay giao dich thay nguoi dung. "
    "Neu nguoi dung muon thao tac tien, AI chi huong dan ho dung lenh, "
    "hoac tao yeu cau cho va bao nguoi dung go !xacnhan.\n"
    "AI KHONG BAO GIO tu chuyen tien. Khi co [KE_HOACH_SAN_SANG] hoac "
    "[KE_HOACH_BI_TU_CHOI] trong tin nhan, AI phai noi ro: go lenh nao de "
    "xem/duyet, va KHONG hua tien gi.\n"
)


#: Nguyen tac bat bien cho AI: KHONG duoc doan so lieu, phai dung so
#: lieu trong [DỮ LIỆU THỰC TẾ] / [BAN LA AI — THÔNG TIN THAT].
AI_GROUND_RULES = (
    "\n[QUY TAC SU THAT — BAT BUOC]\n"
    "1. KHONG BAO GIO tu doan so lieu. Chi tra loi bang SO LIỆU có trong "
    "[BAN LA AI — THÔNG TIN THAT], [TAI SAN...], [THONG KE CHUNG CUA SERVER], "
    "[LAI SUAT].\n"
    "2. Ve so server: dung DUNG con so 'Bot dang online tai N server'. "
    "Neu cau hoi ma thong tin do khong co, hay noi 'em chua co du lieu, "
    "go !stats de xem' — TUYET DOI KHONG duoc bao so khac.\n"
    "3. Ve tien: don vi la VNĐ ảo trong server, dung so lieu trong [TAI SAN]. "
    "Khong tinh toan lai, khong doan.\n"
    "4. Neu cau hoi nam ngoai he thong (vi du '1+1'), dung kien thuc chung.\n"
)


class CustomMoodModal(discord.ui.Modal, title='Tạo tính cách riêng cho Bot'):
    mood_desc = discord.ui.TextInput(
        label='Mô tả tính cách bạn muốn:',
        style=discord.TextStyle.paragraph,
        placeholder='Ví dụ: Hãy đóng vai một tên cướp biển say xỉn, luôn dùng từ "Arrr"...',
        required=True,
        max_length=1000
    )

    def __init__(self, cog, user_id):
        super().__init__()
        self.cog = cog
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        self.cog.set_user_mood(self.user_id, self.mood_desc.value)
        await interaction.response.send_message(f"✅ Đã áp dụng tính cách mới cho riêng bạn!\n**Mô tả:** {self.mood_desc.value}", ephemeral=True)

class MoodSelect(discord.ui.Select):
    def __init__(self, cog, user_id):
        self.cog = cog
        self.user_id = user_id
        options = [
            discord.SelectOption(label=mood.title(), description=desc[:50]+"...", value=mood, emoji="🎭")
            for mood, desc in EMOTION_PRESETS.items()
        ]
        options.append(discord.SelectOption(label="Tự Viết Tính Cách (Custom)", description="Tự tạo một tính cách độc nhất vô nhị", value="custom", emoji="✍️"))
        super().__init__(placeholder="Chọn tính cách cho Bot...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ Menu này không dành cho bạn!", ephemeral=True)

        selected_mood = self.values[0]
        if selected_mood == "custom":
            await interaction.response.send_modal(CustomMoodModal(self.cog, self.user_id))
        else:
            self.cog.set_user_mood(self.user_id, EMOTION_PRESETS[selected_mood])
            await interaction.response.send_message(f"✅ Đã chuyển đổi tính cách thành: **{selected_mood.upper()}** (Chỉ áp dụng với bạn).", ephemeral=True)

class MoodView(discord.ui.View):
    def __init__(self, cog, user_id):
        super().__init__(timeout=60)
        self.add_item(MoodSelect(cog, user_id))

# ==========================================
# 🧠 CORE AI - HỆ THỐNG ĐA LUỒNG
# ==========================================
class AIChat(commands.Cog):
    """KaiAI Neural Chat — AI đa luồng, bộ nhớ theo người dùng và UI Discord."""

# ------------------------------------------------------------------
    # Thao tac giao dich — LUON phai qua xac nhan
    # ------------------------------------------------------------------

    #: Cac thao tac AI duoc phep *de xuat*. Khi nguoi dung go ``!xacnhan
    #: <token>`` thi han moi chay (va chi chay mot lan).
    AI_ACTIONS = ("transfer", "deposit", "withdraw")

    def __init__(self, bot):
        self.bot = bot
        self.user_sessions = {}
        self.request_cooldowns = {}
        self.ai_semaphore = asyncio.Semaphore(AI_CONCURRENCY)
        #: Yeu cau cho cua AI (token ngau nhien, het han, dung mot lan).
        self.pending = PendingActions()
        #: Lo trinh nhieu buoc — nguoi dung duyet mot lan, bot tu chay.
        self.plans = PlanStore()
        #: Lich tu dong (SQLite) — song sot qua restart.
        self.schedules = ScheduleStore(
            str(CONFIG.data_dir / "ai_schedules.db"))
        self._scheduler_task = None
        self._cleanup_task = self._spawn_cleanup_task()

    # ------------------------------------------------------------------

    async def propose_action(
        self,
        *,
        user_id: int,
        action: str,
        args: dict,
    ) -> PendingAction | None:
        """Ghi nhan mot thao tac AI **de xuat** va tra ve phieu can xac nhan.

        Day KHONG phai la giao dich — chi la "keu goi". Tra ve ``None`` neu
        :attr:`AI_ACTIONS` khong chua hanh dong yeu cau.
        """
        key = str(action)
        if key not in self.AI_ACTIONS:
            log.warning("Bo qua hanh dong AI khong cho phep: %s", key)
            return None
        # SỞ HỮU (lớp 1/3): lệnh đề xuất phải nằm trên ví của **chính**
        # người đang yêu cầu — AI không thể soạn `transfer.sender` hay
        # `deposit.user_id` của người khác để qua mặt bước xác nhận.
        owner_error = step_owner_error(key, dict(args or {}), int(user_id))
        if owner_error is not None:
            log.warning(
                "Tu choi de xuat AI %s cho user=%s: %s",
                key, user_id, owner_error,
            )
            return None
        item = self.pending.create(user_id=int(user_id), action=key, args=args)
        log.info(
            "AI de xuat %s cho user=%s (token=%s)", key, user_id, item.token,
        )
        return item

    def _ai_runner(self, action: str):
        """Tra ve ham thuc thi that cua cog ``Games`` cho mot hanh dong."""
        games = self.bot.get_cog("Games")
        if games is None:
            return None
        tools = AITools(games)
        if not tools.is_ready():
            return None
        table = {
            "transfer": self._run_transfer,
            "deposit": self._run_deposit,
            "withdraw": self._run_withdraw,
        }
        return table.get(action)

    async def _run_transfer(self, sender: int, receiver: int, amount: int):
        tools = AITools(self.bot.get_cog("Games"))
        return await tools.do_transfer(sender, receiver, amount, confirm=True)

    async def _run_deposit(self, user_id: int, amount: int):
        tools = AITools(self.bot.get_cog("Games"))
        return await tools.do_deposit(user_id, amount, confirm=True)

    async def _run_withdraw(self, user_id: int, amount: int):
        tools = AITools(self.bot.get_cog("Games"))
        return await tools.do_withdraw(user_id, amount, confirm=True)

    # ------------------------------------------------------------------
    # Lich tu dong — bot tu chay vao co gi
    # ------------------------------------------------------------------

    async def _notify_schedule(self, user_id: int, ok: bool, report: str) -> None:
        """Gửi báo cáo kết quả lịch tự động cho chủ lịch."""
        user = self.bot.get_user(user_id)
        if user is None:
            return
        try:
            await user.send(report[:1900])
        except Exception as exc:  # noqa: BLE001 - không gửi được thì bỏ qua
            log.debug("Khong gui bao cao lich cho %s: %s", user_id, exc)

    @commands.command(name="datlich", aliases=["lichkehoach", "hove"])
    async def set_schedule(self, ctx, kind: str, hour: int = 9, minute: int = 0):
        """Đặt lịch chạy tự động: `!datlich daily 9 0` (ngày | tuần | tháng)."""
        if not scheduler_enabled():
            return await ctx.reply(
                "❌ Tính năng lịch tự động đang bị tắt (AI_SCHEDULER).")
        key = kind.strip().lower()
        alias = {"ngay": "daily", "tuan": "weekly", "thang": "monthly"}
        key = alias.get(key, key)
        plans = self.plans.list_for(ctx.author.id)
        ready = [p for p in plans if p.is_runnable]
        if not ready:
            return await ctx.reply(
                "🗺️ Bạn chưa có lộ trình nào chờ duyệt.\n"
                "Nhờ AI lập trước, rồi `!xemkehoach <mã>` để kiểm tra."
            )
        plan = ready[0]
        run = await self.schedules.create(
            user_id=ctx.author.id, kind=key, steps=[
                {"action": s.action, "args": dict(s.args)} for s in plan.steps
            ],
            title=plan.title, hour=int(hour), minute=int(minute),
        )
        if run is None:
            return await ctx.reply(f"❌ Không đặt được lịch: {self.schedules.last_error}")
        await ctx.reply(
            f"⏰ Đã đặt lịch **tự động** cho **{plan.title}**\n"
            f"• Chạy {run.when_text()}\n"
            f"• Mỗi lần: **{run.total:,} VNĐ**\n"
            "Từ giờ bạn **không cần** gõ lệnh nữa — bot tự chạy và báo kết quả.\n"
            "Hủy bằng `!huylich <số>`."
        )

    @commands.command(name="danhsachlich", aliases=["dslich"])
    async def list_schedules(self, ctx):
        """Xem các lịch tự động đang bật."""
        runs = await self.schedules.list_for(ctx.author.id)
        if not runs:
            return await ctx.reply("⏰ Bạn chưa đặt lịch tự động nào.")
        lines = [
            f"• **#{r.id}** — {r.title} • {r.when_text()} "
            f"• {r.total:,} VNĐ/lần".replace(",", ".")
            for r in runs
        ]
        await ctx.reply(
            "⏰ **Lịch tự động của bạn:**\n" + "\n".join(lines)[:1900])

    @commands.command(name="huylich", aliases=["huylichkehoach"])
    async def cancel_schedule(self, ctx, schedule_id: int):
        """Hủy một lịch tự động theo số thứ tự (`!danhsachlich`)."""
        if await self.schedules.cancel(
                user_id=ctx.author.id, schedule_id=int(schedule_id)):
            return await ctx.reply("🚫 Đã hủy lịch tự động.")
        await ctx.reply("❌ Không có lịch đó (xem `!danhsachlich`).")

    @commands.command(name="chaylich", aliases=["chayngay"])
    async def run_schedule_now(self, ctx, schedule_id: int):
        """Chạy ngay một lịch tự động (bỏ qua lịch, không đổi lịch)."""
        outcome = await run_one_schedule(
            self.schedules, self._plan_runner,
            user_id=ctx.author.id, schedule_id=int(schedule_id),
        )
        if outcome is None:
            # Sai mã, không phải chủ lịch, lịch đã tắt hoặc steps hỏng.
            return await ctx.reply(
                "❌ Không có lịch đó hoặc chưa chạy được "
                "(xem `!danhsachlich`)."
            )
        ok, report = outcome
        icon = "🏃" if ok else "⚠️"
        await ctx.reply(
            f"{icon} Đã chạy thử lịch **#{int(schedule_id)}** "
            "(lịch tự động **không đổi**).\n" + report[-1500:]
        )

    # ------------------------------------------------------------------
    # Lo trinh tu dong (AI plan)
    # ------------------------------------------------------------------

    async def propose_plan(
        self,
        *,
        user_id: int,
        title: str,
        steps: list[dict],
    ):
        """AI dung lo trinh moi va tra ve ``Plan`` (hoac ``None``).

        Chi **de xuat** — chua chay gi. Nguoi dung phai xem
        (``!xemkehoach``) roi duyet (``!chaykehoach``).
        """
        return self.plans.create(user_id=int(user_id), title=title, steps=steps)

    async def _plan_runner(self, action: str, **kwargs):
        """Ham thuc thi that cua cog ``Games`` cho mot buoc cua lo trinh."""
        tools = AITools(self.bot.get_cog("Games"))
        if action == "transfer":
            return await tools.do_transfer(
                kwargs["sender"], kwargs["receiver"],
                kwargs["amount"], confirm=True,
            )
        if action == "deposit":
            return await tools.do_deposit(
                kwargs["user_id"], kwargs["amount"], confirm=True)
        if action == "withdraw":
            return await tools.do_withdraw(
                kwargs["user_id"], kwargs["amount"], confirm=True)
        return None

    @commands.command(name="kehoach", aliases=["lochinhh", "roadmap"])
    async def list_plans(self, ctx):
        """Xem các lộ trình AI đang chờ bạn duyệt."""
        plans = self.plans.list_for(ctx.author.id)
        if not plans:
            return await ctx.reply("🗺️ Bạn chưa có lộ trình nào.")
        lines = []
        for p in plans:
            done = sum(1 for s in p.steps if s.status == STEP_DONE)
            lines.append(
                f"• `{p.token}` — **{p.title}** "
                f"({done}/{len(p.steps)} bước • {p.total:,} VNĐ)".replace(",", ".")
            )
        await ctx.reply("🗺️ **Lộ trình của bạn:**\n" + "\n".join(lines)[:1900])

    @commands.command(name="xemkehoach", aliases=["xemlo", "chitiet"])
    async def show_plan(self, ctx, token: str):
        """Xem chi tiết một lộ trình (chưa chạy gì)."""
        plan = self.plans.get(token.strip(), user_id=ctx.author.id)
        if plan is None:
            return await ctx.reply(
                "❌ Không tìm thấy lộ trình (sai mã, hết hạn, "
                "hoặc không phải của bạn)."
            )
        await ctx.reply(plan.render()[:1900])

    @commands.command(name="chaykehoach", aliases=["duyetlo", "chaylo"])
    async def run_plan(self, ctx, token: str, *, dry_run: bool = False):
        """Duyệt và chạy lộ trình. Thêm `thu` để xem trước mà không chạy."""
        plan = self.plans.get(token.strip(), user_id=ctx.author.id)
        if plan is None:
            return await ctx.reply(
                "❌ Không tìm thấy lộ trình (sai mã, hết hạn, "
                "hoặc không phải của bạn)."
            )
        if not dry_run and self.bot.get_cog("Games") is None:
            return await ctx.reply("❌ Hệ thống kinh tế chưa sẵn sàng.")
        ok, report = await execute_plan(
            self.plans, user_id=ctx.author.id, token=token.strip(),
            runner=self._plan_runner, dry_run=bool(dry_run),
        )
        report = report if ok else f"❌ {report}"
        await ctx.reply(report[:1900])

    @commands.command(name="huykehoach", aliases=["huylo", "xoaykehoach"])
    async def cancel_plan(self, ctx, token: str):
        """Hủy một lộ trình (chỉ khi còn chờ duyệt)."""
        if self.plans.cancel(user_id=ctx.author.id, token=token.strip()):
            return await ctx.reply("🚫 Đã hủy lộ trình.")
        await ctx.reply(
            "❌ Không hủy được — lộ trình không tồn tại, không phải của bạn, "
            "hoặc đã chạy xong."
        )

    # ------------------------------------------------------------------
    # !xacnhan / !huyxacnhan / !duyety
    # ------------------------------------------------------------------

    @commands.command(name="duyety", aliases=["yeucau", "cho"])
    async def list_pending(self, ctx):
        """Xem các yêu cầu giao dịch của AI đang chờ bạn xác nhận."""
        items = self.pending.list_for(ctx.author.id)
        if not items:
            return await ctx.reply("✅ Bạn không có yêu cầu nào đang chờ.")
        lines = [
            f"• `{it.token}` — **{it.describe()}** "
            f"(còn {max(0, int(it.expires_at - time.time()))}s)"
            for it in items
        ]
        await ctx.reply(
            f"⏳ **Yêu cầu đang chờ xác nhận:**\n" + "\n".join(lines)[:1900]
        )

    @commands.command(name="xacnhan", aliases=["dongy", "ok", "confirm"])
    async def confirm_action(self, ctx, token: str):
        """Xác nhận một yêu cầu giao dịch của AI (dùng 1 lần, có hạn)."""
        token = token.strip()
        runner = self._ai_runner(
            (self.pending.get(token, user_id=ctx.author.id) or
             PendingAction(token=token, user_id=ctx.author.id, action="")).action
        )
        if runner is None:
            return await ctx.reply(
                "❌ Hệ thống kinh tế chưa sẵn sàng, không thể xác nhận."
            )
        ok, msg = await run_confirmed(
            self.pending,
            user_id=ctx.author.id,
            token=token,
            runner=runner,
        )
        prefix = "✅" if ok else "❌"
        await ctx.reply(f"{prefix} {msg}")

    @commands.command(name="huyxacnhan", aliases=["huyyeucau", "tuchoai"])
    async def cancel_action(self, ctx, token: str = None):
        """Hủy một yêu cầu, hoặc hủy tất cả nếu bỏ trống token."""
        if token:
            if self.pending.cancel(user_id=ctx.author.id, token=token.strip()):
                return await ctx.reply("🚫 Đã hủy yêu cầu đó.")
            return await ctx.reply(
                "❌ Không tìm thấy yêu cầu (sai token, hết hạn, "
                "hoặc không phải của bạn)."
            )
        n = self.pending.cancel_all(ctx.author.id)
        await ctx.reply(f"🚫 Đã hủy **{n}** yêu cầu của bạn.")

    async def cog_load(self):
        """Dựng :class:`~kai_core.ai.router.AIRouter` — OpenAI trước, Gemini dự phòng.

        Router thay lớp retry thủ công: có fallback giữa provider, circuit breaker,
        giới hạn đồng thời và hạn mức token. Mọi lệnh cũ giữ nguyên vì
        :meth:`_openai_generate` giữ nguyên chữ ký.
        """
        self.router = build_router_from_env(CONFIG)
        self.bot.ai_router = self.router
        # Vong lap lich tu dong (SQLite) — bot tu chay cac lich da duyet.
        if scheduler_enabled():
            try:
                asyncio.get_running_loop()
                self._scheduler_task = asyncio.get_running_loop().create_task(
                    scheduler_loop(
                        self.schedules, self._plan_runner,
                        notifier=self._notify_schedule,
                    ))
                log.info("Da bat vong lap lich tu dong.")
            except Exception as exc:  # noqa: BLE001
                log.warning("Khong bat duoc vong lap lich: %s", exc)
        else:
            log.info("Lich tu dong dang TAT (AI_SCHEDULER).")
        log.info("AI router: %s", self.router.describe())
        if not self.router.available_providers():
            log.warning(
                "Chưa cấu hình OPENAI_API_KEY/GEMINI_API_KEY — lệnh AI sẽ báo lỗi thân thiện."
            )

    def _spawn_cleanup_task(self):
        """Khởi động task dọn bộ nhớ mà không phụ thuộc ``bot.loop`` (deprecated)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - chỉ xảy ra ngoài event loop
            loop = getattr(self.bot, "loop", None)
        if loop is None:
            return None
        return loop.create_task(self.memory_cleanup_task())

    async def cog_unload(self):
        """Huỷ task nền khi cog bị gỡ để tránh rò rỉ tác vụ."""
        task = getattr(self, "_cleanup_task", None)
        if task is not None and not task.done():
            task.cancel()
        sched = getattr(self, "_scheduler_task", None)
        if sched is not None and not sched.done():
            sched.cancel()
        router = getattr(self, "router", None)
        if router is not None:
            await router.close()

    def get_uptime(self):
        delta = int(time.time() - BOT_START_TIME)
        return f"{delta // 3600}h {(delta % 3600) // 60}m {delta % 60}s"

    def set_user_mood(self, user_id, mood_prompt):
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {"history": [], "mood": "", "last_active": time.time()}
        self.user_sessions[user_id]["mood"] = mood_prompt
        self.user_sessions[user_id]["history"] = []

    async def memory_cleanup_task(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            current_time = time.time()
            users_to_delete = []
            for user_id, session in self.user_sessions.items():
                if current_time - session["last_active"] > SESSION_TIMEOUT:
                    users_to_delete.append(user_id)

            for uid in users_to_delete:
                del self.user_sessions[uid]

            await asyncio.sleep(600)

    async def get_dynamic_instruction(self, message: discord.Message) -> str:
        tz = pytz.timezone('Asia/Ho_Chi_Minh')
        current_time = datetime.datetime.now(tz).strftime("%H:%M:%S - %d/%m/%Y")

        guild_name = message.guild.name if message.guild else "Tin nhắn riêng (DM)"
        user_name = message.author.display_name

        base_prompt = (
            "Bạn là một trợ lý AI thông minh, tinh tế và xịn xò trên nền tảng Discord. "
            "Bạn được tạo ra bởi KaithenorzDoris (Chỉ nhắc đến khi được hỏi). "
            "Quy tắc tuyệt đối: Nói chuyện tự nhiên, ngắn gọn, không spam code block trừ khi được yêu cầu. "
            "[TẠO ẢNH]: Nếu người dùng yêu cầu 'vẽ', 'tạo ảnh', bắt buộc bắt đầu câu trả lời bằng: "
            "`[GEN_IMG]: <câu lệnh tiếng Anh miêu tả ảnh chi tiết>` và thêm 1 câu xác nhận bên dưới."
        )

        system_context = (
            f"\n\n[DỮ LIỆU THỰC TẾ TRONG HỆ THỐNG]\n"
            f"- Thời gian: {current_time}\n"
            f"- Máy chủ: {guild_name}\n"
            f"- Đang trò chuyện với: {user_name}\n"
            f"- Bot đang online tại {len(getattr(self.bot, 'guilds', []) or [])} server"
            f" (tổng ~{sum(int(getattr(g, 'member_count', 0) or 0) for g in (getattr(self.bot, 'guilds', []) or []))} người)."
            f" Khi ai hỏi 'mày ở mấy server', trả lời ĐÚNG con số này,"
            f" KHÔNG được bịa số khác.\n"
        )

        # Nhúng tài sản kinh tế của người dùng để AI trả lời đúng số liệu ví/bank/CP/nợ.
        system_context += await self._build_asset_context(message)

        session = self.user_sessions.get(message.author.id, {})
        custom_mood = session.get("mood", EMOTION_PRESETS["mặc định"])

        return (f"{base_prompt}\n\n[TÍNH CÁCH BẠN PHẢI NHẬP VAI NGAY BÂY GIỜ]: "
                f"{custom_mood}{AI_GROUND_RULES}{system_context}")

    async def _build_asset_context(self, message: discord.Message) -> str:
        """Khoi ``[TAI SAN]`` + ``[LENGH]`` cho AI.

        Dung ``AITools`` de noi AI voi he thong: AI biet so lieu chinh xac cua
        nguoi hoi (va ca server), nhung **chi doc** -- giao dich tien that van
        phai qua lenh cua nguoi dung hoac xac nhan tuyet doi.

        Loi bat ky o cog nao cung chi ghi debug, khong lam sap luong chat.
        """
        try:
            games = self.bot.get_cog("Games")
            if games is None:
                return ""
            tools = AITools(games)

            # 1) Tai san cua chinh nguoi hoi.
            own = await tools.look_up_user(message.author.id)
            parts = []
            if own.ok:
                parts.append(
                    "\n[TAI SAN CUA NGUOI DUNG DANG CHAT]\n" + own.message
                )

            # 2) Thong ke chung cua server.
            stats = await tools.server_stats()
            if stats.ok:
                parts.append("\n[THONG KE CHUNG CUA SERVER]\n" + stats.message)

            # 2b) Ban la ai / dang o dau (so server THAT tu Discord).
            ident = await tools.bot_identity()
            if ident.ok:
                parts.append("\n[BAN LA AI — THONG TIN THAT]\n" + ident.message)

            # 3) Biểu lãi suất hôm nay.
            rates = await tools.interest_rates()
            if rates.ok:
                parts.append("\n" + rates.message)

            return "".join(parts) + "\n" + AI_COMMAND_HINT
        except Exception as exc:  # noqa: BLE001 - phong ho, khong chan chat
            log.debug("Khong lay duoc du lieu he thong cho AI: %s", exc)
            return ""

    async def send_split_reply(self, message: discord.Message, text: str, file: discord.File = None, embed: discord.Embed = None):
        if not text:
            return await message.reply(file=file, embed=embed)

        chunks = []
        while len(text) > 1900:
            split_idx = text.rfind('\n', 0, 1900)
            if split_idx == -1:
                split_idx = text.rfind(' ', 0, 1900)
            if split_idx == -1:
                split_idx = 1900

            chunks.append(text[:split_idx])
            text = text[split_idx:].lstrip()

        if text:
            chunks.append(text)

        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            if i == 0:
                await message.reply(chunk, file=file if is_last else None, embed=embed if is_last else None)
            else:
                await message.channel.send(chunk, file=file if is_last else None, embed=embed if is_last else None)

    @commands.command(name="menu", aliases=["helpai"])
    async def ai_menu_command(self, ctx):
        embed = discord.Embed(title="🤖 KAI AI — NEURAL CHAT", color=discord.Color.blurple())
        embed.description = "Bộ não nhân tạo được nâng cấp với khả năng nhớ từng người dùng độc lập!"
        embed.add_field(name="💬 Cách Giao Tiếp", value="• `ai <câu hỏi>`\n• Tag `@Bot <câu hỏi>`\n• Reply tin nhắn của bot", inline=False)
        embed.add_field(name="🎨 Tính Năng", value="• Vẽ ảnh siêu đẹp (Flux AI)\n• Ghi nhớ hội thoại riêng biệt\n• Đổi tính cách bằng Menu UI", inline=False)
        embed.add_field(name="🛠️ Lệnh Hỗ Trợ", value="`!mood` - Đổi tính cách\n`!clearai` - Xóa trí nhớ của bạn\n`!history` - Xem trí nhớ bot về bạn\n`!stats` - Xem thông số hệ thống", inline=False)
        embed.set_footer(text="Dev: KaithenorzDoris ⚡ Vận hành mượt mà")
        await ctx.reply(embed=embed)

    @commands.command(name="mood", aliases=["tinhcach"])
    async def set_mood_ui(self, ctx):
        embed = discord.Embed(
            title="🎭 TRUNG TÂM NHÂN CÁCH AI",
            description="Hãy chọn cách mà bạn muốn Bot nói chuyện với bạn.\n*(Lưu ý: Tính cách này chỉ áp dụng cho riêng bạn, không ảnh hưởng người khác)*",
            color=discord.Color.magenta()
        )
        view = MoodView(self, ctx.author.id)
        await ctx.reply(embed=embed, view=view)

    @commands.command(name="clearai", aliases=["forget"])
    async def clear_memory(self, ctx):
        if ctx.author.id in self.user_sessions:
            self.user_sessions[ctx.author.id]["history"] = []
            await ctx.reply("🧹 **Ký ức của tôi về bạn đã được xóa sạch.** Bắt đầu lại từ đầu nhé!")
        else:
            await ctx.reply("Tôi có nhớ gì về bạn đâu mà xóa? 😅")

    @commands.command(name="stats", aliases=["thongke"])
    async def show_stats(self, ctx):
        ram_usage = psutil.virtual_memory()
        active_sessions = len(self.user_sessions)

        embed = discord.Embed(title="📊 THỐNG KÊ HỆ THỐNG", color=discord.Color.green())
        embed.add_field(name="🌐 Phủ sóng", value=f"Servers: **{len(self.bot.guilds)}**\nUsers: **{sum(g.member_count for g in self.bot.guilds)}**", inline=True)
        embed.add_field(name="⚡ Hiệu suất", value=f"Ping: **{round(self.bot.latency * 1000)}ms**\nUptime: **{self.get_uptime()}**", inline=True)
        embed.add_field(name="🧠 AI Data", value=f"Phiên đang chat: **{active_sessions} user**\nRAM: **{ram_usage.percent}%**", inline=False)
        await ctx.reply(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message):
        """Secure Discord -> OpenAI Responses API bridge.

        Security controls:
        - per-user cooldown + bounded global concurrency
        - hard input/attachment limits
        - Discord-hosted attachments only
        - bounded HTTP downloads and strict image MIME allow-list
        - bounded conversation memory
        - exponential retry for transient OpenAI failures
        - no provider/API secrets in logs or user-facing errors
        """
        if message.author.bot or message.content.startswith(('!', '/', '?')):
            return

        is_pinged = self.bot.user in message.mentions
        starts_with_ai = message.content.lower().startswith("ai ")
        is_reply_to_bot = (
            bool(message.reference)
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author == self.bot.user
        )

        if not (
            is_pinged
            or starts_with_ai
            or is_reply_to_bot
            or isinstance(message.channel, discord.DMChannel)
        ):
            return

        if openai_client is None:
            return await message.reply(
                "⚠️ AI chưa được cấu hình. Admin cần đặt `OPENAI_API_KEY` trên Render."
            )

        # Hard limit prevents oversized Discord messages from becoming expensive API calls.
        raw_prompt = message.content
        prompt = raw_prompt.replace(
            f'<@{self.bot.user.id}>', ''
        ).replace(
            f'<@!{self.bot.user.id}>', ''
        ).strip()
        if starts_with_ai:
            prompt = prompt[3:].strip()

        if len(prompt) > MAX_PROMPT_CHARS:
            return await message.reply(
                f"⚠️ Tin nhắn AI quá dài. Giới hạn hiện tại là {MAX_PROMPT_CHARS:,} ký tự."
            )

        user_id = message.author.id
        now = time.monotonic()
        last_request = self.request_cooldowns.get(user_id, 0.0)
        if now - last_request < USER_COOLDOWN_SECONDS:
            remaining = USER_COOLDOWN_SECONDS - (now - last_request)
            return await message.reply(
                f"⏳ Bạn đang gửi quá nhanh. Thử lại sau **{remaining:.1f}s**.",
                mention_author=False
            )
        self.request_cooldowns[user_id] = now

        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {
                "history": [],
                "mood": EMOTION_PRESETS["mặc định"],
                "last_active": time.time(),
            }

        session = self.user_sessions[user_id]
        session["last_active"] = time.time()

        # OpenAI Responses input format.
        current_content = []
        if prompt:
            current_content.append({"type": "input_text", "text": prompt})

        # Only fetch image attachments from Discord CDN URLs. No arbitrary URLs supplied by users.
        image_count = 0
        for att in message.attachments:
            if image_count >= MAX_IMAGE_ATTACHMENTS:
                break

            content_type = (att.content_type or "").split(";")[0].lower()
            if content_type not in ALLOWED_IMAGE_MIMES:
                continue
            if att.size > MAX_ATTACHMENT_BYTES:
                await message.reply(
                    f"⚠️ Ảnh `{att.filename}` quá lớn. Giới hạn là "
                    f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB/ảnh.",
                    mention_author=False
                )
                return

            try:
                # Discord attachment URLs are HTTPS CDN URLs. aiohttp follows redirects;
                # final response is still size-checked before it enters the model request.
                timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
                async with aiohttp.ClientSession(timeout=timeout) as http_sess:
                    async with http_sess.get(
                        att.url,
                        allow_redirects=True,
                        headers={"User-Agent": "KaiAI/6.0 Discord bot"}
                    ) as resp:
                        if resp.status != 200:
                            continue
                        if resp.content_length and resp.content_length > MAX_ATTACHMENT_BYTES:
                            return await message.reply(
                                f"⚠️ Ảnh `{att.filename}` vượt giới hạn kích thước.",
                                mention_author=False
                            )

                        img_bytes = await resp.content.read(MAX_ATTACHMENT_BYTES + 1)
                        if len(img_bytes) > MAX_ATTACHMENT_BYTES:
                            return await message.reply(
                                f"⚠️ Ảnh `{att.filename}` vượt giới hạn kích thước.",
                                mention_author=False
                            )

                        # Keep the MIME type from Discord metadata; do not trust a user-provided URL.
                        data_url = (
                            f"data:{content_type};base64,"
                            f"{base64.b64encode(img_bytes).decode('ascii')}"
                        )
                        current_content.append({
                            "type": "input_image",
                            "image_url": data_url,
                            "detail": "auto",
                        })
                        image_count += 1
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue

        # Phát hiện ý định giao dịch → dự kất lộ trình (offline).
        # AI CHỉ địn x dự kất lộ trích chế; nười dùng phải
        # tự duyệt bằng !chaykehoach.
        try:
            draft = draft_plan_from_text(
                prompt or "",
                user_id=user_id,
                mentioned=tuple(
                    int(m.id) for m in getattr(message, "mentions", [])
                ),
            )
        except Exception:  # noqa: BLE001 - phan tich loi thi bo qua,
            draft = None   # khong duoc lam hong cuoc tro chuyen.
        if draft is not None:
            title, steps = draft
            plan = await self.propose_plan(
                user_id=user_id, title=title, steps=steps)
            if plan is None:
                note = (
                    f"[KE_HOACH_BI_TU_CHOI] {self.plans.last_error} "
                    "Hãy gời âi bề rõ hơng kiển để hƣt cǡ."
                )
            else:
                note = (
                    f"[KE_HOACH_SAN_SANG] token={plan.token}\n"
                    f"{plan.render()}\n"
                    "Bào cào người dùng gái `!xemkehoach "
                    f"{plan.token}` để xem và `!chaykehoach {plan.token}` "
                    "để duyệt. TUYỆT ĐỐI không tự "
                    "chuyển tiền."
                )
            current_content.append({"type": "input_text", "text": note})

        if not current_content:
            current_content.append({
                "type": "input_text",
                "text": "Hãy trả lời yêu cầu của người dùng."
            })

        # Keep only bounded, plain text history. Image data is never retained in RAM history.
        history = session["history"]
        history.append({"role": "user", "content": prompt or "Hãy phân tích ảnh tôi gửi."})
        if len(history) > MAX_HISTORY_PAIRS * 2:
            del history[:-MAX_HISTORY_PAIRS * 2]

        # Build request history from prior turns + current multimodal input.
        prior_history = history[:-1]
        input_messages = list(prior_history)
        input_messages.append({"role": "user", "content": current_content})

        async with self.ai_semaphore:
            async with message.channel.typing():
                try:
                    response = await self._openai_generate(
                        input_messages=input_messages,
                        system_instruction=await self.get_dynamic_instruction(message),
                        user_id=message.author.id,
                        guild_id=message.guild.id if message.guild else 0,
                    )
                except Exception as exc:
                    # Chi tiết exception chỉ ghi vào log server; không lộ dữ liệu provider/API ra chat.
                    log.error("OpenAI request failed: %s", exc, exc_info=exc)
                    # Remove the just-added user turn if the request failed.
                    if history and history[-1].get("role") == "user":
                        history.pop()
                    return await message.reply(
                        "🧠 AI đang bận hoặc tạm thời không khả dụng. "
                        "Bạn thử lại sau một chút nhé.",
                        mention_author=False
                    )

        reply_text = (response or "").strip()
        if not reply_text:
            reply_text = "Mình chưa tạo được câu trả lời lần này. Bạn thử diễn đạt lại nhé! 🧠"

        # Store only text output, never base64 images.
        history.append({"role": "assistant", "content": reply_text})
        if len(history) > MAX_HISTORY_PAIRS * 2:
            del history[:-MAX_HISTORY_PAIRS * 2]

        if "[GEN_IMG]:" in reply_text:
            await self._handle_image_request(message, reply_text)
        else:
            await self.send_split_reply(message, reply_text)

    @staticmethod
    def _flatten_content(content) -> tuple[str, bool]:
        """Rút phần **chữ** khỏi content OpenAI (chuỗi hoặc danh sách đa phương tiện).

        Trả về ``(text, has_image)``. Provider dự phòng (Gemini REST) không nhận
        ảnh dạng ``image_url`` của OpenAI, nên phần chữ vẫn được gửi đi và bot vẫn
        trả lời được thay vì báo lỗi.
        """
        if isinstance(content, str):
            return content, False
        if isinstance(content, list):
            parts, has_image = [], False
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in {"text", "input_text"} and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") in {"image_url", "input_image"}:
                    has_image = True
            return "\n".join(p for p in parts if p.strip()), has_image
        return "", False

    async def _openai_generate(
        self,
        input_messages,
        system_instruction,
        user_id: int = 0,
        guild_id: int = 0,
    ):
        """Gọi AI qua :class:`AIRouter` (OpenAI → Gemini dự phòng).

        Giữ nguyên chữ ký cũ để **không** phải sửa call-site nào. Router đã lo
        retry có backoff, circuit breaker, giới hạn đồng thời và hạn mức token;
        ``AIRouter.complete`` không bao giờ ném lỗi ra ngoài nên ta tự chuyển
        thành ``RuntimeError`` để khối ``except`` sẵn có ở caller xử lý.
        """
        router = getattr(self, "router", None)
        if router is None:
            return await self._legacy_openai_generate(input_messages, system_instruction)

        history = []
        has_image = False
        for message in input_messages[:-1]:
            text, img = self._flatten_content(message.get("content"))
            has_image = has_image or img
            history.append(ChatMessage(role=message.get("role", "user"), content=text))
        prompt, has_image_last = self._flatten_content(input_messages[-1].get("content"))
        has_image = has_image or has_image_last

        request = AIRequest(
            prompt=prompt,
            system=system_instruction,
            history=history,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            user_id=user_id,
            guild_id=guild_id,
            kind="chat",
            metadata={"has_image": has_image},
        )
        result = await router.complete(request)
        if not result.ok or not (result.text or "").strip():
            raise RuntimeError(result.error or "AI không phản hồi.")

        self._record_ai_usage(user_id, guild_id, result, has_image)
        return result.text

    def _record_ai_usage(self, user_id, guild_id, result, has_image: bool) -> None:
        """Ghi **metadata** AI vào Log Center (nếu bật) để tra cứu chi phí/lỗi (#17).

        Chỉ ghi số liệu — tuyệt đối không lưu nội dung hội thoại (riêng tư #21).
        """
        if result.fallback_used:
            log.info(
                "AI fallback sang %s sau khi provider chính lỗi (%.0fms)",
                result.provider,
                result.latency * 1000,
            )
        store = getattr(getattr(self.bot, "log_center", None), "store", None)
        if store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - không có loop đang chạy
            return
        loop.create_task(
            store.log_ai(
                user_id=user_id,
                guild_id=guild_id or None,
                provider=result.provider,
                model=result.model,
                surface="chat",
                latency_ms=int(result.latency * 1000),
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                success=result.ok,
                error="" if result.ok else result.error,
            )
        )

    async def _legacy_openai_generate(self, input_messages, system_instruction):
        """Đường gọi OpenAI cũ — chỉ dùng khi router chưa dựng (ví dụ unit test)."""
        last_error = None

        for attempt in range(MAX_API_RETRIES + 1):
            try:
                response = await openai_client.responses.create(
                    model=MODEL_NAME,
                    instructions=system_instruction,
                    input=input_messages,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                )
                return response.output_text
            except openai.RateLimitError as exc:
                last_error = exc
            except openai.APIStatusError as exc:
                # Retry only transient provider failures.
                if getattr(exc, "status_code", None) not in TRANSIENT_STATUS_CODES:
                    raise
                last_error = exc
            except (openai.APITimeoutError, openai.APIConnectionError) as exc:
                last_error = exc

            if attempt < MAX_API_RETRIES:
                delay = min(2 ** attempt + (0.15 * attempt), 8.0)
                await asyncio.sleep(delay)

        raise last_error or RuntimeError("OpenAI request failed")

    async def _handle_image_request(self, message, reply_text):
        """Generate images through OpenAI instead of a third-party image proxy."""
        try:
            parts = reply_text.split("[GEN_IMG]:", 1)
            clean_reply_text = parts[0].strip()
            img_prompt = parts[1].strip().split("\n")[0].strip()

            # Prevent prompt abuse / accidental huge requests.
            img_prompt = img_prompt[:2000]
            if not img_prompt:
                return await self.send_split_reply(message, clean_reply_text)

            response = await openai_client.images.generate(
                model=IMAGE_MODEL_NAME,
                prompt=img_prompt,
                size="1024x1024",
            )

            if not response.data:
                return await self.send_split_reply(
                    message,
                    f"{clean_reply_text}\n\n⚠️ Dịch vụ tạo ảnh chưa trả về ảnh."
                )

            item = response.data[0]
            b64_json = getattr(item, "b64_json", None)
            if not b64_json:
                return await self.send_split_reply(
                    message,
                    f"{clean_reply_text}\n\n⚠️ Dịch vụ tạo ảnh chưa trả về dữ liệu ảnh."
                )

            img_data = base64.b64decode(b64_json, validate=True)
            if len(img_data) > MAX_GENERATED_IMAGE_BYTES:
                raise RuntimeError("Generated image exceeds Discord upload safety limit")

            file = discord.File(io.BytesIO(img_data), filename="kai_ai.png")
            embed = discord.Embed(color=discord.Color.gold())
            embed.set_image(url="attachment://kai_ai.png")
            embed.set_footer(text=f"🎨 OpenAI Image • {img_prompt[:80]}")
            await self.send_split_reply(message, clean_reply_text, file=file, embed=embed)

        except Exception as exc:
            log.error("Image generation failed: %s", exc, exc_info=exc)
            await self.send_split_reply(
                message,
                "⚠️ Mình chưa tạo được ảnh lúc này. Bạn thử lại sau nhé."
            )

async def setup(bot):
    await bot.add_cog(AIChat(bot))

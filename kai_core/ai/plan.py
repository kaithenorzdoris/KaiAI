"""
Lộ trình tự động của AI (AI plans).

Ý tưởng
-------
Thay vì bắt người dùng xác nhận **từng** giao dịch, người dùng có thể nhờ
AI lập một **lộ trình** nhiều bước, duyệt **một lần**, rồi bot tự chạy
từng bước theo đúng thứ tự đã duyệt.

Ví dụ người dùng nói::

    "AI ơi, mỗi tháng gửi 2 triệu vào ngân hàng và chuyển 500k cho bạn
     tôi vào ngày 1 hàng tháng."

AI dựng kế hoạch 2 bước::

    [1] deposit  2.000.000  → ngân hàng
    [2] transfer 500.000   → <@123>

Người dùng xem trước (``!xemkehoach``), duyệt (``!chaykehoach``), bot
chạy từng bước và báo cáo từng kết quả.

Chốt an toàn
------------
Đây là phần **quan trọng nhất** — AI có thể tự chạy nhiều giao dịch nên
phải chặt hơn hẳn so với một lệnh đơn:

* **Danh sách trắng hành động** — chỉ chạy hành động trong
  :data:`ALLOWED_ACTIONS`; tên lạ bị từ chối ngay.
* **Mọi tham số phải là số nguyên dương** — chặn nhập rác/âm/NaN.
* **Người dùng là chủ sở hữu** — không ai chạy được kế hoạch của người khác.
* **Duyệt một lần, chạy một lần** — kế hoạch đã chạy thì không chạy lại
  được; muốn chạy phải tạo kế hoạch mới (chống chạy trùng trừ tiền).
* **Hạn mức** — số bước/kế hoạch, số kế hoạch/người, và tổng tiền tối đa
  trong một lần chạy.
* **Dừng ngay khi hỏng** — mặc định bước lỗi thì dừng, không lặp tiếp
  (tránh trừ tiền hết).
* **Chạy thử (dry-run)** — xem trước tác động mà không đụng tiền.
* **Nhật ký từng bước** — để đối chiếu sau, mỗi bước chỉ chạy đúng 1 lần.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger

log = get_logger("ai.plan")

#: Chi nhung hanh dong duoc phep trong ke hoach. Moi hanh dong deu duoc nho
#: lai bang token ngan de cong doi chieu.
ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"transfer", "deposit", "withdraw"}
)

#: Toi da so buoc trong mot ke hoach.
MAX_STEPS_PER_PLAN = 10
#: Toi da so ke hoach chua xu ly cua mot nguoi.
MAX_PLANS_PER_USER = 5
#: Tong tien lon nhat mot lan chay (chong "chuyen het tong tai san").
MAX_TOTAL_PER_RUN = 500_000_000

#: Trang thai cua mot buoc.
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

#: Trang thai cua ca ke hoach.
PLAN_PENDING = "pending"
PLAN_RUNNING = "running"
PLAN_DONE = "done"
PLAN_FAILED = "failed"
PLAN_CANCELLED = "cancelled"


def _clean_int(value: Any) -> int | None:
    """Chuyen gia tri thanh so nguyen duong, ``None`` neu khong hop le."""
    if isinstance(value, bool):
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


#: Khóa **sở hữu** trên mỗi hành động: khóa nào trong ``args`` phải mang
#: id của chủ kế hoạch / chủ lịch / người đề xuất.
OWNER_KEYS: dict[str, str] = {
    "transfer": "sender",
    "deposit": "user_id",
    "withdraw": "user_id",
}


def step_owner_error(
    action: str, args: dict[str, Any], owner_id: int,
) -> str | None:
    """Kiểm tra một bước có **thuộc về** ``owner_id`` không.

    Đây là lớp chốt chặn **dùng chung** cho cả ba nơi:

    1. ``AIChat.propose_action`` — AI không thể soạn lệnh rút tiền của
       người khác trước khi hỏi xác nhận.
    2. ``PlanStore.create`` — lộ trình đã duyệt chỉ được đụng ví chủ kế hoạch.
    3. ``scheduler`` — lịch cũ bị sửa ngoài luồng cũng không chạy được.

    Trả ``None`` nếu hợp lệ, ngược lại là thông báo lỗi tiếng Việt.
    """
    key = OWNER_KEYS.get(str(action))
    if key is None:
        return f"Hành động không được phép: `{action}`."
    try:
        uid = int((args or {}).get(key))
    except (TypeError, ValueError):
        return f"Bước `{action}` thiếu `{key}` hợp lệ."
    if uid != int(owner_id):
        return f"Bước `{action}` phải thuộc về bạn."
    return None

@dataclass(slots=True)
class PlanStep:
    """Mot buoc trong lo trinh."""

    action: str
    args: dict[str, Any] = field(default_factory=dict)
    status: str = STEP_PENDING
    message: str = ""
    ran_at: float = 0.0

    @property
    def amount(self) -> int:
        """So tien buoc nay can (0 neu khong phai giao dich tien)."""
        return _clean_int(self.args.get("amount")) or 0

    def describe(self) -> str:
        """Dong mo ta ngan de nguoi dung doc truoc khi duyet."""
        marks = {
            STEP_PENDING: "⏳", STEP_RUNNING: "▶️", STEP_DONE: "✅",
            STEP_FAILED: "❌", STEP_SKIPPED: "⏭️",
        }
        head = marks.get(self.status, "•")
        who = self.args.get("receiver") or self.args.get("user_id")
        tail = f" → <@{who}>" if self.args.get("action") == "transfer" else ""
        return f"{head} **{self.action}** {self.amount:,}{tail}".replace(",", ".")


@dataclass(slots=True)
class Plan:
    """Mot lo trinh da duoc AI de xuat, nguoi dung se duyet."""

    token: str
    user_id: int
    title: str
    steps: list[PlanStep] = field(default_factory=list)
    status: str = PLAN_PENDING
    created_at: float = 0.0
    executed_at: float = 0.0

    @property
    def total(self) -> int:
        """Tong so tien ke hoach se chay."""
        return sum(s.amount for s in self.steps)

    @property
    def is_runnable(self) -> bool:
        """Ke hoach co chay duoc khong (da duyet va con buoc cho)."""
        return self.status == PLAN_PENDING and any(
            s.status == STEP_PENDING for s in self.steps)

    def render(self) -> str:
        """Bao cao trang thai day du de nguoi dung xem."""
        head = (
            f"🗺️ **Lộ trình: {self.title}**\n"
            f"Mã: `{self.token}` • Tổng: **{self.total:,} VNĐ**".replace(",", ".")
        )
        body = "\n".join(
            f"{i + 1}. {s.describe()}" for i, s in enumerate(self.steps))
        return f"{head}\n{body}"

class PlanStore:
    """Kho lo trinh cua AI (mon trong RAM, moi restart la sach).

    Keo hao: ke hoach chua chay chi ton tai vai phut cho nguoi dung quyet
    dinh; sau do phai de xuat lai.
    """

    def __init__(
        self,
        *,
        max_steps: int = MAX_STEPS_PER_PLAN,
        max_per_user: int = MAX_PLANS_PER_USER,
        max_total: int = MAX_TOTAL_PER_RUN,
        ttl: float = 900.0,
    ) -> None:
        self.max_steps = int(max_steps)
        self.max_per_user = int(max_per_user)
        self.max_total = int(max_total)
        self.ttl = float(ttl)
        self._plans: dict[str, Plan] = {}

    # ------------------------------------------------------------------

    def create(
        self,
        *,
        user_id: int,
        title: str,
        steps: list[dict[str, Any]],
    ) -> Plan | None:
        """Dung lo trinh moi tu danh sach buoc tho.

        Tra ``None`` neu buoc khong hop le — ly do nam trong ``last_error``
        (de cog hien thi cho nguoi dung ma khong phai do lai logic).
        """
        self.last_error = ""
        if not isinstance(steps, list) or not steps:
            self.last_error = "Lộ trình phải có ít nhất 1 bước."
            return None
        if len(steps) > self.max_steps:
            self.last_error = (
                f"Lộ trình tối đa {self.max_steps} bước (nhận {len(steps)}).")
            return None

        built: list[PlanStep] = []
        total = 0
        for raw in steps:
            if not isinstance(raw, dict):
                self.last_error = "Mỗi bước phải là một object."
                return None
            action = str(raw.get("action", "")).strip().lower()
            if action not in ALLOWED_ACTIONS:
                self.last_error = f"Hành động không được phép: `{action}`."
                log.warning("AI plan bi tu choi hanh dong: %s", action)
                return None
            args = dict(raw.get("args") or {})
            amount = _clean_int(args.get("amount"))
            if amount is None:
                self.last_error = f"Bước `{action}` thiếu số tiền hợp lệ."
                return None
            if action == "transfer":
                for key in ("sender", "receiver"):
                    uid = _clean_int(args.get(key))
                    if uid is None:
                        self.last_error = (
                            f"Bước `transfer` thiếu `{key}` hợp lệ.")
                        return None
                    args[key] = uid
                if args["sender"] == args["receiver"]:
                    self.last_error = "Không thể chuyển tiền cho chính mình."
                    return None
            else:
                uid = _clean_int(args.get("user_id"))
                if uid is None:
                    self.last_error = f"Bước `{action}` thiếu `user_id` hợp lệ."
                    return None
                args["user_id"] = uid
            args["amount"] = amount
            # SỞ HỮU (lớp 2/3): bước phải thuộc về chủ kế hoạch — AI
            # không thể soạn lệnh đụng vào ví của người khác.
            owner_error = step_owner_error(action, args, user_id)
            if owner_error is not None:
                self.last_error = owner_error
                log.warning("AI plan tu choi so huu: %s", owner_error)
                return None
            total += amount
            built.append(PlanStep(action=action, args=args))

        if total > self.max_total:
            self.last_error = (
                f"Tổng {total:,} VNĐ vượt trần {self.max_total:,} VNĐ."
            ).replace(",", ".")
            return None

        self.purge()
        self._drop_oldest(int(user_id))
        plan = Plan(
            token=secrets.token_urlsafe(9),
            user_id=int(user_id),
            title=str(title)[:120] or "Kế hoạch của AI",
            steps=built,
            created_at=time.time(),
        )
        self._plans[plan.token] = plan
        log.info(
            "AI tao lo trinh %s cho user=%s (%d buoc, tong %d)",
            plan.token, user_id, len(built), total,
        )
        return plan

# ------------------------------------------------------------------

    def get(self, token: str, *, user_id: int | None = None) -> Plan | None:
        """Lấy kế hoạch theo mã (chỉ chủ kế hoạch được thấy)."""
        plan = self._plans.get(str(token))
        if plan is None:
            return None
        if user_id is not None and plan.user_id != int(user_id):
            return None
        return plan

    def list_for(self, user_id: int) -> list[Plan]:
        """Danh sách kế hoạch của một người (mới nhất trước)."""
        self.purge()
        mine = [p for p in self._plans.values() if p.user_id == int(user_id)]
        mine.sort(key=lambda p: p.created_at, reverse=True)
        return mine

    def cancel(self, *, user_id: int, token: str) -> bool:
        """Hủy kế hoạch chưa chạy. ``True`` nếu thực sự hủy được."""
        plan = self.get(token, user_id=user_id)
        if plan is None or plan.status != PLAN_PENDING:
            return False
        plan.status = PLAN_CANCELLED
        return True

    def purge(self) -> int:
        """Xoá kế hoạch quá hạn (chưa chạy) và kế hoạch đã xong/lỗi."""
        now = time.time()
        dead = []
        for token, plan in self._plans.items():
            stale = now - plan.created_at > self.ttl
            if plan.status in (PLAN_DONE, PLAN_FAILED, PLAN_CANCELLED):
                dead.append(token)
            elif plan.status == PLAN_PENDING and stale:
                dead.append(token)
        for token in dead:
            self._plans.pop(token, None)
        return len(dead)

    def _drop_oldest(self, user_id: int) -> None:
        """Giữ tối đa ``max_per_user`` kế hoạch chưa chạy cho mỗi người."""
        mine = [p for p in self._plans.values()
                if p.user_id == user_id and p.status == PLAN_PENDING]
        overflow = len(mine) - self.max_per_user + 1
        if overflow <= 0:
            return
        mine.sort(key=lambda p: p.created_at)
        for plan in mine[:overflow]:
            self._plans.pop(plan.token, None)

    @property
    def size(self) -> int:
        """Số kế hoạch đang lưu."""
        return len(self._plans)

# ------------------------------------------------------------------
# Phan tich y doinh (offline — khong can goi LLM)
# ------------------------------------------------------------------

#: Cac tu khoa bao nguoi dung dang muon thao tac tien.
INTENT_WORDS = (
    "chuyen", "chuyển", "gui", "gửi", "nap", "nạp", "rut", "rút",
    "chuyen tien", "gửi tiền", "transfer", "deposit", "withdraw",
)

#: Cac tu khoa bao nguoi dung muon AI *lap ke hoach* (nhieu buoc).
PLAN_WORDS = ("ke hoach", "kế hoach", "lộ trình", "lo trinh", "roadmap",
              "hang thang", "hàng tháng", "moi thang", "mỗi tháng",
              "tu dong", "tự động", "hang ngay", "hàng ngày")


def parse_money(text: str) -> int | None:
    """Đọc số tiền trong câu tiếng Việt.

    Chấp nhận ``500000``, ``500.000``, ``500k``, ``2 triệu``, ``1,5 tr``,
    ``2 trieu``. Trả ``None`` nếu không có số hợp lệ.
    """
    t = text.lower().replace("₫", "").replace("vnd", "").replace("đ", "")
    # "1,5 triệu" -> "1.5 triệu" trước bỏ dấu chấm nghìn.
    t = re.sub(r"(?<=\d),(?=\d)", ".", t)
    # Đẹc "sá âm" đẹủc khềp định: âm thà số khác.
    if re.search(r"(?<![\wđ])-\s*\d", t):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    number = m.group(1)
    # "500.000" la dau cham NGHIN, khong phai thap phan (float("500.000")=500.0).
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", number):
        number = number.replace(".", "")
    # Hệ số đi kèm ngay sau con số: 20k -> 20.000, 1.5tr -> 1.500.000
    suffix = t[m.end():m.end() + 4]
    if re.match(r"\s*(k\b|t\b)", suffix) and not re.match(r"\s*tr\s*\w", suffix):
        mult = 1_000
    elif re.search(r"triệu|trieu", t) or re.match(r"\s*tr\b", suffix):
        mult = 1_000_000
    elif re.search(r"nghìn|nghin", t) or re.match(r"\s*ng\b", suffix):
        mult = 1_000
    else:
        mult = 1
    value = int(float(number) * mult)
    return value if value > 0 else None


def mentions_action(text: str) -> bool:
    """Câu có nhắc tới thao tác tiền không."""
    t = text.lower()
    return any(w in t for w in INTENT_WORDS)


def mentions_plan(text: str) -> bool:
    """Câu có yêu cầu lập kế hoạch (nhiều bước/lặp lại) không."""
    t = text.lower()
    return any(w in t for w in PLAN_WORDS)


#: Tu noi giua cac y dinh trong mot cau ("gửi 2tr **va** chuyển 500k ...").
CLAUSE_SPLIT = re.compile(
    # Phai khop ca bien CO dau ("va") va KHONG dau ("va"): nguoi dung tieng
    # Viet co dau thi khong tach duoc neu chi khop bien khong dau.
    r"(?:,|;|\.|\n|"
    r"\bv\u00e0\b|\bv\u1ea1y\b|\bva\b|\bvay\b|"
    r"\br\u1ed3i\b|\broi\b|\bsau \u0111\u00f3\b|\bsau do\b|"
    r"\bthen\b|\bcu\u1ed1i c\u00f9ng\b|\bcuoi cung\b|\bti\u1ebfp\b)+",
    re.IGNORECASE,
)


def split_clauses(text: str) -> list[str]:
    """Tach cau thanh nhieu y dinh nho (``va``, ``roi``, ``sau do``, dau phay...)."""
    parts = [p.strip(" ,;.") for p in CLAUSE_SPLIT.split(text or "")]
    return [p for p in parts if p]


def draft_plan_from_text(
    text: str,
    *,
    user_id: int,
    mentioned: tuple[int, ...] = (),
) -> tuple[str, list[dict[str, Any]]] | None:
    """Dung *du khat* lo trinh tu cau chat cua nguoi dung.

    Cau co the chua **nhieu** y dinh ("gửi 2tr vao ngan hang va chuyen 500k
    cho <@42>") — moi y dinh thanh mot buoc, giu nguyen thu tu.

    Chi doc cau, khong bao gio tao ke hoach — viec tao lai do
    :meth:`PlanStore.create` quyet dinh (va no moi chan duoc dung).

    Tra ``(tieu_de, buoc)`` hoac ``None`` neu cau khong phai thao tac tien.
    """
    targets = [int(u) for u in mentioned if int(u) != int(user_id)]
    steps: list[dict[str, Any]] = []
    titles: list[str] = []

    for clause in split_clauses(text):
        amount = parse_money(clause)
        if amount is None or not mentions_action(clause):
            continue
        low = clause.lower()
        if re.search(r"\brut|\brút|\bwithdraw", low):
            steps.append({"action": "withdraw",
                          "args": {"user_id": int(user_id), "amount": amount}})
            titles.append(f"Rút {amount:,}".replace(",", "."))
            continue
        wants_person = bool(re.search(r"cho |cho\b|toi |tôi |den |đến ", low))
        if targets and wants_person:
            steps.append({"action": "transfer",
                          "args": {"sender": int(user_id),
                                   "receiver": targets[0], "amount": amount}})
            titles.append(f"Chuyển {amount:,}".replace(",", "."))
            continue
        # Mac dinh: nap tien vao ngan hang (an toan hon rut/chuyen).
        steps.append({"action": "deposit",
                      "args": {"user_id": int(user_id), "amount": amount}})
        titles.append(f"Gửi {amount:,}".replace(",", "."))

    if not steps:
        return None
    title = " + ".join(titles) if titles else "Kế hoạch của AI"
    return (title[:120], steps)


async def execute_plan(
    store: PlanStore,
    *,
    user_id: int,
    token: str,
    runner,
    dry_run: bool = False,
    stop_on_error: bool = True,
) -> tuple[bool, str]:
    """Chay mot lo trinh theo thu tu da duyet.

    ``runner`` la ham thuc thi that cua cog (vi du ``games.transfer``), nhan
    ``**kwargs`` cua tung buoc. Neu ``dry_run`` thi chi kiem tra co the chay
    khong, **khong** dung tien.

    Tra ve ``(ok, bao_cao)``. ``ok=False`` nghia ca it nhat mot buoc that
    bai (hoac khong chay duoc).
    """
    plan = store.get(token, user_id=user_id)
    if plan is None:
        return False, "Không tìm thấy lộ trình (sai mã, hết hạn, hoặc không phải của bạn)."
    if plan.status != PLAN_PENDING:
        return False, "Lộ trình này không ở trạng thái chờ chạy."

    if dry_run:
        lines = [f"{i + 1}. {s.describe()}" for i, s in enumerate(plan.steps)]
        return True, (
            "🧪 **Chạy thử (không đụng tiền):**\n" + "\n".join(lines)
            + f"\nTổng: **{plan.total:,} VNĐ**".replace(",", ".")
        )

    # Khoá "đã chạy" TRƯỚC khi gọi tiền: nếu tiến trình chết giữa chừng
    # thì kế hoạch không bị chạy lại lần nữa (tránh trừ hai lần).
    plan.status = PLAN_RUNNING
    plan.executed_at = time.time()

    results: list[str] = []
    all_ok = True
    for idx, step in enumerate(plan.steps, 1):
        if step.status != STEP_PENDING:
            continue
        step.status = STEP_RUNNING
        try:
            outcome = runner(step.action, **step.args)
            if hasattr(outcome, "__await__"):
                outcome = await outcome
        except Exception as exc:  # noqa: BLE001 - lỗi 1 bước không được
            # làm hỏng cả lộ trình; chỉ dừng phần còn lại.
            log.warning("Ke hoach %s buoc %d loi: %s", token, idx, exc)
            step.status = STEP_FAILED
            step.message = str(exc)[:120]
            results.append(f"{idx}. ❌ `{step.action}` — {exc}")
            all_ok = False
            if stop_on_error:
                for rest in plan.steps[idx:]:
                    if rest.status == STEP_PENDING:
                        rest.status = STEP_SKIPPED
                break
            continue
        failed = getattr(outcome, "ok", True) is False
        step.status = STEP_FAILED if failed else STEP_DONE
        step.ran_at = time.time()
        step.message = str(getattr(outcome, "message", "") or outcome)[:120]
        mark = "❌" if failed else "✅"
        results.append(f"{idx}. {mark} `{step.action}` — {step.message}")
        if failed:
            all_ok = False
            if stop_on_error:
                for rest in plan.steps[idx:]:
                    if rest.status == STEP_PENDING:
                        rest.status = STEP_SKIPPED
                break

    plan.status = PLAN_DONE if all_ok else PLAN_FAILED
    header = "✅" if all_ok else "⚠️"
    tail = "Dừng tại bước lỗi." if not all_ok else "Hoàn tất."
    log.info("Ke hoach %s ket thuc: ok=%s", token, all_ok)
    return all_ok, f"{header} **{plan.title}**\n" + "\n".join(results) + f"\n_{tail}_"



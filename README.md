# KaiAI

**KaiAI** là Discord bot đa năng viết bằng `discord.py`: AI chat, kinh tế, game, quản trị server,
nhật ký & tình cảm. Dự án được nâng cấp lên **v6.2.0** với package lõi `kai_core/`,
entrypoint `main.py` gọn gàng và bộ kiểm tra chất lượng mã nguồn chạy được trong CI.

## Tình trạng migration (2026)

KaiAI hiện đang ở trạng thái `Python app + Rust migration scaffold`, không phải một rewrite hoàn toàn sang Rust.
Mục tiêu hiện nay là giữ behavior hiện tại nguyên vẹn, đồng thời xây dựng nền tảng Rust cho các module cốt lõi theo lộ trình:

- `config` → đã có Rust triển khai cơ bản
- `database` → repository và migration layer đã có test
- `economy` → logic atomic transfer và idempotency cache đã có test
- `AI safety` → routing và prompt validation đã có policy gate
- `web` → router / endpoint và header security layer đã có test
- `Discord runtime` → Gateway + slash-command sync thật, registry, handler dùng SQLite và kiểm tra context/quyền đã có

Rust runtime hiện đã kết nối Discord Gateway, đồng bộ slash command theo cấu hình opt-in và tiếp nhận prefix command cho nhóm business. Các prefix toàn cục lấy từ `COMMAND_PREFIXES`; prefix cấu hình riêng theo guild (qua lệnh admin) sẽ được ưu tiên. Tính năng này cần bật privileged **Message Content Intent** trong Discord Developer Portal. Ngoài diagnostics, economy balance, profile và admin prefix, Rust đã có các lệnh chính của `business.py` cùng các thao tác công ty `ctytao`, `nganhnghe`, `chonnganh`, `tuyennv`, `baotri`, `nhanviencty`, `bannpc`, `chuonhhdt`, `bautich`, `ctytongquan`, `lamvieccty`; repository SQLite xử lý tạo công ty, nhân sự, mua lại cổ phiếu NPC, bầu chủ tịch, hợp đồng, thâu tóm, cổ phiếu ưu đãi, IPO/định giá, khoản vay, ESOP, ký quỹ đấu giá, phá sản và báo cáo theo transaction. Bộ dispatch Rust nhận diện các alias khai báo trong `business.py` và chuyển về handler tên chuẩn; slash command vẫn chỉ đồng bộ tên chuẩn, còn prefix dispatch hiện chỉ hỗ trợ nhóm business. Với các lệnh có nhiều tên công ty dễ nhập nhằng, có thể phân tách trường bằng `|`. `lamvieccty` tự ghi nhận lãi vay trong hạn/quá hạn và thanh toán từ quỹ sau hoạt động kinh doanh, tối đa một lần mỗi ngày chạy. Các ngày bị bỏ lỡ được tính bù khi khoản vay được xử lý tiếp theo (chạy chu kỳ, trả nợ hoặc vay lại); đây vẫn là xử lý theo lệnh, chưa phải lịch chạy nền. Với khoản vay cũ, hệ thống không tính lãi hồi tố cho khoảng thời gian không thể xác định lịch sử đã tính lãi, nhưng điều chỉnh thời hạn còn lại theo ngày mở khoản vay nếu có log. Giá vốn tạo công ty và biến động doanh thu được quyết định ổn định theo ngày, không khớp từng giá trị với PRNG của Python. Chưa có provider inference AI hoặc transport Python↔Rust.

Đây chưa phải migration hoàn chỉnh: Rust đang dùng `kai.sqlite3`, khác `economy.db` của Python; chưa có nhập/chia sẻ dữ liệu, lịch chạy chu kỳ công ty tự động, alias parity, hay parity cho các cog còn lại. Python vẫn là production runtime cho tới khi dữ liệu, hành vi và cutover được kiểm thử đầy đủ.

Trạng thái hợp lệ hiện tại:
- Python test suite đang pass
- Rust workspace đang build và test pass
- Rust migration scaffold là công cụ chuẩn bị cho port có kiểm chứng, không phải bản thay thế production ngay lập tức

---

## Tính năng

| Nhóm | Cog | Mô tả |
| --- | --- | --- |
| AI | `ai_chat` | Chat với AI, sinh ảnh (OpenAI), bộ nhớ theo phiên |
| Quản trị | `admin`, `system` | Warn/ban/lock, `sysinfo`, `reload`, `shutdown`, `ping`, `status` |
| Dữ liệu | `database` | Hồ sơ quân nhân, Roblox lookup, bảng SQLite |
| Nhật ký | `logs`, `logs_center` | Spy message, sửa/xóa tin nhắn, audit log, voice log, trung tâm tra cứu |
| Chẩn đoán | `diagnostics` | Tra lỗi kèm traceback (owner), audit prefix/slash, parity, health |
| Tiện ích | `utility_pro` | Thông tin user/server/role, `EmbedBuilder`, nhắc việc, AFK |
| Game & Kinh tế | `games`, `kai_premium` | Lệnh game, kinh tế, welcome, AutoMod, analytics |
| Tình cảm | `love` | Daily/care/date, roleplay |

Chi tiết lệnh: [`docs/COMMANDS.md`](docs/COMMANDS.md).

## Website KaiAI

Khi `ENABLE_KEEP_ALIVE=true`, trang giới thiệu KaiAI chạy cùng health server tại
`http://localhost:8080/`. Trang tự lấy danh sách lệnh, trạng thái bot, danh sách
server và bảng xếp hạng kinh tế từ bot/database đang chạy. Chủ bot hiển thị trên
website là **KaithenorzDoris**.

Trang có phòng chat chung; mọi khách có thể xem và gửi tin bằng biệt danh,
không cần đăng nhập. Chat giới hạn 500 ký tự, 5 tin mỗi 15 giây theo trình duyệt
(và 60 tin/phút theo địa chỉ client trong mỗi process); nội dung được lưu trong `community_chat.db`
ở `KAI_DATA_DIR`, tối đa 10.000 tin gần nhất. Biệt danh do khách tự chọn, không
được xác minh danh tính.
Nút chat có thể kéo tới vị trí khác và thay đổi kích thước, vị trí được lưu trên
thiết bị hiện tại.
Dashboard hiển thị thống kê bot live (số server, thành viên, Gateway latency và
uptime), cho tìm kiếm/phân trang toàn bộ danh sách server, cùng bảng xếp hạng
kinh tế và thành tích game. KaiAI Guide hỗ trợ câu hỏi tự do qua OpenAI khi đã cấu hình
`WEBSITE_ASSISTANT_API_KEY` (hoặc tái sử dụng `OPENAI_API_KEY`); nếu chưa bật,
website dùng bộ hướng dẫn nhanh có sẵn. Mỗi câu hỏi AI gửi chỉ chứa câu hiện tại
cùng dữ liệu tổng quan công khai; không gửi lịch sử chat, và được giới hạn
token/nhịp gọi.

Website không có tính năng đăng nhập/tạo tài khoản; các endpoint OAuth cũ trả về
404. Chat công khai có bảo vệ CSRF và giới hạn tốc độ; khi triển khai nhiều worker,
nên đặt `DASHBOARD_SESSION_SECRET` cố định để cookie phiên khách dùng ổn định.

Nút mời website và lệnh `!invite` dùng link Discord của bot với các quyền cơ bản
để xem/gửi tin nhắn, dùng thread, đọc lịch sử, embed, file và reaction. Link
không xin Administrator; lệnh kiểm duyệt, quản lý role hoặc voice cần cấp thêm
quyền tương ứng trong Discord.

---

## Cấu trúc dự án

```
KaiAI-main/
├── main.py              # Entry point: KaiAI Bot, phát hiện & nạp Cog
├── keep_alive.py        # Health server (Flask + waitress) cho Render/Railway/Replit
├── web/                 # Website giới thiệu, dashboard và assets
├── kai_core/            # Package lõi dùng chung (không chứa Cog)
│   ├── config.py        #   Đọc/validate biến môi trường → BotConfig
│   ├── logging_setup.py #   Log console màu + file xoay vòng (kai.log)
│   ├── database.py      #   Helper SQLite async (WAL, retry, transaction)
│   ├── utils.py         #   Tiện ích text/số/lệnh thuần (không side-effect)
│   └── ui.py            #   View/Modal dùng chung (ConfirmView, Paginator)
│   ├── errors.py        #   Lỗi an toàn: friendly message, redact secret, ErrorRecorder
│   ├── pagination.py    #   Phân trang embed + cắt message dài
│   ├── parity.py        #   Tự sinh slash mirror cho lệnh prefix
│   ├── version.py       #   Single source of truth cho version (6.2.0)
│   ├── ratelimit.py     #   Giới hạn tần suất dùng chung
│   ├── idempotency.py   #   Chống chạy lệnh trùng (double-click / retry)
│   ├── migrations.py    #   Migration SQLite có version
│   ├── logstore.py      #   Kho log/moderation dùng chung
│   ├── economy.py       #   Engine kinh tế nguyên tử (SQLite)
│   └── security.py      #   Chặn input nguy hiểm, phát hiện secret
├── cogs/                # 11 Cog chính
├── tests/               # pytest / unittest
├── scripts/validate.py  # Kiểm tra cú pháp, hợp đồng Cog, trùng lệnh…
├── scripts/make_manifest.py  # Sinh SOURCE_MANIFEST.json (sha256 từng file)
└── docs/                # Tài liệu
```

---

## Yêu cầu

- Python **3.10+** (đã kiểm chứng trên 3.12)
- Discord token + 2 intent đặc quyền: **Message Content** và **Server Members**
  (Developer Portal → Bot → Privileged Gateway Intents)

---

## Cài đặt nhanh

```bash
git clone <repo-url>
cd KaiAI-main

python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows
.venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env      # Windows: copy .env.example .env
# mở .env và điền DISCORD_TOKEN

python main.py
```

---

## Cấu hình

Toàn bộ cấu hình nằm trong **`.env`** (xem `.env.example`):

| Biến | Bắt buộc | Mặc định | Ý nghĩa |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | ✅ | – | Token bot |
| `BOT_OWNER_ID` | – | – | ID owner bổ sung; nếu trống, Discord application owner được nhận diện tự động |
| `COMMAND_PREFIXES` | – | `!,.,?` | Prefix lệnh text, cách nhau dấu phẩy |
| `OPENAI_API_KEY` | – | trống | Bật tính năng AI; bỏ trống nếu không dùng |
| `OPENAI_MODEL` / `OPENAI_IMAGE_MODEL` | – | `gpt-4o-mini` / `gpt-image-1` | Model AI |
| `WEBSITE_ASSISTANT_API_KEY` | – | trống | Key riêng cho AI website; nếu trống, Guide dùng `OPENAI_API_KEY` |
| `WEBSITE_ASSISTANT_MODEL` | – | `gpt-4o-mini` | Model OpenAI dùng cho website Guide |
| `GEMINI_API_KEY` | – | trống | Gemini — provider dự phòng khi OpenAI lỗi |
| `GEMINI_MODEL` | – | `gemini-3.5-flash-lite` | Model Gemini dùng cho fallback |
| `KAI_DATA_DIR` | – | `.` | Thư mục chứa `*.db` và `kai.log` |
| `LOG_LEVEL` | – | `INFO` | `DEBUG`…`CRITICAL` |
| `ENABLE_KEEP_ALIVE` | – | `true` | Bật health server |
| `PORT` | – | `8080` | Cổng health server |
| `DASHBOARD_SESSION_SECRET` | – | tự sinh | Khoá ký cookie phiên khách; đặt cố định nếu chạy nhiều worker |
| `DASHBOARD_COOKIE_SECURE` | – | `false` | Đặt `true` khi website dùng HTTPS |
| `SYNC_GUILD_ID` | – | `0` | Sync slash command cho 1 guild (0 = toàn cục) |
| `KAI_BACKUP_URL` | – | trống | Endpoint R2: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com/<BUCKET>`. **Cần cho Render** |
| `KAI_BACKUP_ACCESS_KEY_ID` | – | trống | Access Key ID của R2 |
| `KAI_BACKUP_SECRET_ACCESS_KEY` | – | trống | Secret Access Key của R2 |
| `KAI_BACKUP_PREFIX` | – | `kaiai` | Tên khoá trên kho lưu trữ |
| `KAI_BACKUP_INTERVAL_MIN` | – | `30` | Chu kỳ backup (phút) |

Không hard-code token trong source — `.env` đã được đưa vào `.gitignore`.

---

## Kiểm tra chất lượng mã nguồn

```bash
# Cú pháp + hợp đồng Cog + trùng tên lệnh + except trống
python scripts/validate.py

# Bộ unit test
python -m pytest tests/ -q
# hoặc không cần pytest
python -m unittest discover -s tests -v

# Lint (tuỳ chọn)
ruff check .

# Cập nhật danh mục file nguồn (sha256) sau khi sửa code
python scripts/make_manifest.py
python scripts/make_manifest.py --check   # đối chiếu, không ghi
```

---

## Docker

```bash
docker build -t kaiai .
docker run --env-file .env -p 8080:8080 -v kaiai-data:/app/data kaiai
```

Hoặc `docker compose up -d`. Image chạy bằng user không phải root và có `HEALTHCHECK`
trỏ tới `http://127.0.0.1:8080/health`.

---

## Exit code của `main.py`

| Mã | Ý nghĩa |
| --- | --- |
| `0` | Tắt sạch sẽ (hoặc Ctrl+C) |
| `1` | Thiếu `DISCORD_TOKEN` |
| `2` | Token không hợp lệ |
| `3` | Thiếu privileged intents |
| `4` | Lỗi khởi động ngoài dự kiến |

---

## Tài liệu khác

- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — triển khai Render / Railway / Replit / Docker
- [`docs/COMMANDS.md`](docs/COMMANDS.md) — danh mục lệnh
- [`docs/SECURITY.md`](docs/SECURITY.md) — quy tắc bảo mật
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — lịch sử thay đổi
- [`docs/UPGRADE_V5.md`](docs/UPGRADE_V5.md) — nâng cấp từ bản cũ

Bản sao lưu mã nguồn trước nâng cấp nằm trong `_backup_pre_v5/` (đã gitignore).

---

## License

MIT — xem [`LICENSE`](LICENSE).

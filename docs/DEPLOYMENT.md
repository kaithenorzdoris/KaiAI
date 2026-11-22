# Deployment Guide

## Local
1. Tạo virtual environment.
2. `pip install -r requirements.txt`
3. Copy `.env.example` thành `.env` nếu muốn dùng dotenv ở môi trường triển khai.
4. Đặt secrets trong Environment Variables.
5. `python main.py`.

## Replit / Render / Railway
- Start command: `python main.py`.
- `keep_alive.py` phục vụ website KaiAI ở `/`, dữ liệu live ở `/api/*` và
  health checks tại `/health`, `/healthz`, `/ready`, `/version`.
- Website không có đăng nhập/tạo tài khoản; các endpoint OAuth cũ trả về 404.
  Người dùng chat bằng biệt danh khách (2–24 ký tự), nội dung tối đa 500 ký tự,
  giới hạn 5 tin mỗi 15 giây theo phiên trình duyệt. Chat công khai nên cần
  theo dõi spam/moderation và bật HTTPS khi triển khai.
- Đặt `DASHBOARD_SESSION_SECRET` ổn định khi chạy nhiều worker để cookie phiên
  khách và CSRF hoạt động nhất quán. Không đưa session secret vào mã nguồn.
- Chat thêm giới hạn theo địa chỉ client (60 tin/phút/process) bên cạnh giới
  hạn 5 tin/15 giây theo phiên. Nếu chạy nhiều worker, các giới hạn trong bộ
  nhớ được áp dụng riêng cho từng worker.
- Phòng chat dùng chung được lưu trong `community_chat.db` dưới `KAI_DATA_DIR`;
  cần persistent disk/volume nếu muốn giữ tin nhắn sau khi redeploy. Chat giới
  hạn 500 ký tự/tin, tốc độ gửi và lưu tối đa 10.000 tin gần nhất.
- Bảng xếp hạng game đọc tổng hợp `game_stats` chỉ đọc từ `economy.db`; cần giữ
  database này trên persistent volume để bảo toàn thành tích sau khi deploy.
- Thông báo cập nhật toàn bot dùng `!setupdatechannel [#kênh]` (quản trị viên
  server có quyền Manage Server) để chọn kênh nhận tin; nếu chưa chọn, bot dùng
  Discord System Channel khi có thể gửi. Lệnh `!clearupdatechannel` đặt lại lựa
  chọn này. Owner của ứng dụng bot mới dùng được `!broadcastupdate <nội dung>`;
  lệnh yêu cầu xác nhận và có cooldown 5 phút. Thêm `--ping` sau lệnh nếu cần
  ping `@everyone`; các server thiếu quyền Mention Everyone sẽ bị bỏ qua và
  được báo trong kết quả. Cấu hình lưu ở `system_settings.db` trong `KAI_DATA_DIR`,
  vì vậy cần persistent storage để giữ lựa chọn kênh qua redeploy.
- Website có thể tìm kiếm/phân trang toàn bộ server bot đang ở, đồng thời cập
  nhật định kỳ số thành viên, độ trễ Gateway, uptime và bảng xếp hạng. Link mời
  mặc định chỉ xin quyền xem/gửi tin nhắn, thread, lịch sử, embed, file và
  reaction; không cấp Administrator. Một số lệnh moderation, role và voice cần
  cấp thêm permission cụ thể qua Discord.
- KaiAI Guide dùng OpenAI Responses API khi đặt `WEBSITE_ASSISTANT_API_KEY`
  (hoặc `OPENAI_API_KEY`); đặt `WEBSITE_ASSISTANT_MODEL` nếu muốn chọn model.
  Khi thiếu key, Guide tự chuyển về câu trả lời nhanh có sẵn. API giới hạn câu
  hỏi 500 ký tự, tối đa 5 lượt/phút cho mỗi browser session, 100 lượt/10 phút
  trên mỗi process, tối đa 280 output tokens/lượt, timeout 12 giây và tắt lưu
  Responses. Câu hỏi chỉ được gửi tới OpenAI khi AI đã bật; không gửi lịch sử
  chat. Chạy nhiều worker sẽ áp dụng giới hạn theo từng process.
- Bật `DASHBOARD_COOKIE_SECURE=true` khi website chạy HTTPS.
- Không hard-code token trong source.
- Không cần đưa Discord ID owner vào source: nếu `BOT_OWNER_ID`/`BOT_OWNER_IDS`
  để trống, bot dùng owner của Discord application; chỉ khai báo biến môi
  trường khi cần thêm owner tin cậy.
- Persistent disk nên được bật nếu muốn SQLite tồn tại sau restart/deploy.

### ⚠️ Vì sao deploy lại mất dữ liệu

Render (và Replit) dùng **ephemeral filesystem**: mỗi lần deploy, container
cũ bị xoá và container mới khởi động với ổ trống. Free tier **không** hỗ trợ
Persistent Disk, nên mọi file `.db`/`.json` trong `KAI_DATA_DIR` biến mất
sạch — kể cả tiền của người chơi, level, công ty.

Hai cách chữa:

| Cách | Chi phí | Ghi chú |
|---|---|---|
| **A. Persistent Disk** | ~$0,15/GB/tháng | Gắn ổ vào `/app/data` trong Dashboard. Không sửa code. Tắt zero-downtime deploy. |
| **B. Auto-backup ra ngoài** | Miễn phí | Bot tự tải dữ liệu lên R2 và tự kéo về lúc khởi động. Xem bên dưới. |

### Backup dữ liệu (cách B)

Code đã có sẵn trong `kai_core/backup.py`. Chỉ cần cấu hình biến môi trường:

```bash
KAI_BACKUP_URL=https://<ACCOUNT_ID>.r2.cloudflarestorage.com/<BUCKET_NAME>
KAI_BACKUP_ACCESS_KEY_ID=<Access Key ID>
KAI_BACKUP_SECRET_ACCESS_KEY=<Secret Access Key>
KAI_BACKUP_REGION=              # bỏ trống = tự nhận diện (R2 → auto)
KAI_BACKUP_PREFIX=kaiai          # tên khoá trên R2 (tuỳ chọn)
KAI_BACKUP_INTERVAL_MIN=30       # chu kỳ backup, phút (tuỳ chọn)
KAI_BACKUP_KEEP=5                # số bản xoay vòng (tuỳ chọn)
```

### Dùng Backblaze B2 (miễn phí, không cần thẻ)

Chỉ cần email để đăng ký (không có bước thẻ như R2). Sau khi tạo tài khoản:

1. **Buckets → Create a Bucket** → đặt tên, ví dụ `kaiai-backup`, Private.
2. **Application Keys → Add a New Application Key** → giới hạn
   `Allow access to Bucket(s): kaiai-backup`, quyền **Read and Write**.
   Copy ngay **keyID** (= Access Key ID) và **applicationKey** (= Secret) —
   applicationKey chỉ hiện đúng 1 lần.
3. Xem **endpoint** trong trang bucket, dạng
   `https://s3.<REGION>.backblazeb2.com` (ví dụ `s3.us-west-004...`) → ghép:

```bash
KAI_BACKUP_URL=https://s3.<REGION>.backblazeb2.com/kaiai-backup
KAI_BACKUP_ACCESS_KEY_ID=<keyID>
KAI_BACKUP_SECRET_ACCESS_KEY=<applicationKey>
# KAI_BACKUP_REGION bỏ trống — bot tự nhận diện region từ URL.
# (Nếu muốn ép tay: KAI_BACKUP_REGION=us-west-004)
```

⚠️ B2 **bắt buộc** ký SigV4 bằng region thật (dùng `auto` sẽ bị 403).
Bot tự nhận diện region từ URL nên cấu hình trên là đủ.

### Cách tạo token Cloudflare R2

1. Vào **Cloudflare Dashboard → R2** (lần đầu sẽ phải trả phí 1 USD để kích hoạt).
2. **Create bucket** → đặt tên, ví dụ `kaiai-backup`, chọn region gần bạn.
3. Vào **R2 → Manage R2 Tokens → Create Account API token**.
   - Quyền: chọn **Object Read & Write**.
   - Scope: chỉ bucket `kaiai-backup` vừa tạo (đừng chọn tất cả).
4. Bấm **Create** → hiện ra **Access Key ID** và **Secret Access Key**.
   ⚠️ **Secret Access Key chỉ hiện đúng 1 lần** — hãy copy ngay, không xem lại được.
5. Lấy **Account ID**: ở trang R2, mục *Account Details* (bên phải).

Ghép lại thành URL:
```
https://<ACCOUNT_ID>.r2.cloudflarestorage.com/<BUCKET_NAME>
```

Bot sẽ hoạt động như sau:
- **Lúc khởi động** → tải bản backup mới nhất về `KAI_DATA_DIR`
  (trước khi nạp cog, nên không bị ghi đè).
- **Mỗi 30 phút** → đóng gói `.db`/`.json` rồi tải lên R2.
- **Trước khi tắt** (Render deploy) → đẩy một bản cuối.

Kiểm tra bằng lệnh `!backup` (chỉ chủ bot). Xem file trên R2: bucket
`kaiai-backup` → khoá `kaiai/latest.zip` sẽ xuất hiện sau ~30 giây.

Nếu thiếu bất kỳ biến nào trong 3 biến bắt buộc, bot vẫn chạy bình thường —
chỉ là không có bản sao, và log sẽ ghi `Backup du lieu: TAT`.

**Về bảo mật:** endpoint R2 là API tương thích S3 nên **bắt buộc chữ ký
AWS Signature V4** (không phải header `Bearer`). `kai_core/backup.py` tự ký
bằng `hashlib`/`hmac` — không cần thêm thư viện — và đã được kiểm chứng bằng
vector chữ ký chính thức của AWS. Token giới hạn cho một bucket, bot chỉ cần
quyền đọc/ghi object.

## Docker
Có thể build bằng:
`docker build -t kaiai .`
và chạy:
`docker run --env-file .env -v kaiai-data:/app/data kaiai`

Nếu dùng `KAI_DATA_DIR=/app/data`, database sẽ nằm trên volume.

## Discord intents
Bật Message Content Intent và Server Members Intent trong Developer Portal.
Chỉ cấp permissions bot cần dùng.

## Discord trả về HTTP 429 / Cloudflare Error 1015
Nếu log đăng nhập có trang HTML của Cloudflare kèm `Error 1015`, Discord/Cloudflare
đang giới hạn IP outbound của dịch vụ hosting. Đây không phải lỗi `DISCORD_TOKEN`
hay lỗi code của bot. Tránh restart/redeploy liên tục vì chúng tạo thêm yêu cầu từ
cùng IP; chờ giới hạn hết hạn rồi thử lại. Nếu lỗi vẫn tiếp diễn, liên hệ nhà cung
cấp hosting hoặc triển khai từ môi trường có IP outbound được Discord chấp nhận.

## Slash command sync
`main.py` gọi `bot.tree.sync()` sau khi login. Với server lớn, Discord có thể cần thời gian để cập nhật command cache.

## SQLite
KaiAI Premium dùng WAL. Không copy database khi bot đang ghi nếu không cần thiết; ưu tiên `!backupkai`.

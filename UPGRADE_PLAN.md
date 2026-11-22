# UPGRADE PLAN — KAI AI ULTIMATE V7
## KẾ HOẠCH NÂNG CẤP VÀ TRIỂN KHAI TOÀN DIỆN

**Dự án:** KaiAI Ultimate Overhaul V7  
**Lộ trình thực hiện:** 7 Giai đoạn tuần tự theo chuẩn kỹ thuật cao cấp, không nhảy cóc, không bỏ qua kiểm thử.

---

### GIAI ĐOẠN 1: KIỂM TOÁN VÀ LẬP KẾ HOẠCH (HOÀN TẤT)
- [x] Đọc và khảo sát toàn bộ codebase hiện tại (`kai_core/`, `cogs/`, `web/`, `tests/`, `main.py`, `keep_alive.py`).
- [x] Xác định entrypoints, dependencies, cogs, database schemas, web endpoints và luồng xử lý.
- [x] Chạy bộ test suites hiện có (534 tests) để xác định độ ổn định nền tảng ban đầu và các điểm nghẽn hiệu năng.
- [x] Tạo `CODEBASE_AUDIT.md`: Đánh giá chi tiết toàn bộ các module và trace luồng dữ liệu.
- [x] Tạo `SECURITY_AUDIT.md`: Kiểm toán các lỗ hổng theo OWASP Top 10 và LLM Top 10.
- [x] Tạo `ARCHITECTURE_PLAN.md`: Bản thiết kế kiến trúc KAI AI V7 với sơ đồ Mermaid trực quan.
- [x] Tạo `UPGRADE_PLAN.md`: Kế hoạch nâng cấp và kiểm tra chi tiết theo từng bước.

---

### GIAI ĐOẠN 2: BẢO MẬT TRUNG TÂM, PHÂN QUYỀN VÀ BẢO VỆ GIAO DỊCH
**Mục tiêu:** Khắc phục triệt để các lỗ hổng Critical & High (SEC-01, SEC-02, SEC-03, SEC-04).
- **Công việc thực hiện:**
  1. Xây dựng `kai_core/economy_coordinator.py`:
     - Cung cấp `EconomyCoordinator` tập trung cho toàn bộ tiến trình.
     - Cung cấp `get_economy_lock()` dùng chung duy nhất cho `Games`, `Business`, `Company`, `RealWorld`, `Career`.
     - Tích hợp `IdempotencyManager` tự động phát hiện và ngăn chặn replay attack / double-spending giao dịch tiền tệ.
  2. Nâng cấp `kai_core/security.py`:
     - Thiết lập hệ thống RBAC đa cấp (`Owner`, `Administrator`, `Moderator`, `User`).
     - Tích hợp kiểm tra quyền tự động trên cả Prefix Command, Slash Command và Button Interactions.
  3. Xây dựng `kai_core/ai/safety.py`:
     - Lớp phòng thủ Prompt Injection & Jailbreak Guard cho mọi đầu vào AI (cả trên Discord và Web Guide API).
     - Lớp Output Sanitizer tự động loại bỏ secret, token và dữ liệu nhạy cảm trước khi trả lời người dùng.
  4. Cập nhật `cogs/games.py`, `cogs/company.py`, `cogs/business.py`:
     - Thay thế các lock riêng rẽ bằng `EconomyCoordinator` chung.
     - Đảm bảo 100% các câu lệnh `UPDATE users` đều có guard chặn số dư âm (`balance >= 0`).
  5. Viết unit & integration tests kiểm thử:
     - Test giao dịch đồng thời (Concurrency / Race Conditions).
     - Test chống double-spending bằng idempotency key.
     - Test chặn prompt injection và jailbreak attacks.

---

### GIAI ĐOẠN 3: NÂNG CẤP DATABASE ENGINE, BACKUP & DISASTER RECOVERY
**Mục tiêu:** Củng cố tính toàn vẹn dữ liệu, tối ưu hóa WAL và hoàn thiện sao lưu dự phòng.
- **Công việc thực hiện:**
  1. Nâng cấp `kai_core/migrations.py`:
     - Hệ thống quản lý Migration hợp nhất hỗ trợ cả `economy.db` và `kai_core.db`.
     - Tạo migration bổ sung cột `idempotency_key` và chỉ mục cho bảng `transactions`.
     - Tạo migration bổ sung bảng lưu trữ kế hoạch bền vững `ai_plans` và bảng quản lý phân quyền.
  2. Chuẩn hóa cấu hình SQLite qua `kai_core/database.py`:
     - Bật `PRAGMA journal_mode=WAL` và `PRAGMA synchronous=NORMAL`.
     - Thiết lập `PRAGMA busy_timeout=15000` và `PRAGMA foreign_keys=ON`.
     - Cung cấp Repository Abstraction Interfaces (`IUserRepository`, `ITransactionRepository`, `IPlanRepository`).
  3. Nâng cấp hệ thống sao lưu `kai_core/backup.py`:
     - Thêm cơ chế kiểm tra tính toàn vẹn bằng mã băm SHA-256 cho mỗi file sao lưu.
     - Xoay vòng bản sao lưu tự động (Backup Rotation: lưu 7 bản gần nhất, xóa bản cũ).
     - Thêm tính năng xác thực phục hồi (Dry-run restore validation) trước khi ghi đè database.
  4. Viết tests kiểm thử:
     - Test migration up và rollback.
     - Test tính toàn vẹn của backup và quy trình restore.

---

### GIAI ĐOẠN 4: NÂNG CẤP AI CORE, MEMORY & AUTONOMOUS AGENT
**Mục tiêu:** Đưa AI trở thành trợ lý thông minh, chủ động, có bộ nhớ phân tầng và kế hoạch bền vững.
- **Công việc thực hiện:**
  1. Nâng cấp `kai_core/ai/router.py`:
     - Tích hợp **Model Selection Engine**: Tự động phân loại độ phức tạp của yêu cầu để chọn model tối ưu chi phí và hiệu năng.
     - Hoàn thiện chuỗi Fallback nhiều provider (`OpenAI` ↔ `Gemini`).
  2. Xây dựng Bộ nhớ đa tầng `kai_core/ai/advanced_memory.py`:
     - Tầng Short-term: Lưu phiên hiện tại có TTL tự động hết hạn sau 30 phút.
     - Tầng Long-term: Lưu các sở thích và facts của người dùng, phân vùng nghiêm ngặt theo `(user_id, guild_id)`.
     - Tầng Server Memory: Lưu thông tin riêng của máy chủ.
     - Bổ sung lệnh Discord cho người dùng xem và xóa bộ nhớ (`!aimemory`, `!aimemory clear`) theo chuẩn bảo vệ quyền riêng tư GDPR.
  3. Nâng cấp `kai_core/ai/plan.py` thành Persistent Plan Store:
     - Lưu kế hoạch vào database thay vì RAM.
     - Bổ sung bước Đánh giá Rủi ro (Risk Assessment) trước khi tạo kế hoạch.
     - Tự động phục hồi kế hoạch khi bot khởi động lại.
  4. Viết tests kiểm thử:
     - Test bộ nhớ đa tầng và lệnh xóa bộ nhớ.
     - Test tính bền vững của kế hoạch AI qua các lần restart.

---

### GIAI ĐOẠN 5: NÂNG CẤP DASHBOARD 2.0 & ANALYTICS
**Mục tiêu:** Biến Dashboard thành trung tâm điều khiển hiện đại, đẹp mắt, có RBAC và bảo mật cao.
- **Công việc thực hiện:**
  1. Cấu hình bảo mật HTTP trong `keep_alive.py`:
     - Bổ sung toàn diện các Security Headers: `Content-Security-Policy`, `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`.
     - Lọc danh sách server trên `/api/servers`: Chỉ hiển thị server công khai hoặc tổng số lượng, không làm lộ tên các private guild.
  2. Bổ sung RBAC Endpoint cho Dashboard:
     - Hỗ trợ Admin Access Token để xem các thống kê chuyên sâu: Health status, Memory stats, AI Router analytics, Security Event Logs.
  3. Cải tiến giao diện Dark Premium trong `web/templates/dashboard.html` và `web/static/app.js`:
     - Hiển thị thông số thời gian thực: Uptime, Shards, Server count, Tổng tài sản server, Tỷ lệ giao dịch thành công.
     - Giao diện thân thiện trên cả điện thoại và máy tính.
  4. Viết tests kiểm thử:
     - Test bảo mật HTTP headers và lọc dữ liệu private servers.
     - Test xác thực Dashboard Admin Token.

---

### GIAI ĐOẠN 6: TỐI ƯU HIỆU NĂNG, PLUGIN ARCHITECTURE & SCALABILITY
**Mục tiêu:** Tối ưu hóa việc sử dụng CPU, RAM và mở rộng hệ thống plugin an toàn.
- **Công việc thực hiện:**
  1. Xây dựng `kai_core/plugins/`:
     - `PluginRegistry`: Quản lý việc đăng ký, kích hoạt, vô hiệu hóa các plugin.
     - `PluginManifest`: Khai báo phiên bản, quyền hạn và danh sách phụ thuộc.
     - Cô lập lỗi (Error Isolation) để lỗi plugin không ảnh hưởng đến hệ thống chính.
  2. Tối ưu hóa vòng lặp sự kiện và tài nguyên:
     - Tích hợp Graceful Shutdown hoàn chỉnh đóng kết nối database sạch sẽ trước khi thoát.
     - Circuit Breaker tự động ngắt kết nối khi các dịch vụ bên ngoài gặp sự cố kéo dài.
  3. Viết tests kiểm thử cho hệ thống Plugin Registry.

---

### GIAI ĐOẠN 7: BỘ KIỂM THỬ TOÀN DIỆN, CHUẨN HÓA & ĐÓNG GÓI BẢN PHÁT HÀNH V7
**Mục tiêu:** Đảm bảo 100% tests vượt qua thành công, chuẩn bị bản phát hành sạch.
- **Công việc thực hiện:**
  1. Chạy toàn bộ test suites (`pytest`). Đảm bảo không có test nào bị fail hoặc regression.
  2. Tạo các tài liệu báo cáo kỹ thuật hoàn chỉnh:
     - `FIX_REPORT.md`: Chi tiết tất cả lỗi và giải pháp đã thực hiện.
     - `ARCHITECTURE.md`: Tài liệu kiến trúc chuẩn cho bản V7.
     - `CHANGELOG.md`: Lịch sử các thay đổi từ V6 lên V7.
     - `DEPLOYMENT.md`: Hướng dẫn triển khai (Docker, VPS, Replit, Render) và quy trình phục hồi thảm họa.
     - `TEST_REPORT.md`: Kết quả chạy toàn bộ test suites.
     - `UPGRADE_REPORT.md`: Báo cáo tổng kết toàn bộ quá trình nâng cấp V7.
  3. Dọn dẹp repository:
     - Cập nhật file `.gitignore` chuẩn.
     - Loại bỏ các file tạm, log thừa, cache và đảm bảo không có file database production hay secret nào bị rò rỉ.
     - Cập nhật file `VERSION` thành `7.0.0`.


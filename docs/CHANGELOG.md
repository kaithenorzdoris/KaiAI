# Changelog

## 6.2.0 — Kai Game Arena

### Game & Kinh tế
- Thêm `!topgame` / `!gameboard`: bảng xếp hạng tổng hợp từ mọi trò chơi, có số trận, tỉ lệ thắng và lợi nhuận tiền ảo.
- Bảng game trong Discord và website dùng cùng quy tắc xếp hạng; dữ liệu chỉ đọc từ `game_stats`.
- Làm rõ lệnh thành tích game trong menu `!games` và danh mục lệnh.

## 6.1.0 — Public Community + Game Arena

### Website
- Gỡ đăng nhập OAuth Discord; website và chat dùng được không cần tài khoản.
- Chat khách bằng biệt danh, có CSRF, giới hạn tốc độ theo phiên và địa chỉ client.
- Thêm bảng xếp hạng game trực tiếp, tổng hợp số ván, chiến thắng và lợi nhuận ảo.

### Bot
- Lệnh prefix không tồn tại trả lời tiếng Việt và hướng dẫn `!help`, không còn im lặng.

## 6.0.0 — OpenAI Security Upgrade + Core Hardening

### Core
- Đổi branding runtime/presence sang **KaiAI**.
- Cấu hình owner bằng `BOT_OWNER_ID` thay vì bắt buộc hard-code.
- Gemini model mặc định cập nhật sang `gemini-3.5-flash-lite` và có env override.
- Thêm health/analytics core.
- SQLite WAL + indexes + persistent configuration.

### AI
- Branding KaiAI trong system prompt/UI.
- Fallback khi response rỗng.
- Giữ nguyên memory/mood/attachment flow legacy.

### Community
- Leveling + leaderboard.
- Persistent reminders.
- AFK.
- Todo.
- Suggestions + voting.
- Poll buttons.
- Ticket system.
- Giveaway persistence.
- Custom tags.
- Welcome/goodbye.
- AutoMod nhẹ.
- Server config.
- Backup.

### Utilities
- Safe calculator.
- Timestamp.
- Color lab.
- User/role/channel/emoji info.
- Permissions.
- Dice/random/coin/8ball.
- Embed and announcement helpers.

### Compatibility
Không xóa các legacy cogs: admin, database, games, logs, love, ai_chat.

## 6.0.0 — OpenAI Security Upgrade
- Replaced Google Gemini SDK/AFC with OpenAI Responses API.
- Added bounded retries for transient API failures.
- Added per-user cooldown and global AI concurrency limit.
- Added prompt/image size limits and image MIME allow-list.
- Added safe Discord CDN attachment handling and no external image proxy.
- Image generation moved to OpenAI Images API.

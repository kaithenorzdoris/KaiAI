# Security & Privacy Notes

## Secrets
Never put:
- `DISCORD_TOKEN`
- `OPENAI_API_KEY`
- database backups
- webhook secrets

vào Git, Discord chat hoặc screenshot công khai. Trên Render, đặt secrets bằng Environment Variables/Secret Files; không hard-code trong source.

## AI security / abuse controls
KaiAI dùng OpenAI Responses API. Lớp bảo vệ trong `cogs/ai_chat.py` gồm:
- cooldown theo user để giảm spam và chi phí;
- semaphore giới hạn concurrency để bảo vệ RAM/CPU của Render;
- giới hạn prompt;
- giới hạn số ảnh và kích thước attachment;
- chỉ nhận MIME ảnh nằm trong allow-list;
- timeout cho download attachment;
- không lưu base64 ảnh vào memory history;
- retry có giới hạn cho 429/5xx/network timeout;
- không đưa exception/API details cho người dùng;
- không ghi API key vào log.

Đây là defense-in-depth, không phải bảo đảm an ninh tuyệt đối. Hãy bật Discord AutoMod và least-privilege permissions.

## Discord permissions
Không cấp Administrator nếu bot không cần. Chỉ bật:
- Message Content Intent khi AI prefix/mention cần đọc nội dung;
- Server Members Intent nếu module thành viên thực sự cần.

## AI privacy
AI memory hiện chỉ ở RAM theo user và tự cleanup sau inactivity. Không coi RAM session là database lâu dài. Nếu chuyển sang persistence, cần retention policy rõ ràng.

## Backups
Backup có thể chứa:
- hồ sơ thành viên
- economy/game data
- settings
- AI/game data

Bảo vệ backup như dữ liệu riêng tư; không commit vào Git.

## Logging
Không log prompt đầy đủ, token, authorization header hoặc API key. Chỉ log loại lỗi và thông tin vận hành tối thiểu.

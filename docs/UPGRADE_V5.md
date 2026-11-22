# Hướng dẫn nâng cấp lên KaiAI v5

Tài liệu này mô tả **cái gì đã thay đổi** và **cần làm gì** khi nâng cấp một bản
KaiAI cũ (dạng "một file `main.py` + các cog rời") lên kiến trúc v5.

---

## 1. Những thay đổi lớn

| Trước (v4-) | Sau (v5) |
| --- | --- |
| Bot được định nghĩa trong `cogs/main.py` | Bot định nghĩa trong `main.py` (class `KaiAI`), `cogs/` chỉ chứa Cog |
| `core.py`, `config.py`, `utils.py` rải rác | Gộp thành package **`kai_core/`** (`config`, `logging_setup`, `database`, `utils`, `ui`) |
| `print(...)` để log | `kai_core.get_logger(...)` → console màu + file `kai.log` xoay vòng |
| Mỗi cog tự có `cog_command_error` / `on_command_error` | Xử lý lỗi **tập trung** ở `KaiAI.on_command_error` & `on_app_command_error` |
| Nạp cog bằng cách liệt kê tay / `os.listdir` | Tự phát hiện bằng **AST** (`_has_setup_function`) theo whitelist `PREFERRED_COGS` |
| Không có health server chuẩn | `keep_alive.py` (Flask + waitress) với `/health`, `/ready` |
| Không có test | `tests/` (pytest/unittest) + `scripts/validate.py` |

Bản sao lưu toàn bộ mã nguồn cũ được đặt trong **`_backup_pre_v5/`** (đã được
`.gitignore` để không lọt vào repository).

---

## 2. Các bước nâng cấp

1. **Sao lưu** bản hiện tại (repo này đã có `_backup_pre_v5/`).
2. Cài lại dependency:
   ```bash
   pip install -r requirements.txt
   # môi trường phát triển & chạy test
   pip install -r requirements-dev.txt
   ```
3. Tạo file cấu hình từ mẫu:
   ```bash
   cp .env.example .env      # Windows: copy .env.example .env
   ```
   Sau đó điền `DISCORD_TOKEN`, `BOT_OWNER_ID` và các biến muốn tinh chỉnh.
4. Kiểm tra toàn vẹn mã nguồn:
   ```bash
   python scripts/validate.py
   python -m pytest tests/ -q
   ```
5. Chạy thử:
   ```bash
   python main.py
   ```
   Nếu console in đúng banner khởi động + `✅ Đã nạp Cog: cogs.system` → nâng cấp thành công.
6. Xác nhận **Slash Command** đã đồng bộ (`SYNC_GUILD_ID` để sync tức thì cho 1 guild).

---

## 3. Những lỗi thường gặp

| Triệu chứng | Nguyên nhân | Cách sửa |
| --- | --- | --- |
| Exit code `1` | Thiếu `DISCORD_TOKEN` | Điền token vào `.env` hoặc biến môi trường |
| Exit code `2` | Token sai / đã bị reset | Lấy lại token ở Developer Portal |
| Exit code `3` | Chưa bật privileged intents | Bật **Message Content** + **Server Members** Intent |
| Cog không nạp | Thiếu hàm `setup(bot)` | Thêm `async def setup(bot): await bot.add_cog(MyCog(bot))` |
| Lệnh không hiện | Trùng tên với Cog khác | `python scripts/validate.py` sẽ chỉ ra file trùng |
| Thông báo lỗi gửi 2 lần | Cog vẫn đăng ký `on_command_error` cục bộ | Xoá listener cục bộ, hoặc đánh dấu `ctx.kai_error_handled = True` |

---

## 4. Thêm Cog mới

```python
# cogs/my_feature.py
import discord
from discord.ext import commands

class MyFeature(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="hello")
    async def hello(self, ctx: commands.Context):
        await ctx.send(f"Xin chào {ctx.author.mention}!")

async def setup(bot: commands.Bot):
    await bot.add_cog(MyFeature(bot))
```

Đưa tên module vào `PREFERRED_COGS` trong `main.py` nếu muốn nạp trước theo thứ tự
cố định; nếu không, nó vẫn tự động được phát hiện ở vòng lặp quét mở rộng.

---

## 5. Rollback

Nếu cần quay về bản cũ:

```bash
# dừng bot, rồi sao chép lại từ bản lưu
cp -r _backup_pre_v5/* .
python main.py
```

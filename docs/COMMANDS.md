# KaiAI Command Catalog

## AI
- `!ai <prompt>` — hỏi KaiAI.
- `!mood` — mood UI.
- `!clearai` — xóa memory phiên.
- `!history` — legacy history nếu có.
- `!stats` — thống kê AI.

Lệnh có prefix nhưng không tồn tại sẽ trả lời hướng dẫn dùng `!help` thay vì im lặng.
Website KaiAI có danh mục lệnh đồng bộ trực tiếp, trạng thái bot và bảng xếp hạng game; chat cộng đồng cho phép khách tham gia bằng biệt danh, không cần đăng nhập.

## Premium Core
- `!kai` — panel.
- `!botinfo` — phiên bản/runtime.
- `!health` — health check.
- `!kaistats` — command analytics.

## User
- `!userinfo [@member]`
- `!avatar [@member]`
- `!serverinfo`
- `!perms [@member]`
- `!roleinfo @role`
- `!channelinfo [#channel]`
- `!emojiinfo :emoji:`

## Fun
- `!choose A | B | C`
- `!roll 2d20+5`
- `!8ball câu hỏi`
- `!coin`
- `!random 1 100`
- `!countdown 5`

## Productivity
- `!remind 30m nội dung`
- `!afk lý do`
- `!todo việc`
- `!todo done <id>`
- `!todo delete <id>`

## Community
- `!suggest nội dung`
- `!poll 2h Câu hỏi | A | B | C`
- `!ticketsetup [category] [role]`
- `!giveaway 1h 2 Phần thưởng`
- `!tag create tên nội dung`
- `!tag tên`
- `!tag list`
- `!tag delete tên`

## Configuration
- `!kaiconfig`
- `!kaiconfig welcome #channel`
- `!kaiconfig goodbye #channel`
- `!kaiconfig levelup #channel`
- `!kaiconfig log #channel`
- `!kaiconfig levels on`
- `!kaiconfig automod on on on off`
- `!backupkai`

## Admin legacy
Toàn bộ `!admin`, `!warn`, `!ban`, `!kick`, `!mute`, `!lock`, `!unlock`, `!slowmode`, `!nuke`, `!lockdown`, quản lý role/permission… vẫn được giữ trong `cogs/admin.py`.

## Thông báo cập nhật bot
- `!setupdatechannel [#kênh]` *(Server Admin)* — chọn kênh nhận thông báo cập nhật bot trong server. Bỏ kênh để dùng Discord System Channel.
- `!clearupdatechannel` *(Server Admin)* — xoá lựa chọn riêng và quay lại System Channel.
- `!broadcastupdate <nội dung>` *(Bot Owner)* — gửi thông báo tới các server có kênh đích và quyền gửi tin; phải xác nhận trước khi phát.
- `!broadcastupdate --ping <nội dung>` *(Bot Owner)* — ping `@everyone` chỉ tại các server bot được quyền Mention Everyone. Không cho phép ping user/role từ nội dung.

Kênh đích mặc định là System Channel của server. Quản trị viên có thể đặt kênh riêng bằng `!setupdatechannel #thong-bao`. Cấu hình được lưu ở `KAI_DATA_DIR/system_settings.db`; hãy dùng persistent storage khi triển khai. Lệnh phát toàn bot chỉ dành cho chủ ứng dụng Discord hoặc owner ID khai báo qua environment để tránh admin của một server gửi quảng bá tới mọi cộng đồng.

## Group database legacy
`!reg`, `!linkroblox`, `!profile`, `!top`, `!mission`, `!addpoint`, `!mod`, `!backup`… vẫn được giữ.

## Economy/Games legacy
`!taixiu`, `!duangua`, `!bongda`, `!vaytien`, `!trano`, `!taisan`, hệ thống công ty/cổ tức/đầu tư… vẫn được giữ.
- `!topgame` *(alias `!gameboard`)* — bảng xếp hạng 10 người chơi, cộng gộp thành tích game và xếp theo lợi nhuận tiền ảo.
- `!gamestats [@member]` — xem số ván, tỉ lệ thắng và lợi nhuận theo từng game của một người chơi.

## Economy nâng cao (vay/tiết kiệm/sàn CK)
- `!laissuat` *(alias `!rate`, `!interest`)* — biểu lãi suất **công bố theo ngày** (cả server thấy cùng một mức): gửi tiết kiệm, vay không bảo đảm, vay thế chấp.
- `!thechap <công ty> <số CP>` *(alias `!pawn`, `!collateral`)* — cầm cố cổ phiếu công ty để vay tiền theo LTV **60%** với lãi suất thế chấp rẻ hơn vay thường.
- `!chuoc <công ty>` *(alias `!unpawn`)* — trả nợ thế chấp để nhận lại số cổ phiếu đã cầm cố.
- `!market` *(alias `!chungkhoan`)* — bảng giá sàn; giá chỉ nhích tối đa **1 lần/phút** (chống spam), hiển thị P/L cổ phiếu đang giữ.
- `!muacp <MÃ> <số CP>` *(alias `!buystock`)* — mua cổ phiếu niêm yết (phí **1%**).
- `!bancp <MÃ> <số CP>` *(alias `!sellstock`)* — bán cổ phiếu đang giữ (phí **1%**).

Lãi suất ngân hàng và lãi vay đều tính **lãi kép theo từng ngày**; `!taisan` (và trợ lý AI) hiển thị đầy đủ ví/bank/cổ phiếu/nợ/tài sản ròng.

## Love legacy
Nhóm `!love` và các tính năng tình yêu, shop, marriage/roleplay… vẫn được giữ.

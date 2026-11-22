# KaiAI Ultimate Economy v5

## Tổng quan

KaiAI Economy v5 là một nền kinh tế ảo chạy hoàn toàn trong SQLite. Mục tiêu của bản này là tạo cảm giác như một game server hoàn chỉnh thay vì chỉ là vài lệnh cộng/trừ tiền.

### Hệ thống cốt lõi

- Ví tiền và ngân hàng.
- Chuyển tiền giữa người chơi.
- Daily streak.
- Nghề nghiệp và thu nhập theo cấp độ.
- XP, level, reputation.
- Nhiệm vụ hằng ngày.
- Câu cá, khai khoáng, phi vụ.
- Lịch sử giao dịch.
- Bảng xếp hạng tài sản.
- Thống kê riêng cho từng mini-game.
- Khoản vay và lãi suất.
- Công ty, cổ phần, takeover và cổ tức.
- Thị trường mô phỏng.

## Arcade catalog

### Tài Xỉu
`!taixiu 10k`

Ba viên xúc xắc. 11–18 là Tài, 3–10 là Xỉu. Người chơi chọn cửa bằng Button. Tiền cược được khóa trước khi kết quả được tính.

### Chẵn Lẻ
`!chanle 10k`

Một số từ 1–100 được tạo ngẫu nhiên. Chọn Chẵn hoặc Lẻ.

### Coinflip
`!coinflip 10k`

Chọn Ngửa hoặc Sấp.

### Slots
`!slots 10k`

Bảy biểu tượng. Ba số 7 là jackpot x15, ba biểu tượng giống nhau là x8, hai biểu tượng giống nhau là x2, Diamond là hoàn vốn x1.

### Dice
`!dice 10k`

Bạn đấu một xúc xắc với Kai. Cao hơn thắng x2, bằng nhau hoàn cược.

### High/Low
`!highlow 10k cao`

So sánh lá đầu và lá sau.

### Rock Paper Scissors
`!rps 10k keo`

Kéo–Búa–Bao với Kai.

### Blackjack
`!blackjack 10k`

Một ván mô phỏng không giữ state giữa message. Cách này giúp bot không bị khóa bàn nếu restart.

### Roulette
`!roulette 10k red`

Hỗ trợ red/black, odd/even và alias tiếng Việt.

### Đua Ngựa
`!duangua 10k`

Chọn một trong bốn đối thủ. Kết quả và settlement nằm trên server.

### Bóng Đá
`!bongda 10k`

Kai tạo hai đội ngẫu nhiên và cho người chơi chọn đội A, hòa hoặc đội B.

## Anti-exploit

### Atomic balance
Các khoản trừ tiền quan trọng dùng SQLite `BEGIN IMMEDIATE` và điều kiện số dư. Điều này giảm nguy cơ hai command đồng thời cùng sử dụng một số dư.

### Transaction log
Mọi thay đổi balance qua primitive chính được ghi vào `transactions` với loại giao dịch, amount, balance sau giao dịch và timestamp.

### Game statistics
Mỗi ván ghi `played`, `wins`, `losses`, `wagered`, `profit`. Không dùng dữ liệu client để xác định số tiền trả thưởng.

### Cooldown persistence
Daily, work, fish, mine, crime và quest dùng bảng cooldowns. Restart bot không reset các mốc thời gian này.

### No eval
KaiAI không thực thi biểu thức người dùng bằng `eval()` trong economy engine.

## Progression

XP cần để lên level tăng theo level. Game, daily, work, fish, mine và quest cấp XP. Level cao hơn tăng hạn mức vay và một số reward.

## Company simulator

Một company có:

- owner
- total shares
- price per share
- company cash
- shareholders
- takeover khi một cổ đông vượt 50%
- dividend từ company cash
- chuyển nhượng công ty

### Luồng đầu tư

1. Kiểm tra công ty.
2. Tính số cổ phần theo giá hiện tại.
3. Transaction lock.
4. Kiểm tra lại balance.
5. Trừ tiền.
6. Cộng cash cho công ty.
7. Phát hành cổ phần.
8. Kiểm tra takeover.
9. Commit.

## Market simulator

Market có 8 mã ảo: KAI, ROB, NOVA, BYTE, LUX, OCEAN, ZEN và APEX. Mỗi lần xem market, giá có thể biến động trong phạm vi nhỏ. Đây là game economy, không phải dữ liệu thị trường thật.

## Khuyến nghị triển khai

- Đặt `DISCORD_TOKEN` trong environment.
- Bật Message Content Intent và Server Members Intent nếu các cog khác yêu cầu.
- Sao lưu `economy.db` định kỳ.
- Không commit `.env`.
- Dùng Python 3.11+.
- Cài dependencies từ `requirements.txt`.

## Backup

Có thể sao chép `economy.db` khi bot tắt để tạo backup đơn giản. SQLite WAL sẽ được checkpoint khi cần.

## Tương thích legacy

Các lệnh cũ của economy/company vẫn tồn tại:

`!cash`, `!dep`, `!with`, `!pay`, `!vaytien`, `!trano`, `!taisan`, `!games`, `!taixiu`, `!duangua`, `!bongda`, `!ctytao`, `!ctyinfo`, `!ctydautu`, `!ctycotuc`, `!ctyban`.

Các alias phổ biến cũng được giữ.

## Checklist QA

- [x] Parse toàn bộ Python source.
- [x] Không dùng eval trong economy engine.
- [x] SQLite schema có migration cho cột mới.
- [x] Index cho transaction, game stats và shares.
- [x] Atomic debit cho game.
- [x] Cooldown persistent.
- [x] Game settlement chạy server-side.
- [x] Không lưu trạng thái bàn blackjack dài hạn.
- [x] Menu game được phân trang.
- [x] File ZIP được kiểm tra CRC/integrity trước khi phát hành.

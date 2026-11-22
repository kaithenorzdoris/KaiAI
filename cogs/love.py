import json
import os
import random
from datetime import datetime, timedelta

import discord
from discord.ext import commands

# --- HỆ THỐNG QUẢN LÝ DỮ LIỆU ---
DATA_FILE = "love_data.json"
CODES_FILE = "love_codes.json"

def load_json(file_path):
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_json(file_path, data):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def get_user(user_id, data):
    user_id = str(user_id)
    if user_id not in data:
        data[user_id] = {}

    defaults = {
        "partner": None,
        "love_points": 0,
        "children": 0,
        "money": 2000,
        "married_since": None,
        "ring": "❌ Chưa có",
        "car": "🚶 Đi bộ",
        "house": "⛺ Lều chõng",
        "bio": "Chưa đặt lời chúc tình yêu nào!",
        "last_daily": None
    }
    for key, value in defaults.items():
        if key not in data[user_id]:
            data[user_id][key] = value

    return data[user_id]

# --- GIFS CHO ROLEPLAY ---
GIFS = {
    "kiss": [
        "https://media.giphy.com/media/G3va31oGhenUZB1qP5/giphy.gif",
        "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExM3Z2eHk4YzR2ZWh0ZWh2eHk0OHZld2M1Z3R4/flLjc2M4mS33L7jP3E/giphy.gif"
    ],
    "hug": [
        "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExMnB4ZzR2eHk0OHZld2M1Z3R4/3M4NpbLCTxBqU/giphy.gif",
        "https://media.giphy.com/media/l2QDM9Jnim1YV55YA/giphy.gif"
    ],
    "pat": [
        "https://media.giphy.com/media/ARSp9T7wwxNUXG07rC/giphy.gif",
        "https://media.giphy.com/media/N0CIxcy454AKs/giphy.gif"
    ],
    "slap": [
        "https://media.giphy.com/media/Gf3AUz3eBNbTW/giphy.gif",
        "https://media.giphy.com/media/j3iGKfXRKlLqw/giphy.gif"
    ],
    "cuddle": [
        "https://media.giphy.com/media/143v0Z4767T15e/giphy.gif"
    ],
    "feed": [
        "https://media.giphy.com/media/11z1x3S870I260/giphy.gif"
    ]
}

# --- GIAO DIỆN NÚT BẤM CẦU HÔN ---
class ProposeView(discord.ui.View):
    def __init__(self, proposer: discord.Member, target: discord.Member, data: dict, cog):
        super().__init__(timeout=120)
        self.proposer = proposer
        self.target = target
        self.data = data
        self.cog = cog

    @discord.ui.button(label="💍 Đồng ý", style=discord.ButtonStyle.success, emoji="💖")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("❌ Lời cầu hôn này không dành cho bạn!", ephemeral=True)

        p_id = str(self.proposer.id)
        t_id = str(self.target.id)

        self.data[p_id]["partner"] = t_id
        self.data[t_id]["partner"] = p_id

        timestamp = int(datetime.now().timestamp())
        self.data[p_id]["married_since"] = timestamp
        self.data[t_id]["married_since"] = timestamp

        save_json(DATA_FILE, self.data)

        embed = discord.Embed(
            title="🎉 CHÚC MỪNG HẠNH PHÚC LỨA ĐÔI! 🎉",
            description=f"💖 **{self.target.mention}** đã đồng ý lời cầu hôn của **{self.proposer.mention}**!\n\n"
                        f"Từ hôm nay, hai bạn chính thức về chung một nhà. Chúc hai bạn bách niên giai lão! 💍✨",
            color=discord.Color.brand_red()
        )
        embed.set_image(url="https://media.giphy.com/media/xT0xeQ1ZUQ0lvz2HQI/giphy.gif")

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="💔 Từ chối", style=discord.ButtonStyle.danger, emoji="🥀")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            return await interaction.response.send_message("❌ Bạn không có quyền từ chối thay người khác!", ephemeral=True)

        embed = discord.Embed(
            title="🥀 LỜI CẦU HÔN BỊ TỪ CHỐI",
            description=f"Rất tiếc **{self.proposer.mention}**... **{self.target.mention}** đã trao cho bạn thẻ người tốt! 😭",
            color=discord.Color.dark_gray()
        )
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

# --- GIAO DIỆN XÁC NHẬN LY HÔN ---
class DivorceConfirmView(discord.ui.View):
    def __init__(self, author: discord.Member, partner: discord.User, data: dict):
        super().__init__(timeout=60)
        self.author = author
        self.partner = partner
        self.data = data

    @discord.ui.button(label="Xác nhận Ly hôn", style=discord.ButtonStyle.danger, emoji="💔")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("❌ Bạn không phải là người yêu cầu ly hôn!", ephemeral=True)

        a_id = str(self.author.id)
        p_id = str(self.partner.id)

        self.data[a_id]["partner"] = None
        self.data[a_id]["married_since"] = None
        self.data[a_id]["love_points"] = 0

        if p_id in self.data:
            self.data[p_id]["partner"] = None
            self.data[p_id]["married_since"] = None
            self.data[p_id]["love_points"] = 0

        save_json(DATA_FILE, self.data)

        embed = discord.Embed(
            title="💔 CHÍNH THỨC LY HÔN",
            description=f"**{self.author.mention}** và **{self.partner.mention}** đã giải thoát cho nhau.\n"
                        f"Điểm tình yêu của cả hai đã bị dắt về **0**.",
            color=discord.Color.dark_grey()
        )
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Hủy bỏ", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await interaction.response.send_message("❌ Bạn không có quyền hủy thao tác này!", ephemeral=True)

        await interaction.response.edit_message(content="✅ Đã hủy thao tác ly hôn. Hãy suy nghĩ kỹ trước khi quyết định nhé!", embed=None, view=None)

# --- GIAO DIỆN SHOP CHỌN MUA BẰNG SELECT MENU ---
class ShopSelect(discord.ui.Select):
    def __init__(self, data: dict):
        self.data = data
        options = [
            # Nhẫn
            discord.SelectOption(label="Nhẫn Bạc", value="bac", description="2,000 xu | Nhẫn đính hôn đơn giản", emoji="💍"),
            discord.SelectOption(label="Nhẫn Vàng", value="vang", description="5,000 xu | Nhẫn vàng 24K óng ánh", emoji="🌟"),
            discord.SelectOption(label="Nhẫn Kim Cương", value="kimcuong", description="20,000 xu | Kim cương lấp lánh VIP PRO", emoji="💎"),
            # Xe
            discord.SelectOption(label="Honda SH 150i", value="xemay", description="15,000 xu | Xe máy vi vu dạo phố", emoji="🏍️"),
            discord.SelectOption(label="Vinfast VF8", value="oto", description="100,000 xu | Ô tô điện hiện đại", emoji="🚘"),
            discord.SelectOption(label="Ferrari SF90", value="sieuxe", description="500,000 xu | Siêu xe đỉnh cao tốc độ", emoji="🏎️"),
            # Bất động sản
            discord.SelectOption(label="Căn Hộ Cao Cấp", value="chungcu", description="300,000 xu | Nhìn ra toàn thành phố", emoji="🏢"),
            discord.SelectOption(label="Biệt Thự Ven Biển", value="bietthu", description="1,000,000 xu | View biển cực chill", emoji="🏡"),
            discord.SelectOption(label="Lâu Đài Hoàng Gia", value="laudai", description="5,000,000 xu | Dành cho đẳng cấp Vua/Chúa", emoji="🏰"),
        ]
        super().__init__(placeholder="🛒 Chọn món đồ bạn muốn mua...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        items = {
            "bac": ["💍 Nhẫn Bạc", 2000, "ring"],
            "vang": ["🌟 Nhẫn Vàng", 5000, "ring"],
            "kimcuong": ["💎 Nhẫn Kim Cương", 20000, "ring"],
            "xemay": ["🏍️ Honda SH 150i", 15000, "car"],
            "oto": ["🚘 Vinfast VF8", 100000, "car"],
            "sieuxe": ["🏎️ Siêu xe Ferrari", 500000, "car"],
            "chungcu": ["🏢 Căn Hộ Cao Cấp", 300000, "house"],
            "bietthu": ["🏡 Biệt Thự Ven Biển", 1000000, "house"],
            "laudai": ["🏰 Lâu Đài Hoàng Gia", 5000000, "house"],
        }

        item_key = self.values[0]
        name, price, category = items[item_key]
        user_info = get_user(interaction.user.id, self.data)

        if user_info["money"] < price:
            return await interaction.response.send_message(
                f"❌ Bạn chỉ có **{user_info['money']:,} xu**, không đủ **{price:,} xu** để mua **{name}**!",
                ephemeral=True
            )

        user_info["money"] -= price
        user_info[category] = name
        save_json(DATA_FILE, self.data)

        await interaction.response.send_message(
            f"🎉 **CHÚC MỪNG!** {interaction.user.mention} đã mua thành công **{name}** với giá **{price:,} xu**!"
        )

class ShopView(discord.ui.View):
    def __init__(self, data: dict):
        super().__init__(timeout=60)
        self.add_item(ShopSelect(data))


# --- COG CHÍNH ---
class LoveSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_json(DATA_FILE)
        self.codes = load_json(CODES_FILE)

    # --- MENU LỆNH CHÍNH ---
    @commands.group(name="love", invoke_without_command=True, aliases=["tinhyeu"])
    async def love(self, ctx):
        embed = discord.Embed(
            title="💖 HỆ THỐNG ROLEPLAY TÌNH YÊU & GIA ĐÌNH VIP PRO 💖",
            description="Chào mừng bạn đến với thế giới mô phỏng tình yêu, gia đình và kinh tế siêu hấp dẫn!",
            color=discord.Color.magenta()
        )
        embed.add_field(
            name="💞 Tương tác Tình Yêu",
            value="`profile`, `marry`, `divorce`, `date`, `baby`, `lovetest`, `setbio`",
            inline=False
        )
        embed.add_field(
            name="🎭 Cảm xúc Roleplay",
            value="`kiss`, `hug`, `pat`, `slap`, `cuddle`, `feed`",
            inline=False
        )
        embed.add_field(
            name="💰 Kinh tế & Đời sống",
            value="`daily`, `work`, `care`, `shop`, `pay`",
            inline=False
        )
        embed.add_field(
            name="🎁 Giftcode Thưởng",
            value="`redeem <code_name>`\n`createcode` *(Chỉ Admin)*",
            inline=False
        )
        embed.add_field(
            name="🏆 Bảng Xếp Hạng",
            value="`top love` (Top cặp đôi) | `top rich` (Top đại gia)",
            inline=False
        )
        embed.set_footer(text="Gõ !love <tên_lệnh> để sử dụng | Ví dụ: !love profile")
        await ctx.send(embed=embed)

    # --- HỒ SƠ CÁ NHÂN ---
    @love.command(name="profile", aliases=["p", "infor"])
    async def profile(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        user_info = get_user(member.id, self.data)

        embed = discord.Embed(title=f"💌 Hồ sơ cá nhân của {member.display_name}", color=discord.Color.pink())
        embed.set_thumbnail(url=member.display_avatar.url)

        # Kiểm tra bạn đời
        if user_info["partner"]:
            partner_id = int(user_info["partner"])
            partner = self.bot.get_user(partner_id) or await self.bot.fetch_user(partner_id)
            time_str = f"<t:{user_info['married_since']}:R>" if user_info['married_since'] else "Không rõ"
            partner_text = f"Đã kết hôn với {partner.mention}\n💍 Kết hôn: {time_str}"
        else:
            partner_text = "*Độc thân vui tính (Đang tìm một nửa)* 🥀"

        embed.add_field(name="💕 Tình trạng hôn nhân", value=partner_text, inline=False)
        embed.add_field(name="🔥 Điểm Tình Yêu", value=f"**{user_info['love_points']:,}**", inline=True)
        embed.add_field(name="👶 Con Cái", value=f"**{user_info['children']}** đứa", inline=True)
        embed.add_field(name="💰 Tài Sản", value=f"**{user_info['money']:,}** xu", inline=True)

        embed.add_field(name="💍 Nhẫn Cưới", value=user_info['ring'], inline=True)
        embed.add_field(name="🚗 Phương Tiện", value=user_info['car'], inline=True)
        embed.add_field(name="🏡 Nơi Ở", value=user_info['house'], inline=True)

        embed.add_field(name="📝 Lời chúc / Tiểu sử", value=f"*{user_info['bio']}*", inline=False)

        await ctx.send(embed=embed)

    @love.command(name="setbio")
    async def setbio(self, ctx, *, bio: str):
        if len(bio) > 100:
            return await ctx.send("❌ Lời chúc/Tiểu sử tối đa 100 ký tự thôi nhé!")
        user_info = get_user(ctx.author.id, self.data)
        user_info["bio"] = bio
        save_json(DATA_FILE, self.data)
        await ctx.send(f"✅ Đã cập nhật tiểu sử thành công: *\"{bio}\"*")

    # --- ĐIỂM DANH HÀNG NGÀY ---
    @love.command(name="daily")
    async def daily(self, ctx):
        user_info = get_user(ctx.author.id, self.data)
        now = datetime.now()

        if user_info["last_daily"]:
            last_date = datetime.fromtimestamp(user_info["last_daily"])
            if now.date() == last_date.date():
                return await ctx.send("⏳ Hôm nay bạn đã nhận quà điểm danh rồi! Hãy quay lại vào ngày mai nhé.")

        reward = 1000
        bonus_msg = ""

        # Thưởng thêm nếu đã kết hôn
        if user_info["partner"]:
            reward += 500
            bonus_msg = " (Bao gồm +500 xu thưởng kết hôn! 💕)"

        user_info["money"] += reward
        user_info["last_daily"] = int(now.timestamp())
        save_json(DATA_FILE, self.data)

        await ctx.send(f"🎁 **{ctx.author.display_name}** vừa nhận quà điểm danh hôm nay: **+{reward:,} xu**!{bonus_msg}")

    # --- LÀM VIỆC ---
    @love.command(name="work", aliases=["lamviec"])
    @commands.cooldown(1, 1800, commands.BucketType.user) # 30 phút
    async def work(self, ctx):
        user_info = get_user(ctx.author.id, self.data)
        jobs = [
            ("Lập trình ứng dụng Bot Discord", 300, 600),
            ("Pha chế tại quán Cafe Mèo", 200, 450),
            ("Chạy xe ôm công nghệ", 150, 400),
            ("Bán hàng online chốt đơn liên tục", 250, 500),
            ("Livestream chơi game", 300, 700),
            ("Phụ hồ công trình", 200, 500)
        ]
        job_name, min_pay, max_pay = random.choice(jobs)
        earned = random.randint(min_pay, max_pay)

        user_info["money"] += earned
        save_json(DATA_FILE, self.data)

        await ctx.send(f"💼 **{ctx.author.display_name}** đã đi làm **{job_name}** và nhận được **+{earned:,} xu**!")

    # --- CON PHỤNG DƯỠNG ---
    @love.command(name="care", aliases=["phungduong"])
    @commands.cooldown(1, 28800, commands.BucketType.user) # 8 tiếng
    async def care(self, ctx):
        user_info = get_user(ctx.author.id, self.data)
        if user_info["children"] <= 0:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send("❌ Bạn chưa có đứa con nào! Hãy sinh con bằng lệnh `!love baby` trước nhé.")

        earned_per_child = random.randint(300, 800)
        total_earned = earned_per_child * user_info["children"]

        user_info["money"] += total_earned
        save_json(DATA_FILE, self.data)

        embed = discord.Embed(
            title="👨‍👩‍👧‍👦 CON CÁI HIẾU THẢO",
            description=f"Thật có phúc! **{user_info['children']} đứa con** của bạn vừa gửi tiền về biếu gia đình.\n"
                        f"💰 Bạn nhận được tổng cộng: **+{total_earned:,} xu**! 💸",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)

    # --- SHOP & BẤT ĐỘNG SẢN ---
    @love.command(name="shop")
    async def shop(self, ctx):
        embed = discord.Embed(
            title="🛒 CỬA HÀNG TRANG SỨC & BẤT ĐỘNG SẢN VIP PRO",
            description="Hãy chọn món đồ bạn muốn mua từ Menu chọn bên dưới:",
            color=discord.Color.gold()
        )
        embed.add_field(name="💍 Nhẫn Cưới", value="• Nhẫn Bạc (2k)\n• Nhẫn Vàng (5k)\n• Nhẫn Kim Cương (20k)", inline=True)
        embed.add_field(name="🚗 Phương Tiện", value="• Xe SH (15k)\n• Vinfast VF8 (100k)\n• Supercar Ferrari (500k)", inline=True)
        embed.add_field(name="🏡 Bất Động Sản", value="• Chung Cư (300k)\n• Biệt Thự (1M)\n• Lâu Đài (5M)", inline=True)

        view = ShopView(self.data)
        await ctx.send(embed=embed, view=view)

    # --- CHUYỂN TIỀN / TẶNG TIỀN ---
    @love.command(name="pay", aliases=["chuyentien", "give"])
    async def pay(self, ctx, member: discord.Member, amount: int):
        if member == ctx.author:
            return await ctx.send("❌ Bạn không thể tự chuyển tiền cho chính mình!")
        if member.bot:
            return await ctx.send("❌ Bot không cần xài xu đâu bạn ơi!")
        if amount <= 0:
            return await ctx.send("❌ Số tiền chuyển phải lớn hơn 0!")

        sender = get_user(ctx.author.id, self.data)
        if sender["money"] < amount:
            return await ctx.send(f"❌ Bạn không đủ tiền! Bạn chỉ có **{sender['money']:,} xu**.")

        receiver = get_user(member.id, self.data)
        sender["money"] -= amount
        receiver["money"] += amount
        save_json(DATA_FILE, self.data)

        await ctx.send(f"💸 **{ctx.author.display_name}** đã chuyển thành công **{amount:,} xu** cho **{member.display_name}**!")

    # --- HỆ THỐNG GIFTCODE ---
    @love.command(name="createcode")
    @commands.has_permissions(administrator=True)
    async def createcode(self, ctx, code: str, money: int, points: int, max_uses: int):
        code = code.upper()
        if code in self.codes:
            return await ctx.send("❌ Giftcode này đã tồn tại rồi!")

        self.codes[code] = {
            "money": money,
            "love_points": points,
            "max_uses": max_uses,
            "used_by": []
        }
        save_json(CODES_FILE, self.codes)
        await ctx.send(f"✅ Đã tạo Giftcode **`{code}`**: +{money:,} xu, +{points} điểm tình yêu (Giới hạn: {max_uses} lượt).")

    @love.command(name="redeem")
    async def redeem(self, ctx, code: str):
        code = code.upper()
        if code not in self.codes:
            return await ctx.send("❌ Giftcode không tồn tại hoặc đã bị xóa!")

        code_data = self.codes[code]
        user_id_str = str(ctx.author.id)

        if user_id_str in code_data["used_by"]:
            return await ctx.send("❌ Bạn đã nhận Giftcode này rồi!")

        if len(code_data["used_by"]) >= code_data["max_uses"]:
            return await ctx.send("❌ Giftcode này đã hết lượt sử dụng!")

        user_info = get_user(ctx.author.id, self.data)
        user_info["money"] += code_data["money"]
        user_info["love_points"] += code_data["love_points"]

        code_data["used_by"].append(user_id_str)
        save_json(DATA_FILE, self.data)
        save_json(CODES_FILE, self.codes)

        await ctx.send(f"🎁 **THÀNH CÔNG!** Bạn nhận được **+{code_data['money']:,} xu** và **+{code_data['love_points']} điểm tình yêu**!")

    # --- BẢNG XẾP HẠNG (LEADERBOARD) ---
    @love.group(name="top", invoke_without_command=True)
    async def top(self, ctx):
        await ctx.send("💡 Hãy dùng lệnh `!love top love` (Top tình yêu) hoặc `!love top rich` (Top đại gia).")

    @top.command(name="love")
    async def top_love(self, ctx):
        sorted_users = sorted(
            [u for u in self.data.items() if u[1].get("partner")],
            key=lambda x: x[1].get("love_points", 0),
            reverse=True
        )

        if not sorted_users:
            return await ctx.send("📊 Chưa có cặp đôi nào trên bảng xếp hạng!")

        embed = discord.Embed(title="🏆 BẢNG XẾP HẠNG TÌNH YÊU 🏆", color=discord.Color.red())
        seen = set()
        rank = 1

        for user_id, user_data in sorted_users:
            if user_id in seen:
                continue
            partner_id = user_data.get("partner")
            seen.add(user_id)
            seen.add(partner_id)

            u1 = self.bot.get_user(int(user_id))
            u2 = self.bot.get_user(int(partner_id)) if partner_id else None

            name1 = u1.display_name if u1 else f"ID:{user_id}"
            name2 = u2.display_name if u2 else "Ẩn danh"

            embed.add_field(
                name=f"#{rank} | {name1} 💕 {name2}",
                value=f"🔥 Điểm Tình Yêu: **{user_data.get('love_points', 0):,}**",
                inline=False
            )
            rank += 1
            if rank > 10:
                break

        await ctx.send(embed=embed)

    @top.command(name="rich")
    async def top_rich(self, ctx):
        sorted_users = sorted(
            self.data.items(),
            key=lambda x: x[1].get("money", 0),
            reverse=True
        )

        embed = discord.Embed(title="💰 BẢNG XẾP HẠNG ĐẠI GIA 💰", color=discord.Color.gold())
        rank = 1

        for user_id, user_data in sorted_users:
            u = self.bot.get_user(int(user_id))
            name = u.display_name if u else f"ID:{user_id}"
            embed.add_field(
                name=f"#{rank} | {name}",
                value=f"💵 Tài sản: **{user_data.get('money', 0):,} xu**",
                inline=False
            )
            rank += 1
            if rank > 10:
                break

        await ctx.send(embed=embed)

    # --- LỆNH TÌNH YÊU (MARRY, DIVORCE, DATE, BABY) ---
    @love.command(name="marry")
    async def marry(self, ctx, member: discord.Member):
        if member == ctx.author:
            return await ctx.send("❌ Bạn không thể tự kết hôn với chính mình!")
        if member.bot:
            return await ctx.send("❌ Bot không thể kết hôn được đâu bạn ơi!")

        user_info = get_user(ctx.author.id, self.data)
        target_info = get_user(member.id, self.data)

        if user_info["partner"]:
            return await ctx.send("❌ Bạn đã có vợ/chồng rồi! Tính ngoại tình sao? 😡")
        if target_info["partner"]:
            return await ctx.send(f"❌ **{member.display_name}** đã là hoa có chủ rồi!")

        embed = discord.Embed(
            title="💍 LỜI CẦU HÔN NGỌT NGÀO",
            description=f"Này **{member.mention}**! **{ctx.author.mention}** đang quỳ gối trao nhẫn và muốn về chung nhà với bạn! 💕",
            color=discord.Color.gold()
        )
        view = ProposeView(ctx.author, member, self.data, self)
        await ctx.send(content=member.mention, embed=embed, view=view)

    @love.command(name="divorce")
    async def divorce(self, ctx):
        user_info = get_user(ctx.author.id, self.data)
        if not user_info["partner"]:
            return await ctx.send("❌ Bạn hiện đang độc thân, không có ai để ly hôn cả!")

        partner_id = int(user_info["partner"])
        partner = self.bot.get_user(partner_id) or await self.bot.fetch_user(partner_id)

        view = DivorceConfirmView(ctx.author, partner, self.data)
        await ctx.send(
            content=f"⚠️ **{ctx.author.mention}**, bạn có chắc chắn muốn ly hôn với **{partner.display_name}** không?\nLưu ý: Điểm tình yêu sẽ bị xóa sạch!",
            view=view
        )

    @love.command(name="date")
    @commands.cooldown(1, 3600, commands.BucketType.user) # 1 tiếng
    async def date(self, ctx):
        user_info = get_user(ctx.author.id, self.data)
        if not user_info["partner"]:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send("❌ Bạn phải kết hôn thì mới đi hẹn hò lãng mạn được chứ!")

        if user_info["money"] < 500:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send("❌ Hẹn hò tốn 500 xu! Hãy `!love work` kiếm thêm tiền nhé.")

        user_info["money"] -= 500
        gained_points = random.randint(20, 60)

        user_info["love_points"] += gained_points
        partner_id = user_info["partner"]
        if partner_id in self.data:
            self.data[partner_id]["love_points"] += gained_points

        save_json(DATA_FILE, self.data)

        dates_places = ["nhà hàng sang trọng 5 sao", "rạp chiếu phim VIP", "bãi biển ngắm hoàng hôn", "quán cafe không gian chill"]
        place = random.choice(dates_places)

        embed = discord.Embed(
            title="🍷 BUỔI HẸN HỌ LÃNG MẠN",
            description=f"Hai người đã tiêu **500 xu** để hẹn hò tại **{place}**.\n"
                        f"✨ Cả hai vừa nhận thêm **+{gained_points} Điểm tình yêu!**",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)

    @love.command(name="baby")
    async def baby(self, ctx):
        user_info = get_user(ctx.author.id, self.data)
        if not user_info["partner"]:
            return await ctx.send("❌ Bạn chưa kết hôn!")

        cost = 5000
        required_points = 100

        if user_info["money"] < cost or user_info["love_points"] < required_points:
            return await ctx.send(f"❌ Để sinh em bé cần ít nhất **{cost:,} xu** và **{required_points} Điểm tình yêu**!")

        user_info["money"] -= cost
        user_info["love_points"] -= 50
        user_info["children"] += 1

        partner_id = user_info["partner"]
        if partner_id in self.data:
            self.data[partner_id]["children"] += 1

        save_json(DATA_FILE, self.data)

        embed = discord.Embed(
            title="🍼 CHÚC MỪNG GIA ĐÌNH CÓ THÊM THÀNH VIÊN MỚI!",
            description=f"🎉 Gia đình bạn vừa đón chào một thiên thần nhỏ đáng yêu! *(Trừ {cost:,} xu)*",
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)

    @love.command(name="lovetest")
    async def lovetest(self, ctx, member: discord.Member):
        percent = random.randint(0, 100)
        bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))

        embed = discord.Embed(
            title="🔮 BÓI ĐỘ TƯƠNG HỢP TÌNH YÊU",
            description=f"Tỷ lệ hợp nhau giữa **{ctx.author.display_name}** và **{member.display_name}**:\n\n"
                        f"**{percent}%** [{bar}]",
            color=discord.Color.purple()
        )
        await ctx.send(embed=embed)

    # --- LỆNH ROLEPLAY INTERACTIVE (KISS, HUG, PAT, SLAP, CUDDLE, FEED) ---
    async def _send_roleplay(self, ctx, member: discord.Member, action_name: str, message_text: str):
        if member == ctx.author:
            return await ctx.send(f"❌ Bạn không thể tự {action_name} chính mình được!")

        gif_url = random.choice(GIFS.get(action_name, [""]))
        embed = discord.Embed(description=message_text, color=discord.Color.pink())
        if gif_url:
            embed.set_image(url=gif_url)
        await ctx.send(embed=embed)

    @love.command(name="kiss")
    async def kiss(self, ctx, member: discord.Member):
        await self._send_roleplay(ctx, member, "kiss", f"💋 **{ctx.author.display_name}** vừa trao một nụ hôn nồng cháy cho **{member.display_name}**!")

    @love.command(name="hug")
    async def hug(self, ctx, member: discord.Member):
        await self._send_roleplay(ctx, member, "hug", f"🫂 **{ctx.author.display_name}** ôm chặt **{member.display_name}** vào lòng vô cùng ấm áp!")

    @love.command(name="pat")
    async def pat(self, ctx, member: discord.Member):
        await self._send_roleplay(ctx, member, "pat", f"🫳 **{ctx.author.display_name}** nhẹ nhàng xoa đầu **{member.display_name}** thật dịu dàng!")

    @love.command(name="slap")
    async def slap(self, ctx, member: discord.Member):
        await self._send_roleplay(ctx, member, "slap", f"🖐️ **{ctx.author.display_name}** đã tát **{member.display_name}** một cái đôm đốp! 😱")

    @love.command(name="cuddle")
    async def cuddle(self, ctx, member: discord.Member):
        await self._send_roleplay(ctx, member, "cuddle", f"🧸 **{ctx.author.display_name}** nựng nịu và cưng chiều **{member.display_name}**!")

    @love.command(name="feed")
    async def feed(self, ctx, member: discord.Member):
        await self._send_roleplay(ctx, member, "feed", f"🍰 **{ctx.author.display_name}** đút cho **{member.display_name}** ăn từng miếng ngọt ngào!")

    # --- XỬ LÝ LỖI HỒI CHIÊU & QUYỀN HẠN ---
    # Lưu ý: discord.py vẫn dispatch ``command_error`` sau khi chạy handler cục bộ,
    # nên ta đánh dấu ``ctx.kai_error_handled`` để handler tập trung ở ``main.py``
    # không gửi tin nhắn lần thứ hai cho cùng một lỗi.
    @work.error
    @care.error
    @date.error
    async def cooldown_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            until = int((datetime.now() + timedelta(seconds=error.retry_after)).timestamp())
            await ctx.send(f"⏳ Tính năng này đang trong thời gian hồi chiêu! Vui lòng thử lại vào <t:{until}:R>.")
            ctx.kai_error_handled = True

    @createcode.error
    async def admin_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Bạn phải là Administrator mới có quyền dùng lệnh này!")
            ctx.kai_error_handled = True

async def setup(bot):
    await bot.add_cog(LoveSystem(bot))

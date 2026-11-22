import json
import os
from datetime import datetime

import aiohttp
import aiosqlite
import discord
from discord.ext import commands

from kai_core import get_logger
from kai_core.config import CONFIG

log = get_logger("database")


class GroupDatabase(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Phải nằm trong KAI_DATA_DIR: đường dẫn tương đối sẽ ghi ra
        # thư mục chạy và MẤT SẠCH mỗi lần Render deploy lại container.
        self.db_path = str(CONFIG.data_dir / "group_data.db")
        self.logger = log

    async def cog_load(self):
        """Khởi tạo Database an toàn khi Cog được nạp"""
        await self.setup_database()

    async def setup_database(self):
        async with aiosqlite.connect(self.db_path) as db:
            # Bảng thành viên
            await db.execute('''
                CREATE TABLE IF NOT EXISTS members (
                    discord_id INTEGER PRIMARY KEY,
                    badge_number TEXT,
                    name TEXT,
                    unit TEXT,
                    position TEXT,
                    rank TEXT,
                    points INTEGER DEFAULT 0,
                    mission_status TEXT DEFAULT 'Chưa hoàn thành',
                    roblox_username TEXT,
                    join_date TEXT
                )
            ''')

            # Bảng quản trị viên (Mod)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS mods (
                    discord_id INTEGER PRIMARY KEY
                )
            ''')

            # Bảng cài đặt hệ thống (Lưu kênh duyệt)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')

            # Bảng hàng đợi duyệt yêu cầu
            await db.execute('''
                CREATE TABLE IF NOT EXISTS pending_requests (
                    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    type TEXT,
                    data TEXT,
                    status TEXT DEFAULT 'pending'
                )
            ''')

            # Tạo index để query tốc độ cao cho bảng xếp hạng
            await db.execute("CREATE INDEX IF NOT EXISTS idx_points ON members(points DESC)")
            await db.commit()

    async def check_is_mod_user(self, user):
        """Hàm kiểm tra quyền độc lập dùng cho cả Command và Interaction."""
        permissions = getattr(user, "guild_permissions", None)
        if permissions is not None and permissions.administrator:
            return True

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT discord_id FROM mods WHERE discord_id = ?", (user.id,)) as cursor:
                result = await cursor.fetchone()

        if result:
            return True
        return False

    async def is_mod(self, ctx):
        """Hàm kiểm tra quyền cho Lệnh (Command)"""
        is_authorized = await self.check_is_mod_user(ctx.author)
        if is_authorized:
            return True

        await ctx.send("❌ **Từ chối truy cập:** Bạn không có quyền quản trị Database để sử dụng lệnh này trực tiếp! Nếu bạn là người dùng, hãy làm theo hướng dẫn gửi yêu cầu để được duyệt.")
        return False

    def get_level(self, points):
        """Hệ thống Đẳng cấp"""
        if points < 50:
            return "Tân Binh (Hạng F)"
        elif points < 150:
            return "Binh Nhì (Hạng E)"
        elif points < 300:
            return "Binh Nhất (Hạng D)"
        elif points < 500:
            return "Hạ Sĩ (Hạng C)"
        elif points < 1000:
            return "Trung Sĩ (Hạng B)"
        elif points < 2000:
            return "Thượng Sĩ (Hạng A)"
        else:
            return "Sĩ Quan Chỉ Huy (Hạng S)"

    async def fetch_roblox_info(self, username: str):
        """API lấy thông tin Roblox an toàn"""
        try:
            async with aiohttp.ClientSession() as session:
                url_id = "https://users.roblox.com/v1/usernames/users"
                async with session.post(url_id, json={"usernames": [username]}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("data"):
                            user_id = data["data"][0]["id"]
                            display_name = data["data"][0]["displayName"]

                            url_avatar = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}&size=420x420&format=Png&isCircular=false"
                            async with session.get(url_avatar) as av_resp:
                                av_data = await av_resp.json()
                                avatar_url = av_data["data"][0]["imageUrl"] if av_data.get("data") else None

                            return {"id": user_id, "display_name": display_name, "avatar_url": avatar_url}
        except Exception:
            pass
        return None

    # ==========================================
    # HỆ THỐNG XỬ LÝ NÚT BẤM (DUYỆT / TỪ CHỐI)
    # ==========================================
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Lắng nghe sự kiện click nút bấm vĩnh viễn (Persistent Buttons)"""
        if interaction.type != discord.InteractionType.component:
            return

        component = interaction.data or {}
        custom_id = str(component.get("custom_id", ""))
        if not (custom_id.startswith("req_approve_") or custom_id.startswith("req_reject_")):
            return

        # Kiểm tra người ấn nút có phải là Mod không
        if not await self.check_is_mod_user(interaction.user):
            return await interaction.response.send_message(
                "❌ Chỉ có Quản trị viên hoặc Mod mới có thể duyệt/từ chối yêu cầu này!",
                ephemeral=True,
            )

        if interaction.guild is None:
            return await interaction.response.send_message(
                "❌ Chỉ sử dụng được trong server.", ephemeral=True
            )

        try:
            request_id = int(custom_id.rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            return await interaction.response.send_message(
                "⚠️ Yêu cầu không hợp lệ (mã không đúng định dạng).", ephemeral=True
            )
        action = "approve" if custom_id.startswith("req_approve_") else "reject"

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT user_id, type, data, status FROM pending_requests WHERE request_id = ?", (request_id,)) as cursor:
                req = await cursor.fetchone()

            if not req or req[3] != 'pending':
                await interaction.response.send_message("⚠️ Yêu cầu này đã được xử lý hoặc không tồn tại!", ephemeral=True)
                return

            user_id, req_type, data_json, _ = req
            try:
                data = json.loads(data_json) if data_json else {}
            except (TypeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            join_date = datetime.now().strftime("%d/%m/%Y")
            target_user = interaction.guild.get_member(user_id)
            user_mention = f"<@{user_id}>" if not target_user else target_user.mention

            if action == "approve":
                if req_type == "register":
                    # Xóa bản ghi cũ nếu có để ghi đè (hoặc Update tùy logic)
                    await db.execute("DELETE FROM members WHERE discord_id = ?", (user_id,))
                    await db.execute('''
                        INSERT INTO members (discord_id, badge_number, name, unit, position, rank, points, mission_status, join_date)
                        VALUES (?, ?, ?, ?, ?, ?, 0, 'Chưa hoàn thành', ?)
                    ''', (
                        user_id,
                        data.get('badge', 'N/A'),
                        data.get('name', 'N/A'),
                        data.get('unit', 'N/A'),
                        data.get('pos', 'N/A'),
                        data.get('rank', 'Hạng F'),
                        join_date,
                    ))
                    result_msg = f"✅ Đã **DUYỆT** hồ sơ đăng ký cho {user_mention}."

                elif req_type == "linkroblox":
                    # Kiểm tra xem user đã có profile chưa
                    roblox = str(data.get('roblox', '')).strip()
                    async with db.execute("SELECT discord_id FROM members WHERE discord_id = ?", (user_id,)) as c:
                        exists = await c.fetchone()
                    if exists:
                        await db.execute("UPDATE members SET roblox_username = ? WHERE discord_id = ?", (roblox, user_id))
                    else:
                        await db.execute('''
                            INSERT INTO members (discord_id, badge_number, name, unit, position, rank, points, roblox_username, join_date)
                            VALUES (?, 'Tân Binh', 'Chưa rõ', 'Chưa có', 'Lính mới', 'Hạng F', 0, ?, ?)
                        ''', (user_id, roblox, join_date))
                    result_msg = f"✅ Đã **DUYỆT** liên kết Roblox `{roblox}` cho {user_mention}."
                else:
                    result_msg = f"⚠️ Loại yêu cầu `{req_type}` không được hỗ trợ."

                await db.execute("UPDATE pending_requests SET status = 'approved' WHERE request_id = ?", (request_id,))

            elif action == "reject":
                await db.execute("UPDATE pending_requests SET status = 'rejected' WHERE request_id = ?", (request_id,))
                result_msg = f"❌ Đã **TỪ CHỐI** yêu cầu của {user_mention}."
            else:
                result_msg = "⚠️ Hành động không hợp lệ."

            await db.commit()

        # Chỉnh sửa tin nhắn Embed ban đầu để vô hiệu hóa nút bấm
        message = interaction.message
        embed = (
            message.embeds[0]
            if message is not None and message.embeds
            else discord.Embed(title="Yêu cầu duyệt hồ sơ")
        )
        embed.color = discord.Color.green() if action == "approve" else discord.Color.red()
        embed.add_field(
            name="Trạng thái",
            value=f"{'✅ Đã duyệt' if action == 'approve' else '❌ Đã từ chối'} bởi {interaction.user.mention}",
            inline=False,
        )

        # Xóa các nút bấm (Gửi view rỗng)
        try:
            await interaction.response.edit_message(embed=embed, view=None)
        except discord.HTTPException as exc:
            log.warning("Không cập nhật được tin nhắn duyệt: %s", exc)



        # Gửi thông báo chung trong kênh duyệt.
        if result_msg and interaction.channel is not None:
            try:
                await interaction.channel.send(result_msg)
            except discord.HTTPException as exc:
                log.warning("Không gửi được thông báo duyệt: %s", exc)

        # Thông báo riêng cho người gửi yêu cầu (bỏ qua nếu họ chặn DM).
        if target_user is not None:
            try:
                await target_user.send(f"🔔 **Thông báo từ hệ thống:** Yêu cầu của bạn đã được {interaction.user.mention} **{'DUYỆT' if action == 'approve' else 'TỪ CHỐI'}**.")
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ==========================================
    # PHẦN 1: QUẢN TRỊ VIÊN (ADMIN)
    # ==========================================

    @commands.command(name="mod")
    @commands.has_permissions(administrator=True)
    async def add_mod(self, ctx, member: discord.Member):
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute("INSERT INTO mods (discord_id) VALUES (?)", (member.id,))
                await db.commit()
                await ctx.send(f"✅ Đã cấp quyền Mod Database cho {member.mention}.")
            except aiosqlite.IntegrityError:
                await ctx.send(f"⚠️ {member.mention} đã là Mod từ trước!")

    @commands.command(name="unmod")
    @commands.has_permissions(administrator=True)
    async def remove_mod(self, ctx, member: discord.Member):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM mods WHERE discord_id = ?", (member.id,))
            await db.commit()
        await ctx.send(f"✅ Đã thu hồi quyền Mod Database của {member.mention}.")

    @commands.command(name="setchannel")
    @commands.has_permissions(administrator=True)
    async def set_approval_channel(self, ctx, channel: discord.TextChannel):
        """Cài đặt kênh gửi yêu cầu duyệt (Đăng ký, Liên kết)"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("REPLACE INTO settings (key, value) VALUES ('approval_channel', ?)", (str(channel.id),))
            await db.commit()
        await ctx.send(f"✅ Đã cài đặt kênh duyệt yêu cầu thành công tại {channel.mention}.")

    @commands.command(name="backup")
    @commands.has_permissions(administrator=True)
    async def backup_db(self, ctx):
        if not os.path.isfile(self.db_path):
            return await ctx.send("❌ Chưa có file dữ liệu để sao lưu.")
        await ctx.send("📦 Đang trích xuất dữ liệu cơ sở...", file=discord.File(self.db_path))

    # ==========================================
    # PHẦN 2: MODERATOR & GỬI YÊU CẦU DUYỆT
    # ==========================================

    async def get_approval_channel(self, guild):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT value FROM settings WHERE key = 'approval_channel'") as cursor:
                res = await cursor.fetchone()
        if res:
            return guild.get_channel(int(res[0]))
        return None

    @commands.command(name="register", aliases=["reg"])
    async def register_member(self, ctx, member: discord.Member, badge_number: str, name: str, unit: str, position: str, rank: str):
        """Đăng ký hồ sơ. Nếu là Mod: Đăng ký thẳng. Nếu là User: Gửi yêu cầu chờ duyệt."""
        is_mod_user = await self.check_is_mod_user(ctx.author)

        # Trạng thái 1: MOD sử dụng lệnh -> Đăng ký ngay lập tức
        if is_mod_user:
            join_date = datetime.now().strftime("%d/%m/%Y")
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM members WHERE discord_id = ?", (member.id,))
                await db.execute('''
                    INSERT INTO members (discord_id, badge_number, name, unit, position, rank, points, mission_status, join_date)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 'Chưa hoàn thành', ?)
                ''', (member.id, badge_number, name, unit, position, rank, join_date))
                await db.commit()

            embed = discord.Embed(title="✅ ĐĂNG KÝ THÀNH CÔNG (MOD FORCE)", color=discord.Color.green())
            embed.add_field(name="Thành viên", value=member.mention, inline=False)
            embed.add_field(name="Thông tin", value=f"**Mã:** {badge_number}\n**Tên:** {name}\n**Hàm:** {rank}", inline=True)
            embed.add_field(name="Đơn vị", value=f"**Đơn vị:** {unit}\n**Chức vụ:** {position}", inline=True)
            await ctx.send(embed=embed)
            return

        # Trạng thái 2: Người dùng bình thường sử dụng -> Gửi vào Kênh duyệt
        # (Lưu ý: Bạn có thể chỉ định ctx.author thay vì member để họ tự đăng ký cho chính họ)
        if ctx.author != member:
            return await ctx.send("❌ Bạn chỉ có thể tự gửi yêu cầu đăng ký cho chính mình! Hãy dùng lệnh: `!reg @tag_chính_bạn ...`")

        approval_channel = await self.get_approval_channel(ctx.guild)
        if not approval_channel:
            return await ctx.send("⚠️ Ban quản trị chưa thiết lập Kênh Duyệt. Xin hãy liên hệ Admin!")

        # Lưu vào DB Hàng đợi
        req_data = json.dumps({
            "badge": badge_number, "name": name, "unit": unit, "pos": position, "rank": rank
        })

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("INSERT INTO pending_requests (user_id, type, data) VALUES (?, 'register', ?)", (member.id, req_data))
            req_id = cursor.lastrowid
            await db.commit()

        # Tạo Embed gửi vào kênh duyệt
        embed = discord.Embed(title="📝 YÊU CẦU ĐĂNG KÝ HỒ SƠ", color=discord.Color.gold())
        embed.add_field(name="Người gửi", value=member.mention, inline=False)
        embed.add_field(name="Nội dung đăng ký", value=f"**Mã số:** {badge_number}\n**Tên:** {name}\n**Đơn vị:** {unit}\n**Chức vụ:** {position}\n**Cấp bậc:** {rank}")
        embed.set_footer(text=f"Mã yêu cầu: #{req_id} | Chờ phê duyệt")

        # Tạo Nút bấm
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Duyệt", style=discord.ButtonStyle.success, custom_id=f"req_approve_{req_id}"))
        view.add_item(discord.ui.Button(label="Từ chối", style=discord.ButtonStyle.danger, custom_id=f"req_reject_{req_id}"))

        await approval_channel.send(embed=embed, view=view)
        await ctx.send(f"✅ Yêu cầu đăng ký của bạn đã được gửi tới kênh {approval_channel.mention}. Vui lòng chờ Mod xét duyệt!")


    @commands.command(name="linkroblox")
    async def link_roblox(self, ctx, roblox_username: str):
        """Người dùng tự liên kết Roblox (Yêu cầu qua hệ thống duyệt)"""
        approval_channel = await self.get_approval_channel(ctx.guild)
        if not approval_channel:
            return await ctx.send("⚠️ Ban quản trị chưa thiết lập Kênh Duyệt. Admin cần sử dụng lệnh `!setchannel` trước!")

        req_data = json.dumps({"roblox": roblox_username})

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("INSERT INTO pending_requests (user_id, type, data) VALUES (?, 'linkroblox', ?)", (ctx.author.id, req_data))
            req_id = cursor.lastrowid
            await db.commit()

        embed = discord.Embed(title="🎮 YÊU CẦU LIÊN KẾT ROBLOX", color=discord.Color.blue())
        embed.add_field(name="Thành viên", value=ctx.author.mention, inline=False)
        embed.add_field(name="Username Roblox", value=f"`{roblox_username}`", inline=False)
        embed.set_footer(text=f"Mã yêu cầu: #{req_id} | Chờ phê duyệt")

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Duyệt Link", style=discord.ButtonStyle.success, custom_id=f"req_approve_{req_id}"))
        view.add_item(discord.ui.Button(label="Từ chối", style=discord.ButtonStyle.danger, custom_id=f"req_reject_{req_id}"))

        await approval_channel.send(embed=embed, view=view)
        await ctx.send(f"✅ Yêu cầu liên kết tài khoản `{roblox_username}` đã được gửi chờ xét duyệt!")

    # ==========================================
    # CÁC LỆNH QUẢN LÝ (CHỈ DÀNH CHO MOD TRỞ LÊN)
    # ==========================================

    @commands.command(name="update")
    async def update_member(self, ctx, member: discord.Member, column: str, *, new_value: str):
        if not await self.is_mod(ctx):
            return

        valid_columns = ("badge_number", "name", "unit", "position", "rank", "roblox_username")
        key = column.strip().lower()
        if key not in valid_columns:
            return await ctx.send(f"❌ Cột không hợp lệ! Hãy chọn 1 trong: `{', '.join(valid_columns)}`")

        # ``key`` đã nằm trong whitelist nên an toàn khi nội suy vào câu SQL.
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT 1 FROM members WHERE discord_id = ?", (member.id,))
            if await cursor.fetchone() is None:
                return await ctx.send(f"❌ {member.mention} chưa có hồ sơ trong hệ thống.")
            await db.execute(f"UPDATE members SET {key} = ? WHERE discord_id = ?", (new_value, member.id))
            await db.commit()
        await ctx.send(f"✅ Đã cập nhật **{key}** của {member.mention} thành: `{new_value}`")

    @commands.command(name="addpoint", aliases=["ap"])
    async def add_points(self, ctx, member: discord.Member, points: int):
        if not await self.is_mod(ctx):
            return

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT points FROM members WHERE discord_id = ?", (member.id,)) as cursor:
                result = await cursor.fetchone()

            if result is None:
                return await ctx.send(f"❌ Không tìm thấy {member.mention} trong hệ thống.")

            if abs(points) > 1_000_000:
                return await ctx.send("❌ Mỗi lần chỉ được cộng/trừ tối đa **1,000,000** điểm.")

            new_points = max(0, min(1_000_000_000, result[0] + points))
            await db.execute("UPDATE members SET points = ? WHERE discord_id = ?", (new_points, member.id))
            await db.commit()

        action = "Cộng" if points > 0 else "Trừ"
        await ctx.send(f"📈 Đã **{action} {abs(points)} điểm** cho {member.mention}. (Tổng: **{new_points}**)")

    @commands.command(name="mission")
    async def toggle_mission(self, ctx, member: discord.Member):
        if not await self.is_mod(ctx):
            return

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT mission_status FROM members WHERE discord_id = ?", (member.id,)) as cursor:
                result = await cursor.fetchone()

            if result is None:
                return await ctx.send(f"❌ Không tìm thấy {member.mention} trong DB.")

            new_status = "Hoàn thành" if result[0] == "Chưa hoàn thành" else "Chưa hoàn thành"
            await db.execute("UPDATE members SET mission_status = ? WHERE discord_id = ?", (new_status, member.id))
            await db.commit()

        icon = "🟢" if new_status == "Hoàn thành" else "🔴"
        await ctx.send(f"{icon} Đã cập nhật trạng thái nhiệm vụ của {member.mention} thành: **{new_status}**")

    @commands.command(name="checkrole")
    async def check_role(self, ctx, role: discord.Role):
        if not await self.is_mod(ctx):
            return

        members_in_role = role.members
        if not members_in_role:
            return await ctx.send(f"⚠️ Không có ai đang sở hữu role **{role.name}**.")

        registered, unregistered = [], []
        await ctx.send(f"🔍 Đang quét {len(members_in_role)} thành viên có role {role.name}...")

        async with aiosqlite.connect(self.db_path) as db:
            for member in members_in_role:
                async with db.execute("SELECT name, rank FROM members WHERE discord_id = ?", (member.id,)) as cursor:
                    res = await cursor.fetchone()
                    if res:
                        registered.append(f"✅ {member.mention} - {res[1]} {res[0]}")
                    else:
                        unregistered.append(f"❌ {member.mention}")

        embed = discord.Embed(title=f"📊 THỐNG KÊ ROLE: {role.name}", color=role.color)
        reg_text = "\n".join(registered) if registered else "Không có ai."
        unreg_text = "\n".join(unregistered) if unregistered else "Không có ai."

        if len(reg_text) > 1024:
            reg_text = reg_text[:1000] + "...\n*(Hiển thị một phần)*"
        if len(unreg_text) > 1024:
            unreg_text = unreg_text[:1000] + "...\n*(Hiển thị một phần)*"

        embed.add_field(name=f"Đã Đăng Ký ({len(registered)})", value=reg_text, inline=False)
        embed.add_field(name=f"Chưa Đăng Ký ({len(unregistered)})", value=unreg_text, inline=False)
        await ctx.send(embed=embed)

    # ==========================================
    # PHẦN 3: XEM THÔNG TIN CÁ NHÂN & CHUNG
    # ==========================================

    @commands.command(name="profile", aliases=["p"])
    async def view_profile(self, ctx, member: discord.Member = None):
        target = member or ctx.author

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT badge_number, name, unit, position, rank, points, mission_status, roblox_username, join_date FROM members WHERE discord_id = ?", (target.id,)) as cursor:
                result = await cursor.fetchone()

        if result is None:
            return await ctx.send(f"📭 Hồ sơ của {target.mention} chưa được ghi nhận hoặc chưa được phê duyệt.")

        badge, name, unit, position, rank, points, mission_status, roblox_user, join_date = result
        level = self.get_level(points)
        mission_icon = "🟢" if mission_status == "Hoàn thành" else "🔴"

        embed = discord.Embed(title=f"HỒ SƠ QUÂN NHÂN: {(name or 'KHÔNG RÕ').upper()}", color=discord.Color.blurple())
        embed.set_thumbnail(url=target.display_avatar.url if target.display_avatar else None)

        embed.add_field(name="Mã Số", value=f"`{badge}`", inline=True)
        embed.add_field(name="Đơn vị", value=f"**{unit}**", inline=True)
        embed.add_field(name="Quân hàm", value=f"**{rank}**", inline=True)
        embed.add_field(name="Chức vụ", value=f"{position}", inline=True)
        embed.add_field(name="Thành tích", value=f"🏆 Điểm: **{points}**\n⭐ Hạng: **{level}**", inline=True)
        embed.add_field(name="Nhiệm vụ", value=f"{mission_icon} {mission_status}", inline=True)

        if roblox_user:
            noti_msg = await ctx.send(f"🔍 Đang truy xuất dữ liệu Roblox của `{roblox_user}`...")
            rbx_data = await self.fetch_roblox_info(roblox_user)
            try:
                await noti_msg.delete()
            except (discord.Forbidden, discord.HTTPException):
                self.logger.debug("Không xoá được thông báo Roblox của %s", ctx.author.id)

            if rbx_data:
                rbx_text = f"**Tên hiển thị:** {rbx_data['display_name']}\n**ID Roblox:** `{rbx_data['id']}`\n[🔗 Bấm xem Profile](https://www.roblox.com/users/{rbx_data['id']}/profile)"
                if rbx_data['avatar_url']:
                    embed.set_image(url=rbx_data['avatar_url'])
            else:
                rbx_text = f"❌ Không thể tìm thấy user `{roblox_user}` trên Roblox."
            embed.add_field(name="🎮 Tài Khoản Roblox", value=rbx_text, inline=False)
        else:
            embed.add_field(name="🎮 Tài Khoản Roblox", value="*Chưa liên kết.*", inline=False)

        embed.set_footer(text=f"Ngày gia nhập hệ thống: {join_date}")
        await ctx.send(embed=embed)

    @commands.command(name="top")
    async def leaderboard(self, ctx):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT name, rank, points, discord_id FROM members ORDER BY points DESC LIMIT 10") as cursor:
                top_users = await cursor.fetchall()

        if not top_users:
            return await ctx.send("📭 Hệ thống chưa có dữ liệu thành viên.")

        embed = discord.Embed(title="🏆 BẢNG XẾP HẠNG THÀNH TÍCH", color=discord.Color.gold())
        leaderboard_text = ""
        for index, (name, rank, points, user_id) in enumerate(top_users, start=1):
            medal = "🥇" if index == 1 else "🥈" if index == 2 else "🥉" if index == 3 else f"`#{index}`"
            leaderboard_text += f"{medal} **{rank} {name}** (<@{user_id}>) - **{points}** điểm\n"

        embed.description = leaderboard_text
        await ctx.send(embed=embed)

    @commands.command(name="bot")
    async def show_help(self, ctx):
        embed = discord.Embed(
            title="🛠️ HỆ THỐNG LỆNH CỦA BOT QUẢN LÝ",
            description="Lưu ý: Nếu thông tin chứa dấu cách, hãy bọc trong ngoặc kép `\" \"`.",
            color=discord.Color.teal()
        )
        embed.set_thumbnail(url=self.bot.user.display_avatar.url if self.bot.user.display_avatar else None)

        user_cmds = """
        🔹 `!reg @tag_chinh_ban <mã> <tên> <đơn vị> <chức vụ> <hàm>` - Gửi yêu cầu đăng ký hồ sơ mới (Chờ duyệt).
        🔹 `!linkroblox <tên>` - Gửi yêu cầu liên kết acc Roblox (Chờ duyệt).
        🔹 `!p [@tag]` - Xem hồ sơ của bạn hoặc người khác.
        🔹 `!top` - Xem bảng xếp hạng 10 người điểm cao nhất.
        """
        embed.add_field(name="👤 DÀNH CHO THÀNH VIÊN", value=user_cmds, inline=False)

        mod_cmds = """
        🔸 `!reg @tag ...` - Nếu là Mod dùng lệnh này, sẽ đăng ký NGAY LẬP TỨC bỏ qua bước duyệt.
        🔸 `!update @tag <cột> <nội dung>` - Sửa dữ liệu (badge_number, name, unit, position, rank, roblox_username).
        🔸 `!ap @tag <số_điểm>` - Cộng / trừ điểm thành tích.
        🔸 `!mission @tag` - Đánh dấu Hoàn thành nhiệm vụ.
        🔸 `!checkrole @Role` - Quét những người có Role đã đăng ký hay chưa.
        """
        embed.add_field(name="🛡️ DÀNH CHO MODERATOR", value=mod_cmds, inline=False)

        admin_cmds = """
        👑 `!mod @tag` / `!unmod @tag` - Cấp/Thu hồi quyền Mod Bot.
        👑 `!setchannel #kenh-duyet` - Cài đặt nơi hệ thống gửi các nút bấm duyệt/từ chối.
        👑 `!backup` - Tải xuống file Database.
        """
        embed.add_field(name="👑 DÀNH CHO QUẢN TRỊ VIÊN", value=admin_cmds, inline=False)

        embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None)
        await ctx.send(embed=embed)

    # ==========================================
    # XỬ LÝ LỖI
    # ==========================================
    # Việc báo lỗi cho người dùng được TẬP TRUNG tại ``main.py``
    # (Bot.on_command_error). Trước đây cog này đăng ký thêm một listener
    # ``on_command_error`` khiến mỗi lỗi bị thông báo hai lần — đã bỏ.

async def setup(bot):
    await bot.add_cog(GroupDatabase(bot))

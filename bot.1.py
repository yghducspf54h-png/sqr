import os
import sqlite3
from datetime import datetime, timezone, timedelta, time as dtime

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# =======================
# ENV
# =======================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "")
if not TOKEN:
    raise SystemExit("❌ حط DISCORD_TOKEN داخل .env")

# Riyadh fixed offset (Saudi no DST)
RIYADH_TZ = timezone(timedelta(hours=3))

# =======================
# INTENTS
# =======================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.messages = True
intents.voice_states = True

# ملاحظة: Message Content Intent "يفضل" من البورتال
# لو فعلته خله True هنا:
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =======================
# DB
# =======================
DB_PATH = "staff_duty.db"

def db():
    return sqlite3.connect(DB_PATH)

def add_column_if_missing(con, table: str, col: str, col_type: str):
    cur = con.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if col not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")

def init_db():
    with db() as con:
        # settings
        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            staff_role_id INTEGER DEFAULT 0,
            onduty_role_id INTEGER DEFAULT 0,
            log_channel_id INTEGER DEFAULT 0,
            weekly_channel_id INTEGER DEFAULT 0,
            staff_week_role_id INTEGER DEFAULT 0,
            alert_channel_id INTEGER DEFAULT 0,
            auto_out_hours INTEGER DEFAULT 6,
            last_weekly_key TEXT DEFAULT ''
        )
        """)

        # ترقيات لو DB قديمة
        add_column_if_missing(con, "settings", "weekly_channel_id", "INTEGER DEFAULT 0")
        add_column_if_missing(con, "settings", "staff_week_role_id", "INTEGER DEFAULT 0")
        add_column_if_missing(con, "settings", "alert_channel_id", "INTEGER DEFAULT 0")
        add_column_if_missing(con, "settings", "auto_out_hours", "INTEGER DEFAULT 6")
        add_column_if_missing(con, "settings", "last_weekly_key", "TEXT DEFAULT ''")

        # active duty includes shift
        con.execute("""
        CREATE TABLE IF NOT EXISTS active_duty (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            start_ts INTEGER NOT NULL,
            shift TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """)
        # لو كانت قديمة بدون shift
        add_column_if_missing(con, "active_duty", "shift", "TEXT NOT NULL DEFAULT 'Support'")

        # duty sessions (with shift)
        con.execute("""
        CREATE TABLE IF NOT EXISTS duty_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER NOT NULL,
            duration_sec INTEGER NOT NULL,
            shift TEXT NOT NULL
        )
        """)
        add_column_if_missing(con, "duty_sessions", "shift", "TEXT NOT NULL DEFAULT 'Support'")

        # message stats daily
        con.execute("""
        CREATE TABLE IF NOT EXISTS msg_daily (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day_key TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, day_key)
        )
        """)

        # voice stats (active sessions)
        con.execute("""
        CREATE TABLE IF NOT EXISTS voice_active (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            join_ts INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """)

        # voice daily totals
        con.execute("""
        CREATE TABLE IF NOT EXISTS voice_daily (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day_key TEXT NOT NULL,
            voice_sec INTEGER NOT NULL DEFAULT 0,
            joins INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, day_key)
        )
        """)

        con.commit()

def ensure_guild(guild_id: int):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (guild_id,))
        con.commit()

def get_settings(guild_id: int) -> dict:
    ensure_guild(guild_id)
    with db() as con:
        cur = con.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,))
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))

def set_setting(guild_id: int, key: str, value):
    ensure_guild(guild_id)
    with db() as con:
        con.execute(f"UPDATE settings SET {key}=? WHERE guild_id=?", (value, guild_id))
        con.commit()

def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def riyadh_now() -> datetime:
    return datetime.now(RIYADH_TZ)

def day_key_riyadh(dt: datetime | None = None) -> str:
    if dt is None:
        dt = riyadh_now()
    return dt.strftime("%Y-%m-%d")  # based on Riyadh date

def fmt_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"

# =======================
# Roles / checks
# =======================
def is_admin(inter: discord.Interaction) -> bool:
    return inter.user.guild_permissions.administrator

def get_roles(guild: discord.Guild):
    s = get_settings(guild.id)
    staff_role = guild.get_role(int(s["staff_role_id"] or 0))
    onduty_role = guild.get_role(int(s["onduty_role_id"] or 0))
    staff_week_role = guild.get_role(int(s["staff_week_role_id"] or 0))
    return staff_role, onduty_role, staff_week_role

def is_staff_member(member: discord.Member, staff_role: discord.Role | None) -> bool:
    return bool(staff_role and staff_role in member.roles)

def is_onduty(member: discord.Member, onduty_role: discord.Role | None) -> bool:
    return bool(onduty_role and onduty_role in member.roles)

async def send_log(guild: discord.Guild, text: str):
    s = get_settings(guild.id)
    ch_id = int(s["log_channel_id"] or 0)
    if ch_id == 0:
        return
    ch = guild.get_channel(ch_id)
    if isinstance(ch, discord.TextChannel):
        await ch.send(text)

# =======================
# Duty DB
# =======================
def set_active_duty(guild_id: int, user_id: int, start_ts: int, shift: str):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO active_duty (guild_id, user_id, start_ts, shift) VALUES (?,?,?,?)",
            (guild_id, user_id, start_ts, shift),
        )
        con.commit()

def get_active_duty(guild_id: int, user_id: int):
    with db() as con:
        cur = con.execute(
            "SELECT start_ts, shift FROM active_duty WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        return int(row[0]), str(row[1])

def clear_active_duty(guild_id: int, user_id: int):
    with db() as con:
        con.execute("DELETE FROM active_duty WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        con.commit()

def add_duty_session(guild_id: int, user_id: int, start_ts: int, end_ts: int, shift: str) -> int:
    dur = max(0, end_ts - start_ts)
    with db() as con:
        con.execute(
            "INSERT INTO duty_sessions (guild_id, user_id, start_ts, end_ts, duration_sec, shift) VALUES (?,?,?,?,?,?)",
            (guild_id, user_id, start_ts, end_ts, dur, shift),
        )
        con.commit()
    return dur

def duty_weekly_totals(guild_id: int, since_ts: int):
    # returns dict user_id -> (total_sec, sessions_count)
    with db() as con:
        cur = con.execute(
            """
            SELECT user_id, SUM(duration_sec) AS total, COUNT(*) AS sessions
            FROM duty_sessions
            WHERE guild_id=? AND end_ts>=?
            GROUP BY user_id
            """,
            (guild_id, since_ts),
        )
        out = {}
        for uid, total, sessions in cur.fetchall():
            out[int(uid)] = (int(total or 0), int(sessions or 0))
        return out

# =======================
# Message stats
# =======================
def inc_msg(guild_id: int, user_id: int, day_key: str):
    with db() as con:
        con.execute(
            """
            INSERT INTO msg_daily (guild_id, user_id, day_key, count)
            VALUES (?,?,?,1)
            ON CONFLICT(guild_id, user_id, day_key)
            DO UPDATE SET count=count+1
            """,
            (guild_id, user_id, day_key),
        )
        con.commit()

def msg_weekly_total(guild_id: int, since_day_key: str):
    # since_day_key inclusive, day_key is YYYY-MM-DD
    with db() as con:
        cur = con.execute(
            """
            SELECT user_id, SUM(count) AS total
            FROM msg_daily
            WHERE guild_id=? AND day_key>=?
            GROUP BY user_id
            """,
            (guild_id, since_day_key),
        )
        return {int(uid): int(total or 0) for uid, total in cur.fetchall()}

# =======================
# Voice stats
# =======================
def voice_join(guild_id: int, user_id: int, join_ts: int):
    with db() as con:
        # mark active
        con.execute(
            "INSERT OR REPLACE INTO voice_active (guild_id, user_id, join_ts) VALUES (?,?,?)",
            (guild_id, user_id, join_ts),
        )
        con.commit()

def voice_leave(guild_id: int, user_id: int, leave_ts: int):
    with db() as con:
        cur = con.execute(
            "SELECT join_ts FROM voice_active WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        row = cur.fetchone()
        if not row:
            return 0
        join_ts = int(row[0])
        con.execute("DELETE FROM voice_active WHERE guild_id=? AND user_id=?", (guild_id, user_id))

        # add to daily voice totals (based on Riyadh day when leaving)
        dur = max(0, leave_ts - join_ts)
        dk = day_key_riyadh()
        con.execute(
            """
            INSERT INTO voice_daily (guild_id, user_id, day_key, voice_sec, joins)
            VALUES (?,?,?,?,1)
            ON CONFLICT(guild_id, user_id, day_key)
            DO UPDATE SET voice_sec=voice_sec+excluded.voice_sec, joins=joins+1
            """,
            (guild_id, user_id, dk, dur),
        )
        con.commit()
        return dur

def voice_weekly_total(guild_id: int, since_day_key: str):
    with db() as con:
        cur = con.execute(
            """
            SELECT user_id, SUM(voice_sec) AS vsec, SUM(joins) AS joins
            FROM voice_daily
            WHERE guild_id=? AND day_key>=?
            GROUP BY user_id
            """,
            (guild_id, since_day_key),
        )
        return {int(uid): (int(vsec or 0), int(joins or 0)) for uid, vsec, joins in cur.fetchall()}

# =======================
# Dashboard Embed
# =======================
SHIFTS = ["Support", "Chat", "Patrol"]

def build_dashboard_embed(guild: discord.Guild) -> discord.Embed:
    staff_role, onduty_role, staff_week_role = get_roles(guild)

    # build lists by shift from active_duty table
    shift_map = {s: [] for s in SHIFTS}
    if onduty_role:
        with db() as con:
            cur = con.execute("SELECT user_id, shift FROM active_duty WHERE guild_id=?", (guild.id,))
            for uid, sh in cur.fetchall():
                member = guild.get_member(int(uid))
                if member and not member.bot and onduty_role in member.roles:
                    sh = sh if sh in shift_map else "Support"
                    shift_map[sh].append(member)

    for sh in SHIFTS:
        shift_map[sh].sort(key=lambda m: m.display_name.lower())

    e = discord.Embed(
        title="🛡️ لوحة حضور الإدارة",
        description="✅ دخول يفتح اختيار شفت (Support/Chat/Patrol)\n"
                    "🛑 خروج يشيل OnDuty\n"
                    "🚨 طوارئ ينبه OnDuty",
    )
    e.add_field(name="👤 Staff (شكل فقط)", value=staff_role.mention if staff_role else "غير محددة", inline=True)
    e.add_field(name="⚡ OnDuty (صلاحيات)", value=onduty_role.mention if onduty_role else "غير محددة", inline=True)
    e.add_field(name="🏆 Staff of the Week", value=staff_week_role.mention if staff_week_role else "غير محددة", inline=True)

    # show per shift
    for sh in SHIFTS:
        members = shift_map[sh]
        if members:
            text = "\n".join([f"🟢 {m.mention}" for m in members[:20]])
            if len(members) > 20:
                text += f"\n… (+{len(members)-20})"
        else:
            text = "—"
        e.add_field(name=f"📌 المتواجدين الآن — {sh}", value=text, inline=False)

    e.set_footer(text="🔄 تحديث يعيد تحديث القائمة")
    return e

# =======================
# Emergency Modal
# =======================
class EmergencyModal(discord.ui.Modal, title="🚨 تنبيه طوارئ"):
    reason = discord.ui.TextInput(label="السبب", placeholder="وش صار؟", max_length=200)

    async def on_submit(self, inter: discord.Interaction):
        if not inter.guild:
            return await inter.response.send_message("داخل سيرفر فقط.", ephemeral=True)

        staff_role, onduty_role, _ = get_roles(inter.guild)
        member: discord.Member = inter.user  # type: ignore
        if not (is_admin(inter) or is_staff_member(member, staff_role)):
            return await inter.response.send_message("❌ للستاف فقط.", ephemeral=True)

        if not onduty_role:
            return await inter.response.send_message("❌ OnDuty مو محددة.", ephemeral=True)

        s = get_settings(inter.guild.id)
        alert_id = int(s["alert_channel_id"] or 0)

        target_channel = inter.channel
        if alert_id:
            ch = inter.guild.get_channel(alert_id)
            if isinstance(ch, discord.TextChannel):
                target_channel = ch

        embed = discord.Embed(
            title="🚨 نداء طوارئ للإدارة",
            description=f"**السبب:** {self.reason.value}\n**المُرسل:** {inter.user.mention}",
        )
        await target_channel.send(content=onduty_role.mention, embed=embed)
        await send_log(inter.guild, f"🚨 **Emergency** by {inter.user}: {self.reason.value}")
        await inter.response.send_message("✅ تم إرسال نداء الطوارئ.", ephemeral=True)

# =======================
# Shift Picker
# =======================
class ShiftSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Support", description="تذاكر/دعم", value="Support"),
            discord.SelectOption(label="Chat", description="مراقبة الشات", value="Chat"),
            discord.SelectOption(label="Patrol", description="دوريات/مراقبة عامة", value="Patrol"),
        ]
        super().__init__(placeholder="اختر الشفت…", options=options, min_values=1, max_values=1)

    async def callback(self, inter: discord.Interaction):
        view: ShiftPickerView = self.view  # type: ignore
        view.selected_shift = self.values[0]
        await inter.response.edit_message(content=f"✅ اخترت: **{view.selected_shift}** — اضغط Confirm", view=view)

class ShiftPickerView(discord.ui.View):
    def __init__(self, panel_view):
        super().__init__(timeout=45)
        self.selected_shift = "Support"
        self.panel_view = panel_view
        self.add_item(ShiftSelect())

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        # نفذ الدخول الفعلي
        await self.panel_view._do_duty_in(inter, self.selected_shift)
        try:
            await inter.edit_original_response(view=None)
        except Exception:
            pass

# =======================
# Main Panel View
# =======================
class DutyPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _guard_staff(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("داخل سيرفر فقط.", ephemeral=True)
            return None

        staff_role, onduty_role, _ = get_roles(inter.guild)
        if not staff_role or not onduty_role:
            await inter.response.send_message("❌ اللوحة مو مهيأة. خله Admin يسوي /setup_duty.", ephemeral=True)
            return None

        member: discord.Member = inter.user  # type: ignore
        if not is_staff_member(member, staff_role):
            await inter.response.send_message("❌ هذي للستاف فقط.", ephemeral=True)
            return None

        return staff_role, onduty_role

    async def _do_duty_in(self, inter: discord.Interaction, shift: str):
        roles = await self._guard_staff(inter)
        if not roles:
            return
        _, onduty_role = roles
        member: discord.Member = inter.user  # type: ignore

        if is_onduty(member, onduty_role):
            return await inter.followup.send("أنت أصلًا **مداوم** ✅", ephemeral=True)

        try:
            await member.add_roles(onduty_role, reason="Duty IN")
            start = now_ts()
            set_active_duty(inter.guild.id, member.id, start, shift)
            await send_log(inter.guild, f"🟢 **Duty IN**: {member} | shift={shift} | <t:{start}:t>")
            # تحديث اللوحة
            await inter.message.edit(embed=build_dashboard_embed(inter.guild), view=self)
            await inter.followup.send(f"✅ تم تسجيل دخولك (OnDuty) — **{shift}** 🟢", ephemeral=True)
        except discord.Forbidden:
            await inter.followup.send("❌ ما عندي صلاحية أعدل الرتب. ارفع رتبة البوت وفعل Manage Roles.", ephemeral=True)

    @discord.ui.button(label="✅ دخول", style=discord.ButtonStyle.success, custom_id="duty_in_btn")
    async def duty_in(self, inter: discord.Interaction, button: discord.ui.Button):
        roles = await self._guard_staff(inter)
        if not roles:
            return
        member: discord.Member = inter.user  # type: ignore
        staff_role, onduty_role = roles
        if is_onduty(member, onduty_role):
            return await inter.response.send_message("أنت أصلًا **مداوم** ✅", ephemeral=True)

        # افتح اختيار الشفت بشكل Ephemeral
        await inter.response.send_message(
            "اختر الشفت ثم Confirm:",
            view=ShiftPickerView(self),
            ephemeral=True
        )

    @discord.ui.button(label="🛑 خروج", style=discord.ButtonStyle.danger, custom_id="duty_out_btn")
    async def duty_out(self, inter: discord.Interaction, button: discord.ui.Button):
        roles = await self._guard_staff(inter)
        if not roles:
            return
        _, onduty_role = roles
        member: discord.Member = inter.user  # type: ignore

        if not is_onduty(member, onduty_role):
            return await inter.response.send_message("أنت أصلًا **مو مداوم** 💤", ephemeral=True)

        try:
            await member.remove_roles(onduty_role, reason="Duty OUT")
            end = now_ts()
            active = get_active_duty(inter.guild.id, member.id)
            if active:
                start, shift = active
            else:
                start, shift = end, "Support"

            dur = add_duty_session(inter.guild.id, member.id, start, end, shift)
            clear_active_duty(inter.guild.id, member.id)

            await send_log(inter.guild, f"🔴 **Duty OUT**: {member} | shift={shift} | مدة: **{fmt_duration(dur)}**")
            await inter.message.edit(embed=build_dashboard_embed(inter.guild), view=self)
            await inter.response.send_message(f"🛑 تم تسجيل خروجك. دوامك: **{fmt_duration(dur)}**", ephemeral=True)
        except discord.Forbidden:
            await inter.response.send_message("❌ ما عندي صلاحية أعدل الرتب. ارفع رتبة البوت وفعل Manage Roles.", ephemeral=True)

    @discord.ui.button(label="🚨 طوارئ", style=discord.ButtonStyle.primary, custom_id="duty_emergency_btn")
    async def emergency(self, inter: discord.Interaction, button: discord.ui.Button):
        if not inter.guild:
            return await inter.response.send_message("داخل سيرفر فقط.", ephemeral=True)

        staff_role, _, _ = get_roles(inter.guild)
        member: discord.Member = inter.user  # type: ignore
        if not (is_admin(inter) or is_staff_member(member, staff_role)):
            return await inter.response.send_message("❌ للستاف فقط.", ephemeral=True)

        await inter.response.send_modal(EmergencyModal())

    @discord.ui.button(label="🔄 تحديث", style=discord.ButtonStyle.secondary, custom_id="duty_refresh_btn")
    async def refresh(self, inter: discord.Interaction, button: discord.ui.Button):
        if not inter.guild:
            return await inter.response.send_message("داخل سيرفر فقط.", ephemeral=True)

        staff_role, _, _ = get_roles(inter.guild)
        member: discord.Member = inter.user  # type: ignore
        if not (is_admin(inter) or is_staff_member(member, staff_role)):
            return await inter.response.send_message("❌ التحديث للستاف فقط.", ephemeral=True)

        await inter.message.edit(embed=build_dashboard_embed(inter.guild), view=self)
        await inter.response.send_message("🔄 تم تحديث لوحة الحضور.", ephemeral=True)

# =======================
# Event: Count messages
# =======================
@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    dk = day_key_riyadh()
    inc_msg(message.guild.id, message.author.id, dk)
    await bot.process_commands(message)  # ما يضر حتى لو ما عندك أوامر prefix

# =======================
# Event: Voice tracking
# =======================
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot or not member.guild:
        return

    # join voice
    if before.channel is None and after.channel is not None:
        voice_join(member.guild.id, member.id, now_ts())
        return

    # leave voice
    if before.channel is not None and after.channel is None:
        voice_leave(member.guild.id, member.id, now_ts())
        return

    # move between channels -> treat as leave+join (counts as join)
    if before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        voice_leave(member.guild.id, member.id, now_ts())
        voice_join(member.guild.id, member.id, now_ts())

# =======================
# Auto clockout
# =======================
@tasks.loop(minutes=10)
async def auto_clockout_loop():
    for guild in bot.guilds:
        s = get_settings(guild.id)
        max_hours = int(s.get("auto_out_hours", 6) or 6)
        limit_sec = max_hours * 3600

        staff_role, onduty_role, _ = get_roles(guild)
        if not onduty_role:
            continue

        # pull actives
        with db() as con:
            cur = con.execute("SELECT user_id, start_ts, shift FROM active_duty WHERE guild_id=?", (guild.id,))
            actives = [(int(uid), int(st), str(sh)) for uid, st, sh in cur.fetchall()]

        nowu = now_ts()
        for uid, start_ts_, shift in actives:
            if nowu - start_ts_ < limit_sec:
                continue

            member = guild.get_member(uid)
            if not member:
                clear_active_duty(guild.id, uid)
                continue

            # remove onduty
            try:
                if onduty_role in member.roles:
                    await member.remove_roles(onduty_role, reason="Auto clock-out")
            except discord.Forbidden:
                # لو ما قدر، على الأقل نسجل
                await send_log(guild, f"⚠️ Auto clockout failed (no perms) for {member}")
                continue

            end = nowu
            dur = add_duty_session(guild.id, uid, start_ts_, end, shift)
            clear_active_duty(guild.id, uid)

            await send_log(guild, f"⏲️ **Auto Clock-Out**: {member} | shift={shift} | مدة: **{fmt_duration(dur)}** (limit {max_hours}h)")

# =======================
# Weekly report
# =======================
def points(duty_sec: int, msg_count: int, voice_sec: int, voice_joins: int, sessions: int) -> int:
    # نظام نقاط بسيط: تقدر تغيّره
    # - كل ساعة دوام = 10 نقاط
    # - كل 20 رسالة = 1 نقطة
    # - كل 30 دقيقة فويس = 2 نقاط
    # - كل دخول فويس = 1 نقطة
    # - كل Session دوام = 2 نقاط
    return (
        (duty_sec // 3600) * 10
        + (msg_count // 20)
        + (voice_sec // 1800) * 2
        + (voice_joins * 1)
        + (sessions * 2)
    )

async def run_weekly_report_for_guild(guild: discord.Guild):
    s = get_settings(guild.id)
    weekly_channel_id = int(s["weekly_channel_id"] or 0)
    staff_week_role_id = int(s["staff_week_role_id"] or 0)

    if weekly_channel_id == 0 or staff_week_role_id == 0:
        return

    channel = guild.get_channel(weekly_channel_id)
    staff_week_role = guild.get_role(staff_week_role_id)
    if not isinstance(channel, discord.TextChannel) or not staff_week_role:
        return

    since_ts = now_ts() - 7 * 24 * 3600
    since_day = (riyadh_now() - timedelta(days=6)).strftime("%Y-%m-%d")  # آخر 7 أيام شامل اليوم

    duty_map = duty_weekly_totals(guild.id, since_ts)        # uid -> (sec, sessions)
    msg_map = msg_weekly_total(guild.id, since_day)          # uid -> msg
    voice_map = voice_weekly_total(guild.id, since_day)      # uid -> (sec, joins)

    # اجمع كل IDs
    all_ids = set(duty_map.keys()) | set(msg_map.keys()) | set(voice_map.keys())
    if not all_ids:
        embed = discord.Embed(title="📊 تقرير حضور الإدارة الأسبوعي", description="ما فيه بيانات هذا الأسبوع.")
        await channel.send(embed=embed)
        return

    rows = []
    for uid in all_ids:
        duty_sec, sessions = duty_map.get(uid, (0, 0))
        msg_count = msg_map.get(uid, 0)
        vsec, vjoins = voice_map.get(uid, (0, 0))
        p = points(duty_sec, msg_count, vsec, vjoins, sessions)
        rows.append((uid, p, duty_sec, sessions, msg_count, vsec, vjoins))

    rows.sort(key=lambda x: x[1], reverse=True)

    embed = discord.Embed(
        title="📊 تقرير الإدارة الأسبوعي (متقدم)",
        description="آخر 7 أيام — يعتمد على (دوام + رسائل + فويس + دخول فويس + عدد الشفتات)."
    )

    # Top 10 leaderboard
    lines = []
    for i, (uid, p, duty_sec, sessions, msg_count, vsec, vjoins) in enumerate(rows[:10], start=1):
        lines.append(
            f"**{i})** <@{uid}> — **{p} pts** | "
            f"⏱️ {fmt_duration(duty_sec)} | 🧾 {sessions} | 💬 {msg_count} | 🔊 {fmt_duration(vsec)} | 🎧 {vjoins}"
        )
    embed.add_field(name="🏁 الترتيب الأسبوعي", value="\n".join(lines), inline=False)

    winner_id = rows[0][0]
    winner_pts = rows[0][1]
    embed.add_field(name="🏆 Staff of the Week", value=f"<@{winner_id}> — **{winner_pts} pts**", inline=False)

    # Update role
    try:
        for m in list(staff_week_role.members):
            if m.id != winner_id:
                await m.remove_roles(staff_week_role, reason="Weekly winner updated")
        winner = guild.get_member(winner_id)
        if winner and staff_week_role not in winner.roles:
            await winner.add_roles(staff_week_role, reason="Staff of the Week")
    except discord.Forbidden:
        embed.add_field(name="⚠️ تنبيه", value="ما قدرت أعدل رتبة Staff of the Week (ترتيب رتب/صلاحيات).", inline=False)

    await channel.send(embed=embed)

# scheduler: run Friday 20:00 Riyadh, once per date key
@tasks.loop(minutes=1)
async def weekly_scheduler():
    now = riyadh_now()
    if now.weekday() != 4:  # Friday
        return
    if not (now.hour == 20 and now.minute == 0):
        return

    week_key = now.strftime("%Y-%m-%d")
    for guild in bot.guilds:
        s = get_settings(guild.id)
        last_key = str(s.get("last_weekly_key") or "")
        if last_key == week_key:
            continue
        try:
            await run_weekly_report_for_guild(guild)
            set_setting(guild.id, "last_weekly_key", week_key)
        except Exception:
            pass

# =======================
# SLASH (ADMIN SETUP)
# =======================
@bot.tree.command(name="setup_duty", description="Setup duty roles + channels (Admin)")
@app_commands.describe(
    staff_role="رتبة Staff (شكل فقط) - اللي يقدر يداوم",
    onduty_role="رتبة OnDuty (الصلاحيات)",
    log_channel="روم اللوق (اختياري)",
)
async def setup_duty(inter: discord.Interaction, staff_role: discord.Role, onduty_role: discord.Role, log_channel: discord.TextChannel | None = None):
    if not inter.guild:
        return await inter.response.send_message("داخل سيرفر فقط.", ephemeral=True)
    if not is_admin(inter):
        return await inter.response.send_message("❌ Admin فقط.", ephemeral=True)

    set_setting(inter.guild.id, "staff_role_id", staff_role.id)
    set_setting(inter.guild.id, "onduty_role_id", onduty_role.id)
    if log_channel:
        set_setting(inter.guild.id, "log_channel_id", log_channel.id)

    await inter.response.send_message("✅ تم الإعداد. استخدم /post_duty_panel لنشر اللوحة.", ephemeral=True)

@bot.tree.command(name="setup_weekly", description="Setup weekly report + Staff of the Week role (Admin)")
@app_commands.describe(
    weekly_channel="الروم اللي ينزل فيه التقرير الأسبوعي",
    staff_week_role="رتبة Staff of the Week"
)
async def setup_weekly(inter: discord.Interaction, weekly_channel: discord.TextChannel, staff_week_role: discord.Role):
    if not inter.guild:
        return await inter.response.send_message("داخل سيرفر فقط.", ephemeral=True)
    if not is_admin(inter):
        return await inter.response.send_message("❌ Admin فقط.", ephemeral=True)

    set_setting(inter.guild.id, "weekly_channel_id", weekly_channel.id)
    set_setting(inter.guild.id, "staff_week_role_id", staff_week_role.id)

    await inter.response.send_message(
        "✅ تم ضبط التقرير الأسبوعي.\n"
        "📅 ينزل كل **جمعة 8:00 مساءً** بتوقيت الرياض.",
        ephemeral=True
    )

@bot.tree.command(name="set_alert_channel", description="Set emergency alert channel (Admin)")
@app_commands.describe(channel="روم نداء الطوارئ")
async def set_alert_channel(inter: discord.Interaction, channel: discord.TextChannel):
    if not inter.guild:
        return await inter.response.send_message("داخل سيرفر فقط.", ephemeral=True)
    if not is_admin(inter):
        return await inter.response.send_message("❌ Admin فقط.", ephemeral=True)

    set_setting(inter.guild.id, "alert_channel_id", channel.id)
    await inter.response.send_message(f"✅ تم ضبط روم الطوارئ: {channel.mention}", ephemeral=True)

@bot.tree.command(name="set_auto_out", description="Set auto clock-out hours (Admin)")
@app_commands.describe(hours="بعد كم ساعة يطلع تلقائي (مثال 6)")
async def set_auto_out(inter: discord.Interaction, hours: app_commands.Range[int, 1, 48]):
    if not inter.guild:
        return await inter.response.send_message("داخل سيرفر فقط.", ephemeral=True)
    if not is_admin(inter):
        return await inter.response.send_message("❌ Admin فقط.", ephemeral=True)

    set_setting(inter.guild.id, "auto_out_hours", int(hours))
    await inter.response.send_message(f"✅ تم ضبط Auto-Clockout على **{hours}** ساعة.", ephemeral=True)

@bot.tree.command(name="post_duty_panel", description="Post the staff duty panel (Admin)")
@app_commands.describe(channel="الروم اللي تبي تنزل فيه اللوحة")
async def post_duty_panel(inter: discord.Interaction, channel: discord.TextChannel):
    if not inter.guild:
        return await inter.response.send_message("داخل سيرفر فقط.", ephemeral=True)
    if not is_admin(inter):
        return await inter.response.send_message("❌ Admin فقط.", ephemeral=True)

    staff_role, onduty_role, _ = get_roles(inter.guild)
    if not staff_role or not onduty_role:
        return await inter.response.send_message("❌ سو /setup_duty أول.", ephemeral=True)

    await channel.send(embed=build_dashboard_embed(inter.guild), view=DutyPanelView())
    await inter.response.send_message(f"✅ تم نشر لوحة الحضور في {channel.mention}", ephemeral=True)

@bot.tree.command(name="weekly_now", description="Send weekly report now (Owner only)")
async def weekly_now(inter: discord.Interaction):
    if not inter.guild:
        return await inter.response.send_message("داخل سيرفر فقط.", ephemeral=True)
    if inter.user.id != inter.guild.owner_id:
        return await inter.response.send_message("❌ هذا الأمر للأونر فقط.", ephemeral=True)

    await run_weekly_report_for_guild(inter.guild)
    await inter.response.send_message("✅ تم إرسال التقرير الأسبوعي يدويًا.", ephemeral=True)

# =======================
# READY
# =======================
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        bot.add_view(DutyPanelView())
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")

        if not weekly_scheduler.is_running():
            weekly_scheduler.start()
        if not auto_clockout_loop.is_running():
            auto_clockout_loop.start()

        print("✅ Ready + weekly scheduler running + auto-clockout running")
    except Exception as e:
        print("❌ Ready error:", e)

# =======================
# RUN
# =======================
init_db()




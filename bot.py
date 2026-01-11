import os
import time
import random
import discord
import aiosqlite
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# =========================
# CONFIG (FROM .env)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

GUILD_ID = int(os.getenv("GUILD_ID", "0"))

QUESTS_CHANNEL_ID = int(os.getenv("QUESTS_CHANNEL_ID", "0"))
SUBMISSIONS_CHANNEL_ID = int(os.getenv("SUBMISSIONS_CHANNEL_ID", "0"))
ENVELOPES_CHANNEL_ID = int(os.getenv("ENVELOPES_CHANNEL_ID", "0"))
LEDGER_CHANNEL_ID = int(os.getenv("LEDGER_CHANNEL_ID", "0"))

STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "event.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Put it in your .env file.")

# =========================
# EVENT SETTINGS
# =========================
MAX_EVENT_DAY = 14
MAX_SUBMISSIONS_PER_DAY = 2  # anti-spam
OPEN_COOLDOWN_SECONDS = 10

# RNG tiers (name, weight, points)
TIERS = [
    ("🟢 Small Blessing", 55, 1),
    ("🔵 Prosperity Blessing", 30, 2),
    ("🟣 Fortune Blessing", 12, 4),
    ("🟡 Dragon’s Favor", 3, 8),  # grants dragon mark too
]

# =========================
# BOT SETUP
# =========================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

open_cooldowns: dict[int, float] = {}  # user_id -> last_open_time

# =========================
# DB HELPERS
# =========================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            envelopes INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            dragon INTEGER NOT NULL DEFAULT 0
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            day INTEGER NOT NULL,
            proof_url TEXT NOT NULL,
            note TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            message_id INTEGER,
            channel_id INTEGER,
            created_at INTEGER NOT NULL
        )
        """)
        await db.commit()

async def ensure_user(db: aiosqlite.Connection, user_id: int):
    await db.execute(
        "INSERT OR IGNORE INTO users(user_id, envelopes, points, dragon) VALUES (?, 0, 0, 0)",
        (user_id,),
    )

async def add_envelopes(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        await db.execute("UPDATE users SET envelopes = envelopes + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

async def get_user_stats(user_id: int) -> tuple[int, int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        async with db.execute("SELECT envelopes, points, dragon FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return int(row[0]), int(row[1]), int(row[2])

async def consume_envelope_and_award(user_id: int, points: int, is_dragon: bool) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        async with db.execute("SELECT envelopes FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row or int(row[0]) <= 0:
                return False

        await db.execute(
            "UPDATE users SET envelopes = envelopes - 1, points = points + ? WHERE user_id = ?",
            (points, user_id),
        )
        if is_dragon:
            await db.execute("UPDATE users SET dragon = dragon + 1 WHERE user_id = ?", (user_id,))
        await db.commit()
        return True

async def set_submission_status(submission_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE submissions SET status = ? WHERE submission_id = ?", (status, submission_id))
        await db.commit()

async def get_submission(submission_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT submission_id, user_id, day, proof_url, note, status, message_id, channel_id
            FROM submissions WHERE submission_id = ?
        """, (submission_id,)) as cur:
            return await cur.fetchone()

async def insert_submission(user_id: int, day: int, proof_url: str, note: str | None, message_id: int, channel_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO submissions(user_id, day, proof_url, note, status, message_id, channel_id, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
        """, (user_id, day, proof_url, note, message_id, channel_id, int(time.time())))
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
            return int(row[0])

async def update_submission_message(submission_id: int, message_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE submissions SET message_id=?, channel_id=? WHERE submission_id=?",
            (message_id, channel_id, submission_id),
        )
        await db.commit()

async def submissions_for_day(user_id: int, day: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COUNT(*) FROM submissions
            WHERE user_id = ? AND day = ? AND status IN ('PENDING','APPROVED')
        """, (user_id, day)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

async def top_leaderboard(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, points, envelopes, dragon
            FROM users
            ORDER BY points DESC, dragon DESC, envelopes DESC
            LIMIT ?
        """, (limit,)) as cur:
            return await cur.fetchall()

async def adjust_user_field(user_id: int, field: str, delta: int) -> tuple[int, int]:
    if field not in ("envelopes", "points", "dragon"):
        raise ValueError("Invalid field")

    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)

        async with db.execute(f"SELECT {field} FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            current = int(row[0]) if row else 0

        new_val = current + int(delta)
        if new_val < 0:
            new_val = 0

        await db.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (new_val, user_id))
        await db.commit()
        return current, new_val

async def try_remove_one_envelope(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        async with db.execute("SELECT envelopes FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row or int(row[0]) <= 0:
                return False
        await db.execute("UPDATE users SET envelopes = envelopes - 1 WHERE user_id = ?", (user_id,))
        await db.commit()
        return True

# =========================
# HELPERS
# =========================
def is_staff(member: discord.abc.User) -> bool:
    if STAFF_ROLE_ID == 0:
        return False
    if not isinstance(member, discord.Member):
        return False
    return any(r.id == STAFF_ROLE_ID for r in member.roles)

async def log_ledger(guild: discord.Guild | None, text: str):
    if LEDGER_CHANNEL_ID == 0 or guild is None:
        return
    ch = guild.get_channel(LEDGER_CHANNEL_ID)
    if not ch:
        return
    try:
        await ch.send(text)
    except (discord.Forbidden, discord.HTTPException):
        return

# =========================
# APPROVAL VIEW (PERSISTENT)
# =========================
class ReviewView(discord.ui.View):
    def __init__(self, submission_id: int):
        super().__init__(timeout=None)
        self.submission_id = submission_id

        # Make persistent: custom_id + no timeout
        # (self.approve / self.reject are Button instances after init)
        self.approve.custom_id = f"review:approve:{submission_id}"
        self.reject.custom_id = f"review:reject:{submission_id}"

    async def finalize_message(self, interaction: discord.Interaction, status: str, footer_note: str):
        for item in self.children:
            item.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.set_footer(text=footer_note)
        await interaction.message.edit(embed=embed, view=self)
        await set_submission_status(self.submission_id, status)

    @discord.ui.button(label="Approve +1 🧧", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        sub = await get_submission(self.submission_id)
        if not sub:
            return await interaction.response.send_message("Submission not found.", ephemeral=True)

        _, user_id, day, _, _, status, _, _ = sub
        if status != "PENDING":
            return await interaction.response.send_message("Already reviewed.", ephemeral=True)

        await add_envelopes(int(user_id), 1)
        await self.finalize_message(interaction, "APPROVED", f"✅ Approved by {interaction.user} • +1 Red Envelope granted")
        await log_ledger(interaction.guild, f"✅ {interaction.user} approved submission #{self.submission_id} (Day {day}) → <@{user_id}> +1 🧧")

        # no spammy ephemeral message
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Reject ❌", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        sub = await get_submission(self.submission_id)
        if not sub:
            return await interaction.response.send_message("Submission not found.", ephemeral=True)

        _, user_id, day, _, _, status, _, _ = sub
        if status != "PENDING":
            return await interaction.response.send_message("Already reviewed.", ephemeral=True)

        await self.finalize_message(interaction, "REJECTED", f"❌ Rejected by {interaction.user}")
        await log_ledger(interaction.guild, f"❌ {interaction.user} rejected submission #{self.submission_id} (Day {day}) → <@{user_id}>")

        await interaction.response.defer(ephemeral=True)

# =========================
# COMMANDS
# =========================
class EventCommands(app_commands.Group):
    def __init__(self):
        super().__init__(name="event", description="Fortune of the Red Dragon (CNY Event)")

    @app_commands.command(name="submit", description="Submit proof for today's quest (screenshot required).")
    @app_commands.describe(day="Quest day number (1-14)", proof="Upload screenshot proof", note="Optional short note")
    async def submit(self, interaction: discord.Interaction, day: int, proof: discord.Attachment, note: str | None = None):
        if interaction.channel_id != SUBMISSIONS_CHANNEL_ID:
            return await interaction.response.send_message("Use this command in the submissions channel.", ephemeral=True)

        if day < 1 or day > MAX_EVENT_DAY:
            return await interaction.response.send_message(f"Day must be between 1 and {MAX_EVENT_DAY}.", ephemeral=True)

        count = await submissions_for_day(interaction.user.id, day)
        if count >= MAX_SUBMISSIONS_PER_DAY:
            return await interaction.response.send_message(
                f"You already submitted the maximum ({MAX_SUBMISSIONS_PER_DAY}) for Day {day}.",
                ephemeral=True
            )

        if proof.content_type and not proof.content_type.startswith("image/"):
            return await interaction.response.send_message("Please upload an image screenshot.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="🧧 Quest Submission",
            description=f"**Day:** {day}\n**Clasher:** {interaction.user.mention}",
            color=0xE74C3C
        )
        embed.add_field(name="Note", value=note if note else "—", inline=False)
        embed.set_image(url=proof.url)
        embed.set_footer(text="Status: PENDING")

        submission_id = await insert_submission(interaction.user.id, day, proof.url, note, 0, 0)

        view = ReviewView(submission_id=submission_id)
        msg = await interaction.channel.send(embed=embed, view=view)

        await update_submission_message(submission_id, msg.id, msg.channel.id)

        await log_ledger(interaction.guild, f"📮 New submission #{submission_id} (Day {day}) by {interaction.user.mention}")
        await interaction.followup.send(f"✅ Submission received! ID **#{submission_id}** (pending review).", ephemeral=True)

    @app_commands.command(name="open", description="Open 1 Red Envelope and reveal your fortune.")
    async def open(self, interaction: discord.Interaction):
        if interaction.channel_id != ENVELOPES_CHANNEL_ID:
            return await interaction.response.send_message("Use this command in the envelopes channel.", ephemeral=True)

        now = time.time()
        last = open_cooldowns.get(interaction.user.id, 0)
        if now - last < OPEN_COOLDOWN_SECONDS:
            wait = int(OPEN_COOLDOWN_SECONDS - (now - last))
            return await interaction.response.send_message(f"⏳ Slow down—try again in {wait}s.", ephemeral=True)
        open_cooldowns[interaction.user.id] = now

        envelopes, points, dragon = await get_user_stats(interaction.user.id)
        if envelopes <= 0:
            return await interaction.response.send_message("You have no Red Envelopes 🧧. Complete quests to earn more!", ephemeral=True)

        weights = [t[1] for t in TIERS]
        choice = random.choices(TIERS, weights=weights, k=1)[0]
        tier_name, _, tier_points = choice
        is_dragon = tier_name.startswith("🟡")

        ok = await consume_envelope_and_award(interaction.user.id, tier_points, is_dragon)
        if not ok:
            return await interaction.response.send_message("You have no envelopes.", ephemeral=True)

        envelopes2, points2, dragon2 = await get_user_stats(interaction.user.id)

        flavor = {
            "🟢": "A small blessing drifts from the lantern lights…",
            "🔵": "Prosperity follows your footsteps through Narcia’s snow…",
            "🟣": "The Dragon’s shadow passes over your fortress—fortune rises.",
            "🟡": "The Red Dragon awakens and places its mark upon you…"
        }
        key = tier_name.split()[0]
        text = flavor.get(key, "Fortune smiles upon you.")

        embed = discord.Embed(
            title="🎁 Red Envelope Opened!",
            description=f"{tier_name}\n*{text}*",
            color=0xF1C40F if is_dragon else 0xE74C3C
        )
        embed.add_field(name="Reward", value=f"**+{tier_points} Fortune Points**", inline=True)
        embed.add_field(name="Remaining Envelopes", value=str(envelopes2), inline=True)
        embed.add_field(name="Total Points", value=str(points2), inline=True)
        if is_dragon:
            embed.add_field(name="Dragon Marks", value=str(dragon2), inline=True)

        await log_ledger(interaction.guild, f"🎁 {interaction.user.mention} opened an envelope → {tier_name} (+{tier_points} points)")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="balance", description="Check your envelopes and points.")
    async def balance(self, interaction: discord.Interaction):
        envelopes, points, dragon = await get_user_stats(interaction.user.id)
        embed = discord.Embed(title="🧧 Your Fortune", color=0xE74C3C)
        embed.add_field(name="Red Envelopes", value=str(envelopes), inline=True)
        embed.add_field(name="Fortune Points", value=str(points), inline=True)
        embed.add_field(name="Dragon Marks", value=str(dragon), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="postquest", description="(Staff) Post the daily quest into the quests channel.")
    @app_commands.describe(
        day="Quest day number (1-14)",
        title="Short quest title",
        quest="Full quest instructions",
        bonus="Optional bonus mission text",
        image="Optional image/banner for the quest",
        pin="Pin the quest message (optional)"
    )
    async def postquest(
        self,
        interaction: discord.Interaction,
        day: int,
        title: str,
        quest: str,
        bonus: str | None = None,
        image: discord.Attachment | None = None,
        pin: bool = False
    ):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        if QUESTS_CHANNEL_ID == 0:
            return await interaction.response.send_message("QUESTS_CHANNEL_ID is not set in .env", ephemeral=True)

        if day < 1 or day > MAX_EVENT_DAY:
            return await interaction.response.send_message(f"Day must be between 1 and {MAX_EVENT_DAY}.", ephemeral=True)

        if not interaction.guild:
            return await interaction.response.send_message("This command must be used in a server.", ephemeral=True)

        ch = interaction.guild.get_channel(QUESTS_CHANNEL_ID)
        if not ch:
            return await interaction.response.send_message("I can't access the quests channel (check ID/permissions).", ephemeral=True)

        embed = discord.Embed(
            title=f"🧧 Day {day} Quest — {title}",
            description=quest,
            color=0xE74C3C
        )

        if bonus:
            embed.add_field(name="🌟 Bonus Mission (Optional)", value=bonus, inline=False)

        embed.add_field(
            name="📮 How to Submit",
            value=f"Go to <#{SUBMISSIONS_CHANNEL_ID}> and use:\n`/event submit day:{day}` (attach proof if required)",
            inline=False
        )

        if image:
            if image.content_type and image.content_type.startswith("image/"):
                embed.set_image(url=image.url)
            else:
                return await interaction.response.send_message("Please upload a valid image file.", ephemeral=True)

        try:
            msg = await ch.send(embed=embed)
        except discord.Forbidden:
            return await interaction.response.send_message("I don't have permission to post in the quests channel.", ephemeral=True)

        if pin:
            try:
                await msg.pin(reason=f"CNY Quest Day {day}")
            except discord.Forbidden:
                pass

        await log_ledger(interaction.guild, f"📌 Quest posted: Day {day} by {interaction.user.mention}")
        await interaction.response.send_message(f"✅ Posted Day {day} quest in {ch.mention}.", ephemeral=True)

    @app_commands.command(name="leaderboard", description="Top Fortune Points.")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await top_leaderboard(10)
        if not rows:
            return await interaction.response.send_message("No data yet.", ephemeral=True)

        lines = []
        for i, (user_id, points, envelopes, dragon) in enumerate(rows, start=1):
            lines.append(f"**{i}.** <@{user_id}> — **{points} pts** • 🧧{envelopes} • 🐉{dragon}")

        embed = discord.Embed(
            title="🏆 Fortune Leaderboard",
            description="\n".join(lines),
            color=0xE74C3C
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="revoke", description="(Staff) Revoke an approved submission. Removes 1 envelope if possible.")
    @app_commands.describe(submission_id="Submission ID number (e.g. 12)")
    async def revoke(self, interaction: discord.Interaction, submission_id: int):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        sub = await get_submission(submission_id)
        if not sub:
            return await interaction.response.send_message("Submission not found.", ephemeral=True)

        _, user_id, day, _, _, status, message_id, channel_id = sub

        if status == "REVOKED":
            return await interaction.response.send_message("This submission is already revoked.", ephemeral=True)

        if status != "APPROVED":
            return await interaction.response.send_message(f"Only APPROVED submissions can be revoked. Current: {status}", ephemeral=True)

        await set_submission_status(submission_id, "REVOKED")
        removed = await try_remove_one_envelope(int(user_id))

        # Update original submission message (best-effort)
        try:
            if interaction.guild and channel_id and message_id:
                ch = interaction.guild.get_channel(int(channel_id))
                if ch:
                    msg = await ch.fetch_message(int(message_id))
                    if msg and msg.embeds:
                        emb = msg.embeds[0]
                        emb.set_footer(text=f"⚠️ REVOKED by {interaction.user}")
                        await msg.edit(embed=emb, view=None)
        except Exception:
            pass

        envelopes, points, dragon = await get_user_stats(int(user_id))

        if removed:
            text = (
                f"✅ Revoked submission **#{submission_id}** (Day {day}).\n"
                f"➖ Removed **1 envelope** from <@{user_id}>.\n"
                f"Now: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**"
            )
            await log_ledger(interaction.guild, f"🧹 {interaction.user.mention} revoked submission #{submission_id} → <@{user_id}> (envelope removed)")
        else:
            text = (
                f"✅ Revoked submission **#{submission_id}** (Day {day}).\n"
                f"⚠️ Could NOT remove an envelope (likely already spent).\n"
                f"Use adjust commands if needed.\n"
                f"Current: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**"
            )
            await log_ledger(interaction.guild, f"🧹 {interaction.user.mention} revoked submission #{submission_id} → <@{user_id}> (envelope NOT removed; adjust needed)")

        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="adjustpoints", description="(Staff) Adjust a user's Fortune Points (+/-). Clamped at 0.")
    @app_commands.describe(user="Target user", amount="Use negative to subtract (e.g., -4)")
    async def adjustpoints(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        before, after = await adjust_user_field(user.id, "points", amount)
        envelopes, points, dragon = await get_user_stats(user.id)

        await log_ledger(interaction.guild, f"🛠️ {interaction.user.mention} adjusted POINTS for {user.mention}: {before} -> {after} (delta {amount})")
        await interaction.response.send_message(
            f"✅ Points updated for {user.mention}: **{before} → {after}**\nNow: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**",
            ephemeral=True
        )

    @app_commands.command(name="adjustenvelopes", description="(Staff) Adjust a user's envelopes (+/-). Clamped at 0.")
    @app_commands.describe(user="Target user", amount="Use negative to subtract (e.g., -1)")
    async def adjustenvelopes(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        before, after = await adjust_user_field(user.id, "envelopes", amount)
        envelopes, points, dragon = await get_user_stats(user.id)

        await log_ledger(interaction.guild, f"🛠️ {interaction.user.mention} adjusted ENVELOPES for {user.mention}: {before} -> {after} (delta {amount})")
        await interaction.response.send_message(
            f"✅ Envelopes updated for {user.mention}: **{before} → {after}**\nNow: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**",
            ephemeral=True
        )

    @app_commands.command(name="adjustdragon", description="(Staff) Adjust a user's Dragon Marks (+/-). Clamped at 0.")
    @app_commands.describe(user="Target user", amount="Use negative to subtract (e.g., -1)")
    async def adjustdragon(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        before, after = await adjust_user_field(user.id, "dragon", amount)
        envelopes, points, dragon = await get_user_stats(user.id)

        await log_ledger(interaction.guild, f"🛠️ {interaction.user.mention} adjusted DRAGON for {user.mention}: {before} -> {after} (delta {amount})")
        await interaction.response.send_message(
            f"✅ Dragon Marks updated for {user.mention}: **{before} → {after}**\nNow: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**",
            ephemeral=True
        )
# =========================
# BOT SETUP
# =========================
intents = discord.Intents.default()

class FortuneBot(commands.Bot):
    async def setup_hook(self):
        # 1) Always register locally FIRST (no awaits before this)
        # so the bot can handle interactions immediately.
        if not any(cmd.name == "event" for cmd in self.tree.get_commands()):
            self.tree.add_command(EventCommands())

        # 2) Sync fast to your guild (instant updates)
        try:
            if GUILD_ID and GUILD_ID != 0:
                guild = discord.Object(id=GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                print(f"✅ Synced {len(synced)} commands to guild {GUILD_ID}")
            else:
                synced = await self.tree.sync()
                print(f"✅ Synced {len(synced)} GLOBAL commands (may take time to appear)")
        except Exception as e:
            print("Command sync failed:", e)

bot = FortuneBot(command_prefix="!", intents=intents)

# =========================
# STARTUP
# =========================
@bot.event
async def on_ready():
    await init_db()

    # Re-register persistent views for pending submissions
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT submission_id FROM submissions WHERE status='PENDING'") as cur:
            pending = await cur.fetchall()

    for (submission_id,) in pending:
        bot.add_view(ReviewView(submission_id=int(submission_id)))

    # Debug: confirm the bot truly has /event locally
    print("Local tree commands:", [c.name for c in bot.tree.get_commands()])

    print(f"Logged in as {bot.user} ✅")

bot.run(BOT_TOKEN)

import os
import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
import random
import time
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
# RNG tiers (name, weight, points)
TIERS = [
    ("🟢 Small Blessing", 55, 1),
    ("🔵 Prosperity Blessing", 30, 2),
    ("🟣 Fortune Blessing", 12, 4),
    ("🟡 Dragon’s Favor", 3, 8),
]

OPEN_COOLDOWN_SECONDS = 10
ONE_SUBMISSION_PER_DAY = True  # keeps event clean and anti-spam

# =========================
# BOT SETUP
# =========================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

open_cooldowns = {}  # user_id -> last_open_time

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

async def ensure_user(db, user_id: int):
    await db.execute(
        "INSERT OR IGNORE INTO users(user_id, envelopes, points, dragon) VALUES (?, 0, 0, 0)",
        (user_id,)
    )

async def add_envelopes(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        await db.execute("UPDATE users SET envelopes = envelopes + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

async def get_user_stats(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        async with db.execute("SELECT envelopes, points, dragon FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0], row[1], row[2]

async def consume_envelope_and_award(user_id: int, points: int, is_dragon: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        # check balance
        async with db.execute("SELECT envelopes FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row or row[0] <= 0:
                return False

        await db.execute("UPDATE users SET envelopes = envelopes - 1, points = points + ? WHERE user_id = ?",
                         (points, user_id))
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

async def user_already_submitted_day(user_id: int, day: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COUNT(*) FROM submissions
            WHERE user_id = ? AND day = ? AND status IN ('PENDING','APPROVED')
        """, (user_id, day)) as cur:
            row = await cur.fetchone()
            return row[0] > 0

async def insert_submission(user_id: int, day: int, proof_url: str, note: str | None, message_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO submissions(user_id, day, proof_url, note, status, message_id, channel_id, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
        """, (user_id, day, proof_url, note, message_id, channel_id, int(time.time())))
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
            return row[0]

async def top_leaderboard(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, points, envelopes, dragon
            FROM users
            ORDER BY points DESC, dragon DESC, envelopes DESC
            LIMIT ?
        """, (limit,)) as cur:
            return await cur.fetchall()

# =========================
# LOGGING
# =========================
async def log_ledger(guild: discord.Guild, text: str):
    if LEDGER_CHANNEL_ID == 0:
        return
    ch = guild.get_channel(LEDGER_CHANNEL_ID)
    if ch:
        await ch.send(text)

# =========================
# APPROVAL VIEW
# =========================
class ReviewView(discord.ui.View):
    def __init__(self, submission_id: int):
        super().__init__(timeout=None)
        self.submission_id = submission_id

    def staff_check(self, interaction: discord.Interaction) -> bool:
        if STAFF_ROLE_ID == 0:
            return False
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        return any(role.id == STAFF_ROLE_ID for role in member.roles)

    async def finalize_message(self, interaction: discord.Interaction, status: str, footer_note: str):
        # Disable buttons
        for item in self.children:
            item.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.set_footer(text=footer_note)
        await interaction.message.edit(embed=embed, view=self)

        await set_submission_status(self.submission_id, status)

    @discord.ui.button(label="Approve +1 🧧", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.staff_check(interaction):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        sub = await get_submission(self.submission_id)
        if not sub:
            return await interaction.response.send_message("Submission not found.", ephemeral=True)
        _, user_id, day, _, _, status, _, _ = sub
        if status != "PENDING":
            return await interaction.response.send_message("Already reviewed.", ephemeral=True)

        await add_envelopes(user_id, 1)
        await self.finalize_message(interaction, "APPROVED",
                                    f"✅ Approved by {interaction.user} • +1 Red Envelope granted")

        await log_ledger(interaction.guild, f"✅ {interaction.user} approved submission #{self.submission_id} (Day {day}) → <@{user_id}> +1 🧧")
        await interaction.response.send_message("Approved. Envelope granted.", ephemeral=True)

    @discord.ui.button(label="Reject ❌", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.staff_check(interaction):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        sub = await get_submission(self.submission_id)
        if not sub:
            return await interaction.response.send_message("Submission not found.", ephemeral=True)
        _, user_id, day, _, _, status, _, _ = sub
        if status != "PENDING":
            return await interaction.response.send_message("Already reviewed.", ephemeral=True)

        await self.finalize_message(interaction, "REJECTED",
                                    f"❌ Rejected by {interaction.user}")

        await log_ledger(interaction.guild, f"❌ {interaction.user} rejected submission #{self.submission_id} (Day {day}) → <@{user_id}>")
        await interaction.response.send_message("Rejected.", ephemeral=True)

# Make views persistent across restarts (we re-add on ready)
PERSISTENT_VIEWS = {}

# =========================
# COMMANDS
# =========================
class EventCommands(app_commands.Group):
    def __init__(self):
        super().__init__(name="event", description="Fortune of the Red Dragon (CNY Event)")

    @app_commands.command(name="submit", description="Submit proof for today's quest (screenshot required).")
    @app_commands.describe(day="Quest day number (e.g. 1, 2, 3...)",
                           proof="Upload screenshot proof",
                           note="Optional short note")
    async def submit(self, interaction: discord.Interaction, day: int, proof: discord.Attachment, note: str | None = None):
        if interaction.channel_id != SUBMISSIONS_CHANNEL_ID:
            return await interaction.response.send_message("Use this command in the submissions channel.", ephemeral=True)

        if day <= 0 or day > 31:
            return await interaction.response.send_message("Day must be between 1 and 31.", ephemeral=True)

        if ONE_SUBMISSION_PER_DAY:
            if await user_already_submitted_day(interaction.user.id, day):
                return await interaction.response.send_message("You already submitted for that day (pending/approved).", ephemeral=True)

        if proof.content_type and not proof.content_type.startswith("image/"):
            return await interaction.response.send_message("Please upload an image screenshot.", ephemeral=True)

        # Create a placeholder message first so we can store message_id
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="🧧 Quest Submission",
            description=f"**Day:** {day}\n**Clasher:** {interaction.user.mention}",
            color=0xE74C3C
        )
        embed.add_field(name="Note", value=note if note else "—", inline=False)
        embed.set_image(url=proof.url)
        embed.set_footer(text="Status: PENDING")

        # Send submission message with buttons
        view = ReviewView(submission_id=-1)  # temporary
        msg = await interaction.channel.send(embed=embed, view=view)

        # Insert in DB and update view with real submission_id
        submission_id = await insert_submission(interaction.user.id, day, proof.url, note, msg.id, msg.channel.id)
        view.submission_id = submission_id
        PERSISTENT_VIEWS[submission_id] = view
        await msg.edit(view=view)

        await log_ledger(interaction.guild, f"📮 New submission #{submission_id} (Day {day}) by {interaction.user.mention}")
        await interaction.followup.send(f"✅ Submission received! ID **#{submission_id}** (pending review).", ephemeral=True)

    @app_commands.command(name="open", description="Open 1 Red Envelope and reveal your fortune.")
    async def open(self, interaction: discord.Interaction):
        if interaction.channel_id != ENVELOPES_CHANNEL_ID:
            return await interaction.response.send_message("Use this command in the envelopes channel.", ephemeral=True)

        # cooldown
        now = time.time()
        last = open_cooldowns.get(interaction.user.id, 0)
        if now - last < OPEN_COOLDOWN_SECONDS:
            wait = int(OPEN_COOLDOWN_SECONDS - (now - last))
            return await interaction.response.send_message(f"⏳ Slow down—try again in {wait}s.", ephemeral=True)
        open_cooldowns[interaction.user.id] = now

        envelopes, points, dragon = await get_user_stats(interaction.user.id)
        if envelopes <= 0:
            return await interaction.response.send_message("You have no Red Envelopes 🧧. Complete quests to earn more!", ephemeral=True)

        # weighted roll
        names = [t[0] for t in TIERS]
        weights = [t[1] for t in TIERS]
        points_awards = [t[2] for t in TIERS]
        choice = random.choices(range(len(TIERS)), weights=weights, k=1)[0]
        tier_name = names[choice]
        tier_points = points_awards[choice]
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
        key = tier_name.split()[0]  # emoji token
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

event_group = EventCommands()
bot.tree.add_command(event_group)

# =========================
# STARTUP
# =========================
@bot.event
async def on_ready():
    await init_db()

    # Command sync (fast dev)
    try:
        if GUILD_ID and GUILD_ID != 0:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
    except Exception as e:
        print("Command sync failed:", e)

    # Re-register persistent views for pending submissions (so buttons keep working after restart)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT submission_id FROM submissions WHERE status = 'PENDING'
        """) as cur:
            pending = await cur.fetchall()

    for (submission_id,) in pending:
        view = ReviewView(submission_id=submission_id)
        bot.add_view(view)
        PERSISTENT_VIEWS[submission_id] = view

    print(f"Logged in as {bot.user} ✅")

bot.run(BOT_TOKEN)
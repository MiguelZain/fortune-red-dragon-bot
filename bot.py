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
# CONFIG (.env)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
QUESTS_CHANNEL_ID = int(os.getenv("QUESTS_CHANNEL_ID", "0"))
SUBMISSIONS_CHANNEL_ID = int(os.getenv("SUBMISSIONS_CHANNEL_ID", "0"))
ENVELOPES_CHANNEL_ID = int(os.getenv("ENVELOPES_CHANNEL_ID", "0"))
LEDGER_CHANNEL_ID = int(os.getenv("LEDGER_CHANNEL_ID", "0"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "event.db")

# Optional: add a thumbnail URL if you want (discord cdn / imgur / etc)
OPEN_THUMBNAIL_URL = os.getenv("OPEN_THUMBNAIL_URL", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Put it in your .env file.")

# =========================
# EVENT SETTINGS
# =========================
OPEN_COOLDOWN_SECONDS = 10
DAILY_COOLDOWN_SECONDS = 6 * 60 * 60  # 6 hours
DAILY_ENVELOPES_AWARD = 1

PARTICIPATION_GOAL = 7  # "Participation Reward" threshold (approved missions)

# RNG tiers (name, weight, points)
TIERS = [
    ("🟢 Small Blessing", 55, 1),
    ("🔵 Prosperity Blessing", 30, 2),
    ("🟣 Fortune Blessing", 12, 4),
    ("🟡 Dragon’s Favor", 3, 8),  # grants dragon mark too
]

# Colors (CNY vibe)
COLOR_RED = 0xEE1C25
COLOR_GOLD = 0xFFD700

# Tier-specific flavor libraries (randomized so it won't get repetitive)
FLAVOR = {
    "🟢": [
        "A lantern flickers… and a humble blessing finds you.",
        "A quiet wind carries luck across your walls.",
        "A small fortune settles like snow on Narcia’s rooftops.",
        "The festival drums echo—good things begin with small steps.",
    ],
    "🔵": [
        "Prosperity follows your footsteps through Narcia’s frost.",
        "Your vaults glow brighter—fortune walks beside you.",
        "The crowd cheers as your luck rises with the fireworks.",
        "A silver tide of blessings rolls in with the night.",
    ],
    "🟣": [
        "The Dragon’s shadow passes over your fortress—fortune surges.",
        "A royal omen appears—your destiny sharpens.",
        "A violet star burns in the sky… and your luck answers.",
        "The festival gates open—your name is written in fortune.",
    ],
    "🟡": [
        "The Red Dragon awakens… and leaves its mark upon you.",
        "A golden roar shakes the realm—your fate is chosen tonight.",
        "Imperial flames dance—your fortune is crowned.",
        "The Dragon’s gaze meets yours. The mark is yours to bear.",
    ],
}

# =========================
# BOT SETUP
# =========================
intents = discord.Intents.default()

open_cooldowns: dict[int, float] = {}  # user_id -> last_open_time


# =========================
# DB HELPERS
# =========================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # users
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            envelopes INTEGER NOT NULL DEFAULT 0,
            points INTEGER NOT NULL DEFAULT 0,
            dragon INTEGER NOT NULL DEFAULT 0
        )
        """)

        # quests (staff-posted missions)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS quests (
            quest_id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            bonus TEXT,
            reward_envelopes INTEGER NOT NULL DEFAULT 1,
            image_url TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            message_id INTEGER,
            channel_id INTEGER,
            created_at INTEGER NOT NULL
        )
        """)

        # submissions (player proof submissions tied to quest_id)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            quest_id INTEGER NOT NULL,
            proof_url TEXT NOT NULL,
            note TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            reward_envelopes_awarded INTEGER NOT NULL DEFAULT 0,
            message_id INTEGER,
            channel_id INTEGER,
            created_at INTEGER NOT NULL
        )
        """)

        # daily claims (6h cooldown)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS daily_claims (
            user_id INTEGER PRIMARY KEY,
            last_claim_at INTEGER NOT NULL DEFAULT 0
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
        await db.execute(
            "UPDATE users SET envelopes = envelopes + ? WHERE user_id = ?",
            (int(amount), int(user_id)),
        )
        await db.commit()


async def get_user_stats(user_id: int) -> tuple[int, int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        async with db.execute(
            "SELECT envelopes, points, dragon FROM users WHERE user_id = ?",
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]), int(row[1]), int(row[2])


async def consume_envelope_and_award(user_id: int, points: int, is_dragon: bool) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        async with db.execute(
            "SELECT envelopes FROM users WHERE user_id = ?",
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
            if not row or int(row[0]) <= 0:
                return False

        await db.execute(
            "UPDATE users SET envelopes = envelopes - 1, points = points + ? WHERE user_id = ?",
            (int(points), int(user_id)),
        )
        if is_dragon:
            await db.execute(
                "UPDATE users SET dragon = dragon + 1 WHERE user_id = ?",
                (int(user_id),),
            )
        await db.commit()
        return True


async def top_leaderboard(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, points, envelopes, dragon
            FROM users
            ORDER BY points DESC, dragon DESC, envelopes DESC
            LIMIT ?
        """, (int(limit),)) as cur:
            return await cur.fetchall()


async def adjust_user_field(user_id: int, field: str, delta: int) -> tuple[int, int]:
    if field not in ("envelopes", "points", "dragon"):
        raise ValueError("Invalid field")

    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)

        async with db.execute(
            f"SELECT {field} FROM users WHERE user_id = ?",
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
            current = int(row[0]) if row else 0

        new_val = current + int(delta)
        if new_val < 0:
            new_val = 0

        await db.execute(
            f"UPDATE users SET {field} = ? WHERE user_id = ?",
            (int(new_val), int(user_id)),
        )
        await db.commit()
        return current, new_val


async def try_remove_envelopes(user_id: int, amount: int) -> bool:
    amount = int(amount)
    if amount <= 0:
        return True

    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        async with db.execute(
            "SELECT envelopes FROM users WHERE user_id = ?",
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
            bal = int(row[0]) if row else 0
            if bal < amount:
                return False

        await db.execute(
            "UPDATE users SET envelopes = envelopes - ? WHERE user_id = ?",
            (amount, int(user_id)),
        )
        await db.commit()
        return True


# -------- quests --------
async def create_quest(title: str, body: str, bonus: str | None, reward_envelopes: int, image_url: str | None,
                       message_id: int, channel_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO quests(title, body, bonus, reward_envelopes, image_url, active, message_id, channel_id, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
        """, (
            title.strip(),
            body.strip(),
            bonus.strip() if bonus else None,
            int(reward_envelopes),
            image_url,
            int(message_id) if message_id else None,
            int(channel_id) if channel_id else None,
            int(time.time()),
        ))
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
            return int(row[0])


async def get_quest(quest_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT quest_id, title, body, bonus, reward_envelopes, image_url, active, message_id, channel_id
            FROM quests WHERE quest_id = ?
        """, (int(quest_id),)) as cur:
            return await cur.fetchone()


async def list_active_quests(limit: int = 25):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT quest_id, title, reward_envelopes
            FROM quests
            WHERE active = 1
            ORDER BY quest_id DESC
            LIMIT ?
        """, (int(limit),)) as cur:
            return await cur.fetchall()


async def close_quest(quest_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE quests SET active = 0 WHERE quest_id = ?", (int(quest_id),))
        await db.commit()
        return True


# -------- submissions --------
async def insert_submission(user_id: int, quest_id: int, proof_url: str, note: str | None,
                            message_id: int, channel_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO submissions(user_id, quest_id, proof_url, note, status, reward_envelopes_awarded, message_id, channel_id, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)
        """, (
            int(user_id),
            int(quest_id),
            proof_url,
            note,
            int(message_id) if message_id else None,
            int(channel_id) if channel_id else None,
            int(time.time()),
        ))
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
            return int(row[0])


async def update_submission_message(submission_id: int, message_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE submissions SET message_id=?, channel_id=? WHERE submission_id=?",
            (int(message_id), int(channel_id), int(submission_id)),
        )
        await db.commit()


async def get_submission(submission_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT submission_id, user_id, quest_id, proof_url, note, status, reward_envelopes_awarded, message_id, channel_id
            FROM submissions WHERE submission_id = ?
        """, (int(submission_id),)) as cur:
            return await cur.fetchone()


async def set_submission_status(submission_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE submissions SET status = ? WHERE submission_id = ?", (status, int(submission_id)))
        await db.commit()


async def mark_submission_award(submission_id: int, reward_envelopes_awarded: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE submissions
            SET reward_envelopes_awarded = ?
            WHERE submission_id = ?
        """, (int(reward_envelopes_awarded), int(submission_id)))
        await db.commit()


async def user_has_submission_for_quest(user_id: int, quest_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COUNT(*)
            FROM submissions
            WHERE user_id = ? AND quest_id = ? AND status IN ('PENDING','APPROVED')
        """, (int(user_id), int(quest_id))) as cur:
            row = await cur.fetchone()
            return int(row[0]) > 0


async def count_user_approved(user_id: int) -> int:
    """Counts approved submissions (missions completed)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT COUNT(*)
            FROM submissions
            WHERE user_id = ? AND status = 'APPROVED'
        """, (int(user_id),)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


# -------- daily claim --------
async def can_claim_daily(user_id: int) -> tuple[bool, int]:
    """Returns (can_claim, seconds_remaining)."""
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT last_claim_at FROM daily_claims WHERE user_id = ?",
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
            last = int(row[0]) if row else 0

        if now - last >= DAILY_COOLDOWN_SECONDS:
            return True, 0
        return False, int(DAILY_COOLDOWN_SECONDS - (now - last))


async def set_daily_claim(user_id: int):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO daily_claims(user_id, last_claim_at)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_claim_at = excluded.last_claim_at
        """, (int(user_id), now))
        await db.commit()


# =========================
# HELPERS
# =========================
def is_staff(member: discord.abc.User) -> bool:
    if STAFF_ROLE_ID == 0:
        return False
    if not isinstance(member, discord.Member):
        return False
    return any(r.id == STAFF_ROLE_ID for r in member.roles)


def msg_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


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
        self.submission_id = int(submission_id)

        # Persistent buttons: custom_id required + no timeout
        self.approve.custom_id = f"review:approve:{self.submission_id}"
        self.reject.custom_id = f"review:reject:{self.submission_id}"

    async def finalize_message(self, interaction: discord.Interaction, status: str, footer_note: str):
        for item in self.children:
            item.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(color=COLOR_RED)
        embed.set_footer(text=footer_note)
        await interaction.message.edit(embed=embed, view=self)
        await set_submission_status(self.submission_id, status)

    @discord.ui.button(label="Approve ✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        sub = await get_submission(self.submission_id)
        if not sub:
            return await interaction.response.send_message("Submission not found.", ephemeral=True)

        submission_id, user_id, quest_id, _, _, status, _, message_id, channel_id = sub
        if status != "PENDING":
            return await interaction.response.send_message("Already reviewed.", ephemeral=True)

        quest = await get_quest(int(quest_id))
        if not quest:
            return await interaction.response.send_message("Quest not found (it may have been deleted).", ephemeral=True)

        _, q_title, _, _, q_reward, _, _, _, _ = quest
        reward = int(q_reward)

        await add_envelopes(int(user_id), reward)
        await mark_submission_award(self.submission_id, reward)
        await self.finalize_message(
            interaction,
            "APPROVED",
            f"✅ Approved by {interaction.user} • +{reward} 🧧 for “{q_title}”"
        )

        # ledger with direct link to the submission message
        if interaction.guild and channel_id and message_id:
            link = msg_link(interaction.guild.id, int(channel_id), int(message_id))
        else:
            link = "(link unavailable)"

        await log_ledger(
            interaction.guild,
            f"✅ APPROVED • Sub#{submission_id} • Quest#{quest_id} • +{reward}🧧 → <@{user_id}> • {link}"
        )

        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Reject ❌", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        sub = await get_submission(self.submission_id)
        if not sub:
            return await interaction.response.send_message("Submission not found.", ephemeral=True)

        submission_id, user_id, quest_id, _, _, status, _, message_id, channel_id = sub
        if status != "PENDING":
            return await interaction.response.send_message("Already reviewed.", ephemeral=True)

        await self.finalize_message(interaction, "REJECTED", f"❌ Rejected by {interaction.user}")

        if interaction.guild and channel_id and message_id:
            link = msg_link(interaction.guild.id, int(channel_id), int(message_id))
        else:
            link = "(link unavailable)"

        await log_ledger(
            interaction.guild,
            f"❌ REJECTED • Sub#{submission_id} • Quest#{quest_id} → <@{user_id}> • {link}"
        )

        await interaction.response.defer(ephemeral=True)


# =========================
# COMMANDS
# =========================
async def quest_id_autocomplete(interaction: discord.Interaction, current: str):
    rows = await list_active_quests(limit=25)
    choices = []
    for qid, title, reward in rows:
        label = f"#{qid} • +{reward}🧧 • {title}"
        if current.strip():
            if current.strip().lower() not in label.lower():
                continue
        choices.append(app_commands.Choice(name=label[:100], value=int(qid)))
    return choices[:25]


class EventCommands(app_commands.Group):
    def __init__(self):
        super().__init__(name="event", description="Fortune of the Red Dragon (CNY Missions)")

    # -------- PLAYER: submit --------
    @app_commands.command(name="submit", description="Submit proof for a quest (screenshot required).")
    @app_commands.describe(
        quest_id="Quest ID (pick from autocomplete)",
        proof="Upload screenshot proof",
        note="Optional short note"
    )
    @app_commands.autocomplete(quest_id=quest_id_autocomplete)
    async def submit(
        self,
        interaction: discord.Interaction,
        quest_id: int,
        proof: discord.Attachment,
        note: str | None = None
    ):
        if interaction.channel_id != SUBMISSIONS_CHANNEL_ID:
            return await interaction.response.send_message("Use this command in the submissions channel.", ephemeral=True)

        quest = await get_quest(int(quest_id))
        if not quest:
            return await interaction.response.send_message("That quest ID does not exist.", ephemeral=True)

        _, q_title, q_body, q_bonus, q_reward, _, active, _, _ = quest
        if int(active) != 1:
            return await interaction.response.send_message("That quest is closed.", ephemeral=True)

        if proof.content_type and not proof.content_type.startswith("image/"):
            return await interaction.response.send_message("Please upload an image screenshot.", ephemeral=True)

        already = await user_has_submission_for_quest(interaction.user.id, int(quest_id))
        if already:
            return await interaction.response.send_message("You already submitted for that quest (pending/approved).", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="🧧 Quest Submission",
            description=(
                f"**Quest:** #{quest_id} — **{q_title}**\n"
                f"**Clasher:** {interaction.user.mention}\n"
                f"**Reward (on approval):** +{int(q_reward)} 🧧"
            ),
            color=COLOR_RED
        )
        embed.add_field(name="Note", value=note if note else "—", inline=False)
        embed.set_image(url=proof.url)
        embed.set_footer(text="Status: PENDING")

        submission_id = await insert_submission(
            user_id=interaction.user.id,
            quest_id=int(quest_id),
            proof_url=proof.url,
            note=note,
            message_id=0,
            channel_id=0,
        )

        view = ReviewView(submission_id=submission_id)
        msg = await interaction.channel.send(embed=embed, view=view)
        await update_submission_message(submission_id, msg.id, msg.channel.id)

        link = msg_link(interaction.guild.id, msg.channel.id, msg.id) if interaction.guild else "(link unavailable)"
        await log_ledger(
            interaction.guild,
            f"📮 SUBMITTED • Sub#{submission_id} • Quest#{quest_id} • {interaction.user.mention} • {link}"
        )

        await interaction.followup.send(f"✅ Submission received! ID **#{submission_id}** (pending review).", ephemeral=True)

    # -------- PLAYER: open --------
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
            msg = "You have no Red Envelopes 🧧. Complete quests to earn more!"
            if QUESTS_CHANNEL_ID:
                msg += f" Check <#{QUESTS_CHANNEL_ID}>."
            return await interaction.response.send_message(msg, ephemeral=True)

        weights = [t[1] for t in TIERS]
        tier_name, _, tier_points = random.choices(TIERS, weights=weights, k=1)[0]
        is_dragon = tier_name.startswith("🟡")

        ok = await consume_envelope_and_award(interaction.user.id, tier_points, is_dragon)
        if not ok:
            return await interaction.response.send_message("You have no envelopes.", ephemeral=True)

        envelopes2, points2, dragon2 = await get_user_stats(interaction.user.id)

        key = tier_name.split()[0]  # emoji token
        text = random.choice(FLAVOR.get(key, ["Fortune smiles upon you."]))

        completed = await count_user_approved(interaction.user.id)
        progress = f"{min(completed, PARTICIPATION_GOAL)}/{PARTICIPATION_GOAL}"

        # Choose color: gold for dragon tier, red otherwise
        embed_color = COLOR_GOLD if is_dragon else COLOR_RED

        embed = discord.Embed(
            title="🎁 Red Envelope Opened!",
            description=f"**{tier_name}**\n*{text}*",
            color=embed_color
        )

        # Thumbnail (optional)
        if OPEN_THUMBNAIL_URL:
            embed.set_thumbnail(url=OPEN_THUMBNAIL_URL)

        # Better hierarchy: show reward first, then totals
        embed.add_field(name="Reward", value=f"**+{tier_points} Fortune Points**", inline=False)
        embed.add_field(name="Total Points", value=f"**{points2}**", inline=True)
        embed.add_field(name="Dragon Marks", value=f"**{dragon2}**", inline=True)
        embed.add_field(name="Remaining Envelopes", value=f"**{envelopes2}**", inline=True)

        # Progress tracker
        embed.add_field(
            name="Progress to Participation Reward",
            value=f"**{progress}** missions approved",
            inline=False
        )

        # Close the loop
        footer = "Fortune favors the consistent."
        if envelopes2 == 0 and QUESTS_CHANNEL_ID:
            footer = f"Out of envelopes? Head to #{interaction.guild.get_channel(QUESTS_CHANNEL_ID).name} for new missions."
        elif envelopes2 == 0:
            footer = "You're out of envelopes—check the quests channel for new missions."

        embed.set_footer(text=footer)

        await log_ledger(
            interaction.guild,
            f"🎁 OPENED • {interaction.user.mention} → {tier_name} (+{tier_points} pts) • envelopes now {envelopes2}"
        )
        await interaction.response.send_message(embed=embed)

    # -------- PLAYER: daily --------
    @app_commands.command(name="daily", description="Claim a free envelope (6h cooldown).")
    async def daily(self, interaction: discord.Interaction):
        can, remaining = await can_claim_daily(interaction.user.id)
        if not can:
            mins = max(1, remaining // 60)
            return await interaction.response.send_message(f"⏳ Daily not ready. Try again in ~{mins} min.", ephemeral=True)

        await set_daily_claim(interaction.user.id)
        await add_envelopes(interaction.user.id, DAILY_ENVELOPES_AWARD)

        envelopes, points, dragon = await get_user_stats(interaction.user.id)
        await log_ledger(interaction.guild, f"🧧 DAILY • {interaction.user.mention} claimed +{DAILY_ENVELOPES_AWARD}🧧")
        await interaction.response.send_message(
            f"✅ You claimed **+{DAILY_ENVELOPES_AWARD} 🧧**.\nNow: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**",
            ephemeral=True
        )

    # -------- PLAYER: balance --------
    @app_commands.command(name="balance", description="Check your envelopes, points, and progress.")
    async def balance(self, interaction: discord.Interaction):
        envelopes, points, dragon = await get_user_stats(interaction.user.id)
        completed = await count_user_approved(interaction.user.id)
        embed = discord.Embed(title="🧧 Your Fortune", color=COLOR_RED)
        embed.add_field(name="Red Envelopes", value=str(envelopes), inline=True)
        embed.add_field(name="Fortune Points", value=str(points), inline=True)
        embed.add_field(name="Dragon Marks", value=str(dragon), inline=True)
        embed.add_field(
            name="Participation Progress",
            value=f"{min(completed, PARTICIPATION_GOAL)}/{PARTICIPATION_GOAL} approved missions",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -------- PLAYER: leaderboard --------
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
            color=COLOR_RED
        )
        await interaction.response.send_message(embed=embed)

    # -------- STAFF: postquest --------
    @app_commands.command(name="postquest", description="(Staff) Post a quest (mission) to the quests channel.")
    @app_commands.describe(
        title="Quest title (short and clear)",
        quest="Quest instructions (full text)",
        reward_envelopes="How many envelopes this quest grants on approval",
        bonus="Optional bonus text (purely informational)",
        image="Optional image/banner for the quest",
        pin="Pin the quest message"
    )
    async def postquest(
        self,
        interaction: discord.Interaction,
        title: str,
        quest: str,
        reward_envelopes: int = 1,
        bonus: str | None = None,
        image: discord.Attachment | None = None,
        pin: bool = False
    ):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        if QUESTS_CHANNEL_ID == 0:
            return await interaction.response.send_message("QUESTS_CHANNEL_ID is not set in .env", ephemeral=True)

        if not interaction.guild:
            return await interaction.response.send_message("This command must be used in a server.", ephemeral=True)

        if reward_envelopes < 1 or reward_envelopes > 10:
            return await interaction.response.send_message("reward_envelopes must be between 1 and 10.", ephemeral=True)

        ch = interaction.guild.get_channel(QUESTS_CHANNEL_ID)
        if not ch:
            return await interaction.response.send_message("I can't access the quests channel (check ID/permissions).", ephemeral=True)

        image_url = None
        if image:
            if image.content_type and image.content_type.startswith("image/"):
                image_url = image.url
            else:
                return await interaction.response.send_message("Please upload a valid image file.", ephemeral=True)

        # Post message first (we'll update quest_id after we insert)
        embed = discord.Embed(
            title=f"🧧 New Quest — {title}",
            description=quest,
            color=COLOR_RED
        )
        embed.add_field(name="Reward", value=f"**+{reward_envelopes} 🧧** (on approval)", inline=False)

        if bonus:
            embed.add_field(name="🌟 Bonus (Optional)", value=bonus, inline=False)

        embed.add_field(
            name="📮 How to Submit",
            value=f"Go to <#{SUBMISSIONS_CHANNEL_ID}> and use:\n`/event submit quest_id:<ID>` (attach proof)",
            inline=False
        )

        if image_url:
            embed.set_image(url=image_url)

        await interaction.response.defer(ephemeral=True)

        try:
            msg = await ch.send(embed=embed)
        except discord.Forbidden:
            return await interaction.followup.send("I don't have permission to post in the quests channel.", ephemeral=True)

        if pin:
            try:
                await msg.pin(reason="Event quest")
            except discord.Forbidden:
                pass

        quest_id = await create_quest(
            title=title,
            body=quest,
            bonus=bonus,
            reward_envelopes=reward_envelopes,
            image_url=image_url,
            message_id=msg.id,
            channel_id=msg.channel.id
        )

        # Edit message to include quest id prominently
        embed.title = f"🧧 Quest #{quest_id} — {title}"
        embed.set_footer(text=f"Quest ID: {quest_id} • Use /event submit quest_id:{quest_id}")
        await msg.edit(embed=embed)

        link = msg_link(interaction.guild.id, msg.channel.id, msg.id)
        await log_ledger(interaction.guild, f"📌 QUEST POSTED • Quest#{quest_id} • +{reward_envelopes}🧧 • {link}")
        await interaction.followup.send(f"✅ Posted Quest **#{quest_id}** in {ch.mention}.", ephemeral=True)

    # -------- STAFF: closequest --------
    @app_commands.command(name="closequest", description="(Staff) Close a quest so it can’t be submitted anymore.")
    @app_commands.describe(quest_id="Quest ID to close")
    async def closequest(self, interaction: discord.Interaction, quest_id: int):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        q = await get_quest(int(quest_id))
        if not q:
            return await interaction.response.send_message("Quest not found.", ephemeral=True)

        await close_quest(int(quest_id))
        await log_ledger(interaction.guild, f"🔒 QUEST CLOSED • Quest#{quest_id} by {interaction.user.mention}")
        await interaction.response.send_message(f"✅ Quest #{quest_id} closed.", ephemeral=True)

    # -------- STAFF: revoke --------
    @app_commands.command(name="revoke", description="(Staff) Revoke an approved submission (removes awarded envelopes if possible).")
    @app_commands.describe(submission_id="Submission ID number (e.g. 12)")
    async def revoke(self, interaction: discord.Interaction, submission_id: int):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        sub = await get_submission(int(submission_id))
        if not sub:
            return await interaction.response.send_message("Submission not found.", ephemeral=True)

        sid, user_id, quest_id, _, _, status, awarded, message_id, channel_id = sub

        if status == "REVOKED":
            return await interaction.response.send_message("This submission is already revoked.", ephemeral=True)

        if status != "APPROVED":
            return await interaction.response.send_message(f"Only APPROVED submissions can be revoked. Current: {status}", ephemeral=True)

        await set_submission_status(int(submission_id), "REVOKED")

        # remove exactly what was awarded (quest-based reward)
        remove_amount = int(awarded)
        removed = await try_remove_envelopes(int(user_id), remove_amount)

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

        link = "(link unavailable)"
        if interaction.guild and channel_id and message_id:
            link = msg_link(interaction.guild.id, int(channel_id), int(message_id))

        if removed:
            text = (
                f"✅ Revoked submission **#{submission_id}**.\n"
                f"➖ Removed **{remove_amount} envelope(s)** from <@{user_id}>.\n"
                f"Now: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**"
            )
            await log_ledger(interaction.guild, f"🧹 REVOKED • Sub#{sid} • -{remove_amount}🧧 → <@{user_id}> • {link}")
        else:
            text = (
                f"✅ Revoked submission **#{submission_id}**.\n"
                f"⚠️ Could NOT remove **{remove_amount} envelope(s)** (likely already spent).\n"
                f"Please use adjust commands if needed.\n"
                f"Now: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**"
            )
            await log_ledger(interaction.guild, f"🧹 REVOKED • Sub#{sid} • envelopes NOT removed → <@{user_id}> • {link}")

        await interaction.response.send_message(text, ephemeral=True)

    # -------- STAFF: adjust --------
    @app_commands.command(name="adjustpoints", description="(Staff) Adjust a user's Fortune Points (+/-). Clamped at 0.")
    @app_commands.describe(user="Target user", amount="Use negative to subtract (e.g., -4)")
    async def adjustpoints(self, interaction: discord.Interaction, user: discord.Member, amount: int):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)

        before, after = await adjust_user_field(user.id, "points", amount)
        envelopes, points, dragon = await get_user_stats(user.id)

        await log_ledger(interaction.guild, f"🛠️ ADJUST • points {before}->{after} (Δ{amount}) • {user.mention} by {interaction.user.mention}")
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

        await log_ledger(interaction.guild, f"🛠️ ADJUST • envelopes {before}->{after} (Δ{amount}) • {user.mention} by {interaction.user.mention}")
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

        await log_ledger(interaction.guild, f"🛠️ ADJUST • dragon {before}->{after} (Δ{amount}) • {user.mention} by {interaction.user.mention}")
        await interaction.response.send_message(
            f"✅ Dragon Marks updated for {user.mention}: **{before} → {after}**\nNow: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**",
            ephemeral=True
        )

    # -------- STAFF: reset (for testing) --------
    @app_commands.command(name="reset", description="(Staff) Reset ALL event data (DANGEROUS).")
    @app_commands.describe(confirm="Type: CONFIRM")
    async def reset(self, interaction: discord.Interaction, confirm: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        if confirm != "CONFIRM":
            return await interaction.response.send_message("Type **CONFIRM** to reset.", ephemeral=True)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM submissions")
            await db.execute("DELETE FROM quests")
            await db.execute("DELETE FROM users")
            await db.execute("DELETE FROM daily_claims")
            await db.commit()

        await log_ledger(interaction.guild, f"🧨 RESET • Event data wiped by {interaction.user.mention}")
        await interaction.response.send_message("✅ Event data reset complete.", ephemeral=True)


# =========================
# BOT CLASS (clean sync + no duplicates)
# =========================
class FortuneBot(commands.Bot):
    async def setup_hook(self):
        # Register group once
        if not any(cmd.name == "event" for cmd in self.tree.get_commands()):
            self.tree.add_command(EventCommands())

        # Sync to your guild (fast updates)
        try:
            if GUILD_ID and GUILD_ID != 0:
                guild = discord.Object(id=GUILD_ID)
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

    # Re-register persistent views for pending submissions (buttons survive restarts)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT submission_id FROM submissions WHERE status='PENDING'") as cur:
            pending = await cur.fetchall()

    for (submission_id,) in pending:
        bot.add_view(ReviewView(submission_id=int(submission_id)))

    print("Local tree commands:", [c.name for c in bot.tree.get_commands()])
    print(f"Logged in as {bot.user} ✅")


bot.run(BOT_TOKEN)

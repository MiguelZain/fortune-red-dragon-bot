import os
import time
import random
import math
import asyncio
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

# Public channel where users submit quests
SUBMISSIONS_CHANNEL_ID = int(os.getenv("SUBMISSIONS_CHANNEL_ID", "0"))

# Staff-only channel where submission embeds should be posted
PRIVATE_SUBMISSIONS_CHANNEL_ID = 1461470513649160356  # hard-coded per your request

ENVELOPES_CHANNEL_ID = int(os.getenv("ENVELOPES_CHANNEL_ID", "0"))
LEDGER_CHANNEL_ID = int(os.getenv("LEDGER_CHANNEL_ID", "0"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "event.db")

# Neo-only /reset owner
OWNER_USER_ID = 736938613903720458

# Role to ping + self-assign
RED_DRAGON_HUNTERS_ROLE_ID = 1470440988748156992

# Optional: /open thumbnail URLs by tier (set these later)
OPEN_THUMBNAIL_GREEN = os.getenv("OPEN_THUMBNAIL_GREEN", "").strip()
OPEN_THUMBNAIL_BLUE = os.getenv("OPEN_THUMBNAIL_BLUE", "").strip()
OPEN_THUMBNAIL_PURPLE = os.getenv("OPEN_THUMBNAIL_PURPLE", "").strip()
OPEN_THUMBNAIL_GOLD = os.getenv("OPEN_THUMBNAIL_GOLD", "").strip()

# Backwards-compatible single thumbnail (optional). Used only if tier thumb missing.
OPEN_THUMBNAIL_URL = os.getenv("OPEN_THUMBNAIL_URL", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Put it in your .env file.")

# =========================
# EVENT SETTINGS
# =========================
OPEN_COOLDOWN_SECONDS = 10
DAILY_COOLDOWN_SECONDS = 6 * 60 * 60  # 6 hours

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
COLOR_GRAY = 0x808080  # for closed quests

FOOTER_DEV = "Developed by Neo"

# Tier-specific flavor libraries
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
# NOTE: role assignment via /role works best if Members Intent is enabled in Dev Portal too.
intents.members = True

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
            created_at INTEGER NOT NULL,
            expires_at INTEGER
        )
        """)

        # submissions (player submissions tied to quest_id)
        # NOTE: proof_url stays in DB for backwards-compatibility; we store "" when no image is provided.
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

        # --- MIGRATION: add expires_at if missing (safe to run every startup)
        try:
            await db.execute("ALTER TABLE quests ADD COLUMN expires_at INTEGER")
        except Exception:
            pass

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


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def top_leaderboard_page(offset: int, limit: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, points, envelopes, dragon
            FROM users
            ORDER BY points DESC, dragon DESC, envelopes DESC, user_id ASC
            LIMIT ? OFFSET ?
        """, (int(limit), int(offset))) as cur:
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


# -------- rank helpers (exact rank + context) --------
async def get_rank_row(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, int(user_id))
        async with db.execute("""
            WITH ranked AS (
                SELECT
                    user_id, points, envelopes, dragon,
                    ROW_NUMBER() OVER (ORDER BY points DESC, dragon DESC, envelopes DESC, user_id ASC) AS r,
                    COUNT(*) OVER () AS total
                FROM users
            )
            SELECT user_id, points, envelopes, dragon, r, total
            FROM ranked
            WHERE user_id = ?
        """, (int(user_id),)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "user_id": int(row[0]),
                "points": int(row[1]),
                "envelopes": int(row[2]),
                "dragon": int(row[3]),
                "rank": int(row[4]),
                "total": int(row[5]),
            }


async def get_rank_context(rank: int, around: int = 2):
    start_r = max(1, int(rank) - int(around))
    end_r = int(rank) + int(around)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            WITH ranked AS (
                SELECT
                    user_id, points, envelopes, dragon,
                    ROW_NUMBER() OVER (ORDER BY points DESC, dragon DESC, envelopes DESC, user_id ASC) AS r
                FROM users
            )
            SELECT r, user_id, points, envelopes, dragon
            FROM ranked
            WHERE r BETWEEN ? AND ?
            ORDER BY r ASC
        """, (int(start_r), int(end_r))) as cur:
            return await cur.fetchall()


# -------- quests --------
async def create_quest(
    title: str,
    body: str,
    bonus: str | None,
    reward_envelopes: int,
    image_url: str | None,
    message_id: int,
    channel_id: int,
    expires_at: int | None = None
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO quests(title, body, bonus, reward_envelopes, image_url, active, message_id, channel_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """, (
            title.strip(),
            body.strip(),
            bonus.strip() if bonus else None,
            int(reward_envelopes),
            image_url,
            int(message_id) if message_id else None,
            int(channel_id) if channel_id else None,
            int(time.time()),
            int(expires_at) if expires_at else None,
        ))
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
            return int(row[0])


async def get_quest(quest_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT quest_id, title, body, bonus, reward_envelopes, image_url, active, message_id, channel_id, created_at, expires_at
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


async def get_expired_active_quests(now_ts: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT quest_id, title, message_id, channel_id, expires_at
            FROM quests
            WHERE active = 1
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            ORDER BY expires_at ASC
        """, (int(now_ts),)) as cur:
            return await cur.fetchall()


# -------- submissions --------
async def insert_submission(
    user_id: int,
    quest_id: int,
    image_url: str | None,
    text: str | None,
    message_id: int,
    channel_id: int
) -> int:
    # DB column is proof_url NOT NULL, so store "" when no image is provided.
    img = (image_url or "").strip()
    txt = text.strip() if text else None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO submissions(user_id, quest_id, proof_url, note, status, reward_envelopes_awarded, message_id, channel_id, created_at)
            VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)
        """, (
            int(user_id),
            int(quest_id),
            img,
            txt,
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


def tier_thumbnail_for_key(key: str) -> str:
    mapping = {
        "🟢": OPEN_THUMBNAIL_GREEN,
        "🔵": OPEN_THUMBNAIL_BLUE,
        "🟣": OPEN_THUMBNAIL_PURPLE,
        "🟡": OPEN_THUMBNAIL_GOLD,
    }
    url = (mapping.get(key) or "").strip()
    if url:
        return url
    return OPEN_THUMBNAIL_URL  # fallback (maybe empty)


async def safe_send(channel: discord.abc.Messageable | None, content: str = "", embed: discord.Embed | None = None):
    if not channel:
        return
    try:
        await channel.send(content=content, embed=embed)
    except Exception:
        pass


def guild_only():
    # Makes commands appear as individual, guild-scoped commands (fast + no duplicate integration entries).
    if GUILD_ID and GUILD_ID != 0:
        return app_commands.guilds(GUILD_ID)
    return lambda x: x


def mark_quest_embed_closed(embed: discord.Embed, reason: str):
    # Avoid duplicating Status field if closed twice
    existing = [f for f in embed.fields if f.name.strip().lower() != "status"]
    embed.clear_fields()
    for f in existing:
        embed.add_field(name=f.name, value=f.value, inline=f.inline)

    # Title formatting
    t = (embed.title or "").strip()
    upper = t.upper()
    if "CLOSED" not in upper:
        embed.title = f"🔒 CLOSED • {t}" if t else "🔒 CLOSED"
    else:
        if not t.startswith("🔒"):
            embed.title = f"🔒 {t}"

    # Gray color + footer
    embed.colour = COLOR_GRAY
    embed.add_field(name="Status", value=reason, inline=False)
    embed.set_footer(text="Quest Closed")
    return embed


async def try_edit_quest_message_closed(bot: commands.Bot, quest_id: int, reason: str):
    q = await get_quest(int(quest_id))
    if not q:
        return False

    _, q_title, _, _, _, _, _, message_id, channel_id, _, _ = q
    if not message_id or not channel_id:
        return False

    # Try every guild the bot is in (usually 1)
    for g in bot.guilds:
        ch = g.get_channel(int(channel_id))
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(int(message_id))
        except Exception:
            continue

        if not msg:
            continue

        if msg.embeds:
            emb = msg.embeds[0]
        else:
            emb = discord.Embed(title=f"🧧 Quest #{quest_id} — {q_title}", color=COLOR_GRAY)

        emb = mark_quest_embed_closed(emb, reason=reason)

        try:
            await msg.edit(embed=emb)
            return True
        except Exception:
            return False

    return False


async def auto_close_loop(bot: commands.Bot):
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now_ts = int(time.time())
            expired = await get_expired_active_quests(now_ts)

            for (quest_id, title, message_id, channel_id, expires_at) in expired:
                await close_quest(int(quest_id))

                # Edit the original quest message to show CLOSED (best-effort)
                try:
                    await try_edit_quest_message_closed(
                        bot,
                        quest_id=int(quest_id),
                        reason="Auto-closed (time expired)."
                    )
                except Exception:
                    pass

                await log_ledger(
                    bot.guilds[0] if bot.guilds else None,
                    f"⏳ AUTO-CLOSED • Quest#{quest_id} • “{title}”"
                )

        except Exception:
            pass

        await asyncio.sleep(60)


# =========================
# APPROVAL VIEW (PERSISTENT)
# =========================
class ReviewView(discord.ui.View):
    def __init__(self, submission_id: int):
        super().__init__(timeout=None)
        self.submission_id = int(submission_id)

        self.approve.custom_id = f"review:approve:{self.submission_id}"
        self.reject.custom_id = f"review:reject:{self.submission_id}"

    async def finalize_message(self, interaction: discord.Interaction, status: str, status_text: str):
        for item in self.children:
            item.disabled = True

        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(color=COLOR_RED)

        embed.add_field(name="Review Result", value=status_text, inline=False)
        embed.set_footer(text=FOOTER_DEV)

        await interaction.message.edit(embed=embed, view=self)
        await set_submission_status(self.submission_id, status)

    async def notify_user_in_submit_channel(self, guild: discord.Guild | None, user_id: int, text: str):
        if not guild or SUBMISSIONS_CHANNEL_ID == 0:
            return
        submit_ch = guild.get_channel(SUBMISSIONS_CHANNEL_ID)
        await safe_send(submit_ch, content=f"<@{user_id}> {text}")

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

        _, q_title, _, _, q_reward, _, _, _, _, _, _ = quest
        reward = int(q_reward)

        await add_envelopes(int(user_id), reward)
        await mark_submission_award(self.submission_id, reward)

        await self.finalize_message(
            interaction,
            "APPROVED",
            f"✅ Approved by {interaction.user.mention} • +{reward} 🧧"
        )

        if interaction.guild and channel_id and message_id:
            link = msg_link(interaction.guild.id, int(channel_id), int(message_id))
        else:
            link = "(link unavailable)"

        await log_ledger(
            interaction.guild,
            f"✅ APPROVED • Sub#{submission_id} • Quest#{quest_id} • +{reward}🧧 → <@{user_id}> • by {interaction.user.mention} • {link}"
        )

        await self.notify_user_in_submit_channel(
            interaction.guild,
            int(user_id),
            f"✅ **Your submission #{submission_id}** for **Quest #{quest_id} — {q_title}** was **APPROVED**. "
            f"You received **+{reward} 🧧**. 🐉"
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

        quest = await get_quest(int(quest_id))
        q_title = quest[1] if quest else "Unknown Quest"

        await self.finalize_message(
            interaction,
            "REJECTED",
            f"❌ Rejected by {interaction.user.mention}"
        )

        if interaction.guild and channel_id and message_id:
            link = msg_link(interaction.guild.id, int(channel_id), int(message_id))
        else:
            link = "(link unavailable)"

        await log_ledger(
            interaction.guild,
            f"❌ REJECTED • Sub#{submission_id} • Quest#{quest_id} → <@{user_id}> • by {interaction.user.mention} • {link}"
        )

        await self.notify_user_in_submit_channel(
            interaction.guild,
            int(user_id),
            f"❌ **Your submission #{submission_id}** for **Quest #{quest_id} — {q_title}** was **Rejected**. "
            f"You can **try again** by making a new submission, contact mods for assistance."
        )

        await interaction.response.defer(ephemeral=True)


# =========================
# LEADERBOARD VIEW (PAGED)
# =========================
class LeaderboardView(discord.ui.View):
    def __init__(self, page: int, per_page: int, max_pages: int, limit_total: int):
        super().__init__(timeout=120)
        self.page = int(page)
        self.per_page = int(per_page)
        self.max_pages = int(max_pages)
        self.limit_total = int(limit_total)

        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.max_pages

    async def build_embed(self) -> discord.Embed:
        offset = (self.page - 1) * self.per_page
        rows = await top_leaderboard_page(offset=offset, limit=self.per_page)

        start_rank = offset + 1
        lines = []
        for idx, (user_id, points, envelopes, dragon) in enumerate(rows):
            rank = start_rank + idx
            lines.append(f"**{rank}.** <@{user_id}> — **{points} pts** • 🧧{envelopes} • 🐉{dragon}")

        if not lines:
            lines = ["No data yet."]

        embed = discord.Embed(
            title="🏆 Fortune Leaderboard",
            description="\n".join(lines),
            color=COLOR_RED
        )
        embed.add_field(name="Page", value=f"{self.page}/{self.max_pages}", inline=True)
        embed.add_field(name="Scope", value=f"Top {self.limit_total}", inline=True)
        embed.add_field(
        name="Sorting",
        value="Points ↓, then Dragon Marks ↓, then Envelopes ↓ (ties by user_id).",
        inline=False
        )
        embed.set_footer(text=FOOTER_DEV)
        return embed

    @discord.ui.button(label="⬅ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(1, self.page - 1)
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.max_pages
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.max_pages, self.page + 1)
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.max_pages
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)


# =========================
# AUTOCOMPLETE
# =========================
async def quest_id_autocomplete(interaction: discord.Interaction, current: str):
    rows = await list_active_quests(limit=25)
    choices = []
    for qid, title, reward in rows:
        label = f"#{qid} • +{reward}🧧 • {title}"
        if current.strip() and current.strip().lower() not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=int(qid)))
    return choices[:25]


# =========================
# BOT CLASS
# =========================
class FortuneBot(commands.Bot):
    async def setup_hook(self):
        # Sync commands (guild-only if GUILD_ID set; otherwise global)
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
# COMMANDS (NO /event GROUP)
# =========================

# -------- PLAYER: submit --------
@bot.tree.command(name="submit", description="Submit for a quest (image and/or text).")
@guild_only()
@app_commands.describe(
    quest_id="Quest ID (pick from autocomplete)",
    image="Optional: upload an image (screenshot/photo)",
    text="Optional: type text if the quest doesn't need an image"
)
@app_commands.autocomplete(quest_id=quest_id_autocomplete)
async def submit(
    interaction: discord.Interaction,
    quest_id: int,
    image: discord.Attachment | None = None,
    text: str | None = None
):
    # Must run in the public submissions channel
    if interaction.channel_id != SUBMISSIONS_CHANNEL_ID:
        return await interaction.response.send_message("Use this command in the submissions channel.", ephemeral=True)

    if not interaction.guild:
        return await interaction.response.send_message("This command must be used in a server.", ephemeral=True)

    # Require at least one of (image/text) so empty submissions are impossible
    has_text = bool(text and text.strip())
    has_image = image is not None
    if not has_text and not has_image:
        return await interaction.response.send_message(
            "You must provide **either an image or text** (or both).",
            ephemeral=True
        )

    quest = await get_quest(int(quest_id))
    if not quest:
        return await interaction.response.send_message("That quest ID does not exist.", ephemeral=True)

    _, q_title, _, _, q_reward, _, active, _, _, _, _ = quest
    if int(active) != 1:
        return await interaction.response.send_message("That quest is closed.", ephemeral=True)

    if has_image:
        if image.content_type and not image.content_type.startswith("image/"):
            return await interaction.response.send_message("Please upload a valid image file.", ephemeral=True)

    already = await user_has_submission_for_quest(interaction.user.id, int(quest_id))
    if already:
        return await interaction.response.send_message(
            "You already submitted for that quest (pending/approved).",
            ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)

    # Build STAFF-ONLY embed
    embed = discord.Embed(
        title="🧧 Quest Submission (Staff Review)",
        description=(
            f"**Quest:** #{quest_id} — **{q_title}**\n"
            f"**Clasher:** {interaction.user.mention}\n"
            f"**User ID:** `{interaction.user.id}`\n"
            f"**Reward (on approval):** +{int(q_reward)} 🧧"
        ),
        color=COLOR_RED
    )
    embed.add_field(name="Text", value=text.strip() if has_text else "—", inline=False)
    embed.add_field(name="Status", value="PENDING", inline=False)

    image_url = None
    if has_image:
        image_url = image.url
        embed.set_image(url=image_url)

    embed.set_footer(text=FOOTER_DEV)

    submission_id = await insert_submission(
        user_id=interaction.user.id,
        quest_id=int(quest_id),
        image_url=image_url,
        text=text,
        message_id=0,
        channel_id=0,
    )

    view = ReviewView(submission_id=submission_id)

    # Send the submission to the PRIVATE staff channel
    private_ch = interaction.guild.get_channel(PRIVATE_SUBMISSIONS_CHANNEL_ID)
    if not private_ch:
        await log_ledger(interaction.guild, "⚠️ WARNING: Private submissions channel not found or not accessible.")
        return await interaction.followup.send(
            "⚠️ I couldn't access the staff review channel. Please contact staff/admin to fix permissions.",
            ephemeral=True
        )

    msg = await private_ch.send(embed=embed, view=view)
    await update_submission_message(submission_id, msg.id, msg.channel.id)

    link = msg_link(interaction.guild.id, msg.channel.id, msg.id)
    await log_ledger(
        interaction.guild,
        f"📮 SUBMITTED • Sub#{submission_id} • Quest#{quest_id} • {interaction.user.mention} • {link}"
    )

    await interaction.followup.send(
        f"✅ Submission received! ID **#{submission_id}** (pending review).",
        ephemeral=True
    )


# -------- PLAYER: open --------
@bot.tree.command(name="open", description="Open 1 Red Envelope and reveal your fortune.")
@guild_only()
async def open_cmd(interaction: discord.Interaction):
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

    key = tier_name.split()[0]  # 🟢 / 🔵 / 🟣 / 🟡
    text = random.choice(FLAVOR.get(key, ["Fortune smiles upon you."]))

    completed = await count_user_approved(interaction.user.id)
    progress = f"{min(completed, PARTICIPATION_GOAL)}/{PARTICIPATION_GOAL}"

    embed_color = COLOR_GOLD if is_dragon else COLOR_RED
    embed = discord.Embed(
        title="🎁 Red Envelope Opened!",
        description=f"**{tier_name}**\n*{text}*",
        color=embed_color
    )

    thumb = tier_thumbnail_for_key(key)
    if thumb:
        embed.set_thumbnail(url=thumb)

    embed.add_field(name="Reward", value=f"**+{tier_points} Fortune Points**", inline=False)
    embed.add_field(name="Total Points", value=f"**{points2}**", inline=True)
    embed.add_field(name="Dragon Marks", value=f"**{dragon2}**", inline=True)
    embed.add_field(name="Remaining Envelopes", value=f"**{envelopes2}**", inline=True)
    embed.add_field(
        name="Progress to Participation Reward",
        value=f"**{progress}** missions approved",
        inline=False
    )

    if envelopes2 == 0 and QUESTS_CHANNEL_ID:
        embed.add_field(
            name="Tip",
            value=f"Out of envelopes? Head to <#{QUESTS_CHANNEL_ID}> for new missions.",
            inline=False
        )

    embed.set_footer(text=FOOTER_DEV)

    await log_ledger(
        interaction.guild,
        f"🎁 OPENED • {interaction.user.mention} → {tier_name} (+{tier_points} pts) • envelopes now {envelopes2}"
    )
    await interaction.response.send_message(embed=embed)


# -------- PLAYER: daily --------
@bot.tree.command(name="daily", description="Claim a free envelope (6h cooldown).")
@guild_only()
async def daily(interaction: discord.Interaction):
    can, remaining = await can_claim_daily(interaction.user.id)
    if not can:
        mins = max(1, remaining // 60)
        return await interaction.response.send_message(f"⏳ Daily not ready. Try again in ~{mins} min.", ephemeral=True)

    # RNG distribution:
    # 40% -> 1, 30% -> 2, 20% -> 3, 10% -> 4
    awarded = random.choices([1, 2, 3, 4], weights=[40, 30, 20, 10], k=1)[0]

    await set_daily_claim(interaction.user.id)
    await add_envelopes(interaction.user.id, awarded)

    envelopes, points, dragon = await get_user_stats(interaction.user.id)
    await log_ledger(interaction.guild, f"🧧 DAILY • {interaction.user.mention} claimed +{awarded}🧧")
    await interaction.response.send_message(
        f"✅ You claimed **+{awarded} 🧧**.\nNow: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**",
        ephemeral=True
    )


# -------- PLAYER: balance --------
@bot.tree.command(name="balance", description="Check your envelopes, points, and progress.")
@guild_only()
async def balance(interaction: discord.Interaction):
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
    embed.set_footer(text=FOOTER_DEV)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# -------- PLAYER: leaderboard --------
@bot.tree.command(name="leaderboard", description="Top Fortune Points (paged).")
@guild_only()
async def leaderboard(interaction: discord.Interaction):
    total = await count_users()
    if total <= 0:
        return await interaction.response.send_message("No data yet.", ephemeral=True)

    limit_total = min(100, total)
    per_page = 10
    max_pages = max(1, math.ceil(limit_total / per_page))

    view = LeaderboardView(page=1, per_page=per_page, max_pages=max_pages, limit_total=limit_total)
    embed = await view.build_embed()
    await interaction.response.send_message(embed=embed, view=view)


# -------- PLAYER: rank --------
@bot.tree.command(name="rank", description="Show exact rank, totals, and nearby players.")
@guild_only()
@app_commands.describe(user="Optional: check someone else's rank")
async def rank(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user

    r = await get_rank_row(int(target.id))
    if not r:
        return await interaction.response.send_message("No rank data yet.", ephemeral=True)

    ctx = await get_rank_context(r["rank"], around=2)

    lines = []
    for (rk, uid, pts, env, drg) in ctx:
        marker = "➡️ " if int(uid) == int(target.id) else ""
        lines.append(f"{marker}**#{rk}** <@{uid}> — **{pts} pts** • 🧧{env} • 🐉{drg}")

    embed = discord.Embed(
        title="📊 Fortune Rank",
        description="\n".join(lines) if lines else "—",
        color=COLOR_RED
    )
    embed.add_field(name="Player", value=target.mention, inline=False)
    embed.add_field(name="Rank", value=f"**#{r['rank']} / {r['total']}**", inline=True)
    embed.add_field(name="Points", value=f"**{r['points']}**", inline=True)
    embed.add_field(name="Envelopes", value=f"**{r['envelopes']}**", inline=True)
    embed.add_field(name="Dragon Marks", value=f"**{r['dragon']}**", inline=True)
    embed.set_footer(text=FOOTER_DEV)
    await interaction.response.send_message(embed=embed, ephemeral=False)


# -------- PLAYER: role --------
@bot.tree.command(name="role", description="Get the RedDragonHunters role.")
@guild_only()
async def role_cmd(interaction: discord.Interaction):
    # Acknowledge instantly to avoid "Unknown interaction"
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        # If it's already acknowledged, we'll just use followup
        pass

    if not interaction.guild:
        return await interaction.followup.send("Server only.", ephemeral=True)

    role = interaction.guild.get_role(RED_DRAGON_ROLE_ID)
    if not role:
        return await interaction.followup.send("Role not found.", ephemeral=True)

    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("Could not identify you as a server member.", ephemeral=True)

    if role in interaction.user.roles:
        return await interaction.followup.send("You already have the role ✅", ephemeral=True)

    try:
        await interaction.user.add_roles(role, reason="User used /role")
    except discord.Forbidden:
        return await interaction.followup.send("I don't have permission to give roles.", ephemeral=True)
    except Exception:
        return await interaction.followup.send("Failed to add role (unexpected error).", ephemeral=True)

    return await interaction.followup.send(f"✅ Granted {role.mention}", ephemeral=True)

# -------- STAFF: postquest --------
@bot.tree.command(name="postquest", description="(Staff) Post a quest (mission) to the quests channel.")
@guild_only()
@app_commands.describe(
    title="Quest title (short and clear)",
    quest="Quest instructions (full text)",
    reward_envelopes="How many envelopes this quest grants on approval",
    bonus="Optional bonus text (purely informational)",
    image="Optional image/banner for the quest",
    pin="Pin the quest message",
    duration="Optional: auto-close after this duration"
)
@app_commands.choices(duration=[
    app_commands.Choice(name="No auto-close", value="none"),
    app_commands.Choice(name="12 hours", value="12h"),
    app_commands.Choice(name="24 hours", value="24h"),
    app_commands.Choice(name="7 days", value="7d"),
])
async def postquest(
    interaction: discord.Interaction,
    title: str,
    quest: str,
    reward_envelopes: int = 1,
    bonus: str | None = None,
    image: discord.Attachment | None = None,
    pin: bool = False,
    duration: app_commands.Choice[str] | None = None
):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)

    if QUESTS_CHANNEL_ID == 0:
        return await interaction.response.send_message("QUESTS_CHANNEL_ID is not set in .env", ephemeral=True)

    if not interaction.guild:
        return await interaction.response.send_message("This command must be used in a server.", ephemeral=True)

    if reward_envelopes < 1 or reward_envelopes > 100:
        return await interaction.response.send_message("reward_envelopes must be between 1 and 100.", ephemeral=True)

    ch = interaction.guild.get_channel(QUESTS_CHANNEL_ID)
    if not ch:
        return await interaction.response.send_message("I can't access the quests channel (check ID/permissions).", ephemeral=True)

    image_url = None
    if image:
        if image.content_type and image.content_type.startswith("image/"):
            image_url = image.url
        else:
            return await interaction.response.send_message("Please upload a valid image file.", ephemeral=True)

    dur_val = duration.value if duration else "none"
    expires_at = None
    if dur_val == "12h":
        expires_at = int(time.time()) + 12 * 60 * 60
    elif dur_val == "24h":
        expires_at = int(time.time()) + 24 * 60 * 60
    elif dur_val == "7d":
        expires_at = int(time.time()) + 7 * 24 * 60 * 60

    embed = discord.Embed(
        title=f"🧧 New Quest — {title}",
        description=quest,
        color=COLOR_RED
    )
    embed.add_field(name="Reward", value=f"**+{reward_envelopes} 🧧** (on approval)", inline=False)

    if bonus:
        embed.add_field(name="", value=bonus, inline=False)

    embed.add_field(
        name="📮 How to Submit",
        value=(
            f"Go to <#{SUBMISSIONS_CHANNEL_ID}> and use:\n"
            f"`/submit quest_id:<ID>`\n"
            f"Attach an **image** or add **text** (at least one is required)."
        ),
        inline=False
    )

    if dur_val != "none":
        embed.add_field(name="⏳ Auto-Close", value=f"This quest will auto-close after **{dur_val}**.", inline=False)

    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text=FOOTER_DEV)

    await interaction.response.defer(ephemeral=True)

    # Ping role OUTSIDE the embed in the same message
    ping_text = f"<@&{RED_DRAGON_HUNTERS_ROLE_ID}>"
    try:
        msg = await ch.send(content=ping_text, embed=embed)
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
        channel_id=msg.channel.id,
        expires_at=expires_at
    )

    embed.title = f"🧧 Quest #{quest_id} — {title}"
    embed.add_field(name="Quest ID", value=str(quest_id), inline=True)
    if expires_at:
        embed.add_field(name="Auto-Close", value=dur_val, inline=True)
    embed.set_footer(text=FOOTER_DEV)

    await msg.edit(embed=embed)

    link = msg_link(interaction.guild.id, msg.channel.id, msg.id)
    await log_ledger(interaction.guild, f"📌 QUEST POSTED • Quest#{quest_id} • +{reward_envelopes}🧧 • by {interaction.user.mention} • {link}")
    await interaction.followup.send(f"✅ Posted Quest **#{quest_id}** in {ch.mention}.", ephemeral=True)


# -------- STAFF: closequest --------
@bot.tree.command(name="closequest", description="(Staff) Close a quest so it can’t be submitted anymore.")
@guild_only()
@app_commands.describe(quest_id="Quest ID to close")
async def closequest(interaction: discord.Interaction, quest_id: int):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)

    q = await get_quest(int(quest_id))
    if not q:
        return await interaction.response.send_message("Quest not found.", ephemeral=True)

    await close_quest(int(quest_id))

    # Auto-edit quest embed when manually closed
    await try_edit_quest_message_closed(
        bot,
        quest_id=int(quest_id),
        reason=f"Manually closed by {interaction.user.mention}."
    )

    await log_ledger(interaction.guild, f"🔒 QUEST CLOSED • Quest#{quest_id} by {interaction.user.mention}")
    await interaction.response.send_message(f"✅ Quest #{quest_id} closed.", ephemeral=True)


# -------- STAFF: revoke --------
@bot.tree.command(name="revoke", description="(Staff) Revoke an approved submission (removes awarded envelopes if possible).")
@guild_only()
@app_commands.describe(submission_id="Submission ID number (e.g. 12)")
async def revoke(interaction: discord.Interaction, submission_id: int):
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

    remove_amount = int(awarded)
    removed = await try_remove_envelopes(int(user_id), remove_amount)

    try:
        if interaction.guild and channel_id and message_id:
            ch = interaction.guild.get_channel(int(channel_id))
            if ch:
                msg = await ch.fetch_message(int(message_id))
                if msg and msg.embeds:
                    emb = msg.embeds[0]
                    emb.add_field(name="Status", value=f"⚠️ REVOKED by {interaction.user.mention}", inline=False)
                    emb.set_footer(text=FOOTER_DEV)
                    await msg.edit(embed=emb, view=None)
    except Exception:
        pass

    envelopes, points, dragon = await get_user_stats(int(user_id))

    link = "(link unavailable)"
    if interaction.guild and channel_id and message_id:
        link = msg_link(interaction.guild.id, int(channel_id), int(message_id))

    if removed:
        text_out = (
            f"✅ Revoked submission **#{submission_id}**.\n"
            f"➖ Removed **{remove_amount} envelope(s)** from <@{user_id}>.\n"
            f"Now: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**"
        )
        await log_ledger(interaction.guild, f"🧹 REVOKED • Sub#{sid} • -{remove_amount}🧧 → <@{user_id}> • by {interaction.user.mention} • {link}")
    else:
        text_out = (
            f"✅ Revoked submission **#{submission_id}**.\n"
            f"⚠️ Could NOT remove **{remove_amount} envelope(s)** (likely already spent).\n"
            f"Please use adjust commands if needed.\n"
            f"Now: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**"
        )
        await log_ledger(interaction.guild, f"🧹 REVOKED • Sub#{sid} • envelopes NOT removed → <@{user_id}> • by {interaction.user.mention} • {link}")

    await interaction.response.send_message(text_out, ephemeral=True)


# -------- STAFF: adjust --------
@bot.tree.command(name="adjustpoints", description="(Staff) Adjust a user's Fortune Points (+/-). Clamped at 0.")
@guild_only()
@app_commands.describe(user="Target user", amount="Use negative to subtract (e.g., -4)")
async def adjustpoints(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)

    before, after = await adjust_user_field(user.id, "points", amount)
    envelopes, points, dragon = await get_user_stats(user.id)

    await log_ledger(interaction.guild, f"🛠️ ADJUST • points {before}->{after} (Δ{amount}) • {user.mention} by {interaction.user.mention}")
    await interaction.response.send_message(
        f"✅ Points updated for {user.mention}: **{before} → {after}**\nNow: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**",
        ephemeral=True
    )


@bot.tree.command(name="adjustenvelopes", description="(Staff) Adjust a user's envelopes (+/-). Clamped at 0.")
@guild_only()
@app_commands.describe(user="Target user", amount="Use negative to subtract (e.g., -1)")
async def adjustenvelopes(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)

    before, after = await adjust_user_field(user.id, "envelopes", amount)
    envelopes, points, dragon = await get_user_stats(user.id)

    await log_ledger(interaction.guild, f"🛠️ ADJUST • envelopes {before}->{after} (Δ{amount}) • {user.mention} by {interaction.user.mention}")
    await interaction.response.send_message(
        f"✅ Envelopes updated for {user.mention}: **{before} → {after}**\nNow: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**",
        ephemeral=True
    )


@bot.tree.command(name="adjustdragon", description="(Staff) Adjust a user's Dragon Marks (+/-). Clamped at 0.")
@guild_only()
@app_commands.describe(user="Target user", amount="Use negative to subtract (e.g., -1)")
async def adjustdragon(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)

    before, after = await adjust_user_field(user.id, "dragon", amount)
    envelopes, points, dragon = await get_user_stats(user.id)

    await log_ledger(interaction.guild, f"🛠️ ADJUST • dragon {before}->{after} (Δ{amount}) • {user.mention} by {interaction.user.mention}")
    await interaction.response.send_message(
        f"✅ Dragon Marks updated for {user.mention}: **{before} → {after}**\nNow: 🧧 **{envelopes}** | ⭐ **{points}** | 🐉 **{dragon}**",
        ephemeral=True
    )


# -------- STAFF/OWNER: reset --------
@bot.tree.command(name="reset", description="(DANGEROUS) Reset ALL event data.")
@guild_only()
@app_commands.describe(confirm="Type: CONFIRM")
async def reset(interaction: discord.Interaction, confirm: str):
    # Neo-only even if staff
    if int(interaction.user.id) != int(OWNER_USER_ID):
        return await interaction.response.send_message("you are not neo!", ephemeral=True)

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

    # Start auto-close loop once
    if not hasattr(bot, "_auto_close_task"):
        bot._auto_close_task = bot.loop.create_task(auto_close_loop(bot))

    print("Local tree commands:", [c.name for c in bot.tree.get_commands()])
    print(f"Logged in as {bot.user} ✅")


bot.run(BOT_TOKEN)

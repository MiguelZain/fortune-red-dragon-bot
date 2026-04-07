import os
import time
import random
import math
import asyncio
import re
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
PRIVATE_SUBMISSIONS_CHANNEL_ID = 1461470513649160356  # hard-coded per your request
ENVELOPES_CHANNEL_ID = int(os.getenv("ENVELOPES_CHANNEL_ID", "0"))
LEDGER_CHANNEL_ID = int(os.getenv("LEDGER_CHANNEL_ID", "0"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "event.db")
OWNER_USER_ID = 736938613903720458

# Keep existing role ID for compatibility. You can change it later without touching the rest of the code.
EVENT_ROLE_ID = 1470440988748156992

# Optional thumbnails for /open by tier (still works if you keep old env names)
OPEN_THUMBNAIL_GREEN = os.getenv("OPEN_THUMBNAIL_GREEN", "").strip()
OPEN_THUMBNAIL_BLUE = os.getenv("OPEN_THUMBNAIL_BLUE", "").strip()
OPEN_THUMBNAIL_PURPLE = os.getenv("OPEN_THUMBNAIL_PURPLE", "").strip()
OPEN_THUMBNAIL_GOLD = os.getenv("OPEN_THUMBNAIL_GOLD", "").strip()
OPEN_THUMBNAIL_URL = os.getenv("OPEN_THUMBNAIL_URL", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Put it in your .env file.")

# =========================
# EVENT SETTINGS
# =========================
OPEN_COOLDOWN_SECONDS = 10
CLAIM_COOLDOWN_SECONDS = 6 * 60 * 60  # 6 hours
MILESTONE_TARGET = 7
MILESTONE_STEPS = [3, 5, 7, 10, 15]

TIERS = [
    ("🟢 Meadow Egg", 55, 1),
    ("🔵 Spring Basket", 30, 2),
    ("🟣 Dawn Relic", 12, 4),
    ("🟡 Golden Crest", 3, 8),  # also grants a crest
]

COLOR_PINK = 0xFF7AA2
COLOR_GOLD = 0xFFD700
COLOR_GRAY = 0x808080
COLOR_GREEN = 0x7ED957

FOOTER_DEV = "Developed by Neo"
EGG_EMOJI = "🥚"
CREST_EMOJI = "🌟"

FLAVOR = {
    "🟢": [
        "Morning dew settles on your basket, and a gentle blessing follows.",
        "A small spring omen finds its way to you.",
        "A quiet egg of fortune glows with fresh dawn light.",
        "The meadow stirs—small blessings are often the first to arrive.",
    ],
    "🔵": [
        "Your spring basket grows heavier with promise.",
        "Festival bells echo—fortune now walks beside you.",
        "A brighter blessing blooms among the lilies.",
        "The dawn breeze carries a stronger gift to your path.",
    ],
    "🟣": [
        "A relic of dawn answers your devotion.",
        "The sky blushes violet as a rare blessing unfolds.",
        "A sacred bloom opens—fortune rises with it.",
        "This is no ordinary blessing; the season clearly favors you.",
    ],
    "🟡": [
        "A Golden Crest shines in your hands—the season has marked you.",
        "The festival crown turns toward you, and its blessing is absolute.",
        "A radiant crest breaks through the dawn like sunlight through stained glass.",
        "The Easter vigil answers you with a golden sign.",
    ],
}

# =========================
# BOT SETUP
# =========================
intents = discord.Intents.default()
intents.members = True
open_cooldowns: dict[int, float] = {}

# =========================
# GENERIC HELPERS
# =========================
def guild_only():
    if GUILD_ID and GUILD_ID != 0:
        return app_commands.guilds(GUILD_ID)
    return lambda x: x


def is_staff(member: discord.abc.User) -> bool:
    if STAFF_ROLE_ID == 0:
        return False
    if not isinstance(member, discord.Member):
        return False
    return any(r.id == STAFF_ROLE_ID for r in member.roles)


def has_event_role(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return any(r.id == EVENT_ROLE_ID for r in member.roles)


def msg_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def tier_thumbnail_for_key(key: str) -> str:
    mapping = {
        "🟢": OPEN_THUMBNAIL_GREEN,
        "🔵": OPEN_THUMBNAIL_BLUE,
        "🟣": OPEN_THUMBNAIL_PURPLE,
        "🟡": OPEN_THUMBNAIL_GOLD,
    }
    url = (mapping.get(key) or "").strip()
    return url or OPEN_THUMBNAIL_URL


def normalize_igg_id(raw: str) -> str:
    cleaned = re.sub(r"\D", "", raw or "")
    if len(cleaned) < 5 or len(cleaned) > 25:
        raise ValueError("IGG ID must contain 5-25 digits.")
    return cleaned


def build_milestone_progress(completed: int) -> tuple[str, str]:
    current_hits = sum(1 for step in MILESTONE_STEPS if completed >= step)
    next_step = next((step for step in MILESTONE_STEPS if completed < step), None)

    ladder_parts = []
    for step in MILESTONE_STEPS:
        icon = "✅" if completed >= step else "▫️"
        ladder_parts.append(f"{icon}{step}")

    ladder = "  ".join(ladder_parts)
    if next_step is None:
        summary = f"Milestone Master • {completed}/{MILESTONE_STEPS[-1]}+ approved quests"
    else:
        summary = f"Milestone {current_hits}/{len(MILESTONE_STEPS)} • {completed}/{next_step} approved quests"
    return summary, ladder


def remaining_to_text(seconds_left: int) -> str:
    hours = seconds_left // 3600
    minutes = (seconds_left % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


async def safe_send(channel: discord.abc.Messageable | None, content: str = "", embed: discord.Embed | None = None):
    if not channel:
        return
    try:
        await channel.send(content=content, embed=embed)
    except Exception:
        pass


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


async def ensure_participation_ready(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command must be used inside the server.", ephemeral=True)
        return False

    if has_event_role(interaction.user):
        return True

    role_obj = interaction.guild.get_role(EVENT_ROLE_ID) if interaction.guild else None
    role_name = role_obj.mention if role_obj else "the event role"
    embed = discord.Embed(
        title="🐣 Easter Entry Required",
        description=(
            f"You need {role_name} before joining the event.") if role_obj else "You need the event role before joining the event.",
        color=COLOR_PINK,
    )
    embed.add_field(
        name="How to Join",
        value="Use **`/role`** first. After that, you can use **`/claim`**, **`/submit`**, and **`/open`**.",
        inline=False,
    )
    embed.set_footer(text=FOOTER_DEV)

    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    return False


# =========================
# DB HELPERS
# =========================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                envelopes INTEGER NOT NULL DEFAULT 0,
                points INTEGER NOT NULL DEFAULT 0,
                dragon INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        await db.execute(
            """
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
            """
        )

        await db.execute(
            """
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
                created_at INTEGER NOT NULL,
                igg_id TEXT,
                review_note TEXT,
                reviewed_by INTEGER,
                reviewed_at INTEGER
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS claim_history (
                user_id INTEGER PRIMARY KEY,
                last_claim_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS igg_links (
                user_id INTEGER PRIMARY KEY,
                igg_id TEXT NOT NULL UNIQUE,
                linked_at INTEGER NOT NULL,
                linked_by INTEGER
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS submission_blocks (
                user_id INTEGER NOT NULL,
                quest_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                note TEXT,
                set_by INTEGER,
                set_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, quest_id)
            )
            """
        )

        # Safe migrations for existing databases
        migration_statements = [
            "ALTER TABLE quests ADD COLUMN expires_at INTEGER",
            "ALTER TABLE submissions ADD COLUMN igg_id TEXT",
            "ALTER TABLE submissions ADD COLUMN review_note TEXT",
            "ALTER TABLE submissions ADD COLUMN reviewed_by INTEGER",
            "ALTER TABLE submissions ADD COLUMN reviewed_at INTEGER",
        ]
        for stmt in migration_statements:
            try:
                await db.execute(stmt)
            except Exception:
                pass

        # Migrate old daily_claims table to claim_history name if the old table exists.
        try:
            await db.execute(
                """
                INSERT OR IGNORE INTO claim_history(user_id, last_claim_at)
                SELECT user_id, last_claim_at FROM daily_claims
                """
            )
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


async def consume_envelope_and_award(user_id: int, points: int, is_crest: bool) -> bool:
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
        if is_crest:
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


async def adjust_user_field(user_id: int, field: str, delta: int) -> tuple[int, int]:
    if field not in ("envelopes", "points", "dragon"):
        raise ValueError("Invalid field")

    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        async with db.execute(f"SELECT {field} FROM users WHERE user_id = ?", (int(user_id),)) as cur:
            row = await cur.fetchone()
            current = int(row[0]) if row else 0

        new_val = max(0, current + int(delta))
        await db.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (int(new_val), int(user_id)))
        await db.commit()
        return current, new_val


async def try_remove_envelopes(user_id: int, amount: int) -> bool:
    amount = int(amount)
    if amount <= 0:
        return True

    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, user_id)
        async with db.execute("SELECT envelopes FROM users WHERE user_id = ?", (int(user_id),)) as cur:
            row = await cur.fetchone()
            balance = int(row[0]) if row else 0
            if balance < amount:
                return False

        await db.execute(
            "UPDATE users SET envelopes = envelopes - ? WHERE user_id = ?",
            (amount, int(user_id)),
        )
        await db.commit()
        return True


async def get_linked_igg(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT igg_id FROM igg_links WHERE user_id = ?", (int(user_id),)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def lookup_user_by_igg(igg_id: str) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM igg_links WHERE igg_id = ?", (igg_id,)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def link_igg(user_id: int, igg_id: str, linked_by: int | None = None, force: bool = False):
    igg_id = normalize_igg_id(igg_id)
    now = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM igg_links WHERE igg_id = ?", (igg_id,)) as cur:
            row = await cur.fetchone()
            if row and int(row[0]) != int(user_id):
                raise ValueError(f"That IGG ID is already linked to Discord user `{int(row[0])}`.")

        async with db.execute("SELECT igg_id FROM igg_links WHERE user_id = ?", (int(user_id),)) as cur:
            current = await cur.fetchone()

        if current and current[0] == igg_id:
            return igg_id

        if current and not force:
            raise ValueError(f"This Discord user is already linked to IGG ID `{current[0]}`.")

        if force:
            await db.execute("DELETE FROM igg_links WHERE user_id = ?", (int(user_id),))

        await db.execute(
            """
            INSERT INTO igg_links(user_id, igg_id, linked_at, linked_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                igg_id = excluded.igg_id,
                linked_at = excluded.linked_at,
                linked_by = excluded.linked_by
            """,
            (int(user_id), igg_id, now, int(linked_by) if linked_by else None),
        )
        await db.commit()
        return igg_id


async def unlink_igg(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT igg_id FROM igg_links WHERE user_id = ?", (int(user_id),)) as cur:
            row = await cur.fetchone()
            old = row[0] if row else None
        await db.execute("DELETE FROM igg_links WHERE user_id = ?", (int(user_id),))
        await db.commit()
        return old


async def get_submission_block(user_id: int, quest_id: int) -> tuple[bool, str | None]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT active, note FROM submission_blocks WHERE user_id = ? AND quest_id = ?",
            (int(user_id), int(quest_id)),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return False, None
            return bool(int(row[0])), row[1]


async def set_submission_block(user_id: int, quest_id: int, active: bool, note: str | None, set_by: int | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO submission_blocks(user_id, quest_id, active, note, set_by, set_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, quest_id) DO UPDATE SET
                active = excluded.active,
                note = excluded.note,
                set_by = excluded.set_by,
                set_at = excluded.set_at
            """,
            (int(user_id), int(quest_id), 1 if active else 0, note, int(set_by) if set_by else None, int(time.time())),
        )
        await db.commit()


async def create_quest(
    title: str,
    body: str,
    bonus: str | None,
    reward_envelopes: int,
    image_url: str | None,
    message_id: int,
    channel_id: int,
    expires_at: int | None = None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO quests(title, body, bonus, reward_envelopes, image_url, active, message_id, channel_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                title.strip(),
                body.strip(),
                bonus.strip() if bonus else None,
                int(reward_envelopes),
                image_url,
                int(message_id) if message_id else None,
                int(channel_id) if channel_id else None,
                int(time.time()),
                int(expires_at) if expires_at else None,
            ),
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
            return int(row[0])


async def get_quest(quest_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT quest_id, title, body, bonus, reward_envelopes, image_url, active, message_id, channel_id, created_at, expires_at
            FROM quests WHERE quest_id = ?
            """,
            (int(quest_id),),
        ) as cur:
            return await cur.fetchone()


async def list_active_quests(limit: int = 25):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT quest_id, title, reward_envelopes
            FROM quests
            WHERE active = 1
            ORDER BY quest_id DESC
            LIMIT ?
            """,
            (int(limit),),
        ) as cur:
            return await cur.fetchall()


async def close_quest(quest_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE quests SET active = 0 WHERE quest_id = ?", (int(quest_id),))
        await db.commit()
        return True


async def get_expired_active_quests(now_ts: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT quest_id, title, message_id, channel_id, expires_at
            FROM quests
            WHERE active = 1
              AND expires_at IS NOT NULL
              AND expires_at <= ?
            ORDER BY expires_at ASC
            """,
            (int(now_ts),),
        ) as cur:
            return await cur.fetchall()


async def insert_submission(
    user_id: int,
    quest_id: int,
    image_url: str | None,
    text: str | None,
    message_id: int,
    channel_id: int,
    igg_id: str,
) -> int:
    img = (image_url or "").strip()
    txt = text.strip() if text else None

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO submissions(
                user_id, quest_id, proof_url, note, status, reward_envelopes_awarded,
                message_id, channel_id, created_at, igg_id
            )
            VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                int(quest_id),
                img,
                txt,
                int(message_id) if message_id else None,
                int(channel_id) if channel_id else None,
                int(time.time()),
                igg_id,
            ),
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
            return int(row[0])


async def update_submission_message(submission_id: int, message_id: int, channel_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE submissions SET message_id = ?, channel_id = ? WHERE submission_id = ?",
            (int(message_id), int(channel_id), int(submission_id)),
        )
        await db.commit()


async def get_submission(submission_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT submission_id, user_id, quest_id, proof_url, note, status,
                   reward_envelopes_awarded, message_id, channel_id, igg_id,
                   review_note, reviewed_by, reviewed_at
            FROM submissions
            WHERE submission_id = ?
            """,
            (int(submission_id),),
        ) as cur:
            return await cur.fetchone()


async def finalize_submission_review(
    submission_id: int,
    status: str,
    review_note: str | None,
    reviewed_by: int | None,
    reward_envelopes_awarded: int | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE submissions
            SET status = ?,
                review_note = ?,
                reviewed_by = ?,
                reviewed_at = ?,
                reward_envelopes_awarded = COALESCE(?, reward_envelopes_awarded)
            WHERE submission_id = ?
            """,
            (
                status,
                review_note.strip() if review_note else None,
                int(reviewed_by) if reviewed_by else None,
                int(time.time()),
                int(reward_envelopes_awarded) if reward_envelopes_awarded is not None else None,
                int(submission_id),
            ),
        )
        await db.commit()


async def user_has_active_submission_for_quest(user_id: int, quest_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*)
            FROM submissions
            WHERE user_id = ? AND quest_id = ? AND status IN ('PENDING', 'APPROVED')
            """,
            (int(user_id), int(quest_id)),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) > 0


async def count_user_approved(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM submissions WHERE user_id = ? AND status = 'APPROVED'",
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def get_user_claim_state(user_id: int) -> tuple[bool, int]:
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT last_claim_at FROM claim_history WHERE user_id = ?", (int(user_id),)) as cur:
            row = await cur.fetchone()
            last = int(row[0]) if row else 0

        if now - last >= CLAIM_COOLDOWN_SECONDS:
            return True, 0
        return False, int(CLAIM_COOLDOWN_SECONDS - (now - last))


async def set_claim_time(user_id: int):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO claim_history(user_id, last_claim_at)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_claim_at = excluded.last_claim_at
            """,
            (int(user_id), now),
        )
        await db.commit()


async def get_rank_row(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, int(user_id))
        async with db.execute(
            """
            WITH approved AS (
                SELECT user_id, COUNT(*) AS approved_count
                FROM submissions
                WHERE status = 'APPROVED'
                GROUP BY user_id
            ),
            ranked AS (
                SELECT
                    u.user_id,
                    u.points,
                    u.envelopes,
                    u.dragon,
                    COALESCE(a.approved_count, 0) AS approved_count,
                    ROW_NUMBER() OVER (ORDER BY u.points DESC, u.dragon DESC, u.envelopes DESC, u.user_id ASC) AS r,
                    COUNT(*) OVER () AS total
                FROM users u
                LEFT JOIN approved a ON a.user_id = u.user_id
            )
            SELECT user_id, points, envelopes, dragon, approved_count, r, total
            FROM ranked
            WHERE user_id = ?
            """,
            (int(user_id),),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "user_id": int(row[0]),
                "points": int(row[1]),
                "envelopes": int(row[2]),
                "dragon": int(row[3]),
                "approved_count": int(row[4]),
                "rank": int(row[5]),
                "total": int(row[6]),
            }


async def get_rank_context(rank: int, around: int = 2):
    start_r = max(1, int(rank) - int(around))
    end_r = int(rank) + int(around)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            WITH approved AS (
                SELECT user_id, COUNT(*) AS approved_count
                FROM submissions
                WHERE status = 'APPROVED'
                GROUP BY user_id
            ),
            ranked AS (
                SELECT
                    u.user_id,
                    u.points,
                    u.envelopes,
                    u.dragon,
                    COALESCE(a.approved_count, 0) AS approved_count,
                    ROW_NUMBER() OVER (ORDER BY u.points DESC, u.dragon DESC, u.envelopes DESC, u.user_id ASC) AS r
                FROM users u
                LEFT JOIN approved a ON a.user_id = u.user_id
            )
            SELECT r, user_id, points, envelopes, dragon, approved_count
            FROM ranked
            WHERE r BETWEEN ? AND ?
            ORDER BY r ASC
            """,
            (int(start_r), int(end_r)),
        ) as cur:
            return await cur.fetchall()


async def top_leaderboard_page(offset: int, limit: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            WITH approved AS (
                SELECT user_id, COUNT(*) AS approved_count
                FROM submissions
                WHERE status = 'APPROVED'
                GROUP BY user_id
            )
            SELECT u.user_id, u.points, u.envelopes, u.dragon, COALESCE(a.approved_count, 0) AS approved_count
            FROM users u
            LEFT JOIN approved a ON a.user_id = u.user_id
            ORDER BY u.points DESC, u.dragon DESC, u.envelopes DESC, u.user_id ASC
            LIMIT ? OFFSET ?
            """,
            (int(limit), int(offset)),
        ) as cur:
            return await cur.fetchall()


async def count_milestone_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT user_id
                FROM submissions
                WHERE status = 'APPROVED'
                GROUP BY user_id
                HAVING COUNT(*) >= ?
            )
            """,
            (MILESTONE_TARGET,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def get_milestone_page(offset: int, limit: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            WITH approved AS (
                SELECT user_id, COUNT(*) AS approved_count
                FROM submissions
                WHERE status = 'APPROVED'
                GROUP BY user_id
            )
            SELECT u.user_id, COALESCE(a.approved_count, 0) AS approved_count, u.points, u.envelopes, u.dragon
            FROM users u
            JOIN approved a ON a.user_id = u.user_id
            WHERE a.approved_count >= ?
            ORDER BY a.approved_count DESC, u.points DESC, u.dragon DESC, u.envelopes DESC, u.user_id ASC
            LIMIT ? OFFSET ?
            """,
            (MILESTONE_TARGET, int(limit), int(offset)),
        ) as cur:
            return await cur.fetchall()


# =========================
# QUEST / MESSAGE HELPERS
# =========================
def mark_quest_embed_closed(embed: discord.Embed, reason: str):
    existing = [f for f in embed.fields if f.name.strip().lower() != "status"]
    embed.clear_fields()
    for f in existing:
        embed.add_field(name=f.name, value=f.value, inline=f.inline)

    title = (embed.title or "").strip()
    upper = title.upper()
    if "CLOSED" not in upper:
        embed.title = f"🔒 CLOSED • {title}" if title else "🔒 CLOSED"
    elif not title.startswith("🔒"):
        embed.title = f"🔒 {title}"

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

    for guild in bot.guilds:
        ch = guild.get_channel(int(channel_id))
        if not ch:
            continue
        try:
            msg = await ch.fetch_message(int(message_id))
        except Exception:
            continue

        if msg.embeds:
            emb = msg.embeds[0]
        else:
            emb = discord.Embed(title=f"🐣 Quest #{quest_id} — {q_title}", color=COLOR_GRAY)

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
            for (quest_id, title, _message_id, _channel_id, _expires_at) in expired:
                await close_quest(int(quest_id))
                try:
                    await try_edit_quest_message_closed(bot, quest_id=int(quest_id), reason="Auto-closed (time expired).")
                except Exception:
                    pass
                await log_ledger(bot.guilds[0] if bot.guilds else None, f"⏳ AUTO-CLOSED • Quest#{quest_id} • “{title}”")
        except Exception:
            pass
        await asyncio.sleep(60)


# =========================
# REVIEW FLOW
# =========================
class ReviewNoteModal(discord.ui.Modal):
    def __init__(self, submission_id: int, action: str):
        super().__init__(title=f"Review Submission #{submission_id}")
        self.submission_id = int(submission_id)
        self.action = action
        self.note = discord.ui.TextInput(
            label="Staff note",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=1000,
            placeholder="Optional note shown to the user in #submit.",
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        await handle_review_action(
            interaction=interaction,
            submission_id=self.submission_id,
            action=self.action,
            note=(str(self.note.value).strip() or None),
        )


class ReviewView(discord.ui.View):
    def __init__(self, submission_id: int):
        super().__init__(timeout=None)
        self.submission_id = int(submission_id)
        self.approve.custom_id = f"review:approve:{self.submission_id}"
        self.approve_note.custom_id = f"review:approve_note:{self.submission_id}"
        self.reject.custom_id = f"review:reject:{self.submission_id}"
        self.reject_note.custom_id = f"review:reject_note:{self.submission_id}"
        self.hard_reject.custom_id = f"review:hard_reject:{self.submission_id}"

    @discord.ui.button(label="Approve ✅", style=discord.ButtonStyle.success, row=0)
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await handle_review_action(interaction=interaction, submission_id=self.submission_id, action="APPROVE", note=None)

    @discord.ui.button(label="Approve + Note 📝", style=discord.ButtonStyle.secondary, row=0)
    async def approve_note(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.response.send_modal(ReviewNoteModal(self.submission_id, "APPROVE"))

    @discord.ui.button(label="Reject ❌", style=discord.ButtonStyle.danger, row=1)
    async def reject(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await handle_review_action(interaction=interaction, submission_id=self.submission_id, action="REJECT", note=None)

    @discord.ui.button(label="Reject + Note 📝", style=discord.ButtonStyle.secondary, row=1)
    async def reject_note(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.response.send_modal(ReviewNoteModal(self.submission_id, "REJECT"))

    @discord.ui.button(label="Hard Reject ⛔", style=discord.ButtonStyle.danger, row=1)
    async def hard_reject(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.response.send_modal(ReviewNoteModal(self.submission_id, "HARD_REJECT"))


async def disable_review_message(
    guild: discord.Guild | None,
    submission_id: int,
    stored_channel_id: int | None,
    stored_message_id: int | None,
    result_text: str,
):
    message = None

    if guild and stored_channel_id and stored_message_id:
        channel = guild.get_channel(int(stored_channel_id))
        if channel:
            try:
                message = await channel.fetch_message(int(stored_message_id))
            except Exception:
                message = None

    if not message:
        return

    embed = message.embeds[0] if message.embeds else discord.Embed(color=COLOR_PINK)
    existing = [f for f in embed.fields if f.name.strip().lower() != "review result"]
    embed.clear_fields()
    for field in existing:
        embed.add_field(name=field.name, value=field.value, inline=field.inline)
    embed.add_field(name="Review Result", value=result_text, inline=False)
    embed.set_footer(text=FOOTER_DEV)

    disabled_view = ReviewView(submission_id=int(submission_id))
    for item in disabled_view.children:
        item.disabled = True
    await message.edit(embed=embed, view=disabled_view)


async def notify_user_in_submit_channel(guild: discord.Guild | None, user_id: int, text: str):
    if not guild or SUBMISSIONS_CHANNEL_ID == 0:
        return
    submit_ch = guild.get_channel(SUBMISSIONS_CHANNEL_ID)
    await safe_send(submit_ch, content=f"<@{user_id}> {text}")


async def handle_review_action(interaction: discord.Interaction, submission_id: int, action: str, note: str | None):
    if not is_staff(interaction.user):
        if interaction.response.is_done():
            await interaction.followup.send("Staff only.", ephemeral=True)
        else:
            await interaction.response.send_message("Staff only.", ephemeral=True)
        return

    sub = await get_submission(int(submission_id))
    if not sub:
        if interaction.response.is_done():
            await interaction.followup.send("Submission not found.", ephemeral=True)
        else:
            await interaction.response.send_message("Submission not found.", ephemeral=True)
        return

    sid, user_id, quest_id, _proof_url, _user_note, status, awarded, message_id, channel_id, igg_id, _review_note, _reviewed_by, _reviewed_at = sub
    if status != "PENDING":
        if interaction.response.is_done():
            await interaction.followup.send("Already reviewed.", ephemeral=True)
        else:
            await interaction.response.send_message("Already reviewed.", ephemeral=True)
        return

    quest = await get_quest(int(quest_id))
    if not quest:
        if interaction.response.is_done():
            await interaction.followup.send("Quest not found.", ephemeral=True)
        else:
            await interaction.response.send_message("Quest not found.", ephemeral=True)
        return

    _, q_title, _body, _bonus, q_reward, _image, _active, _msg_id, _ch_id, _created_at, _expires_at = quest
    link = msg_link(interaction.guild.id, int(channel_id), int(message_id)) if interaction.guild and channel_id and message_id else "(link unavailable)"

    if action == "APPROVE":
        reward = int(q_reward)
        await add_envelopes(int(user_id), reward)
        await finalize_submission_review(int(submission_id), "APPROVED", note, int(interaction.user.id), reward_envelopes_awarded=reward)
        result_text = f"✅ Approved by {interaction.user.mention} • +{reward} {EGG_EMOJI}"
        if note:
            result_text += f"\n**Note:** {note}"

        await disable_review_message(interaction.guild, int(submission_id), channel_id, message_id, result_text)
        await log_ledger(
            interaction.guild,
            f"✅ APPROVED • Sub#{sid} • Quest#{quest_id} • +{reward}{EGG_EMOJI} → <@{user_id}> • IGG `{igg_id}` • by {interaction.user.mention} • {link}",
        )

        notify_text = (
            f"✅ **Your submission #{sid}** for **Quest #{quest_id} — {q_title}** was **APPROVED**. "
            f"You received **+{reward} {EGG_EMOJI}**."
        )
        if note:
            notify_text += f"\n📝 **Staff note:** {note}"
        await notify_user_in_submit_channel(interaction.guild, int(user_id), notify_text)

    elif action == "REJECT":
        await finalize_submission_review(int(submission_id), "REJECTED", note, int(interaction.user.id), reward_envelopes_awarded=int(awarded or 0))
        result_text = f"❌ Rejected by {interaction.user.mention}"
        if note:
            result_text += f"\n**Note:** {note}"

        await disable_review_message(interaction.guild, int(submission_id), channel_id, message_id, result_text)
        await log_ledger(
            interaction.guild,
            f"❌ REJECTED • Sub#{sid} • Quest#{quest_id} → <@{user_id}> • IGG `{igg_id}` • by {interaction.user.mention} • {link}",
        )

        notify_text = f"❌ **Your submission #{sid}** for **Quest #{quest_id} — {q_title}** was **Rejected**."
        if note:
            notify_text += f"\n📝 **Staff note:** {note}"
        await notify_user_in_submit_channel(interaction.guild, int(user_id), notify_text)

    elif action == "HARD_REJECT":
        await finalize_submission_review(int(submission_id), "HARD_REJECTED", note, int(interaction.user.id), reward_envelopes_awarded=int(awarded or 0))
        await set_submission_block(int(user_id), int(quest_id), True, note, int(interaction.user.id))
        result_text = f"⛔ Hard rejected by {interaction.user.mention} • Resubmission blocked"
        if note:
            result_text += f"\n**Note:** {note}"

        await disable_review_message(interaction.guild, int(submission_id), channel_id, message_id, result_text)
        await log_ledger(
            interaction.guild,
            f"⛔ HARD REJECT • Sub#{sid} • Quest#{quest_id} → <@{user_id}> • IGG `{igg_id}` • by {interaction.user.mention} • {link}",
        )

        notify_text = (
            f"⛔ **Your submission #{sid}** for **Quest #{quest_id} — {q_title}** was **Hard Rejected**. "
            f"You cannot resubmit for this quest unless staff unlock it."
        )
        if note:
            notify_text += f"\n📝 **Staff note:** {note}"
        await notify_user_in_submit_channel(interaction.guild, int(user_id), notify_text)

    else:
        if interaction.response.is_done():
            await interaction.followup.send("Unknown review action.", ephemeral=True)
        else:
            await interaction.response.send_message("Unknown review action.", ephemeral=True)
        return

    if interaction.response.is_done():
        await interaction.followup.send("Review saved.", ephemeral=True)
    else:
        await interaction.response.send_message("Review saved.", ephemeral=True)


# =========================
# PAGED VIEWS
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
        for idx, (user_id, points, envelopes, crests, approved_count) in enumerate(rows):
            rank = start_rank + idx
            lines.append(
                f"**{rank}.** <@{user_id}> — **{points} pts** • ✅ {approved_count} quests • {EGG_EMOJI}{envelopes} • {CREST_EMOJI}{crests}"
            )
        if not lines:
            lines = ["No data yet."]

        embed = discord.Embed(
            title="🏆 Easter Fortune Leaderboard",
            description="\n".join(lines),
            color=COLOR_PINK,
        )
        embed.add_field(name="Page", value=f"{self.page}/{self.max_pages}", inline=True)
        embed.add_field(name="Scope", value=f"Top {self.limit_total}", inline=True)
        embed.add_field(name="Sorting", value="Points ↓, then Golden Crests ↓, then Blessed Eggs ↓.", inline=False)
        embed.set_footer(text=FOOTER_DEV)
        return embed

    @discord.ui.button(label="⬅ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page = max(1, self.page - 1)
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.max_pages
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page = min(self.max_pages, self.page + 1)
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.max_pages
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)


class MilestoneView(discord.ui.View):
    def __init__(self, page: int, per_page: int, max_pages: int, total: int):
        super().__init__(timeout=120)
        self.page = int(page)
        self.per_page = int(per_page)
        self.max_pages = int(max_pages)
        self.total = int(total)
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.max_pages

    async def build_embed(self) -> discord.Embed:
        offset = (self.page - 1) * self.per_page
        rows = await get_milestone_page(offset=offset, limit=self.per_page)
        lines = []
        start_rank = offset + 1
        for idx, (user_id, approved_count, points, envelopes, crests) in enumerate(rows):
            rank = start_rank + idx
            lines.append(
                f"**{rank}.** <@{user_id}> — **{approved_count} approved** • **{points} pts** • {EGG_EMOJI}{envelopes} • {CREST_EMOJI}{crests}"
            )

        embed = discord.Embed(
            title=f"🌸 Milestone Hall — {MILESTONE_TARGET}+ Approved Quests",
            description="\n".join(lines) if lines else "No one has reached the milestone yet.",
            color=COLOR_GREEN,
        )
        embed.add_field(name="Page", value=f"{self.page}/{self.max_pages}", inline=True)
        embed.add_field(name="Qualified", value=str(self.total), inline=True)
        embed.set_footer(text=FOOTER_DEV)
        return embed

    @discord.ui.button(label="⬅ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page = max(1, self.page - 1)
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.max_pages
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page = min(self.max_pages, self.page + 1)
        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= self.max_pages
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)


# =========================
# AUTOCOMPLETE
# =========================
async def quest_id_autocomplete(_interaction: discord.Interaction, current: str):
    rows = await list_active_quests(limit=25)
    choices = []
    for qid, title, reward in rows:
        label = f"#{qid} • +{reward}{EGG_EMOJI} • {title}"
        if current.strip() and current.strip().lower() not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=int(qid)))
    return choices[:25]


# =========================
# BOT CLASS
# =========================
class FortuneBot(commands.Bot):
    async def setup_hook(self):
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
# PLAYER COMMANDS
# =========================
@bot.tree.command(name="submit", description="Submit for a quest (image and/or text).")
@guild_only()
@app_commands.describe(
    quest_id="Quest ID (pick from autocomplete)",
    igg_id="Required the first time. Later optional if already linked.",
    image="Optional: upload an image (screenshot/photo)",
    text="Optional: type text if the quest doesn't need an image",
)
@app_commands.autocomplete(quest_id=quest_id_autocomplete)
async def submit(
    interaction: discord.Interaction,
    quest_id: int,
    igg_id: str | None = None,
    image: discord.Attachment | None = None,
    text: str | None = None,
):
    if interaction.channel_id != SUBMISSIONS_CHANNEL_ID:
        return await interaction.response.send_message("Use this command in the submissions channel.", ephemeral=True)

    if not interaction.guild:
        return await interaction.response.send_message("This command must be used in a server.", ephemeral=True)

    if not await ensure_participation_ready(interaction):
        return

    has_text = bool(text and text.strip())
    has_image = image is not None
    if not has_text and not has_image:
        return await interaction.response.send_message("You must provide **either an image or text** (or both).", ephemeral=True)

    quest = await get_quest(int(quest_id))
    if not quest:
        return await interaction.response.send_message("That quest ID does not exist.", ephemeral=True)

    _, q_title, _body, _bonus, q_reward, _image_url, active, _message_id, _channel_id, _created_at, _expires_at = quest
    if int(active) != 1:
        return await interaction.response.send_message("That quest is closed.", ephemeral=True)

    blocked, block_note = await get_submission_block(interaction.user.id, int(quest_id))
    if blocked:
        msg = "Staff blocked resubmission for this quest."
        if block_note:
            msg += f"\n📝 **Staff note:** {block_note}"
        return await interaction.response.send_message(msg, ephemeral=True)

    if has_image and image.content_type and not image.content_type.startswith("image/"):
        return await interaction.response.send_message("Please upload a valid image file.", ephemeral=True)

    if await user_has_active_submission_for_quest(interaction.user.id, int(quest_id)):
        return await interaction.response.send_message("You already have a pending or approved submission for that quest.", ephemeral=True)

    linked_igg = await get_linked_igg(interaction.user.id)
    try:
        if igg_id:
            linked_igg = await link_igg(interaction.user.id, igg_id, linked_by=interaction.user.id, force=False)
        elif not linked_igg:
            return await interaction.response.send_message(
                "Please provide your **IGG ID** the first time you submit, so I can link it to your Discord account.",
                ephemeral=True,
            )
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(
        title="🐣 Easter Quest Submission (Staff Review)",
        description=(
            f"**Quest:** #{quest_id} — **{q_title}**\n"
            f"**Clasher:** {interaction.user.mention}\n"
            f"**User ID:** `{interaction.user.id}`\n"
            f"**IGG ID:** `{linked_igg}`\n"
            f"**Reward (on approval):** +{int(q_reward)} {EGG_EMOJI}"
        ),
        color=COLOR_PINK,
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
        igg_id=linked_igg,
    )

    view = ReviewView(submission_id=submission_id)
    private_ch = interaction.guild.get_channel(PRIVATE_SUBMISSIONS_CHANNEL_ID)
    if not private_ch:
        await log_ledger(interaction.guild, "⚠️ WARNING: Private submissions channel not found or not accessible.")
        return await interaction.followup.send(
            "⚠️ I couldn't access the staff review channel. Please contact staff/admin to fix permissions.",
            ephemeral=True,
        )

    msg = await private_ch.send(embed=embed, view=view)
    await update_submission_message(submission_id, msg.id, msg.channel.id)
    link = msg_link(interaction.guild.id, msg.channel.id, msg.id)
    await log_ledger(
        interaction.guild,
        f"📮 SUBMITTED • Sub#{submission_id} • Quest#{quest_id} • {interaction.user.mention} • IGG `{linked_igg}` • {link}",
    )
    await interaction.followup.send(f"✅ Submission received! ID **#{submission_id}** (pending review).", ephemeral=True)


@bot.tree.command(name="open", description="Open 1 Blessed Egg and reveal your Easter fortune.")
@guild_only()
async def open_cmd(interaction: discord.Interaction):
    if interaction.channel_id != ENVELOPES_CHANNEL_ID:
        return await interaction.response.send_message("Use this command in the eggs channel.", ephemeral=True)

    if not await ensure_participation_ready(interaction):
        return

    now = time.time()
    last = open_cooldowns.get(interaction.user.id, 0)
    if now - last < OPEN_COOLDOWN_SECONDS:
        wait = int(OPEN_COOLDOWN_SECONDS - (now - last))
        return await interaction.response.send_message(f"⏳ Slow down—try again in {wait}s.", ephemeral=True)
    open_cooldowns[interaction.user.id] = now

    envelopes, points, crests = await get_user_stats(interaction.user.id)
    if envelopes <= 0:
        msg = f"You have no Blessed Eggs {EGG_EMOJI}. Use **`/claim`** every **6 hours** to collect **1-4 {EGG_EMOJI}**, and check the quest channel for more."
        if QUESTS_CHANNEL_ID:
            msg += f"\nQuest board: <#{QUESTS_CHANNEL_ID}>"
        return await interaction.response.send_message(msg, ephemeral=True)

    weights = [t[1] for t in TIERS]
    tier_name, _weight, tier_points = random.choices(TIERS, weights=weights, k=1)[0]
    is_crest = tier_name.startswith("🟡")

    ok = await consume_envelope_and_award(interaction.user.id, tier_points, is_crest)
    if not ok:
        return await interaction.response.send_message(f"You have no Blessed Eggs {EGG_EMOJI}.", ephemeral=True)

    envelopes2, points2, crests2 = await get_user_stats(interaction.user.id)
    key = tier_name.split()[0]
    text = random.choice(FLAVOR.get(key, ["Fortune smiles upon you."]))
    completed = await count_user_approved(interaction.user.id)
    milestone_summary, milestone_ladder = build_milestone_progress(completed)

    embed = discord.Embed(
        title="🎁 Blessed Egg Opened!",
        description=f"**{tier_name}**\n*{text}*",
        color=COLOR_GOLD if is_crest else COLOR_PINK,
    )
    thumb = tier_thumbnail_for_key(key)
    if thumb:
        embed.set_thumbnail(url=thumb)

    embed.add_field(name="Reward", value=f"**+{tier_points} Fortune Points**", inline=False)
    embed.add_field(name="Total Points", value=f"**{points2}**", inline=True)
    embed.add_field(name="Golden Crests", value=f"**{crests2}**", inline=True)
    embed.add_field(name="Remaining Blessed Eggs", value=f"**{envelopes2}**", inline=True)
    embed.add_field(name="Milestone Progress", value=f"{milestone_summary}\n{milestone_ladder}", inline=False)

    if envelopes2 == 0:
        tip = f"You used your last Blessed Egg. Use **`/claim`** every **6 hours** to collect **1-4 {EGG_EMOJI}**."
        if QUESTS_CHANNEL_ID:
            tip += f"\nAlso keep checking <#{QUESTS_CHANNEL_ID}> for quests."
        embed.add_field(name="Out of Eggs?", value=tip, inline=False)

    await log_ledger(
        interaction.guild,
        f"🎁 OPENED • {interaction.user.mention} → {tier_name} (+{tier_points} pts) • eggs now {envelopes2}",
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="claim", description="Claim free Blessed Eggs (6h cooldown, 1-4 eggs).")
@guild_only()
async def claim(interaction: discord.Interaction):
    if not await ensure_participation_ready(interaction):
        return

    can_claim, remaining = await get_user_claim_state(interaction.user.id)
    if not can_claim:
        return await interaction.response.send_message(
            f"⏳ Your claim is not ready yet. Try again in **{remaining_to_text(remaining)}**.",
            ephemeral=True,
        )

    awarded = random.choices([1, 2, 3, 4], weights=[40, 30, 20, 10], k=1)[0]
    await set_claim_time(interaction.user.id)
    await add_envelopes(interaction.user.id, awarded)

    envelopes, points, crests = await get_user_stats(interaction.user.id)
    await log_ledger(interaction.guild, f"🥚 CLAIM • {interaction.user.mention} claimed +{awarded}{EGG_EMOJI}")

    embed = discord.Embed(title="🐣 Easter Claim Received", color=COLOR_GREEN)
    embed.description = f"You claimed **+{awarded} {EGG_EMOJI}**."
    embed.add_field(name="Blessed Eggs", value=str(envelopes), inline=True)
    embed.add_field(name="Fortune Points", value=str(points), inline=True)
    embed.add_field(name="Golden Crests", value=str(crests), inline=True)
    embed.add_field(name="Next Claim", value="In **6 hours**", inline=False)
    embed.set_footer(text=FOOTER_DEV)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="balance", description="Check your Blessed Eggs, points, and milestones.")
@guild_only()
async def balance(interaction: discord.Interaction):
    envelopes, points, crests = await get_user_stats(interaction.user.id)
    completed = await count_user_approved(interaction.user.id)
    milestone_summary, milestone_ladder = build_milestone_progress(completed)

    embed = discord.Embed(title="🐣 Your Easter Fortune", color=COLOR_PINK)
    embed.add_field(name="Blessed Eggs", value=str(envelopes), inline=True)
    embed.add_field(name="Fortune Points", value=str(points), inline=True)
    embed.add_field(name="Golden Crests", value=str(crests), inline=True)
    embed.add_field(name="Approved Quests", value=str(completed), inline=True)
    embed.add_field(name="Milestone Progress", value=f"{milestone_summary}\n{milestone_ladder}", inline=False)
    embed.set_footer(text=FOOTER_DEV)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leaderboard", description="Top Easter Fortune rankings.")
@guild_only()
async def leaderboard(interaction: discord.Interaction):
    total = await count_users()
    if total <= 0:
        return await interaction.response.send_message("No data yet.", ephemeral=True)

    limit_total = min(100, total)
    per_page = 10
    max_pages = max(1, math.ceil(limit_total / per_page))
    view = LeaderboardView(page=1, per_page=per_page, max_pages=max_pages, limit_total=limit_total)
    await interaction.response.send_message(embed=await view.build_embed(), view=view)


@bot.tree.command(name="rank", description="Show exact rank, nearby players, and milestone progress.")
@guild_only()
@app_commands.describe(user="Optional: check someone else's rank")
async def rank(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user
    r = await get_rank_row(int(target.id))
    if not r:
        return await interaction.response.send_message("No rank data yet.", ephemeral=True)

    ctx = await get_rank_context(r["rank"], around=2)
    lines = []
    for (rk, uid, pts, env, crests, approved_count) in ctx:
        marker = "➡️ " if int(uid) == int(target.id) else ""
        lines.append(
            f"{marker}**#{rk}** <@{uid}> — **{pts} pts** • ✅ {approved_count} • {EGG_EMOJI}{env} • {CREST_EMOJI}{crests}"
        )

    milestone_summary, milestone_ladder = build_milestone_progress(r["approved_count"])
    embed = discord.Embed(title="📊 Easter Rank", description="\n".join(lines) if lines else "—", color=COLOR_PINK)
    embed.add_field(name="Player", value=target.mention, inline=False)
    embed.add_field(name="Rank", value=f"**#{r['rank']} / {r['total']}**", inline=True)
    embed.add_field(name="Points", value=f"**{r['points']}**", inline=True)
    embed.add_field(name="Blessed Eggs", value=f"**{r['envelopes']}**", inline=True)
    embed.add_field(name="Golden Crests", value=f"**{r['dragon']}**", inline=True)
    embed.add_field(name="Approved Quests", value=f"**{r['approved_count']}**", inline=True)
    embed.add_field(name="Milestone Progress", value=f"{milestone_summary}\n{milestone_ladder}", inline=False)
    embed.set_footer(text=FOOTER_DEV)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="milestone", description=f"Show everyone who completed {MILESTONE_TARGET}+ approved quests.")
@guild_only()
async def milestone(interaction: discord.Interaction):
    total = await count_milestone_users()
    if total <= 0:
        return await interaction.response.send_message(
            f"No one has reached **{MILESTONE_TARGET}+ approved quests** yet.",
            ephemeral=True,
        )

    per_page = 10
    max_pages = max(1, math.ceil(total / per_page))
    view = MilestoneView(page=1, per_page=per_page, max_pages=max_pages, total=total)
    await interaction.response.send_message(embed=await view.build_embed(), view=view)


@bot.tree.command(name="role", description="Claim the Easter event role and unlock event participation.")
@guild_only()
async def role(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("This command must be used in a server.", ephemeral=True)

    role_obj = interaction.guild.get_role(EVENT_ROLE_ID)
    if not role_obj:
        return await interaction.response.send_message("Role not found. Ask staff to check the role ID.", ephemeral=True)

    if not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Could not resolve your member object. Try again.", ephemeral=True)

    already_has = role_obj in interaction.user.roles
    if not already_has:
        try:
            await interaction.user.add_roles(role_obj, reason="Self-assign via /role")
        except discord.Forbidden:
            return await interaction.response.send_message("⚠️ I don't have permission to give that role.", ephemeral=True)
        except discord.HTTPException:
            return await interaction.response.send_message("⚠️ Discord error while assigning the role. Try again.", ephemeral=True)

    embed = discord.Embed(
        title="🌸 Welcome to the Easter Event",
        description=(
            f"You now hold {role_obj.mention}. The spring path is open to you."
            if not already_has else
            f"You already hold {role_obj.mention}. The spring path is still open to you."
        ),
        color=COLOR_GREEN,
    )
    embed.add_field(
        name="Unlocked",
        value="**`/claim`** for free eggs every 6 hours\n**`/submit`** for quest entries\n**`/open`** to reveal your fortune",
        inline=False,
    )
    embed.add_field(
        name="Onboarding",
        value="Your **IGG ID** is required on your first `/submit`. After that, your linked IGG is reused automatically.",
        inline=False,
    )
    embed.set_footer(text=FOOTER_DEV)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="myigg", description="Check the IGG ID linked to your Discord account.")
@guild_only()
async def myigg(interaction: discord.Interaction):
    linked = await get_linked_igg(interaction.user.id)
    if not linked:
        return await interaction.response.send_message("No IGG ID is linked yet. Add it on your first `/submit`.", ephemeral=True)
    await interaction.response.send_message(f"Your linked IGG ID is **`{linked}`**.", ephemeral=True)


# =========================
# STAFF COMMANDS
# =========================
@bot.tree.command(name="postquest", description="(Staff) Post a quest to the quests channel.")
@guild_only()
@app_commands.describe(
    title="Quest title (short and clear)",
    quest="Quest instructions (full text)",
    reward_envelopes="How many Blessed Eggs this quest grants on approval",
    bonus="Optional bonus text (purely informational)",
    image="Optional image/banner for the quest",
    pin="Pin the quest message",
    duration="Optional: auto-close after this duration",
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
    duration: app_commands.Choice[str] | None = None,
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

    embed = discord.Embed(title=f"🐣 New Easter Quest — {title}", description=quest, color=COLOR_PINK)
    embed.add_field(name="Reward", value=f"**+{reward_envelopes} {EGG_EMOJI}** (on approval)", inline=False)
    if bonus:
        embed.add_field(name="Bonus", value=bonus, inline=False)
    embed.add_field(
        name="📮 How to Submit",
        value=(
            f"Go to <#{SUBMISSIONS_CHANNEL_ID}> and use:\n"
            f"`/submit quest_id:<ID> igg_id:<your id>`\n"
            f"Attach an **image** or add **text** (at least one is required)."
        ),
        inline=False,
    )
    embed.add_field(
        name="Participation Requirement",
        value="Players must claim the event role first with **`/role`**.",
        inline=False,
    )
    if dur_val != "none":
        embed.add_field(name="⏳ Auto-Close", value=f"This quest will auto-close after **{dur_val}**.", inline=False)
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text=FOOTER_DEV)

    await interaction.response.defer(ephemeral=True)
    ping_text = f"<@&{EVENT_ROLE_ID}>"
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
        expires_at=expires_at,
    )

    embed.title = f"🐣 Quest #{quest_id} — {title}"
    embed.add_field(name="Quest ID", value=str(quest_id), inline=True)
    if expires_at:
        embed.add_field(name="Auto-Close", value=dur_val, inline=True)
    await msg.edit(embed=embed)

    link = msg_link(interaction.guild.id, msg.channel.id, msg.id)
    await log_ledger(interaction.guild, f"📌 QUEST POSTED • Quest#{quest_id} • +{reward_envelopes}{EGG_EMOJI} • by {interaction.user.mention} • {link}")
    await interaction.followup.send(f"✅ Posted Quest **#{quest_id}** in {ch.mention}.", ephemeral=True)


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
    await try_edit_quest_message_closed(bot, quest_id=int(quest_id), reason=f"Manually closed by {interaction.user.mention}.")
    await log_ledger(interaction.guild, f"🔒 QUEST CLOSED • Quest#{quest_id} by {interaction.user.mention}")
    await interaction.response.send_message(f"✅ Quest #{quest_id} closed.", ephemeral=True)


@bot.tree.command(name="allowresubmit", description="(Staff) Remove a hard reject block and allow resubmission again.")
@guild_only()
@app_commands.describe(user="Target user", quest_id="Quest ID to unlock")
@app_commands.autocomplete(quest_id=quest_id_autocomplete)
async def allowresubmit(interaction: discord.Interaction, user: discord.Member, quest_id: int):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)

    await set_submission_block(user.id, int(quest_id), False, f"Unlocked by {interaction.user}", interaction.user.id)
    await log_ledger(interaction.guild, f"🔓 RESUBMIT UNLOCKED • Quest#{quest_id} • {user.mention} by {interaction.user.mention}")
    await interaction.response.send_message(f"✅ {user.mention} can submit again for quest **#{quest_id}**.", ephemeral=True)


@bot.tree.command(name="viewigg", description="(Staff) View a member's linked IGG ID.")
@guild_only()
@app_commands.describe(user="Target member")
async def viewigg(interaction: discord.Interaction, user: discord.Member):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    linked = await get_linked_igg(user.id)
    if not linked:
        return await interaction.response.send_message(f"{user.mention} has no linked IGG ID.", ephemeral=True)
    await interaction.response.send_message(f"{user.mention} → **`{linked}`**", ephemeral=True)


@bot.tree.command(name="setigg", description="(Staff) Set or reset a member's IGG ID.")
@guild_only()
@app_commands.describe(user="Target member", igg_id="New IGG ID")
async def setigg(interaction: discord.Interaction, user: discord.Member, igg_id: str):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    try:
        new_igg = await link_igg(user.id, igg_id, linked_by=interaction.user.id, force=True)
    except ValueError as e:
        return await interaction.response.send_message(str(e), ephemeral=True)
    await log_ledger(interaction.guild, f"🆔 IGG SET • {user.mention} → `{new_igg}` by {interaction.user.mention}")
    await interaction.response.send_message(f"✅ Linked {user.mention} to **`{new_igg}`**.", ephemeral=True)


@bot.tree.command(name="unlinkigg", description="(Staff) Remove a member's linked IGG ID.")
@guild_only()
@app_commands.describe(user="Target member")
async def unlinkigg_cmd(interaction: discord.Interaction, user: discord.Member):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    old = await unlink_igg(user.id)
    if not old:
        return await interaction.response.send_message(f"{user.mention} has no linked IGG ID.", ephemeral=True)
    await log_ledger(interaction.guild, f"🆔 IGG UNLINK • {user.mention} from `{old}` by {interaction.user.mention}")
    await interaction.response.send_message(f"✅ Unlinked **`{old}`** from {user.mention}.", ephemeral=True)


@bot.tree.command(name="revoke", description="(Staff) Revoke an approved submission (removes awarded eggs if possible).")
@guild_only()
@app_commands.describe(submission_id="Submission ID number (e.g. 12)")
async def revoke(interaction: discord.Interaction, submission_id: int):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)

    sub = await get_submission(int(submission_id))
    if not sub:
        return await interaction.response.send_message("Submission not found.", ephemeral=True)

    sid, user_id, quest_id, _proof_url, _user_note, status, awarded, message_id, channel_id, igg_id, _review_note, _reviewed_by, _reviewed_at = sub
    if status == "REVOKED":
        return await interaction.response.send_message("This submission is already revoked.", ephemeral=True)
    if status != "APPROVED":
        return await interaction.response.send_message(f"Only APPROVED submissions can be revoked. Current: {status}", ephemeral=True)

    await finalize_submission_review(int(submission_id), "REVOKED", f"Revoked by {interaction.user}", int(interaction.user.id), reward_envelopes_awarded=int(awarded or 0))
    remove_amount = int(awarded or 0)
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

    envelopes, points, crests = await get_user_stats(int(user_id))
    link = msg_link(interaction.guild.id, int(channel_id), int(message_id)) if interaction.guild and channel_id and message_id else "(link unavailable)"

    if removed:
        text_out = (
            f"✅ Revoked submission **#{submission_id}**.\n"
            f"➖ Removed **{remove_amount} Blessed Egg(s)** from <@{user_id}>.\n"
            f"Now: {EGG_EMOJI} **{envelopes}** | ⭐ **{points}** | {CREST_EMOJI} **{crests}**"
        )
        await log_ledger(interaction.guild, f"🧹 REVOKED • Sub#{sid} • -{remove_amount}{EGG_EMOJI} → <@{user_id}> • IGG `{igg_id}` • by {interaction.user.mention} • {link}")
    else:
        text_out = (
            f"✅ Revoked submission **#{submission_id}**.\n"
            f"⚠️ Could NOT remove **{remove_amount} Blessed Egg(s)** (likely already spent).\n"
            f"Please use adjust commands if needed.\n"
            f"Now: {EGG_EMOJI} **{envelopes}** | ⭐ **{points}** | {CREST_EMOJI} **{crests}**"
        )
        await log_ledger(interaction.guild, f"🧹 REVOKED • Sub#{sid} • eggs NOT removed → <@{user_id}> • IGG `{igg_id}` • by {interaction.user.mention} • {link}")

    await interaction.response.send_message(text_out, ephemeral=True)


@bot.tree.command(name="adjustpoints", description="(Staff) Adjust a user's Fortune Points (+/-). Clamped at 0.")
@guild_only()
@app_commands.describe(user="Target user", amount="Use negative to subtract (e.g., -4)")
async def adjustpoints(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    before, after = await adjust_user_field(user.id, "points", amount)
    envelopes, points, crests = await get_user_stats(user.id)
    await log_ledger(interaction.guild, f"🛠️ ADJUST • points {before}->{after} (Δ{amount}) • {user.mention} by {interaction.user.mention}")
    await interaction.response.send_message(
        f"✅ Points updated for {user.mention}: **{before} → {after}**\nNow: {EGG_EMOJI} **{envelopes}** | ⭐ **{points}** | {CREST_EMOJI} **{crests}**",
        ephemeral=True,
    )


@bot.tree.command(name="adjustenvelopes", description="(Staff) Adjust a user's Blessed Eggs (+/-). Clamped at 0.")
@guild_only()
@app_commands.describe(user="Target user", amount="Use negative to subtract (e.g., -1)")
async def adjustenvelopes(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    before, after = await adjust_user_field(user.id, "envelopes", amount)
    envelopes, points, crests = await get_user_stats(user.id)
    await log_ledger(interaction.guild, f"🛠️ ADJUST • eggs {before}->{after} (Δ{amount}) • {user.mention} by {interaction.user.mention}")
    await interaction.response.send_message(
        f"✅ Blessed Eggs updated for {user.mention}: **{before} → {after}**\nNow: {EGG_EMOJI} **{envelopes}** | ⭐ **{points}** | {CREST_EMOJI} **{crests}**",
        ephemeral=True,
    )


@bot.tree.command(name="adjustdragon", description="(Staff) Adjust a user's Golden Crests (+/-). Clamped at 0.")
@guild_only()
@app_commands.describe(user="Target user", amount="Use negative to subtract (e.g., -1)")
async def adjustdragon(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not is_staff(interaction.user):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    before, after = await adjust_user_field(user.id, "dragon", amount)
    envelopes, points, crests = await get_user_stats(user.id)
    await log_ledger(interaction.guild, f"🛠️ ADJUST • crests {before}->{after} (Δ{amount}) • {user.mention} by {interaction.user.mention}")
    await interaction.response.send_message(
        f"✅ Golden Crests updated for {user.mention}: **{before} → {after}**\nNow: {EGG_EMOJI} **{envelopes}** | ⭐ **{points}** | {CREST_EMOJI} **{crests}**",
        ephemeral=True,
    )


@bot.tree.command(name="reset", description="(DANGEROUS) Reset ALL event data.")
@guild_only()
@app_commands.describe(confirm="Type: CONFIRM")
async def reset(interaction: discord.Interaction, confirm: str):
    if int(interaction.user.id) != int(OWNER_USER_ID):
        return await interaction.response.send_message("you are not neo!", ephemeral=True)
    if confirm != "CONFIRM":
        return await interaction.response.send_message("Type **CONFIRM** to reset.", ephemeral=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM submissions")
        await db.execute("DELETE FROM quests")
        await db.execute("DELETE FROM users")
        await db.execute("DELETE FROM claim_history")
        await db.execute("DELETE FROM submission_blocks")
        await db.execute("DELETE FROM igg_links")
        try:
            await db.execute("DELETE FROM daily_claims")
        except Exception:
            pass
        await db.commit()

    await log_ledger(interaction.guild, f"🧨 RESET • Event data wiped by {interaction.user.mention}")
    await interaction.response.send_message("✅ Event data reset complete.", ephemeral=True)


# =========================
# STARTUP
# =========================
@bot.event
async def on_ready():
    await init_db()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT submission_id FROM submissions WHERE status = 'PENDING'") as cur:
            pending = await cur.fetchall()

    for (submission_id,) in pending:
        bot.add_view(ReviewView(submission_id=int(submission_id)))

    if not hasattr(bot, "_auto_close_task"):
        bot._auto_close_task = bot.loop.create_task(auto_close_loop(bot))

    print("Local tree commands:", [c.name for c in bot.tree.get_commands()])
    print(f"Logged in as {bot.user} ✅")


bot.run(BOT_TOKEN)

"""
clash_chaos_modifiers.py
Handles all chaos modifier logic for Zappy Clash brackets.
Plug into your existing clash scheduling / bracket resolution flow.
"""

import random
from datetime import date, timedelta
import hashlib

# ─────────────────────────────────────────────
# MODIFIER REGISTRY
# ─────────────────────────────────────────────

MODIFIERS = {
    "freaky_friday":  "freaky_friday",
    "gravity_flip":   "gravity_flip",
    "equalizer":      "equalizer",
    "oracle_speaks":  "oracle_speaks",  # always active — not mutually exclusive
}

# These two can only fire once per week each, and never on the same bracket
WEEKLY_RANDOM = ["gravity_flip", "equalizer"]

# ─────────────────────────────────────────────
# WEEKLY MODIFIER STATE  (store in your DB)
# ─────────────────────────────────────────────
# Expected Supabase table: clash_weekly_modifiers
# Columns: week_start (date PK), gravity_flip_used (bool), equalizer_used (bool)
#
# You can also track this in a simple dict if you persist it — swap in your DB calls below.

async def get_weekly_modifier_state(db, week_start: date) -> dict:
    """Fetch or create this week's modifier usage record."""
    row = await db.table("clash_weekly_modifiers").select("*").eq("week_start", str(week_start)).maybe_single().execute()
    if row.data:
        return row.data
    # First bracket of the week — initialize
    new_row = {"week_start": str(week_start), "gravity_flip_used": False, "equalizer_used": False}
    await db.table("clash_weekly_modifiers").insert(new_row).execute()
    return new_row


async def mark_modifier_used(db, week_start: date, modifier: str):
    await db.table("clash_weekly_modifiers").update({f"{modifier}_used": True}).eq("week_start", str(week_start)).execute()


def get_week_start() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())  # Monday


# ─────────────────────────────────────────────
# MODIFIER SELECTION
# ─────────────────────────────────────────────

GRAVITY_FLIP_CHANCE   = 0.20   # 20% per bracket, max once/week
EQUALIZER_CHANCE      = 0.15   # 15% per bracket, max once/week
FREAKY_FRIDAY_CHANCE  = 0.40   # 40% — fires independently, any bracket


async def roll_modifiers(db) -> list[str]:
    """
    Roll which modifiers are active for this bracket.
    Returns a list of active modifier keys.
    Call once per bracket, before seeding.
    """
    week_start = get_week_start()
    state = await get_weekly_modifier_state(db, week_start)
    active = []

    # Gravity Flip
    if not state["gravity_flip_used"] and random.random() < GRAVITY_FLIP_CHANCE:
        active.append("gravity_flip")
        await mark_modifier_used(db, week_start, "gravity_flip")

    # Equalizer — can't stack with Gravity Flip (both alter CP math)
    if "gravity_flip" not in active and not state["equalizer_used"] and random.random() < EQUALIZER_CHANCE:
        active.append("equalizer")
        await mark_modifier_used(db, week_start, "equalizer")

    # Freaky Friday — independent roll, can stack with Oracle
    if random.random() < FREAKY_FRIDAY_CHANCE:
        active.append("freaky_friday")

    # Oracle always accompanies any chaos modifier (or can fire solo at lower rate)
    if active or random.random() < 0.25:
        active.append("oracle_speaks")

    return active


# ─────────────────────────────────────────────
# FREAKY FRIDAY
# ─────────────────────────────────────────────

def apply_freaky_friday(participants: list[dict]) -> tuple[list[dict], tuple[str, str]]:
    """
    Swap CP scores between two random participants.
    participants: list of dicts with keys 'user_id', 'zappy_name', 'cp'
    Returns modified list + the names of the swapped Zappies (for reveal later).
    """
    if len(participants) < 2:
        return participants, ("", "")

    a, b = random.sample(range(len(participants)), 2)
    participants[a]["cp"], participants[b]["cp"] = participants[b]["cp"], participants[a]["cp"]

    # Store which two swapped — reveal AFTER results drop
    swapped = (participants[a]["zappy_name"], participants[b]["zappy_name"])
    return participants, swapped


def freaky_friday_reveal(zappy_a: str, zappy_b: str) -> str:
    return (
        f"🔀 **FREAKY FRIDAY REVEAL** — This bracket was cursed.\n"
        f"**{zappy_a}** and **{zappy_b}** secretly swapped CP scores before the first punch was thrown.\n"
        f"The bracket you watched wasn't the bracket you thought."
    )


# ─────────────────────────────────────────────
# GRAVITY FLIP
# ─────────────────────────────────────────────

def apply_gravity_flip(participants: list[dict]) -> list[dict]:
    """
    Invert all CP scores so lowest becomes highest.
    Simple: flipped_cp = max_cp + 1 - original_cp
    Seeding should then sort descending on flipped_cp as usual.
    """
    max_cp = max(p["cp"] for p in participants)
    for p in participants:
        p["cp"] = max_cp + 1 - p["cp"]
    return participants


GRAVITY_FLIP_ANNOUNCE = (
    "🌀 **GRAVITY FLIP** is in effect.\n"
    "The laws of Clash have inverted. Lowest CP rises. Highest CP falls.\n"
    "The bracket runs as normal — but nothing is normal.\n"
    "*Results drop at the usual time.*"
)


# ─────────────────────────────────────────────
# THE EQUALIZER
# ─────────────────────────────────────────────

EQUALIZER_CP = 1000

def apply_equalizer(participants: list[dict]) -> list[dict]:
    """Set everyone's CP to 1000. Seeding becomes pure RNG."""
    for p in participants:
        p["cp"] = EQUALIZER_CP
    return participants


EQUALIZER_ANNOUNCE = (
    "⚖️ **THE EQUALIZER** has been activated.\n"
    "Every Zappy enters this bracket at exactly 1,000 CP.\n"
    "No advantages. No favorites. No mercy.\n"
    "*May the chaos decide.*"
)


# ─────────────────────────────────────────────
# THE ORACLE SPEAKS
# ─────────────────────────────────────────────

ORACLE_PROPHECIES = [
    ("The one who entered quietly will not leave quietly.", "underdog"),
    ("Two will meet in the final who have never faced each other.", "final"),
    ("The strongest seed carries a hidden crack.", "top_seed_falls"),
    ("Before the last bell rings, a name will surprise everyone.", "upset"),
    ("Power means little when fate has already decided.", "chaos"),
    ("Watch the one seeded in shadow — they see something the others don't.", "underdog"),
    ("The bracket is a mirror. What enters, transformed, exits.", "chaos"),
    ("One Zappy will win a match they were never supposed to survive.", "upset"),
    ("The final clash will not be between who you expect.", "final"),
    ("A champion doubts. That doubt will cost them.", "top_seed_falls"),
    ("Something old will outlast something new.", "underdog"),
    ("The numbers lie today. Trust only the result.", "chaos"),
]

def get_oracle_prophecy(modifier_hint: str = None) -> str:
    """
    Pick a prophecy. If a modifier is active, loosely weight toward thematic fits.
    modifier_hint: 'gravity_flip' | 'equalizer' | 'freaky_friday' | None
    """
    hint_map = {
        "gravity_flip":  ["top_seed_falls", "chaos"],
        "equalizer":     ["chaos", "upset"],
        "freaky_friday": ["chaos", "upset"],
    }
    preferred_themes = hint_map.get(modifier_hint, [])

    if preferred_themes:
        preferred = [p for p in ORACLE_PROPHECIES if p[1] in preferred_themes]
        if preferred:
            text, _ = random.choice(preferred)
            return text

    text, _ = random.choice(ORACLE_PROPHECIES)
    return text


def build_oracle_embed(prophecy: str) -> dict:
    """
    Returns a dict you can unpack into a discord.py Embed.
    Call this BEFORE posting bracket seedings.
    """
    return {
        "title": "🔮 The Oracle Speaks",
        "description": f"*\"{prophecy}\"*",
        "color": 0x6A0DAD,
        "footer": {"text": "The bracket drops shortly. Interpret wisely."},
    }


# ─────────────────────────────────────────────
# MASTER APPLY FUNCTION
# ─────────────────────────────────────────────

async def apply_all_modifiers(db, participants: list[dict]) -> dict:
    """
    Roll and apply all active modifiers for this bracket.
    Returns:
        {
            "participants": [...],        # modified list
            "active_modifiers": [...],    # list of active modifier keys
            "announcements": [...],       # strings to post before bracket
            "oracle_embed": dict | None,  # embed dict if oracle fires
            "freaky_friday_swap": (str, str) | None,  # reveal after results
        }
    """
    active = await roll_modifiers(db)
    announcements = []
    oracle_embed = None
    freaky_friday_swap = None

    # Apply CP-altering modifiers (mutually exclusive pair handled in roll_modifiers)
    if "gravity_flip" in active:
        participants = apply_gravity_flip(participants)
        announcements.append(GRAVITY_FLIP_ANNOUNCE)

    if "equalizer" in active:
        participants = apply_equalizer(participants)
        announcements.append(EQUALIZER_ANNOUNCE)

    if "freaky_friday" in active:
        participants, freaky_friday_swap = apply_freaky_friday(participants)
        # No announcement yet — reveal comes AFTER results

    if "oracle_speaks" in active:
        modifier_hint = next((m for m in active if m != "oracle_speaks"), None)
        prophecy = get_oracle_prophecy(modifier_hint)
        oracle_embed = build_oracle_embed(prophecy)

    return {
        "participants": participants,
        "active_modifiers": active,
        "announcements": announcements,
        "oracle_embed": oracle_embed,
        "freaky_friday_swap": freaky_friday_swap,
    }


# ─────────────────────────────────────────────
# USAGE NOTES
# ─────────────────────────────────────────────
#
# In your clash scheduling flow:
#
#   1. Fetch participants from DB as usual
#   2. Call: result = await apply_all_modifiers(db, participants)
#   3. Post result["oracle_embed"] if not None (before seedings)
#   4. Post each string in result["announcements"]
#   5. Seed and run the bracket using result["participants"] (modified CPs)
#   6. After results drop, if result["freaky_friday_swap"]:
#        post freaky_friday_reveal(*result["freaky_friday_swap"])
#
# The freaky_friday_swap reveal is intentionally delayed — post it AFTER
# the results embed lands so people experience the "wait what??" moment.

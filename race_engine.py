"""
race_engine.py
Zappy Grand Prix — core race logic
Handles stat-weighted RNG, lap resolution, narration generation, and result writing.
"""

import random
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional
from supabase import Client


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WAGER_ALGO        = 5
WINNER_PAYOUT     = 9
RAKE_ALGO         = 1   # goes to bot wallet

SURGE_CHANCE      = 0.15  # 15% chance of a surge event on lap 3

# Upgrade cost tiers (ZAP per stat point)
TIER_1_COST = 200   # stat points 1–6
TIER_2_COST = 800   # stat points 7–9
TIER_3_COST = 2000  # stat points 10–11

# Stat initialization ranges
STAT_BASE_MIN = 3
STAT_BASE_MAX = 6
STAT_CAP_MIN  = 8
STAT_CAP_MAX  = 11

# Race beat timing (seconds between Discord message edits)
BEAT_DELAYS = [8, 8, 6, 6]  # start → lap1 → lap2 → final → result


# ---------------------------------------------------------------------------
# Stat seeding
# ---------------------------------------------------------------------------

def seed_stats(zappy_id: str) -> dict:
    """
    Generate randomized base stats and max potentials for a new Zappy.
    Uses zappy_id as part of the seed so results are reproducible
    if called again with the same ID.
    """
    rng = random.Random(zappy_id)

    def rand_stat():
        base = rng.randint(STAT_BASE_MIN, STAT_BASE_MAX)
        cap  = rng.randint(STAT_CAP_MIN, STAT_CAP_MAX)
        return base, cap

    speed_base,     speed_max     = rand_stat()
    endurance_base, endurance_max = rand_stat()
    clutch_base,    clutch_max    = rand_stat()

    return {
        "speed":          speed_base,
        "speed_max":      speed_max,
        "endurance":      endurance_base,
        "endurance_max":  endurance_max,
        "clutch":         clutch_base,
        "clutch_max":     clutch_max,
        "total_zap_spent": 0,
    }


# ---------------------------------------------------------------------------
# Upgrade cost calculator
# ---------------------------------------------------------------------------

def upgrade_cost(current_stat: int, target_stat: int) -> int:
    """
    Calculate total ZAP cost to upgrade a single stat from current to target.
    Tiers: 1–6 = 200 ZAP/pt, 7–9 = 800 ZAP/pt, 10–11 = 2000 ZAP/pt
    """
    if target_stat <= current_stat:
        return 0

    cost = 0
    for point in range(current_stat + 1, target_stat + 1):
        if point <= 6:
            cost += TIER_1_COST
        elif point <= 9:
            cost += TIER_2_COST
        else:
            cost += TIER_3_COST
    return cost


def max_upgrade_cost(stats: dict) -> int:
    """Estimate total ZAP to fully max all three stats from current values."""
    return (
        upgrade_cost(stats["speed"],     stats["speed_max"])
        + upgrade_cost(stats["endurance"], stats["endurance_max"])
        + upgrade_cost(stats["clutch"],    stats["clutch_max"])
    )


# ---------------------------------------------------------------------------
# Race RNG core
# ---------------------------------------------------------------------------

def weighted_roll(stat_a: int, stat_b: int) -> tuple[str, float, float]:
    """
    Roll for a single lap. Higher stat = better odds, not guaranteed win.
    Returns ('a' | 'b', roll_a, roll_b).

    Each racer rolls random * their stat. Highest roll wins.
    This means a stat-8 vs stat-4 Zappy has roughly 2:1 odds per lap —
    meaningful but not deterministic.
    """
    roll_a = random.random() * stat_a
    roll_b = random.random() * stat_b
    winner = "a" if roll_a >= roll_b else "b"
    return winner, round(roll_a, 4), round(roll_b, 4)


def resolve_race(stats_a: dict, stats_b: dict) -> dict:
    """
    Run a full 3-lap race between two Zappies.

    Returns a result dict containing per-lap outcomes, surge flag,
    lap scores, and the final winner ('a' or 'b').
    """
    results = {}

    # Lap 1 — Speed
    lap1_winner, r1a, r1b = weighted_roll(stats_a["speed"], stats_b["speed"])
    results["lap1"] = {"winner": lap1_winner, "roll_a": r1a, "roll_b": r1b}

    # Lap 2 — Endurance
    lap2_winner, r2a, r2b = weighted_roll(stats_a["endurance"], stats_b["endurance"])
    results["lap2"] = {"winner": lap2_winner, "roll_a": r2a, "roll_b": r2b}

    # Surge check (before lap 3) — gives trailing Zappy a stat boost
    score_a = sum(1 for lap in ["lap1", "lap2"] if results[lap]["winner"] == "a")
    score_b = 2 - score_a
    surge_triggered = False
    surge_beneficiary = None

    if random.random() < SURGE_CHANCE:
        surge_triggered = True
        # Surge benefits the trailing Zappy, or random if tied
        if score_a < score_b:
            surge_beneficiary = "a"
        elif score_b < score_a:
            surge_beneficiary = "b"
        else:
            surge_beneficiary = random.choice(["a", "b"])

    # Lap 3 — Clutch (with optional surge boost)
    clutch_a = stats_a["clutch"]
    clutch_b = stats_b["clutch"]

    if surge_triggered:
        boost = random.randint(2, 4)  # surge adds 2–4 phantom clutch points
        if surge_beneficiary == "a":
            clutch_a += boost
        else:
            clutch_b += boost

    lap3_winner, r3a, r3b = weighted_roll(clutch_a, clutch_b)
    results["lap3"] = {"winner": lap3_winner, "roll_a": r3a, "roll_b": r3b}

    # Final score
    score_a = sum(1 for lap in ["lap1", "lap2", "lap3"] if results[lap]["winner"] == "a")
    score_b = 3 - score_a

    # Tiebreaker (shouldn't happen with 3 laps, but safety net)
    if score_a == score_b:
        final_winner = random.choice(["a", "b"])
    else:
        final_winner = "a" if score_a > score_b else "b"

    results["score_a"]          = score_a
    results["score_b"]          = score_b
    results["surge_triggered"]  = surge_triggered
    results["surge_beneficiary"] = surge_beneficiary
    results["winner"]           = final_winner

    return results


# ---------------------------------------------------------------------------
# Narration generator
# ---------------------------------------------------------------------------

LAP1_LEAD_A = [
    "🏎️ **{a} rockets off the line!** Speed advantage early.",
    "⚡ **{a} grabs the early lead!** {b} plays it conservative.",
    "🔥 **{a} out front!** Low, clean, and fast off the start.",
]
LAP1_LEAD_B = [
    "🏎️ **{b} nails the launch!** {a} scrambles to respond.",
    "⚡ **{b} takes the early lead!** Smooth off the line.",
    "🔥 **{b} out front!** {a} already in catch-up mode.",
]
LAP1_EVEN = [
    "🏎️ Dead even off the start. Both Zappies holding their line.",
    "⚡ Side by side out of the gate. Speed stats too close to call.",
]

LAP2_LEAD_A = [
    "💨 **{a} holding strong through the mid-section.** Endurance doing work.",
    "🌀 **{a} extends the gap!** {b} fading in the middle laps.",
    "💪 **{a} digging in.** Endurance stat paying off.",
]
LAP2_LEAD_B = [
    "💨 **{b} surges in the mid-race!** Closing fast on {a}.",
    "🌀 **{b} takes over!** Endurance kicking in late.",
    "💪 **{b} finds another gear.** {a} looks rattled.",
]
LAP2_EVEN = [
    "💨 Still dead even. This one's going to the wire.",
    "🌀 Neither Zappy giving an inch. Mid-race tension is real.",
]

SURGE_LINES = [
    "⚡ **SURGE!** {surge} finds a burst of electric energy — closing the gap dramatically!",
    "🌩️ **SURGE EVENT!** {surge} catches a slip-stream and explodes forward!",
    "💥 **SURGE!** The crowd erupts — {surge} wasn't done yet!",
]

LAP3_TENSION = [
    "😤 **FINAL LAP.** Clutch stat decides it all...",
    "🏁 **ONE LAP LEFT.** Everything on the line...",
    "🎯 **IT COMES DOWN TO THIS.** Who wants it more...",
]

WIN_LINES_A = [
    "🏆 **{a} WINS!** Holds the line under pressure!",
    "🥇 **{a} TAKES IT!** Photo finish — but it's clear!",
    "⚡ **{a} CROSSES FIRST!** The clutch stat delivered!",
]
WIN_LINES_B = [
    "🏆 **{b} WINS!** Came from behind and took it!",
    "🥇 **{b} TAKES IT!** What a comeback!",
    "⚡ **{b} CROSSES FIRST!** Clutch when it counted!",
]


def build_progress_bar(laps_won: int, total_laps: int = 3, width: int = 10) -> str:
    """Build a simple emoji progress bar showing lap wins."""
    filled = round((laps_won / total_laps) * width)
    return "🟩" * filled + "⬜" * (width - filled)


def generate_narration(
    result: dict,
    name_a: str,
    name_b: str,
    zappy_a: str,
    zappy_b: str,
) -> list[dict]:
    """
    Convert a race result into a list of timed narration beats.

    Returns a list of dicts: [{"delay": int, "text": str}, ...]
    Each beat is sent/edited as a Discord message after `delay` seconds.
    """
    beats = []

    def pick(lines: list, **kwargs) -> str:
        return random.choice(lines).format(
            a=f"**{zappy_a}** ({name_a})",
            b=f"**{zappy_b}** ({name_b})",
            **kwargs,
        )

    # --- Beat 0: Race start ---
    l1w = result["lap1"]["winner"]
    if l1w == "a":
        lap1_line = pick(LAP1_LEAD_A)
        bar_a, bar_b = build_progress_bar(1), build_progress_bar(0)
    else:
        lap1_line = pick(LAP1_LEAD_B)
        bar_a, bar_b = build_progress_bar(0), build_progress_bar(1)

    beats.append({
        "delay": 0,
        "text": (
            f"🏁 **RACE START — ZAPPY GRAND PRIX**\n"
            f"*5 ALGO on the line. 30 seconds. Let's go.*\n\n"
            f"{lap1_line}\n\n"
            f"> {zappy_a} ({name_a})  {bar_a}\n"
            f"> {zappy_b} ({name_b})  {bar_b}"
        ),
    })

    # --- Beat 1: Mid race ---
    score_a_mid = 1 if l1w == "a" else 0
    score_b_mid = 1 - score_a_mid
    l2w = result["lap2"]["winner"]

    if l2w == "a":
        score_a_mid += 1
        lap2_line = pick(LAP2_LEAD_A)
    else:
        score_b_mid += 1
        lap2_line = pick(LAP2_LEAD_B)

    bar_a = build_progress_bar(score_a_mid)
    bar_b = build_progress_bar(score_b_mid)

    beats.append({
        "delay": BEAT_DELAYS[0],
        "text": (
            f"🌀 **LAP 2 — MID RACE**\n\n"
            f"{lap2_line}\n\n"
            f"> {zappy_a} ({name_a})  {bar_a}\n"
            f"> {zappy_b} ({name_b})  {bar_b}"
        ),
    })

    # --- Beat 2: Surge or tension ---
    surge_text = ""
    if result["surge_triggered"]:
        surge_name = (
            f"{zappy_a} ({name_a})" if result["surge_beneficiary"] == "a"
            else f"{zappy_b} ({name_b})"
        )
        surge_text = "\n\n" + random.choice(SURGE_LINES).format(surge=f"**{surge_name}**")

    tension_line = random.choice(LAP3_TENSION)

    beats.append({
        "delay": BEAT_DELAYS[1],
        "text": (
            f"😤 **FINAL LAP**{surge_text}\n\n"
            f"{tension_line}"
        ),
    })

    # --- Beat 3: Dramatic pause ---
    beats.append({
        "delay": BEAT_DELAYS[2],
        "text": (
            f"😤 **FINAL LAP**{surge_text}\n\n"
            f"{tension_line}\n\n"
            f"*It's going to be close...*"
        ),
    })

    # --- Beat 4: Winner ---
    winner = result["winner"]
    final_score_a = result["score_a"]
    final_score_b = result["score_b"]

    if winner == "a":
        win_line = pick(WIN_LINES_A)
        winner_display = f"{zappy_a} ({name_a})"
    else:
        win_line = pick(WIN_LINES_B)
        winner_display = f"{zappy_b} ({name_b})"

    bar_a = build_progress_bar(final_score_a)
    bar_b = build_progress_bar(final_score_b)

    beats.append({
        "delay": BEAT_DELAYS[3],
        "text": (
            f"{win_line}\n\n"
            f"> {zappy_a} ({name_a})  {bar_a}  {final_score_a} laps\n"
            f"> {zappy_b} ({name_b})  {bar_b}  {final_score_b} laps\n\n"
            f"🏦 **{winner_display}** receives **9 ALGO**\n"
            f"💰 Bot collects **1 ALGO** rake"
        ),
    })

    return beats


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

async def get_racer(db: Client, discord_user_id: str) -> Optional[dict]:
    res = db.table("zappy_racers").select("*").eq("discord_user_id", discord_user_id).single().execute()
    return res.data if res.data else None


async def get_stats(db: Client, zappy_id: str) -> Optional[dict]:
    res = db.table("zappy_stats").select("*").eq("zappy_id", zappy_id).single().execute()
    return res.data if res.data else None


async def create_duel(db: Client, challenger_id: str, opponent_id: str) -> dict:
    """Insert a new pending duel and return it."""
    res = db.table("race_duels").insert({
        "challenger_id": challenger_id,
        "opponent_id":   opponent_id,
        "status":        "pending",
    }).execute()
    return res.data[0]


async def confirm_payment(db: Client, duel_id: str, player_role: str, txid: str) -> dict:
    """
    Record a payment txid for challenger or opponent.
    If both txids are now set, flip status to 'paid'.
    Returns updated duel row.
    """
    field = "challenger_txid" if player_role == "challenger" else "opponent_txid"
    db.table("race_duels").update({field: txid}).eq("id", duel_id).execute()

    duel = db.table("race_duels").select("*").eq("id", duel_id).single().execute().data
    if duel["challenger_txid"] and duel["opponent_txid"]:
        db.table("race_duels").update({"status": "paid"}).eq("id", duel_id).execute()
        duel["status"] = "paid"
    return duel


async def write_race_result(
    db: Client,
    duel_id: str,
    result: dict,
    winner_id: str,
    payout_txid: str,
) -> None:
    """Write race result rows and update win/loss counters."""

    # race_results row
    db.table("race_results").insert({
        "duel_id":          duel_id,
        "lap1_winner":      winner_id if result["lap1"]["winner"] == "a" else None,
        "lap1_roll_a":      result["lap1"]["roll_a"],
        "lap1_roll_b":      result["lap1"]["roll_b"],
        "lap2_winner":      winner_id if result["lap2"]["winner"] == "a" else None,
        "lap2_roll_a":      result["lap2"]["roll_a"],
        "lap2_roll_b":      result["lap2"]["roll_b"],
        "lap3_winner":      winner_id if result["lap3"]["winner"] == "a" else None,
        "lap3_roll_a":      result["lap3"]["roll_a"],
        "lap3_roll_b":      result["lap3"]["roll_b"],
        "surge_triggered":  result["surge_triggered"],
        "payout_txid":      payout_txid,
    }).execute()

    # Mark duel done
    db.table("race_duels").update({
        "status":    "done",
        "winner_id": winner_id,
    }).eq("id", duel_id).execute()

    # Update bot wallet rake
    db.rpc("increment_rake", {"amount": RAKE_ALGO}).execute()


async def expire_stale_duels(db: Client) -> list[str]:
    """
    Find pending duels past their expiry and mark them expired.
    Returns list of duel IDs that were expired (so bot can trigger refunds).
    """
    now = datetime.now(timezone.utc).isoformat()
    res = (
        db.table("race_duels")
        .select("id, challenger_id, opponent_id, challenger_txid, opponent_txid")
        .eq("status", "pending")
        .lt("expires_at", now)
        .execute()
    )
    expired_ids = []
    for duel in res.data or []:
        db.table("race_duels").update({"status": "expired"}).eq("id", duel["id"]).execute()
        expired_ids.append(duel)
    return expired_ids


# ---------------------------------------------------------------------------
# Upgrade handler
# ---------------------------------------------------------------------------

async def apply_upgrade(
    db: Client,
    discord_user_id: str,
    stat_name: str,
    points: int,
) -> dict:
    """
    Spend ZAP to upgrade a stat. Returns a result dict with
    success bool, cost, new stat value, and remaining ZAP balance.
    """
    racer = await get_racer(db, discord_user_id)
    if not racer:
        return {"success": False, "error": "Not registered. Use /register first."}

    stats = await get_stats(db, racer["zappy_id"])
    if not stats:
        return {"success": False, "error": "Zappy stats not found."}

    if stat_name not in ("speed", "endurance", "clutch"):
        return {"success": False, "error": f"Invalid stat: {stat_name}"}

    current = stats[stat_name]
    cap     = stats[f"{stat_name}_max"]
    target  = current + points

    if target > cap:
        return {
            "success": False,
            "error": (
                f"{stat_name.capitalize()} is already at {current}/{cap}. "
                f"Max potential reached."
            ),
        }

    cost = upgrade_cost(current, target)
    if racer["zap_balance"] < cost:
        return {
            "success": False,
            "error": (
                f"Need {cost:,} ZAP but only have {racer['zap_balance']:,}. "
                f"Keep racing to earn more."
            ),
        }

    # Deduct ZAP and update stat
    new_balance = racer["zap_balance"] - cost
    db.table("zappy_racers").update({"zap_balance": new_balance}).eq("discord_user_id", discord_user_id).execute()
    db.table("zappy_stats").update({
        stat_name:           target,
        "total_zap_spent":   stats["total_zap_spent"] + cost,
    }).eq("zappy_id", racer["zappy_id"]).execute()

    return {
        "success":     True,
        "stat":        stat_name,
        "old_value":   current,
        "new_value":   target,
        "cap":         cap,
        "cost":        cost,
        "new_balance": new_balance,
    }


# ---------------------------------------------------------------------------
# Discord command helper — run the full race with timed narration
# ---------------------------------------------------------------------------

async def run_race_narration(
    message,           # discord.Message to edit in place
    result: dict,
    name_a: str,
    name_b: str,
    zappy_a: str,
    zappy_b: str,
) -> None:
    """
    Edit a Discord message through each narration beat with delays.
    Call this after status flips to 'racing'.
    """
    beats = generate_narration(result, name_a, name_b, zappy_a, zappy_b)

    for i, beat in enumerate(beats):
        if i > 0:
            await asyncio.sleep(beat["delay"])
        await message.edit(content=beat["text"])

    # Hold the final result on screen
    await asyncio.sleep(5)

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
BEAT_DELAYS = [7, 8, 6, 8, 5, 4]  # lights_out → lap1 → lap2 → surge/tension → lap3 → pause → winner


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

# --- Lap 1 (Speed) ---
LAP1_WIN_A = [
    "🚦 LIGHTS OUT — AND {a} LAUNCHES FIRST! Speed advantage off the line!",
    "🚦 GO GO GO — {a} FIRES OUT THE GATE! {b} eating dust already!",
    "🚦 AND THEY'RE OFF — {a} TAKES THE EARLY LEAD! Pure speed on display!",
    "🚦 BOOM! {a} EXPLODES OFF THE LINE! That Speed stat is doing work!",
]
LAP1_WIN_B = [
    "🚦 LIGHTS OUT — {b} JUMPS AHEAD! Caught {a} sleeping at the start!",
    "🚦 AND THEY'RE OFF — {b} OUT FRONT! Clean launch, perfect timing!",
    "🚦 GO GO GO — {b} TAKES IT EARLY! {a} already chasing!",
    "🚦 BOOM! {b} GRABS THE HOLE SHOT! {a} in catch-up mode from lap one!",
]

# --- Lap 2 (Endurance) ---
LAP2_HOLD_LEAD = [
    "💨 LAP 2 — {leader} HOLDS THE LINE! Endurance stat locking in the gap!",
    "💨 LAP 2 — {leader} WON'T CRACK! {trailer} pushing hard but getting nowhere!",
    "💨 LAP 2 — {leader} PULLING AWAY! This race might be over early!",
    "💨 LAP 2 — {leader} IN COMPLETE CONTROL! Endurance built different!",
]
LAP2_FLIP = [
    "💨 LAP 2 — LEAD CHANGE! {leader} TAKES OVER! {trailer} fading fast!",
    "💨 LAP 2 — COMEBACK ALERT! {leader} storms through! This race is ALIVE!",
    "💨 LAP 2 — {leader} SNATCHES THE LEAD! {trailer} ran out of road!",
    "💨 LAP 2 — UNBELIEVABLE! {leader} FLIPS IT! Nobody saw that coming!",
]
LAP2_TIED = [
    "💨 LAP 2 — ONE EACH! IT'S ALL SQUARE! Final lap decides everything!",
    "💨 LAP 2 — KNOTTED UP! {leader} evens it out! This goes to the wire!",
    "💨 LAP 2 — DEAD EVEN! Neither Zappy giving an inch! One lap left!",
]

# --- Surge ---
SURGE_A = [
    "⚡ SURGE! {a} FINDS ANOTHER GEAR! Electric boost on the final lap!",
    "⚡ SURGE EVENT! {a} LIGHTS UP! The crowd goes absolutely wild!",
    "⚡ SURGE! {a} NOT DONE YET! Closing speed out of nowhere!",
]
SURGE_B = [
    "⚡ SURGE! {b} FINDS ANOTHER GEAR! Electric boost on the final lap!",
    "⚡ SURGE EVENT! {b} LIGHTS UP! The crowd goes absolutely wild!",
    "⚡ SURGE! {b} NOT DONE YET! Closing speed out of nowhere!",
]

# --- Lap 3 tension (no surge) ---
LAP3_TENSION = [
    "🏁 FINAL LAP — IT ALL COMES DOWN TO THIS! CLUTCH STAT DECIDES!",
    "🏁 LAST LAP! NO MORE EXCUSES! WHO WANTS IT MORE?!",
    "🏁 FINAL LAP — THE CROWD IS ON ITS FEET! HERE WE GO!",
    "🏁 ONE LAP LEFT! THE PRESSURE IS INSANE! LET'S GO!",
]

# --- Lap 3 result ---
LAP3_WIN_LEADER = [
    "🔥 LAP 3 — {leader} SEALS IT! Clutch when it counted most!",
    "🔥 LAP 3 — {leader} CROSSES FIRST! Couldn't be caught!",
    "🔥 LAP 3 — {leader} HOLDS ON! Pure guts on the final lap!",
]
LAP3_WIN_COMEBACK = [
    "🔥 LAP 3 — {leader} STEALS IT! COMEBACK FOR THE AGES!",
    "🔥 LAP 3 — {leader} FROM BEHIND! NOBODY EXPECTED THAT!",
    "🔥 LAP 3 — UNREAL! {leader} TAKES THE FINAL LAP! WHAT A RACE!",
]
LAP3_WIN_TIEBREAK = [
    "🔥 LAP 3 — {leader} EDGES IT! Closest finish you'll ever see!",
    "🔥 LAP 3 — {leader} BY A NOSE! Photo finish goes their way!",
    "🔥 LAP 3 — {leader} NABS IT! They were inseparable all race!",
]

# --- Winner ---
WIN_LINES = [
    "🏆 {winner} WINS! {winner} WINS! {winner} WINS!",
    "🏆 THAT'S IT! {winner} TAKES THE GRAND PRIX!",
    "🏆 GAME OVER! {winner} IS YOUR WINNER!",
    "🏆 UNBELIEVABLE SCENES! {winner} CROSSES THE LINE!",
]


def build_track(position: int, total: int = 14, marker: str = "🟢") -> str:
    """
    Show a racer's position on a 14-segment track ending with 🏁.
    position: 0 = start, total-1 = finish line
    marker: 🟢 for leader, 🔴 for trailer, 🟡 for tied
    """
    track = ["——"] * total
    pos = min(position, total - 1)
    track[pos] = marker
    return "".join(track) + "🏁"


def get_markers(score_a: int, score_b: int) -> tuple[str, str]:
    """Return (marker_a, marker_b) based on current race positions."""
    if score_a > score_b:
        return "🟢", "🔴"
    elif score_b > score_a:
        return "🔴", "🟢"
    else:
        return "🟡", "🟡"


def score_to_position(score: int, total_laps: int = 3, track_len: int = 14) -> int:
    """Convert laps won to a track position (0-indexed, 0=start, 13=near finish)."""
    # Each lap win moves racer forward. Max position = track_len - 1
    # Winner at 3 laps = position 13, loser at 0 wins = position 3
    base = 3  # everyone starts a little off the line
    return base + round((score / total_laps) * (track_len - base - 1))


def _margin_text(score_a: int, score_b: int, winner: str) -> str:
    """Return 'Won by X lap(s)' or 'Won by a whisker' for a sweep."""
    diff = abs(score_a - score_b)
    if diff == 3:
        return "Dominant — won every lap"
    elif diff == 2:
        return "Won by 2 laps"
    else:
        return "Won by 1 lap"


def generate_narration(
    result: dict,
    name_a: str,
    name_b: str,
    zappy_a: str,
    zappy_b: str,
    mode: str = "algo",
) -> list[dict]:
    beats = []

    def track_lines(sa, sb):
        ma, mb = get_markers(sa, sb)
        pa = score_to_position(sa)
        pb = score_to_position(sb)
        ta = build_track(pa, marker=ma)
        tb = build_track(pb, marker=mb)
        return ta, tb

    label_a = f"**{zappy_a}** ({name_a})"
    label_b = f"**{zappy_b}** ({name_b})"
    wager   = "5 ALGO" if mode == "algo" else "500 ZAPP"

    l1w = result["lap1"]["winner"]
    l2w = result["lap2"]["winner"]
    l3w = result["lap3"]["winner"]
    final_winner = result["winner"]
    s_a, s_b = 0, 0

    # Beat 0 — LIGHTS OUT
    # Both start at position 0, both yellow (tied)
    t0 = build_track(score_to_position(0), marker="🟡")
    beats.append({
        "delay": 0,
        "text": (
            "🎮 **ZAPPY GRAND PRIX**\n"
            f"*{wager} on the line — 3 laps — winner takes all*\n\n"
            f"{zappy_a} ({name_a})\n{t0}\n\n"
            f"{zappy_b} ({name_b})\n{t0}\n\n"
            "*Engines revving... lights on...*\n"
            "🔴 🔴 🔴 🔴 🔴"
        ),
    })

    # Beat 1 — LAP 1 (Speed)
    if l1w == "a":
        s_a += 1
        line = random.choice(LAP1_WIN_A).format(a=label_a, b=label_b)
    else:
        s_b += 1
        line = random.choice(LAP1_WIN_B).format(a=label_a, b=label_b)

    ta, tb = track_lines(s_a, s_b)
    leader_label = label_a if l1w == "a" else label_b

    beats.append({
        "delay": BEAT_DELAYS[0],
        "text": (
            f"{line}\n\n"
            f"{zappy_a} ({name_a})\n{ta}\n\n"
            f"{zappy_b} ({name_b})\n{tb}\n\n"
            f"*Lap 1 done — {leader_label} leads*"
        ),
    })

    # Beat 2 — LAP 2 (Endurance)
    prev_leader = l1w
    if l2w == "a":
        s_a += 1
    else:
        s_b += 1

    ta, tb = track_lines(s_a, s_b)

    if s_a == s_b:
        evener = label_a if l2w == "a" else label_b
        line   = random.choice(LAP2_TIED).format(leader=evener)
        status = "ALL SQUARE — final lap decides!"
    elif l2w != prev_leader:
        new_leader = label_a if l2w == "a" else label_b
        trailer    = label_b if l2w == "a" else label_a
        line   = random.choice(LAP2_FLIP).format(leader=new_leader, trailer=trailer)
        status = f"{zappy_a if l2w == 'a' else zappy_b} now leads"
    else:
        leader  = label_a if l2w == "a" else label_b
        trailer = label_b if l2w == "a" else label_a
        line   = random.choice(LAP2_HOLD_LEAD).format(leader=leader, trailer=trailer)
        status = f"{zappy_a if l2w == 'a' else zappy_b} still leads"

    beats.append({
        "delay": BEAT_DELAYS[1],
        "text": (
            f"{line}\n\n"
            f"{zappy_a} ({name_a})\n{ta}\n\n"
            f"{zappy_b} ({name_b})\n{tb}\n\n"
            f"*Lap 2 done — {status}*"
        ),
    })

    # Beat 3 — Surge or Tension
    if result["surge_triggered"]:
        ben = result["surge_beneficiary"]
        surge_line = random.choice(SURGE_A if ben == "a" else SURGE_B).format(a=label_a, b=label_b)
        beats.append({
            "delay": BEAT_DELAYS[2],
            "text": (
                f"{surge_line}\n\n"
                f"{zappy_a} ({name_a})\n{ta}\n\n"
                f"{zappy_b} ({name_b})\n{tb}\n\n"
                "*Final lap incoming...*"
            ),
        })
    else:
        tension = random.choice(LAP3_TENSION)
        beats.append({
            "delay": BEAT_DELAYS[2],
            "text": (
                f"{tension}\n\n"
                f"{zappy_a} ({name_a})\n{ta}\n\n"
                f"{zappy_b} ({name_b})\n{tb}"
            ),
        })

    # Beat 4 — LAP 3 (Clutch)
    if l3w == "a":
        s_a += 1
    else:
        s_b += 1

    ta, tb = track_lines(s_a, s_b)

    is_comeback_a = (l3w == "a" and s_a > s_b and l2w != "a" and l1w != "a")
    is_comeback_b = (l3w == "b" and s_b > s_a and l2w != "b" and l1w != "b")

    if is_comeback_a:
        line = random.choice(LAP3_WIN_COMEBACK).format(leader=label_a)
    elif is_comeback_b:
        line = random.choice(LAP3_WIN_COMEBACK).format(leader=label_b)
    else:
        clincher = label_a if final_winner == "a" else label_b
        line = random.choice(LAP3_WIN_LEADER).format(leader=clincher)

    beats.append({
        "delay": BEAT_DELAYS[3],
        "text": (
            f"{line}\n\n"
            f"{zappy_a} ({name_a})\n{ta}\n\n"
            f"{zappy_b} ({name_b})\n{tb}"
        ),
    })

    # Beat 5 — Dramatic pause
    beats.append({
        "delay": BEAT_DELAYS[4],
        "text": (
            f"{line}\n\n"
            f"{zappy_a} ({name_a})\n{ta}\n\n"
            f"{zappy_b} ({name_b})\n{tb}\n\n"
            "*Checking the replay...*"
        ),
    })

    # Beat 6 — WINNER
    # Winner track goes to finish line (pos 13 = full green)
    # Loser track shows their actual position (how far back)
    final_score_a = result["score_a"]
    final_score_b = result["score_b"]

    if final_winner == "a":
        winner_zappy, winner_name = zappy_a, name_a
        winner_display = label_a
        bot_label = f"{zappy_b} ({name_b})"
        # Winner at finish line, loser at their earned position
        t_winner = build_track(13, marker="🟢")
        t_loser  = build_track(score_to_position(final_score_b), marker="🔴")
    else:
        winner_zappy, winner_name = zappy_b, name_b
        winner_display = label_b
        bot_label = f"{zappy_a} ({name_a})"
        t_winner = build_track(13, marker="🟢")
        t_loser  = build_track(score_to_position(final_score_a), marker="🔴")

    win_line = random.choice(WIN_LINES).format(winner=winner_display)
    margin   = _margin_text(final_score_a, final_score_b, final_winner)

    if mode == "algo":
        payout_line = f"\U0001f3e6 **{winner_display}** receives **9 ALGO** · Bot rakes **1 ALGO**"
    else:
        payout_line = f"\U0001fa99 **{winner_display}** receives **1,000 ZAPP**"

    beats.append({
        "delay": BEAT_DELAYS[5],
        "text": (
            f"{win_line}\n"
            f"*{margin}*\n\n"
            f"🥇 {winner_zappy} ({winner_name})\n{t_winner}\n\n"
            f"{bot_label}\n{t_loser}\n\n"
            f"{payout_line}"
        ),
    })

    return beats

# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

async def get_racer(db: Client, discord_user_id: str) -> Optional[dict]:
    """Get first registered Zappy for a player. Use get_all_racers for multi-Zappy."""
    res = db.table("zappy_racers").select("*").eq("discord_user_id", discord_user_id).execute()
    return res.data[0] if res.data else None


async def get_all_racers(db: Client, discord_user_id: str) -> list[dict]:
    """Get all Zappies registered to a player."""
    res = db.table("zappy_racers").select("*").eq("discord_user_id", discord_user_id).execute()
    return res.data or []


async def get_stats(db: Client, zappy_id: str) -> Optional[dict]:
    res = db.table("zappy_stats").select("*").eq("zappy_id", zappy_id).execute()
    return res.data[0] if res.data else None


async def get_available_racers(db: Client, discord_user_id: str) -> tuple[list[dict], list[dict]]:
    """
    Returns (available, on_cooldown) lists of (racer, stats) dicts.
    A Zappy is on cooldown for 1hr after its last race.
    """
    from datetime import datetime, timezone, timedelta
    racers = await get_all_racers(db, discord_user_id)
    available = []
    on_cooldown = []

    for racer in racers:
        stats = await get_stats(db, racer["zappy_id"])
        if not stats:
            continue
        last_raced = stats.get("last_raced_at")
        if last_raced:
            # Parse timestamp
            if isinstance(last_raced, str):
                last_raced = datetime.fromisoformat(last_raced.replace("Z", "+00:00"))
            cooldown_ends = last_raced + timedelta(hours=1)
            if datetime.now(timezone.utc) < cooldown_ends:
                on_cooldown.append({"racer": racer, "stats": stats, "cooldown_ends": cooldown_ends})
                continue
        available.append({"racer": racer, "stats": stats})

    return available, on_cooldown


async def set_zappy_cooldown(db: Client, zappy_id: str) -> None:
    """Mark a Zappy as just raced — starts the 1hr cooldown."""
    from datetime import datetime, timezone
    db.table("zappy_stats").update({
        "last_raced_at": datetime.now(timezone.utc).isoformat()
    }).eq("zappy_id", zappy_id).execute()


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

    duel_res = db.table("race_duels").select("*").eq("id", duel_id).execute()
    duel = duel_res.data[0] if duel_res.data else {}
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
    zappy_id: str = None,
) -> dict:
    """
    Spend ZAPP to upgrade a stat on a specific Zappy.
    If zappy_id is None, uses the first registered Zappy.
    ZAP balance is shared across all a player's Zappies (taken from first row).
    """
    all_racers = await get_all_racers(db, discord_user_id)
    if not all_racers:
        return {"success": False, "error": "Not registered. Use /gpregister first."}

    # Find the specific Zappy or default to first
    if zappy_id:
        racer = next((r for r in all_racers if r["zappy_id"] == zappy_id), None)
        if not racer:
            return {"success": False, "error": f"{zappy_id} not found in your garage."}
    else:
        racer = all_racers[0]

    # ZAP balance lives on the first registered Zappy row
    balance_racer = all_racers[0]

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
    if balance_racer["zap_balance"] < cost:
        return {
            "success": False,
            "error": (
                f"Need {cost:,} ZAPP but only have {balance_racer['zap_balance']:,}. "
                f"Keep racing to earn more."
            ),
        }

    # Deduct ZAPP from the balance row (first Zappy)
    new_balance = balance_racer["zap_balance"] - cost
    db.table("zappy_racers").update({"zap_balance": new_balance}).eq("discord_user_id", discord_user_id).eq("zappy_id", balance_racer["zappy_id"]).execute()
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
    message,
    result: dict,
    name_a: str,
    name_b: str,
    zappy_a: str,
    zappy_b: str,
    mode: str = "algo",
) -> None:
    beats = generate_narration(result, name_a, name_b, zappy_a, zappy_b, mode=mode)

    for i, beat in enumerate(beats):
        if i > 0:
            await asyncio.sleep(beat["delay"])
        await message.edit(content=beat["text"])

    # Hold the final result on screen
    await asyncio.sleep(5)

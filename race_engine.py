"""
race_engine_v2.py
Zappy Grand Prix — position-based race engine

Two Zappies race to position 20. Each tick both move based on
their relevant stat (Speed early, Endurance mid, Clutch late).
Bad rolls can push them backwards. First to 20 wins.
No fixed lap count — races end when someone crosses the line.

Average duration: ~20s | 90th percentile: ~30s at 1.5s/beat
"""

import random
import asyncio
from dataclasses import dataclass, field

FINISH_LINE   = 20
BEAT_SECONDS  = 1.5   # seconds between narration messages
SURGE_ZONE    = 14    # position at which clutch surge can trigger
SURGE_CHANCE  = 0.12
SURGE_BONUS   = 3
STUMBLE_EARLY = 0.08  # stumble chance in first 3 positions
STUMBLE_LATE  = 0.18  # stumble chance elsewhere


# ---------------------------------------------------------------------------
# Stat phase
# ---------------------------------------------------------------------------

def _active_stat(pos: int, stats: dict) -> int:
    """Return the stat that governs movement at this track position."""
    if pos < 7:
        return stats.get("speed", 5)
    if pos < 14:
        return stats.get("endurance", 5)
    return stats.get("clutch", 5)


# ---------------------------------------------------------------------------
# Single roll
# ---------------------------------------------------------------------------

def _roll(stat: int, pos: int) -> int:
    """
    Generate a single movement roll.
    Base: 0-2. Stat bonus: +0/+1/+2 based on tier.
    Stumble chance: 8% early, 18% elsewhere → -1 or -2.
    """
    stumble_chance = STUMBLE_EARLY if pos < 3 else STUMBLE_LATE
    if random.random() < stumble_chance:
        return random.randint(-2, -1)
    base  = random.randint(0, 2)
    bonus = (stat - 1) // 4   # 0 for stat 1-4, 1 for 5-8, 2 for 9-11
    return base + bonus


# ---------------------------------------------------------------------------
# Race tick
# ---------------------------------------------------------------------------

@dataclass
class Tick:
    tick:    int
    pos_a:   int
    pos_b:   int
    move_a:  int
    move_b:  int
    surge_a: bool = False
    surge_b: bool = False

    @property
    def gap(self) -> int:
        return self.pos_a - self.pos_b

    @property
    def leader(self) -> str:
        if self.pos_a > self.pos_b: return "a"
        if self.pos_b > self.pos_a: return "b"
        return "tied"


@dataclass
class RaceResult:
    winner:  str          # "a" or "b"
    ticks:   list[Tick]
    pos_a:   int
    pos_b:   int


# ---------------------------------------------------------------------------
# Race simulation
# ---------------------------------------------------------------------------

def simulate_race(stats_a: dict, stats_b: dict) -> RaceResult:
    """
    Run a full race simulation. Returns RaceResult with all ticks.
    stats_a/b: {"speed": int, "endurance": int, "clutch": int}
    """
    pos_a, pos_b = 0, 0
    ticks: list[Tick] = []

    for n in range(1, 61):   # hard cap at 60 ticks
        surge_a = pos_a >= SURGE_ZONE and random.random() < SURGE_CHANCE
        surge_b = pos_b >= SURGE_ZONE and random.random() < SURGE_CHANCE

        move_a  = _roll(_active_stat(pos_a, stats_a), pos_a) + (SURGE_BONUS if surge_a else 0)
        move_b  = _roll(_active_stat(pos_b, stats_b), pos_b) + (SURGE_BONUS if surge_b else 0)

        pos_a = max(0, pos_a + move_a)
        pos_b = max(0, pos_b + move_b)

        ticks.append(Tick(
            tick=n,
            pos_a=min(pos_a, FINISH_LINE),
            pos_b=min(pos_b, FINISH_LINE),
            move_a=move_a,
            move_b=move_b,
            surge_a=surge_a,
            surge_b=surge_b,
        ))

        if pos_a >= FINISH_LINE or pos_b >= FINISH_LINE:
            break

    # Determine winner — if tie on same tick, higher position wins
    last = ticks[-1]
    winner = "a" if last.pos_a >= last.pos_b else "b"

    return RaceResult(winner=winner, ticks=ticks, pos_a=last.pos_a, pos_b=last.pos_b)


# ---------------------------------------------------------------------------
# Track display
# ---------------------------------------------------------------------------

def build_track(pos: int, marker: str = "🟢", total: int = 20) -> str:
    """Render a position-based track. pos is 0-20."""
    track   = ["——"] * total
    display = min(pos, total - 1)
    track[display] = marker
    return "".join(track) + "🏁"


# ---------------------------------------------------------------------------
# Narration
# ---------------------------------------------------------------------------

def _gap_phrase(gap: int, leader: str, name_a: str, name_b: str) -> str:
    leader_name = name_a if leader == "a" else name_b
    trailer_name = name_b if leader == "a" else name_a
    if gap == 0:
        return "**Dead even.** Neither gives an inch."
    absgap = abs(gap)
    if absgap == 1:
        return f"Separated by a single position — **{leader_name}** just ahead."
    if absgap <= 3:
        return f"**{leader_name}** leads by {absgap}. **{trailer_name}** still very much in this."
    if absgap <= 7:
        return f"**{leader_name}** pulling ahead. **{trailer_name}** needs to respond."
    return f"**{leader_name}** is running away with it. **{trailer_name}** in trouble."


def _move_phrase(name: str, move: int, surge: bool) -> str:
    if surge:
        return f"⚡ **{name}** hits a SURGE — blasts forward!"
    if move <= -2:
        return f"**{name}** stumbles badly — loses ground!"
    if move == -1:
        return f"**{name}** slips back."
    if move == 0:
        return f"**{name}** stalls."
    if move == 1:
        return f"**{name}** inches forward."
    if move <= 3:
        return f"**{name}** makes a move."
    return f"**{name}** surges ahead!"


def _finish_phrase(margin: int) -> str:
    if margin == 0:
        return "Photo finish — it could not be closer!"
    if margin <= 2:
        return "Wins by a whisker."
    if margin <= 5:
        return "Crosses with room to spare."
    return "Dominant — never really threatened."


async def narrate_race(
    channel,
    result: RaceResult,
    name_a: str,
    name_b: str,
    payout_str: str,
    mode: str = "algo",
) -> None:
    """
    Narrate the race in a single Discord message that gets edited each tick.
    Track, event callout, and gap commentary all live in one block — no stacking.
    """
    winner_name = name_a if result.winner == "a" else name_b
    loser_name  = name_b if result.winner == "a" else name_a
    margin      = abs(result.pos_a - result.pos_b)
    finish_line = _finish_phrase(margin)
    ticks       = result.ticks

    def render(t: Tick, event_lines: list[str] = None) -> str:
        a_marker = "🟢" if t.pos_a >= t.pos_b else "🔴"
        b_marker = "🟢" if t.pos_b > t.pos_a  else "🔴"
        track_a  = build_track(t.pos_a, a_marker)
        track_b  = build_track(t.pos_b, b_marker)
        gap_line = _gap_phrase(t.gap, t.leader, name_a, name_b)
        events   = "\n".join(event_lines) if event_lines else "🏎  Racing..."
        return (
            f"```\n"
            f"{name_a:<20} {t.pos_a:>2}/20\n"
            f"{track_a}\n\n"
            f"{name_b:<20} {t.pos_b:>2}/20\n"
            f"{track_b}\n"
            f"```"
            f"{events}\n"
            f"*{gap_line}*"
        )

    def render_final(winner_pos: int, loser_pos: int) -> str:
        return (
            f"```\n"
            f"🥇 {winner_name:<19} {FINISH_LINE:>2}/20\n"
            f"{build_track(FINISH_LINE, '🟢')}\n\n"
            f"   {loser_name:<19} {loser_pos:>2}/20\n"
            f"{build_track(loser_pos, '🔴')}\n"
            f"```"
            f"🏆 **{winner_name} WINS!** {finish_line}\n\n"
            f"{payout_str}"
        )

    # Single opening message — all edits happen here
    race_msg = await channel.send(
        f"🚦 **LIGHTS OUT** — **{name_a}** vs **{name_b}**\n"
        f"First to position {FINISH_LINE} wins!\n\n"
        f"*(race starting...)*"
    )
    await asyncio.sleep(BEAT_SECONDS)

    # Initial tick
    await race_msg.edit(content=render(ticks[0]))
    await asyncio.sleep(BEAT_SECONDS)

    # All subsequent ticks — edit in place
    prev_leader = ticks[0].leader
    for t in ticks[1:]:
        events = []

        if t.surge_a:
            events.append(f"⚡ **{name_a}** hits a SURGE — blasts forward!")
        elif t.move_a <= -2:
            events.append(f"💥 **{name_a}** stumbles badly — loses ground!")

        if t.surge_b:
            events.append(f"⚡ **{name_b}** hits a SURGE — blasts forward!")
        elif t.move_b <= -2:
            events.append(f"💥 **{name_b}** stumbles badly — loses ground!")

        if t.leader != prev_leader and t.leader != "tied" and prev_leader is not None:
            leader_name = name_a if t.leader == "a" else name_b
            events.append(f"🔀 **{leader_name}** takes the lead!")

        prev_leader = t.leader
        await race_msg.edit(content=render(t, events if events else None))
        await asyncio.sleep(BEAT_SECONDS)

    # Final edit — show result in same message
    last      = ticks[-1]
    winner_pos = last.pos_a if result.winner == "a" else last.pos_b
    loser_pos  = last.pos_b if result.winner == "a" else last.pos_a
    await race_msg.edit(content=render_final(winner_pos, loser_pos))


# ---------------------------------------------------------------------------
# Stat seeding — generates randomized base stats for a new Zappy
# ---------------------------------------------------------------------------

import random as _random

STAT_BASE_MIN = 2
STAT_BASE_MAX = 3
STAT_CAP_MIN  = 10
STAT_CAP_MAX  = 11

def seed_stats(zappy_id: str) -> dict:
    rng = _random.Random(zappy_id)
    def rand_stat():
        base = rng.randint(STAT_BASE_MIN, STAT_BASE_MAX)
        cap  = rng.randint(STAT_CAP_MIN, STAT_CAP_MAX)
        return base, cap
    speed_base,     speed_max     = rand_stat()
    endurance_base, endurance_max = rand_stat()
    clutch_base,    clutch_max    = rand_stat()
    return {
        "speed":           speed_base,
        "speed_max":       speed_max,
        "endurance":       endurance_base,
        "endurance_max":   endurance_max,
        "clutch":          clutch_base,
        "clutch_max":      clutch_max,
        "total_zap_spent": 0,
    }


# ---------------------------------------------------------------------------
# Supabase stat lookup
# ---------------------------------------------------------------------------

async def get_stats(db, zappy_id: str) -> dict | None:
    res = db.table("zappy_stats").select("*").eq("zappy_id", zappy_id).execute()
    return res.data[0] if res.data else None

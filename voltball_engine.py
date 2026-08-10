"""
voltball_engine.py
-------------------
Resolves a Voltball match between two 7-Zappy teams.

FORMAT
  - Each team fields exactly 7 Zappies, coached by a Hero (Hero = 1-per-team,
    ownership-gated at the Discord/roster layer — not enforced here).
  - Before the match, the coach assigns all 7 Zappies to one of 3 positions
    (Striker / Mid / Guard) under one of 3 FIXED formations:

        OFFENSE:  5 Striker / 1 Mid / 1 Guard
        BALANCED: 3 Striker / 2 Mid / 2 Guard
        DEFENSE:  1 Striker / 2 Mid / 4 Guard

    Formation (and which specific Zappies fill which slot) can change every
    match — it is NOT locked to the roster long-term.

  - Positions drive pooled team stats:
        Striker -> VLT pool (offense)
        Mid     -> SPK pool (playmaking / crit)
        Guard   -> INS pool (defense + turnovers)

  - A match is 4 QUARTERS. Each quarter, mostly-deterministic scoring:

        striker_component = Striker VLT pool * OFFENSE_WEIGHT
        mid_component     = Mid SPK pool     * PLAYMAKING_WEIGHT
        base_offense      = striker_component + mid_component

        defense_reduction = min(Opponent Guard INS pool * DEFENSE_RATE, DEFENSE_CAP)
        turnover_bonus    = Own Guard INS pool * TURNOVER_RATE

        quarter_voltage   = base_offense * (1 - defense_reduction) + turnover_bonus

  - On top of that, each quarter rolls independent LOW-FREQUENCY random events
    per position group (same "rare but flavorful" density as Spark Jobs'
    Lucky Shifts / Easter eggs) that nudge the quarter's score and produce
    broadcast-style flavor lines.

  - COACH SIGNATURE PLAYS: each Hero coach has one signature effect that
    fires independent of formation/position pools — it does NOT scale with
    how many Zappies you put in any particular slot. This is deliberate:
    the original design tied a coach's bonus to boosting whichever position
    the Hero was "flavored" for (e.g. Wolf boosting the Striker pool), but
    that meant the bonus only mattered if you happened to run a formation
    with pieces in that slot, and simulation showed a MATCHED coach+formation
    combo was worth an 83% win rate — bigger than the formation choice itself.
    Signature plays fix both problems: they apply the same regardless of
    formation, and they're sized to a consistent ~7-9 point match-average
    edge (roughly matching the smaller, rebalanced pool-bonus from before)
    rather than compounding with formation choice.

  - Final score = sum of 4 quarters. Higher wins. Ties go to whichever team
    would win the current MATCH DIFFERENTIAL tiebreak (weighted stat roll),
    same "fate decides" spirit as battle_engine's tie handling.

Called by: (future) voltball_cog.py -> weekly match resolution job
"""

import random
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────
# Core formula constants — tuned via grid search
# (see voltball_tune.py) to keep all 3 formations
# within ~1-3% of 50/50 against each other.
# ─────────────────────────────────────────────
OFFENSE_WEIGHT     = 0.17   # Striker VLT pool -> base offense
PLAYMAKING_WEIGHT  = 0.11   # Mid SPK pool -> base offense
DEFENSE_RATE       = 0.0005 # per opponent Guard INS point -> % reduction (0.05%/pt)
DEFENSE_CAP        = 0.25   # defense can never fully zero out an opponent
TURNOVER_RATE       = 0.10  # own Guard INS pool -> bonus Voltage (steal-to-score)
# Retuned against REAL Zappy stat distributions (VLT avg 34.9, INS avg 49.0,
# SPK avg 54.5 across the actual 1585-Zappy collection), not the synthetic
# ~65/52/52-per-stat rosters used in the original tuning pass. That first
# pass produced a formula that looked balanced in testing but was badly
# broken against real data: Defense beat Offense 97% of the time and
# Balanced 89% of the time, because VLT (what Striker/offense runs on)
# is systematically ~20 points lower than INS/SPK across real Zappies —
# a gap the synthetic rosters never had. Re-ran the grid search (see
# voltball_tune.py) against real averages and widened it to include
# OFFENSE_WEIGHT/PLAYMAKING_WEIGHT this time, not just the defense knobs,
# since the root cause was VLT's smaller scale, not the defense formula.

# Applied to a team's quarter voltage when they never submitted a lineup
# and got auto-fielded from their held Zappies in a default formation —
# a real, scoreable match instead of a forfeit, but one they should
# almost always lose. Tuned via sim: applied once per quarter (compounds
# over 4 quarters), 0.90 gives the prepared opponent a ~90% win rate —
# a no-lineup team wins roughly 1 in 10, not more. (0.65 was a 100% loss
# every time — too harsh; 0.95 only got to ~71% — too soft. Per-quarter
# compounding makes this less linear than a single flat number suggests.)
NO_LINEUP_PENALTY = 0.82
NO_LINEUP_DEFAULT_FORMATION = "BALANCED"
# Retuned alongside the formula weights above (was 0.90 against synthetic
# stats, but the real-data formula produces different quarter-voltage
# composition, so the old value only got to ~69% instead of the ~90%
# target). Re-swept against real data: 0.82 lands at ~90%.

QUARTERS = 4

# ─────────────────────────────────────────────
# Fixed formations — the ONLY 3 legal position splits
# ─────────────────────────────────────────────
FORMATIONS = {
    "OFFENSE":  {"Striker": 5, "Mid": 1, "Guard": 1},
    "BALANCED": {"Striker": 3, "Mid": 2, "Guard": 2},
    "DEFENSE":  {"Striker": 1, "Mid": 2, "Guard": 4},
}
ROSTER_SIZE = 7
POSITIONS = ("Striker", "Mid", "Guard")

# ─────────────────────────────────────────────
# Random event odds + magnitudes, rolled once per
# position per quarter. Mirrors Spark Jobs density:
# ~8-12% "good" tier, ~3% "big/rare" tier.
# ─────────────────────────────────────────────
STRIKER_LIGHTNING_CHANCE = 0.10   # bonus to striker_component
STRIKER_LIGHTNING_BONUS  = 0.18
STRIKER_COLDSTREAK_CHANCE = 0.03  # downside, keeps events from being pure upside
STRIKER_COLDSTREAK_PENALTY = 0.15

MID_TRICKPLAY_CHANCE   = 0.09     # doubles mid_component this quarter
MID_MOMENTUM_CHANCE    = 0.03     # small team-wide multiplier, persists rest of match
MID_MOMENTUM_MULT      = 1.10

GUARD_INTERCEPTION_CHANCE = 0.09  # bonus turnover on top of normal turnover_bonus
GUARD_INTERCEPTION_RATE   = 0.05  # extra % of own Guard pool added as bonus points
GUARD_SHUTDOWN_CHANCE     = 0.03  # opponent's defense_reduction against you is voided this quarter


# ─────────────────────────────────────────────
# Coach Signature Plays — formation-agnostic.
# Every effect below is sized to land around a
# ~7-9 point total match-average edge (comparable
# to the earlier ~1.04x pool bonus at its best-case
# match). None of them scale with position pools.
# ─────────────────────────────────────────────
SIGNATURE_CHANCE_STANDARD = 0.25   # baseline per-quarter trigger chance for "flat bonus" style plays
SIGNATURE_FLAT_BONUS      = 8.0    # flat Voltage bonus when a standard signature play fires

HERO_SIGNATURES = {
    # Escalating: grows every quarter the match continues. 0.8*Q -> totals 8.0 over 4 quarters.
    "Wolf":   {"type": "escalating", "label": "Pack Hunt (Coach)",
               "desc": "The pack gets hungrier as the match wears on.", "growth": 0.8},

    # First/last: small early penalty, bigger guaranteed late payoff. Net +8.
    "Frog":   {"type": "first_last", "label": "Patience (Coach)",
               "desc": "Sits back early, cashes in late.", "q1_penalty": -3.0, "q4_bonus": 11.0},

    # Negates opponent's defense reduction against this team when it fires.
    "Bear":   {"type": "shutdown_chance", "label": "Beardown (Coach)",
               "desc": "Bulldozes straight through the opposing defense.", "chance": SIGNATURE_CHANCE_STANDARD},

    # Denies one of the opponent's random events AND capitalizes with a bonus.
    "Crocodile": {"type": "deny_chance", "label": "Death Roll (Coach)",
               "desc": "Locks the opponent out of their big play.", "chance": 0.35, "bonus": 4.0},

    # Guaranteed once per match: floors the team's single WORST quarter back
    # up by a flat amount. Not chance-based — always fires exactly once, on
    # whichever quarter would otherwise be the low point.
    "Cat":    {"type": "floor_lowest", "label": "Nine Lives (Coach)",
               "desc": "Refuses to let the worst quarter sink them.", "bonus": 10.0},

    # Standard flat-bonus chance play.
    "Rabbit": {"type": "flat_chance", "label": "Lucky Foot (Coach)",
               "desc": "A lucky bounce goes their way.", "chance": SIGNATURE_CHANCE_STANDARD, "bonus": SIGNATURE_FLAT_BONUS},

    # Big chance, but only fires in Q1 — 50% * 16 = expected 8.
    "Eagle":  {"type": "q1_burst", "label": "Talon Strike (Coach)",
               "desc": "Strikes early and hard before the opponent settles in.", "chance": 0.50, "bonus": 16.0},

    # Escalating flat REDUCTION applied to the opponent. Same total magnitude as Wolf, mirrored onto the other side.
    "Buck":   {"type": "escalating_debuff", "label": "Antler Clash (Coach)",
               "desc": "Wears the opponent down possession by possession.", "growth": 0.8},

    # Flat drain on the opponent, starting Q2 (poison theme — doesn't hit immediately).
    "Snake":  {"type": "poison_debuff", "label": "Venom Bite (Coach)",
               "desc": "The bite doesn't hurt right away — the poison does.", "per_quarter": 2.67, "start_quarter": 2},

    # Bonus scales with how far behind the opponent's running total is — kicks them while they're down.
    "Shark":  {"type": "frenzy", "label": "Feeding Frenzy (Coach)",
               "desc": "Smells blood in the water when the opponent is trailing.", "rate": 0.55, "cap": 20.0},

    # Chaos: standard flat-bonus play, with cosmetic "random stat" flavor text.
    # (Originally scaled to whichever pool component was smallest, but that
    # reintroduced formation-dependence — Balanced/Defense's bigger Guard
    # pools made it hit much harder than on Offense. Flat like the others now.)
    "ShittyKitties": {"type": "flat_chance", "label": "Chaos Mode (Coach)",
               "desc": "Even the bot doesn't know which stat is about to triple.",
               "chance": SIGNATURE_CHANCE_STANDARD, "bonus": SIGNATURE_FLAT_BONUS},
}


@dataclass
class ZappyPlayer:
    """One Zappy on a Voltball roster."""
    asset_id: int
    name:     str
    VLT:      int
    INS:      int
    SPK:      int


@dataclass
class Team:
    """A Voltball team: 7 Zappies, a Hero coach, and this match's formation."""
    name:            str
    coach_hero_type: Optional[str]
    formation:       str                          # one of FORMATIONS keys
    assignments:     dict                          # {"Striker": [ZappyPlayer,...], "Mid": [...], "Guard": [...]}

    # Match-state (not set at construction)
    momentum_multiplier: float = field(default=1.0, init=False)
    nine_lives_used:      bool = field(default=False, init=False)
    chaos_used:            bool = field(default=False, init=False)
    auto_lineup_penalty:   bool = field(default=False, init=False)  # True if this team was auto-fielded from holdings, no lineup submitted

    def __post_init__(self):
        if self.formation not in FORMATIONS:
            raise ValueError(f"Unknown formation: {self.formation}")
        expected = FORMATIONS[self.formation]
        total = sum(len(v) for v in self.assignments.values())
        if total != ROSTER_SIZE:
            raise ValueError(f"Team {self.name} must field exactly {ROSTER_SIZE} Zappies, got {total}")
        for pos in POSITIONS:
            got = len(self.assignments.get(pos, []))
            want = expected[pos]
            if got != want:
                raise ValueError(
                    f"Team {self.name} formation {self.formation} requires "
                    f"{want} {pos}(s), got {got}"
                )

    def pool(self, position: str, stat: str) -> int:
        """Sum of a stat across all Zappies assigned to a position."""
        return sum(getattr(z, stat) for z in self.assignments.get(position, []))

    @property
    def signature(self) -> Optional[dict]:
        return HERO_SIGNATURES.get(self.coach_hero_type)


def build_team(name: str, coach_hero_type: Optional[str], formation: str,
                roster: list[ZappyPlayer], position_map: dict) -> Team:
    """
    Convenience builder.
    position_map: {asset_id: "Striker"|"Mid"|"Guard"} for all 7 roster Zappies.
    """
    by_id = {z.asset_id: z for z in roster}
    assignments = {"Striker": [], "Mid": [], "Guard": []}
    for asset_id, pos in position_map.items():
        if pos not in POSITIONS:
            raise ValueError(f"Invalid position '{pos}' for asset {asset_id}")
        assignments[pos].append(by_id[asset_id])
    return Team(name=name, coach_hero_type=coach_hero_type, formation=formation, assignments=assignments)


def _base_quarter(quarter_num: int, offense_team: "Team", defense_team: "Team",
                   events_denied: bool = False) -> dict:
    """
    Computes one team's base Quarter Voltage against one opponent — formation
    math + random events only. Coach signature plays are layered on afterward
    in resolve_match, since several of them need cross-team information.
    """
    log = []

    striker_pool = offense_team.pool("Striker", "VLT")
    mid_pool     = offense_team.pool("Mid", "SPK")
    own_guard_pool = offense_team.pool("Guard", "INS")
    opp_guard_pool = defense_team.pool("Guard", "INS")

    striker_component = striker_pool * OFFENSE_WEIGHT
    mid_component     = mid_pool * PLAYMAKING_WEIGHT

    if events_denied:
        log.append(f"  🐊 **DEATH ROLL** — {defense_team.name}'s coach locks {offense_team.name} out of their big play this quarter!")
    else:
        # ── Striker events ──
        if random.random() < STRIKER_LIGHTNING_CHANCE:
            striker_component *= (1 + STRIKER_LIGHTNING_BONUS)
            log.append(f"  ⚡ **LIGHTNING STRIKE** — {offense_team.name}'s Striker lane catches fire!")
        elif random.random() < STRIKER_COLDSTREAK_CHANCE:
            striker_component *= (1 - STRIKER_COLDSTREAK_PENALTY)
            log.append(f"  ❄️ **COLD STREAK** — {offense_team.name}'s Strikers go quiet this quarter.")

        # ── Mid events ──
        if random.random() < MID_TRICKPLAY_CHANCE:
            mid_component *= 2.0
            log.append(f"  🎭 **TRICK PLAY** — {offense_team.name}'s Mids double up their playmaking!")
        elif random.random() < MID_MOMENTUM_CHANCE:
            offense_team.momentum_multiplier *= MID_MOMENTUM_MULT
            log.append(f"  📈 **MOMENTUM SWING** — {offense_team.name} carries a hot hand into the rest of the match!")

    base_offense = striker_component + mid_component

    # ── Guard events ── (not subject to events_denied — that only covers the offense_team's own attacking events above)
    shutdown = (not events_denied) and random.random() < GUARD_SHUTDOWN_CHANCE
    defense_reduction = 0.0 if shutdown else min(opp_guard_pool * DEFENSE_RATE, DEFENSE_CAP)
    if shutdown:
        log.append(f"  🛑 **SHUTDOWN** — {offense_team.name} plays right through {defense_team.name}'s defense!")

    turnover_bonus = own_guard_pool * TURNOVER_RATE
    if not events_denied and random.random() < GUARD_INTERCEPTION_CHANCE:
        extra = own_guard_pool * GUARD_INTERCEPTION_RATE
        turnover_bonus += extra
        log.append(f"  🥊 **INTERCEPTION** — {offense_team.name}'s Guards force a turnover and cash it in!")

    quarter_voltage = (base_offense * (1 - defense_reduction) + turnover_bonus) * offense_team.momentum_multiplier

    if offense_team.auto_lineup_penalty:
        quarter_voltage *= NO_LINEUP_PENALTY
        log.append(f"  📋 {offense_team.name} never submitted a game plan this week — playing disorganized.")

    return {
        "voltage": quarter_voltage,
        "log": log,
        "striker_component": striker_component,
        "mid_component": mid_component,
        "turnover_bonus": turnover_bonus,
    }


def _apply_signature(quarter_num: int, team: "Team", opponent: "Team",
                      base_result: dict, opp_base_result: dict,
                      own_running_total: float, opp_running_total: float) -> tuple[float, list]:
    """
    Applies a team's coach signature play for this quarter. Returns
    (voltage_delta, log_lines). Only affects `team`'s own score EXCEPT
    for escalating_debuff/poison_debuff, which are applied to the
    opponent's score by the caller using the returned delta's sign
    convention (negative delta targets self is never used here — those
    two types return a delta meant to be subtracted from the OPPONENT,
    flagged via the returned "target" field).
    """
    sig = team.signature
    if not sig:
        return 0.0, [], None

    label = sig["label"]
    sig_type = sig["type"]
    log = []

    if sig_type == "escalating":
        delta = sig["growth"] * quarter_num
        log.append(f"  🦸 **{label}** — {team.name} grows stronger as the match wears on (+{round(delta,1)}).")
        return delta, log, "self"

    if sig_type == "first_last":
        if quarter_num == 1:
            delta = sig["q1_penalty"]
            log.append(f"  🦸 **{label}** — {team.name} plays it patient early ({round(delta,1)}).")
            return delta, log, "self"
        if quarter_num == QUARTERS:
            delta = sig["q4_bonus"]
            log.append(f"  🦸 **{label}** — {team.name}'s patience pays off big in the final quarter (+{round(delta,1)}).")
            return delta, log, "self"
        return 0.0, [], None

    if sig_type == "shutdown_chance":
        if random.random() < sig["chance"]:
            # Recompute what this quarter's voltage would be with defense_reduction fully voided.
            # Cheap approximation: refund the amount the reduction cost this team.
            base_offense = base_result["striker_component"] + base_result["mid_component"]
            opp_guard_pool = opponent.pool("Guard", "INS")
            reduction = min(opp_guard_pool * DEFENSE_RATE, DEFENSE_CAP)
            refund = base_offense * reduction
            if refund > 0:
                log.append(f"  🦸 **{label}** — {team.name} bulldozes straight through the defense (+{round(refund,1)}).")
                return refund, log, "self"
        return 0.0, [], None

    if sig_type == "deny_chance":
        # Bonus-on-deny is applied directly in resolve_match (it already knows
        # whether this team's deny roll landed) — nothing to do here.
        return 0.0, [], None

    if sig_type == "floor_lowest":
        # Handled as a post-match adjustment in resolve_match, not per-quarter.
        return 0.0, [], None

    if sig_type == "flat_chance":
        if random.random() < sig["chance"]:
            log.append(f"  🦸 **{label}** — a lucky bounce goes {team.name}'s way (+{sig['bonus']}).")
            return sig["bonus"], log, "self"
        return 0.0, [], None

    if sig_type == "q1_burst":
        if quarter_num == 1 and random.random() < sig["chance"]:
            log.append(f"  🦸 **{label}** — {team.name} strikes early and hard (+{sig['bonus']}).")
            return sig["bonus"], log, "self"
        return 0.0, [], None

    if sig_type == "escalating_debuff":
        delta = sig["growth"] * quarter_num
        log.append(f"  🦸 **{label}** — {team.name} wears {opponent.name} down (-{round(delta,1)} to {opponent.name}).")
        return delta, log, "opponent"

    if sig_type == "poison_debuff":
        if quarter_num >= sig["start_quarter"]:
            delta = sig["per_quarter"]
            log.append(f"  🦸 **{label}** — the poison sets in on {opponent.name} (-{round(delta,1)}).")
            return delta, log, "opponent"
        return 0.0, [], None

    if sig_type == "frenzy":
        deficit = own_running_total - opp_running_total
        if deficit > 0:
            delta = min(sig["cap"], deficit * sig["rate"])
            if delta > 0.1:
                log.append(f"  🦸 **{label}** — {team.name} smells blood in the water (+{round(delta,1)}).")
                return delta, log, "self"
        return 0.0, [], None

    return 0.0, [], None


def resolve_match(team_a: Team, team_b: Team) -> dict:
    """Resolves a full 4-quarter Voltball match between two teams."""
    log = [f"🏟️ **VOLTBALL MATCH** — {team_a.name} ({team_a.formation}) vs. {team_b.name} ({team_b.formation})", ""]

    score_a, score_b = 0.0, 0.0
    quarter_breakdown = []

    for q in range(1, QUARTERS + 1):
        log.append(f"**— Quarter {q} —**")

        # Death Roll denial rolls happen before the opponent's base quarter is computed.
        a_denies_b = team_a.signature and team_a.signature["type"] == "deny_chance" and random.random() < team_a.signature["chance"]
        b_denies_a = team_b.signature and team_b.signature["type"] == "deny_chance" and random.random() < team_b.signature["chance"]

        result_a = _base_quarter(q, team_a, team_b, events_denied=b_denies_a)
        result_b = _base_quarter(q, team_b, team_a, events_denied=a_denies_b)

        sig_delta_a, sig_log_a, target_a = _apply_signature(q, team_a, team_b, result_a, result_b, score_a, score_b)
        sig_delta_b, sig_log_b, target_b = _apply_signature(q, team_b, team_a, result_b, result_a, score_b, score_a)

        voltage_a = result_a["voltage"] + (sig_delta_a if target_a == "self" else 0)
        voltage_b = result_b["voltage"] + (sig_delta_b if target_b == "self" else 0)

        if target_a == "opponent":
            voltage_b = max(0.0, voltage_b - sig_delta_a)
        if target_b == "opponent":
            voltage_a = max(0.0, voltage_a - sig_delta_b)

        # Death Roll's capitalize-on-lockdown bonus, applied directly since it
        # depends on the deny roll made earlier this quarter, not on _apply_signature.
        if a_denies_b and team_a.signature and team_a.signature["type"] == "deny_chance":
            bonus = team_a.signature["bonus"]
            voltage_a += bonus
            log.append(f"  🦸 **{team_a.signature['label']}** — {team_a.name} capitalizes on the lockdown (+{bonus}).")
        if b_denies_a and team_b.signature and team_b.signature["type"] == "deny_chance":
            bonus = team_b.signature["bonus"]
            voltage_b += bonus
            log.append(f"  🦸 **{team_b.signature['label']}** — {team_b.name} capitalizes on the lockdown (+{bonus}).")

        voltage_a, voltage_b = round(voltage_a, 1), round(voltage_b, 1)
        score_a += voltage_a
        score_b += voltage_b

        log.extend(result_a["log"])
        log.extend(sig_log_a)
        log.extend(result_b["log"])
        log.extend(sig_log_b)
        log.append(f"  Quarter {q} Voltage — **{team_a.name}** {voltage_a} · **{team_b.name}** {voltage_b}")
        log.append(f"  Running total — **{team_a.name}** {round(score_a,1)} · **{team_b.name}** {round(score_b,1)}")
        log.append("")

        quarter_breakdown.append({"quarter": q, "team_a_voltage": voltage_a, "team_b_voltage": voltage_b})

    # ── Post-match: Nine Lives (Cat) floors the team's single worst quarter ──
    for team, quarters_key, score_ref in [(team_a, "team_a_voltage", "score_a"), (team_b, "team_b_voltage", "score_b")]:
        if team.signature and team.signature["type"] == "floor_lowest":
            worst_idx = min(range(len(quarter_breakdown)), key=lambda i: quarter_breakdown[i][quarters_key])
            bonus = team.signature["bonus"]
            quarter_breakdown[worst_idx][quarters_key] = round(quarter_breakdown[worst_idx][quarters_key] + bonus, 1)
            if score_ref == "score_a":
                score_a = round(score_a + bonus, 1)
            else:
                score_b = round(score_b + bonus, 1)
            log.append(f"  🦸 **{team.signature['label']}** — {team.name} refuses to let Quarter {worst_idx+1} sink them (+{bonus}).")

    score_a, score_b = round(score_a, 1), round(score_b, 1)

    # ── Winner (tie -> weighted fate roll, same spirit as battle_engine) ──
    if score_a > score_b:
        winner, loser = team_a, team_b
    elif score_b > score_a:
        winner, loser = team_b, team_a
    else:
        total_a = sum(team_a.pool(p, s) for p, s in
                      [("Striker", "VLT"), ("Mid", "SPK"), ("Guard", "INS")])
        total_b = sum(team_b.pool(p, s) for p, s in
                      [("Striker", "VLT"), ("Mid", "SPK"), ("Guard", "INS")])
        roll_a = random.randint(1, max(1, total_a))
        roll_b = random.randint(1, max(1, total_b))
        log.append(f"⚡ **TIE — Fate decides!** {team_a.name} rolls {roll_a}, {team_b.name} rolls {roll_b}.")
        winner, loser = (team_a, team_b) if roll_a >= roll_b else (team_b, team_a)

    log.append(f"🏆 **FINAL: {team_a.name} {score_a} — {score_b} {team_b.name}**")
    log.append(f"🏆 **{winner.name} wins!**")

    return {
        "team_a": team_a.name,
        "team_b": team_b.name,
        "score_a": score_a,
        "score_b": score_b,
        "winner": winner.name,
        "loser": loser.name,
        "quarters": quarter_breakdown,
        "log": log,
        "log_text": "\n".join(log),
    }


# ─────────────────────────────────────────────
# Quick test
# ─────────────────────────────────────────────
if __name__ == "__main__":
    def make_roster(prefix: str, n: int, vlt: int, ins: int, spk: int) -> list[ZappyPlayer]:
        return [ZappyPlayer(asset_id=i, name=f"{prefix}{i}", VLT=vlt, INS=ins, SPK=spk) for i in range(n)]

    # Team A: Balanced formation, Wolf coach
    roster_a = make_roster("A", 7, vlt=65, ins=52, spk=52)
    pos_map_a = {0: "Striker", 1: "Striker", 2: "Striker",
                 3: "Mid", 4: "Mid",
                 5: "Guard", 6: "Guard"}
    team_a = build_team("Thunder Coasts", "Wolf", "BALANCED", roster_a, pos_map_a)

    # Team B: Defense formation, Frog coach — deliberately mismatched vs Wolf's
    # Striker-flavored theme to prove the signature play still applies.
    roster_b = make_roster("B", 7, vlt=68, ins=45, spk=50)
    pos_map_b = {0: "Striker",
                 1: "Mid", 2: "Mid",
                 3: "Guard", 4: "Guard", 5: "Guard", 6: "Guard"}
    team_b = build_team("Volt Runners", "Frog", "DEFENSE", roster_b, pos_map_b)

    result = resolve_match(team_a, team_b)
    print(result["log_text"])

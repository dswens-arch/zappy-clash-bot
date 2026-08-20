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

  - Positions drive pooled team stats — each position has a PRIMARY stat
    (70% weight share) and a SECONDARY stat (30% weight share), rotating
    VLT -> SPK -> INS -> VLT so every stat matters somewhere as a primary
    and somewhere else as a secondary assist:
        Striker -> VLT pool (primary, offense) + SPK pool (secondary)
        Mid     -> SPK pool (primary, playmaking/crit) + INS pool (secondary)
        Guard   -> INS pool (primary, defense + turnovers) + VLT pool (secondary)
    Secondary weights are MEAN-NORMALIZED against the real collection's
    actual VLT/INS/SPK averages (verified: mean VLT 34.95, INS 49.03,
    SPK 54.61 -- a real ~20pt skew), not a flat 70/30 of the old weight
    number. A flat split was checked and would have silently inflated
    Striker output ~17% because SPK's mean is so much higher than VLT's.

  - A match is 4 QUARTERS. Each quarter, mostly-deterministic scoring:

        striker_component = Striker VLT pool * OFFENSE_WEIGHT_PRIMARY
                             + Striker SPK pool * OFFENSE_WEIGHT_SECONDARY
        mid_component     = Mid SPK pool * PLAYMAKING_WEIGHT_PRIMARY
                             + Mid INS pool * PLAYMAKING_WEIGHT_SECONDARY
        base_offense      = striker_component + mid_component

        defense_reduction = min(Opponent Guard INS pool * DEFENSE_RATE, DEFENSE_CAP)
        turnover_bonus    = Own Guard INS pool * TURNOVER_RATE_PRIMARY
                             + Own Guard VLT pool * TURNOVER_RATE_SECONDARY

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
OFFENSE_WEIGHT_PRIMARY    = 0.119   # Striker VLT pool -> base offense (primary, 70% value share)
OFFENSE_WEIGHT_SECONDARY  = 0.0326  # Striker SPK pool -> base offense (secondary, 30% value share, mean-normalized vs real collection VLT/SPK averages)
PLAYMAKING_WEIGHT_PRIMARY   = 0.077   # Mid SPK pool -> base offense (primary, 70% value share)
PLAYMAKING_WEIGHT_SECONDARY = 0.0368  # Mid INS pool -> base offense (secondary, 30% value share, mean-normalized)
DEFENSE_RATE       = 0.0005 # per opponent Guard INS point -> % reduction (0.05%/pt) -- unchanged, cross-team mechanic, not part of this change
DEFENSE_CAP        = 0.25   # defense can never fully zero out an opponent -- unchanged
TURNOVER_RATE_PRIMARY   = 0.070   # own Guard INS pool -> bonus Voltage (primary, 70% value share)
TURNOVER_RATE_SECONDARY = 0.0421  # own Guard VLT pool -> bonus Voltage (secondary, 30% value share, mean-normalized -- largest correction of the three since VLT's mean is furthest from INS's)
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
# QB is constant across all three (1 always) — same real-football
# convention this whole design already borrows from (Striker/Mid/Guard
# counts shift by formation, QB doesn't). Purely additive: the Striker/
# Mid/Guard counts are UNCHANGED from before QB existed, so the
# existing formation-pairing balance from voltball_tune.py's grid
# search isn't disturbed by this addition — verify with simulation
# before trusting that claim, not just by inspection (see the
# post-implementation simulation run).
# ─────────────────────────────────────────────
FORMATIONS = {
    "OFFENSE":  {"QB": 1, "Striker": 5, "Mid": 1, "Guard": 1},
    "BALANCED": {"QB": 1, "Striker": 3, "Mid": 2, "Guard": 2},
    "DEFENSE":  {"QB": 1, "Striker": 1, "Mid": 2, "Guard": 4},
}
ROSTER_SIZE = 8
POSITIONS = ("QB", "Striker", "Mid", "Guard")

# Internal keys (POSITIONS above) are never renamed -- they're baked
# into FORMATIONS, position_map, and every already-stored
# voltball_lineups.assignments JSONB row. This mapping exists purely
# for human-readable log text, matching the website's own DISPLAY_NAME
# constant exactly (lineup.html/scout.html/results.html) -- without it,
# match highlights said "Strikers"/"Mids" while every roster panel on
# the site said "Surge Back"/"Conductor", a real inconsistency a real
# person actually got confused by, not just a theoretical one.
DISPLAY_NAME = {"QB": "QB", "Striker": "Surge Back", "Mid": "Conductor", "Guard": "Guard"}

# QB reuses SPK (no new stat, no re-deriving anything from trait data
# across the collection) — but unlike Mid, which pools SPK additively,
# QB's SPK drives a MULTIPLIER on the whole team's Base_Offense. A
# strong QB lifts everything; a weak one drags it down. Calibrated
# against the REAL collection's measured SPK distribution (mean 54.61,
# stdev 11.93, p10=43, p90=74, p99=91, min 26 / max 100 across all
# 1,678 Zappies) — not a guessed range.
#
# The FIRST calibration attempt (rate=0.004, clamp [0.85,1.15]) was
# simulated before shipping and found to be badly overtuned: a modest,
# common gap (SPK 65 vs 45) produced an 87% win rate, and an extreme
# gap hit 100% — QB alone was fully overwhelming formation, tempo, and
# every random event combined, the opposite of "one input among
# several." Retuned by simulating a grid of (rate, clamp) pairs against
# three gap sizes (a common ~20-point gap, a p90-vs-p10 gap, and an
# extreme p99-vs-floor gap) until the shape looked right: a common gap
# is a real-but-modest edge (~56%), a strong gap matters more (~62%),
# and even the rare extreme case (~73%) leaves real room for the
# weaker-QB side to still win. At rate=0.0006: mean SPK (54.61) gives
# exactly 1.0 (baseline unchanged), p90 (74) gives ~1.012, p99 (91)
# gives ~1.022, the absolute floor (26) gives ~0.983 before clamping.
QB_BASELINE_SPK = 54.61
QB_MULT_RATE = 0.0006
QB_MULT_MIN = 0.95
QB_MULT_MAX = 1.05


def qb_multiplier(qb_spk: float) -> float:
    mult = 1.0 + (qb_spk - QB_BASELINE_SPK) * QB_MULT_RATE
    return max(QB_MULT_MIN, min(QB_MULT_MAX, mult))

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

# Injury -- rolled ONCE PER PLAYED ZAPPY, per match (not per quarter,
# unlike every other event above). 4% at Standard tempo (5.5) was
# chosen by working the actual expected-value math before picking a
# number: at 8 Zappies played/team/week, 4% gives ~28% chance of at
# least one injury in a given week and ~3-4 expected over a 10-week
# season -- present and real enough to make bench depth matter, without
# firing essentially every week. Scaled by the same swing_mult as the
# other events (Aggressive tempo = real injury risk on top of bigger
# score swings, Controlled = safer on both fronts), via
# _tempo_event_constants below. CPU teams skip this roll entirely (see
# Team.is_cpu) -- they re-roll a fresh random roster every week, so
# there's no continuity for an injury to attach to.
INJURY_CHANCE_BASE = 0.04


# ─────────────────────────────────────────────
# Tempo — a second per-week dial alongside Formation.
# Deliberately ASYMMETRIC, not a coinflip triangle: a naive "bigger
# swings both ways, same average" version is mathematically a no-op
# decision (see design discussion, citing Sirlin's RPS-design writing —
# equal-payoff RPS options are provably interchangeable, so they don't
# add a real choice). AGGRESSIVE trades a WORSE average outcome for
# bigger swings — a real bet, meant for an underdog. CONTROLLED trades
# a BETTER average outcome for smaller swings — protects a lead.
# STANDARD is today's math, byte-for-byte unchanged (all multipliers
# are 1.0, so _tempo_event_constants("STANDARD") reproduces the exact
# original constants).
#
# Deliberately does NOT touch OFFENSE_WEIGHT_PRIMARY/SECONDARY,
# PLAYMAKING_WEIGHT_PRIMARY/SECONDARY, DEFENSE_RATE, DEFENSE_CAP, or
# TURNOVER_RATE_PRIMARY/SECONDARY. Those are the tuned core formula (see
# voltball_tune.py's real-collection-data grid search, and the
# mean-normalized secondary-stat calibration that added the _PRIMARY/
# _SECONDARY split) — changing them here would silently re-break balance
# work that already took two separate calibration passes (the
# 97%-Defense-wins incident, then the two-stat-per-position addition).
# turnover_bonus specifically is also excluded on separate grounds: it's
# a deterministic per-quarter value (own_guard_pool * TURNOVER_RATE_PRIMARY
# + own_guard_secondary_pool * TURNOVER_RATE_SECONDARY),
# not a random swing — scaling it by tempo would just be a disguised
# flat buff/debuff, not real variance. Tempo only touches the RANDOM
# EVENT layer: the chance and magnitude of Lightning/Cold Streak/Trick
# Play/Momentum Swing/Interception/Shutdown.
# ─────────────────────────────────────────────
TEMPO_MIN = 1.0
TEMPO_MAX = 10.0
TEMPO_STEP = 0.5
TEMPO_DEFAULT = 5.5  # midpoint -- reproduces the old STANDARD math almost exactly (see below)

# A continuous 1-10 dial, 1 = most Controlled, 10 = most Aggressive, in
# 0.5 increments. Linearly interpolated between the same two endpoint
# multiplier pairs the original 3-preset version used -- this is a
# direct generalization, not a redesign of the underlying math, which
# is why the midpoint (5.5) reproduces the old STANDARD behavior:
# ev_mult interpolates to exactly 1.0 at 5.5, swing_mult to ~1.05 (not
# exactly 1.0 -- a ~5% deviation that's not worth a piecewise function
# to eliminate, since the whole point of a continuous dial is that
# "STANDARD" no longer needs to be a byte-exact preserved case).
_TEMPO_ENDPOINTS = {
    "ev_mult":    (1.06, 0.94),   # (at TEMPO_MIN=Controlled, at TEMPO_MAX=Aggressive)
    "swing_mult": (0.5, 1.6),
}


def clamp_tempo(value: float) -> float:
    """Clamps to [TEMPO_MIN, TEMPO_MAX] and snaps to the nearest 0.5 step."""
    value = max(TEMPO_MIN, min(TEMPO_MAX, value))
    return round(value / TEMPO_STEP) * TEMPO_STEP


def tempo_label(value: float) -> str:
    """Human-readable bucket for a numeric tempo value, for embeds/display."""
    if value <= 3.0:
        return "Controlled"
    if value >= 8.0:
        return "Aggressive"
    return "Standard"


def _lerp(t: float, lo: float, hi: float) -> float:
    frac = (t - TEMPO_MIN) / (TEMPO_MAX - TEMPO_MIN)
    return lo + (hi - lo) * frac


def _tempo_event_constants(tempo: float) -> dict:
    """
    Returns event chance/magnitude constants scaled for a given numeric
    tempo (1-10). See TEMPO endpoints above for the ev_mult/swing_mult
    design rationale (separate knobs for average vs. variance, since a
    single uniform event-scaling knob doesn't control the average
    reliably -- the event mix is upside-heavy by construction, verified
    by simulation during development).
    """
    tempo = clamp_tempo(tempo)
    swing_mult = _lerp(tempo, *_TEMPO_ENDPOINTS["swing_mult"])
    ev_mult = _lerp(tempo, *_TEMPO_ENDPOINTS["ev_mult"])
    s = swing_mult
    return {
        "STRIKER_LIGHTNING_CHANCE":   min(1.0, STRIKER_LIGHTNING_CHANCE * s),
        "STRIKER_LIGHTNING_BONUS":    STRIKER_LIGHTNING_BONUS * s,
        "STRIKER_COLDSTREAK_CHANCE":  min(1.0, STRIKER_COLDSTREAK_CHANCE * s),
        "STRIKER_COLDSTREAK_PENALTY": min(1.0, STRIKER_COLDSTREAK_PENALTY * s),
        "MID_TRICKPLAY_CHANCE":       min(1.0, MID_TRICKPLAY_CHANCE * s),
        "MID_MOMENTUM_CHANCE":        min(1.0, MID_MOMENTUM_CHANCE * s),
        "MID_MOMENTUM_MULT":          1.0 + (MID_MOMENTUM_MULT - 1.0) * s,
        "GUARD_INTERCEPTION_CHANCE":  min(1.0, GUARD_INTERCEPTION_CHANCE * s),
        "GUARD_INTERCEPTION_RATE":    GUARD_INTERCEPTION_RATE * s,
        "GUARD_SHUTDOWN_CHANCE":      min(1.0, GUARD_SHUTDOWN_CHANCE * s),
        "INJURY_CHANCE":              min(1.0, INJURY_CHANCE_BASE * s),
        "EV_MULT":                    ev_mult,
    }


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
    assignments:     dict                          # {"QB": [ZappyPlayer], "Striker": [...], "Mid": [...], "Guard": [...]}
    tempo:           float = TEMPO_DEFAULT          # 1.0 (Controlled) - 10.0 (Aggressive)
    is_cpu:          bool = False                   # CPU teams skip injury rolls -- see roll_injuries()

    # Match-state (not set at construction)
    momentum_multiplier: float = field(default=1.0, init=False)
    nine_lives_used:      bool = field(default=False, init=False)
    chaos_used:            bool = field(default=False, init=False)
    auto_lineup_penalty:   bool = field(default=False, init=False)  # True if this team was auto-fielded from holdings, no lineup submitted

    def __post_init__(self):
        if self.formation not in FORMATIONS:
            raise ValueError(f"Unknown formation: {self.formation}")
        if not (TEMPO_MIN <= self.tempo <= TEMPO_MAX):
            raise ValueError(f"tempo must be between {TEMPO_MIN} and {TEMPO_MAX}, got {self.tempo}")
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
                roster: list[ZappyPlayer], position_map: dict, tempo: float = TEMPO_DEFAULT, is_cpu: bool = False) -> Team:
    """
    Convenience builder.
    position_map: {asset_id: "QB"|"Striker"|"Mid"|"Guard"} for all ROSTER_SIZE roster Zappies.
    """
    by_id = {z.asset_id: z for z in roster}
    assignments = {pos: [] for pos in POSITIONS}
    for asset_id, pos in position_map.items():
        if pos not in POSITIONS:
            raise ValueError(f"Invalid position '{pos}' for asset {asset_id}")
        assignments[pos].append(by_id[asset_id])
    return Team(name=name, coach_hero_type=coach_hero_type, formation=formation, assignments=assignments, tempo=tempo, is_cpu=is_cpu)


def _base_quarter(quarter_num: int, offense_team: "Team", defense_team: "Team",
                   events_denied: bool = False, team_key: str = None) -> dict:
    """
    Computes one team's base Quarter Voltage against one opponent — formation
    math + random events only. Coach signature plays are layered on afterward
    in resolve_match, since several of them need cross-team information.

    team_key ("a"/"b", optional): only used to tag the parallel `events` list
    below with which side an event belongs to, for consumers that want
    structured data (site/Discord recap) instead of parsing `log` strings.
    Has zero effect on the score math — purely a labeling convenience.
    """
    log = []
    events = []  # structured mirror of `log`, flavor-only (no `delta` — see
                 # resolve_match's docstring note on why quarter-level events
                 # don't get an isolable per-line point delta).

    consts = _tempo_event_constants(offense_team.tempo)

    striker_pool = offense_team.pool("Striker", "VLT")
    mid_pool     = offense_team.pool("Mid", "SPK")
    own_guard_pool = offense_team.pool("Guard", "INS")
    opp_guard_pool = defense_team.pool("Guard", "INS")

    # Secondary stats -- rotation is VLT -> SPK -> INS -> VLT, so every
    # stat is a primary somewhere and a secondary assist somewhere else.
    striker_secondary_pool = offense_team.pool("Striker", "SPK")
    mid_secondary_pool     = offense_team.pool("Mid", "INS")
    own_guard_secondary_pool = offense_team.pool("Guard", "VLT")

    striker_component = (striker_pool * OFFENSE_WEIGHT_PRIMARY
                          + striker_secondary_pool * OFFENSE_WEIGHT_SECONDARY)
    mid_component     = (mid_pool * PLAYMAKING_WEIGHT_PRIMARY
                          + mid_secondary_pool * PLAYMAKING_WEIGHT_SECONDARY)

    if events_denied:
        log.append(f"  🐊 **DEATH ROLL** — {defense_team.name}'s defense stuffs {offense_team.name} before the play even develops!")
        events.append({"quarter": quarter_num, "kind": "denied", "icon": "🐊", "team": team_key,
                        "text": f"{defense_team.name}'s defense stuffs {offense_team.name} before the play even develops.", "delta": None})
    else:
        # ── Striker events ──
        if random.random() < consts["STRIKER_LIGHTNING_CHANCE"]:
            striker_component *= (1 + consts["STRIKER_LIGHTNING_BONUS"])
            log.append(f"  ⚡ **LIGHTNING STRIKE** — {offense_team.name}'s {DISPLAY_NAME['Striker']}s break off a huge gain!")
            events.append({"quarter": quarter_num, "kind": "lightning", "icon": "⚡", "team": team_key,
                            "text": f"{offense_team.name}'s {DISPLAY_NAME['Striker']}s break off a huge gain.", "delta": None})
        elif random.random() < consts["STRIKER_COLDSTREAK_CHANCE"]:
            striker_component *= (1 - consts["STRIKER_COLDSTREAK_PENALTY"])
            log.append(f"  ❄️ **COLD STREAK** — {offense_team.name}'s {DISPLAY_NAME['Striker']}s get stuffed at the line this quarter.")
            events.append({"quarter": quarter_num, "kind": "coldstreak", "icon": "❄️", "team": team_key,
                            "text": f"{offense_team.name}'s {DISPLAY_NAME['Striker']}s get stuffed at the line.", "delta": None})

        # ── Mid events ──
        if random.random() < consts["MID_TRICKPLAY_CHANCE"]:
            mid_component *= 2.0
            log.append(f"  🎭 **TRICK PLAY** — {offense_team.name}'s {DISPLAY_NAME['Mid']}s catch the defense off guard for a huge gain!")
            events.append({"quarter": quarter_num, "kind": "trickplay", "icon": "🎭", "team": team_key,
                            "text": f"{offense_team.name}'s {DISPLAY_NAME['Mid']}s catch the defense off guard.", "delta": None})
        elif random.random() < consts["MID_MOMENTUM_CHANCE"]:
            offense_team.momentum_multiplier *= consts["MID_MOMENTUM_MULT"]
            log.append(f"  📈 **MOMENTUM SWING** — {offense_team.name} seizes the momentum and doesn't look back!")
            events.append({"quarter": quarter_num, "kind": "momentum", "icon": "📈", "team": team_key,
                            "text": f"{offense_team.name} seizes the momentum and doesn't look back.", "delta": None})

    base_offense = striker_component + mid_component

    # ── Guard events ── (not subject to events_denied — that only covers the offense_team's own attacking events above)
    shutdown = (not events_denied) and random.random() < consts["GUARD_SHUTDOWN_CHANCE"]
    defense_reduction = 0.0 if shutdown else min(opp_guard_pool * DEFENSE_RATE, DEFENSE_CAP)
    if shutdown:
        log.append(f"  🛑 **SHUTDOWN** — {offense_team.name} blows right past {defense_team.name}'s defense, untouched!")
        events.append({"quarter": quarter_num, "kind": "shutdown", "icon": "🛑", "team": team_key,
                        "text": f"{offense_team.name} blows right past {defense_team.name}'s defense, untouched.", "delta": None})

    turnover_bonus = (own_guard_pool * TURNOVER_RATE_PRIMARY
                       + own_guard_secondary_pool * TURNOVER_RATE_SECONDARY)
    if not events_denied and random.random() < consts["GUARD_INTERCEPTION_CHANCE"]:
        extra = own_guard_pool * consts["GUARD_INTERCEPTION_RATE"]
        turnover_bonus += extra
        log.append(f"  🥊 **INTERCEPTION** — {offense_team.name}'s {DISPLAY_NAME['Guard']}s pick it off and take it to the house!")
        events.append({"quarter": quarter_num, "kind": "interception", "icon": "🥊", "team": team_key,
                        "text": f"{offense_team.name}'s {DISPLAY_NAME['Guard']}s pick it off and take it to the house.", "delta": None})

    quarter_voltage = (base_offense * (1 - defense_reduction) + turnover_bonus) * offense_team.momentum_multiplier
    quarter_voltage *= consts["EV_MULT"]
    quarter_voltage *= qb_multiplier(offense_team.pool("QB", "SPK"))

    if offense_team.auto_lineup_penalty:
        quarter_voltage *= NO_LINEUP_PENALTY
        log.append(f"  📋 {offense_team.name} never submitted a game plan this week — playing disorganized.")
        events.append({"quarter": quarter_num, "kind": "penalty", "icon": "📋", "team": team_key,
                        "text": f"{offense_team.name} never submitted a game plan this week — playing disorganized.", "delta": None})

    return {
        "voltage": quarter_voltage,
        "log": log,
        "events": events,
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
            log.append(f"  🦸 **{label}** — {team.name} catches a break and cashes in (+{sig['bonus']}).")
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


def roll_injuries(team: "Team") -> list["ZappyPlayer"]:
    """
    Rolls injury chance once per rostered Zappy, post-match -- not
    per-quarter like the other events, since availability is a
    match-level outcome (hurt or not), not something that escalates or
    resets within a single game. Skipped entirely for CPU teams (see
    Team.is_cpu) -- they re-roll a fresh random roster every week, so
    there's no continuity for an injury to attach to.
    """
    if team.is_cpu:
        return []
    consts = _tempo_event_constants(team.tempo)
    injured = []
    for players in team.assignments.values():
        for z in players:
            if random.random() < consts["INJURY_CHANCE"]:
                injured.append(z)
    return injured


def resolve_match(team_a: Team, team_b: Team) -> dict:
    """
    Resolves a full 4-quarter Voltball match between two teams.

    Alongside the existing Discord-flavored `log`/`log_text`, this also
    returns a structured `events` list for consumers (site playback,
    future recap generation) that need machine-readable data instead of
    parsing markdown strings.

    IMPORTANT — why most events carry `delta: None`:
    Voltage is NOT football scoring. A quarter's score is one continuous
    formula (pools -> defense reduction -> turnover bonus -> momentum ->
    tempo EV -> QB multiplier), and most in-quarter events (Lightning
    Strike, Trick Play, Interception, etc.) scale a COMPONENT of that
    formula, not a standalone point total — the same event is worth a
    different number of points depending on the rest of the formula that
    quarter. There is no honest single number to attach to "Lightning
    Strike" the way a touchdown is always worth 7. Giving these a fake
    delta would just reintroduce the exact bug this was built to fix
    (text and score visibly disagreeing) one level down.

    Coach SIGNATURE plays are the one category that genuinely IS a clean,
    isolated add/subtract on top of the formula (see HERO_SIGNATURES) —
    those get a real `delta` and are the natural "big play" moment for a
    UI to spotlight, same role a scoring play fills in real football.

    The actual score reveal happens once per quarter, matching the
    formula's real granularity — see `quarter_totals` in the return dict.
    """
    log = [f"🏟️ **VOLTBALL MATCH** — {team_a.name} ({team_a.formation}) vs. {team_b.name} ({team_b.formation})", ""]
    events = []

    score_a, score_b = 0.0, 0.0
    quarter_breakdown = []

    for q in range(1, QUARTERS + 1):
        log.append(f"**— Quarter {q} —**")

        # Death Roll denial rolls happen before the opponent's base quarter is computed.
        a_denies_b = team_a.signature and team_a.signature["type"] == "deny_chance" and random.random() < team_a.signature["chance"]
        b_denies_a = team_b.signature and team_b.signature["type"] == "deny_chance" and random.random() < team_b.signature["chance"]

        result_a = _base_quarter(q, team_a, team_b, events_denied=b_denies_a, team_key="a")
        result_b = _base_quarter(q, team_b, team_a, events_denied=a_denies_b, team_key="b")
        events.extend(result_a["events"])
        events.extend(result_b["events"])

        sig_delta_a, sig_log_a, target_a = _apply_signature(q, team_a, team_b, result_a, result_b, score_a, score_b)
        sig_delta_b, sig_log_b, target_b = _apply_signature(q, team_b, team_a, result_b, result_a, score_b, score_a)

        voltage_a = result_a["voltage"] + (sig_delta_a if target_a == "self" else 0)
        voltage_b = result_b["voltage"] + (sig_delta_b if target_b == "self" else 0)

        if target_a == "opponent":
            voltage_b = max(0.0, voltage_b - sig_delta_a)
        if target_b == "opponent":
            voltage_a = max(0.0, voltage_a - sig_delta_b)

        # Signature plays are the one event type with a clean, isolated
        # delta — tag them so the UI can spotlight them like a scoring play.
        if sig_delta_a and target_a == "self":
            events.append({"quarter": q, "kind": "signature", "icon": "🦸", "team": "a",
                            "text": f"{team_a.name} — {team_a.signature['label']}.", "delta": round(sig_delta_a, 1)})
        elif sig_delta_a and target_a == "opponent":
            events.append({"quarter": q, "kind": "signature", "icon": "🦸", "team": "b",
                            "text": f"{team_a.name}'s {team_a.signature['label']} wears down {team_b.name}.", "delta": round(-sig_delta_a, 1)})
        if sig_delta_b and target_b == "self":
            events.append({"quarter": q, "kind": "signature", "icon": "🦸", "team": "b",
                            "text": f"{team_b.name} — {team_b.signature['label']}.", "delta": round(sig_delta_b, 1)})
        elif sig_delta_b and target_b == "opponent":
            events.append({"quarter": q, "kind": "signature", "icon": "🦸", "team": "a",
                            "text": f"{team_b.name}'s {team_b.signature['label']} wears down {team_a.name}.", "delta": round(-sig_delta_b, 1)})

        # Death Roll's capitalize-on-lockdown bonus, applied directly since it
        # depends on the deny roll made earlier this quarter, not on _apply_signature.
        if a_denies_b and team_a.signature and team_a.signature["type"] == "deny_chance":
            bonus = team_a.signature["bonus"]
            voltage_a += bonus
            log.append(f"  🦸 **{team_a.signature['label']}** — {team_a.name} forces a takeaway and cashes in a bonus drive (+{bonus}).")
            events.append({"quarter": q, "kind": "signature", "icon": "🦸", "team": "a",
                            "text": f"{team_a.name} forces a takeaway and cashes in a bonus drive.", "delta": round(bonus, 1)})
        if b_denies_a and team_b.signature and team_b.signature["type"] == "deny_chance":
            bonus = team_b.signature["bonus"]
            voltage_b += bonus
            log.append(f"  🦸 **{team_b.signature['label']}** — {team_b.name} forces a takeaway and cashes in a bonus drive (+{bonus}).")
            events.append({"quarter": q, "kind": "signature", "icon": "🦸", "team": "b",
                            "text": f"{team_b.name} forces a takeaway and cashes in a bonus drive.", "delta": round(bonus, 1)})

        voltage_a, voltage_b = round(voltage_a, 1), round(voltage_b, 1)
        score_a += voltage_a
        score_b += voltage_b

        # Guaranteed per-quarter commentary line. Random flavor events
        # (Lightning Strike, Trick Play, etc.) don't fire every quarter --
        # a quarter can legitimately have zero of them, which otherwise
        # leaves nothing but a bare score tally with no explanation of why
        # it landed there. This is always derived from the real voltage_a/
        # voltage_b just computed above, purely qualitative (no invented
        # number), so it can never disagree with the score the way a fake
        # per-event delta would.
        #
        # Phrasing rotates deterministically by quarter number (not
        # random) -- a team that's ahead every single quarter should
        # still get 4 different sentences across the match, not the
        # exact same one four times in a row.
        margin = abs(voltage_a - voltage_b)
        bigger = max(voltage_a, voltage_b, 0.01)
        ratio = margin / bigger
        if ratio < 0.12:
            summary_team = None
            even_phrasings = [
                "An even quarter — {a} and {b} trade blows.",
                "{a} and {b} go blow for blow this quarter.",
                "Nothing separates {a} and {b} this quarter.",
                "A dead heat between {a} and {b}.",
            ]
            summary_text = even_phrasings[(q - 1) % len(even_phrasings)].format(a=team_a.name, b=team_b.name)
        else:
            leader, leader_key = (team_a, "a") if voltage_a >= voltage_b else (team_b, "b")
            summary_team = leader_key
            if ratio >= 0.35:
                dominant_phrasings = [
                    "{leader} dominates the quarter.",
                    "{leader} pulls away.",
                    "{leader} takes total control of the quarter.",
                    "{leader} runs away with it this quarter.",
                ]
                summary_text = dominant_phrasings[(q - 1) % len(dominant_phrasings)].format(leader=leader.name)
            else:
                edge_phrasings = [
                    "{leader} edges the quarter.",
                    "{leader} keeps a slim lead.",
                    "{leader} stays a step ahead.",
                    "{leader}'s advantage stays narrow but real.",
                ]
                summary_text = edge_phrasings[(q - 1) % len(edge_phrasings)].format(leader=leader.name)
        events.append({"quarter": q, "kind": "quarter_summary", "icon": "📊", "team": summary_team,
                        "text": summary_text, "delta": None})

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
            events.append({"quarter": worst_idx + 1, "kind": "signature", "icon": "🦸",
                            "team": "a" if team is team_a else "b",
                            "text": f"{team.name}'s {team.signature['label']} refuses to let Quarter {worst_idx+1} sink them.", "delta": round(bonus, 1)})

    score_a, score_b = round(score_a, 1), round(score_b, 1)

    # Cumulative running score at the end of each quarter, computed AFTER the
    # Nine Lives correction above so it can never drift from the real final
    # score — this is what a UI should reveal once per quarter, not a
    # per-event bump (see docstring).
    quarter_totals = []
    cum_a, cum_b = 0.0, 0.0
    for qb in quarter_breakdown:
        cum_a += qb["team_a_voltage"]
        cum_b += qb["team_b_voltage"]
        quarter_totals.append({"quarter": qb["quarter"], "team_a_total": round(cum_a, 1), "team_b_total": round(cum_b, 1)})

    # ── Winner (tie -> weighted fate roll, same spirit as battle_engine) ──
    if score_a > score_b:
        winner, loser = team_a, team_b
    elif score_b > score_a:
        winner, loser = team_b, team_a
    else:
        total_a = sum(team_a.pool(p, s) for p, s in
                      [("QB", "SPK"), ("Striker", "VLT"), ("Mid", "SPK"), ("Guard", "INS")])
        total_b = sum(team_b.pool(p, s) for p, s in
                      [("QB", "SPK"), ("Striker", "VLT"), ("Mid", "SPK"), ("Guard", "INS")])
        roll_a = random.randint(1, max(1, total_a))
        roll_b = random.randint(1, max(1, total_b))
        log.append(f"⚡ **TIE — Fate decides!** {team_a.name} rolls {roll_a}, {team_b.name} rolls {roll_b}.")
        winner, loser = (team_a, team_b) if roll_a >= roll_b else (team_b, team_a)
        events.append({"quarter": None, "kind": "tie_breaker", "icon": "⚡", "team": None,
                        "text": f"Tied — fate decides. {team_a.name} rolls {roll_a}, {team_b.name} rolls {roll_b}.", "delta": None})

    log.append(f"🏆 **FINAL: {team_a.name} {score_a} — {score_b} {team_b.name}**")
    log.append(f"🏆 **{winner.name} wins!**")

    injured_a = roll_injuries(team_a)
    injured_b = roll_injuries(team_b)
    for z in injured_a:
        log.append(f"  🤕 **INJURY** — {team_a.name}'s {z.name} goes down, out next week.")
        events.append({"quarter": None, "kind": "injury", "icon": "🤕", "team": "a",
                        "text": f"{team_a.name}'s {z.name} goes down, out next week.", "delta": None})
    for z in injured_b:
        log.append(f"  🤕 **INJURY** — {team_b.name}'s {z.name} goes down, out next week.")
        events.append({"quarter": None, "kind": "injury", "icon": "🤕", "team": "b",
                        "text": f"{team_b.name}'s {z.name} goes down, out next week.", "delta": None})

    return {
        "team_a": team_a.name,
        "team_b": team_b.name,
        "score_a": score_a,
        "score_b": score_b,
        "winner": winner.name,
        "loser": loser.name,
        "quarters": quarter_breakdown,
        "quarter_totals": quarter_totals,
        "events": events,
        "injured_a": [{"asset_id": z.asset_id, "name": z.name} for z in injured_a],
        "injured_b": [{"asset_id": z.asset_id, "name": z.name} for z in injured_b],
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
    roster_a = make_roster("A", 8, vlt=65, ins=52, spk=52)
    pos_map_a = {0: "QB",
                 1: "Striker", 2: "Striker", 3: "Striker",
                 4: "Mid", 5: "Mid",
                 6: "Guard", 7: "Guard"}
    team_a = build_team("Thunder Coasts", "Wolf", "BALANCED", roster_a, pos_map_a)

    # Team B: Defense formation, Frog coach — deliberately mismatched vs Wolf's
    # Striker-flavored theme to prove the signature play still applies.
    roster_b = make_roster("B", 8, vlt=68, ins=45, spk=50)
    pos_map_b = {0: "QB",
                 1: "Striker",
                 2: "Mid", 3: "Mid",
                 4: "Guard", 5: "Guard", 6: "Guard", 7: "Guard"}
    team_b = build_team("Volt Runners", "Frog", "DEFENSE", roster_b, pos_map_b)

    result = resolve_match(team_a, team_b)
    print(result["log_text"])

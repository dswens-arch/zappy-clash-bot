"""
voltball_schedule.py
---------------------
Generates and stores the full-season schedule ONCE, at season start —
not regenerated week to week. Uses the standard "circle method" for
round-robin pairings: N-1 rounds for even team counts (N rounds with a
bye for odd counts), then cycles through those rounds again as many
times as needed to fill the season's week_count.

Example: 12 teams -> 11 rounds per single pass. A 16-week season plays
the full 11-round cycle once, then repeats the first 5 rounds of a
second pass to reach 16 (matches teams facing most others twice, some
only once — acceptable per the season design's "double round-robin or
partial second pass" discussion).
"""

import random
from database import get_supabase


def _generate_round_robin_rounds(team_ids: list[str]) -> list[list[tuple[str, str | None]]]:
    """
    Standard circle-method round robin. Returns a list of rounds, each a
    list of (team_a, team_b) pairings. team_b is None for a bye when the
    team count is odd (a dummy None seat rotates through).
    """
    teams = list(team_ids)
    if len(teams) % 2 == 1:
        teams.append(None)  # bye seat

    n = len(teams)
    num_rounds = n - 1
    half = n // 2

    rounds = []
    current = teams[:]
    for _ in range(num_rounds):
        pairings = []
        for i in range(half):
            t1, t2 = current[i], current[n - 1 - i]
            if t1 is None:  # normalize so the bye slot is always second, consistently, everywhere this is used
                t1, t2 = t2, t1
            pairings.append((t1, t2))
        rounds.append(pairings)
        # Rotate all but the first seat
        current = [current[0]] + [current[-1]] + current[1:-1]

    return rounds


def generate_schedule(team_ids: list[str], week_count: int, shuffle_seed: int | None = None) -> list[list[tuple[str, str | None]]]:
    """
    Returns week_count weeks of pairings, cycling through the round-robin
    rounds as many times as needed. Teams are shuffled once before
    generating (deterministically if shuffle_seed given) so the schedule
    isn't always in team-creation order.
    """
    teams = list(team_ids)
    rng = random.Random(shuffle_seed)
    rng.shuffle(teams)

    rounds = _generate_round_robin_rounds(teams)
    if not rounds:
        return []

    weeks = []
    for week_idx in range(week_count):
        weeks.append(rounds[week_idx % len(rounds)])
    return weeks


def save_schedule(season_id: str, team_ids: list[str], week_count: int, shuffle_seed: int | None = None):
    """
    Generates and writes the full schedule to voltball_schedule in one
    batch insert. Call this once when a season is started/activated —
    NOT on every weekly resolution.
    """
    weeks = generate_schedule(team_ids, week_count, shuffle_seed=shuffle_seed)

    rows = []
    for week_idx, pairings in enumerate(weeks, start=1):
        for team_a, team_b in pairings:
            rows.append({
                "season_id": season_id,
                "week_number": week_idx,
                "team_a_id": team_a,
                "team_b_id": team_b,  # None = bye
                "is_playoff": False,
            })

    db = get_supabase()
    if rows:
        db.table("voltball_schedule").insert(rows).execute()
    return rows


def save_playoff_round(season_id: str, week_number: int, pairings: list[tuple[str, str]]):
    """
    Writes a single playoff round's pairings — 2 semifinal matchups, or
    the 1 championship matchup. Unlike save_schedule(), this is called
    incrementally, once per round, as each round's outcome determines
    the next: semifinal winners aren't known until the semifinal week
    actually resolves, so the championship pairing can't be generated
    up front the way the regular season's full schedule can.
    """
    rows = [
        {"season_id": season_id, "week_number": week_number, "team_a_id": a, "team_b_id": b, "is_playoff": True}
        for a, b in pairings
    ]
    db = get_supabase()
    if rows:
        db.table("voltball_schedule").insert(rows).execute()
    return rows


def get_week_pairings(season_id: str, week_number: int) -> list[dict]:
    """
    Returns this week's real matchups (byes excluded) as
    [{"team_a_id": ..., "team_b_id": ..., "scheduled_kickoff_at": ...,
    "resolved_at": ...}, ...]. scheduled_kickoff_at is None until
    _open_season_week (voltball_cog.py) assigns it; resolved_at is None
    until that pairing's match has actually been simulated.

    Deliberately does NOT filter by is_playoff — regular season weeks
    are always 1..week_count and playoff weeks are always week_count+1
    (semis) / week_count+2 (final), so week numbers never collide
    between phases. A given week is unambiguously one or the other.
    """
    db = get_supabase()
    rows = (
        db.table("voltball_schedule")
        .select("id, season_id, week_number, is_playoff, team_a_id, team_b_id, scheduled_kickoff_at, resolved_at")
        .eq("season_id", season_id)
        .eq("week_number", week_number)
        .execute()
        .data
    ) or []
    return [r for r in rows if r["team_b_id"] is not None]


def get_bye_team(season_id: str, week_number: int) -> str | None:
    """Returns the team_id sitting out this week, if any (odd team count)."""
    db = get_supabase()
    rows = (
        db.table("voltball_schedule")
        .select("team_a_id, team_b_id")
        .eq("season_id", season_id)
        .eq("week_number", week_number)
        .execute()
        .data
    ) or []
    for r in rows:
        if r["team_b_id"] is None:
            return r["team_a_id"]
    return None


def week_is_open(season_id: str, week_number: int) -> bool:
    """
    True once this week's real pairings have a scheduled_kickoff_at —
    i.e. _open_season_week (daily open job or /voltball_open_week) has
    already run for this week. Used as the guard against double-
    assigning times or double-posting the matchup preview.
    """
    db = get_supabase()
    rows = (
        db.table("voltball_schedule")
        .select("id")
        .eq("season_id", season_id)
        .eq("week_number", week_number)
        .not_.is_("team_b_id", "null")
        .not_.is_("scheduled_kickoff_at", "null")
        .limit(1)
        .execute()
        .data
    ) or []
    return len(rows) > 0


def open_week(season_id: str, week_number: int, kickoff_times: dict):
    """
    Writes each real pairing's assigned scheduled_kickoff_at, called
    once at week-open time. kickoff_times keys are (team_a_id,
    team_b_id) tuples matching get_week_pairings' rows, values are ISO
    timestamp strings. One UPDATE per pairing — this table is small
    (one row per matchup per week), so there's no batching concern here
    the way there is for the season-long insert in save_schedule().
    """
    db = get_supabase()
    for (team_a_id, team_b_id), kickoff_at in kickoff_times.items():
        db.table("voltball_schedule").update({"scheduled_kickoff_at": kickoff_at}).eq(
            "season_id", season_id
        ).eq("week_number", week_number).eq("team_a_id", team_a_id).eq("team_b_id", team_b_id).execute()


def get_due_pairings(cutoff_iso: str) -> list[dict]:
    """
    Real pairings (any season, any guild) whose scheduled_kickoff_at is
    at or before cutoff_iso and haven't been resolved yet. Caller
    passes now + PLAYBACK_KICKOFF_DELAY_SECONDS as cutoff_iso so a
    match's actual simulation (which reads and locks in that pairing's
    lineup) happens PLAYBACK_KICKOFF_DELAY_SECONDS before its
    announced/displayed kickoff time — the same lead time the "kicks
    off in 5 minutes" kickoff post has always promised, now gating the
    simulation itself rather than only the post. This is the real lock
    instant; submit-lineup.ts's deadline check must use the same math.

    resolved_at is the single source of truth for "has this pairing
    been processed" — including forfeits, which never get a
    voltball_matches row, so that table alone can't answer this. This
    also replaces the old duplicate-match guard: a restart mid-batch
    just resumes here since already-resolved pairings drop out of this
    query on their own.
    """
    db = get_supabase()
    rows = (
        db.table("voltball_schedule")
        .select("id, season_id, week_number, team_a_id, team_b_id, is_playoff, scheduled_kickoff_at")
        .not_.is_("team_b_id", "null")
        .not_.is_("scheduled_kickoff_at", "null")
        .is_("resolved_at", "null")
        .lte("scheduled_kickoff_at", cutoff_iso)
        .execute()
        .data
    ) or []
    return rows


def mark_pairing_resolved(schedule_row_id: str, resolved_at_iso: str):
    """Marks one pairing done — real match, single forfeit, or double forfeit all call this."""
    db = get_supabase()
    db.table("voltball_schedule").update({"resolved_at": resolved_at_iso}).eq("id", schedule_row_id).execute()


def count_unresolved_pairings(season_id: str, week_number: int) -> int:
    """
    How many of this week's real pairings still haven't resolved —
    0 means the week is fully done and it's safe to run the playoff
    bracket / current_week advance logic. Same "still pending" pattern
    post_ready_recaps already uses for last_completed_week, applied one
    step earlier since resolution itself is now spread across the day
    instead of happening in one batch.
    """
    db = get_supabase()
    rows = (
        db.table("voltball_schedule")
        .select("id")
        .eq("season_id", season_id)
        .eq("week_number", week_number)
        .not_.is_("team_b_id", "null")
        .is_("resolved_at", "null")
        .execute()
        .data
    ) or []
    return len(rows)

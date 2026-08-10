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


def get_week_pairings(season_id: str, week_number: int) -> list[dict]:
    """
    Returns this week's real matchups (byes excluded) as
    [{"team_a_id": ..., "team_b_id": ...}, ...] for the weekly resolution
    job to build Team objects and call resolve_match() against.
    """
    db = get_supabase()
    rows = (
        db.table("voltball_schedule")
        .select("team_a_id, team_b_id")
        .eq("season_id", season_id)
        .eq("week_number", week_number)
        .eq("is_playoff", False)
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

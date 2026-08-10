"""
voltball_db.py
---------------
Supabase read/write helpers for Voltball teams, seasons, and standings.
Lineup-specific writes live in voltball_lineup_service.py since they're
tangled up with roster validation; this module covers the surrounding
team/season/standings queries the cog needs.

Uses the same `from database import get_supabase` pattern confirmed in
algo_quota_guard.py — no guessing at client setup here.
"""

from datetime import datetime, timezone
from database import get_supabase


def get_team_by_id(team_id: str) -> dict | None:
    """Returns a team row by id — used by the weekly resolution job to look up both sides of a pairing."""
    db = get_supabase()
    result = db.table("voltball_teams").select("*").eq("id", team_id).execute()
    rows = result.data or []
    return rows[0] if rows else None


def create_season(guild_id: str, name: str, week_count: int, is_test: bool = False) -> dict:
    """Creates a new season in 'upcoming' status — teams can register once this exists."""
    db = get_supabase()
    row = {
        "guild_id": guild_id,
        "name": name,
        "week_count": week_count,
        "status": "upcoming",
        "current_week": 0,
        "is_test": is_test,
    }
    result = db.table("voltball_seasons").insert(row).execute()
    return result.data[0]


def list_seasons(guild_id: str) -> list[dict]:
    """All seasons for a guild, newest first — used by /voltball_season_wipe to pick a target."""
    db = get_supabase()
    result = db.table("voltball_seasons").select("*").eq("guild_id", guild_id).order("created_at", desc=True).execute()
    return result.data or []


def wipe_season(season_id: str):
    """
    Deletes EVERYTHING tied to a season — teams, lineups, matches,
    standings, schedule, and the season row itself. No permanent record
    survives. Used for test seasons, or scrapping a season that needs a
    clean restart. Irreversible — the caller (Discord command) should
    confirm with the admin before calling this.
    """
    db = get_supabase()
    for table in ["voltball_matches", "voltball_standings", "voltball_lineups", "voltball_schedule", "voltball_teams"]:
        db.table(table).delete().eq("season_id", season_id).execute()
    db.table("voltball_seasons").delete().eq("id", season_id).execute()


def get_teams_for_season(season_id: str) -> list[dict]:
    """Returns every registered team for a season — used to generate the schedule at season start."""
    db = get_supabase()
    result = db.table("voltball_teams").select("*").eq("season_id", season_id).execute()
    return result.data or []


def get_lineup(team_id: str, week_number: int) -> dict | None:
    """Returns a team's submitted lineup row for a given week, or None if they never set one."""
    db = get_supabase()
    result = (
        db.table("voltball_lineups")
        .select("*")
        .eq("team_id", team_id)
        .eq("week_number", week_number)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def get_week_lineups(season_id: str, week_number: int) -> list[dict]:
    """
    Returns every team's lineup (or lack of one) for a given week, joined
    with team names/hero types — powers the public /voltball_lineups
    scouting report. Teams that haven't submitted yet still appear, with
    lineup=None, so the report shows the full field either way.
    """
    teams = get_teams_for_season(season_id)
    db = get_supabase()
    lineup_rows = (
        db.table("voltball_lineups")
        .select("*")
        .eq("season_id", season_id)
        .eq("week_number", week_number)
        .execute()
        .data
    ) or []
    lineup_by_team = {r["team_id"]: r for r in lineup_rows}

    return [
        {
            "team_id": t["id"],
            "team_name": t["team_name"],
            "hero_type": t["hero_type"],
            "lineup": lineup_by_team.get(t["id"]),
        }
        for t in teams
    ]


def get_guild_config(guild_id: str) -> dict:
    """
    Returns this guild's Voltball config, with defaults filled in if no
    row exists yet (announcement_channel_id=None means "not configured,
    don't post anywhere"; resolution_weekday defaults to 6/Sunday).
    """
    db = get_supabase()
    result = db.table("voltball_config").select("*").eq("guild_id", guild_id).execute()
    rows = result.data or []
    if rows:
        return rows[0]
    return {"guild_id": guild_id, "announcement_channel_id": None, "resolution_weekday": 6}


def set_guild_config(guild_id: str, announcement_channel_id: str | None = None, resolution_weekday: int | None = None):
    """Upserts guild config — only overwrites the fields actually passed."""
    db = get_supabase()
    current = get_guild_config(guild_id)
    row = {
        "guild_id": guild_id,
        "announcement_channel_id": announcement_channel_id if announcement_channel_id is not None else current.get("announcement_channel_id"),
        "resolution_weekday": resolution_weekday if resolution_weekday is not None else current.get("resolution_weekday", 6),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    db.table("voltball_config").upsert(row).execute()
    return row


def get_active_season(guild_id: str) -> dict | None:
    """
    Returns the active season row for this guild, or None if none is active.
    NOTE: deliberately NOT using .single() here — PostgREST's .single()
    raises PGRST116 ("Cannot coerce the result to a single JSON object")
    when zero rows match, rather than returning None. That's the correct,
    documented behavior for real Supabase/PostgREST — the mock client used
    during development incorrectly returned None for an empty result,
    which is why this bug wasn't caught until testing against the real DB.
    Using the same plain-list pattern as get_team_by_owner instead, which
    handles zero rows gracefully.
    """
    db = get_supabase()
    result = (
        db.table("voltball_seasons")
        .select("*")
        .eq("guild_id", guild_id)
        .eq("status", "active")
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def get_upcoming_season(guild_id: str) -> dict | None:
    """Returns the upcoming (not-yet-started) season for this guild, if any — used by /voltball_season_start."""
    db = get_supabase()
    result = (
        db.table("voltball_seasons")
        .select("*")
        .eq("guild_id", guild_id)
        .eq("status", "upcoming")
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def get_team_by_owner(guild_id: str, owner_discord_id: str, season_id: str) -> dict | None:
    """Returns this coach's team for the given season, or None if not registered."""
    db = get_supabase()
    result = (
        db.table("voltball_teams")
        .select("*")
        .eq("guild_id", guild_id)
        .eq("owner_discord_id", owner_discord_id)
        .eq("season_id", season_id)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def create_team(guild_id: str, owner_discord_id: str, wallet_address: str, team_name: str,
                 hero_asset_id: int, hero_type: str, is_collab_hero: bool, season_id: str) -> dict:
    """
    Inserts a new team. Relies on the DB-level UNIQUE constraints (one team
    per coach per season, one team per Hero NFT per season) to reject
    duplicates — catch the resulting exception in the caller rather than
    pre-checking here, since that's a race-free way to enforce it.
    """
    db = get_supabase()
    row = {
        "guild_id": guild_id,
        "owner_discord_id": owner_discord_id,
        "wallet_address": wallet_address,
        "team_name": team_name,
        "hero_asset_id": hero_asset_id,
        "hero_type": hero_type,
        "is_collab_hero": is_collab_hero,
        "season_id": season_id,
    }
    result = db.table("voltball_teams").insert(row).execute()
    return result.data[0]


def get_standings(season_id: str) -> list[dict]:
    """
    Returns standings rows joined with team_name, ordered by wins then
    points_for — matches the ordering the /voltball_standings embed expects.
    """
    db = get_supabase()
    result = (
        db.table("voltball_standings")
        .select("*, voltball_teams(team_name)")
        .eq("season_id", season_id)
        .order("wins", desc=True)
        .order("points_for", desc=True)
        .execute()
    )
    rows = result.data or []
    # Flatten the joined team_name up a level for easier use in the embed.
    for r in rows:
        r["team_name"] = (r.get("voltball_teams") or {}).get("team_name", "Unknown Team")
    return rows


def update_standings_after_match(season_id: str, winner_team_id: str, loser_team_id: str,
                                  winner_score: float, loser_score: float):
    """
    Increments both teams' standings rows after a match resolves. Upserts
    so a team's first-ever match creates its standings row automatically.
    """
    db = get_supabase()

    for team_id, is_winner, points_for, points_against in [
        (winner_team_id, True, winner_score, loser_score),
        (loser_team_id, False, loser_score, winner_score),
    ]:
        existing = (
            db.table("voltball_standings")
            .select("*")
            .eq("team_id", team_id)
            .eq("season_id", season_id)
            .execute()
        )
        current = existing.data[0] if existing.data else {
            "wins": 0, "losses": 0, "points_for": 0, "points_against": 0, "streak": 0,
        }

        new_streak = (current["streak"] + 1 if current["streak"] >= 0 else 1) if is_winner \
            else (current["streak"] - 1 if current["streak"] <= 0 else -1)

        db.table("voltball_standings").upsert({
            "team_id": team_id,
            "season_id": season_id,
            "wins": current["wins"] + (1 if is_winner else 0),
            "losses": current["losses"] + (0 if is_winner else 1),
            "points_for": current["points_for"] + points_for,
            "points_against": current["points_against"] + points_against,
            "streak": new_streak,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

"""
database.py
-----------
All database operations using Supabase (Postgres).
Handles: wallet links, CP scores, win streaks, brackets, leaderboard.

You will paste your SUPABASE_URL and SUPABASE_KEY into .env
"""

import os
import asyncio
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client


# ─────────────────────────────────────────────
# Setup — reads from environment variables
# ─────────────────────────────────────────────
def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in .env")
    return create_client(url, key)


# ─────────────────────────────────────────────
# WALLET LINKING
# ─────────────────────────────────────────────

def link_wallet(discord_user_id: str, wallet_address: str) -> dict:
    """
    Save a Discord user's Algorand wallet address.
    One wallet per Discord user. Overwrites if already linked.
    """
    db = get_supabase()
    data = {
        "discord_user_id": discord_user_id,
        "wallet_address":  wallet_address,
        "linked_at":       datetime.now(timezone.utc).isoformat(),
    }
    result = db.table("wallets").upsert(data, on_conflict="discord_user_id").execute()
    return result.data


def get_wallet(discord_user_id: str) -> str | None:
    """Returns the wallet address for a Discord user, or None if not linked."""
    db = get_supabase()
    result = db.table("wallets").select("wallet_address").eq("discord_user_id", discord_user_id).execute()
    if result.data:
        return result.data[0]["wallet_address"]
    return None


def unlink_wallet(discord_user_id: str) -> bool:
    """Remove a wallet link."""
    db = get_supabase()
    db.table("wallets").delete().eq("discord_user_id", discord_user_id).execute()
    return True


# ─────────────────────────────────────────────
# BRACKET MANAGEMENT
# ─────────────────────────────────────────────

def register_for_bracket(discord_user_id: str, asset_id: int, bracket_id: str) -> dict:
    """
    Register a player for the current bracket session.
    bracket_id = "morning_YYYY-MM-DD" or "evening_YYYY-MM-DD"
    """
    db = get_supabase()
    data = {
        "discord_user_id": discord_user_id,
        "asset_id":        asset_id,
        "bracket_id":      bracket_id,
        "registered_at":   datetime.now(timezone.utc).isoformat(),
        "status":          "registered",
    }
    result = db.table("bracket_entries").upsert(
        data, on_conflict="discord_user_id,bracket_id"
    ).execute()
    return result.data


def get_bracket_entries(bracket_id: str) -> list:
    """Get all registered players for a bracket."""
    db = get_supabase()
    result = db.table("bracket_entries").select("*").eq("bracket_id", bracket_id).execute()
    return result.data or []


def is_registered(discord_user_id: str, bracket_id: str) -> bool:
    """Check if a player is already registered for a bracket."""
    db = get_supabase()
    result = (
        db.table("bracket_entries")
        .select("discord_user_id")
        .eq("discord_user_id", discord_user_id)
        .eq("bracket_id", bracket_id)
        .execute()
    )
    return len(result.data) > 0


def close_registration(bracket_id: str) -> int:
    """Mark bracket registration as closed. Returns entry count."""
    entries = get_bracket_entries(bracket_id)
    return len(entries)


# ─────────────────────────────────────────────
# BATTLE RESULTS
# ─────────────────────────────────────────────

def save_battle_result(
    bracket_id: str,
    winner_discord_id: str,
    loser_discord_id: str,
    winner_asset_id: int,
    loser_asset_id: int,
    is_upset: bool,
    round_num: int,  # Which round of the bracket (R16, QF, SF, Final)
) -> dict:
    """Save a battle result to the database."""
    db = get_supabase()
    data = {
        "bracket_id":        bracket_id,
        "winner_discord_id": winner_discord_id,
        "loser_discord_id":  loser_discord_id,
        "winner_asset_id":   winner_asset_id,
        "loser_asset_id":    loser_asset_id,
        "is_upset":          is_upset,
        "bracket_round":     round_num,
        "played_at":         datetime.now(timezone.utc).isoformat(),
    }
    result = db.table("battles").insert(data).execute()
    return result.data


# ─────────────────────────────────────────────
# CLASH POINTS (CP) + LEADERBOARD
# ─────────────────────────────────────────────

CP_WIN           = 120
CP_LOSS          =  30   # Participation CP
CP_UPSET_BONUS   =  40
CP_BRACKET_WIN   = 200   # Winning the full bracket

def award_cp(discord_user_id: str, amount: int, reason: str) -> dict:
    """
    Award Clash Points to a player.
    Upserts their total — adds to existing CP.
    """
    db = get_supabase()

    # Get current CP
    existing = db.table("leaderboard").select("cp_total").eq("discord_user_id", discord_user_id).execute()
    current_cp = existing.data[0]["cp_total"] if existing.data else 0

    new_cp = current_cp + amount
    data = {
        "discord_user_id": discord_user_id,
        "cp_total":        new_cp,
        "updated_at":      datetime.now(timezone.utc).isoformat(),
    }
    result = db.table("leaderboard").upsert(data, on_conflict="discord_user_id").execute()

    # Log the CP transaction
    db.table("cp_log").insert({
        "discord_user_id": discord_user_id,
        "amount":          amount,
        "reason":          reason,
        "logged_at":       datetime.now(timezone.utc).isoformat(),
    }).execute()

    return {"discord_user_id": discord_user_id, "cp_awarded": amount, "new_total": new_cp}


def get_leaderboard(limit: int = 10) -> list:
    """Get top players by CP."""
    db = get_supabase()
    result = (
        db.table("leaderboard")
        .select("*")
        .order("cp_total", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def get_player_rank(discord_user_id: str) -> dict:
    """Get a player's CP and rank."""
    db = get_supabase()
    player = db.table("leaderboard").select("cp_total").eq("discord_user_id", discord_user_id).execute()
    if not player.data:
        return {"discord_user_id": discord_user_id, "cp_total": 0, "rank": None}

    cp = player.data[0]["cp_total"]
    # Count players with more CP
    higher = db.table("leaderboard").select("discord_user_id", count="exact").gt("cp_total", cp).execute()
    rank = (higher.count or 0) + 1
    return {"discord_user_id": discord_user_id, "cp_total": cp, "rank": rank}


# ─────────────────────────────────────────────
# WIN STREAKS
# ─────────────────────────────────────────────

STREAK_REWARDS = {
    3:  {"cp": 50,  "role": "On a Roll 🔥"},
    7:  {"cp": 200, "role": "Charged Up ⚡"},
    14: {"cp": 0,   "role": "Veteran Zappy 🏆"},
    30: {"cp": 0,   "role": "Hall of Fame ⭐"},
}

def update_streak(discord_user_id: str, won: bool) -> dict:
    """
    Update a player's daily participation streak.
    Streak = consecutive days played (both AM + PM sessions).
    Returns streak info and any rewards earned.
    """
    db = get_supabase()
    today = datetime.now(timezone.utc).date().isoformat()

    existing = db.table("streaks").select("*").eq("discord_user_id", discord_user_id).execute()

    if not existing.data:
        # New player
        streak_data = {
            "discord_user_id":  discord_user_id,
            "current_streak":   1 if won else 0,
            "longest_streak":   1 if won else 0,
            "last_played_date": today,
            "total_wins":       1 if won else 0,
            "total_played":     1,
        }
        db.table("streaks").insert(streak_data).execute()
        return {"streak": streak_data["current_streak"], "rewards": []}

    streak = existing.data[0]
    last_played = streak.get("last_played_date", "")
    rewards = []

    # Check if they played yesterday (streak continues) or today already (no change)
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    if last_played == today:
        # Already played today — just update win count if needed
        if won:
            db.table("streaks").update({
                "total_wins": streak["total_wins"] + 1,
            }).eq("discord_user_id", discord_user_id).execute()
        return {"streak": streak["current_streak"], "rewards": []}

    elif last_played == yesterday:
        # Streak continues
        new_streak = streak["current_streak"] + 1
    else:
        # Streak broken
        new_streak = 1

    longest = max(new_streak, streak.get("longest_streak", 0))

    # Check for streak milestone rewards
    if new_streak in STREAK_REWARDS:
        reward = STREAK_REWARDS[new_streak]
        rewards.append({
            "days": new_streak,
            "cp_bonus": reward["cp"],
            "role": reward["role"],
        })
        if reward["cp"] > 0:
            award_cp(discord_user_id, reward["cp"], f"streak_{new_streak}_days")

    # Save updated streak
    db.table("streaks").update({
        "current_streak":   new_streak,
        "longest_streak":   longest,
        "last_played_date": today,
        "total_wins":       streak["total_wins"] + (1 if won else 0),
        "total_played":     streak["total_played"] + 1,
    }).eq("discord_user_id", discord_user_id).execute()

    return {"streak": new_streak, "rewards": rewards, "longest": longest}


def get_streak(discord_user_id: str) -> dict:
    """Get a player's current streak info."""
    db = get_supabase()
    result = db.table("streaks").select("*").eq("discord_user_id", discord_user_id).execute()
    if result.data:
        return result.data[0]
    return {"current_streak": 0, "total_wins": 0, "total_played": 0}


# ─────────────────────────────────────────────
# BRACKET SEEDING
# ─────────────────────────────────────────────

def seed_bracket(entries: list) -> list:
    """
    Seed the bracket by CP ranking (higher CP gets better seed).
    Returns list of matchups: [(player_a, player_b), ...]
    
    With n players, byes_needed = next_power_of_2(n) - n.
    Top seeds get the byes. Remaining players all fight in round 1.
    This ensures only the minimum byes are given and they don't cascade.
    """
    import random

    # Sort by CP descending
    ranked = []
    db = get_supabase()
    for entry in entries:
        cp_data = db.table("leaderboard").select("cp_total").eq(
            "discord_user_id", entry["discord_user_id"]
        ).execute()
        cp = cp_data.data[0]["cp_total"] if cp_data.data else 0
        ranked.append({**entry, "cp": cp})

    ranked.sort(key=lambda x: x["cp"], reverse=True)

    n = len(ranked)
    if n < 2:
        return []

    # Find next power of 2
    next_pow2 = 1
    while next_pow2 < n:
        next_pow2 *= 2

    byes_needed = next_pow2 - n

    # Top seeds get byes (they advance automatically)
    bye_players   = ranked[:byes_needed]
    fight_players = ranked[byes_needed:]

    # Pair up the fighting players: highest vs lowest seed
    matchups = []
    lo, hi = 0, len(fight_players) - 1
    while lo < hi:
        matchups.append((fight_players[lo], fight_players[hi]))
        lo += 1
        hi -= 1

    # Add bye matchups at end (they'll be processed as byes in the scheduler)
    for player in bye_players:
        matchups.append((player, None))

    return matchups

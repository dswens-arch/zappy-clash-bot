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

def register_for_bracket(
    discord_user_id: str,
    asset_id: int,
    bracket_id: str,
    spark_asa: int | None = None,
    spark_type: str | None = None,
    spark_tier: int = 0,
) -> dict:
    """
    Register a player for the current bracket session.
    bracket_id = "morning_YYYY-MM-DD" or "evening_YYYY-MM-DD"
    Optionally stores the Spark companion equipped for this entry.
    """
    db = get_supabase()
    data = {
        "discord_user_id": discord_user_id,
        "asset_id":        asset_id,
        "bracket_id":      bracket_id,
        "registered_at":   datetime.now(timezone.utc).isoformat(),
        "status":          "registered",
        "spark_asa":       spark_asa,
        "spark_type":      spark_type,
        "spark_tier":      spark_tier,
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
    round_num: int,
    is_champion: bool = False,
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

    # Keep zappy_records in sync
    update_zappy_record(winner_asset_id, loser_asset_id, is_champion=is_champion, champion_asset_id=winner_asset_id)

    return result.data


def update_zappy_record(
    winner_asset_id: int,
    loser_asset_id: int,
    is_champion: bool = False,
    champion_asset_id: int | None = None,
    champ_only: bool = False,
) -> None:
    """
    Upsert win/loss counts in zappy_records.
    - champ_only=True: only increment champ_wins for winner_asset_id, no win/loss changes.
    - is_champion=True: also increment champ_wins alongside win/loss update.
    """
    db = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    def _increment(asset_id: int, win: bool = False, loss: bool = False, champ: bool = False):
        existing = db.table("zappy_records").select("*").eq("asset_id", asset_id).execute()
        if existing.data:
            row = existing.data[0]
            db.table("zappy_records").update({
                "wins":       row["wins"]       + (1 if win   else 0),
                "losses":     row["losses"]     + (1 if loss  else 0),
                "champ_wins": row["champ_wins"] + (1 if champ else 0),
                "updated_at": now,
            }).eq("asset_id", asset_id).execute()
        else:
            db.table("zappy_records").insert({
                "asset_id":   asset_id,
                "wins":       1 if win   else 0,
                "losses":     1 if loss  else 0,
                "champ_wins": 1 if champ else 0,
                "updated_at": now,
            }).execute()

    if champ_only:
        # Just add champion win — wins/losses already recorded from the battle
        _increment(winner_asset_id, champ=True)
        return

    _increment(winner_asset_id, win=True,  champ=is_champion)
    _increment(loser_asset_id,  loss=True, champ=False)


def get_zappy_record(asset_id: int) -> dict:
    """Single lookup for a Zappy's record. Returns wins, losses, champ_wins."""
    try:
        db = get_supabase()
        result = db.table("zappy_records").select("wins,losses,champ_wins").eq("asset_id", asset_id).execute()
        if result.data:
            return result.data[0]
    except Exception as e:
        print(f"[db] get_zappy_record failed: {e}")
    return {"wins": 0, "losses": 0, "champ_wins": 0}


# ─────────────────────────────────────────────
# CLASH POINTS (CP) + LEADERBOARD
# ─────────────────────────────────────────────

CP_WIN           = 120
CP_LOSS          =  30   # Participation CP
CP_UPSET_BONUS   =  40
CP_BRACKET_WIN   = 200   # Winning the full bracket

def award_cp(discord_user_id: str, amount: int, reason: str, retries: int = 3, delay: float = 2.0) -> dict:
    """
    Award Clash Points to a player.
    Upserts their total — adds to existing CP.
    Retries on transient Supabase errors (e.g. 502 Bad Gateway).
    """
    import time

    last_error = None
    for attempt in range(retries):
        try:
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

        except Exception as e:
            last_error = e
            print(f"[WARN] award_cp attempt {attempt + 1}/{retries} failed for {discord_user_id}: {e}")
            if attempt < retries - 1:
                time.sleep(delay)

    raise RuntimeError(f"award_cp failed after {retries} attempts for {discord_user_id}: {last_error}")


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


# ─────────────────────────────────────────────
# SPARK XP + TIER UPGRADES
# ─────────────────────────────────────────────

SPARK_XP_PARTICIPATE = 50
SPARK_XP_WIN         = 50   # Bonus on top of participation XP
SPARK_TIER_THRESHOLDS = {1: 1000, 2: 5000}
SPARK_TIER_NAMES      = {1: "Spark", 2: "Flare", 3: "Blaze"}

# T2 and T3 image CIDs per type — fill in after uploading to Pinata
# Format: { "zolt": {"2": "bafk...", "3": "bafk..."}, ... }
# Metadata JSON CIDs (uploaded to Pinata) — these become the reserve address
SPARK_UPGRADE_CIDS: dict = {
    "zolt":   {"2": "bafkreiczc6gmqqyy4kwdgjfv7qlktde2so2gxzgk6rr3kxnjaggrenirsy", "3": "bafkreigl5nmfxsit5ctqmigk3rgt4zsaq243uv5u3zypg72nv4jmj3d6vy"},
    "scorch": {"2": "bafkreifjxt736tgqcll5r3f3m6qbimkx7fwkojvyejfgrdzzklpi2mfluu", "3": "bafkreigwh2hrzm3k6dsjt4lef2i6j6a2cneu34h6ywkvmnfjbyjmpbkcsu"},
    "jinx":   {"2": "bafkreic6ek5tslf4cjvv7wjbfw5epkzsswioggbawnvi6uhvxhvhulb3li",  "3": "bafkreifcl24y75bkpo4q2osirjdlnpq3l7c3s7xf4szwb4we6bukzfo2z4"},
    "moss":   {"2": "bafkreidwthf72n2dagc7vrzo3laxzuya5lu5ovuu2begclzadf2xu7mlxm",  "3": "bafkreigsvmjhbgvfm7wr3c2aupkosh4kq4yyuj6xzsgebiq7gg3ljzsnpi"},
    "glitch": {"2": "bafkreifaogqn6zax4egxq6kemn5i4vcn6gfmjv4tetcaxbmd7gawyacuua", "3": "bafkreietobff4pf2xy4ga5lrkrrgsnv63vwtnm4i25lqf3ctftkxjhigui"},
    "null":   {"2": "bafkreihicpgj6xxhmskioo3hxsh3d3o4pigdz25htg5zeab2va6dfmzaqu", "3": "bafkreie7et7fyp64qdfnpfb7eezdjrvhluvbyvopys7xrtr4hbcqfcmsoi"},
}

# Pre-computed reserve addresses from metadata CIDs (avoids runtime CID math)
SPARK_UPGRADE_RESERVES: dict = {
    "zolt":   {"2": "LELYZSCDDDRKYMZEWX6BNKMMTKJ3I27EZL2GHNK5VEAY2ERVCGLDTDJFME", "3": "ZPVVQW6JCPUKOBRAZLOE2PTGICDLTOSXWTPHB437JWXRFRHMP2XNMBGRTY"},
    "scorch": {"2": "VG6P7P2M2AJNPWHMXNT2AFBRK74WZJZGXAREU2EPHFJN5DJQVOSSQSCR4E", "3": "2Y7I6HFTNLYOJGPRMQXJDZHYDIJUSTPQ73CZKVRUVEHBFR4FIKKQFRQVJ4"},
    "jinx":   {"2": "LYRLWOJMXQJGWX6ZEEW3UR5LGKKZBYYYECZWVD2Q6W46U6RMHNNHW7EYJE", "3": "UJPLTD7UFJ53SDJ2JCFENNV6DNP4LOL64XSLGYHSYTYGRLEV3LH4RELLZU"},
    "moss":   {"2": "O2M4X7JXIMAYL6WHF3NMC7GTADVOTV2WSTIEQYJPEAMXK6T5RO53HEZQCQ", "3": "2KVRE4E2UVT62HMLICR5J2I7RKDTDCRH27GIYQFCD4Y3NNHGJV5DVYDLWA"},
    "glitch": {"2": "UBY2BX3EC7QQ26DZIRRXVDSUJXYYVRGXSMSMIC4FQP4YC3AAKSQP5V7BLQ",  "3": "SNYEUXR4XK7DQYDVOFKGE2JWX3OW2NVTRDLVOAXMKMWNK5E5A2RP2OB5AU"},
    "null":   {"2": "5AJ4ZH2645SJJBZ3M66I7MPN3R5AYPHLU6M3XEQAHKUDYMVTECC4RADMKE", "3": "T4SP4XB73SAMVV4UH4QTENGGU5OSUHCVZ7CL66GOHQ4EKAUJSJZCUSBYLU"},
}


def get_spark(asset_id: int) -> dict | None:
    """Fetch a single Spark record by ASA ID."""
    db = get_supabase()
    result = db.table("spark_holdings").select("*").eq("asset_id", asset_id).execute()
    return result.data[0] if result.data else None


def get_sparks_for_wallet(wallet: str) -> list:
    """Return all Sparks owned by a wallet."""
    db = get_supabase()
    result = (
        db.table("spark_holdings")
        .select("asset_id, name, spark_type, tier, xp")
        .eq("wallet", wallet)
        .execute()
    )
    return result.data or []


def award_spark_xp(asset_id: int, won: bool) -> dict:
    """
    Award XP to a Spark after a Clash event.
    Returns dict with xp_gained, new_xp, tier_before, tier_after, upgraded.
    """
    db   = get_supabase()
    spark = get_spark(asset_id)
    if not spark:
        return {}

    xp_gain  = SPARK_XP_PARTICIPATE + (SPARK_XP_WIN if won else 0)
    new_xp   = spark["xp"] + xp_gain
    old_tier = spark["tier"]
    new_tier = old_tier

    # Check for tier upgrade
    if old_tier < 3 and new_xp >= SPARK_TIER_THRESHOLDS.get(old_tier, 999999):
        new_tier = old_tier + 1

    update_data = {"xp": new_xp}
    if new_tier != old_tier:
        update_data["tier"]        = new_tier
        update_data["upgraded_at"] = datetime.now(timezone.utc).isoformat()

    db.table("spark_holdings").update(update_data).eq("asset_id", asset_id).execute()

    return {
        "asset_id":   asset_id,
        "spark_type": spark["spark_type"],
        "name":       spark["name"],
        "xp_gained":  xp_gain,
        "new_xp":     new_xp,
        "tier_before": old_tier,
        "tier_after":  new_tier,
        "upgraded":    new_tier != old_tier,
        "wallet":      spark.get("wallet"),
        "discord_user_id": spark.get("discord_user_id"),
    }


def push_spark_arc19_upgrade(asset_id: int, spark_type: str, new_tier: int) -> bool:
    """
    Push an ARC-19 metadata update to upgrade a Spark NFT on-chain.
    Uses pre-computed reserve addresses from metadata CIDs.
    Returns True on success, False on failure.

    Requires env vars:
        SPARK_MANAGER_MNEMONIC — mnemonic for the Spark creator/manager wallet
        ALGOD_TOKEN            — AlgoNode API token (X-Algo-API-Token header)
    """
    try:
        from algosdk import mnemonic, account, encoding as algo_encoding
        from algosdk.v2client import algod
        from algosdk.transaction import AssetConfigTxn, wait_for_confirmation

        # ── Look up pre-computed reserve address ──────────────────────────
        new_reserve = SPARK_UPGRADE_RESERVES.get(spark_type, {}).get(str(new_tier), "")
        if not new_reserve:
            print(f"[SPARK] No upgrade reserve configured for {spark_type} T{new_tier}")
            return False

        # ── Connect to Algorand ───────────────────────────────────────────
        algod_token   = os.environ.get("ALGOD_TOKEN", "")
        algod_address = "https://mainnet-api.algonode.cloud"
        headers       = {"X-Algo-API-Token": algod_token} if algod_token else {}
        algod_client  = algod.AlgodClient(algod_token, algod_address, headers=headers)

        # ── Sign with manager wallet ──────────────────────────────────────
        manager_mnemonic = os.environ["SPARK_MANAGER_MNEMONIC"]
        private_key      = mnemonic.to_private_key(manager_mnemonic)
        manager_address  = account.address_from_private_key(private_key)

        # Fetch current asset params to preserve manager/freeze/clawback
        asset_info = algod_client.asset_info(asset_id)
        params     = asset_info["params"]

        sp  = algod_client.suggested_params()
        txn = AssetConfigTxn(
            sender   = manager_address,
            sp       = sp,
            index    = asset_id,
            manager  = manager_address,
            reserve  = new_reserve,
            freeze   = params.get("freeze"),
            clawback = params.get("clawback"),
            strict_empty_address_check = False,
        )
        signed = txn.sign(private_key)
        tx_id  = algod_client.send_transaction(signed)
        wait_for_confirmation(algod_client, tx_id, 4)

        # Update reserve in Supabase to reflect new on-chain state
        db = get_supabase()
        db.table("spark_holdings").update({
            "reserve_address": new_reserve
        }).eq("asset_id", asset_id).execute()

        print(f"[SPARK] ARC-19 upgrade complete — ASA {asset_id} → T{new_tier} | tx {tx_id}")
        return True

    except Exception as e:
        print(f"[SPARK] ARC-19 upgrade failed for ASA {asset_id}: {e}")
        return False


# ─────────────────────────────────────────────
# SPARK JOBS — DAILY WORK SYSTEM
# ─────────────────────────────────────────────
# spark_job_log is the source of truth (same principle as gp_transactions —
# reconstructable, never silently skipped even if a payout leg fails).
# `paid` tracks whether the payout leg (ALGO transfer or NFT send) for a
# resolved row has gone out yet, so a failed transfer can be retried without
# re-rolling or re-resolving the row.

JOB_WALLET_TRANSFER_COOLDOWN_HOURS = 24
JOB_SAME_SPARK_COOLDOWN_HOURS      = 24
JOB_DURATION_HOURS                 = 8


def get_eligible_sparks_for_job(wallet: str) -> dict:
    """
    Returns {"eligible": [...], "skipped": {asset_id: reason}} for a wallet.
    reason is one of: "wallet_transfer_cooldown", "already_working", "already_paid_today"
    """
    db = get_supabase()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=JOB_WALLET_TRANSFER_COOLDOWN_HOURS)).isoformat()

    holdings = (
        db.table("spark_holdings")
        .select("asset_id, name, spark_type, tier, wallet, discord_user_id, purchased_at")
        .eq("wallet", wallet)
        .execute()
        .data or []
    )

    eligible, skipped = [], {}
    for h in holdings:
        asa = h["asset_id"]

        if h.get("purchased_at") and h["purchased_at"] > cutoff_iso:
            skipped[asa] = "wallet_transfer_cooldown"
            continue

        last = (
            db.table("spark_job_log")
            .select("status, clock_in_at")
            .eq("spark_asa", asa)
            .order("clock_in_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if last:
            row = last[0]
            if row["status"] == "working":
                skipped[asa] = "already_working"
                continue
            if row["status"] == "complete" and row["clock_in_at"] > cutoff_iso:
                skipped[asa] = "already_paid_today"
                continue

        eligible.append(h)

    return {"eligible": eligible, "skipped": skipped}


def create_spark_job(spark: dict, job: str, flavor_line: str) -> dict:
    """Clock a Spark in. Writes a 'working' row with resolve_at = now + 8h."""
    db = get_supabase()
    now = datetime.now(timezone.utc)
    data = {
        "spark_asa":       spark["asset_id"],
        "wallet":          spark["wallet"],
        "discord_user_id": spark.get("discord_user_id"),
        "spark_type":      spark["spark_type"],
        "spark_tier":      spark["tier"],
        "spark_name":      spark.get("name"),
        "job":             job,
        "status":          "working",
        "clock_in_at":     now.isoformat(),
        "resolve_at":      (now + timedelta(hours=JOB_DURATION_HOURS)).isoformat(),
        "flavor_line":     flavor_line,
    }
    result = db.table("spark_job_log").insert(data).execute()
    return result.data[0] if result.data else {}


def get_due_jobs() -> list:
    """Jobs whose 8-hour timer has elapsed and are ready to resolve."""
    db = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    result = (
        db.table("spark_job_log")
        .select("*")
        .eq("status", "working")
        .lte("resolve_at", now_iso)
        .execute()
    )
    return result.data or []


def get_working_job_by_spark(spark_asa: int) -> dict | None:
    """Fetch a Spark's current in-progress shift regardless of resolve_at — used by /spark-job-force-resolve."""
    db = get_supabase()
    result = (
        db.table("spark_job_log")
        .select("*")
        .eq("spark_asa", spark_asa)
        .eq("status", "working")
        .order("clock_in_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def complete_job(job_id: int, outcome: str, amount: float | None, nft_asa: int | None, flavor_line: str) -> None:
    """Resolve a due job. outcome is 'miss' | 'algo' | 'nft'."""
    db = get_supabase()
    db.table("spark_job_log").update({
        "status":      "complete",
        "outcome":     outcome,
        "amount":      amount,
        "nft_asa":     nft_asa,
        "flavor_line": flavor_line,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()


def get_unpaid_algo_jobs() -> list:
    """Completed jobs with an ALGO hit that haven't been paid out yet."""
    db = get_supabase()
    result = (
        db.table("spark_job_log")
        .select("*")
        .eq("status", "complete")
        .eq("paid", False)
        .gt("amount", 0)
        .execute()
    )
    return result.data or []


def get_unpaid_nft_jobs() -> list:
    """Completed jobs with an NFT hit that haven't been sent yet."""
    db = get_supabase()
    result = (
        db.table("spark_job_log")
        .select("*")
        .eq("status", "complete")
        .eq("paid", False)
        .not_.is_("nft_asa", "null")
        .execute()
    )
    return result.data or []


def mark_jobs_paid(job_ids: list) -> None:
    if not job_ids:
        return
    db = get_supabase()
    db.table("spark_job_log").update({"paid": True}).in_("id", job_ids).execute()


def create_job_payout(wallet: str, total_algo: float, spark_count: int, job_log_ids: list, tx_id: str | None) -> dict:
    db = get_supabase()
    data = {
        "wallet":      wallet,
        "total_algo":  total_algo,
        "spark_count": spark_count,
        "job_log_ids": job_log_ids,
        "tx_id":       tx_id,
    }
    result = db.table("spark_job_payouts").insert(data).execute()
    return result.data[0] if result.data else {}


# ── NFT prizes for Spark Jobs draw from the reward wallet's live on-chain
#    inventory (via nft_rewards.pick_random_nft()) — no tagging/reservation
#    table needed. Once an NFT is sent out, it naturally drops out of the
#    wallet's balance and won't be picked again.


# ── Flat per-shift XP (separate from Clash's award_spark_xp, which uses
#    Clash-sized 50/100 values — Jobs XP is a fixed, smaller amount per shift) ──

SPARK_JOB_XP_PER_SHIFT = 5


def award_spark_job_xp(asset_id: int) -> dict:
    """Award flat XP for completing a Spark Job shift, win or miss. Same tier-upgrade check as Clash XP."""
    db = get_supabase()
    spark = get_spark(asset_id)
    if not spark:
        return {}

    xp_gain  = SPARK_JOB_XP_PER_SHIFT
    new_xp   = spark["xp"] + xp_gain
    old_tier = spark["tier"]
    new_tier = old_tier

    if old_tier < 3 and new_xp >= SPARK_TIER_THRESHOLDS.get(old_tier, 999999):
        new_tier = old_tier + 1

    update_data = {"xp": new_xp}
    if new_tier != old_tier:
        update_data["tier"]        = new_tier
        update_data["upgraded_at"] = datetime.now(timezone.utc).isoformat()

    db.table("spark_holdings").update(update_data).eq("asset_id", asset_id).execute()

    return {
        "asset_id":    asset_id,
        "spark_type":  spark["spark_type"],
        "name":        spark["name"],
        "xp_gained":   xp_gain,
        "new_xp":      new_xp,
        "tier_before": old_tier,
        "tier_after":  new_tier,
        "upgraded":    new_tier != old_tier,
    }

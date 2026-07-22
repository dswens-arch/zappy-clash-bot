"""
database.py
-----------
All database operations using Supabase (Postgres).
Handles: wallet links, CP scores, win streaks, brackets, leaderboard.

You will paste your SUPABASE_URL and SUPABASE_KEY into .env
"""

import os
import asyncio
import time
from datetime import datetime, timezone, timedelta, time as dt_time
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
JOB_DURATION_HOURS                 = 8


def get_eligible_sparks_for_job(wallet: str) -> dict:
    """
    Returns {"eligible": [...], "skipped": {asset_id: reason}} for a wallet.
    reason is one of: "wallet_transfer_cooldown", "already_working", "office_seat"

    No daily cooldown — a Spark is eligible again the moment its current
    shift resolves. The 8-hour shift duration itself is the natural pacing;
    the other gates are the wallet-transfer anti-farming check and Office
    seat exclusivity — a promoted Spark works Office shifts only, never
    both at once.
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

    office_seated = {
        s["spark_asa"] for s in
        db.table("spark_office_seats").select("spark_asa").eq("wallet", wallet).execute().data or []
    }

    eligible, skipped = [], {}
    for h in holdings:
        asa = h["asset_id"]

        if asa in office_seated:
            skipped[asa] = "office_seat"
            continue

        if h.get("purchased_at") and h["purchased_at"] > cutoff_iso:
            skipped[asa] = "wallet_transfer_cooldown"
            continue

        last = (
            db.table("spark_job_log")
            .select("status")
            .eq("spark_asa", asa)
            .order("clock_in_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        if last and last[0]["status"] == "working":
            skipped[asa] = "already_working"
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


def get_weekly_luck_leaders(limit: int = 3) -> list:
    """
    Top Sparks by hit count (ALGO or NFT) over the trailing 7 days —
    powers the weekly shoutout. Rolling window, not a fixed calendar week,
    so it works no matter what day the shoutout actually posts on.
    """
    db = get_supabase()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = (
        db.table("spark_job_log")
        .select("spark_asa, spark_name, spark_type, wallet, discord_user_id, outcome")
        .eq("status", "complete")
        .in_("outcome", ["algo", "nft"])
        .gte("clock_in_at", cutoff_iso)
        .execute()
        .data or []
    )

    tallies: dict[int, dict] = {}
    for r in rows:
        asa = r["spark_asa"]
        if asa not in tallies:
            tallies[asa] = {
                "spark_asa": asa,
                "spark_name": r.get("spark_name") or r.get("spark_type"),
                "wallet": r["wallet"],
                "discord_user_id": r.get("discord_user_id"),
                "hits": 0,
            }
        tallies[asa]["hits"] += 1

    leaders = sorted(tallies.values(), key=lambda t: t["hits"], reverse=True)
    return leaders[:limit]


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


# ─────────────────────────────────────────────
# SPARK OFFICE — PROMOTION SYSTEM
# (append this whole block to the end of database.py — after
#  award_spark_job_xp, which it reuses for shift XP)
# ─────────────────────────────────────────────
# spark_office_log is the source of truth for Office shifts (same principle
# as spark_job_log — reconstructable, never silently skipped). Promotion
# is deliberately luck-gated, not effort-gated: eligibility and demotion
# both key off HIT outcomes, not raw shift volume, so grinding alone can't
# force your way in or keep a seat warm.

OFFICE_MIN_SHIFTS          = 20   # floor — need this many completed base shifts before checking hits at all
OFFICE_WINDOW_SHIFTS       = 40   # trailing window checked for the hit requirement
OFFICE_HITS_NEEDED         = 3    # hits required inside that window
OFFICE_SEAT_CAP            = 20
OFFICE_SPONSOR_ZAPPY_COUNT = 5    # one-time check at promotion, not locked afterward

OFFICE_SHIFT_DURATION_HOURS   = 8    # same shift length as base Jobs
OFFICE_DAILY_COOLDOWN_HOURS   = 24   # only 1 shift/day, unlike base (no cooldown)
OFFICE_NO_SHOW_GRACE_HOURS    = 4    # window after next_shift_due_at before it's a no-show
OFFICE_DEMOTION_MISS_DAYS     = 7    # consecutive misses (~days, since 1 shift/day) before demotion
OFFICE_MIN_SHIFTS_FOR_DUEL    = 5    # a seat must have this many Office shifts before it's duel-eligible
OFFICE_DUEL_SUBMIT_HOURS      = 1    # window to submit picks before the bot auto-rolls for you

# Office odds/payouts — a genuine bump over base Jobs (base: ALGO_HIT_CHANCE /
# NFT_HIT_CHANCE / PAYOUT_RANGE in spark_jobs.py), not just cushier flavor text.
OFFICE_ALGO_HIT_CHANCE = {1: 0.070, 2: 0.080, 3: 0.090}
OFFICE_NFT_HIT_CHANCE  = {1: 0.0060, 2: 0.0070, 3: 0.0080}
OFFICE_PAYOUT_RANGE    = {1: (0.2, 0.6), 2: (0.35, 1.0), 3: (0.5, 1.5)}
OFFICE_MAX_SHIFT_PAYOUT = 1.5

# High-stakes gamble — trade the safe daily roll for much lower odds and a
# much bigger payout. Multipliers apply on top of the normal tables above.
OFFICE_GAMBLE_HIT_CHANCE_MULT = 0.5   # half the normal ALGO hit chance
OFFICE_GAMBLE_NFT_CHANCE_MULT = 2.0   # double the NFT shot — the "big win" flavor
OFFICE_GAMBLE_PAYOUT_MULT     = 3.0   # triple the payout range (and cap)

# Office-wide events — periodic surprise that boosts EVERY seat's odds at
# once, independent of any one person's roll. At most one active at a time.
OFFICE_EVENT_DURATION_HOURS = 24  # spans a full day so every fixed shift-time anchor gets one shot at it


def get_active_office_event() -> dict | None:
    """Returns the current office-wide event if one is active, else None."""
    db = get_supabase()
    row = db.table("spark_office_events").select("*").eq("id", 1).single().execute().data
    if not row or not row.get("expires_at"):
        return None
    expires = datetime.fromisoformat(row["expires_at"])
    if datetime.now(timezone.utc) >= expires:
        return None
    return row


def set_office_event(name: str, bonus_algo_pct: float, bonus_nft_pct: float, hours: int = OFFICE_EVENT_DURATION_HOURS) -> None:
    db = get_supabase()
    expires = datetime.now(timezone.utc) + timedelta(hours=hours)
    db.table("spark_office_events").upsert({
        "id":             1,
        "event_name":     name,
        "bonus_algo_pct": bonus_algo_pct,
        "bonus_nft_pct":  bonus_nft_pct,
        "expires_at":     expires.isoformat(),
        "created_at":     datetime.now(timezone.utc).isoformat(),
    }).execute()


def get_office_seat_count() -> int:
    db = get_supabase()
    result = db.table("spark_office_seats").select("id", count="exact").eq("status", "active").execute()
    return result.count or 0


def get_office_seats() -> list:
    """All active Office seats, lowest hit-rate first (for duel targeting display)."""
    db = get_supabase()
    result = db.table("spark_office_seats").select("*").eq("status", "active").execute()
    seats = result.data or []
    seats.sort(key=lambda s: (s["hits"] / s["shifts_completed"]) if s["shifts_completed"] else 0.0)
    return seats


def get_office_seat(spark_asa: int) -> dict | None:
    db = get_supabase()
    result = db.table("spark_office_seats").select("*").eq("spark_asa", spark_asa).execute()
    return result.data[0] if result.data else None


def get_lowest_hitrate_seat() -> dict | None:
    """
    Duel target: lowest lifetime hit rate among seats with enough Office
    shifts to be judged fairly (OFFICE_MIN_SHIFTS_FOR_DUEL floor). Excludes
    any seat already mid-duel.
    """
    db = get_supabase()
    result = (
        db.table("spark_office_seats")
        .select("*")
        .eq("status", "active")
        .gte("shifts_completed", OFFICE_MIN_SHIFTS_FOR_DUEL)
        .execute()
    )
    seats = result.data or []
    if not seats:
        return None
    seats.sort(key=lambda s: s["hits"] / s["shifts_completed"])
    return seats[0]


def check_office_eligibility(asset_id: int) -> dict:
    """
    Returns {"eligible": bool, "reason": str, "shifts_seen": int, "hits_seen": int}.
    Pulls the most recent OFFICE_WINDOW_SHIFTS completed base-Jobs rows for
    this Spark. Since rows come back newest-first and capped at the window
    size, a returned count below OFFICE_MIN_SHIFTS on its own means the
    floor isn't met yet — no separate count query needed.
    """
    db = get_supabase()
    rows = (
        db.table("spark_job_log")
        .select("outcome")
        .eq("spark_asa", asset_id)
        .eq("status", "complete")
        .order("clock_in_at", desc=True)
        .limit(OFFICE_WINDOW_SHIFTS)
        .execute()
        .data or []
    )
    shifts_seen = len(rows)
    hits_seen   = sum(1 for r in rows if r.get("outcome") in ("algo", "nft"))

    if shifts_seen < OFFICE_MIN_SHIFTS:
        return {"eligible": False, "reason": "not_enough_shifts", "shifts_seen": shifts_seen, "hits_seen": hits_seen}
    if hits_seen < OFFICE_HITS_NEEDED:
        return {"eligible": False, "reason": "not_lucky_enough", "shifts_seen": shifts_seen, "hits_seen": hits_seen}
    return {"eligible": True, "reason": "eligible", "shifts_seen": shifts_seen, "hits_seen": hits_seen}


def get_all_office_candidates() -> list:
    """
    Every Spark, across every wallet, currently eligible for Office
    promotion and not already seated — ranked luckiest-first (most hits in
    its trailing window). Used by the twice-daily auto-promotion sweep.

    NOTE: this runs check_office_eligibility() (one query each) across all
    of spark_holdings. Fine at current scale (~100 Sparks); if the
    collection grows a lot, this is the first place to optimize (e.g. a
    materialized hits-in-window column maintained by complete_job instead
    of computed fresh each sweep).
    """
    db = get_supabase()
    seated = {
        s["spark_asa"] for s in
        db.table("spark_office_seats").select("spark_asa").execute().data or []
    }
    holdings = (
        db.table("spark_holdings")
        .select("asset_id, name, spark_type, tier, wallet, discord_user_id")
        .execute()
        .data or []
    )

    candidates = []
    for h in holdings:
        asa = h["asset_id"]
        if asa in seated:
            continue
        check = check_office_eligibility(asa)
        if check["eligible"]:
            candidates.append({**h, "hits_seen": check["hits_seen"]})

    candidates.sort(key=lambda c: c["hits_seen"], reverse=True)
    return candidates


def set_office_shift_time(spark_asa: int, hour: int, minute: int = 0) -> dict | None:
    """
    Set or change a seat's fixed daily shift-time anchor. Recomputes
    next_shift_due_at to the next occurrence of the new time immediately,
    rather than waiting for the old anchor to fire once more first — so
    picking a new time takes effect right away.
    """
    db = get_supabase()
    now = datetime.now(timezone.utc)
    new_time = dt_time(hour=hour, minute=minute)
    next_due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_due <= now:
        next_due += timedelta(days=1)

    result = db.table("spark_office_seats").update({
        "shift_time_utc":    new_time.isoformat(),
        "next_shift_due_at": next_due.isoformat(),
    }).eq("spark_asa", spark_asa).execute()
    return result.data[0] if result.data else None


def seat_spark(spark: dict) -> dict:
    """Promote a Spark into an open Office seat. Caller must have already
    checked eligibility, sponsorship, and seat availability."""
    db = get_supabase()
    now = datetime.now(timezone.utc)
    data = {
        "spark_asa":         spark["asset_id"],
        "wallet":            spark["wallet"],
        "discord_user_id":   spark.get("discord_user_id"),
        "spark_name":        spark.get("name"),
        "spark_type":        spark["spark_type"],
        "spark_tier":        spark["tier"],
        "seated_at":         now.isoformat(),
        "next_shift_due_at": now.isoformat(),  # eligible to clock in immediately
        # Fixed daily anchor, locked in once at promotion — every future
        # clock-in advances to tomorrow's occurrence of THIS time, not
        # "24h from whenever you happened to click." Prevents the schedule
        # from drifting later every time you're a bit late.
        "shift_time_utc":    now.time().isoformat(),
        "status":            "active",
    }
    result = db.table("spark_office_seats").upsert(data, on_conflict="spark_asa").execute()
    return result.data[0] if result.data else {}


def vacate_seat(spark_asa: int) -> None:
    """Remove a Spark from its Office seat (demotion or duel loss). The
    Spark itself is untouched — it just falls back to base Jobs eligibility."""
    db = get_supabase()
    db.table("spark_office_seats").delete().eq("spark_asa", spark_asa).execute()


def get_eligible_sparks_for_office(wallet: str) -> dict:
    """
    Mirrors get_eligible_sparks_for_job's shape. Returns
    {"eligible": [...holdings dicts...], "skipped": {asset_id: reason}}.
    reason: "already_seated" | "not_enough_shifts" | "not_lucky_enough"
    """
    db = get_supabase()
    holdings = (
        db.table("spark_holdings")
        .select("asset_id, name, spark_type, tier, wallet, discord_user_id")
        .eq("wallet", wallet)
        .execute()
        .data or []
    )

    seated_asas = {
        s["spark_asa"] for s in
        db.table("spark_office_seats").select("spark_asa").eq("wallet", wallet).execute().data or []
    }

    eligible, skipped = [], {}
    for h in holdings:
        asa = h["asset_id"]
        if asa in seated_asas:
            skipped[asa] = "already_seated"
            continue
        check = check_office_eligibility(asa)
        if not check["eligible"]:
            skipped[asa] = check["reason"]
            continue
        eligible.append(h)

    return {"eligible": eligible, "skipped": skipped}


# ── Office shift lifecycle — mirrors create_spark_job / get_due_jobs /
#    complete_job exactly, just against spark_office_log ──────────────────

def create_office_shift(seat: dict, job: str, flavor_line: str, is_gamble: bool = False) -> dict:
    """Clock an Office Spark in. Writes a 'working' row, resolve in 8h."""
    db = get_supabase()
    now = datetime.now(timezone.utc)
    data = {
        "spark_asa":       seat["spark_asa"],
        "wallet":          seat["wallet"],
        "discord_user_id": seat.get("discord_user_id"),
        "spark_type":      seat["spark_type"],
        "spark_tier":      seat["spark_tier"],
        "spark_name":      seat.get("spark_name"),
        "job":             job,
        "status":          "working",
        "clock_in_at":     now.isoformat(),
        "resolve_at":      (now + timedelta(hours=OFFICE_SHIFT_DURATION_HOURS)).isoformat(),
        "flavor_line":     flavor_line,
        "is_gamble":       is_gamble,
    }
    result = db.table("spark_office_log").insert(data).execute()

    # Advance to TOMORROW'S occurrence of this seat's fixed shift_time_utc —
    # not "24h from right now." A fixed anchor means clocking in late once
    # doesn't push every future day later too; it always snaps back to the
    # same time-of-day. Falls back to the old now+24h behavior only if a
    # seat somehow has no shift_time_utc set (shouldn't happen post-migration).
    shift_time = seat.get("shift_time_utc")
    if shift_time:
        if isinstance(shift_time, str):
            shift_time = dt_time.fromisoformat(shift_time)
        next_due = now.replace(hour=shift_time.hour, minute=shift_time.minute, second=0, microsecond=0)
        if next_due <= now:
            next_due += timedelta(days=1)
    else:
        next_due = now + timedelta(hours=OFFICE_DAILY_COOLDOWN_HOURS)

    db.table("spark_office_seats").update({
        "last_shift_at":     now.isoformat(),
        "next_shift_due_at": next_due.isoformat(),
    }).eq("spark_asa", seat["spark_asa"]).execute()

    return result.data[0] if result.data else {}


def get_office_seats_for_wallet(wallet: str) -> list:
    db = get_supabase()
    result = db.table("spark_office_seats").select("*").eq("wallet", wallet).execute()
    return result.data or []


def get_working_office_shifts_map() -> dict:
    """spark_asa -> current 'working' spark_office_log row, for every seat
    at once — used by the live board so it doesn't run 20 separate queries."""
    db = get_supabase()
    result = db.table("spark_office_log").select("*").eq("status", "working").execute()
    return {r["spark_asa"]: r for r in (result.data or [])}


def get_working_office_shift(spark_asa: int) -> dict | None:
    """The current 'working' spark_office_log row for a Spark, if any.
    Used both by the bulk clock-in skip-check and by the admin
    force-resolve test command."""
    db = get_supabase()
    result = (
        db.table("spark_office_log")
        .select("*")
        .eq("spark_asa", spark_asa)
        .eq("status", "working")
        .order("clock_in_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def get_due_office_jobs() -> list:
    db = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    result = (
        db.table("spark_office_log")
        .select("*")
        .eq("status", "working")
        .lte("resolve_at", now_iso)
        .execute()
    )
    return result.data or []


def complete_office_job(job_id: int, spark_asa: int, outcome: str, amount: float | None,
                         nft_asa: int | None, flavor_line: str) -> None:
    """Resolve a due Office shift AND update the seat's running hit-rate /
    consecutive-miss counters in the same call — those counters are what
    duel targeting and cold-streak demotion both read."""
    db = get_supabase()
    db.table("spark_office_log").update({
        "status":      "complete",
        "outcome":     outcome,
        "amount":      amount,
        "nft_asa":     nft_asa,
        "flavor_line": flavor_line,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", job_id).execute()

    seat = get_office_seat(spark_asa)
    if not seat:
        return  # seat was vacated between roll and resolve — nothing to update

    hit = outcome in ("algo", "nft")
    update = {"shifts_completed": seat["shifts_completed"] + 1}
    if hit:
        update["hits"] = seat["hits"] + 1
        update["consecutive_misses"] = 0
    else:
        update["consecutive_misses"] = seat["consecutive_misses"] + 1
    db.table("spark_office_seats").update(update).eq("spark_asa", spark_asa).execute()


def get_seats_needing_reminder() -> list:
    """
    Active seats whose daily window has opened, haven't clocked in for it
    yet, and haven't already been reminded for this specific cycle (so a
    5-min resolver tick doesn't re-DM someone every pass until they show up).
    """
    db = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    seats = (
        db.table("spark_office_seats")
        .select("*")
        .eq("status", "active")
        .lte("next_shift_due_at", now_iso)
        .execute()
        .data or []
    )

    result = []
    for seat in seats:
        due = seat.get("next_shift_due_at")
        last_reminder = seat.get("last_reminder_at")
        if last_reminder and due and last_reminder >= due:
            continue  # already reminded since this window opened

        working = (
            db.table("spark_office_log")
            .select("id")
            .eq("spark_asa", seat["spark_asa"])
            .eq("status", "working")
            .execute()
            .data
        )
        if working:
            continue  # already clocked in, nothing to remind about

        result.append(seat)
    return result


def mark_seat_reminded(spark_asa: int, when: datetime) -> None:
    db = get_supabase()
    db.table("spark_office_seats").update({"last_reminder_at": when.isoformat()}).eq("spark_asa", spark_asa).execute()


def get_seats_for_cold_streak_demotion() -> list:
    """Active seats that just crossed OFFICE_DEMOTION_MISS_DAYS consecutive misses."""
    db = get_supabase()
    result = (
        db.table("spark_office_seats")
        .select("*")
        .eq("status", "active")
        .gte("consecutive_misses", OFFICE_DEMOTION_MISS_DAYS)
        .execute()
    )
    return result.data or []


def get_seats_for_noshow_demotion() -> list:
    """
    Active seats past their grace window with no 'working' shift open —
    i.e. they never clocked in for the current daily window at all.
    """
    db = get_supabase()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=OFFICE_NO_SHOW_GRACE_HOURS)).isoformat()
    seats = (
        db.table("spark_office_seats")
        .select("*")
        .eq("status", "active")
        .lt("next_shift_due_at", cutoff_iso)
        .execute()
        .data or []
    )
    no_show = []
    for seat in seats:
        working = (
            db.table("spark_office_log")
            .select("id")
            .eq("spark_asa", seat["spark_asa"])
            .eq("status", "working")
            .execute()
            .data
        )
        if not working:
            no_show.append(seat)
    return no_show


def get_unpaid_office_algo_jobs() -> list:
    db = get_supabase()
    result = (
        db.table("spark_office_log")
        .select("*")
        .eq("status", "complete")
        .eq("paid", False)
        .gt("amount", 0)
        .execute()
    )
    return result.data or []


def mark_office_jobs_paid(job_ids: list) -> None:
    if not job_ids:
        return
    db = get_supabase()
    db.table("spark_office_log").update({"paid": True}).in_("id", job_ids).execute()


def create_office_payout(wallet: str, total_algo: float, spark_count: int, office_log_ids: list, tx_id: str | None) -> dict:
    db = get_supabase()
    data = {
        "wallet":         wallet,
        "total_algo":     total_algo,
        "spark_count":    spark_count,
        "office_log_ids": office_log_ids,
        "tx_id":          tx_id,
    }
    result = db.table("spark_office_payouts").insert(data).execute()
    return result.data[0] if result.data else {}


# ── Duels — async best-of-7 RPS for seat takeover ─────────────────────────

def create_office_duel(challenger: dict, defender_seat: dict) -> dict:
    db = get_supabase()
    now = datetime.now(timezone.utc)
    data = {
        "challenger_asa":        challenger["asset_id"],
        "challenger_wallet":     challenger["wallet"],
        "challenger_discord_id": challenger.get("discord_user_id"),
        "challenger_name":       challenger.get("name"),

        "defender_asa":          defender_seat["spark_asa"],
        "defender_wallet":       defender_seat["wallet"],
        "defender_discord_id":   defender_seat.get("discord_user_id"),
        "defender_name":         defender_seat.get("spark_name"),

        "status":     "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=OFFICE_DUEL_SUBMIT_HOURS)).isoformat(),
    }
    result = db.table("spark_office_duels").insert(data).execute()

    db.table("spark_office_seats").update({"status": "in_duel"}).eq("spark_asa", defender_seat["spark_asa"]).execute()
    return result.data[0] if result.data else {}


def get_pending_duel_for_spark(asset_id: int) -> dict | None:
    """Find a pending duel where this Spark is either side — used by the
    pick-submission command to figure out which duel/side the caller is in."""
    db = get_supabase()
    for col in ("challenger_asa", "defender_asa"):
        result = db.table("spark_office_duels").select("*").eq(col, asset_id).eq("status", "pending").execute()
        if result.data:
            return result.data[0]
    return None


def submit_duel_picks(duel_id: int, side: str, picks: list) -> None:
    """side is 'challenger' or 'defender'."""
    db = get_supabase()
    db.table("spark_office_duels").update({
        f"{side}_picks":         picks,
        f"{side}_submitted_at":  datetime.now(timezone.utc).isoformat(),
    }).eq("id", duel_id).execute()


def get_expired_pending_duels() -> list:
    db = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    result = (
        db.table("spark_office_duels")
        .select("*")
        .eq("status", "pending")
        .lt("expires_at", now_iso)
        .execute()
    )
    return result.data or []


def resolve_office_duel(duel_id: int, winner_asa: int, rounds: list) -> None:
    db = get_supabase()
    db.table("spark_office_duels").update({
        "status":      "resolved",
        "winner_asa":  winner_asa,
        "rounds":      rounds,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", duel_id).execute()

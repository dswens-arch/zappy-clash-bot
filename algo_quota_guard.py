"""
algo_quota_guard.py
--------------------
Circuit breaker for AlgoNode's daily free-tier quota.

Problem this solves: AlgoNode's free tier counts FAILED requests against
your daily quota too. Once you cross the daily cap, every retry across
every system (Spark Jobs, Spark Office, Clash, GP) keeps hitting AlgoNode,
getting another 403, and burning more quota that never recovers until the
next day's reset — turning one bad morning into an all-day lockout.

This module gives every Algorand call site a single, cheap check before
it touches AlgoNode at all: "are we already known to be quota-blocked?"
That check hits Supabase, not AlgoNode, so it costs nothing against the
budget we're trying to protect. If we're blocked, skip the AlgoNode call
entirely instead of adding to the pile.

The block is stored in Supabase (not in-memory) so it survives Railway
restarts/redeploys and is shared across every cog/process in the bot.

Setup — run once in Supabase SQL editor:

    create table if not exists algo_quota_state (
        id integer primary key default 1,
        blocked_until timestamptz,
        last_error text,
        updated_at timestamptz default now()
    );
    insert into algo_quota_state (id) values (1) on conflict (id) do nothing;

Usage at any Algorand call site:

    from algo_quota_guard import is_quota_blocked, mark_quota_exceeded

    if is_quota_blocked():
        raise RuntimeError("Algorand API quota exceeded — try again later.")

    try:
        ...  # the real algod/indexer call
    except Exception as e:
        if _looks_like_quota_error(e):
            mark_quota_exceeded()
        raise
"""

from datetime import datetime, timezone, timedelta

_ROW_ID = 1

# How long to stay blocked once we confirm a real quota-exceeded 403.
# AlgoNode's quota resets daily; 6 hours is a safe middle ground that
# avoids re-testing constantly while not waiting a full day unnecessarily.
DEFAULT_BLOCK_HOURS = 6

# Local (per-process) cache of the last Supabase read, so a burst of many
# calls in the same second doesn't each round-trip to Supabase. This is
# just an optimization — Supabase reads are free relative to the problem
# we're solving, this just avoids hammering Supabase itself under load.
_local_cache = {"blocked_until": None, "checked_at": None}
_LOCAL_CACHE_TTL_SECONDS = 30


def _quota_error_text(e: Exception) -> str:
    """Best-effort extraction of an exception's message text for matching."""
    return str(e).lower()


def looks_like_quota_error(e: Exception) -> bool:
    """
    True if this exception is AlgoNode's daily-quota 403, not some other
    403/error (bad auth, real IP ban, network blip, etc). We only want to
    trip the breaker for the specific error we've confirmed AlgoNode sends:
    'Daily free API quota exceeded.'
    """
    text = _quota_error_text(e)
    return "quota exceeded" in text or ("403" in text and "quota" in text)


def is_quota_blocked() -> bool:
    """
    Returns True if we're currently inside a known quota-block window.
    Fails OPEN (returns False) on any Supabase error, so a Supabase hiccup
    never blocks real Algorand traffic — this breaker should only ever
    make things safer, never add a new failure mode of its own.
    """
    now = datetime.now(timezone.utc)

    if _local_cache["checked_at"] is not None:
        age = (now - _local_cache["checked_at"]).total_seconds()
        if age < _LOCAL_CACHE_TTL_SECONDS:
            blocked_until = _local_cache["blocked_until"]
            return blocked_until is not None and now < blocked_until

    try:
        from database import get_supabase
        db = get_supabase()
        row = (
            db.table("algo_quota_state")
            .select("blocked_until")
            .eq("id", _ROW_ID)
            .single()
            .execute()
            .data
        )
        blocked_until = None
        if row and row.get("blocked_until"):
            blocked_until = datetime.fromisoformat(row["blocked_until"].replace("Z", "+00:00"))

        _local_cache["blocked_until"] = blocked_until
        _local_cache["checked_at"] = now

        return blocked_until is not None and now < blocked_until

    except Exception as e:
        print(f"[algo_quota_guard] check failed, failing open (not blocking): {e}")
        return False


def mark_quota_exceeded(hours: int = DEFAULT_BLOCK_HOURS, detail: str = ""):
    """
    Call this the moment ANY call site actually confirms AlgoNode's
    quota-exceeded 403. Every other call site across every cog will start
    skipping AlgoNode immediately (within _LOCAL_CACHE_TTL_SECONDS) instead
    of piling on more failed requests.
    """
    blocked_until = datetime.now(timezone.utc) + timedelta(hours=hours)

    _local_cache["blocked_until"] = blocked_until
    _local_cache["checked_at"] = datetime.now(timezone.utc)

    try:
        from database import get_supabase
        db = get_supabase()
        db.table("algo_quota_state").upsert({
            "id": _ROW_ID,
            "blocked_until": blocked_until.isoformat(),
            "last_error": detail or "Daily free API quota exceeded",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        print(f"[algo_quota_guard] Quota exceeded — blocking all Algorand calls until {blocked_until.isoformat()}")
    except Exception as e:
        print(f"[algo_quota_guard] failed to persist block to Supabase (local block still active): {e}")


def clear_quota_block():
    """Manual override — call from a console/admin command if you confirm
    quota has actually reset before the block window naturally expires."""
    _local_cache["blocked_until"] = None
    _local_cache["checked_at"] = datetime.now(timezone.utc)
    try:
        from database import get_supabase
        db = get_supabase()
        db.table("algo_quota_state").upsert({"id": _ROW_ID, "blocked_until": None}).execute()
        print("[algo_quota_guard] Block cleared manually.")
    except Exception as e:
        print(f"[algo_quota_guard] failed to clear block in Supabase: {e}")

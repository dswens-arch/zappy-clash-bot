"""
algorand_lookup.py
------------------
Looks up Zappy NFT data using a pre-computed collection table.
ALL traits and image URLs are hardcoded — zero IPFS calls needed.

Only network call: Algorand indexer to verify wallet holdings (once per /link).
Everything else is instant from the local lookup table.
"""

import os
import aiohttp
import asyncio
from datetime import datetime, timezone
from zappy_collection import ZAPPY_COLLECTION, ZAPPY_ASSET_IDS
from stats_engine import calculate_stats, get_hero_stats, get_collab_stats, get_king_stats
from algo_quota_guard import is_quota_blocked, mark_quota_exceeded, looks_like_quota_error, record_call

KING_ASA = 3562991430

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
INDEXER_URL  = os.getenv("INDEXER_URL", "https://mainnet-idx.algonode.cloud")
INDEXER_URL2 = os.getenv("INDEXER_URL2", "https://mainnet-idx.4160.nodely.io")

HERO_ASSET_IDS = {
    2742429215: "Bear",
    2742451787: "Crocodile",
    2779491623: "Cat",
    3091046425: "Rabbit",
    3509675278: "Wolf",
    3509678849: "Frog",
    3565577964: "Eagle",
    3575335199: "Buck",
    3627099067: "Snake",
    3627104888: "Shark",
    3672860859: "Alien",
    3684236824: "Bat",
    3686974146: "Bison",
}

COLLAB_ASSET_IDS = {
    2647684790: "ShittyKitties",
}

# Hero image URLs — hardcoded since they're not in the CSV
HERO_IMAGES = {
    "Bear":       "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Bear.jpg",
    "Crocodile":  "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Crocodile.jpg",
    "Cat":        "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Cat.jpg",
    "Rabbit":     "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Rabbit.jpg",
    "Wolf":       "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Wolf.jpg",
    "Frog":       "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Frog.jpg",
    "Eagle":      "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Eagle.jpg",
    "Buck":       "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Buck.jpg",
    "Snake":      "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Snake.jpg",
    "Shark":      "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Shark.jpg",
    "Alien":      "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Alien.jpg",
    "Bat":        "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Bat.jpg",
    "Bison":      "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/hero_Bison.jpg",
}

COLLAB_IMAGES = {
    "ShittyKitties": "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/collab_ShittyKitties.jpg",
}

# In-memory cache (traits are already local, but cache computed stats)
_zappy_cache: dict = {}

# Wallet ownership cache — avoids re-hitting the indexer on every command.
# Two layers:
#   1. _wallet_cache (this process, in memory) — near-free, but wiped on
#      every Railway restart/redeploy, so it can't be the only layer.
#   2. Supabase (wallet_cache table) — survives restarts/redeploys, shared
#      across every cog/process, same pattern as algo_quota_state.
# Successful lookups are cached for WALLET_CACHE_TTL. Errors (network
# blips, indexer 5xx, etc.) are cached for the much shorter ERROR_CACHE_TTL
# instead of not at all — long enough to stop a bad patch of indexer
# flakiness from turning into a retry storm across every command a user
# touches, short enough that a real fix or retry lands quickly.
#
# Setup — run once in Supabase SQL editor:
#
#     create table if not exists wallet_cache (
#         wallet_address text primary key,
#         data jsonb not null,
#         is_error boolean not null default false,
#         cached_at timestamptz not null default now()
#     );
import time as _time
_wallet_cache: dict = {}
_wallet_cache_ts: dict = {}
WALLET_CACHE_TTL = 43200   # 12 hours, for successful results
ERROR_CACHE_TTL  = 90      # 90 seconds, for results that came back as an error


def _describe_403(resp, body: str) -> str:
    """Short, loggable description of a 403 so we can tell WHICH limit was hit
    (the body/headers usually say) instead of guessing. Also stored in
    algo_quota_state.last_error by mark_quota_exceeded."""
    keep = {k: v for k, v in resp.headers.items()
            if k.lower().startswith(("x-", "retry-after", "ratelimit", "cf-ray", "server"))}
    text = " ".join((body or "").split())[:200]
    msg = f"body={text!r} headers={keep}"
    print(f"[algorand_lookup] 403 from indexer: {msg}")
    return msg


# ─────────────────────────────────────────────
# Main Zappy fetch — pure local lookup
# ─────────────────────────────────────────────

async def fetch_zappy_traits(asset_id: int) -> dict | None:
    """
    Return full Zappy data: name, image URL, traits, calculated stats.
    Uses the hardcoded collection table — no network calls.
    """
    if asset_id in _zappy_cache:
        return _zappy_cache[asset_id]

    # King Zappy #1200 — boosted stats + Royal Decree override
    if asset_id == KING_ASA:
        king_data = get_king_stats(asset_id)
        entry = ZAPPY_COLLECTION.get(asset_id, {})
        if king_data and entry:
            result = {
                "asset_id":  asset_id,
                "name":      entry.get("name", "Zappy #1200"),
                "unit_name": entry.get("unit_name", "ZAPP1200"),
                "is_hero":   False,
                "is_collab": False,
                "traits":    entry,
                "stats":     king_data,
                "image_url": entry.get("image_url", ""),
            }
            _zappy_cache[asset_id] = result
            return result

    # Heroes
    if asset_id in HERO_ASSET_IDS:
        hero_type = HERO_ASSET_IDS[asset_id]
        stats = get_hero_stats(hero_type)
        result = {
            "asset_id":  asset_id,
            "name":      f"Zappy Hero — {hero_type}",
            "unit_name": "ZAPPH",
            "is_hero":   True,
            "hero_type": hero_type,
            "stats":     stats,
            "traits":    {"hero_type": hero_type},
            "image_url": HERO_IMAGES.get(hero_type, ""),
        }
        _zappy_cache[asset_id] = result
        return result

    # Collabs
    if asset_id in COLLAB_ASSET_IDS:
        collab_type = COLLAB_ASSET_IDS[asset_id]
        stats = get_collab_stats(collab_type)
        result = {
            "asset_id":    asset_id,
            "name":        "Shitty Zappy Kitty",
            "unit_name":   "ZAPPC001",
            "is_collab":   True,
            "collab_type": collab_type,
            "stats":       stats,
            "traits":      {"collab_type": collab_type},
            "image_url":   COLLAB_IMAGES.get(collab_type, ""),
        }
        _zappy_cache[asset_id] = result
        return result

    # Main collection — look up from table
    entry = ZAPPY_COLLECTION.get(asset_id)
    if not entry:
        print(f"ASA {asset_id} not found in collection table")
        return None

    traits = {
        "background": entry["background"],
        "body":       entry["body"],
        "earring":    entry["earring"],
        "eyes":       entry["eyes"],
        "eyewear":    entry["eyewear"],
        "head":       entry["head"],
        "mouth":      entry["mouth"],
        "skin":       entry["skin"],
    }

    stats = calculate_stats(traits)

    result = {
        "asset_id":  asset_id,
        "name":      entry["name"],
        "unit_name": entry["unit_name"],
        "is_hero":   False,
        "is_collab": False,
        "traits":    traits,
        "stats":     stats,
        "image_url": entry["image_url"],
    }
    _zappy_cache[asset_id] = result
    return result


# ─────────────────────────────────────────────
# Wallet cache — local layer + Supabase, see module notes above
# ─────────────────────────────────────────────

def _cache_ttl_for(cached: dict) -> int:
    return ERROR_CACHE_TTL if cached.get("error") else WALLET_CACHE_TTL


def _get_cached_wallet_result(wallet_address: str) -> dict | None:
    """Fresh cached result if one exists, checking the local layer first
    (cheap) and falling back to Supabase (survives restarts/redeploys)."""
    now = _time.monotonic()

    if wallet_address in _wallet_cache:
        cached = _wallet_cache[wallet_address]
        if now - _wallet_cache_ts.get(wallet_address, 0) < _cache_ttl_for(cached):
            return cached

    try:
        from database import get_supabase
        db = get_supabase()
        row = (
            db.table("wallet_cache")
            .select("data, is_error, cached_at")
            .eq("wallet_address", wallet_address)
            .maybe_single()
            .execute()
        )
        row = row.data if row else None
        if not row:
            return None
        cached_at = datetime.fromisoformat(row["cached_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        ttl = ERROR_CACHE_TTL if row.get("is_error") else WALLET_CACHE_TTL
        if age >= ttl:
            return None
        result = row["data"]
        # Warm the local layer so the rest of this burst of commands is free
        _wallet_cache[wallet_address] = result
        _wallet_cache_ts[wallet_address] = now
        return result
    except Exception as e:
        print(f"[algorand_lookup] wallet cache read failed, treating as a miss: {e}")
        return None


def _last_good_from_entry(entry: dict | None) -> dict | None:
    """A cache entry's last successful result: the entry itself if it was a
    success, or the 'stale' copy an error entry carries."""
    if not entry:
        return None
    if not entry.get("error"):
        return entry
    stale = entry.get("stale")
    return stale if stale and not stale.get("error") else None


def get_last_known_wallet_result(wallet_address: str) -> dict | None:
    """
    The most recent SUCCESSFUL holdings for a wallet, however old. Never calls
    the indexer. Used by games that would rather act on holdings that are a
    few hours old than refuse to play while the indexer or quota is down.
    Returns None if this wallet has never had a successful lookup cached.
    """
    good = _last_good_from_entry(_wallet_cache.get(wallet_address))
    if good:
        return good
    try:
        from database import get_supabase
        row = (
            get_supabase().table("wallet_cache")
            .select("data")
            .eq("wallet_address", wallet_address)
            .maybe_single()
            .execute()
        )
        return _last_good_from_entry(row.data["data"]) if row and row.data else None
    except Exception as e:
        print(f"[algorand_lookup] last-known read failed: {e}")
        return None


def _store_wallet_result(wallet_address: str, result: dict) -> None:
    """Write a lookup result to both cache layers. Errors are stored too
    (with the short TTL applied on read) so a bad patch of indexer
    flakiness doesn't turn every subsequent command into a fresh live call."""
    # An error must not wipe out the last good holdings (the old behaviour left
    # nothing to fall back to during an indexer outage or quota block). Carry
    # the last good result along inside the error entry as "stale". Readers
    # that only check result["error"] behave exactly as before.
    if result.get("error") and "stale" not in result:
        prev = get_last_known_wallet_result(wallet_address)
        if prev:
            result = {**result, "stale": prev}

    _wallet_cache[wallet_address] = result
    _wallet_cache_ts[wallet_address] = _time.monotonic()

    try:
        from database import get_supabase
        db = get_supabase()
        db.table("wallet_cache").upsert({
            "wallet_address": wallet_address,
            "data":           result,
            "is_error":       bool(result.get("error")),
            "cached_at":      datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"[algorand_lookup] wallet cache write failed (local cache still set): {e}")


def clear_wallet_cache(wallet_address: str) -> None:
    """Force the next lookup for this wallet to hit the indexer live.
    Used by /link, which should always re-verify on demand."""
    _wallet_cache.pop(wallet_address, None)
    _wallet_cache_ts.pop(wallet_address, None)
    try:
        from database import get_supabase
        db = get_supabase()
        db.table("wallet_cache").delete().eq("wallet_address", wallet_address).execute()
    except Exception as e:
        print(f"[algorand_lookup] wallet cache clear failed (local cache still cleared): {e}")


# ─────────────────────────────────────────────
# Wallet verification — one indexer call only
# ─────────────────────────────────────────────

async def verify_wallet_owns_zappy(wallet_address: str) -> dict:
    """
    Verify wallet holdings via Algorand indexer. Successful results are
    cached for WALLET_CACHE_TTL; errors for the much shorter ERROR_CACHE_TTL.
    The cache is Supabase-backed so it survives restarts/redeploys.
    """
    cached = _get_cached_wallet_result(wallet_address)
    if cached is not None:
        # A cached ERROR (e.g. from a quota 403) shouldn't lock a player out of
        # the games when we still have their last good holdings on file.
        if cached.get("error") and is_quota_blocked():
            good = _last_good_from_entry(cached)
            if good:
                return {**good, "stale_fallback": True}
        return cached

    result = {
        "owns":    False,
        "zappies": [],
        "heroes":  [],
        "collabs": [],
        "error":   None,
    }

    if is_quota_blocked():
        # Quota block active: serve the last known good holdings (NFT ownership
        # rarely changes within hours) instead of failing every game command.
        good = get_last_known_wallet_result(wallet_address)
        if good:
            return {**good, "stale_fallback": True}
        result["error"] = "Algorand API quota exceeded — try again later."
        _store_wallet_result(wallet_address, result)
        return result

    try:
        async with aiohttp.ClientSession() as session:
            url    = f"{INDEXER_URL}/v2/accounts/{wallet_address}/assets"
            assets = []
            next_token = None

            # Paginate through all assets — wallets with many ASAs need multiple calls
            while True:
                params = {"limit": 1000}
                if next_token:
                    params["next"] = next_token

                headers = {"X-Indexer-API-Token": os.getenv("INDEXER_TOKEN", "")}
                record_call()
                status, data, body403 = None, None, ""
                try:
                    async with session.get(url, params=params, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        status = resp.status
                        if status == 200:
                            data = await resp.json()
                        elif status == 403:
                            body403 = _describe_403(resp, await resp.text())
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    status = None  # treated as a primary failure below

                if status == 403:
                    mark_quota_exceeded(detail=f"algorand_lookup: primary indexer 403 {body403}"[:500])
                    good = get_last_known_wallet_result(wallet_address)
                    if good:
                        return {**good, "stale_fallback": True}
                    result["error"] = "Algorand API quota exceeded — try again later."
                    _store_wallet_result(wallet_address, result)
                    return result

                if data is not None:
                    assets.extend(data.get("assets", []))
                    next_token = data.get("next-token")
                    if not next_token:
                        break
                    continue

                # Only fall back to the secondary indexer on a real failure
                # (5xx, timeout, or network error) — not on every non-200,
                # so an ordinary 4xx doesn't silently double our call volume.
                if status is not None and status < 500:
                    result["error"] = f"Indexer returned {status}"
                    _store_wallet_result(wallet_address, result)
                    return result

                fallback_url = f"{INDEXER_URL2}/v2/accounts/{wallet_address}/assets"
                record_call()
                try:
                    async with session.get(fallback_url, params=params, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=15)) as resp2:
                        if resp2.status == 403:
                            mark_quota_exceeded(detail=f"algorand_lookup: fallback indexer 403 {_describe_403(resp2, await resp2.text())}"[:500])
                            result["error"] = "Algorand API quota exceeded — try again later."
                            _store_wallet_result(wallet_address, result)
                            return result
                        if resp2.status != 200:
                            result["error"] = f"Both indexers failed: {status}, {resp2.status}"
                            _store_wallet_result(wallet_address, result)
                            return result
                        data2 = await resp2.json()
                except (asyncio.TimeoutError, aiohttp.ClientError) as e2:
                    result["error"] = f"Both indexers failed: {status}, {e2}"
                    _store_wallet_result(wallet_address, result)
                    return result

                assets.extend(data2.get("assets", []))
                next_token = data2.get("next-token")
                if not next_token:
                    break

        for asset in assets:
            if asset.get("amount", 0) <= 0:
                continue
            asset_id = asset["asset-id"]

            if asset_id in HERO_ASSET_IDS:
                result["heroes"].append({
                    "asset_id":  asset_id,
                    "hero_type": HERO_ASSET_IDS[asset_id],
                    "name":      f"Zappy Hero — {HERO_ASSET_IDS[asset_id]}",
                })
                result["owns"] = True

            elif asset_id in COLLAB_ASSET_IDS:
                result["collabs"].append({
                    "asset_id":    asset_id,
                    "collab_type": COLLAB_ASSET_IDS[asset_id],
                    "name":        "Shitty Zappy Kitty",
                })
                result["owns"] = True

            elif asset_id in ZAPPY_ASSET_IDS:
                entry = ZAPPY_COLLECTION[asset_id]
                result["zappies"].append({
                    "asset_id":  asset_id,
                    "unit_name": entry["unit_name"],
                    "name":      entry["name"],
                    "image_url": entry.get("image_url", ""),
                })
                result["owns"] = True

    except aiohttp.ClientError as e:
        result["error"] = f"Network error: {e}"
    except Exception as e:
        result["error"] = f"Error: {e}"

    if result["error"]:
        print(f"[algorand_lookup] wallet check error for {wallet_address[:8]}...: {result['error']}")

    _store_wallet_result(wallet_address, result)
    return result


# ─────────────────────────────────────────────
# Single-asset balance (e.g. ZAPP) — quota-guard aware
# ─────────────────────────────────────────────

_asset_balance_cache: dict = {}          # key -> (monotonic_ts, amount)
ASSET_BALANCE_CACHE_TTL = WALLET_CACHE_TTL   # 12h, same as wallet holdings


def _asset_key(wallet_address: str, asset_id: int) -> str:
    # Stored in the existing wallet_cache table under a namespaced key, so no
    # new table is needed.
    return f"{wallet_address}:asset:{asset_id}"


def _read_asset_balance_cache(wallet_address: str, asset_id: int) -> tuple[int | None, float | None]:
    """(balance, age_seconds) from memory, then Supabase. (None, None) if never cached.
    No TTL applied here, so callers decide how old is too old."""
    key = _asset_key(wallet_address, asset_id)
    hit = _asset_balance_cache.get(key)
    if hit:
        return hit[1], _time.monotonic() - hit[0]
    try:
        from database import get_supabase
        row = (
            get_supabase().table("wallet_cache")
            .select("data, cached_at")
            .eq("wallet_address", key)
            .maybe_single()
            .execute()
        )
        row = row.data if row else None
        if not row or "balance" not in row["data"]:
            return None, None
        cached_at = datetime.fromisoformat(row["cached_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - cached_at).total_seconds()
        _asset_balance_cache[key] = (_time.monotonic() - age, row["data"]["balance"])
        return row["data"]["balance"], age
    except Exception as e:
        print(f"[algorand_lookup] asset balance cache read failed: {e}")
        return None, None


def _write_asset_balance_cache(wallet_address: str, asset_id: int, balance: int) -> None:
    key = _asset_key(wallet_address, asset_id)
    _asset_balance_cache[key] = (_time.monotonic(), balance)
    try:
        from database import get_supabase
        get_supabase().table("wallet_cache").upsert({
            "wallet_address": key,
            "data":           {"balance": balance},
            "is_error":       False,
            "cached_at":      datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"[algorand_lookup] asset balance cache write failed (local cache still set): {e}")


def bump_cached_asset_balance(wallet_address: str, asset_id: int, delta: int) -> None:
    """After the bot pays a wallet, add the payout to its cached balance (if one
    exists) so the cache stays right without a chain lookup."""
    balance, _ = _read_asset_balance_cache(wallet_address, asset_id)
    if balance is not None:
        _write_asset_balance_cache(wallet_address, asset_id, max(0, balance + delta))


def is_opt_in_cached(wallet_address: str, asset_id: int) -> bool:
    """True if we've already seen this wallet opted in to the asset. Opt-ins
    don't get undone in practice, so a positive result is cached permanently."""
    key = f"{wallet_address}:optin:{asset_id}"
    if key in _asset_balance_cache:
        return True
    try:
        from database import get_supabase
        row = (
            get_supabase().table("wallet_cache")
            .select("wallet_address")
            .eq("wallet_address", key)
            .maybe_single()
            .execute()
        )
        if row and row.data:
            _asset_balance_cache[key] = (_time.monotonic(), 1)
            return True
    except Exception as e:
        print(f"[algorand_lookup] opt-in cache read failed: {e}")
    return False


def cache_opt_in(wallet_address: str, asset_id: int) -> None:
    key = f"{wallet_address}:optin:{asset_id}"
    _asset_balance_cache[key] = (_time.monotonic(), 1)
    try:
        from database import get_supabase
        get_supabase().table("wallet_cache").upsert({
            "wallet_address": key,
            "data":           {"opted_in": True},
            "is_error":       False,
            "cached_at":      datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"[algorand_lookup] opt-in cache write failed (local cache still set): {e}")


async def get_asset_balance(
    wallet_address: str,
    asset_id: int,
    max_age: int = ASSET_BALANCE_CACHE_TTL,
    allow_stale: bool = True,
) -> tuple[int | None, str | None]:
    """
    Balance of one ASA for a wallet.

    Cache-first (memory, then Supabase, so it survives redeploys). Only goes to
    the indexer when there's no cached value younger than `max_age`. If that
    live lookup can't happen (quota block, 403, indexer error, timeout) the
    last cached value is returned however old when `allow_stale`, since a gate
    like the expedition entry fee doesn't need chain-fresh numbers.

    Returns (balance, None), or (None, reason) when it couldn't be determined
    at all. A failed lookup is never reported as a balance of 0. Honors the
    quota circuit breaker, counts toward record_call(), and trips the breaker
    on a quota 403.
    """
    cached, age = _read_asset_balance_cache(wallet_address, asset_id)
    if cached is not None and age is not None and age < max_age:
        return cached, None

    def _fallback(reason: str):
        if allow_stale and cached is not None:
            return cached, None
        return None, reason

    if is_quota_blocked():
        return _fallback("quota_blocked")

    url     = f"{INDEXER_URL}/v2/accounts/{wallet_address}/assets"
    headers = {"X-Indexer-API-Token": os.getenv("INDEXER_TOKEN", "")}
    record_call()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"asset-id": asset_id}, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 403:
                    mark_quota_exceeded(
                        detail=f"get_asset_balance 403 {_describe_403(resp, await resp.text())}"[:500]
                    )
                    return _fallback("quota_blocked")
                if resp.status != 200:
                    return _fallback(f"indexer_{resp.status}")
                data = await resp.json()
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        return _fallback(f"network_error: {e}")

    balance = next((a.get("amount", 0) for a in data.get("assets", [])
                    if a.get("asset-id") == asset_id), 0)
    _write_asset_balance_cache(wallet_address, asset_id, balance)
    return balance, None


# ─────────────────────────────────────────────
# Convenience wrappers
# ─────────────────────────────────────────────

async def get_zappy_for_battle(asset_id: int) -> dict | None:
    return await fetch_zappy_traits(asset_id)


async def link_wallet(discord_user_id: str, wallet_address: str) -> dict:
    result = await verify_wallet_owns_zappy(wallet_address)
    result["discord_user_id"] = discord_user_id
    result["wallet_address"]  = wallet_address
    return result


# ─────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────
if __name__ == "__main__":
    async def test():
        for test_id in [2644039660, 2601408785]:
            result = await fetch_zappy_traits(test_id)
            if result:
                s = result['stats']
                print(f"{result['name']}: VLT {s['VLT']} | INS {s['INS']} | SPK {s['SPK']}")
                print(f"  Image: {result['image_url']}")
                print(f"  Traits: {result['traits']}")
            else:
                print(f"ASA {test_id}: NOT FOUND")
    asyncio.run(test())

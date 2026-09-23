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


def _store_wallet_result(wallet_address: str, result: dict) -> None:
    """Write a lookup result to both cache layers. Errors are stored too
    (with the short TTL applied on read) so a bad patch of indexer
    flakiness doesn't turn every subsequent command into a fresh live call."""
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
        return cached

    result = {
        "owns":    False,
        "zappies": [],
        "heroes":  [],
        "collabs": [],
        "error":   None,
    }

    if is_quota_blocked():
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
                status, data = None, None
                try:
                    async with session.get(url, params=params, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        status = resp.status
                        if status == 200:
                            data = await resp.json()
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    status = None  # treated as a primary failure below

                if status == 403:
                    mark_quota_exceeded(detail="algorand_lookup: primary indexer 403")
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
                            mark_quota_exceeded(detail="algorand_lookup: fallback indexer 403")
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

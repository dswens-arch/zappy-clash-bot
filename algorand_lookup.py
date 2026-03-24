"""
algorand_lookup.py
------------------
Looks up Zappy NFT data using a pre-computed collection table.
ALL traits and image URLs are hardcoded — zero IPFS calls needed.

Only network call: Algorand indexer to verify wallet holdings (once per /link).
Everything else is instant from the local lookup table.
"""

import aiohttp
import asyncio
from zappy_collection import ZAPPY_COLLECTION, ZAPPY_ASSET_IDS
from stats_engine import calculate_stats, get_hero_stats, get_collab_stats

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
INDEXER_URL = "https://mainnet-idx.algonode.cloud"

HERO_ASSET_IDS = {
    2742429215: "Bear",
    2742451787: "Crocodile",
    2779491623: "Cat",
    3091046425: "Rabbit",
}

COLLAB_ASSET_IDS = {
    2647684790: "ShittyKitties",
}

# Hero image URLs — hardcoded since they're not in the CSV
HERO_IMAGES = {
    "Bear":       "",
    "Crocodile":  "",
    "Cat":        "",
    "Rabbit":     "",
}

# In-memory cache (traits are already local, but cache computed stats)
_zappy_cache: dict = {}

# Wallet ownership cache — avoids re-hitting the indexer on every command
# Expires after 5 minutes so it stays fresh if someone buys/sells
import time as _time
_wallet_cache: dict = {}
_wallet_cache_ts: dict = {}
WALLET_CACHE_TTL = 300   # seconds


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
            "image_url":   "",
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
# Wallet verification — one indexer call only
# ─────────────────────────────────────────────

async def verify_wallet_owns_zappy(wallet_address: str) -> dict:
    """
    Verify wallet holdings via Algorand indexer.
    Results cached for 5 minutes to avoid repeat indexer calls.
    """
    # Return cached result if fresh
    now = _time.monotonic()
    if wallet_address in _wallet_cache:
        if now - _wallet_cache_ts[wallet_address] < WALLET_CACHE_TTL:
            return _wallet_cache[wallet_address]
    result = {
        "owns":    False,
        "zappies": [],
        "heroes":  [],
        "collabs": [],
        "error":   None,
    }

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

                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        result["error"] = f"Indexer returned {resp.status}"
                        return result
                    data = await resp.json()
                    assets.extend(data.get("assets", []))
                    next_token = data.get("next-token")
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
                })
                result["owns"] = True

    except aiohttp.ClientError as e:
        result["error"] = f"Network error: {e}"
    except Exception as e:
        result["error"] = f"Error: {e}"

    # Cache the result (even errors, to avoid hammering on failures)
    _wallet_cache[wallet_address]    = result
    _wallet_cache_ts[wallet_address] = _time.monotonic()
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

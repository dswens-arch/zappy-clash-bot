"""
algorand_lookup.py
------------------
Looks up Zappy NFT data using a pre-computed collection table.
No Algorand indexer calls needed for asset info — all names and
metadata CIDs are hardcoded from the CSV.

Only network calls:
  - Algorand indexer: verify wallet holdings (once per /link or /clash)
  - IPFS: fetch metadata JSON to get traits + image URL (cached after first fetch)
"""

import aiohttp
import asyncio
import base64
from zappy_collection import ZAPPY_COLLECTION, ZAPPY_ASSET_IDS
from stats_engine import calculate_stats, get_hero_stats, get_collab_stats

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
INDEXER_URL = "https://mainnet-idx.algonode.cloud"

# IPFS gateways — tried in order
IPFS_GATEWAYS = [
    "https://nftstorage.link/ipfs/",
    "https://dweb.link/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://ipfs.io/ipfs/",
]

# ─────────────────────────────────────────────
# Special assets
# ─────────────────────────────────────────────
HERO_ASSET_IDS = {
    2742429215: "Bear",
    2742451787: "Crocodile",
    2779491623: "Cat",
    3091046425: "Rabbit",
}

COLLAB_ASSET_IDS = {
    2647684790: "ShittyKitties",
}

# In-memory cache: asset_id -> full zappy data (traits + stats + image)
_zappy_cache: dict = {}


# ─────────────────────────────────────────────
# IPFS helpers
# ─────────────────────────────────────────────

def cid_to_image_url(cid: str) -> str:
    """
    Convert a CID to a Discord-friendly image URL.
    CIDv1 (bafkrei...) uses nftstorage subdomain format.
    CIDv0 (Qm...) uses cloudflare path format.
    """
    if not cid:
        return ""
    if cid.startswith("Qm"):
        return f"https://cloudflare-ipfs.com/ipfs/{cid}"
    return f"https://{cid}.ipfs.nftstorage.link"


async def fetch_ipfs_json(session: aiohttp.ClientSession, cid: str) -> dict | None:
    """Fetch JSON metadata from IPFS, trying multiple gateways."""
    for gateway in IPFS_GATEWAYS:
        url = f"{gateway}{cid}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
        except Exception as e:
            print(f"IPFS gateway {gateway} failed for {cid}: {e}")
            continue
    print(f"All IPFS gateways failed for CID: {cid}")
    return None


def extract_traits_from_metadata(metadata: dict, metadata_cid: str) -> dict:
    """Extract traits and image URL from IPFS metadata JSON."""
    props = metadata.get("properties", {})

    if isinstance(props, list):
        props = {item["trait_type"]: item["value"] for item in props if "trait_type" in item}

    if not props:
        attrs = metadata.get("attributes", {})
        if isinstance(attrs, list):
            props = {item["trait_type"]: item["value"] for item in attrs if "trait_type" in item}
        else:
            props = attrs

    # Get image CID from metadata
    raw_image = metadata.get("image", "")
    if raw_image.startswith("ipfs://"):
        image_cid = raw_image.replace("ipfs://", "").split("/")[0]
        image_url = cid_to_image_url(image_cid)
    else:
        image_url = raw_image

    return {
        "background": props.get("Background", ""),
        "body":       props.get("Body", ""),
        "earring":    props.get("Earring", "None"),
        "eyes":       props.get("Eyes", ""),
        "eyewear":    props.get("Eyewear", "None"),
        "head":       props.get("Head", ""),
        "mouth":      props.get("Mouth", ""),
        "skin":       props.get("Skin", ""),
        "image_url":  image_url,
    }


# ─────────────────────────────────────────────
# Main fetch — uses collection table, no indexer
# ─────────────────────────────────────────────

async def fetch_zappy_traits(asset_id: int) -> dict | None:
    """
    Fetch full data for a Zappy: name, traits, stats, image URL.
    Uses the pre-computed collection table for name/CID lookup.
    Only hits IPFS for metadata (cached after first fetch).
    """
    # Return cached result if available
    if asset_id in _zappy_cache:
        return _zappy_cache[asset_id]

    # Heroes
    if asset_id in HERO_ASSET_IDS:
        hero_type = HERO_ASSET_IDS[asset_id]
        stats = get_hero_stats(hero_type)
        result = {
            "asset_id":  asset_id,
            "name":      f"Zappy Hero — {hero_type}",
            "unit_name": f"ZAPPH",
            "is_hero":   True,
            "hero_type": hero_type,
            "stats":     stats,
            "traits":    {"hero_type": hero_type},
            "image_url": "",
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

    # Main collection — look up from pre-computed table
    entry = ZAPPY_COLLECTION.get(asset_id)
    if not entry:
        print(f"ASA {asset_id} not found in collection table")
        return None

    name         = entry["name"]
    unit_name    = entry["unit_name"]
    metadata_cid = entry["metadata_cid"]

    # Fetch metadata from IPFS
    try:
        async with aiohttp.ClientSession() as session:
            metadata = await fetch_ipfs_json(session, metadata_cid)

        if not metadata:
            # Return partial result without traits/image if IPFS fails
            # Stats will be zeroed but at least the name works
            print(f"IPFS failed for {name} — returning name-only result")
            return {
                "asset_id":  asset_id,
                "name":      name,
                "unit_name": unit_name,
                "is_hero":   False,
                "is_collab": False,
                "traits":    {},
                "stats":     {"VLT": 50, "INS": 50, "SPK": 50, "ability": None, "combo": None},
                "image_url": "",
            }

        traits = extract_traits_from_metadata(metadata, metadata_cid)
        stats  = calculate_stats(traits)

        result = {
            "asset_id":  asset_id,
            "name":      name,
            "unit_name": unit_name,
            "is_hero":   False,
            "is_collab": False,
            "traits":    traits,
            "stats":     stats,
            "image_url": traits.get("image_url", ""),
        }
        _zappy_cache[asset_id] = result
        return result

    except Exception as e:
        print(f"Error fetching traits for {name} ({asset_id}): {e}")
        return None


# ─────────────────────────────────────────────
# Wallet verification — still uses indexer
# but only to get asset IDs, not asset details
# ─────────────────────────────────────────────

async def verify_wallet_owns_zappy(wallet_address: str) -> dict:
    """
    Check wallet holdings via Algorand indexer.
    Uses the collection table to identify Zappies — no per-asset indexer calls.
    """
    result = {
        "owns":    False,
        "zappies": [],
        "heroes":  [],
        "collabs": [],
        "error":   None,
    }

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{INDEXER_URL}/v2/accounts/{wallet_address}/assets"
            async with session.get(url, params={"limit": 1000},
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    result["error"] = f"Indexer returned status {resp.status}"
                    return result
                data   = await resp.json()
                assets = data.get("assets", [])

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
                # Look up name directly from collection table — no indexer call needed
                entry = ZAPPY_COLLECTION[asset_id]
                result["zappies"].append({
                    "asset_id":  asset_id,
                    "unit_name": entry["unit_name"],
                    "name":      entry["name"],
                })
                result["owns"] = True

    except aiohttp.ClientError as e:
        result["error"] = f"Network error: {str(e)}"
    except Exception as e:
        result["error"] = f"Unexpected error: {str(e)}"

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
            print(f"\nFetching ASA {test_id}...")
            result = await fetch_zappy_traits(test_id)
            if result:
                print(f"  Name:      {result['name']}")
                print(f"  Image URL: {result['image_url']}")
                s = result['stats']
                print(f"  Stats:     VLT {s['VLT']} | INS {s['INS']} | SPK {s['SPK']}")
            else:
                print(f"  FAILED")

    asyncio.run(test())

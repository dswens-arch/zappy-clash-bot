"""
algorand_lookup.py
------------------
Queries the Algorand indexer to verify a wallet holds a Zappy ASA
and fetches the NFT's traits from its ARC-19 metadata.

Uses the free public Algorand indexer — no API key needed.
"""

import aiohttp
import asyncio
import json
import base64
from stats_engine import calculate_stats, get_hero_stats, get_collab_stats

# ─────────────────────────────────────────────
# Algorand public indexer endpoint
# ─────────────────────────────────────────────
INDEXER_URL = "https://mainnet-idx.algonode.cloud"
ALGOD_URL   = "https://mainnet-api.algonode.cloud"

# IPFS gateway for fetching metadata
IPFS_GATEWAY = "https://ipfs.io/ipfs/"

# ─────────────────────────────────────────────
# Known Zappy ASA IDs (from your CSV)
# Heroes and Collab tracked separately
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

# Unit name prefix for main collection
ZAPPY_UNIT_PREFIX = "ZAPP"


async def verify_wallet_owns_zappy(wallet_address: str) -> dict:
    """
    Check if a wallet holds any Zappy ASA.
    Returns:
      {
        "owns": True/False,
        "zappies": [{"asset_id": int, "unit_name": str, "name": str}],
        "heroes": [{"asset_id": int, "hero_type": str}],
        "collabs": [{"asset_id": int, "collab_type": str}],
      }
    """
    result = {
        "owns": False,
        "zappies": [],
        "heroes": [],
        "collabs": [],
        "error": None,
    }

    try:
        async with aiohttp.ClientSession() as session:
            # Fetch all assets held by this wallet
            url = f"{INDEXER_URL}/v2/accounts/{wallet_address}/assets"
            params = {"limit": 1000}

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    result["error"] = f"Indexer returned status {resp.status}"
                    return result

                data = await resp.json()
                assets = data.get("assets", [])

            # Filter for Zappy assets with balance > 0
            for asset in assets:
                if asset.get("amount", 0) <= 0:
                    continue

                asset_id = asset["asset-id"]

                # Check if it's a Hero
                if asset_id in HERO_ASSET_IDS:
                    result["heroes"].append({
                        "asset_id": asset_id,
                        "hero_type": HERO_ASSET_IDS[asset_id],
                    })
                    result["owns"] = True
                    continue

                # Check if it's a Collab
                if asset_id in COLLAB_ASSET_IDS:
                    result["collabs"].append({
                        "asset_id": asset_id,
                        "collab_type": COLLAB_ASSET_IDS[asset_id],
                    })
                    result["owns"] = True
                    continue

                # Check if it's a main Zappy (we'll verify unit name)
                # We batch lookup asset info to confirm it's a Zappy
                asset_info = await fetch_asset_info(session, asset_id)
                if asset_info and asset_info.get("unit_name", "").startswith(ZAPPY_UNIT_PREFIX):
                    result["zappies"].append({
                        "asset_id": asset_id,
                        "unit_name": asset_info["unit_name"],
                        "name": asset_info["name"],
                    })
                    result["owns"] = True

    except aiohttp.ClientError as e:
        result["error"] = f"Network error: {str(e)}"
    except Exception as e:
        result["error"] = f"Unexpected error: {str(e)}"

    return result


async def fetch_asset_info(session: aiohttp.ClientSession, asset_id: int) -> dict | None:
    """Fetch basic asset info (name, unit-name) from indexer."""
    try:
        url = f"{INDEXER_URL}/v2/assets/{asset_id}"
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            asset = data.get("asset", {}).get("params", {})
            return {
                "unit_name": asset.get("unit-name", ""),
                "name": asset.get("name", ""),
                "reserve": asset.get("reserve", ""),
                "url": asset.get("url", ""),
            }
    except Exception:
        return None


async def fetch_zappy_traits(asset_id: int) -> dict | None:
    """
    Fetches the full trait metadata for a Zappy NFT.
    ARC-19 stores metadata on IPFS — we fetch the reserve address
    which contains the IPFS CID.

    Returns trait dict compatible with stats_engine.calculate_stats()
    """
    try:
        async with aiohttp.ClientSession() as session:
            # Step 1: Get asset info to find reserve address (IPFS CID encoded)
            asset_info = await fetch_asset_info(session, asset_id)
            if not asset_info:
                return None

            unit_name = asset_info.get("unit_name", "")

            # Step 2: Check if it's a Hero or Collab
            if asset_id in HERO_ASSET_IDS:
                hero_type = HERO_ASSET_IDS[asset_id]
                stats = get_hero_stats(hero_type)
                return {
                    "asset_id": asset_id,
                    "name": asset_info["name"],
                    "unit_name": unit_name,
                    "is_hero": True,
                    "hero_type": hero_type,
                    "stats": stats,
                    "traits": {"hero_type": hero_type},
                }

            if asset_id in COLLAB_ASSET_IDS:
                collab_type = COLLAB_ASSET_IDS[asset_id]
                stats = get_collab_stats(collab_type)
                return {
                    "asset_id": asset_id,
                    "name": asset_info["name"],
                    "unit_name": unit_name,
                    "is_collab": True,
                    "collab_type": collab_type,
                    "stats": stats,
                    "traits": {"collab_type": collab_type},
                }

            # Step 3: For main collection, fetch metadata from IPFS via reserve address
            # ARC-19: reserve address encodes the IPFS CID
            reserve = asset_info.get("reserve", "")
            asset_url = asset_info.get("url", "")

            # Try to get metadata from the URL (arc19 scheme)
            traits = await fetch_arc19_metadata(session, asset_url, reserve)

            if not traits:
                return None

            # Step 4: Calculate stats
            stats = calculate_stats(traits)

            return {
                "asset_id": asset_id,
                "name": asset_info["name"],
                "unit_name": unit_name,
                "is_hero": False,
                "is_collab": False,
                "traits": traits,
                "stats": stats,
            }

    except Exception as e:
        print(f"Error fetching traits for {asset_id}: {e}")
        return None


async def fetch_arc19_metadata(
    session: aiohttp.ClientSession,
    asset_url: str,
    reserve_address: str
) -> dict | None:
    """
    ARC-19 metadata is stored on IPFS.
    The reserve address is a base32-encoded IPFS CID.
    We decode it and fetch the JSON from IPFS.
    """
    try:
        # Method 1: Direct IPFS URL in asset URL field
        if asset_url.startswith("ipfs://"):
            cid = asset_url.replace("ipfs://", "").split("/")[0]
            metadata = await fetch_ipfs_json(session, cid)
            if metadata:
                return extract_traits_from_metadata(metadata)

        # Method 2: template-ipfs with reserve address encoding (ARC-19)
        if "template-ipfs" in asset_url and reserve_address:
            cid = decode_reserve_to_cid(reserve_address)
            if cid:
                metadata = await fetch_ipfs_json(session, cid)
                if metadata:
                    return extract_traits_from_metadata(metadata)

        return None

    except Exception as e:
        print(f"Error fetching ARC-19 metadata: {e}")
        return None


def decode_reserve_to_cid(reserve_address: str) -> str | None:
    """
    Decode an Algorand reserve address (base32 encoded) to an IPFS CID.
    ARC-19 stores the multihash in the reserve field.
    """
    try:
        import algosdk
        # Decode the reserve address to public key bytes
        pk_bytes = algosdk.encoding.decode_address(reserve_address)
        # Prepend the multihash prefix for CIDv1 (0x1220 = sha2-256)
        multihash = bytes([0x12, 0x20]) + pk_bytes
        # Encode as base58 to get CIDv0 (Qm...)
        import base58
        cid = base58.b58encode(multihash).decode()
        return cid
    except Exception:
        # Fallback: try direct base32 decode
        try:
            decoded = base64.b32decode(reserve_address + "======")
            return base64.b32encode(decoded).decode().lower()
        except Exception:
            return None


async def fetch_ipfs_json(session: aiohttp.ClientSession, cid: str) -> dict | None:
    """Fetch JSON metadata from IPFS via public gateway."""
    gateways = [
        f"https://ipfs.io/ipfs/{cid}",
        f"https://cloudflare-ipfs.com/ipfs/{cid}",
        f"https://gateway.pinata.cloud/ipfs/{cid}",
    ]
    for url in gateways:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
        except Exception:
            continue
    return None


def extract_traits_from_metadata(metadata: dict) -> dict:
    """
    Extract trait values from ARC-69/ARC-19 metadata JSON.
    Maps to our internal trait key names.
    """
    props = metadata.get("properties", {})

    # Handle both flat and nested formats
    if not props:
        props = metadata.get("attributes", {})

    # If attributes is a list (ARC-69 style)
    if isinstance(props, list):
        props = {item["trait_type"]: item["value"] for item in props if "trait_type" in item}

    return {
        "background": props.get("Background", ""),
        "body":       props.get("Body", ""),
        "earring":    props.get("Earring", "None"),
        "eyes":       props.get("Eyes", ""),
        "eyewear":    props.get("Eyewear", "None"),
        "head":       props.get("Head", ""),
        "mouth":      props.get("Mouth", ""),
        "skin":       props.get("Skin", ""),
    }


# ─────────────────────────────────────────────
# Convenience wrapper for the bot
# ─────────────────────────────────────────────

async def get_zappy_for_battle(asset_id: int) -> dict | None:
    """
    Main entry point for the battle system.
    Returns a fully resolved Zappy with traits + calculated stats,
    or None if lookup fails.
    """
    return await fetch_zappy_traits(asset_id)


async def link_wallet(discord_user_id: str, wallet_address: str) -> dict:
    """
    Verify a Discord user's wallet holds a Zappy and return their collection.
    Used by the /link command.
    """
    result = await verify_wallet_owns_zappy(wallet_address)
    result["discord_user_id"] = discord_user_id
    result["wallet_address"] = wallet_address
    return result


# ─────────────────────────────────────────────
# Test runner
# ─────────────────────────────────────────────
if __name__ == "__main__":
    async def test():
        # Test with a known Zappy asset ID from the CSV
        test_id = 2644039660  # Zappy #1474
        print(f"Fetching traits for ASA {test_id}...")
        result = await fetch_zappy_traits(test_id)
        if result:
            print(f"Name: {result['name']}")
            print(f"Traits: {result['traits']}")
            print(f"Stats: VLT {result['stats']['VLT']} | INS {result['stats']['INS']} | SPK {result['stats']['SPK']}")
        else:
            print("Failed to fetch — check IPFS gateway or network")

    asyncio.run(test())

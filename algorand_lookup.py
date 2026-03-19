"""
algorand_lookup.py
------------------
Queries the Algorand indexer to verify a wallet holds a Zappy ASA
and fetches the NFT's traits and image from its ARC-19 metadata.

Uses the free public Algorand indexer — no API key needed.
"""

import aiohttp
import asyncio
import base64
import re
from stats_engine import calculate_stats, get_hero_stats, get_collab_stats

# ─────────────────────────────────────────────
# Algorand public endpoints
# ─────────────────────────────────────────────
INDEXER_URL  = "https://mainnet-idx.algonode.cloud"
ALGOD_URL    = "https://mainnet-api.algonode.cloud"

# IPFS gateways — tried in order until one responds
IPFS_GATEWAYS = [
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
    "https://dweb.link/ipfs/",
]

# ─────────────────────────────────────────────
# Known special ASA IDs
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

ZAPPY_UNIT_PREFIX = "ZAPP"


# ─────────────────────────────────────────────
# ARC-19 CID decoding
# ─────────────────────────────────────────────

def encode_varint(n: int) -> bytes:
    """Encode an integer as a protobuf-style varint."""
    buf = []
    while True:
        towrite = n & 0x7f
        n >>= 7
        if n:
            buf.append(towrite | 0x80)
        else:
            buf.append(towrite)
            break
    return bytes(buf)


def decode_arc19_reserve(asset_url: str, reserve_address: str) -> str | None:
    """
    Parse an ARC-19 template URL and decode the reserve address into an IPFS CID.

    Template format: template-ipfs://{ipfscid:<version>:<multicodec>:<field>:<hash>}
    Example:         template-ipfs://{ipfscid:1:raw:reserve:sha2-256}

    The reserve address public key bytes ARE the 32-byte digest of the CID multihash.
    """
    try:
        from algosdk import encoding as algo_encoding

        # Parse the template
        match = re.search(r'\{ipfscid:(\d+):([^:]+):([^:]+):([^}]+)\}', asset_url)
        if not match:
            print(f"Could not parse ARC-19 template: {asset_url}")
            return None

        version   = int(match.group(1))   # 0 or 1
        codec_str = match.group(2)         # "raw", "dag-pb", etc.
        hash_type = match.group(4)         # "sha2-256", etc.

        # Decode reserve address to 32-byte digest
        digest = algo_encoding.decode_address(reserve_address)

        if version == 0:
            # CIDv0: always dag-pb + sha2-256, base58btc (Qm...)
            import base58
            multihash = bytes([0x12, 0x20]) + digest
            return base58.b58encode(multihash).decode()

        elif version == 1:
            # CIDv1: build bytes then base32-encode with 'b' multibase prefix
            codec_map = {"raw": 0x55, "dag-pb": 0x70, "dag-cbor": 0x71}
            hash_map  = {"sha2-256": 0x12, "sha2-512": 0x13}

            codec_code = codec_map.get(codec_str, 0x55)
            hash_code  = hash_map.get(hash_type, 0x12)

            multihash = encode_varint(hash_code) + encode_varint(len(digest)) + digest
            cid_bytes = encode_varint(1) + encode_varint(codec_code) + multihash

            # base32 lower, no padding, with 'b' multibase prefix
            cid = 'b' + base64.b32encode(cid_bytes).decode().lower().rstrip('=')
            return cid

        return None

    except Exception as e:
        print(f"Error decoding ARC-19 reserve: {e}")
        return None


def ipfs_to_gateway_url(ipfs_url: str) -> str:
    """Convert ipfs://CID to a usable HTTPS gateway URL."""
    if not ipfs_url:
        return ""
    if ipfs_url.startswith("ipfs://"):
        cid = ipfs_url.replace("ipfs://", "").split("/")[0]
        return f"{IPFS_GATEWAYS[0]}{cid}"
    return ipfs_url  # already an https URL


# ─────────────────────────────────────────────
# IPFS fetch
# ─────────────────────────────────────────────

async def fetch_ipfs_json(session: aiohttp.ClientSession, cid: str) -> dict | None:
    """Fetch JSON metadata from IPFS, trying multiple gateways."""
    for gateway in IPFS_GATEWAYS:
        url = f"{gateway}{cid}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
        except Exception as e:
            print(f"IPFS gateway {gateway} failed: {e}")
            continue
    print(f"All IPFS gateways failed for CID: {cid}")
    return None


# ─────────────────────────────────────────────
# Trait + image extraction
# ─────────────────────────────────────────────

def extract_traits_from_metadata(metadata: dict) -> dict:
    """
    Extract trait values and image URL from ARC-19/ARC-69 metadata JSON.
    Returns trait dict compatible with stats_engine.calculate_stats()
    plus an image_url field.
    """
    props = metadata.get("properties", {})

    # Handle list format (ARC-69 style)
    if isinstance(props, list):
        props = {item["trait_type"]: item["value"] for item in props if "trait_type" in item}

    # Fallback to attributes key
    if not props:
        attrs = metadata.get("attributes", {})
        if isinstance(attrs, list):
            props = {item["trait_type"]: item["value"] for item in attrs if "trait_type" in item}
        else:
            props = attrs

    # Get image URL
    raw_image = metadata.get("image", "")
    image_url = ipfs_to_gateway_url(raw_image)

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
# Asset info fetch
# ─────────────────────────────────────────────

async def fetch_asset_info(session: aiohttp.ClientSession, asset_id: int) -> dict | None:
    """Fetch basic asset info (name, unit-name, reserve, url) from indexer."""
    try:
        url = f"{INDEXER_URL}/v2/assets/{asset_id}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data  = await resp.json()
            asset = data.get("asset", {}).get("params", {})
            return {
                "unit_name": asset.get("unit-name", ""),
                "name":      asset.get("name", ""),
                "reserve":   asset.get("reserve", ""),
                "url":       asset.get("url", ""),
            }
    except Exception as e:
        print(f"Error fetching asset info for {asset_id}: {e}")
        return None


async def fetch_metadata_for_asset(
    session: aiohttp.ClientSession,
    asset_info: dict
) -> dict | None:
    """
    Given asset_info (with url and reserve fields), resolve and fetch
    the IPFS metadata JSON, returning extracted traits + image_url.
    """
    asset_url = asset_info.get("url", "")
    reserve   = asset_info.get("reserve", "")

    # Direct ipfs:// URL
    if asset_url.startswith("ipfs://"):
        cid = asset_url.replace("ipfs://", "").split("/")[0]
        metadata = await fetch_ipfs_json(session, cid)
        if metadata:
            return extract_traits_from_metadata(metadata)

    # ARC-19 template
    if asset_url.startswith("template-ipfs://") and reserve:
        cid = decode_arc19_reserve(asset_url, reserve)
        if cid:
            metadata = await fetch_ipfs_json(session, cid)
            if metadata:
                return extract_traits_from_metadata(metadata)

    return None


# ─────────────────────────────────────────────
# Wallet verification
# ─────────────────────────────────────────────

async def verify_wallet_owns_zappy(wallet_address: str) -> dict:
    """
    Check if a wallet holds any Zappy ASA.
    Returns owned zappies, heroes, and collabs.
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
                    })
                    result["owns"] = True
                    continue

                if asset_id in COLLAB_ASSET_IDS:
                    result["collabs"].append({
                        "asset_id":    asset_id,
                        "collab_type": COLLAB_ASSET_IDS[asset_id],
                    })
                    result["owns"] = True
                    continue

                asset_info = await fetch_asset_info(session, asset_id)
                if asset_info and asset_info.get("unit_name", "").startswith(ZAPPY_UNIT_PREFIX):
                    result["zappies"].append({
                        "asset_id":  asset_id,
                        "unit_name": asset_info["unit_name"],
                        "name":      asset_info["name"],
                    })
                    result["owns"] = True

    except aiohttp.ClientError as e:
        result["error"] = f"Network error: {str(e)}"
    except Exception as e:
        result["error"] = f"Unexpected error: {str(e)}"

    return result


# ─────────────────────────────────────────────
# Full Zappy fetch — traits + stats + image
# ─────────────────────────────────────────────

async def fetch_zappy_traits(asset_id: int) -> dict | None:
    """
    Fetch full metadata for a Zappy: name, image URL, traits, and calculated stats.
    Main entry point used by the battle system and /stats command.
    """
    try:
        async with aiohttp.ClientSession() as session:

            # ── Heroes ──
            if asset_id in HERO_ASSET_IDS:
                hero_type  = HERO_ASSET_IDS[asset_id]
                stats      = get_hero_stats(hero_type)
                asset_info = await fetch_asset_info(session, asset_id)
                image_url  = ""
                if asset_info:
                    meta = await fetch_metadata_for_asset(session, asset_info)
                    if meta:
                        image_url = meta.get("image_url", "")
                return {
                    "asset_id":  asset_id,
                    "name":      asset_info["name"] if asset_info else f"Hero {hero_type}",
                    "unit_name": asset_info["unit_name"] if asset_info else "",
                    "is_hero":   True,
                    "hero_type": hero_type,
                    "stats":     stats,
                    "traits":    {"hero_type": hero_type},
                    "image_url": image_url,
                }

            # ── Collabs ──
            if asset_id in COLLAB_ASSET_IDS:
                collab_type = COLLAB_ASSET_IDS[asset_id]
                stats       = get_collab_stats(collab_type)
                asset_info  = await fetch_asset_info(session, asset_id)
                return {
                    "asset_id":    asset_id,
                    "name":        asset_info["name"] if asset_info else "Shitty Zappy Kitty",
                    "unit_name":   asset_info["unit_name"] if asset_info else "",
                    "is_collab":   True,
                    "collab_type": collab_type,
                    "stats":       stats,
                    "traits":      {"collab_type": collab_type},
                    "image_url":   "",
                }

            # ── Main collection ──
            asset_info = await fetch_asset_info(session, asset_id)
            if not asset_info:
                print(f"Could not fetch asset info for {asset_id}")
                return None

            traits = await fetch_metadata_for_asset(session, asset_info)
            if not traits:
                print(f"Could not fetch IPFS metadata for {asset_id}")
                return None

            stats = calculate_stats(traits)

            return {
                "asset_id":  asset_id,
                "name":      asset_info["name"],
                "unit_name": asset_info["unit_name"],
                "is_hero":   False,
                "is_collab": False,
                "traits":    traits,
                "stats":     stats,
                "image_url": traits.get("image_url", ""),
            }

    except Exception as e:
        print(f"Error fetching Zappy {asset_id}: {e}")
        return None


# ─────────────────────────────────────────────
# Convenience wrappers
# ─────────────────────────────────────────────

async def get_zappy_for_battle(asset_id: int) -> dict | None:
    """Main entry point for the battle system."""
    return await fetch_zappy_traits(asset_id)


async def link_wallet(discord_user_id: str, wallet_address: str) -> dict:
    """Verify a Discord user's wallet and return their collection."""
    result = await verify_wallet_owns_zappy(wallet_address)
    result["discord_user_id"] = discord_user_id
    result["wallet_address"]  = wallet_address
    return result


# ─────────────────────────────────────────────
# Test runner
# ─────────────────────────────────────────────
if __name__ == "__main__":
    async def test():
        test_id = 2644039660  # Zappy #1474
        print(f"Fetching traits for ASA {test_id}...")
        result = await fetch_zappy_traits(test_id)
        if result:
            print(f"Name:   {result['name']}")
            print(f"Image:  {result['image_url']}")
            print(f"Traits: {result['traits']}")
            s = result['stats']
            print(f"Stats:  VLT {s['VLT']} | INS {s['INS']} | SPK {s['SPK']}")
        else:
            print("Failed — check IPFS or network")

    asyncio.run(test())

"""
zappy_import_cog.py

Slash command that takes a newly-minted Zappy ASA ID, pulls its name/image/traits
from the Algorand chain + IPFS, and upserts it into the `extra_zappies` table in
Supabase.

Drop this file alongside your other bot cogs (e.g. next to algo_quota_guard.py)
and load it the same way you load your other extensions:

    await bot.load_extension("zappy_import_cog")

Requires env vars (reuse whatever your bot already has for these):
    SUPABASE_URL
    SUPABASE_KEY
    ALGORAND_INDEXER   (optional, defaults to mainnet-idx.algonode.cloud)

Requires packages: discord.py, aiohttp, supabase (pip install supabase)

If you already have a shared Supabase client singleton elsewhere in the bot,
swap get_supabase() below to import and reuse that instead of creating a new one.
"""

import os
import re
import base64
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp

INDEXER = os.environ.get("ALGORAND_INDEXER", "https://mainnet-idx.algonode.cloud")

# Ordered fallback list. ipfs-pera.algonode.dev goes first — it's the one
# confirmed to reliably serve ARC19 metadata for this collection.
GATEWAYS = [
    "https://ipfs-pera.algonode.dev/ipfs/",
    "https://ipfs.algonode.dev/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
    "https://ipfs.io/ipfs/",
    "https://dweb.link/ipfs/",
    "https://w3s.link/ipfs/",
    "https://4everland.io/ipfs/",
]

ARC19_RE = re.compile(r"\{ipfscid:(\d+):([^:]+):([^:]+):([^}]+)\}")
B32_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
CODEC_MAP = {"raw": 0x55, "dag-pb": 0x70}
HASH_MAP = {"sha2-256": 0x12}


def _varint(n: int) -> bytes:
    buf = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            break
    return bytes(buf)


def decode_arc19(asset_url: str, reserve: str) -> str | None:
    """Decode an ARC19 template URL + reserve address into an IPFS CID (v1, base32)."""
    match = ARC19_RE.search(asset_url)
    if not match:
        return None
    codec = match.group(2)
    hash_name = match.group(4)

    try:
        padded = reserve + "=" * ((8 - len(reserve) % 8) % 8)
        raw = base64.b32decode(padded.upper())
    except Exception:
        return None

    digest = raw[:32]  # first 32 bytes; last 4 are the Algorand address checksum

    multihash = _varint(HASH_MAP.get(hash_name, 0x12)) + _varint(len(digest)) + digest
    cid_bytes = _varint(1) + _varint(CODEC_MAP.get(codec, 0x55)) + multihash

    b32lower = "abcdefghijklmnopqrstuvwxyz234567"
    bits = 0
    bit_count = 0
    out = []
    for byte in cid_bytes:
        bits = (bits << 8) | byte
        bit_count += 8
        while bit_count >= 5:
            bit_count -= 5
            out.append(b32lower[(bits >> bit_count) & 0x1F])
    if bit_count > 0:
        out.append(b32lower[(bits << (5 - bit_count)) & 0x1F])

    return "b" + "".join(out)


async def fetch_json(session: aiohttp.ClientSession, url: str, timeout: int = 10) -> dict | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception:
        return None


async def fetch_metadata(session: aiohttp.ClientSession, cid: str) -> tuple[dict, str] | None:
    for gw in GATEWAYS:
        url = gw + cid
        data = await fetch_json(session, url)
        if data:
            return data, url
    return None


def extract_traits(meta: dict) -> dict:
    """Handle both `properties` (object or array) and `attributes` (array) formats."""
    def to_props(raw):
        if not raw:
            return {}
        if isinstance(raw, list):
            return {
                (p.get("trait_type") or p.get("name")): p.get("value")
                for p in raw if p.get("trait_type") or p.get("name")
            }
        return raw

    merged = {**to_props(meta.get("properties")), **to_props(meta.get("attributes"))}
    lookup = {k.lower(): v for k, v in merged.items()}

    def get(key, default=""):
        return lookup.get(key.lower(), default)

    return {
        "background": get("Background"),
        "body": get("Body"),
        "earring": get("Earring", "None"),
        "eyes": get("Eyes"),
        "eyewear": get("Eyewear", "None"),
        "head": get("Head"),
        "mouth": get("Mouth"),
        "skin": get("Skin"),
    }


def get_supabase():
    """Create a Supabase client from env vars. Swap this out to reuse an existing
    singleton if your bot already has one set up."""
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


class ZappyImportCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="addzappy", description="Fetch a newly minted Zappy's metadata and add it to Supabase")
    @app_commands.describe(asa_id="The ASA ID of the newly minted Zappy")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def addzappy(self, interaction: discord.Interaction, asa_id: int):
        await interaction.response.defer(thinking=True)

        async with aiohttp.ClientSession() as session:
            asset_data = await fetch_json(session, f"{INDEXER}/v2/assets/{asa_id}")
            if not asset_data:
                await interaction.followup.send(f"❌ Couldn't find ASA `{asa_id}` on the indexer.")
                return

            params = asset_data.get("asset", {}).get("params", {})
            name = params.get("name") or f"Zappy #{asa_id}"
            unit_name = params.get("unit-name", "")
            asset_url = params.get("url", "")
            reserve = params.get("reserve", "")

            cid = None
            if asset_url.startswith("template-ipfs://") and reserve:
                cid = decode_arc19(asset_url, reserve)
            elif asset_url.startswith("ipfs://"):
                cid = asset_url.replace("ipfs://", "").split("#")[0].split("?")[0]

            if not cid:
                await interaction.followup.send(f"❌ Couldn't determine metadata CID for ASA `{asa_id}`. Asset URL: `{asset_url}`")
                return

            result = await fetch_metadata(session, cid)
            if not result:
                await interaction.followup.send(
                    f"❌ Metadata CID resolved (`{cid}`) but no gateway served it. "
                    f"It may not be pinned, or this could be a temporary gateway outage."
                )
                return

            meta, source_url = result
            traits = extract_traits(meta)

            image_url = meta.get("image", "")
            if image_url.startswith("ipfs://"):
                image_cid = image_url.replace("ipfs://", "").split("/")[0]
                image_url = GATEWAYS[0] + image_cid

        row = {
            "asset_id": asa_id,
            "name": name,
            "unit_name": unit_name,
            "image_url": image_url,
            **traits,
        }

        try:
            supabase = get_supabase()
            supabase.table("extra_zappies").upsert(row, on_conflict="asset_id").execute()
        except Exception as e:
            await interaction.followup.send(f"❌ Fetched metadata fine but Supabase write failed: `{e}`")
            return

        embed = discord.Embed(title=f"✅ Added {name} ({unit_name})", color=0xF5E642)
        embed.add_field(name="Asset ID", value=str(asa_id), inline=True)
        embed.add_field(name="Source", value=source_url.split("/ipfs/")[0], inline=True)
        for k, v in traits.items():
            embed.add_field(name=k.capitalize(), value=v or "—", inline=True)
        if image_url:
            embed.set_thumbnail(url=image_url)

        await interaction.followup.send(embed=embed)

    @addzappy.error
    async def addzappy_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("You need Manage Server permission to run this.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Error: {error}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ZappyImportCog(bot))

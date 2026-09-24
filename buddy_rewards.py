# -*- coding: utf-8 -*-
"""
buddy_rewards.py
----------------
Handles Zappy Buddy drops for Expedition Zones 3, 4, and 5.

Drop chances:
  Zone 3 - Molten Circuit:  2% per run
  Zone 4 - The Null Space:  4% per run
  Zone 5 - Apex Summit:     5% per run

The buddy pool is NOT registered anywhere. It is computed live:

    pool = assets currently held by the creator wallet
           AND created by BUDDY_MINTER_ADDRESS
           AND not already awarded to someone (pending)

Assets created by any address in BLOCKED_CREATORS are never sent -- this is
enforced again at send time, right before the transfer.

Buddies are sent straight from the creator wallet at /claimnft time.
The Supabase buddy_pool table is only a ledger of awards (pending / claimed);
rows are created automatically when a buddy is awarded.

Env vars:
  CREATOR_WALLET_MNEMONIC   required - wallet that holds and sends the buddies
  BUDDY_MINTER_ADDRESS      optional - overrides the default minter address below
  BUDDY_BLOCKED_CREATORS    optional - comma-separated; overrides the default blocklist below
"""

import random
import asyncio
import os
import time
from datetime import datetime, timezone

import aiohttp
from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk import transaction

ALGOD_URL   = os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud")
ALGOD_TOKEN = os.getenv("ALGOD_TOKEN", "")

BUDDY_DROP_CHANCES = {
    3: 0.02,   # Molten Circuit
    4: 0.04,   # Null Space
    5: 0.05,   # Apex Summit
}

# Fair game: held Zappies whose creator address is this.
DEFAULT_BUDDY_MINTER = "EUCSNIT6VEW5XY5KQN74HIYQBROGT6KB3BPYWXSKST32V5IGWLG7GSCQOA"

# Never sent, no matter what.
DEFAULT_BLOCKED_CREATORS = "UF5DSSCT3GO62CSTSFB4QN5GNKFIMO7HCF2OIY6D57Z37IETEXRKUUNOPU"

BUDDY_MINTER = os.getenv("BUDDY_MINTER_ADDRESS", "").strip() or DEFAULT_BUDDY_MINTER
BLOCKED_CREATORS = {
    a.strip()
    for a in (os.getenv("BUDDY_BLOCKED_CREATORS", "").strip() or DEFAULT_BLOCKED_CREATORS).split(",")
    if a.strip()
}

# Indexer lookups are cached so a drop doesn't hammer the API.
MINTED_TTL = 3600   # what the minter address has created (effectively immutable)
HELD_TTL   = 120    # what the creator wallet currently holds

_minted_cache = {"ts": 0.0, "assets": {}}   # asset_id -> asset name
_held_cache   = {"ts": 0.0, "ids": set()}   # asset ids held with amount > 0
_award_lock   = asyncio.Lock()              # one award at a time


# ---------------------------------------------------------------------------
# Wallets / clients
# ---------------------------------------------------------------------------

def get_algod_client():
    return algod.AlgodClient(ALGOD_TOKEN, ALGOD_URL, headers={"X-Algo-API-Token": ALGOD_TOKEN})


def get_bot_account():
    phrase = os.environ.get("BOT_WALLET_MNEMONIC", "")
    if not phrase:
        raise ValueError("BOT_WALLET_MNEMONIC not set")
    private_key = mnemonic.to_private_key(phrase)
    address     = account.address_from_private_key(private_key)
    return private_key, address


def get_creator_account():
    phrase = os.environ.get("CREATOR_WALLET_MNEMONIC", "")
    if not phrase:
        raise ValueError("CREATOR_WALLET_MNEMONIC not set")
    private_key = mnemonic.to_private_key(phrase)
    address     = account.address_from_private_key(private_key)
    return private_key, address


def send_buddy(receiver: str, asset_id: int, note: str = "") -> str | None:
    """Send 1 unit of a buddy ASA straight from the creator wallet. Returns txid or None."""
    try:
        client = get_algod_client()
        private_key, sender = get_creator_account()
        txn = transaction.AssetTransferTxn(
            sender=sender,
            sp=client.suggested_params(),
            receiver=receiver,
            amt=1,
            index=asset_id,
            note=note.encode()[:1000],
        )
        txid = client.send_transaction(txn.sign(private_key))
        transaction.wait_for_confirmation(client, txid, 4)
        return txid
    except Exception as e:
        print(f"send_buddy failed for asset {asset_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Live pool: creator-wallet holdings filtered by minter address
# ---------------------------------------------------------------------------

async def _paged(session, url: str, key: str) -> list:
    """Fetch every page of an indexer list endpoint."""
    items, token = [], None
    while True:
        params = {"limit": 1000}
        if token:
            params["next"] = token
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"indexer returned {resp.status} for {url}")
            data = await resp.json()
        items.extend(data.get(key, []))
        token = data.get("next-token")
        if not token:
            break
    return items


async def _asset_creator(asset_id: int) -> str | None:
    """Look up an asset's creator address. None if it can't be verified."""
    from algorand_lookup import INDEXER_URL
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{INDEXER_URL}/v2/assets/{asset_id}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("asset", {}).get("params", {}).get("creator")
    except Exception as e:
        print(f"Creator lookup failed for asset {asset_id}: {e}")
        return None


async def get_buddy_candidates() -> list[dict]:
    """
    Every asset the creator wallet currently holds that was created by the
    buddy minter address. Returns [{"asset_id": int, "asset_name": str}, ...].
    """
    from algorand_lookup import INDEXER_URL

    if BUDDY_MINTER in BLOCKED_CREATORS:
        print("Buddy minter is also in the blocklist -- refusing to build a pool.")
        return []

    _, creator_addr = get_creator_account()
    now = time.time()

    async with aiohttp.ClientSession() as session:
        if not _minted_cache["assets"] or now - _minted_cache["ts"] > MINTED_TTL:
            created = await _paged(
                session, f"{INDEXER_URL}/v2/accounts/{BUDDY_MINTER}/created-assets", "assets"
            )
            _minted_cache["assets"] = {
                a["index"]: a.get("params", {}).get("name") or f"Zappy #{a['index']}"
                for a in created
            }
            _minted_cache["ts"] = now

        if now - _held_cache["ts"] > HELD_TTL:
            held = await _paged(
                session, f"{INDEXER_URL}/v2/accounts/{creator_addr}/assets", "assets"
            )
            _held_cache["ids"] = {
                h["asset-id"] for h in held if h.get("amount", 0) > 0
            }
            _held_cache["ts"] = now

    minted = _minted_cache["assets"]
    return [
        {"asset_id": aid, "asset_name": minted[aid]}
        for aid in _held_cache["ids"]
        if aid in minted
    ]


async def get_random_available_buddy() -> dict | None:
    """Pick a random buddy from the live pool, skipping ones already awarded."""
    try:
        candidates = await get_buddy_candidates()
        if not candidates:
            return None

        from database import get_supabase
        db = get_supabase()
        pending = (
            db.table("buddy_pool")
            .select("asset_id")
            .eq("status", "pending")
            .execute()
        ).data or []
        pending_ids = {r["asset_id"] for r in pending}

        candidates = [c for c in candidates if c["asset_id"] not in pending_ids]
        if not candidates:
            return None
        return random.choice(candidates)
    except Exception as e:
        print(f"Error building buddy pool: {e}")
        return None


# ---------------------------------------------------------------------------
# Drops, awards, claims
# ---------------------------------------------------------------------------

def check_buddy_drop(zone_num: int) -> bool:
    """Roll for a Zappy buddy drop in the given zone."""
    chance = BUDDY_DROP_CHANCES.get(zone_num, 0)
    if chance == 0:
        return False
    return random.random() < chance


async def award_buddy(discord_user_id: str, wallet_address: str, zone_num: int) -> dict:
    """
    Award a Zappy buddy to a winner.
    Reserves the asset as pending in buddy_pool so nobody else can be awarded it.
    """
    async with _award_lock:
        buddy = await get_random_available_buddy()
        if not buddy:
            return {
                "success": False,
                "reason":  "no_buddies_available",
                "message": "The buddy pool is empty -- contact the admin!",
            }

        asset_id = buddy["asset_id"]
        name     = buddy["asset_name"]

        try:
            from database import get_supabase
            db = get_supabase()
            db.table("buddy_pool").upsert({
                "asset_id":   asset_id,
                "asset_name": name,
                "status":     "pending",
                "awarded_to": discord_user_id,
                "awarded_at": datetime.now(timezone.utc).isoformat(),
                "claimed_at": None,
                "txid":       None,
            }, on_conflict="asset_id").execute()
        except Exception as e:
            # If we can't reserve it, don't promise it.
            print(f"Error recording buddy award: {e}")
            return {
                "success": False,
                "reason":  "reserve_failed",
                "message": "A buddy was found but couldn't be reserved -- contact the admin!",
            }

    zone_names = {3: "Molten Circuit", 4: "The Null Space", 5: "Apex Summit"}
    zone_name  = zone_names.get(zone_num, f"Zone {zone_num}")

    return {
        "success":   True,
        "asset_id":  asset_id,
        "name":      name,
        "is_buddy":  True,
        "message": (
            f"🐾 **ZAPPY BUDDY FOUND — {name}**\n"
            f"Deep in {zone_name}, a Zappy has chosen you.\n"
            f"ASA ID: `{asset_id}`\n\n"
            f"To claim: add ASA `{asset_id}` to your Algorand wallet in Pera, "
            f"then use `/claimnft` in Discord."
        ),
    }


async def claim_buddy(discord_user_id: str, wallet_address: str) -> dict | None:
    """
    Check if user has a pending buddy claim.
    Returns claim data if found, None if no pending buddy (lets nft_rewards handle it).
    """
    try:
        from database import get_supabase
        db = get_supabase()
        result = (
            db.table("buddy_pool")
            .select("*")
            .eq("awarded_to", discord_user_id)
            .eq("status", "pending")
            .order("awarded_at", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return None

        buddy    = result.data[0]
        asset_id = buddy["asset_id"]
        name     = buddy.get("asset_name") or f"Zappy #{asset_id}"

        # Check opt-in
        from algorand_lookup import INDEXER_URL
        opted_in = False
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{INDEXER_URL}/v2/accounts/{wallet_address}/assets"
                async with session.get(url, params={"asset-id": asset_id},
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data   = await resp.json()
                        assets = data.get("assets", [])
                        opted_in = any(a["asset-id"] == asset_id for a in assets)
        except Exception:
            pass

        if not opted_in:
            return {
                "success": False,
                "is_buddy": True,
                "reason":  "not_opted_in",
                "message": (
                    f"You have a Zappy Buddy waiting -- **{name}** (ASA `{asset_id}`)!\n"
                    f"Add it to your wallet in Pera first, then run `/claimnft` again."
                ),
            }

        # Hard guard: never send anything created by a blocked address.
        # Fails closed -- if the creator can't be verified, nothing is sent.
        creator = await _asset_creator(asset_id)
        if creator is None:
            return {
                "success":  False,
                "is_buddy": True,
                "reason":   "creator_unverified",
                "message":  "Couldn't verify your buddy right now -- please try `/claimnft` again in a minute.",
            }
        if creator in BLOCKED_CREATORS:
            print(f"BLOCKED send: asset {asset_id} is created by blocked address {creator}")
            return {
                "success":  False,
                "is_buddy": True,
                "reason":   "blocked_creator",
                "message":  "This buddy can't be sent -- contact the admin and they will sort it out.",
            }

        # Send the buddy from the creator wallet
        note = f"Zappy Buddy from Expedition -- {name}"
        txid = await asyncio.to_thread(send_buddy, wallet_address, asset_id, note)

        if not txid:
            # Fallback for buddies still sitting in the bot wallet from the old flow.
            # Safe to delete once no pending awards live there.
            from nft_rewards import send_nft
            txid = await asyncio.to_thread(send_nft, wallet_address, asset_id, note)

        if txid:
            _held_cache["ids"].discard(asset_id)   # don't re-offer it from a stale cache
            db.table("buddy_pool").update({
                "status":     "claimed",
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "txid":       txid,
            }).eq("asset_id", asset_id).execute()

            return {
                "success":  True,
                "is_buddy": True,
                "asset_id": asset_id,
                "name":     name,
                "txid":     txid,
                "message":  f"🐾 **{name}** has been sent to your wallet! Your new Zappy buddy is home.",
            }
        else:
            return {
                "success":  False,
                "is_buddy": True,
                "reason":   "transfer_failed",
                "message":  "Transfer failed -- contact the admin and they will sort it out.",
            }

    except Exception as e:
        print(f"Error in claim_buddy: {e}")
        return None

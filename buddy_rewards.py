# -*- coding: utf-8 -*-
"""
buddy_rewards.py
----------------
Handles Zappy Buddy drops for Expedition Zones 3, 4, and 5.

Drop chances:
  Zone 3 - Molten Circuit:  2% per run
  Zone 4 - The Null Space:  4% per run
  Zone 5 - Apex Summit:     5% per run

Buddy pool is managed manually in Supabase buddy_pool table.
Claim flow uses /claimnft -- same as NFT prizes but message distinguishes buddy vs prize.
"""

import random
import asyncio
import os
from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk import transaction

ALGOD_URL   = "https://mainnet-api.algonode.cloud"
ALGOD_TOKEN = ""

BUDDY_DROP_CHANCES = {
    3: 0.02,   # Molten Circuit
    4: 0.04,   # Null Space
    5: 0.05,   # Apex Summit
}


def get_algod_client():
    return algod.AlgodClient(ALGOD_TOKEN, ALGOD_URL)


def get_bot_account():
    phrase = os.environ.get("BOT_WALLET_MNEMONIC", "")
    if not phrase:
        raise ValueError("BOT_WALLET_MNEMONIC not set")
    private_key = mnemonic.to_private_key(phrase)
    address     = account.address_from_private_key(private_key)
    return private_key, address


def check_buddy_drop(zone_num: int) -> bool:
    """Roll for a Zappy buddy drop in the given zone."""
    chance = BUDDY_DROP_CHANCES.get(zone_num, 0)
    if chance == 0:
        return False
    return random.random() < chance


def get_random_available_buddy() -> dict | None:
    """Pick a random available buddy from the Supabase pool."""
    try:
        from database import get_supabase
        db = get_supabase()
        result = (
            db.table("buddy_pool")
            .select("*")
            .eq("status", "available")
            .execute()
        )
        available = result.data or []
        if not available:
            return None
        return random.choice(available)
    except Exception as e:
        print(f"Error fetching buddy pool: {e}")
        return None


async def award_buddy(discord_user_id: str, wallet_address: str, zone_num: int) -> dict:
    """
    Award a Zappy buddy to a winner.
    Records as pending in buddy_pool table.
    Returns instructions for the winner.
    """
    buddy = get_random_available_buddy()
    if not buddy:
        return {
            "success": False,
            "reason":  "no_buddies_available",
            "message": "The buddy pool is empty -- contact the admin!",
        }

    asset_id = buddy["asset_id"]
    name     = buddy.get("asset_name", f"Zappy #{asset_id}")

    try:
        from database import get_supabase
        from datetime import datetime, timezone
        db = get_supabase()
        db.table("buddy_pool").update({
            "status":     "pending",
            "awarded_to": discord_user_id,
            "awarded_at": datetime.now(timezone.utc).isoformat(),
        }).eq("asset_id", asset_id).execute()
    except Exception as e:
        print(f"Error recording buddy award: {e}")

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
        name     = buddy.get("asset_name", f"Zappy #{asset_id}")

        # Check opt-in
        import aiohttp
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

        # Send the buddy NFT
        from nft_rewards import send_nft
        note = f"Zappy Buddy from Expedition -- {name}"
        txid = await asyncio.to_thread(send_nft, wallet_address, asset_id, note)

        if txid:
            from database import get_supabase
            from datetime import datetime, timezone
            db = get_supabase()
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

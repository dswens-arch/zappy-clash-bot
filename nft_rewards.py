"""
nft_rewards.py
--------------
Handles NFT prize drops for Zappy Expedition Zone 5.

How it works:
  1. You send prize NFTs into the bot reward wallet manually
  2. When a Zone 5 run triggers a drop, the bot picks a random NFT from its wallet
  3. The win is recorded in the database with the specific ASA ID
  4. The winner gets a DM/message telling them to opt in and use /claimnft
  5. Once they opt in, /claimnft sends the NFT to their wallet

Env vars required (same as token_rewards.py):
  BOT_WALLET_MNEMONIC  — bot reward wallet mnemonic
  REWARD_TOKEN_ID      — ZAPP token ASA ID (already set)
"""

import os
import asyncio
from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk import transaction

ALGOD_URL   = "https://mainnet-api.algonode.cloud"
ALGOD_TOKEN = ""
INDEXER_URL = "https://mainnet-idx.algonode.cloud"

# ASAs to exclude from the prize pool — token and known non-prize assets
EXCLUDED_ASSET_IDS = {
    int(os.environ.get("REWARD_TOKEN_ID", "2572874483")),   # ZAPP token
    0,   # ALGO
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


async def get_available_nfts() -> list:
    """
    Query the bot wallet for all NFTs available as prizes.
    Returns list of dicts: { asset_id, name, amount }
    Excludes ZAPP token and any asset with amount < 1.
    """
    import aiohttp
    try:
        _, bot_address = get_bot_account()
        url = f"{INDEXER_URL}/v2/accounts/{bot_address}/assets"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"limit": 1000},
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return []
                data   = await resp.json()
                assets = data.get("assets", [])

        nfts = []
        for a in assets:
            asset_id = a["asset-id"]
            amount   = a.get("amount", 0)
            if amount < 1:
                continue
            if asset_id in EXCLUDED_ASSET_IDS:
                continue
            # Fetch asset info to get name
            async with aiohttp.ClientSession() as session:
                info_url = f"{INDEXER_URL}/v2/assets/{asset_id}"
                async with session.get(info_url,
                                       timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        info   = await resp.json()
                        params = info.get("asset", {}).get("params", {})
                        name   = params.get("name", f"ASA {asset_id}")
                        total  = params.get("total", 0)
                        # Only include NFTs (total supply = 1) not fungible tokens
                        if total == 1:
                            nfts.append({
                                "asset_id": asset_id,
                                "name":     name,
                                "amount":   amount,
                            })
        return nfts

    except Exception as e:
        print(f"Error fetching available NFTs: {e}")
        return []


async def pick_random_nft() -> dict | None:
    """Pick a random NFT from the bot wallet prize pool."""
    import random
    available = await get_available_nfts()
    if not available:
        print("No NFTs available in prize pool")
        return None
    return random.choice(available)


async def check_nft_opt_in(wallet_address: str, asset_id: int) -> bool:
    """Check if a wallet has opted in to a specific NFT."""
    import aiohttp
    try:
        url = f"{INDEXER_URL}/v2/accounts/{wallet_address}/assets"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"asset-id": asset_id},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return False
                data   = await resp.json()
                assets = data.get("assets", [])
                return any(a["asset-id"] == asset_id for a in assets)
    except Exception as e:
        print(f"Error checking NFT opt-in: {e}")
        return False


def send_nft(recipient_address: str, asset_id: int, note: str = "") -> str | None:
    """
    Send an NFT to a recipient.
    Returns transaction ID on success, None on failure.
    Synchronous — wrap in asyncio.to_thread() for async contexts.
    """
    try:
        client      = get_algod_client()
        private_key, sender = get_bot_account()
        params      = client.suggested_params()

        txn = transaction.AssetTransferTxn(
            sender   = sender,
            sp       = params,
            receiver = recipient_address,
            amt      = 1,
            index    = asset_id,
            note     = note.encode() if note else None,
        )
        signed = txn.sign(private_key)
        txid   = client.send_transaction(signed)
        transaction.wait_for_confirmation(client, txid, 4)
        print(f"NFT {asset_id} sent to {recipient_address[:8]}... txid={txid}")
        return txid

    except Exception as e:
        print(f"Error sending NFT {asset_id} to {recipient_address}: {e}")
        return None


async def award_nft_prize(
    discord_user_id: str,
    wallet_address:  str,
    source:          str = "expedition",
) -> dict:
    """
    Full NFT prize flow:
    1. Pick a random NFT from the bot wallet
    2. Record the pending prize in Supabase
    3. Return instructions for the winner

    The actual transfer happens in /claimnft once the winner opts in.
    """
    nft = await pick_random_nft()
    if not nft:
        return {
            "success": False,
            "reason":  "no_nfts_available",
            "message": "The prize pool is currently empty — contact the admin!",
        }

    asset_id = nft["asset_id"]
    name     = nft["name"]

    # Save pending prize to database
    try:
        from database import get_supabase
        from datetime import datetime, timezone
        db = get_supabase()
        db.table("nft_prizes").insert({
            "discord_user_id": discord_user_id,
            "wallet_address":  wallet_address,
            "asset_id":        asset_id,
            "asset_name":      name,
            "status":          "pending",
            "source":          source,
            "awarded_at":      datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"Error saving NFT prize: {e}")
        # Still tell the user they won — we can fix DB issues manually
        pass

    return {
        "success":  True,
        "asset_id": asset_id,
        "name":     name,
        "message": (
            f"🎉 **NFT DROP: {name}**\n"
            f"ASA ID: `{asset_id}`\n\n"
            f"To claim: add ASA `{asset_id}` to your Algorand wallet "
            f"(search for it in Pera Wallet), then use `/claimnft` in Discord."
        ),
    }


async def claim_nft_prize(discord_user_id: str, wallet_address: str) -> dict:
    """
    Called by /claimnft — checks for a pending prize, verifies opt-in,
    and sends the NFT if everything checks out.
    """
    try:
        from database import get_supabase
        from datetime import datetime, timezone
        db = get_supabase()

        # Find pending prize for this user
        result = (
            db.table("nft_prizes")
            .select("*")
            .eq("discord_user_id", discord_user_id)
            .eq("status", "pending")
            .order("awarded_at", desc=True)
            .limit(1)
            .execute()
        )

        if not result.data:
            return {
                "success": False,
                "reason":  "no_pending_prize",
                "message": "You don't have a pending NFT prize. Win a Zone 5 expedition to earn one!",
            }

        prize    = result.data[0]
        asset_id = prize["asset_id"]
        name     = prize["asset_name"]

        # Check opt-in
        opted_in = await check_nft_opt_in(wallet_address, asset_id)
        if not opted_in:
            return {
                "success": False,
                "reason":  "not_opted_in",
                "message": (
                    f"You haven't opted in to **{name}** (ASA `{asset_id}`) yet.\n"
                    f"Add it to your wallet in Pera, then run `/claimnft` again."
                ),
            }

        # Send the NFT
        note = f"Zappy Expedition Zone 5 prize — {name}"
        txid = await asyncio.to_thread(send_nft, wallet_address, asset_id, note)

        if txid:
            # Mark as claimed
            db.table("nft_prizes").update({
                "status":     "claimed",
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "txid":       txid,
            }).eq("id", prize["id"]).execute()

            return {
                "success":  True,
                "asset_id": asset_id,
                "name":     name,
                "txid":     txid,
                "source":   prize.get("source", "expedition"),
                "message":  f"✅ **{name}** has been sent to your wallet! Check Pera to confirm.",
            }
        else:
            return {
                "success": False,
                "reason":  "transfer_failed",
                "message": "Transfer failed — contact the admin and they'll sort it out manually.",
            }

    except Exception as e:
        print(f"Error in claim_nft_prize: {e}")
        return {
            "success": False,
            "reason":  "error",
            "message": "Something went wrong — contact the admin.",
        }

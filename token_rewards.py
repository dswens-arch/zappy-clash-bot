"""
token_rewards.py
----------------
Handles sending Zappy token (ASA 2572874483) rewards to bracket winners.

The bot wallet is funded with tokens and signs transfers after each match.
Recipients must have opted in to the token before they can receive it.

Env vars required:
  BOT_WALLET_MNEMONIC  — 25-word mnemonic for the bot's reward wallet
  REWARD_TOKEN_ID      — ASA ID of the reward token (2572874483)
"""

import os
import asyncio
from algosdk import account, mnemonic
from algosdk.v2client import algod
from algosdk import transaction

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
ALGOD_URL     = "https://mainnet-api.algonode.cloud"
ALGOD_TOKEN   = ""   # Not required for algonode public endpoint
INDEXER_URL   = "https://mainnet-idx.algonode.cloud"

REWARD_TOKEN_ID = int(os.environ.get("REWARD_TOKEN_ID", "2572874483"))

# ─────────────────────────────────────────────
# Token reward amounts
# ─────────────────────────────────────────────
REWARD_WIN           = 100    # Standard bracket win
REWARD_UPSET_BONUS   =  50    # Extra for an upset win
REWARD_CHAMPION      = 500    # Winning the full bracket
REWARD_STREAK_7      = 200    # 7-day streak milestone
REWARD_STREAK_30     = 500    # 30-day streak milestone

# Evening session multiplier (1.25x, rounded)
EVENING_MULTIPLIER   = 1.25


def get_algod_client():
    """Return an Algorand algod client."""
    return algod.AlgodClient(ALGOD_TOKEN, ALGOD_URL)


def get_bot_account():
    """
    Load the bot wallet from the BOT_WALLET_MNEMONIC env var.
    Returns (private_key, address) tuple.
    """
    phrase = os.environ.get("BOT_WALLET_MNEMONIC", "")
    if not phrase:
        raise ValueError("BOT_WALLET_MNEMONIC not set in environment variables")
    private_key = mnemonic.to_private_key(phrase)
    address = account.address_from_private_key(private_key)
    return private_key, address


async def check_opted_in(wallet_address: str, asset_id: int) -> bool:
    """
    Check if a wallet has opted in to the reward token.
    Returns True if opted in, False if not.
    """
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
        print(f"Error checking opt-in for {wallet_address}: {e}")
        return False


async def get_bot_token_balance() -> int:
    """Return the bot wallet's current token balance."""
    import aiohttp
    try:
        _, bot_address = get_bot_account()
        url = f"{INDEXER_URL}/v2/accounts/{bot_address}/assets"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"asset-id": REWARD_TOKEN_ID},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return 0
                data   = await resp.json()
                assets = data.get("assets", [])
                for a in assets:
                    if a["asset-id"] == REWARD_TOKEN_ID:
                        return a.get("amount", 0)
                return 0
    except Exception as e:
        print(f"Error checking bot balance: {e}")
        return 0


def send_token_reward(recipient_address: str, amount: int, note: str = "") -> str | None:
    """
    Send `amount` of the reward token to `recipient_address`.
    Returns the transaction ID on success, None on failure.

    This is a synchronous call — wrap in asyncio.to_thread() for async contexts.
    """
    try:
        client = get_algod_client()
        private_key, sender = get_bot_account()

        # Get suggested params
        params = client.suggested_params()

        # Build the asset transfer transaction
        txn = transaction.AssetTransferTxn(
            sender=sender,
            sp=params,
            receiver=recipient_address,
            amt=amount,
            index=REWARD_TOKEN_ID,
            note=note.encode() if note else None,
        )

        # Sign and send
        signed = txn.sign(private_key)
        txid   = client.send_transaction(signed)

        # Wait for confirmation
        transaction.wait_for_confirmation(client, txid, 4)
        print(f"Token reward sent: {amount} to {recipient_address[:8]}... txid={txid}")
        return txid

    except Exception as e:
        print(f"Error sending token reward to {recipient_address}: {e}")
        return None


async def award_win_tokens(
    discord_user_id: str,
    wallet_address:  str,
    is_upset:        bool = False,
    is_champion:     bool = False,
    is_evening:      bool = False,
) -> dict:
    """
    Award tokens for a bracket win.
    Checks opt-in status first — returns a result dict with status and amount.
    """
    # Check opt-in
    opted_in = await check_opted_in(wallet_address, REWARD_TOKEN_ID)
    if not opted_in:
        return {
            "success":  False,
            "reason":   "not_opted_in",
            "amount":   0,
            "message":  f"You need to opt in to ASA {REWARD_TOKEN_ID} to receive token rewards! Add the asset in your Algorand wallet first.",
        }

    # Calculate amount
    if is_champion:
        amount = REWARD_CHAMPION
    else:
        amount = REWARD_WIN
        if is_upset:
            amount += REWARD_UPSET_BONUS

    if is_evening:
        amount = int(amount * EVENING_MULTIPLIER)

    # Check bot has enough balance
    balance = await get_bot_token_balance()
    if balance < amount:
        print(f"Bot wallet low on tokens! Balance: {balance}, needed: {amount}")
        return {
            "success": False,
            "reason":  "insufficient_balance",
            "amount":  0,
            "message": "Reward wallet is running low — contact the admin!",
        }

    # Send the tokens
    note = f"Zappy Clash reward - {'Champion' if is_champion else 'Win'}"
    txid = await asyncio.to_thread(send_token_reward, wallet_address, amount, note)

    if txid:
        return {
            "success": True,
            "amount":  amount,
            "txid":    txid,
            "message": f"🪙 **+{amount:,} tokens** sent to your wallet!",
        }
    else:
        return {
            "success": False,
            "reason":  "transaction_failed",
            "amount":  0,
            "message": "Token transfer failed — contact the admin.",
        }


async def award_streak_tokens(wallet_address: str, streak_days: int) -> dict:
    """Award tokens for hitting a streak milestone."""
    amounts = {7: REWARD_STREAK_7, 30: REWARD_STREAK_30}
    amount  = amounts.get(streak_days)
    if not amount:
        return {"success": False, "amount": 0}

    opted_in = await check_opted_in(wallet_address, REWARD_TOKEN_ID)
    if not opted_in:
        return {
            "success": False,
            "reason":  "not_opted_in",
            "amount":  0,
            "message": f"Opt in to ASA {REWARD_TOKEN_ID} to receive streak rewards!",
        }

    note = f"Zappy Clash - {streak_days} day streak reward"
    txid = await asyncio.to_thread(send_token_reward, wallet_address, amount, note)

    if txid:
        return {
            "success": True,
            "amount":  amount,
            "txid":    txid,
            "message": f"🪙 **+{amount:,} tokens** for your {streak_days}-day streak!",
        }
    return {"success": False, "amount": 0}

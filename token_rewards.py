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
ALGOD_URL     = os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud")
ALGOD_TOKEN   = os.getenv("ALGOD_TOKEN", "")
INDEXER_URL   = os.getenv("INDEXER_URL", "https://mainnet-idx.algonode.cloud")

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
    return algod.AlgodClient(ALGOD_TOKEN, ALGOD_URL, headers={"X-Algo-API-Token": ALGOD_TOKEN})


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


class AlgorandUnavailable(Exception):
    """The chain lookup couldn't be completed (quota block, 403, indexer error).
    Distinct from 'not opted in' / 'balance is 0' so callers don't report a
    lookup failure to players as a fact about their wallet."""


_PAUSED_RESULT = {
    "success": False,
    "reason":  "algorand_paused",
    "amount":  0,
    "message": "⏸️ Token rewards are paused while Algorand lookups are rate-limited. "
               "Your win still counts; rewards resume once the limit resets.",
}


async def check_opted_in(wallet_address: str, asset_id: int) -> bool:
    """
    Check if a wallet has opted in to the reward token.
    Returns True/False; raises AlgorandUnavailable if it couldn't be determined.
    """
    return await _opt_in_present(wallet_address, asset_id)


async def _opt_in_present(wallet_address: str, asset_id: int) -> bool:
    """Guard-aware presence check (an opted-in wallet with 0 balance still lists the asset)."""
    import aiohttp
    from algo_quota_guard import is_quota_blocked, mark_quota_exceeded, record_call
    from algorand_lookup import is_opt_in_cached, cache_opt_in
    if is_opt_in_cached(wallet_address, asset_id):
        return True          # opted-in is permanent in practice; never re-look-up
    if is_quota_blocked():
        raise AlgorandUnavailable("quota_blocked")
    url = f"{INDEXER_URL}/v2/accounts/{wallet_address}/assets"
    record_call()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"asset-id": asset_id},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 403:
                    mark_quota_exceeded(detail="token_rewards.opt_in 403")
                    raise AlgorandUnavailable("quota_blocked")
                if resp.status != 200:
                    raise AlgorandUnavailable(f"indexer_{resp.status}")
                data = await resp.json()
                opted = any(a["asset-id"] == asset_id for a in data.get("assets", []))
                if opted:
                    cache_opt_in(wallet_address, asset_id)
                return opted
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        raise AlgorandUnavailable(f"network_error: {e}")


async def get_bot_token_balance() -> int:
    """Return the bot wallet's current token balance.
    Raises AlgorandUnavailable if it couldn't be read (never returns a fake 0)."""
    from algorand_lookup import get_asset_balance
    _, bot_address = get_bot_account()
    balance, err = await get_asset_balance(bot_address, REWARD_TOKEN_ID,
                                           max_age=120, allow_stale=False)
    if balance is None:
        raise AlgorandUnavailable(err)
    return balance


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
    try:
        opted_in = await check_opted_in(wallet_address, REWARD_TOKEN_ID)
    except AlgorandUnavailable as e:
        print(f"[token_rewards] win reward skipped, Algorand unavailable: {e}")
        return dict(_PAUSED_RESULT)
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
    try:
        balance = await get_bot_token_balance()
    except AlgorandUnavailable as e:
        print(f"[token_rewards] win reward skipped, Algorand unavailable: {e}")
        return dict(_PAUSED_RESULT)
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
        from algorand_lookup import bump_cached_asset_balance
        bump_cached_asset_balance(wallet_address, REWARD_TOKEN_ID, amount)
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

    try:
        opted_in = await check_opted_in(wallet_address, REWARD_TOKEN_ID)
    except AlgorandUnavailable as e:
        print(f"[token_rewards] streak reward skipped, Algorand unavailable: {e}")
        return dict(_PAUSED_RESULT)
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
        from algorand_lookup import bump_cached_asset_balance
        bump_cached_asset_balance(wallet_address, REWARD_TOKEN_ID, amount)
        return {
            "success": True,
            "amount":  amount,
            "txid":    txid,
            "message": f"🪙 **+{amount:,} tokens** for your {streak_days}-day streak!",
        }
    return {"success": False, "amount": 0}

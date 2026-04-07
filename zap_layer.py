"""
zap_layer.py
Zappy Grand Prix — ZAPP token transaction layer (on-chain ASA)

ZAPP is ASA 2572874483 on Algorand mainnet.
Entry and payout work exactly like the ALGO board but use ASA transfers
instead of native ALGO payments.

Economics:
    Entry:   500 ZAPP each
    Winner:  1,000 ZAPP (full pot, no rake)
"""

import os
import io
import asyncio
import time
import base64
from urllib.parse import urlencode
from typing import Optional, Callable

from algosdk import mnemonic, account, transaction
from algosdk.v2client import algod, indexer

import qrcode
from qrcode.image.pure import PyPNGImage

# Reuse algod/indexer client factories from algo_layer
from algo_layer import get_algod_client, get_indexer_client, get_bot_account, get_bot_address


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ZAPP_ASA_ID      = int(os.getenv("REWARD_TOKEN_ID", "2572874483"))
ZAP_ENTRY        = 500      # ZAPP units (whole tokens, not microunits — ZAPP has 0 decimals)
ZAP_PAYOUT       = 1000
ZAP_WIN_BONUS    = 0        # No bonus — payout IS the reward
ZAP_LOSE_BONUS   = 0

PAYMENT_TIMEOUT  = 180      # seconds
POLL_INTERVAL    = 5


# ---------------------------------------------------------------------------
# Balance check — reads from chain via indexer
# ---------------------------------------------------------------------------

def get_zapp_balance(wallet_address: str) -> int:
    """Return a wallet's ZAPP ASA balance. Returns 0 if not opted in."""
    try:
        client = get_algod_client()
        info   = client.account_info(wallet_address)
        for asset in info.get("assets", []):
            if asset["asset-id"] == ZAPP_ASA_ID:
                return asset["amount"]
        return 0
    except Exception as e:
        print(f"[zap_layer] Balance check error: {e}")
        return 0


async def can_afford_entry(db, discord_user_id: str) -> bool:
    """Check if a player's wallet has enough ZAPP to enter."""
    from race_engine import get_all_racers
    racers = await get_all_racers(db, discord_user_id)
    if not racers:
        return False
    wallet = racers[0]["wallet_address"]
    return get_zapp_balance(wallet) >= ZAP_ENTRY


# ---------------------------------------------------------------------------
# Deep link + QR generation (ASA version)
# ---------------------------------------------------------------------------

def build_payment_note(duel_id: str) -> str:
    return f"zgp:{duel_id}"


def build_pera_zapp_uri(
    bot_address: str,
    duel_id: str,
    amount: int = ZAP_ENTRY,
) -> str:
    """
    Pera Wallet deep link for an ASA transfer.
    Opens send screen pre-filled with ZAPP asset, amount, and note.
    """
    note_b64 = base64.b64encode(build_payment_note(duel_id).encode()).decode()
    params = urlencode({
        "receiver":  bot_address,
        "amount":    amount,
        "asset":     ZAPP_ASA_ID,
        "note":      note_b64,
        "xnote":     "1",
    })
    return f"perawallet://send?{params}"


def build_algorand_uri(
    bot_address: str,
    duel_id: str,
    amount: int = ZAP_ENTRY,
) -> str:
    """
    ARC-0026 algorand:// URI for ASA transfer.
    Supported by Pera, Defly, and other wallets.
    """
    note_b64 = base64.b64encode(build_payment_note(duel_id).encode()).decode()
    params = urlencode({
        "amount":    0,           # native ALGO amount = 0 for ASA transfers
        "asset":     ZAPP_ASA_ID,
        "amount_asset": amount,
        "note":      note_b64,
    })
    return f"algorand://{bot_address}?{params}"


def generate_qr_png(uri: str) -> io.BytesIO:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(image_factory=PyPNGImage)
    buf = io.BytesIO()
    img.save(buf)
    buf.seek(0)
    return buf


def build_payment_ui(duel_id: str) -> dict:
    """
    Build everything the Discord command needs for the ZAPP payment step.
    Returns algo_uri, pera_uri, qr_buf, note, instructions.
    """
    bot_address = get_bot_address()
    algo_uri    = build_algorand_uri(bot_address, duel_id)
    pera_uri    = build_pera_zapp_uri(bot_address, duel_id)
    qr_buf      = generate_qr_png(pera_uri)
    note        = build_payment_note(duel_id)

    instructions = (
        f"**Send 500 ZAPP to enter the race**\n\n"
        f"📱 **Mobile** — tap a button below to open Pera Wallet. "
        f"The asset, amount, and note are pre-filled. Just approve.\n\n"
        f"🖥️ **Desktop** — scan the QR code with Pera on your phone.\n\n"
        f"*Note field:* `{note}` *(do not change this)*\n"
        f"⏳ Challenge expires in **3 minutes**."
    )

    return {
        "bot_address":  bot_address,
        "algo_uri":     algo_uri,
        "pera_uri":     pera_uri,
        "qr_buf":       qr_buf,
        "note":         note,
        "instructions": instructions,
    }


def make_payment_view(algo_uri: str, pera_uri: str):
    """Returns a discord.ui.View with Pera and universal wallet link buttons."""
    import discord

    class ZappPaymentView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.add_item(discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="Open in Pera Wallet",
                url=pera_uri,
                emoji="⚡",
                row=0,
            ))
            self.add_item(discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="Open in Other Wallet",
                url=algo_uri,
                emoji="💳",
                row=0,
            ))

    return ZappPaymentView()


# ---------------------------------------------------------------------------
# Payment verification via indexer
# ---------------------------------------------------------------------------

def find_payment_txn(
    sender_address: str,
    receiver_address: str,
    amount: int,
    after_round: int,
    expected_note: str,
) -> Optional[str]:
    """
    Search indexer for a confirmed ZAPP ASA transfer matching all criteria.
    Returns txid if found, None otherwise.
    """
    idx          = get_indexer_client()
    expected_b64 = base64.b64encode(expected_note.encode()).decode()

    try:
        response = idx.search_transactions(
            address=sender_address,
            address_role="sender",
            txn_type="axfer",           # ASA transfer type
            asset_id=ZAPP_ASA_ID,
            min_round=after_round,
        )
    except Exception as e:
        print(f"[zap_layer] Indexer search error: {e}")
        return None

    for txn in response.get("transactions", []):
        axfer = txn.get("asset-transfer-transaction", {})

        if axfer.get("receiver") != receiver_address:
            continue
        if axfer.get("asset-id") != ZAPP_ASA_ID:
            continue
        if axfer.get("amount", 0) < amount:
            continue

        raw_note = txn.get("note", "")
        if raw_note != expected_b64:
            continue

        return txn["id"]

    return None


async def wait_for_payment(
    sender_address: str,
    duel_id: str,
    after_round: int,
    on_found: Optional[Callable] = None,
    on_timeout: Optional[Callable] = None,
) -> Optional[str]:
    """
    Poll indexer every 5 seconds for a matching ZAPP payment.
    Returns txid on success, None on timeout.
    """
    bot_address   = get_bot_address()
    expected_note = build_payment_note(duel_id)
    deadline      = time.monotonic() + PAYMENT_TIMEOUT

    while time.monotonic() < deadline:
        txid = find_payment_txn(
            sender_address=sender_address,
            receiver_address=bot_address,
            amount=ZAP_ENTRY,
            after_round=after_round,
            expected_note=expected_note,
        )
        if txid:
            if on_found:
                await on_found(txid)
            return txid
        await asyncio.sleep(POLL_INTERVAL)

    if on_timeout:
        await on_timeout()
    return None


# ---------------------------------------------------------------------------
# Sending ZAPP from bot wallet
# ---------------------------------------------------------------------------

def _send_zapp(receiver: str, amount: int, note: str) -> str:
    """
    Send ZAPP ASA from bot wallet to receiver.
    Bot must be opted into ZAPP ASA.
    Returns txid.
    """
    private_key, bot_address = get_bot_account()
    client = get_algod_client()
    params = client.suggested_params()

    txn = transaction.AssetTransferTxn(
        sender=bot_address,
        sp=params,
        receiver=receiver,
        amt=amount,
        index=ZAPP_ASA_ID,
        note=note.encode("utf-8"),
    )

    signed = txn.sign(private_key)
    txid   = client.send_transaction(signed)
    transaction.wait_for_confirmation(client, txid, 10)
    return txid


def send_payout(winner_address: str, duel_id: str) -> str:
    """Send 1,000 ZAPP to winner. Returns txid."""
    # Check bot has enough ZAPP
    bot_address = get_bot_address()
    bot_balance = get_zapp_balance(bot_address)
    if bot_balance < ZAP_PAYOUT:
        raise RuntimeError(
            f"Bot ZAPP balance too low: {bot_balance} < {ZAP_PAYOUT}"
        )
    return _send_zapp(winner_address, ZAP_PAYOUT, f"zgp:payout:{duel_id}")


def send_refund(player_address: str, duel_id: str) -> str:
    """Refund 500 ZAPP to a player after duel expiry. Returns txid."""
    return _send_zapp(player_address, ZAP_ENTRY, f"zgp:refund:{duel_id}")


# ---------------------------------------------------------------------------
# Opt-in check — player must be opted into ZAPP ASA to receive it
# ---------------------------------------------------------------------------

def is_opted_in(wallet_address: str) -> bool:
    """Check if a wallet is opted into the ZAPP ASA."""
    try:
        client = get_algod_client()
        info   = client.account_info(wallet_address)
        return any(a["asset-id"] == ZAPP_ASA_ID for a in info.get("assets", []))
    except Exception:
        return False

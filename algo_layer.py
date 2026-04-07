"""
algo_layer.py
Zappy Grand Prix — Algorand transaction layer

Handles:
- Deep link URI generation (algorand://) for wallet apps
- QR code generation for desktop fallback
- Watching for incoming ALGO payments via indexer polling
- Winner payouts and expired duel refunds from bot hot wallet

Requires env vars:
    BOT_MNEMONIC       — 25-word mnemonic for the bot's hot wallet
    ALGOD_TOKEN        — algod API token (use "" for public nodes)
    ALGOD_URL          — algod node URL
    INDEXER_TOKEN      — indexer API token
    INDEXER_URL        — indexer node URL

Recommended free nodes: AlgoNode (algonode.cloud)
"""

import os
import io
import asyncio
import time
import base64
from urllib.parse import urlencode
from typing import Optional, Callable

import qrcode
from qrcode.image.pure import PyPNGImage

from algosdk import mnemonic, account, transaction
from algosdk.v2client import algod, indexer


# ---------------------------------------------------------------------------
# Client setup
# ---------------------------------------------------------------------------

def get_algod_client() -> algod.AlgodClient:
    return algod.AlgodClient(
        algod_token=os.getenv("ALGOD_TOKEN", ""),
        algod_address=os.getenv("ALGOD_URL", "https://mainnet-api.algonode.cloud"),
    )


def get_indexer_client() -> indexer.IndexerClient:
    return indexer.IndexerClient(
        indexer_token=os.getenv("INDEXER_TOKEN", ""),
        indexer_address=os.getenv("INDEXER_URL", "https://mainnet-idx.algonode.cloud"),
    )


def get_bot_account() -> tuple[str, str]:
    """Return (private_key, address) for the bot's hot wallet."""
    mn = os.getenv("BOT_WALLET_MNEMONIC") or os.getenv("BOT_MNEMONIC")
    if not mn:
        raise EnvironmentError("BOT_WALLET_MNEMONIC env var not set.")
    private_key = mnemonic.to_private_key(mn)
    address = account.address_from_private_key(private_key)
    return private_key, address


def get_bot_address() -> str:
    _, address = get_bot_account()
    return address


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WAGER_ALGO       = 5
PAYOUT_ALGO      = 9
RAKE_ALGO        = 1

WAGER_MICROALGO  = 5_000_000
PAYOUT_MICROALGO = 9_000_000
RAKE_MICROALGO   = 1_000_000

PAYMENT_TIMEOUT  = 180   # seconds before duel expires
POLL_INTERVAL    = 5     # indexer poll cadence in seconds
BOT_MIN_BALANCE  = 10_000_000  # 10 ALGO minimum operating buffer

QR_BOX_SIZE  = 8
QR_BORDER    = 2


# ---------------------------------------------------------------------------
# Deep link + QR generation
# ---------------------------------------------------------------------------

def build_payment_note(duel_id: str) -> str:
    """
    The note players include in their payment so the bot can
    match it to the correct duel. Format: zgp:<duel_id>
    """
    return f"zgp:{duel_id}"


def build_algorand_uri(
    bot_address: str,
    duel_id: str,
    amount_microalgo: int = WAGER_MICROALGO,
) -> str:
    """
    Build an algorand:// deep link URI that opens the player's wallet
    with the transaction pre-filled (ARC-0026 spec).

    Supported by: Pera, Defly, Exodus, Lute
    On mobile this opens the wallet app directly.
    On desktop it won't do much — use the QR fallback instead.

    Note is base64-encoded per the ARC-0026 spec.
    """
    note_b64 = base64.b64encode(
        build_payment_note(duel_id).encode()
    ).decode()

    params = urlencode({
        "amount": amount_microalgo,
        "note":   note_b64,
    })

    # algorand:// scheme not supported in Discord buttons — use HTTPS Pera web
    return f"https://perawallet.app/send/?receiver={bot_address}&{params}"


def build_pera_uri(
    bot_address: str,
    duel_id: str,
    amount_microalgo: int = WAGER_MICROALGO,
) -> str:
    """
    Pera Wallet's own deep link scheme — opens directly into the
    send screen with all fields pre-populated.

    xnote=1 locks the note field so the player can't accidentally
    change it and break the duel matching.
    """
    note_b64 = base64.b64encode(
        build_payment_note(duel_id).encode()
    ).decode()

    params = urlencode({
        "receiver": bot_address,
        "amount":   amount_microalgo,
        "note":     note_b64,
        "xnote":    "1",
    })

    # perawallet:// scheme not supported in Discord buttons — use HTTPS Pera web
    return f"https://perawallet.app/send/?{params}"


def generate_qr_png(uri: str) -> io.BytesIO:
    """
    Generate a QR code PNG of the algorand:// URI.
    Returns a BytesIO buffer ready to attach to a Discord message.
    Desktop players scan this with Pera on their phone.
    Uses pure PNG (no Pillow needed) so it works on Railway out of the box.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=QR_BOX_SIZE,
        border=QR_BORDER,
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
    Build everything the Discord command needs for the payment step.

    Returns:
        bot_address  — bot's Algorand wallet address
        algo_uri     — algorand:// URI (universal, most wallets)
        pera_uri     — perawallet:// URI (Pera-specific, best UX)
        qr_buf       — BytesIO PNG buffer for the QR code
        note         — raw note string player must include
        instructions — formatted Discord message string

    Usage in your bot command:
        ui = build_payment_ui(duel_id)
        qr_file = discord.File(ui["qr_buf"], filename="pay.png")
        view = PaymentView(ui["algo_uri"], ui["pera_uri"])
        await interaction.followup.send(ui["instructions"], file=qr_file, view=view)
    """
    bot_address = get_bot_address()
    algo_uri    = build_algorand_uri(bot_address, duel_id)
    pera_uri    = build_pera_uri(bot_address, duel_id)
    qr_buf      = generate_qr_png(algo_uri)
    note        = build_payment_note(duel_id)

    instructions = (
        f"**Send 5 ALGO to enter the race**\n\n"
        f"📱 **Mobile** — tap a button below to open your wallet. "
        f"The address, amount, and note are pre-filled. Just approve.\n\n"
        f"🖥️ **Desktop** — scan the QR code with Pera Wallet on your phone.\n\n"
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


# ---------------------------------------------------------------------------
# Discord UI view — payment buttons
# ---------------------------------------------------------------------------
# Import this into your bot commands file and pass it to the message send.
# Requires: import discord

def make_payment_view(algo_uri: str, pera_uri: str):
    """
    Returns a discord.ui.View with two link buttons:
      - Open in Pera   (perawallet:// deep link)
      - Open in Wallet (algorand:// universal link)

    These are link-style buttons — tapping them opens the URI directly.
    On mobile this triggers the wallet app. On desktop it's a no-op
    for most users, which is why we also send the QR code.
    """
    import discord

    class PaymentView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)  # matches PAYMENT_TIMEOUT

            self.add_item(discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="Pay 5 ALGO in Pera",
                url=pera_uri,
                emoji="⚡",
                row=0,
            ))

    return PaymentView()


# ---------------------------------------------------------------------------
# Balance helpers
# ---------------------------------------------------------------------------

def get_bot_balance() -> int:
    """Return bot wallet balance in microALGO."""
    client = get_algod_client()
    _, address = get_bot_account()
    return client.account_info(address)["amount"]


def check_bot_can_pay() -> bool:
    return get_bot_balance() >= (PAYOUT_MICROALGO + BOT_MIN_BALANCE)


def get_account_balance(address: str) -> int:
    try:
        return get_algod_client().account_info(address)["amount"]
    except Exception:
        return 0


def get_current_round() -> int:
    return get_algod_client().status()["last-round"]


# ---------------------------------------------------------------------------
# Payment verification via indexer polling
# ---------------------------------------------------------------------------

def find_payment_txn(
    sender_address: str,
    receiver_address: str,
    amount_microalgo: int,
    after_round: int,
    expected_note: str,
) -> Optional[str]:
    """
    Search the indexer for a confirmed ALGO payment matching all criteria.
    Returns txid if found, None otherwise.
    """
    idx          = get_indexer_client()
    expected_b64 = base64.b64encode(expected_note.encode()).decode()

    try:
        response = idx.search_transactions(
            address=sender_address,
            address_role="sender",
            txn_type="pay",
            min_amount=amount_microalgo,
            min_round=after_round,
        )
    except Exception as e:
        print(f"[algo_layer] Indexer search error: {e}")
        return None

    for txn in response.get("transactions", []):
        pay = txn.get("payment-transaction", {})

        # Must send to bot address
        if pay.get("receiver") != receiver_address:
            continue

        # Must meet the wager amount
        if pay.get("amount", 0) < amount_microalgo:
            continue

        # Note must match exactly — this is how we tie the payment to the duel
        raw_note = txn.get("note", "")
        try:
            if raw_note != expected_b64:
                continue
        except Exception:
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
    Poll the indexer every 5 seconds until a matching payment is found
    or the 3-minute window closes.

    on_found(txid)  — async callback fired immediately when payment lands
    on_timeout()    — async callback fired if 3 minutes elapse with nothing

    Returns txid on success, None on timeout.

    Typical usage in a bot command:
        after_round = get_current_round()
        # ... send payment UI to Discord ...
        txid = await wait_for_payment(
            sender_address=racer["wallet_address"],
            duel_id=duel["id"],
            after_round=after_round,
            on_found=lambda txid: confirm_payment(db, duel["id"], role, txid),
            on_timeout=lambda: interaction.followup.send("⏰ Challenge expired."),
        )
    """
    bot_address   = get_bot_address()
    expected_note = build_payment_note(duel_id)
    deadline      = time.monotonic() + PAYMENT_TIMEOUT

    while time.monotonic() < deadline:
        txid = find_payment_txn(
            sender_address=sender_address,
            receiver_address=bot_address,
            amount_microalgo=WAGER_MICROALGO,
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
# Sending transactions from bot wallet
# ---------------------------------------------------------------------------

def _send_algo(receiver: str, amount_microalgo: int, note: str) -> str:
    """Sign, broadcast, and confirm an ALGO payment from the bot wallet."""
    private_key, bot_address = get_bot_account()
    client = get_algod_client()
    params = client.suggested_params()

    txn = transaction.PaymentTxn(
        sender=bot_address,
        sp=params,
        receiver=receiver,
        amt=amount_microalgo,
        note=note.encode("utf-8"),
    )

    signed = txn.sign(private_key)
    txid   = client.send_transaction(signed)
    transaction.wait_for_confirmation(client, txid, 10)
    return txid


def send_payout(winner_address: str, duel_id: str) -> str:
    """Send 9 ALGO to the race winner. Returns txid."""
    if not check_bot_can_pay():
        raise RuntimeError("Bot wallet balance too low to cover payout.")
    return _send_algo(winner_address, PAYOUT_MICROALGO, f"zgp:payout:{duel_id}")


def send_refund(player_address: str, duel_id: str) -> str:
    """Return 5 ALGO to a player whose duel expired. Returns txid."""
    return _send_algo(player_address, WAGER_MICROALGO, f"zgp:refund:{duel_id}")


# ---------------------------------------------------------------------------
# Background expiry task — wire into bot.py
# ---------------------------------------------------------------------------

async def process_expired_duels(db_client, refund: bool = True) -> list[str]:
    """
    Called by a background task in bot.py (e.g. every 30 seconds).
    Finds stale pending duels, marks them expired in Supabase,
    and refunds any player who already sent their ALGO.

    Returns list of expired duel IDs so the bot can post
    expiry notices to the relevant Discord channels.
    """
    from race_engine import expire_stale_duels

    expired  = await expire_stale_duels(db_client)
    duel_ids = []

    for duel in expired:
        duel_id = duel["id"]
        duel_ids.append(duel_id)

        if not refund:
            continue

        for role, user_field, txid_field in [
            ("challenger", "challenger_id", "challenger_txid"),
            ("opponent",   "opponent_id",   "opponent_txid"),
        ]:
            if not duel.get(txid_field):
                continue  # player never paid, nothing to refund

            row = (
                db_client.table("zappy_racers")
                .select("wallet_address")
                .eq("discord_user_id", duel[user_field])
                .single()
                .execute()
                .data
            )
            if not row:
                continue

            try:
                send_refund(row["wallet_address"], duel_id)
                print(f"[algo_layer] Refunded {role} on expired duel {duel_id}")
            except Exception as e:
                print(f"[algo_layer] Refund failed ({role}, {duel_id}): {e}")

    return duel_ids

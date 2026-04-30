"""
gp_accounting.py
Zappy Grand Prix — balance accounting layer

Every balance change goes through this module.
No direct Supabase balance writes should happen outside of here.

Functions:
  credit(db, user_id, currency, amount, reason, ref_id, zappy_id)
  debit(db, user_id, currency, amount, reason, ref_id, zappy_id)
  get_balance(db, user_id, currency)
  get_ledger(db, user_id, currency, limit)
  reconcile_algo(db)  -- compares Supabase vs on-chain
"""

import os
from datetime import datetime, timezone
from typing import Literal

Currency = Literal["ALGO", "ZAPP"]

ALGO_COL = "algo_balance"
ZAPP_COL = "zapp_balance"


def _col(currency: Currency) -> str:
    return ALGO_COL if currency == "ALGO" else ZAPP_COL


# ---------------------------------------------------------------------------
# Core read
# ---------------------------------------------------------------------------

def get_balance(db, discord_user_id: str, currency: Currency) -> float:
    """Read current balance for a player. Returns 0 if not registered."""
    col = _col(currency)
    res = db.table("zappy_racers").select(col).eq(
        "discord_user_id", discord_user_id
    ).order("registered_at", desc=False).limit(1).execute()
    if not res.data:
        return 0
    return float(res.data[0].get(col, 0))


# ---------------------------------------------------------------------------
# Core write — all balance changes go through here
# ---------------------------------------------------------------------------

def _write_balance(db, discord_user_id: str, currency: Currency, new_balance: float) -> bool:
    """
    Write new balance to ALL rows for this player.
    Returns True if at least one row was updated.
    """
    col = _col(currency)
    res = db.table("zappy_racers").update(
        {col: new_balance}
    ).eq("discord_user_id", discord_user_id).execute()
    return bool(res.data)


def _log(db, discord_user_id: str, currency: Currency, amount: float,
         balance_before: float, balance_after: float,
         reason: str, ref_id: str = None, zappy_id: str = None):
    """Write a transaction log entry."""
    try:
        db.table("gp_transactions").insert({
            "discord_user_id": discord_user_id,
            "zappy_id":        zappy_id,
            "currency":        currency,
            "amount":          amount,
            "balance_before":  balance_before,
            "balance_after":   balance_after,
            "reason":          reason,
            "ref_id":          ref_id,
        }).execute()
    except Exception as e:
        print(f"[gp_accounting] Log write failed: {e}")


# ---------------------------------------------------------------------------
# Credit — add funds
# ---------------------------------------------------------------------------

def credit(
    db,
    discord_user_id: str,
    currency: Currency,
    amount: float,
    reason: str,
    ref_id: str = None,
    zappy_id: str = None,
) -> dict:
    """
    Add funds to a player's balance.
    Returns {"ok": True, "balance_before": x, "balance_after": y}
    """
    if amount <= 0:
        return {"ok": False, "error": "Amount must be positive"}

    before = get_balance(db, discord_user_id, currency)
    after  = round(before + amount, 6)

    _write_balance(db, discord_user_id, currency, after)
    _log(db, discord_user_id, currency, amount, before, after, reason, ref_id, zappy_id)

    print(f"[gp_accounting] CREDIT {amount} {currency} to {discord_user_id} "
          f"({before} → {after}) reason={reason} ref={ref_id}")

    return {"ok": True, "balance_before": before, "balance_after": after}


# ---------------------------------------------------------------------------
# Debit — remove funds (atomic with conditional check)
# ---------------------------------------------------------------------------

def debit(
    db,
    discord_user_id: str,
    currency: Currency,
    amount: float,
    reason: str,
    ref_id: str = None,
    zappy_id: str = None,
) -> dict:
    """
    Remove funds from a player's balance.
    Uses conditional update to prevent overdraft.
    Returns {"ok": True, ...} or {"ok": False, "error": ...}
    """
    if amount <= 0:
        return {"ok": False, "error": "Amount must be positive"}

    before = get_balance(db, discord_user_id, currency)

    if before < amount:
        return {
            "ok":      False,
            "error":   f"Insufficient {currency}. Have {before}, need {amount}",
            "balance": before,
        }

    after = round(before - amount, 6)
    col   = _col(currency)

    # Conditional update — only applies if balance is still sufficient
    res = db.table("zappy_racers").update(
        {col: after}
    ).eq("discord_user_id", discord_user_id).gte(col, amount).execute()

    if not res.data:
        # Race condition — balance changed between read and write
        current = get_balance(db, discord_user_id, currency)
        return {
            "ok":      False,
            "error":   f"Balance changed during debit. Current: {current} {currency}",
            "balance": current,
        }

    _log(db, discord_user_id, currency, -amount, before, after, reason, ref_id, zappy_id)

    print(f"[gp_accounting] DEBIT {amount} {currency} from {discord_user_id} "
          f"({before} → {after}) reason={reason} ref={ref_id}")

    return {"ok": True, "balance_before": before, "balance_after": after}


# ---------------------------------------------------------------------------
# Ledger — transaction history for a player
# ---------------------------------------------------------------------------

def get_ledger(
    db,
    discord_user_id: str,
    currency: Currency = None,
    limit: int = 20,
) -> list[dict]:
    """
    Get recent transactions for a player.
    Optionally filter by currency.
    """
    q = db.table("gp_transactions").select("*").eq(
        "discord_user_id", discord_user_id
    )
    if currency:
        q = q.eq("currency", currency)
    res = q.order("created_at", desc=True).limit(limit).execute()
    return res.data or []


# ---------------------------------------------------------------------------
# Reconciliation — compare Supabase balances vs on-chain
# ---------------------------------------------------------------------------

async def reconcile_algo(db) -> dict:
    """
    Compare Supabase ALGO balances against actual on-chain deposits and withdrawals.

    Logic:
      expected_balance = sum(on-chain deposits to bot wallet from player)
                       - sum(on-chain withdrawals from bot wallet to player)

    Returns a report of any discrepancies.
    """
    import asyncio
    from algo_layer import get_indexer_client, get_bot_address

    bot_address = get_bot_address()
    idx         = get_indexer_client()

    # Get all registered players
    all_racers = db.table("zappy_racers").select(
        "discord_user_id, wallet_address, algo_balance"
    ).execute().data or []

    # Deduplicate by discord_user_id (take first row per user)
    seen    = set()
    players = []
    for r in all_racers:
        if r["discord_user_id"] not in seen:
            seen.add(r["discord_user_id"])
            players.append(r)

    report = {
        "checked":       len(players),
        "discrepancies": [],
        "clean":         [],
        "errors":        [],
    }

    for player in players:
        try:
            wallet    = player["wallet_address"]
            db_balance = float(player.get("algo_balance", 0))

            # Sum all deposits (player → bot)
            dep_res = idx.search_transactions(
                address=wallet,
                address_role="sender",
                txn_type="pay",
            )
            total_deposited = sum(
                txn["payment-transaction"]["amount"] / 1_000_000
                for txn in dep_res.get("transactions", [])
                if txn.get("payment-transaction", {}).get("receiver") == bot_address
            )

            # Sum all withdrawals (bot → player)
            with_res = idx.search_transactions(
                address=wallet,
                address_role="receiver",
                txn_type="pay",
            )
            total_withdrawn = sum(
                txn["payment-transaction"]["amount"] / 1_000_000
                for txn in with_res.get("transactions", [])
                if txn.get("sender") == bot_address
            )

            expected = round(total_deposited - total_withdrawn, 6)
            diff     = round(db_balance - expected, 6)

            entry = {
                "discord_user_id": player["discord_user_id"],
                "wallet":          wallet[:10] + "...",
                "db_balance":      db_balance,
                "deposited":       round(total_deposited, 6),
                "withdrawn":       round(total_withdrawn, 6),
                "expected":        expected,
                "diff":            diff,
            }

            if abs(diff) > 0.001:
                report["discrepancies"].append(entry)
            else:
                report["clean"].append(entry)

        except Exception as e:
            report["errors"].append({
                "discord_user_id": player["discord_user_id"],
                "error":           str(e),
            })

    return report

"""
zap_layer.py
Zappy Grand Prix — ZAP token transaction layer

ZAP balances live entirely in Supabase (zappy_racers.zap_balance).
No on-chain transactions, no wallet polling, no payment timeout.
Join → deduct instantly → race → credit winner.

Economics:
    Entry:   500 ZAP each
    Winner:  1,000 ZAP (full pot, no rake)
    Loser:   0 ZAP from race (earns 100 ZAP participation reward separately)
"""

from typing import Optional

ZAP_ENTRY       = 500
ZAP_PAYOUT      = 1000   # full pot to winner, no rake
ZAP_WIN_BONUS   = 500    # participation ZAP on top of payout
ZAP_LOSE_BONUS  = 100    # consolation participation reward


# ---------------------------------------------------------------------------
# Balance checks
# ---------------------------------------------------------------------------

async def get_zap_balance(db, discord_user_id: str) -> int:
    row = (
        db.table("zappy_racers")
        .select("zap_balance")
        .eq("discord_user_id", discord_user_id)
        .single()
        .execute()
        .data
    )
    return row["zap_balance"] if row else 0


async def can_afford_entry(db, discord_user_id: str) -> bool:
    balance = await get_zap_balance(db, discord_user_id)
    return balance >= ZAP_ENTRY


# ---------------------------------------------------------------------------
# Entry deduction — called immediately when player joins ZAP queue
# ---------------------------------------------------------------------------

async def deduct_entry(db, discord_user_id: str) -> dict:
    """
    Deduct 500 ZAP entry fee from a player's balance.
    Returns { success, new_balance, error }
    """
    balance = await get_zap_balance(db, discord_user_id)

    if balance < ZAP_ENTRY:
        return {
            "success": False,
            "error": (
                f"Not enough ZAP. Need **{ZAP_ENTRY:,}** but only have **{balance:,}**.\n"
                f"Race in the ALGO queue to earn more ZAP."
            ),
        }

    new_balance = balance - ZAP_ENTRY
    db.table("zappy_racers").update(
        {"zap_balance": new_balance}
    ).eq("discord_user_id", discord_user_id).execute()

    return {"success": True, "new_balance": new_balance}


# ---------------------------------------------------------------------------
# Payout — called after race resolves
# ---------------------------------------------------------------------------

async def pay_winner(db, winner_id: str, loser_id: str) -> dict:
    """
    Credit 1,000 ZAP to the winner.
    Also applies participation bonuses to both players.
    Returns { winner_balance, loser_balance }
    """
    winner_balance = await get_zap_balance(db, winner_id)
    loser_balance  = await get_zap_balance(db, loser_id)

    new_winner = winner_balance + ZAP_PAYOUT + ZAP_WIN_BONUS
    new_loser  = loser_balance  + ZAP_LOSE_BONUS

    db.table("zappy_racers").update(
        {"zap_balance": new_winner}
    ).eq("discord_user_id", winner_id).execute()

    db.table("zappy_racers").update(
        {"zap_balance": new_loser}
    ).eq("discord_user_id", loser_id).execute()

    return {
        "winner_balance": new_winner,
        "loser_balance":  new_loser,
    }


# ---------------------------------------------------------------------------
# Refund — if race never starts (e.g. bot restart mid-queue)
# ---------------------------------------------------------------------------

async def refund_entry(db, discord_user_id: str) -> int:
    """
    Refund 500 ZAP entry to a player. Returns new balance.
    Call this if a queued player needs to be removed before a race starts.
    """
    balance = await get_zap_balance(db, discord_user_id)
    new_balance = balance + ZAP_ENTRY
    db.table("zappy_racers").update(
        {"zap_balance": new_balance}
    ).eq("discord_user_id", discord_user_id).execute()
    return new_balance

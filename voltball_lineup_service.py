"""
voltball_lineup_service.py
---------------------------
Validates and submits a coach's weekly Voltball lineup, backed by REAL
algorand_lookup.py wallet data and REAL Supabase writes (get_supabase()
from database.py, same pattern as algo_quota_guard.py).

Key shape from algorand_lookup.py:
  - verify_wallet_owns_zappy(wallet) -> {"owns", "zappies": [...], "heroes": [...],
    "collabs": [...], "error"} — ownership only, NO stats attached.
  - fetch_zappy_traits(asset_id) -> {"asset_id","name","stats":{"VLT","INS","SPK"},...}
    — per-asset stats, one indexer-free local lookup call each.

So building a scored roster is two steps: verify ownership (1 indexer call,
cached 12h), then fetch stats per held Zappy (all local table lookups, no
network — cheap to gather concurrently even for a full roster).

Heroes and the ShittyKitties collab are 1/1-style coach assets, NOT roster
material — roster Zappies come only from wallet_data["zappies"] (the main
collection). Hero/collab ownership is used only for coach selection.
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

from algorand_lookup import verify_wallet_owns_zappy, fetch_zappy_traits
from database import get_supabase
from voltball_engine import (
    FORMATIONS, POSITIONS, ROSTER_SIZE, NO_LINEUP_DEFAULT_FORMATION,
    ZappyPlayer, build_team, Team,
)


class LineupValidationError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

    def __str__(self):
        return self.message


async def get_wallet_zappies(wallet_address: str) -> list[dict]:
    """
    Returns roster-eligible Zappies (main collection only — not Heroes/collabs)
    with full VLT/INS/SPK stats attached. One indexer call (cached 12h inside
    algorand_lookup) plus local-table stat lookups per held Zappy.
    """
    wallet_data = await verify_wallet_owns_zappy(wallet_address)
    if wallet_data.get("error"):
        raise LineupValidationError(f"Couldn't verify wallet holdings: {wallet_data['error']}")

    zappy_entries = wallet_data.get("zappies", [])
    if not zappy_entries:
        return []

    full_results = await asyncio.gather(*(fetch_zappy_traits(z["asset_id"]) for z in zappy_entries))

    output = []
    for z in full_results:
        if z is None:
            continue  # shouldn't happen for held assets, but fetch_zappy_traits can return None
        stats = z["stats"]
        output.append({
            "asset_id": z["asset_id"],
            "name": z["name"],
            "VLT": stats["VLT"],
            "INS": stats["INS"],
            "SPK": stats["SPK"],
        })
    return output


async def get_hero_ownership(wallet_address: str) -> list[dict]:
    """Returns Hero + collab NFTs held in the wallet — coach-eligible assets."""
    wallet_data = await verify_wallet_owns_zappy(wallet_address)
    if wallet_data.get("error"):
        raise LineupValidationError(f"Couldn't verify wallet holdings: {wallet_data['error']}")

    heroes = [{"asset_id": h["asset_id"], "hero_type": h["hero_type"], "is_collab": False}
              for h in wallet_data.get("heroes", [])]
    collabs = [{"asset_id": c["asset_id"], "hero_type": c["collab_type"], "is_collab": True}
               for c in wallet_data.get("collabs", [])]
    return heroes + collabs


async def validate_lineup(wallet_address: str, formation: str, position_map: dict) -> Team:
    """
    Validates a proposed lineup against LIVE wallet holdings. Returns a
    built Team ready for resolve_match(), or raises LineupValidationError
    with a Discord-displayable message.

    position_map: {asset_id: "Striker"|"Mid"|"Guard"}
    """
    if formation not in FORMATIONS:
        raise LineupValidationError(f"'{formation}' isn't a valid formation. Choose OFFENSE, BALANCED, or DEFENSE.")

    if len(position_map) != ROSTER_SIZE:
        raise LineupValidationError(
            f"A Voltball lineup needs exactly {ROSTER_SIZE} Zappies assigned — you submitted {len(position_map)}."
        )

    for asset_id, pos in position_map.items():
        if pos not in POSITIONS:
            raise LineupValidationError(f"'{pos}' isn't a valid position (asset {asset_id}). Use Striker, Mid, or Guard.")

    expected_counts = FORMATIONS[formation]
    actual_counts = {p: 0 for p in POSITIONS}
    for pos in position_map.values():
        actual_counts[pos] += 1
    for pos, want in expected_counts.items():
        if actual_counts[pos] != want:
            raise LineupValidationError(
                f"{formation} formation requires {want} {pos}(s) — you assigned {actual_counts[pos]}."
            )

    held = await get_wallet_zappies(wallet_address)
    held_by_id = {z["asset_id"]: z for z in held}
    missing = [aid for aid in position_map if aid not in held_by_id]
    if missing:
        raise LineupValidationError(
            f"These Zappies aren't in your wallet right now: {missing}. "
            f"Lineups are checked against live holdings, not a saved roster."
        )

    roster = [
        ZappyPlayer(asset_id=z["asset_id"], name=z["name"], VLT=z["VLT"], INS=z["INS"], SPK=z["SPK"])
        for z in held_by_id.values() if z["asset_id"] in position_map
    ]

    return build_team(name="", coach_hero_type=None, formation=formation, roster=roster, position_map=position_map)


async def submit_lineup(guild_id: str, team_id: str, season_id: str, week_number: int,
                         formation: str, position_map: dict, wallet_address: str) -> dict:
    """
    Validates then upserts a lineup row. Raises LineupValidationError on
    any rule violation — callers (the Discord command) should catch that
    and send the message back to the user, not a raw traceback.
    """
    team = await validate_lineup(wallet_address, formation, position_map)  # raises on failure

    row = {
        "team_id": team_id,
        "season_id": season_id,
        "week_number": week_number,
        "formation": formation,
        # Storing name alongside asset_id (not just bare ids) — a public
        # scouting report (/voltball_lineups) needs to show every team's
        # roster names cheaply, without re-fetching stats/holdings for
        # every team, every time someone runs the command.
        "assignments": {
            pos: [{"asset_id": z.asset_id, "name": z.name} for z in team.assignments[pos]] for pos in POSITIONS
        },
    }

    db = get_supabase()
    result = db.table("voltball_lineups").upsert(row, on_conflict="team_id,week_number").execute()
    return result.data[0]


async def get_locked_lineup_team(lineup_row: dict, hero_type: str, wallet_address: str) -> Team:
    """
    Rebuilds a Team object from a stored lineup row plus a FRESH stat
    lookup — used by the weekly resolution job. Ownership was already
    checked at lock time; this re-fetches current stats to score the
    match (asset_id is authoritative, the stored "name" is only for
    display elsewhere, e.g. /voltball_lineups).
    """
    assignments = lineup_row["assignments"]
    held = await get_wallet_zappies(wallet_address)
    held_by_id = {z["asset_id"]: z for z in held}

    roster = []
    position_map = {}
    for pos, entries in assignments.items():
        for entry in entries:
            aid = entry["asset_id"] if isinstance(entry, dict) else entry  # supports old bare-id rows too
            z = held_by_id.get(aid)
            if z is None:
                raise LineupValidationError(
                    f"Zappy {aid} from a locked lineup could not be found for scoring — "
                    f"it may have been sold since lineup lock."
                )
            roster.append(ZappyPlayer(asset_id=aid, name=z["name"], VLT=z["VLT"], INS=z["INS"], SPK=z["SPK"]))
            position_map[aid] = pos

    return build_team(name="", coach_hero_type=hero_type, formation=lineup_row["formation"],
                       roster=roster, position_map=position_map)


async def build_fallback_team(wallet_address: str, hero_type: str | None) -> Team | None:
    """
    Auto-fields a team from CURRENT wallet holdings when a coach never
    submitted a lineup. Uses the first ROSTER_SIZE held Zappies (by
    whatever order the wallet returns them) in the default BALANCED
    formation, and flags the team with auto_lineup_penalty=True so the
    engine applies the "no game plan" scoring penalty.

    Returns None if the wallet doesn't hold at least ROSTER_SIZE Zappies
    right now — that's a genuine forfeit case (can't field even a weak
    team), which callers should handle separately from the penalty case.
    """
    held = await get_wallet_zappies(wallet_address)
    if len(held) < ROSTER_SIZE:
        return None

    fallback_seven = held[:ROSTER_SIZE]
    position_map = {}
    counts = FORMATIONS[NO_LINEUP_DEFAULT_FORMATION]
    idx = 0
    for pos, n in counts.items():
        for _ in range(n):
            position_map[fallback_seven[idx]["asset_id"]] = pos
            idx += 1

    roster = [
        ZappyPlayer(asset_id=z["asset_id"], name=z["name"], VLT=z["VLT"], INS=z["INS"], SPK=z["SPK"])
        for z in fallback_seven
    ]

    team = build_team(name="", coach_hero_type=hero_type, formation=NO_LINEUP_DEFAULT_FORMATION,
                       roster=roster, position_map=position_map)
    team.auto_lineup_penalty = True
    return team

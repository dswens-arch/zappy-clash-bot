"""
zappy_chat_voltball.py — Voltball matchup knowledge for the chat bot.

When someone asks a Voltball question (lineup, formation, tempo, "I face <team>
this week", ...), build a compact data block for the model:

  * the engine rules (formations, position stats, QB multiplier, tempo, coach
    signature plays, taken live from voltball_engine so it can't drift),
  * the asker's team and this week's opponent: hero/coach, formation, tempo,
    each position's roster with stats, pooled stats, standings,
  * a Monte Carlo of the real engine (resolve_match) for the submitted lineups,
    plus what-ifs on the SAME 8 Zappies (other formations / tempos).

Fairness: every team's formation and roster for the week is already public via
/voltball_lineups, so nothing here reveals more than that command does.
Zero Algorand traffic: stats come from the local collection / Supabase season
allocation, never a wallet lookup.
"""
from __future__ import annotations

import itertools
import re
import time
from typing import Optional

import voltball_engine as E

N_SIMS = 1500
ROSTER_SIMS = 800
# QB SPK only nudges the whole team's offense by a small multiplier: ~QB_MULT_RATE x ~50 base offense per SPK point,
# vs 0.1+ per point for a primary-position stat, so a QB slot is worth far less per point than a Striker/Mid/Guard slot.
QB_VALUE_PER_SPK = E.QB_MULT_RATE * 50
CACHE_TTL = 120

KEYWORDS = re.compile(
    r"voltball|volt ball|lineup|line up|formation|tempo|striker|surge back|conductor|"
    r"\bguard\b|\bqb\b|quarterback|playoff|matchup|match up|my coach|coach|\bhero\b|"
    r"this week|opponent|standings|cheat win|how do i win|how can i win|face\b",
    re.I,
)

_cache: dict = {"ts": 0.0, "season": None, "teams": []}


def rules_text() -> str:
    sigs = "; ".join(f"{h}: {v['label'].replace(' (Coach)', '')} ({v['desc'].rstrip('.')})"
                     for h, v in sorted(E.HERO_SIGNATURES.items()))
    return (
        "VOLTBALL RULES (the real engine): each team fields 8 Zappies (1 QB + 7 others) and a Hero coach. "
        "Formations: OFFENSE 1 QB/5 Surge Back(Striker)/1 Conductor(Mid)/1 Guard; BALANCED 1/3/2/2; DEFENSE 1/1/2/4. "
        "A match is 4 quarters; score = sum of quarter voltage. Position stat pools: Surge Back = VLT (primary) + SPK; "
        "Conductor = SPK (primary) + INS; Guard = INS (primary) + VLT, and Guard INS also cuts the opponent's offense "
        f"(capped at {int(E.DEFENSE_CAP*100)}%) and adds turnover voltage. The QB's SPK is a small multiplier on the whole team's "
        f"offense (about {E.QB_MULT_MIN}-{E.QB_MULT_MAX}x). Tempo is 1 (Controlled) to 10 (Aggressive) in 0.5 steps: "
        "same average strength, but higher tempo means bigger random swings (good for underdogs, risky for favorites). "
        "The formations are tuned to be near 50/50 against each other, so who fills each slot and the coach matter most. "
        "Random events (lightning strikes, trick plays, interceptions) add luck every quarter. "
        f"Coach signature plays: {sigs}. A team that never submits a lineup is auto-fielded with a big penalty."
    )


def _load_season_and_teams(guild_id: str):
    now = time.monotonic()
    if _cache["season"] is not None and _cache["gid"] == guild_id and now - _cache["ts"] < CACHE_TTL:
        return _cache["season"], _cache["teams"]
    from voltball_db import get_active_or_playoff_season, get_teams_for_season
    season = get_active_or_playoff_season(guild_id)
    teams = get_teams_for_season(season["id"]) if season else []
    _cache.update(ts=now, season=season, teams=teams, gid=guild_id)
    return season, teams


def _stats_for(season_id: str, asset_ids: list[int]) -> dict[int, dict]:
    from voltball_db import get_season_zappy_stats
    from voltball_position_fit import _collection
    _collection._build()
    alloc = {}
    for i in range(0, len(asset_ids), 100):
        try:
            alloc.update(get_season_zappy_stats(season_id, asset_ids=asset_ids[i:i + 100]) or {})
        except Exception:
            pass
    out = {}
    for aid in asset_ids:
        base = _collection.by_asset_id.get(aid)
        st = alloc.get(aid) or base
        if st:
            out[aid] = {"name": (base or {}).get("name", f"Zappy #{aid}"),
                        "VLT": st["VLT"], "INS": st["INS"], "SPK": st["SPK"]}
    return out


class Side:
    """A team's submitted lineup, resolved to stats."""

    def __init__(self, row: dict, lineup: dict, stats: dict[int, dict]):
        self.row, self.lineup = row, lineup
        self.name = row["team_name"]
        self.hero = row.get("hero_type")
        self.formation = lineup["formation"]
        self.tempo = float(lineup.get("tempo") or E.TEMPO_DEFAULT)
        self.players: dict[str, list[E.ZappyPlayer]] = {p: [] for p in E.POSITIONS}
        for pos, entries in lineup["assignments"].items():
            for e in entries:
                aid = e["asset_id"] if isinstance(e, dict) else e
                st = stats.get(aid)
                if st:
                    self.players[pos].append(E.ZappyPlayer(aid, st["name"], st["VLT"], st["INS"], st["SPK"]))

    def complete(self) -> bool:
        return sum(len(v) for v in self.players.values()) == E.ROSTER_SIZE

    def all_players(self) -> list[E.ZappyPlayer]:
        return [z for v in self.players.values() for z in v]

    def team(self, formation=None, tempo=None, assignment=None) -> E.Team:
        a = assignment or self.players
        return E.Team(name=self.name, coach_hero_type=self.hero, formation=formation or self.formation,
                      assignments={p: list(a[p]) for p in E.POSITIONS}, tempo=tempo if tempo is not None else self.tempo)

    def describe(self) -> str:
        names = {"QB": "QB", "Striker": "Surge Back", "Mid": "Conductor", "Guard": "Guard"}
        parts = []
        for pos in E.POSITIONS:
            ps = self.players[pos]
            if not ps:
                continue
            pools = f"VLT {sum(z.VLT for z in ps)}/INS {sum(z.INS for z in ps)}/SPK {sum(z.SPK for z in ps)}"
            roster = ", ".join(f"{z.name} ({z.VLT}/{z.INS}/{z.SPK})" for z in ps)
            parts.append(f"  {names[pos]} [{pools}]: {roster}")
        sig = E.HERO_SIGNATURES.get(self.hero)
        coach = f"{self.hero} coach" + (f" ({sig['label'].replace(' (Coach)', '')}: {sig['desc'].rstrip('.')})" if sig else "")
        return f"{self.name}: {self.formation}, tempo {self.tempo:g}, {coach}\n" + "\n".join(parts)


def _simulate(make_a, make_b, n=N_SIMS) -> dict:
    wa = sa = sb = 0
    for _ in range(n):
        r = E.resolve_match(make_a(), make_b())
        wa += r["winner"] == "A"
        sa += r["score_a"]
        sb += r["score_b"]
    return {"win": round(100 * wa / n), "avg_a": round(sa / n, 1), "avg_b": round(sb / n, 1)}


def _best_assignment(players: list[E.ZappyPlayer], formation: str) -> dict:
    """Highest total engine 'composite value' assignment of these 8 into a formation
    (QB = best SPK; the rest brute-forced over the engine's own position weights)."""
    from voltball_position_fit import _composite_value
    counts = E.FORMATIONS[formation]
    best_total, best = -1.0, None
    for qb in players:
        rest = [z for z in players if z is not qb]
        for strikers in itertools.combinations(rest, counts["Striker"]):
            rest2 = [z for z in rest if z not in strikers]
            for mids in itertools.combinations(rest2, counts["Mid"]):
                guards = [z for z in rest2 if z not in mids]
                total = (sum(_composite_value("Striker", z.VLT, z.INS, z.SPK) for z in strikers)
                         + sum(_composite_value("Mid", z.VLT, z.INS, z.SPK) for z in mids)
                         + sum(_composite_value("Guard", z.VLT, z.INS, z.SPK) for z in guards)
                         + qb.SPK * QB_VALUE_PER_SPK)
                if total > best_total:
                    best_total = total
                    best = {"QB": [qb], "Striker": list(strikers), "Mid": list(mids), "Guard": guards}
    return best


def _what_ifs(mine: Side, opp: Side) -> list[str]:
    """Same 8 Zappies, different formation / tempo, vs the opponent's actual lineup."""
    def run(form, tempo, assign=None):
        return _simulate(lambda: _rename(mine.team(form, tempo, assign), "A"), lambda: _rename(opp.team(), "B"))
    lines = []
    base = run(None, None)
    lines.append(f"  as submitted ({mine.formation}, tempo {mine.tempo:g}): {base['win']}% win")
    players = mine.all_players()
    for form in E.FORMATIONS:
        if form == mine.formation:
            continue
        r = run(form, None, _best_assignment(players, form))
        lines.append(f"  {form} with the best arrangement of the same 8, tempo {mine.tempo:g}: {r['win']}% win")
    for t in (2.0, 5.5, 9.0):
        if abs(t - mine.tempo) < 1.0:
            continue
        r = run(None, t)
        lines.append(f"  same lineup at tempo {t:g}: {r['win']}% win")
    return lines


def _rename(team: E.Team, name: str) -> E.Team:
    team.name = name
    return team


# ── roster-wide optimiser ─────────────────────────────────────────────────────

def _slot_value(pos: str, z: E.ZappyPlayer) -> float:
    from voltball_position_fit import _composite_value
    if pos == "QB":
        return z.SPK * QB_VALUE_PER_SPK
    return _composite_value(pos, z.VLT, z.INS, z.SPK)


def optimise_lineup(pool: list[E.ZappyPlayer], formation: str) -> dict:
    """Best 8-of-N assignment into a formation by the engine's own position weights.
    Greedy fill, then swap-improve (in-lineup swaps and bench swaps) until stable."""
    slots = [p for p in E.POSITIONS for _ in range(E.FORMATIONS[formation][p])]
    cand = sorted(pool, key=lambda z: -max(_slot_value(p, z) for p in E.POSITIONS))[:28]
    chosen: list[Optional[E.ZappyPlayer]] = [None] * len(slots)
    used = set()
    # fill the most demanding slots first (primary-stat positions), QB last
    for i in sorted(range(len(slots)), key=lambda i: slots[i] == "QB"):
        best = max((z for z in cand if z.asset_id not in used), key=lambda z: _slot_value(slots[i], z))
        chosen[i] = best
        used.add(best.asset_id)
    improved = True
    while improved:
        improved = False
        for i in range(len(slots)):
            for j in range(i + 1, len(slots)):
                if slots[i] == slots[j]:
                    continue
                cur = _slot_value(slots[i], chosen[i]) + _slot_value(slots[j], chosen[j])
                new = _slot_value(slots[i], chosen[j]) + _slot_value(slots[j], chosen[i])
                if new > cur + 1e-9:
                    chosen[i], chosen[j] = chosen[j], chosen[i]
                    improved = True
            for z in cand:
                if z.asset_id in {c.asset_id for c in chosen}:
                    continue
                if _slot_value(slots[i], z) > _slot_value(slots[i], chosen[i]) + 1e-9:
                    chosen[i] = z
                    improved = True
    out = {p: [] for p in E.POSITIONS}
    for slot, z in zip(slots, chosen):
        out[slot].append(z)
    return out


def _roster_pool(mine_row: dict, season_id: str, week: int) -> list[E.ZappyPlayer]:
    """Everything the coach could field this week: cached wallet holdings (never a live
    indexer call), main collection only, minus Zappies injured for this week."""
    from algorand_lookup import get_last_known_wallet_result
    from voltball_db import get_injured_asset_ids
    hold = get_last_known_wallet_result(mine_row["wallet_address"]) or {}
    ids = [int(z["asset_id"]) for z in hold.get("zappies", [])]
    if not ids:
        return []
    try:
        injured = get_injured_asset_ids(mine_row["id"], week)
    except Exception:
        injured = set()
    ids = [i for i in ids if i not in injured]
    st = _stats_for(season_id, ids)
    return [E.ZappyPlayer(a, v["name"], v["VLT"], v["INS"], v["SPK"]) for a, v in st.items()]


def _diff_vs(submitted: Optional[Side], best: dict) -> str:
    if not submitted:
        return ""
    old = {z.asset_id: z for z in submitted.all_players()}
    new = {z.asset_id: z for p in E.POSITIONS for z in best[p]}
    out_ = [old[a].name for a in old if a not in new]
    in_ = [new[a].name for a in new if a not in old]
    if not out_ and not in_:
        return "same 8 Zappies"
    return f"swap out {', '.join(out_) or 'nobody'} for {', '.join(in_) or 'nobody'}"


def roster_advice(mine_row: dict, mine: Optional[Side], opp: Optional[Side], season_id: str, week: int) -> list[str]:
    pool = _roster_pool(mine_row, season_id, week)
    if len(pool) < E.ROSTER_SIZE:
        return ["Roster-wide advice unavailable: the asker's cached holdings show fewer than 8 eligible Zappies "
                "(or no holdings check on file yet)."]
    tempo = mine.tempo if mine else E.TEMPO_DEFAULT
    hero = mine_row.get("hero_type")
    lines = [f"BEST LINEUPS from the asker's whole roster ({len(pool)} eligible Zappies, this week's injured excluded), "
             f"coach {hero}, tempo {tempo:g}:"]
    results = []
    for form in E.FORMATIONS:
        best = optimise_lineup(pool, form)
        def mk(best=best, form=form):
            return _rename(E.Team(name="A", coach_hero_type=hero, formation=form,
                                  assignments={p: list(best[p]) for p in E.POSITIONS}, tempo=tempo), "A")
        sim = None
        if opp:
            sim = _simulate(mk, lambda: _rename(opp.team(), "B"), ROSTER_SIMS)["win"]
        total = sum(_slot_value(p, z) for p in E.POSITIONS for z in best[p])
        results.append((sim if sim is not None else 0, form, best, total))
    results.sort(key=lambda r: (-r[0], -r[3]))
    results = [r[:3] for r in results]
    names = {"QB": "QB", "Striker": "Surge Back", "Mid": "Conductor", "Guard": "Guard"}
    for sim, form, best in results[:2]:
        arr = "; ".join(f"{names[p]}: " + ", ".join(f"{z.name} ({z.VLT}/{z.INS}/{z.SPK})" for z in best[p])
                        for p in E.POSITIONS if best[p])
        vs = f" -> {sim}% win vs the opponent's lineup" if opp else ""
        lines.append(f"  {form}{vs}. {arr}. Compared with the submitted lineup: {_diff_vs(mine, best)}.")
    if opp:
        form, best = results[0][1], results[0][2]
        for t in (2.0, 5.5, 9.0):
            if abs(t - tempo) < 1.0:
                continue
            r = _simulate(lambda: _rename(E.Team(name="A", coach_hero_type=hero, formation=form,
                                                 assignments={p: list(best[p]) for p in E.POSITIONS}, tempo=t), "A"),
                          lambda: _rename(opp.team(), "B"), ROSTER_SIMS)
            lines.append(f"  best {form} lineup at tempo {t:g}: {r['win']}% win")
    lines.append("(Lineups are optimised with the engine's own position weights, then simulated; the QB slot matters least.)")
    return lines


def build_voltball_block(guild_id: str, asker_id: int, mentioned_ids: list[int], question: str) -> Optional[str]:
    """Sync (runs in a thread). Returns the data block, or None if the question isn't about Voltball."""
    season, teams = _load_season_and_teams(str(guild_id))
    q = question.lower()
    named = [t for t in teams if t["team_name"] and t["team_name"].lower() in q]
    if not (KEYWORDS.search(question) or named):
        return None

    out = [rules_text()]
    if not season:
        out.append("No Voltball season is active right now.")
        return "\n".join(out)

    from voltball_db import get_lineup, get_standings
    from voltball_schedule import get_week_pairings

    week = season.get("current_week") or 1
    by_id = {t["id"]: t for t in teams}
    by_owner = {str(t.get("owner_discord_id")): t for t in teams if not t.get("is_cpu")}
    mine_row = by_owner.get(str(asker_id))
    out.append(f"Season: {season.get('name')} ({season.get('status')}), current week {week} of {season.get('week_count')}"
               + (" (playoffs)" if season.get("status") == "playoffs" else "") + ".")

    try:
        standings = get_standings(season["id"])
        rank = {r["team_id"]: i for i, r in enumerate(standings, 1)} if standings and "team_id" in standings[0] else {}
        rec = {r.get("team_id"): r for r in standings}
    except Exception:
        standings, rank, rec = [], {}, {}

    def record(t):
        r = rec.get(t["id"])
        if not r:
            return ""
        pos = f"#{rank[t['id']]} " if t["id"] in rank else ""
        return f" ({pos}{r.get('wins', 0)}-{r.get('losses', 0)})"

    # who is "my team" / "their team"
    opp_row = None
    if mine_row:
        try:
            for p in get_week_pairings(season["id"], week):
                if mine_row["id"] in (p["team_a_id"], p["team_b_id"]):
                    oid = p["team_b_id"] if p["team_a_id"] == mine_row["id"] else p["team_a_id"]
                    opp_row = by_id.get(oid)
        except Exception:
            pass
    extra = [t for t in named if t is not mine_row and t is not opp_row]
    for uid in mentioned_ids:
        t = by_owner.get(str(uid))
        if t and t is not mine_row and t is not opp_row and t not in extra:
            extra.append(t)
    if mine_row is None and opp_row is None and not extra:
        out.append("The asker has no team registered this season (they can register to play), and no specific team was identified.")
        return "\n".join(out)

    if mine_row:
        out.append(f"Asker's team: {mine_row['team_name']}{record(mine_row)}, coach {mine_row.get('hero_type')}.")
    if opp_row:
        out.append(f"This week's opponent for the asker: {opp_row['team_name']}{record(opp_row)}"
                   + (" (CPU team: random roster every week)" if opp_row.get("is_cpu") else "") + ".")

    def side_for(row) -> Optional[Side]:
        if row is None or row.get("is_cpu"):
            return None
        lu = get_lineup(row["id"], week)
        if not lu:
            return None
        ids = [e["asset_id"] if isinstance(e, dict) else e for v in lu["assignments"].values() for e in v]
        s = Side(row, lu, _stats_for(season["id"], ids))
        return s if s.complete() else None

    mine, opp = side_for(mine_row), side_for(opp_row)
    for label, row, side in (("Asker's", mine_row, mine), ("Opponent's", opp_row, opp)):
        if row is None:
            continue
        out.append(f"{label} submitted lineup for week {week} (stats VLT/INS/SPK):\n" + side.describe()
                   if side else f"{label} team ({row['team_name']}) has no usable submitted lineup for week {week} "
                                f"(not submitted yet, or CPU). A team with no lineup gets auto-fielded with a heavy penalty.")
    for row in extra[:2]:
        side = side_for(row)
        out.append(f"Other team asked about: {row['team_name']}{record(row)}, coach {row.get('hero_type')}. "
                   + (f"Week {week} lineup:\n" + side.describe() if side else "No submitted lineup this week."))

    if mine and opp:
        res = _simulate(lambda: _rename(mine.team(), "A"), lambda: _rename(opp.team(), "B"))
        out.append(f"SIMULATION of the real engine, {N_SIMS} matches, submitted lineups: asker's team wins {res['win']}% "
                   f"(average score {res['avg_a']} to {res['avg_b']}). Luck matters, so even a big favorite can lose.")
        out.append("What-ifs with the asker's same 8 Zappies against the opponent's actual lineup:\n" + "\n".join(_what_ifs(mine, opp)))
    if mine_row:
        try:
            out.extend(roster_advice(mine_row, mine, opp, season["id"], week))
        except Exception as e:
            print(f"[zappy_chat_voltball] roster advice failed: {e}")
    return "\n".join(out)

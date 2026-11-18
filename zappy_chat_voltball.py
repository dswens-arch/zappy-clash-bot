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
    try:
        alloc = get_season_zappy_stats(season_id, asset_ids=asset_ids) or {}
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
                         + qb.SPK * 0.5)
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
        out.append("Advice is limited to the 8 Zappies already in the submitted lineup; the bot hasn't looked at the rest of the asker's roster.")
    return "\n".join(out)

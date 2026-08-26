"""
voltball_recap.py
------------------
Turns a resolve_match() result (plus the two Team objects that produced
it) into recap material for the site's results page and, later, a
Discord recap post: a narrative paragraph, the match's turning point,
and a "highlight" for each team.

Deliberately does NOT include a "Zappy of the Game" / "Bust of the
Game" in the individual-NFT sense the site mockup sketched. The engine
doesn't support that honestly: events fire against a POSITION GROUP
("Ironclad's Guards pick it off"), not a specific rostered Zappy, and
Wolf/Frog/etc. are Hero COACHES, not roster Zappies. Pretending
otherwise here would just reintroduce the same "text says one thing,
data says another" bug this whole effort started from, one level up.

What's actually derivable, and what this module produces instead:
  - turning_point   -- the single biggest-impact event (a coach signature
                        play, since those are the only events with a real
                        delta) or, if neither team has one, the quarter
                        with the widest voltage swing.
  - highlight_a/b    -- each team's "story of the match": their coach's
                        signature play if they have one and it actually
                        mattered, otherwise which POSITION GROUP (Striker/
                        Mid/Guard) drove their game -- true for No-Coach
                        teams by construction, not a special case.
  - recap_text       -- a short templated paragraph, chosen by how many
                        times the lead changed (wire-to-wire / comeback /
                        back-and-forth), not an LLM call -- deterministic,
                        free, and never invents a stat that isn't in the
                        result.

Called by: voltball_cog.py's weekly resolution job, right after
resolve_match(team_a, team_b) -- needs the Team objects too (for
coach_hero_type and position pools), not just the result dict.
"""

import random
from voltball_engine import HERO_SIGNATURES, DISPLAY_NAME

POSITION_STAT = {"Striker": "VLT", "Mid": "SPK", "Guard": "INS"}

# Maps a flavor event's `kind` to the position group it reflects and
# whether it's a good or bad beat for that group -- used only to pick
# which position group gets spotlighted for a No-Coach (or signature-
# didn't-matter) team, never to award it a fake point value.
_POSITION_EVENT_WEIGHT = {
    "lightning":    ("Striker", 1),
    "coldstreak":   ("Striker", -1),
    "trickplay":    ("Mid", 1),
    "momentum":     ("Mid", 1),
    "interception": ("Guard", 1),
    "shutdown":     ("Guard", 1),
}


def _other(team_key: str) -> str:
    return "b" if team_key == "a" else "a"


def compute_turning_point(result: dict) -> dict | None:
    """
    The moment that mattered most, in order of what the engine can
    actually vouch for:
      1. The biggest-magnitude coach signature play (the only event type
         with a real, isolated delta -- see voltball_engine.resolve_match).
      2. If neither team has a coach or none fired, the quarter with the
         widest voltage swing -- still real, just coarser-grained.
    Returns None only if there are no quarters at all (shouldn't happen
    for a real match).
    """
    sig_events = [e for e in result.get("events", []) if e["kind"] == "signature" and e.get("delta") is not None]
    if sig_events:
        best = max(sig_events, key=lambda e: abs(e["delta"]))
        return {
            "source": "signature",
            "quarter": best["quarter"],
            "team": best["team"],
            "text": best["text"],
            "delta": best["delta"],
        }

    quarters = result.get("quarters", [])
    if not quarters:
        return None
    best_q = max(quarters, key=lambda q: abs(q["team_a_voltage"] - q["team_b_voltage"]))
    diff = round(best_q["team_a_voltage"] - best_q["team_b_voltage"], 1)
    return {
        "source": "quarter_swing",
        "quarter": best_q["quarter"],
        "team": "a" if diff > 0 else "b",
        "text": f"Quarter {best_q['quarter']} was the widest swing of the match ({abs(diff)} Voltage).",
        "delta": abs(diff),
    }


def lead_changes(quarter_totals: list[dict]) -> int:
    """How many times the running-total leader flipped, end-of-quarter to end-of-quarter."""
    changes = 0
    prev_leader = None
    for qt in quarter_totals:
        if qt["team_a_total"] == qt["team_b_total"]:
            leader = "tie"
        else:
            leader = "a" if qt["team_a_total"] > qt["team_b_total"] else "b"
        if prev_leader not in (None, "tie") and leader not in ("tie", prev_leader):
            changes += 1
        prev_leader = leader
    return changes


def _position_group_highlight(result: dict, team_key: str, team) -> dict:
    """
    Honest fallback for a team with no coach, or whose coach's signature
    never meaningfully fired this match: which position group (by real
    flavor-event tally, tie-broken by that group's actual stat pool) told
    the story of their game. Every team has this -- it's the baseline.
    """
    tally = {"Striker": 0, "Mid": 0, "Guard": 0}
    for e in result.get("events", []):
        if e["team"] != team_key:
            continue
        mapping = _POSITION_EVENT_WEIGHT.get(e["kind"])
        if mapping:
            pos, weight = mapping
            tally[pos] += weight

    best_pos = max(tally, key=lambda p: (tally[p], team.pool(p, POSITION_STAT[p])))
    return {
        "type": "position_group",
        "position": best_pos,
        "display_name": DISPLAY_NAME[best_pos],
        "net_events": tally[best_pos],
        "pool_value": team.pool(best_pos, POSITION_STAT[best_pos]),
    }


def team_highlight(result: dict, team_key: str, team) -> dict:
    """
    A team's "story of the match": their coach's signature if they have
    one and it actually moved the needle, otherwise the position group
    that carried (or sank) them. Every team gets SOME highlight -- there's
    no "no data" case, just different honest sources for it.
    """
    if not team.coach_hero_type:
        return _position_group_highlight(result, team_key, team)

    sig = HERO_SIGNATURES.get(team.coach_hero_type)
    if not sig:
        return _position_group_highlight(result, team_key, team)

    opp_key = _other(team_key)

    # Self-boosting signature events are tagged under this team's own key.
    self_events = [
        e for e in result.get("events", [])
        if e["team"] == team_key and e["kind"] == "signature" and e.get("delta", 0) and e["delta"] > 0
        and team.name in e["text"] and sig["label"] in e["text"]
    ]
    self_impact = round(sum(e["delta"] for e in self_events), 1)

    # Opponent-suppressing signature events are tagged under the OPPONENT's
    # key (that's who lost the points) with a negative delta -- see
    # resolve_match's event tagging for escalating_debuff/poison_debuff.
    suppress_events = [
        e for e in result.get("events", [])
        if e["team"] == opp_key and e["kind"] == "signature" and e.get("delta", 0) and e["delta"] < 0
        and team.name in e["text"] and sig["label"] in e["text"]
    ]
    suppress_impact = round(sum(abs(e["delta"]) for e in suppress_events), 1)

    if self_impact <= 0 and suppress_impact <= 0:
        # Coach exists but their signature never meaningfully fired this
        # match (a chance-based play whiffed, Death Roll never landed,
        # etc.) -- fall back rather than spotlighting a no-op.
        return _position_group_highlight(result, team_key, team)

    if self_impact >= suppress_impact:
        return {"type": "coach", "hero_type": team.coach_hero_type, "label": sig["label"],
                "impact": self_impact, "flavor": "self"}
    return {"type": "coach", "hero_type": team.coach_hero_type, "label": sig["label"],
            "impact": suppress_impact, "flavor": "opponent"}


def _highlight_phrase(team_name: str, highlight: dict) -> str:
    """Turns a highlight dict into one recap-ready sentence fragment.
    Multiple phrasings per case, picked at random -- with a 6-team test
    league, the same coach/tone combo repeats often enough that a single
    fixed sentence reads as obviously templated within a couple weeks."""
    if highlight["type"] == "coach":
        if highlight["flavor"] == "self":
            phrasings = [
                "{team}'s {label} did the heavy lifting (+{impact} Voltage)",
                "{team}'s {label} was the difference-maker (+{impact} Voltage)",
                "{team} leaned on {label} all game long (+{impact} Voltage)",
                "{label} carried {team} down the stretch (+{impact} Voltage)",
            ]
        else:
            phrasings = [
                "{team}'s {label} ground the opponent down all match (-{impact} Voltage to them)",
                "{team}'s {label} wore the opposition down all game (-{impact} Voltage to them)",
                "{team}'s {label} throttled the opponent's offense (-{impact} Voltage to them)",
                "{label} quietly drained {impact} Voltage from the other side all match",
            ]
        return random.choice(phrasings).format(team=team_name, label=highlight["label"], impact=highlight["impact"])

    pos = highlight["display_name"]
    if highlight["net_events"] > 0:
        phrasings = [
            "{team}'s {pos}s were the story of the match",
            "{team}'s {pos}s did the heavy lifting all game",
            "It was the {pos}s' night for {team}",
            "{team} rode their {pos}s the whole way",
        ]
    else:
        phrasings = [
            "{team} leaned on their {pos}s, for better or worse",
            "{team}'s {pos}s had a quiet night, for better or worse",
            "{team}'s game came down to their {pos}s tonight",
        ]
    return random.choice(phrasings).format(team=team_name, pos=pos)


def build_recap(result: dict, team_a, team_b) -> dict:
    """
    Full recap package for one resolved match. `team_a`/`team_b` are the
    Team objects passed into resolve_match() -- needed here for
    coach_hero_type and position pools, which aren't in the plain result
    dict.
    """
    quarter_totals = result.get("quarter_totals", [])
    changes = lead_changes(quarter_totals)
    tp = compute_turning_point(result)

    score_a, score_b = result["score_a"], result["score_b"]
    margin = round(abs(score_a - score_b), 1)
    winner_key = "a" if result["winner"] == team_a.name else "b"
    winner_name, loser_name = result["winner"], result["loser"]

    highlight_a = team_highlight(result, "a", team_a)
    highlight_b = team_highlight(result, "b", team_b)
    winner_highlight = highlight_a if winner_key == "a" else highlight_b
    winner_phrase = _highlight_phrase(winner_name, winner_highlight)

    # tp['text'] is always a complete sentence (it ends in a period, whether
    # it came from a signature event's text or the quarter-swing fallback
    # phrasing below). Earlier versions tried to strip the period and
    # splice phrases like "flipped the match" onto the end of it as if it
    # were a fragment -- that reads as a run-on since it's a full clause,
    # not a fragment (e.g. "oyster refuses to let Quarter 1 sink them
    # flipped the match."). Presenting it as its own sentence avoids that
    # regardless of what the underlying event text happens to say.
    turning_point_sentence = f"The turning point: {tp['text']} " if tp else ""

    if changes == 0:
        tone = "wire_to_wire"
        winner_phrase_cap = winner_phrase[0].upper() + winner_phrase[1:]
        openers = [
            "{winner} led wire-to-wire, never trailing {loser} at any point.",
            "{winner} never trailed -- in control from the opening whistle.",
            "{winner} set the pace early and never let go.",
            "A statement game -- {winner} led every step of the way.",
        ]
        opener = random.choice(openers).format(winner=winner_name, loser=loser_name)
        text = (
            f"{opener} "
            f"{winner_phrase_cap}. Final: {winner_name} {max(score_a,score_b)}, {loser_name} {min(score_a,score_b)}."
        )
    elif changes >= 2:
        tone = "back_and_forth"
        openers = [
            "The lead changed hands {changes} times before {winner} pulled it out by {margin}.",
            "{winner} and {loser} traded blows all game -- {winner} finally broke away by {margin}.",
            "A genuine battle -- {changes} lead changes before {winner} closed it out by {margin}.",
        ]
        opener = random.choice(openers).format(changes=changes, winner=winner_name, loser=loser_name, margin=margin)
        text = (
            f"{opener} "
            + turning_point_sentence
            + f"Final: {winner_name} {max(score_a,score_b)}, {loser_name} {min(score_a,score_b)}."
        )
    else:
        tone = "comeback"
        openers = [
            "{winner} trailed for part of the game before turning it around.",
            "{winner} clawed back from behind to steal this one.",
            "Down early, {winner} flipped the script.",
        ]
        opener = random.choice(openers).format(winner=winner_name)
        text = (
            f"{opener} "
            + turning_point_sentence
            + f"Final: {winner_name} {max(score_a,score_b)}, {loser_name} {min(score_a,score_b)}, a {margin}-Voltage margin."
        )

    return {
        "tone": tone,
        "recap_text": text,
        "turning_point": tp,
        "lead_changes": changes,
        "margin": margin,
        "highlight_a": highlight_a,
        "highlight_b": highlight_b,
    }


# ─────────────────────────────────────────────
# Quick test
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import json
    from voltball_engine import ZappyPlayer, build_team, resolve_match

    def make_roster(prefix, n, vlt, ins, spk):
        return [ZappyPlayer(asset_id=i, name=f"{prefix}{i}", VLT=vlt, INS=ins, SPK=spk) for i in range(n)]

    scenarios = [
        ("Both coached", "Wolf", "Frog"),
        ("A has no coach", None, "Frog"),
        ("Both no coach", None, None),
        ("Opponent-suppressing coach (Buck)", "Buck", None),
    ]

    for label, coach_a, coach_b in scenarios:
        roster_a = make_roster("A", 8, vlt=65, ins=52, spk=52)
        pos_map_a = {0: "QB", 1: "Striker", 2: "Striker", 3: "Striker", 4: "Mid", 5: "Mid", 6: "Guard", 7: "Guard"}
        team_a = build_team("Thunder Coasts", coach_a, "BALANCED", roster_a, pos_map_a)

        roster_b = make_roster("B", 8, vlt=68, ins=45, spk=50)
        pos_map_b = {0: "QB", 1: "Striker", 2: "Mid", 3: "Mid", 4: "Guard", 5: "Guard", 6: "Guard", 7: "Guard"}
        team_b = build_team("Volt Runners", coach_b, "DEFENSE", roster_b, pos_map_b)

        result = resolve_match(team_a, team_b)
        recap = build_recap(result, team_a, team_b)
        print(f"\n=== {label} ===")
        print(json.dumps(recap, indent=2))

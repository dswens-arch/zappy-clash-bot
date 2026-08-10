"""
voltball_formation_test.py
---------------------------
Batch-simulates the 3 formations against each other with NO coach, to
confirm the base formula (independent of any Hero signature play) stays
balanced. Coach-specific testing lives in voltball_sig_test.py.
"""

import statistics
from voltball_engine import build_team, ZappyPlayer, resolve_match, FORMATIONS

TRIALS = 300

def make_roster(prefix: str, base_vlt=65, base_ins=52, base_spk=52):
    import random
    random.seed()
    return [
        ZappyPlayer(asset_id=i, name=f"{prefix}{i}",
                    VLT=base_vlt + random.randint(-8, 8),
                    INS=base_ins + random.randint(-8, 8),
                    SPK=base_spk + random.randint(-8, 8))
        for i in range(7)
    ]

def positions_for(formation: str) -> dict:
    counts = FORMATIONS[formation]
    pos_map, idx = {}, 0
    for pos, n in counts.items():
        for _ in range(n):
            pos_map[idx] = pos
            idx += 1
    return pos_map

def run_batch(formation_a, formation_b, trials=TRIALS):
    wins_a = wins_b = 0
    scores_a, scores_b = [], []
    for _ in range(trials):
        team_a = build_team("Team A", None, formation_a, make_roster("A"), positions_for(formation_a))
        team_b = build_team("Team B", None, formation_b, make_roster("B"), positions_for(formation_b))
        result = resolve_match(team_a, team_b)
        scores_a.append(result["score_a"])
        scores_b.append(result["score_b"])
        wins_a += result["winner"] == "Team A"
        wins_b += result["winner"] == "Team B"
    return {
        "win_rate_a": round(wins_a / trials * 100, 1),
        "win_rate_b": round(wins_b / trials * 100, 1),
        "avg_a": round(statistics.mean(scores_a), 1),
        "avg_b": round(statistics.mean(scores_b), 1),
    }

def show(label, r):
    print(f"{label}")
    print(f"  A: {r['win_rate_a']}% (avg {r['avg_a']})   B: {r['win_rate_b']}% (avg {r['avg_b']})\n")

print(f"Formation balance, no coach ({TRIALS} trials each)\n" + "="*50)
show("OFFENSE vs DEFENSE", run_batch("OFFENSE", "DEFENSE"))
show("OFFENSE vs BALANCED", run_batch("OFFENSE", "BALANCED"))
show("DEFENSE vs BALANCED", run_batch("DEFENSE", "BALANCED"))
show("OFFENSE vs OFFENSE (mirror sanity check)", run_batch("OFFENSE", "OFFENSE"))

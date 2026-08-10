"""
voltball_sig_test.py
---------------------
Batch-tests every Hero's coach signature play against a no-coach baseline,
run identically across all 3 formations, to confirm:
  1. Each signature is worth a similar edge (not wildly over/under-tuned)
  2. The edge holds regardless of which formation the coached team runs
"""

import statistics
from voltball_engine import build_team, ZappyPlayer, resolve_match, FORMATIONS, HERO_SIGNATURES

TRIALS = 200

def make_roster(prefix, base_vlt=65, base_ins=52, base_spk=52):
    import random
    random.seed()
    return [
        ZappyPlayer(asset_id=i, name=f"{prefix}{i}",
                    VLT=base_vlt + random.randint(-8, 8),
                    INS=base_ins + random.randint(-8, 8),
                    SPK=base_spk + random.randint(-8, 8))
        for i in range(7)
    ]

def positions_for(formation):
    counts = FORMATIONS[formation]
    pos_map, idx = {}, 0
    for pos, n in counts.items():
        for _ in range(n):
            pos_map[idx] = pos
            idx += 1
    return pos_map

def run_batch(formation, coach, trials=TRIALS):
    wins = 0
    scores_a, scores_b = [], []
    for _ in range(trials):
        team_a = build_team("Coached", coach, formation, make_roster("A"), positions_for(formation))
        team_b = build_team("Uncoached", None, formation, make_roster("B"), positions_for(formation))
        result = resolve_match(team_a, team_b)
        scores_a.append(result["score_a"])
        scores_b.append(result["score_b"])
        if result["winner"] == "Coached":
            wins += 1
    return round(wins / trials * 100, 1), round(statistics.mean(scores_a), 1), round(statistics.mean(scores_b), 1)

print("=" * 70)
print(f"Every Hero's signature vs. no coach, tested on all 3 formations ({TRIALS} trials each)")
print("=" * 70)

for hero in HERO_SIGNATURES:
    print(f"\n{hero} — {HERO_SIGNATURES[hero]['label']} ({HERO_SIGNATURES[hero]['type']})")
    for formation in ("OFFENSE", "BALANCED", "DEFENSE"):
        win_rate, avg_a, avg_b = run_batch(formation, hero)
        print(f"  {formation}: {win_rate}% win rate (avg {avg_a} vs {avg_b}, diff {round(avg_a-avg_b,1)})")

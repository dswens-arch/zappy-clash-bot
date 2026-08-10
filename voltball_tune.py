"""
voltball_tune.py
-----------------
Grid-search DEFENSE_RATE and TURNOVER_RATE (holding OFFENSE_WEIGHT/
PLAYMAKING_WEIGHT fixed) to find the combination that keeps all 3
formation pairings closest to 50/50 -- using the closed-form expected
score (no opponent-specific randomness) as a fast proxy, since the
random events roughly cancel out in expectation.
"""

STRIKER = {"OFFENSE": 5*34.9, "BALANCED": 3*34.9, "DEFENSE": 1*34.9}
MID     = {"OFFENSE": 1*54.5, "BALANCED": 2*54.5, "DEFENSE": 2*54.5}
GUARD   = {"OFFENSE": 1*49.0, "BALANCED": 2*49.0, "DEFENSE": 4*49.0}
# Per-Zappy averages (34.9 VLT / 54.5 SPK / 49.0 INS) are measured from the
# REAL 1585-Zappy collection via calculate_stats(), not assumed. The first
# version of this file used a flat 65/52/52 guess for all three stats,
# which produced a formula that looked balanced in testing but was badly
# broken against real data (Defense beat Offense 97% of the time) because
# VLT runs ~20 points lower than INS/SPK across the actual collection.
# Re-measure this if HEAD_VLT_BONUS/EARRING_SPK_BONUS/etc. in
# stats_engine.py change meaningfully, since the whole formula depends on
# these ratios staying roughly accurate.

OFFENSE_WEIGHT = 0.15
PLAYMAKING_WEIGHT = 0.13

def expected_score(x, y, dr, cap, tr):
    base = STRIKER[x] * OFFENSE_WEIGHT + MID[x] * PLAYMAKING_WEIGHT
    reduction = min(GUARD[y] * dr, cap)
    turnover = GUARD[x] * tr
    return base * (1 - reduction) + turnover

def worst_case_deviation(dr, cap, tr):
    pairs = [("OFFENSE", "DEFENSE"), ("OFFENSE", "BALANCED"), ("DEFENSE", "BALANCED")]
    max_dev = 0
    details = []
    for x, y in pairs:
        sx = expected_score(x, y, dr, cap, tr)
        sy = expected_score(y, x, dr, cap, tr)
        # deviation as % of combined score (proxy for win-rate skew)
        dev = abs(sx - sy) / (sx + sy)
        max_dev = max(max_dev, dev)
        details.append((x, y, round(sx, 1), round(sy, 1), round(dev * 100, 1)))
    return max_dev, details

best = None
for dr_i in range(8, 22):        # 0.0008 - 0.0021
    dr = dr_i / 10000
    for cap_i in range(30, 46, 2):  # 0.30 - 0.45
        cap = cap_i / 100
        for tr_i in range(4, 13):    # 0.04 - 0.12
            tr = tr_i / 100
            dev, details = worst_case_deviation(dr, cap, tr)
            if best is None or dev < best[0]:
                best = (dev, dr, cap, tr, details)

print(f"Best config found: DEFENSE_RATE={best[1]}, DEFENSE_CAP={best[2]}, TURNOVER_RATE={best[3]}")
print(f"Worst-case pairwise deviation: {round(best[0]*100, 1)}%\n")
for x, y, sx, sy, dev in best[4]:
    print(f"  {x} ({sx}) vs {y} ({sy})  -> deviation {dev}%")

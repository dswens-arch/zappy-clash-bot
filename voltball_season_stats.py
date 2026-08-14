"""
voltball_season_stats.py
--------------------------
Season-allocated Zappy stats: once per season, every Zappy in the
collection gets randomly assigned a "performance band" (Legendary
through Practice Squad), weighted by its rarity tier (see
voltball_rarity.py) -- not by anything about its own permanent combat
stats. The actual stat VALUES it gets for the season are borrowed
wholesale from a real peer Zappy currently sitting in that band, rather
than synthetically scaling its own stats -- this naturally preserves
real cross-stat variation (a real Zappy might be VLT-heavy and
SPK-light) instead of an artificial uniform scale-up, and gives a
coherent "good season" or "bad season" story instead of 3 unrelated
independent rolls.

Deliberately reuses voltball_position_fit.py's already-built,
already-live _collection (real calculate_stats() output over the whole
ZAPPY_COLLECTION) rather than depending on a separately-generated
snapshot file -- the website's zappy_stats.json is generated for and
lives in a different repo entirely, and this needs to be self-contained
within the bot's own deployment.

Band cutoffs and the tier-weighted allocation table are both fixed
design decisions confirmed against the real collection's measured
distribution -- see the design conversation this came from for the
reasoning (not just picked to match Oink Soccer's numbers, which
reflect a different game's own multi-collection structure).
"""

import random
from voltball_position_fit import _collection
from voltball_rarity import get_rarity_tier

# Percentile bands on the "overall score" (average of each Zappy's own
# VLT/INS/SPK percentile rank within the real collection) -- same
# method get_position_fit() already uses per-stat, just averaged across
# all 3 to get one combined performance measure per Zappy.
BANDS = ["Legendary", "All-Pro", "Starter", "Backup", "Practice Squad"]
BAND_PERCENTILE_RANGES = {
    "Legendary":      (95, 100),
    "All-Pro":        (80, 95),
    "Starter":        (50, 80),
    "Backup":         (20, 50),
    "Practice Squad": (0, 20),
}

# Confirmed allocation table -- probability of landing in each band,
# by rarity tier. Deliberately structural, not just numeric: Legendary
# is ONLY reachable from Tier 1, and Tier 1 has a real floor (can never
# roll below Starter) -- that's what makes rarity mean something
# concrete beyond "slightly better odds," not just the specific %s.
ALLOCATION_TABLE = {
    1: {"Legendary": 0.05, "All-Pro": 0.35, "Starter": 0.60, "Backup": 0.00, "Practice Squad": 0.00},
    2: {"Legendary": 0.00, "All-Pro": 0.15, "Starter": 0.40, "Backup": 0.35, "Practice Squad": 0.10},
    3: {"Legendary": 0.00, "All-Pro": 0.05, "Starter": 0.25, "Backup": 0.45, "Practice Squad": 0.25},
}


class _BandPools:
    """
    Lazily built once per process -- groups every real Zappy in the
    collection into its performance band, so allocation can cheaply
    sample "a real peer from band X" without rescanning the whole
    collection on every roll.
    """
    def __init__(self):
        self._built = False
        self.pools: dict[str, list[dict]] = {b: [] for b in BANDS}

    def _build(self):
        if self._built:
            return
        all_z = _collection.all_zappies()
        overall_scores = {}
        for z in all_z:
            p_vlt = _collection.percentile("VLT", z["VLT"])
            p_ins = _collection.percentile("INS", z["INS"])
            p_spk = _collection.percentile("SPK", z["SPK"])
            overall_scores[z["asset_id"]] = (p_vlt + p_ins + p_spk) / 3

        for z in all_z:
            score = overall_scores[z["asset_id"]]
            for band, (lo, hi) in BAND_PERCENTILE_RANGES.items():
                if lo <= score <= hi:
                    self.pools[band].append(z)
                    break
        self._built = True

    def sample(self, band: str) -> dict:
        self._build()
        pool = self.pools[band]
        if not pool:
            # Shouldn't happen with a real, reasonably-sized collection and
            # non-degenerate percentile bands, but don't crash a season-start
            # allocation over an edge case -- fall back to the whole collection.
            pool = _collection.all_zappies()
        return random.choice(pool)


_band_pools = _BandPools()


def allocate_season_stats() -> dict[int, dict]:
    """
    Runs the full season allocation once, for every Zappy in the real
    collection. Returns {asset_id: {"VLT":.., "INS":.., "SPK":.., "band":.., "peer_asset_id":..}}.
    Caller is responsible for persisting this (see voltball_db.py's
    save_season_zappy_stats / voltball_schema.sql's
    voltball_season_zappy_stats table).

    "peer_asset_id" is kept in the result for transparency/debugging --
    it's genuinely useful to be able to answer "why does this Zappy have
    these stats this season" by pointing at the real peer it borrowed
    them from, not just the resulting numbers.
    """
    from zappy_collection import ZAPPY_COLLECTION

    allocations = {}
    for asset_id in ZAPPY_COLLECTION:
        tier = get_rarity_tier(asset_id)
        table = ALLOCATION_TABLE[tier]
        bands, weights = zip(*table.items())
        chosen_band = random.choices(bands, weights=weights, k=1)[0]
        peer = _band_pools.sample(chosen_band)
        allocations[asset_id] = {
            "VLT": peer["VLT"],
            "INS": peer["INS"],
            "SPK": peer["SPK"],
            "band": chosen_band,
            "peer_asset_id": peer["asset_id"],
            "rarity_tier": tier,
        }
    return allocations

"""
voltball_rarity.py
--------------------
Computes each Zappy's trait-rarity tier from REAL collection data —
zappy_collection.py's actual trait fields, the same data
calculate_stats() uses, not an external rarity dataset. Uses
"statistical rarity" (sum of 1/frequency across all 8 trait
categories), the same method most NFT rarity tools use.

This is the RARITY axis — how scarce a Zappy's specific trait
combination is. It's deliberately separate from calculate_stats()'s
combat-stat output: a Zappy can be genuinely rare (low population count
on its specific traits) while being combat-mediocre under the fixed
VLT/INS/SPK formula, or vice versa. That's the whole point of the
season-allocation system this feeds into (see voltball_season_stats.py):
rarity tier sets the ODDS of a good season, not a guaranteed outcome,
and now rarity actually buys something in Voltball specifically,
independent of whatever a Zappy's permanent combat stats happen to be.

Tier cutoffs were chosen by computing the real distribution and picking
a genuinely small top tier (10%) rather than defaulting to something
that dilutes what "rare" means — see the design conversation this came
from. Fixed, not configurable per-season: a Zappy's rarity tier reflects
its permanent trait combination, which doesn't change season to season
(only its ALLOCATED performance band does).
"""

from collections import Counter
from zappy_collection import ZAPPY_COLLECTION

TRAIT_CATEGORIES = ["background", "body", "earring", "eyes", "eyewear", "head", "mouth", "skin"]

# Percentile cutoffs on the real distribution, confirmed via actual
# computation against all 1,678 Zappies: Tier 1 = top 10% (~167
# Zappies), Tier 2 = next 30% (~504), Tier 3 = remaining 60% (~1,006).
TIER_1_PERCENTILE = 90  # rarity score >= this percentile -> Tier 1
TIER_2_PERCENTILE = 60  # rarity score >= this percentile -> Tier 2, else Tier 3


class _RarityIndex:
    """Lazily built once, cached for the process lifetime — mirrors voltball_position_fit.py's _CollectionStats pattern."""
    def __init__(self):
        self._built = False
        self.rarity_score: dict[int, float] = {}
        self.tier: dict[int, int] = {}

    def _build(self):
        if self._built:
            return
        total = len(ZAPPY_COLLECTION)
        freq = {cat: Counter(entry[cat] for entry in ZAPPY_COLLECTION.values()) for cat in TRAIT_CATEGORIES}

        for asset_id, entry in ZAPPY_COLLECTION.items():
            score = sum(total / freq[cat][entry[cat]] for cat in TRAIT_CATEGORIES)
            self.rarity_score[asset_id] = score

        scores_sorted = sorted(self.rarity_score.values())
        n = len(scores_sorted)
        tier_1_cutoff = scores_sorted[int(n * TIER_1_PERCENTILE / 100)]
        tier_2_cutoff = scores_sorted[int(n * TIER_2_PERCENTILE / 100)]

        for asset_id, score in self.rarity_score.items():
            if score >= tier_1_cutoff:
                self.tier[asset_id] = 1
            elif score >= tier_2_cutoff:
                self.tier[asset_id] = 2
            else:
                self.tier[asset_id] = 3

        self._built = True


_index = _RarityIndex()


def get_rarity_tier(asset_id: int) -> int:
    """
    Returns 1 (rarest, ~10% of collection), 2 (mid, ~30%), or 3 (common,
    ~60%) for a given asset_id. Raises KeyError if the asset_id isn't in
    ZAPPY_COLLECTION (e.g. a Hero or collab asset — this only covers the
    main roster-eligible collection, same scope as calculate_stats()).
    """
    _index._build()
    return _index.tier[asset_id]


def get_rarity_score(asset_id: int) -> float:
    """Raw statistical rarity score — higher means rarer. Mainly for debugging/display, not needed by the allocation logic itself."""
    _index._build()
    return _index.rarity_score[asset_id]


def tier_counts() -> dict[int, int]:
    """Returns {1: count, 2: count, 3: count} — real population per tier, for verification/display."""
    _index._build()
    counts = {1: 0, 2: 0, 3: 0}
    for t in _index.tier.values():
        counts[t] += 1
    return counts

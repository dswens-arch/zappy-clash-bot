"""
voltball_position_fit.py
--------------------------
Answers "which position is this Zappy actually good at?" — for a coach's
own held Zappies, or for browsing the full collection before buying.

WHY PERCENTILES, NOT RAW STATS: VLT averages 34.9 across the real
collection, INS averages 49.0, SPK averages 54.5 — a real, structural gap
(see voltball_engine.py's tuning notes). Comparing a Zappy's raw VLT to
its raw INS would call almost every Zappy a bad Striker, because VLT's
whole scale runs lower — that's not a meaningful signal about whether
THIS Zappy is unusually good at offense. Instead, each stat is ranked
against the distribution of that SAME stat across the whole collection:
a Zappy in the 90th percentile of VLT is a genuinely elite Striker, even
though its raw VLT number might be smaller than its own raw INS number.

Built entirely from local data (zappy_collection.py + stats_engine.py) —
no network calls, no Supabase dependency. Computed once at import and
cached, since the collection is static.
"""

import bisect
from zappy_collection import ZAPPY_COLLECTION
from stats_engine import calculate_stats

POSITION_STAT = {"Striker": "VLT", "Mid": "SPK", "Guard": "INS"}

# QB shares Mid's stat (SPK) but is deliberately kept OUT of POSITION_STAT
# above — see get_qb_fit()'s docstring for why folding it into the 3-way
# "best_position" comparison would create a meaningless tie. This
# separate mapping is only for rank_collection_for_position() below,
# which has no such ambiguity: browsing "top Zappies for QB" is a
# straightforward SPK sort, same underlying list as "top for Mid" but
# worth surfacing under its own label since real coaches think of them
# as different roles even when the stat happens to be identical.
RANK_STAT = {**POSITION_STAT, "QB": "SPK"}

TIER_THRESHOLDS = [
    (90, "Elite"),
    (70, "Strong"),
    (40, "Solid"),
    (0, "Weak"),
]


def _tier_for_percentile(pct: float) -> str:
    for threshold, label in TIER_THRESHOLDS:
        if pct >= threshold:
            return label
    return "Weak"


class _CollectionStats:
    """Lazily built once, cached for the process lifetime."""
    def __init__(self):
        self._built = False
        self.by_asset_id: dict[int, dict] = {}
        self.sorted_vlt: list[int] = []
        self.sorted_ins: list[int] = []
        self.sorted_spk: list[int] = []

    def _build(self):
        if self._built:
            return
        for asset_id, entry in ZAPPY_COLLECTION.items():
            traits = {k: entry[k] for k in ["background", "body", "earring", "eyes", "eyewear", "head", "mouth", "skin"]}
            stats = calculate_stats(traits)
            self.by_asset_id[asset_id] = {
                "asset_id": asset_id,
                "name": entry["name"],
                "VLT": stats["VLT"], "INS": stats["INS"], "SPK": stats["SPK"],
            }
        self.sorted_vlt = sorted(z["VLT"] for z in self.by_asset_id.values())
        self.sorted_ins = sorted(z["INS"] for z in self.by_asset_id.values())
        self.sorted_spk = sorted(z["SPK"] for z in self.by_asset_id.values())
        self._built = True

    def percentile(self, stat: str, value: int) -> float:
        self._build()
        sorted_list = {"VLT": self.sorted_vlt, "INS": self.sorted_ins, "SPK": self.sorted_spk}[stat]
        # bisect_left gives count of values strictly less than `value`
        rank = bisect.bisect_left(sorted_list, value)
        return round(rank / len(sorted_list) * 100, 1)

    def all_zappies(self) -> list[dict]:
        self._build()
        return list(self.by_asset_id.values())


_collection = _CollectionStats()


def get_qb_fit(spk: int) -> dict:
    """
    QB fit — same SPK percentile machinery as Mid (they share a stat),
    but deliberately kept OUT of get_position_fit()'s 3-way comparison
    rather than added as a 4th entry there. Reason: Mid and QB use the
    identical stat, so a Zappy's Mid percentile and QB percentile are
    always numerically equal — folding QB into "best_position" would
    just create an arbitrary tie-break between two positions that use
    Zappies completely differently (Mid pools additively, QB multiplies
    the whole team's offense), not a real comparison. Which one's
    actually better for a specific roster is a genuine roster-
    construction trade-off the coach should make, not something a
    single percentile number should silently resolve for them.
    """
    pct = _collection.percentile("SPK", spk)
    return {"stat": "SPK", "value": spk, "percentile": pct, "tier": _tier_for_percentile(pct)}


def get_position_fit(vlt: int, ins: int, spk: int) -> dict:
    """
    Returns fit info for all 3 positions plus which one this Zappy is
    best suited for, based on percentile rank within each stat's own
    distribution across the real collection.
    """
    fits = {}
    for position, stat in POSITION_STAT.items():
        value = {"VLT": vlt, "INS": ins, "SPK": spk}[stat]
        pct = _collection.percentile(stat, value)
        fits[position] = {"stat": stat, "value": value, "percentile": pct, "tier": _tier_for_percentile(pct)}

    best_position = max(fits, key=lambda p: fits[p]["percentile"])
    return {"positions": fits, "best_position": best_position}


def rank_collection_for_position(position: str, top_n: int = 15) -> list[dict]:
    """
    Top N Zappies collection-wide for a given position, ranked by raw
    stat value (ties broken by asset_id for stability). Used for
    /voltball_scout — browsing the full collection before buying, not
    just what you already hold.
    """
    stat = RANK_STAT[position]
    all_z = _collection.all_zappies()
    ranked = sorted(all_z, key=lambda z: (-z[stat], z["asset_id"]))
    top = ranked[:top_n]
    return [
        {**z, "percentile": _collection.percentile(stat, z[stat])}
        for z in top
    ]


def label_for_held_zappy(asset_id: int, name: str, vlt: int, ins: int, spk: int) -> str:
    """One-line summary for a held Zappy, e.g. 'Elite Striker (94th percentile VLT)'."""
    fit = get_position_fit(vlt, ins, spk)
    best = fit["positions"][fit["best_position"]]
    return f"{best['tier']} {fit['best_position']} ({best['percentile']}th percentile {best['stat']})"

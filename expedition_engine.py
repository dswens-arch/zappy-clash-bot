"""
expedition_engine.py
--------------------
Manages the state of an active expedition run.
Handles Discord UI (buttons), beat progression, trait influence,
collection bonuses, and final reward calculation.

ZONE 5 BEAT TYPES (new — Apex Summit only)
───────────────────────────────────────────
  narrative   — standard stat-influenced story choice (unchanged behavior)
  momentum    — bold vs safe choices; each updates a momentum meter (0-100);
                meter multiplies the final token payout when banking
  encounter   — 3-round guardian fight; player picks which stat each round;
                wins (0/1/2/3) unlock scaled reward multipliers
  resource    — wager 20% of current run tokens on a 50/50 flip; or decline
  press_luck  — final beat; bank (apply momentum multiplier) or gamble
                (40% → NFT eligible + 50% token bonus; 60% → -15% penalty)

All other zones use the standard narrative beat flow — nothing changed there.
"""

import discord
import random
import os
from expedition_events import (
    ZONES, draw_run, get_image_path,
    get_highest_zone, get_eligible_zones,
    MOMENTUM_START, momentum_multiplier, momentum_label, momentum_bar,
)

# ─────────────────────────────────────────────
# Collection bonus tiers
# ─────────────────────────────────────────────
def get_collection_bonus(zappy_count: int) -> dict:
    if zappy_count >= 50:
        return {"label": "Whale ⚡ (50+)",    "outcome_bonus": 0.30, "token_multiplier": 2.00}
    elif zappy_count >= 25:
        return {"label": "Major (25+)",       "outcome_bonus": 0.20, "token_multiplier": 1.50}
    elif zappy_count >= 10:
        return {"label": "Collector (10+)",   "outcome_bonus": 0.12, "token_multiplier": 1.25}
    elif zappy_count >= 5:
        return {"label": "Builder (5+)",      "outcome_bonus": 0.05, "token_multiplier": 1.10}
    else:
        return {"label": "Explorer (1)",      "outcome_bonus": 0.00, "token_multiplier": 1.00}


def _resolve_tier(stat_key, stats, collection_bonus):
    """Resolve outcome tier with stat check + collection bonus upgrade chance."""
    from expedition_events import stat_tier
    base = stat_tier(stats[stat_key]) if (stat_key and stat_key in stats) else "mid"
    bonus = collection_bonus.get("outcome_bonus", 0.0)
    roll  = random.random()
    if base == "low":   return "mid" if roll < bonus * 2 else "low"
    if base == "mid":   return "high" if roll < bonus else "mid"
    return "high"


def apply_collection_bonus_to_outcome(event, choice_index, stats, collection_bonus):
    """Resolve a narrative beat outcome with tier + collection multiplier applied."""
    choice   = event["choices"][choice_index]
    stat_key = event.get("stat")
    outcomes = choice["outcomes"]
    tier     = _resolve_tier(stat_key, stats, collection_bonus)
    outcome  = outcomes.get(tier, outcomes.get("mid", list(outcomes.values())[0]))
    result   = dict(outcome)
    result["tokens"] = int(outcome.get("tokens", 0) * collection_bonus["token_multiplier"])
    result["_tier"]  = tier
    return result


# ─────────────────────────────────────────────
# Momentum beat resolution
# ─────────────────────────────────────────────
def resolve_momentum_beat(event, choice_index, stats, collection_bonus):
    """
    Resolve a momentum beat choice.
    Returns (outcome_dict, momentum_delta, tier).
    Bold choices swing momentum hard based on stat tier.
    Safe choices always give a small fixed gain.
    """
    choices  = event.get("momentum_choices", [])
    choice   = choices[choice_index]
    stat_key = event.get("stat")
    tier     = _resolve_tier(stat_key, stats, collection_bonus)

    flavor_map = choice.get("flavor", {})
    flavor     = flavor_map.get(tier) or flavor_map.get("any", "")
    delta      = choice.get("delta", {}).get(tier, 0)

    # Base reward comes from the event, scaled by collection multiplier
    base_tokens = int(event.get("base_tokens", 300) * collection_bonus["token_multiplier"])
    base_cp     = event.get("base_cp", 100)

    # Safe choices give full base; bold choices scale with tier
    style = choice.get("style", "bold")
    if style == "safe":
        tokens = base_tokens
        cp     = base_cp
        tone   = "neutral"
    else:
        multipliers = {"high": 1.4, "mid": 0.9, "low": 0.35}
        tokens = int(base_tokens * multipliers[tier])
        cp     = int(base_cp    * multipliers[tier])
        tone   = {"high": "good", "mid": "neutral", "low": "bad"}[tier]

    return {
        "text":   flavor,
        "cp":     cp,
        "tokens": tokens,
        "tone":   tone,
        "_tier":  tier,
        "_style": style,
    }, delta, tier


# ─────────────────────────────────────────────
# Encounter beat resolution
# ─────────────────────────────────────────────
# Win condition: roll(0-100) + stat*0.3 > threshold + 50
# At stat=80, threshold=52: ~68% win rate
# At stat=50, threshold=52: ~48% win rate
ENCOUNTER_MULTIPLIERS = {0: 0.40, 1: 1.00, 2: 1.40, 3: 2.00}

def roll_encounter_round(stat_value: int, threshold: int) -> bool:
    return (random.randint(0, 100) + stat_value * 0.3) > (threshold + 50)

def resolve_encounter_payout(event, wins: int, collection_bonus) -> dict:
    base_tokens = int(event.get("base_tokens", 300) * collection_bonus["token_multiplier"])
    base_cp     = event.get("base_cp", 120)
    mult        = ENCOUNTER_MULTIPLIERS[wins]
    flavor      = event["outcome_flavor"][wins]
    tokens      = int(base_tokens * mult)
    cp          = int(base_cp     * mult)
    tone        = "bad" if wins == 0 else ("neutral" if wins == 1 else "good")
    return {"text": flavor, "cp": cp, "tokens": tokens, "tone": tone, "_encounter_wins": wins}


# ─────────────────────────────────────────────
# Resource beat resolution
# ─────────────────────────────────────────────
RESOURCE_WAGER_PCT  = 0.20
RESOURCE_WIN_CHANCE = 0.50
RESOURCE_WIN_MULT   = 2.2   # net gain when wager returns: wager * 2.2 back

def get_resource_wager(run) -> int:
    return max(1, int(run["total_tokens"] * RESOURCE_WAGER_PCT))

def resolve_resource_bet(run) -> tuple:
    """Returns (won: bool, wager: int, net_token_change: int)."""
    wager = get_resource_wager(run)
    won   = random.random() < RESOURCE_WIN_CHANCE
    net   = int(wager * RESOURCE_WIN_MULT) if won else -wager
    return won, wager, net


# ─────────────────────────────────────────────
# Press-your-luck beat resolution
# ─────────────────────────────────────────────
PRESS_WIN_CHANCE  = 0.40
PRESS_WIN_BONUS   = 0.50   # +50% tokens on win
PRESS_LOSS_TAX    = 0.15   # -15% tokens on loss

def resolve_press_luck(run) -> tuple:
    """Returns (won: bool, token_delta: int)."""
    won = random.random() < PRESS_WIN_CHANCE
    if won:
        return True,  int(run["total_tokens"] * PRESS_WIN_BONUS)
    return     False, -int(run["total_tokens"] * PRESS_LOSS_TAX)


# ─────────────────────────────────────────────
# Active run state
# ─────────────────────────────────────────────
_active_runs: dict = {}


def start_run(discord_user_id: str, zone_num: int, zappy_data: dict, zappy_count: int) -> dict:
    events = draw_run(zone_num)
    bonus  = get_collection_bonus(zappy_count)
    zone   = ZONES[zone_num]

    run = {
        "discord_user_id":  discord_user_id,
        "zone_num":         zone_num,
        "zone_name":        zone["name"],
        "zone_emoji":       zone["emoji"],
        "zone_color":       zone["color"],
        "zappy":            zappy_data,
        "stats":            zappy_data.get("stats", {}),
        "zappy_count":      zappy_count,
        "collection_bonus": bonus,
        "events":           events,
        "beat":             0,
        "total_cp":         0,
        "total_tokens":     0,
        "complete":         False,
        "nft_eligible":     zone_num == 5,
        # Zone 5 extras — None when not in Zone 5
        "momentum":         MOMENTUM_START if zone_num == 5 else None,
        "encounter_state":  None,
        "press_luck_gambled": False,
        "press_luck_won":     False,
    }
    _active_runs[discord_user_id] = run
    return run


def get_run(discord_user_id: str) -> dict | None:
    return _active_runs.get(discord_user_id)


def end_run(discord_user_id: str):
    _active_runs.pop(discord_user_id, None)


# ─── Standard narrative beat (zones 1-4 + zone 5 narrative beats) ───
def advance_beat(discord_user_id: str, choice_index: int) -> dict | None:
    run = _active_runs.get(discord_user_id)
    if not run: return None

    event   = run["events"][run["beat"]]
    outcome = apply_collection_bonus_to_outcome(
        event, choice_index, run["stats"], run["collection_bonus"]
    )
    run["total_cp"]     += outcome.get("cp", 0)
    run["total_tokens"] += outcome.get("tokens", 0)
    run["last_outcome"]  = outcome
    run["last_event"]    = event
    run["beat"]         += 1
    if run["beat"] >= 5: run["complete"] = True
    return run


# ─── Momentum beat ───
def advance_momentum_beat(discord_user_id: str, choice_index: int) -> dict | None:
    run = _active_runs.get(discord_user_id)
    if not run: return None

    event = run["events"][run["beat"]]
    outcome, delta, tier = resolve_momentum_beat(
        event, choice_index, run["stats"], run["collection_bonus"]
    )
    # Clamp momentum
    run["momentum"] = max(0, min(100, (run["momentum"] or MOMENTUM_START) + delta))
    run["last_momentum_delta"] = delta
    run["total_cp"]     += outcome.get("cp", 0)
    run["total_tokens"] += outcome.get("tokens", 0)
    run["last_outcome"]  = outcome
    run["last_event"]    = event
    run["beat"]         += 1
    if run["beat"] >= 5: run["complete"] = True
    return run


# ─── Encounter beat (multi-round) ───
def start_encounter(discord_user_id: str) -> dict | None:
    run = _active_runs.get(discord_user_id)
    if not run: return None
    run["encounter_state"] = {"round": 0, "wins": 0, "log": [], "complete": False}
    return run


def advance_encounter_round(discord_user_id: str, stat_choice: str) -> dict | None:
    """
    Resolve one round of an encounter.
    When all 3 rounds done, writes final payout to run and advances beat.
    Returns run; check run["encounter_state"]["complete"] to know if done.
    """
    run = _active_runs.get(discord_user_id)
    if not run or not run.get("encounter_state"): return None

    event  = run["events"][run["beat"]]
    estate = run["encounter_state"]
    rnd    = estate["round"]

    threshold = event["thresholds"][stat_choice]
    won_round = roll_encounter_round(run["stats"].get(stat_choice, 50), threshold)
    if won_round: estate["wins"] += 1
    flavor = event["win_lines"][rnd] if won_round else event["lose_lines"][rnd]
    estate["log"].append({"round": rnd, "stat": stat_choice, "won": won_round, "flavor": flavor})
    estate["round"] += 1

    if estate["round"] >= 3:
        estate["complete"] = True
        wins    = estate["wins"]
        outcome = resolve_encounter_payout(event, wins, run["collection_bonus"])
        # Encounter nudges momentum: +8/win, -5/loss
        if run["momentum"] is not None:
            run["momentum"] = max(0, min(100,
                run["momentum"] + wins * 8 - (3 - wins) * 5
            ))
        run["total_cp"]     += outcome.get("cp", 0)
        run["total_tokens"] += outcome.get("tokens", 0)
        run["last_outcome"]  = outcome
        run["last_event"]    = event
        run["beat"]         += 1
        if run["beat"] >= 5: run["complete"] = True

    return run


# ─── Resource bet beat ───
def advance_resource_beat(discord_user_id: str, accept: bool) -> dict | None:
    run = _active_runs.get(discord_user_id)
    if not run: return None

    event = run["events"][run["beat"]]
    if accept:
        won, wager, net = resolve_resource_bet(run)
        run["total_tokens"] = max(0, run["total_tokens"] + net)
        run["last_outcome"] = {
            "text":           event["win_text"] if won else event["lose_text"],
            "cp":             0,
            "tokens":         net,
            "tone":           "good" if won else "bad",
            "_resource_won":  won,
            "_resource_wager": wager,
        }
    else:
        cp_gain = event.get("decline_cp", 55)
        run["total_cp"] += cp_gain
        run["last_outcome"] = {
            "text":   event["decline_text"],
            "cp":     cp_gain,
            "tokens": 0,
            "tone":   "neutral",
        }
    run["last_event"] = event
    run["beat"]      += 1
    if run["beat"] >= 5: run["complete"] = True
    return run


# ─── Press-your-luck beat (always beat 5) ───
def advance_press_luck(discord_user_id: str, gamble: bool) -> dict | None:
    """
    Bank:   apply momentum multiplier to full token total, no NFT roll.
    Gamble: 40% → NFT eligible + 50% bonus; 60% → -15% penalty.
    """
    run = _active_runs.get(discord_user_id)
    if not run: return None

    event = run["events"][run["beat"]]
    if gamble:
        won, delta = resolve_press_luck(run)
        run["total_tokens"]     = max(0, run["total_tokens"] + delta)
        run["press_luck_gambled"] = True
        run["press_luck_won"]     = won
        run["nft_eligible"]       = won   # only gamble+win enables NFT roll
        run["last_outcome"] = {
            "text":   event["gamble_win_text"] if won else event["gamble_lose_text"],
            "cp":     0,
            "tokens": delta,
            "tone":   "good" if won else "bad",
        }
    else:
        # Bank — apply momentum multiplier
        mult   = momentum_multiplier(run.get("momentum", MOMENTUM_START))
        bonus  = int(run["total_tokens"] * (mult - 1.0))
        run["total_tokens"] += bonus
        run["nft_eligible"]  = False
        run["last_outcome"] = {
            "text":               event["bank_text"],
            "cp":                 0,
            "tokens":             bonus,
            "tone":               "good" if mult > 1.0 else "neutral",
            "_banked":            True,
            "_momentum_mult":     mult,
            "_momentum_bonus":    bonus,
        }
    run["last_event"] = event
    run["beat"]      += 1
    run["complete"]   = True
    return run


def check_nft_drop(run: dict) -> bool:
    """Roll for NFT drop. Only fires when nft_eligible is True (gamble+win on press_luck)."""
    if not run.get("nft_eligible"):
        return False
    chance = ZONES[5].get("nft_drop_chance", 0.02)
    return random.random() < chance


# ─────────────────────────────────────────────
# Discord Views
# ─────────────────────────────────────────────

class ExpeditionView(discord.ui.View):
    """Standard narrative beat buttons."""
    def __init__(self, run: dict, on_choice_callback):
        super().__init__(timeout=300)
        self.run = run; self.callback = on_choice_callback; self.chosen = False
        event   = run["events"][run["beat"]]
        choices = event.get("choices", [])
        for i, choice in enumerate(choices):
            btn = discord.ui.Button(
                label=choice["label"], style=discord.ButtonStyle.primary,
                custom_id=f"exp_choice_{i}"
            )
            btn.callback = self._make_cb(i)
            self.add_item(btn)

    def _make_cb(self, idx):
        async def cb(interaction: discord.Interaction):
            if self.chosen:
                await interaction.response.send_message("Already chosen.", ephemeral=True); return
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children: item.disabled = True
            await self.callback(interaction, idx)
        return cb

    async def on_timeout(self):
        for item in self.children: item.disabled = True


class MomentumView(discord.ui.View):
    """Momentum beat buttons (bold + safe choices)."""
    def __init__(self, run: dict, on_choice_callback):
        super().__init__(timeout=300)
        self.run = run; self.callback = on_choice_callback; self.chosen = False
        event   = run["events"][run["beat"]]
        choices = event.get("momentum_choices", [])
        for i, choice in enumerate(choices):
            style = discord.ButtonStyle.danger if choice.get("style") == "bold" else discord.ButtonStyle.secondary
            btn   = discord.ui.Button(
                label=choice["label"], style=style, custom_id=f"mom_choice_{i}"
            )
            btn.callback = self._make_cb(i)
            self.add_item(btn)

    def _make_cb(self, idx):
        async def cb(interaction: discord.Interaction):
            if self.chosen:
                await interaction.response.send_message("Already chosen.", ephemeral=True); return
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children: item.disabled = True
            await self.callback(interaction, idx)
        return cb

    async def on_timeout(self):
        for item in self.children: item.disabled = True


class EncounterView(discord.ui.View):
    """Encounter round: pick which stat to use."""
    def __init__(self, run: dict, on_round_callback):
        super().__init__(timeout=300)
        self.run = run; self.callback = on_round_callback; self.chosen = False
        stats = run.get("stats", {})
        for stat, emoji in [("VLT", "⚡"), ("INS", "🛡️"), ("SPK", "💡")]:
            val = stats.get(stat, "?")
            btn = discord.ui.Button(
                label=f"{emoji} {stat}  ({val})", style=discord.ButtonStyle.primary,
                custom_id=f"enc_{stat}"
            )
            btn.callback = self._make_cb(stat)
            self.add_item(btn)

    def _make_cb(self, stat):
        async def cb(interaction: discord.Interaction):
            if self.chosen:
                await interaction.response.send_message("Already chose.", ephemeral=True); return
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children: item.disabled = True
            await self.callback(interaction, stat)
        return cb

    async def on_timeout(self):
        for item in self.children: item.disabled = True


class ResourceView(discord.ui.View):
    """Accept or decline a resource wager."""
    def __init__(self, run: dict, on_decision_callback):
        super().__init__(timeout=300)
        self.run = run; self.callback = on_decision_callback; self.chosen = False
        event = run["events"][run["beat"]]
        wager = get_resource_wager(run)
        accept_btn  = discord.ui.Button(
            label=f"{event['accept_label']}  ({wager} tokens)",
            style=discord.ButtonStyle.danger, custom_id="resource_accept"
        )
        decline_btn = discord.ui.Button(
            label=event["decline_label"],
            style=discord.ButtonStyle.secondary, custom_id="resource_decline"
        )
        accept_btn.callback  = self._make_cb(True)
        decline_btn.callback = self._make_cb(False)
        self.add_item(accept_btn); self.add_item(decline_btn)

    def _make_cb(self, accept):
        async def cb(interaction: discord.Interaction):
            if self.chosen:
                await interaction.response.send_message("Already decided.", ephemeral=True); return
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children: item.disabled = True
            await self.callback(interaction, accept)
        return cb

    async def on_timeout(self):
        for item in self.children: item.disabled = True


class PressLuckView(discord.ui.View):
    """Bank the run or gamble for NFT roll."""
    def __init__(self, run: dict, on_decision_callback):
        super().__init__(timeout=300)
        self.run = run; self.callback = on_decision_callback; self.chosen = False
        event = run["events"][run["beat"]]
        bank_btn   = discord.ui.Button(
            label=event["bank_label"],   style=discord.ButtonStyle.success, custom_id="press_bank"
        )
        gamble_btn = discord.ui.Button(
            label=event["gamble_label"], style=discord.ButtonStyle.danger,  custom_id="press_gamble"
        )
        bank_btn.callback   = self._make_cb(False)
        gamble_btn.callback = self._make_cb(True)
        self.add_item(bank_btn); self.add_item(gamble_btn)

    def _make_cb(self, gamble):
        async def cb(interaction: discord.Interaction):
            if self.chosen:
                await interaction.response.send_message("Already decided.", ephemeral=True); return
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children: item.disabled = True
            await self.callback(interaction, gamble)
        return cb

    async def on_timeout(self):
        for item in self.children: item.disabled = True


class ZoneSelectView(discord.ui.View):
    def __init__(self, eligible_zones: list, cp_total: int, on_zone_callback):
        super().__init__(timeout=120)
        self.callback = on_zone_callback; self.chosen = False
        for zone_num in eligible_zones:
            zone = ZONES[zone_num]
            btn  = discord.ui.Button(
                label=f"{zone['emoji']} {zone['name']}",
                style=discord.ButtonStyle.secondary, custom_id=f"zone_{zone_num}"
            )
            btn.callback = self._make_cb(zone_num)
            self.add_item(btn)

    def _make_cb(self, zone_num):
        async def cb(interaction: discord.Interaction):
            if self.chosen:
                await interaction.response.send_message("Already selected.", ephemeral=True); return
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children: item.disabled = True
            await self.callback(interaction, zone_num)
        return cb


class ZappySelectView(discord.ui.View):
    def __init__(self, zappies: list, on_zappy_callback):
        super().__init__(timeout=120)
        self.callback = on_zappy_callback; self.chosen = False
        for z in zappies[:5]:
            name = z.get("name", z.get("unit_name", f"ASA {z['asset_id']}"))
            btn  = discord.ui.Button(
                label=name[:80], style=discord.ButtonStyle.secondary,
                custom_id=f"zappy_{z['asset_id']}"
            )
            btn.callback = self._make_cb(z["asset_id"])
            self.add_item(btn)

    def _make_cb(self, asset_id):
        async def cb(interaction: discord.Interaction):
            if self.chosen: return
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children: item.disabled = True
            await self.callback(interaction, asset_id)
        return cb


# ─────────────────────────────────────────────
# Embed builders
# ─────────────────────────────────────────────

def _mbar(m: int) -> str:
    filled = round(m / 10)
    return f"{'█' * filled}{'░' * (10 - filled)}  {m}/100  {momentum_label(m)}"


def build_scene_embed(run: dict) -> discord.Embed:
    """Build the scene embed for a narrative beat."""
    event    = run["events"][run["beat"]]
    zone     = ZONES[run["zone_num"]]
    beat_num = run["beat"] + 1
    zappy    = run["zappy"]

    embed = discord.Embed(
        title       = f"{zone['emoji']} {event['title']}",
        description = event["scene"],
        color       = zone["color"],
    )
    embed.set_author(name=f"{zone['name']} · Beat {beat_num} of 5  ·  📖 Story")

    if run.get("momentum") is not None:
        embed.add_field(name="Momentum", value=_mbar(run["momentum"]), inline=False)

    embed.set_footer(text=(
        f"{zappy.get('name','Your Zappy')} · "
        f"VLT {run['stats'].get('VLT','?')} · "
        f"INS {run['stats'].get('INS','?')} · "
        f"SPK {run['stats'].get('SPK','?')} · "
        f"{run['collection_bonus']['label']}"
    ))
    return embed


def build_momentum_scene_embed(run: dict) -> discord.Embed:
    """Scene embed for a momentum beat — shows bold/safe framing."""
    event    = run["events"][run["beat"]]
    zone     = ZONES[run["zone_num"]]
    beat_num = run["beat"] + 1
    zappy    = run["zappy"]
    choices  = event.get("momentum_choices", [])
    stat_key = event.get("stat", "?")

    embed = discord.Embed(
        title       = f"{zone['emoji']} {event['title']}",
        description = event["scene"],
        color       = zone["color"],
    )
    embed.set_author(name=f"{zone['name']} · Beat {beat_num} of 5  ·  ⚡ Momentum")
    embed.add_field(name="Momentum", value=_mbar(run["momentum"]), inline=False)

    # Show the risk framing
    bold_note = "**Bold choices** swing momentum hard — high stat = big gain, low stat = big loss.\n**Safe choice** always gives a small consistent gain."
    embed.add_field(name=f"Stat checked: {stat_key}", value=bold_note, inline=False)

    embed.set_footer(text=(
        f"{zappy.get('name','Your Zappy')} · "
        f"VLT {run['stats'].get('VLT','?')} · "
        f"INS {run['stats'].get('INS','?')} · "
        f"SPK {run['stats'].get('SPK','?')} · "
        f"{run['collection_bonus']['label']}"
    ))
    return embed


def build_encounter_intro_embed(run: dict) -> discord.Embed:
    """Intro embed when an encounter beat starts."""
    event    = run["events"][run["beat"]]
    zone     = ZONES[run["zone_num"]]
    beat_num = run["beat"] + 1
    zappy    = run["zappy"]
    stats    = run["stats"]

    embed = discord.Embed(
        title       = f"⚔️ {event['guardian_name']}",
        description = event["scene"],
        color       = 0xD85A30,
    )
    embed.set_author(name=f"{zone['name']} · Beat {beat_num} of 5  ·  ⚔️ Encounter")
    embed.add_field(
        name  = "3-Round Fight",
        value = (
            "Pick which stat to use each round. Higher stat = better odds.\n"
            "**Win 0** → reduced reward  ·  **Win 2** → 1.4×  ·  **Win 3** → 2.0×"
        ),
        inline=False,
    )
    embed.add_field(name="Momentum", value=_mbar(run["momentum"]), inline=False)
    embed.set_footer(text=(
        f"{zappy.get('name','Your Zappy')} · "
        f"VLT {stats.get('VLT','?')} · "
        f"INS {stats.get('INS','?')} · "
        f"SPK {stats.get('SPK','?')}"
    ))
    return embed


def build_encounter_round_embed(run: dict, round_num: int, won: bool, flavor: str) -> discord.Embed:
    """Shown after each encounter round resolves."""
    estate = run["encounter_state"]
    losses = (round_num + 1) - estate["wins"]
    color  = 0x63992A if won else 0xA32D2D

    embed = discord.Embed(
        title       = f"{'✅' if won else '❌'} Round {round_num + 1}  ·  {estate['wins']}W / {losses}L",
        description = flavor,
        color       = color,
    )
    if not estate["complete"]:
        embed.set_footer(text=f"Round {round_num + 2} of 3 — pick your stat.")
    else:
        embed.set_footer(text="Encounter complete.")

    if run.get("momentum") is not None:
        embed.add_field(name="Momentum", value=_mbar(run["momentum"]), inline=False)

    return embed


def build_resource_bet_embed(run: dict) -> discord.Embed:
    """Scene embed for a resource wager beat."""
    event    = run["events"][run["beat"]]
    zone     = ZONES[run["zone_num"]]
    beat_num = run["beat"] + 1
    wager    = get_resource_wager(run)

    embed = discord.Embed(
        title       = f"{zone['emoji']} {event['title']}",
        description = event["scene"],
        color       = zone["color"],
    )
    embed.set_author(name=f"{zone['name']} · Beat {beat_num} of 5  ·  🎲 Wager")
    embed.add_field(
        name  = "The deal",
        value = (
            f"{event['wager_text']}\n\n"
            f"Your tokens so far: **{run['total_tokens']}**\n"
            f"Wager amount: **{wager}** tokens  ·  50/50 flip  ·  Win returns **{int(wager * RESOURCE_WIN_MULT)}**"
        ),
        inline=False,
    )
    embed.add_field(name="Momentum", value=_mbar(run["momentum"]), inline=False)
    return embed


def build_press_luck_embed(run: dict) -> discord.Embed:
    """Final beat embed — bank vs gamble."""
    event    = run["events"][run["beat"]]
    zone     = ZONES[run["zone_num"]]
    beat_num = run["beat"] + 1
    zappy    = run["zappy"]
    m        = run.get("momentum", MOMENTUM_START)
    mult     = momentum_multiplier(m)
    banked   = int(run["total_tokens"] * mult)

    embed = discord.Embed(
        title       = f"{zone['emoji']} {event['title']}",
        description = event["scene"],
        color       = zone["color"],
    )
    embed.set_author(name=f"{zone['name']} · Beat {beat_num} of 5  ·  🏆 Final Call")
    embed.add_field(name="Momentum", value=_mbar(m), inline=False)
    embed.add_field(
        name  = "Your choice",
        value = (
            f"**🏦 Bank** → momentum {mult:.2f}× applied → **{banked} tokens** locked in\n"
            f"**🎲 Gamble** → 40% chance: NFT roll + 50% bonus · 60% chance: −15% penalty\n\n"
            f"*Banking skips the NFT roll entirely.*"
        ),
        inline=False,
    )
    embed.set_footer(text=f"{zappy.get('name','Your Zappy')} · Current total: {run['total_tokens']} tokens")
    return embed


def build_outcome_embed(run: dict) -> discord.Embed:
    """Outcome embed after any beat resolves."""
    outcome   = run["last_outcome"]
    event     = run["last_event"]
    beat_type = event.get("beat_type", "narrative")

    tone_colors = {"good": 0x63992A, "neutral": 0x888780, "bad": 0xA32D2D}
    color = tone_colors.get(outcome.get("tone", "neutral"), 0x888780)

    embed = discord.Embed(description=outcome["text"], color=color)

    cp_gain  = outcome.get("cp",     0)
    tok_gain = outcome.get("tokens", 0)
    rewards  = []
    if cp_gain  > 0: rewards.append(f"⚡ +{cp_gain} CP")
    if tok_gain > 0: rewards.append(f"🪙 +{tok_gain} tokens")
    if tok_gain < 0: rewards.append(f"🪙 {tok_gain} tokens")
    if rewards:
        embed.add_field(name="Result", value="  ".join(rewards), inline=False)

    # Momentum change callout
    if beat_type == "momentum" and "last_momentum_delta" in run:
        delta = run["last_momentum_delta"]
        sign  = "+" if delta >= 0 else ""
        embed.add_field(
            name  = "Momentum",
            value = f"{sign}{delta}  →  {_mbar(run['momentum'])}",
            inline=False,
        )
    elif run.get("momentum") is not None:
        embed.add_field(name="Momentum", value=_mbar(run["momentum"]), inline=False)

    # Banking callout
    if outcome.get("_banked"):
        mult  = outcome.get("_momentum_mult", 1.0)
        bonus = outcome.get("_momentum_bonus", 0)
        embed.add_field(
            name  = "Momentum bonus applied",
            value = f"{mult:.2f}× · +{bonus} bonus tokens",
            inline=False,
        )

    embed.set_footer(text=f"Run total: {run['total_cp']} CP · {run['total_tokens']} tokens")
    return embed


def build_run_complete_embed(run: dict, nft_drop: bool = False) -> discord.Embed:
    """Final embed when run completes."""
    zone  = ZONES[run["zone_num"]]
    zappy = run.get("zappy", {})

    embed = discord.Embed(
        title = f"🏁 Expedition Complete — {zone['name']}",
        color = zone["color"],
    )

    finish_line = ""
    if run.get("momentum") is not None:
        m = run["momentum"]
        finish_line = f"\n{_mbar(m)}"

    embed.add_field(
        name  = "Run Summary",
        value = (
            f"⚡ **{run['total_cp']} Expedition CP** earned\n"
            f"🪙 **{run['total_tokens']} tokens** collected\n"
            f"📦 Collection bonus: {run['collection_bonus']['label']}"
            f"{finish_line}"
        ),
        inline=False,
    )

    if nft_drop:
        embed.add_field(
            name  = "🎉 RARE DROP!",
            value = "**An NFT has been added to your wallet!** The summit rewards the worthy.",
            inline=False,
        )

    if zappy.get("image_url"):
        embed.set_thumbnail(url=zappy["image_url"])

    embed.set_footer(text=f"{zappy.get('name','Your Zappy')} · Use /exprank to see the Expedition leaderboard")
    return embed

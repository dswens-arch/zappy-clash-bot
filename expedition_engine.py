"""
expedition_engine.py
--------------------
Manages the state of an active expedition run.
Handles Discord UI (buttons), beat progression, trait influence,
collection bonuses, and final reward calculation.
"""

import discord
import random
import os
from expedition_events import (
    ZONES, draw_run, resolve_outcome, get_image_path,
    get_highest_zone, get_eligible_zones
)

# ─────────────────────────────────────────────
# Collection size bonus tiers
# ─────────────────────────────────────────────
def get_collection_bonus(zappy_count: int) -> dict:
    """
    Returns bonus multipliers based on how many Zappies the wallet holds.
    outcome_bonus: added probability of hitting the "high" tier (0.0-1.0)
    token_multiplier: multiplies token rewards
    """
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


def apply_collection_bonus_to_outcome(
    event: dict,
    choice_index: int,
    stats: dict,
    collection_bonus: dict,
) -> dict:
    """
    Resolve outcome with collection bonus applied.
    The bonus shifts odds toward "high" tier outcomes.
    """
    from expedition_events import stat_tier

    choice   = event["choices"][choice_index]
    stat_key = event.get("stat")
    outcomes = choice["outcomes"]

    if stat_key and stat_key in stats:
        base_tier = stat_tier(stats[stat_key])
    else:
        base_tier = "mid"

    # Apply collection bonus: chance to upgrade tier
    bonus = collection_bonus.get("outcome_bonus", 0.0)
    roll  = random.random()

    if base_tier == "low":
        if roll < bonus * 2:       # Double bonus for recovering from bad tier
            tier = "mid"
        else:
            tier = "low"
    elif base_tier == "mid":
        if roll < bonus:
            tier = "high"
        else:
            tier = "mid"
    else:
        tier = "high"

    outcome = outcomes.get(tier, outcomes.get("mid", list(outcomes.values())[0]))

    # Apply token multiplier
    multiplied = dict(outcome)
    multiplied["tokens"] = int(outcome.get("tokens", 0) * collection_bonus["token_multiplier"])

    return multiplied


# ─────────────────────────────────────────────
# Active run state
# Keyed by discord_user_id
# ─────────────────────────────────────────────
_active_runs: dict = {}


def start_run(
    discord_user_id: str,
    zone_num: int,
    zappy_data: dict,
    zappy_count: int,
) -> dict:
    """
    Initialize a new expedition run for a user.
    Returns the run state dict.
    """
    events   = draw_run(zone_num)
    bonus    = get_collection_bonus(zappy_count)
    zone     = ZONES[zone_num]

    run = {
        "discord_user_id": discord_user_id,
        "zone_num":        zone_num,
        "zone_name":       zone["name"],
        "zone_emoji":      zone["emoji"],
        "zone_color":      zone["color"],
        "zappy":           zappy_data,
        "stats":           zappy_data.get("stats", {}),
        "zappy_count":     zappy_count,
        "collection_bonus": bonus,
        "events":          events,
        "beat":            0,          # current beat index (0-4)
        "total_cp":        0,          # accumulated this run
        "total_tokens":    0,          # accumulated this run
        "complete":        False,
        "nft_eligible":    zone_num == 5,
    }

    _active_runs[discord_user_id] = run
    return run


def get_run(discord_user_id: str) -> dict | None:
    return _active_runs.get(discord_user_id)


def end_run(discord_user_id: str):
    _active_runs.pop(discord_user_id, None)


def advance_beat(discord_user_id: str, choice_index: int) -> dict | None:
    """
    Process a choice for the current beat and advance to the next.
    Returns updated run state, or None if run not found.
    """
    run = _active_runs.get(discord_user_id)
    if not run:
        return None

    beat_index = run["beat"]
    event      = run["events"][beat_index]
    bonus      = run["collection_bonus"]
    stats      = run["stats"]

    outcome = apply_collection_bonus_to_outcome(event, choice_index, stats, bonus)

    run["total_cp"]     += outcome.get("cp", 0)
    run["total_tokens"] += outcome.get("tokens", 0)
    run["last_outcome"] =  outcome
    run["last_event"]   =  event
    run["beat"]         += 1

    if run["beat"] >= 5:
        run["complete"] = True

    return run


def check_nft_drop(run: dict) -> bool:
    """Roll for NFT drop at Zone 5."""
    if not run.get("nft_eligible"):
        return False
    chance = ZONES[5].get("nft_drop_chance", 0.02)
    return random.random() < chance


# ─────────────────────────────────────────────
# Discord UI — Choice buttons
# ─────────────────────────────────────────────

class ExpeditionView(discord.ui.View):
    """Button view for a single expedition beat."""

    def __init__(self, run: dict, on_choice_callback):
        super().__init__(timeout=300)   # 5 minute timeout
        self.run      = run
        self.callback = on_choice_callback
        self.chosen   = False

        event   = run["events"][run["beat"]]
        choices = event["choices"]

        for i, choice in enumerate(choices):
            btn = discord.ui.Button(
                label    = choice["label"],
                style    = discord.ButtonStyle.primary,
                custom_id = f"exp_choice_{i}",
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, choice_index: int):
        async def button_callback(interaction: discord.Interaction):
            if self.chosen:
                await interaction.response.send_message(
                    "You already made your choice for this beat.", ephemeral=True
                )
                return

            self.chosen = True
            for item in self.children:
                item.disabled = True
            await interaction.response.defer(ephemeral=True)

            # Process the choice
            await self.callback(interaction, choice_index)

        return button_callback

    async def on_timeout(self):
        """Disable buttons on timeout."""
        for item in self.children:
            item.disabled = True


class ZoneSelectView(discord.ui.View):
    """Lets the player pick which zone to run."""

    def __init__(self, eligible_zones: list, cp_total: int, on_zone_callback):
        super().__init__(timeout=120)
        self.callback = on_zone_callback
        self.chosen   = False

        for zone_num in eligible_zones:
            zone = ZONES[zone_num]
            btn  = discord.ui.Button(
                label    = f"{zone['emoji']} {zone['name']}",
                style    = discord.ButtonStyle.secondary,
                custom_id = f"zone_{zone_num}",
            )
            btn.callback = self._make_callback(zone_num)
            self.add_item(btn)

    def _make_callback(self, zone_num: int):
        async def button_callback(interaction: discord.Interaction):
            if self.chosen:
                await interaction.response.send_message(
                    "Already selected.", ephemeral=True
                )
                return
            self.chosen = True
            for item in self.children:
                item.disabled = True
            await interaction.response.defer(ephemeral=True)
            await self.callback(interaction, zone_num)

        return button_callback


class ZappySelectView(discord.ui.View):
    """Lets the player pick which Zappy to send on expedition."""

    def __init__(self, zappies: list, on_zappy_callback):
        super().__init__(timeout=120)
        self.callback = on_zappy_callback
        self.chosen   = False

        # Show up to 5 Zappies as buttons (Discord limit per row = 5)
        for z in zappies[:5]:
            name = z.get("name", z.get("unit_name", f"ASA {z['asset_id']}"))
            btn  = discord.ui.Button(
                label     = name[:80],
                style     = discord.ButtonStyle.secondary,
                custom_id = f"zappy_{z['asset_id']}",
            )
            btn.callback = self._make_callback(z["asset_id"])
            self.add_item(btn)

    def _make_callback(self, asset_id: int):
        async def button_callback(interaction: discord.Interaction):
            if self.chosen:
                return
            self.chosen = True
            for item in self.children:
                item.disabled = True
            await interaction.response.defer(ephemeral=True)
            await self.callback(interaction, asset_id)

        return button_callback


# ─────────────────────────────────────────────
# Embed builders
# ─────────────────────────────────────────────

def build_scene_embed(run: dict) -> discord.Embed:
    """Build the embed for a beat's opening scene."""
    event    = run["events"][run["beat"]]
    zone     = ZONES[run["zone_num"]]
    beat_num = run["beat"] + 1
    zappy    = run["zappy"]

    embed = discord.Embed(
        title       = f"{zone['emoji']} {event['title']}",
        description = event["scene"],
        color       = zone["color"],
    )
    embed.set_author(name=f"{zone['name']} · Beat {beat_num} of 5")
    embed.set_footer(
        text=(
            f"{zappy.get('name', 'Your Zappy')} · "
            f"VLT {run['stats'].get('VLT','?')} · "
            f"INS {run['stats'].get('INS','?')} · "
            f"SPK {run['stats'].get('SPK','?')} · "
            f"{run['collection_bonus']['label']}"
        )
    )

    return embed


def build_outcome_embed(run: dict) -> discord.Embed:
    """Build the embed showing the result of a choice."""
    outcome  = run["last_outcome"]
    event    = run["last_event"]
    zone     = ZONES[run["zone_num"]]

    tone_colors = {
        "good":    0x63992A,   # green
        "neutral": 0x888780,   # gray
        "bad":     0xA32D2D,   # red
    }
    color = tone_colors.get(outcome.get("tone", "neutral"), 0x888780)

    cp_gain    = outcome.get("cp", 0)
    token_gain = outcome.get("tokens", 0)

    embed = discord.Embed(
        description = outcome["text"],
        color       = color,
    )

    rewards = []
    if cp_gain:
        rewards.append(f"⚡ +{cp_gain} Expedition CP")
    if token_gain:
        rewards.append(f"🪙 +{token_gain} tokens")
    if rewards:
        embed.add_field(name="Rewards", value="  ".join(rewards), inline=False)

    # Running total
    embed.set_footer(
        text=f"Run total: {run['total_cp']} CP · {run['total_tokens']} tokens"
    )

    return embed


def build_run_complete_embed(run: dict, nft_drop: bool = False) -> discord.Embed:
    """Build the final embed when a run completes."""
    zone  = ZONES[run["zone_num"]]
    zappy = run.get("zappy", {})

    embed = discord.Embed(
        title = f"🏁 Expedition Complete — {zone['name']}",
        color = zone["color"],
    )

    if nft_drop:
        embed.add_field(
            name  = "🎉 RARE DROP!",
            value = "**An NFT has been added to your wallet!** The summit rewards the worthy.",
            inline=False,
        )

    if zappy.get("image_url"):
        embed.set_thumbnail(url=zappy["image_url"])

    embed.set_footer(text=f"{zappy.get('name', 'Your Zappy')} · Use /exprank to see the Expedition leaderboard")
    return embed

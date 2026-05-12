"""
expedition_engine.py
--------------------
Manages the state of an active expedition run.
Handles Discord UI (buttons), beat progression, trait influence,
collection bonuses, hard mode question flow, and final reward calculation.

HARD MODE FLOW (per beat):
  1. build_scene_embed()         → show scene + trait hint
  2. QuestionView                → 3 answer buttons (only in hard mode)
       └─ on answer → store result in run["pending_question_correct"]
  3. ExpeditionView              → show action choices
       └─ on choice  → advance_beat() uses question result to resolve outcome
  4. build_outcome_embed()       → show result with hard mode indicator

Normal mode skips step 2 entirely — QuestionView is never shown.
"""

import discord
import random
from expedition_events import (
    ZONES, draw_run, resolve_outcome, resolve_outcome_hard_mode,
    get_image_path, get_highest_zone, get_eligible_zones, get_trait_hint,
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
        return {"label": "Whale ⚡ (50+)",  "outcome_bonus": 0.30, "token_multiplier": 2.00}
    elif zappy_count >= 25:
        return {"label": "Major (25+)",     "outcome_bonus": 0.20, "token_multiplier": 1.50}
    elif zappy_count >= 10:
        return {"label": "Collector (10+)", "outcome_bonus": 0.12, "token_multiplier": 1.25}
    elif zappy_count >= 5:
        return {"label": "Builder (5+)",    "outcome_bonus": 0.05, "token_multiplier": 1.10}
    else:
        return {"label": "Explorer (1)",    "outcome_bonus": 0.00, "token_multiplier": 1.00}


def apply_collection_bonus_to_outcome(
    event: dict,
    choice_index: int,
    stats: dict,
    collection_bonus: dict,
    hard_mode: bool = False,
    question_correct: bool | None = None,
) -> dict:
    """
    Resolve outcome tier, applying collection bonus and hard mode modifiers.

    Hard mode overrides stat-based tier resolution entirely:
      - correct answer  → guaranteed "high"
      - wrong answer    → forced "low" (or "bad" if present)
      - trap choices    → always "low" regardless

    Normal mode uses stat tier + collection bonus roll as before.
    """
    from expedition_events import stat_tier

    choice   = event["choices"][choice_index]
    stat_key = event.get("stat")
    outcomes = choice["outcomes"]
    is_trap  = choice.get("trap", False)

    if is_trap:
        # Traps always resolve low in both modes
        tier = "low"
    elif hard_mode and question_correct is not None:
        tier = "high" if question_correct else ("bad" if "bad" in outcomes else "low")
    else:
        # Normal mode: stat tier + collection bonus roll
        if stat_key and stat_key in stats:
            base_tier = stat_tier(stats[stat_key])
        else:
            base_tier = "mid"

        bonus = collection_bonus.get("outcome_bonus", 0.0)
        roll  = random.random()

        if base_tier == "low":
            tier = "mid" if roll < bonus * 2 else "low"
        elif base_tier == "mid":
            tier = "high" if roll < bonus else "mid"
        else:
            tier = "high"

    outcome = outcomes.get(tier, outcomes.get("mid", list(outcomes.values())[0]))

    # Apply token multiplier from collection bonus
    multiplied = dict(outcome)
    multiplied["tokens"] = int(outcome.get("tokens", 0) * collection_bonus["token_multiplier"])

    # Tag hard mode metadata onto the outcome for embed display
    multiplied["_hard_mode"]        = hard_mode
    multiplied["_question_correct"] = question_correct
    multiplied["_is_trap"]          = is_trap

    return multiplied


# ─────────────────────────────────────────────
# Active run state — keyed by discord_user_id
# ─────────────────────────────────────────────

_active_runs: dict = {}


def start_run(
    discord_user_id: str,
    zone_num: int,
    zappy_data: dict,
    zappy_count: int,
    hard_mode: bool = False,
) -> dict:
    """
    Initialize a new expedition run for a user.
    Returns the run state dict.
    """
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
        "beat":             0,       # current beat index (0-4)
        "total_cp":         0,
        "total_tokens":     0,
        "complete":         False,
        "nft_eligible":     zone_num == 5,
        # Hard mode state
        "hard_mode":                hard_mode,
        "hard_mode_correct":        0,    # questions answered correctly this run
        "hard_mode_wrong":          0,    # questions answered wrong this run
        "pending_question_correct": None, # result of current beat's question; None = not yet answered
    }

    _active_runs[discord_user_id] = run
    return run


def get_run(discord_user_id: str) -> dict | None:
    return _active_runs.get(discord_user_id)


def end_run(discord_user_id: str):
    _active_runs.pop(discord_user_id, None)


def record_question_answer(discord_user_id: str, correct: bool) -> dict | None:
    """
    Store the result of the hard mode question for the current beat.
    Called after the player picks an answer in QuestionView.
    Returns updated run, or None if run not found.
    """
    run = _active_runs.get(discord_user_id)
    if not run:
        return None
    run["pending_question_correct"] = correct
    if correct:
        run["hard_mode_correct"] += 1
    else:
        run["hard_mode_wrong"] += 1
    return run


def advance_beat(discord_user_id: str, choice_index: int) -> dict | None:
    """
    Process a choice for the current beat and advance to the next.
    In hard mode, uses the stored question result from record_question_answer().
    Returns updated run state, or None if run not found.
    """
    run = _active_runs.get(discord_user_id)
    if not run:
        return None

    beat_index = run["beat"]
    event      = run["events"][beat_index]
    bonus      = run["collection_bonus"]
    stats      = run["stats"]
    hard_mode  = run.get("hard_mode", False)
    q_correct  = run.get("pending_question_correct")  # None in normal mode

    outcome = apply_collection_bonus_to_outcome(
        event, choice_index, stats, bonus,
        hard_mode=hard_mode,
        question_correct=q_correct,
    )

    run["total_cp"]     += outcome.get("cp", 0)
    run["total_tokens"] += outcome.get("tokens", 0)
    run["last_outcome"] =  outcome
    run["last_event"]   =  event
    run["beat"]         += 1

    # Reset question state for the next beat
    run["pending_question_correct"] = None

    if run["beat"] >= 5:
        run["complete"] = True

    return run


def check_nft_drop(run: dict) -> bool:
    """Roll for NFT drop at Zone 5."""
    if not run.get("nft_eligible"):
        return False
    chance = ZONES[5].get("nft_drop_chance", 0.02)
    return random.random() < chance


def beat_needs_question(run: dict) -> bool:
    """
    Returns True if the current beat requires a question answer before choices.
    Only True when: hard mode is active AND the event has a question dict.
    """
    if not run.get("hard_mode"):
        return False
    event = run["events"][run["beat"]]
    return bool(event.get("question"))


# ─────────────────────────────────────────────
# Discord UI — Hard Mode Question View
# ─────────────────────────────────────────────

class QuestionView(discord.ui.View):
    """
    Shown before action choices in hard mode when the event has a question.
    Presents 3 answer buttons. On selection, calls on_answer_callback(interaction, correct: bool).
    After answering, the bot should immediately show ExpeditionView for action choices.
    """

    ANSWER_LABELS = ["🅐", "🅑", "🅒"]

    def __init__(self, run: dict, on_answer_callback):
        super().__init__(timeout=300)
        self.run      = run
        self.callback = on_answer_callback
        self.chosen   = False

        event    = run["events"][run["beat"]]
        question = event["question"]
        answers  = question["answers"]
        correct  = question["correct"]   # 0-indexed

        for i, answer_text in enumerate(answers):
            label = f"{self.ANSWER_LABELS[i]}  {answer_text}"
            btn   = discord.ui.Button(
                label     = label[:80],
                style     = discord.ButtonStyle.secondary,
                custom_id = f"exp_q_{i}",
            )
            btn.callback = self._make_callback(i, i == correct)
            self.add_item(btn)

    def _make_callback(self, answer_index: int, is_correct: bool):
        async def button_callback(interaction: discord.Interaction):
            if self.chosen:
                await interaction.response.send_message(
                    "You've already answered.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children:
                item.disabled = True
            await self.callback(interaction, is_correct)

        return button_callback

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ─────────────────────────────────────────────
# Discord UI — Action Choice View
# ─────────────────────────────────────────────

class ExpeditionView(discord.ui.View):
    """Button view for a single expedition beat's action choices."""

    def __init__(self, run: dict, on_choice_callback):
        super().__init__(timeout=300)
        self.run      = run
        self.callback = on_choice_callback
        self.chosen   = False

        event   = run["events"][run["beat"]]
        choices = event["choices"]

        for i, choice in enumerate(choices):
            # Flag trap buttons visually in hard mode so there's a tell
            is_trap = choice.get("trap", False)
            style   = discord.ButtonStyle.danger if (is_trap and run.get("hard_mode")) else discord.ButtonStyle.primary
            btn = discord.ui.Button(
                label     = choice["label"],
                style     = style,
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
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children:
                item.disabled = True
            await self.callback(interaction, choice_index)

        return button_callback

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ─────────────────────────────────────────────
# Discord UI — Zone Select
# ─────────────────────────────────────────────

class ZoneSelectView(discord.ui.View):
    """Lets the player pick which zone to run."""

    def __init__(self, eligible_zones: list, cp_total: int, on_zone_callback):
        super().__init__(timeout=120)
        self.callback = on_zone_callback
        self.chosen   = False

        for zone_num in eligible_zones:
            zone = ZONES[zone_num]
            btn  = discord.ui.Button(
                label     = f"{zone['emoji']} {zone['name']}",
                style     = discord.ButtonStyle.secondary,
                custom_id = f"zone_{zone_num}",
            )
            btn.callback = self._make_callback(zone_num)
            self.add_item(btn)

    def _make_callback(self, zone_num: int):
        async def button_callback(interaction: discord.Interaction):
            if self.chosen:
                await interaction.response.send_message("Already selected.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children:
                item.disabled = True
            await self.callback(interaction, zone_num)

        return button_callback


# ─────────────────────────────────────────────
# Discord UI — Hard Mode Toggle
# ─────────────────────────────────────────────

class HardModeSelectView(discord.ui.View):
    """
    Shown after zone select. Player opts in or out of Hard Mode.
    Hard Mode: every beat shows a question before action choices.
      - Correct → guaranteed high outcome
      - Wrong   → forced low outcome
    Rewards are NOT multiplied separately; the guaranteed-high / forced-low
    tiers already represent higher CP/token values at their respective ends.
    """

    def __init__(self, zone_num: int, on_mode_callback):
        super().__init__(timeout=120)
        self.callback = on_mode_callback
        self.chosen   = False
        self.zone_num = zone_num

        btn_hard = discord.ui.Button(
            label     = "⚡ Hard Mode — Questions on every beat",
            style     = discord.ButtonStyle.danger,
            custom_id = "hard_mode_yes",
        )
        btn_hard.callback = self._make_callback(True)

        btn_normal = discord.ui.Button(
            label     = "🌿 Normal Mode — Stat-based outcomes",
            style     = discord.ButtonStyle.secondary,
            custom_id = "hard_mode_no",
        )
        btn_normal.callback = self._make_callback(False)

        self.add_item(btn_hard)
        self.add_item(btn_normal)

    def _make_callback(self, hard_mode: bool):
        async def button_callback(interaction: discord.Interaction):
            if self.chosen:
                return
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children:
                item.disabled = True
            await self.callback(interaction, self.zone_num, hard_mode)

        return button_callback

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ─────────────────────────────────────────────
# Discord UI — Zappy Select
# ─────────────────────────────────────────────

class ZappySelectView(discord.ui.View):
    """Lets the player pick which Zappy to send on expedition."""

    def __init__(self, zappies: list, on_zappy_callback):
        super().__init__(timeout=120)
        self.callback = on_zappy_callback
        self.chosen   = False

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
            await interaction.response.defer(ephemeral=True)
            self.chosen = True
            for item in self.children:
                item.disabled = True
            await self.callback(interaction, asset_id)

        return button_callback


# ─────────────────────────────────────────────
# Embed builders
# ─────────────────────────────────────────────

def build_scene_embed(run: dict) -> discord.Embed:
    """
    Build the embed for a beat's opening scene.
    Appends trait hint if the Zappy's dominant stat has one for this event.
    In hard mode, appends a note that a question is coming.
    """
    event    = run["events"][run["beat"]]
    zone     = ZONES[run["zone_num"]]
    beat_num = run["beat"] + 1
    zappy    = run["zappy"]
    stats    = run["stats"]

    description = event["scene"]

    # Trait-reactive hint
    trait_hint = get_trait_hint(event, stats)
    if trait_hint:
        description += f"\n\n*{trait_hint}*"

    # Hard mode question warning
    if run.get("hard_mode") and event.get("question"):
        description += "\n\n⚡ **Hard Mode:** A question awaits before you act."

    embed = discord.Embed(
        title       = f"{zone['emoji']} {event['title']}",
        description = description,
        color       = zone["color"],
    )
    embed.set_author(name=f"{zone['name']} · Beat {beat_num} of 5")

    mode_label = "⚡ Hard Mode" if run.get("hard_mode") else "🌿 Normal"
    embed.set_footer(
        text=(
            f"{zappy.get('name', 'Your Zappy')} · "
            f"VLT {stats.get('VLT','?')} · "
            f"INS {stats.get('INS','?')} · "
            f"SPK {stats.get('SPK','?')} · "
            f"{run['collection_bonus']['label']} · {mode_label}"
        )
    )

    return embed


def build_question_embed(run: dict) -> discord.Embed:
    """
    Build the embed shown with QuestionView in hard mode.
    Displays the question prompt and instructs the player to answer
    before their action choices appear.
    """
    event    = run["events"][run["beat"]]
    question = event["question"]
    zone     = ZONES[run["zone_num"]]

    embed = discord.Embed(
        title       = "⚡ Hard Mode Question",
        description = f"**{question['prompt']}**\n\nChoose wisely — the correct answer guarantees a high outcome. The wrong one guarantees a bad one.",
        color       = 0xE8A838,   # amber — distinct from scene/outcome colors
    )
    embed.set_footer(text=f"{zone['name']} · Answer first, then choose your action.")
    return embed


def build_question_result_embed(correct: bool, event: dict) -> discord.Embed:
    """
    Brief feedback embed shown after the player answers the hard mode question,
    before the action choices appear.
    """
    if correct:
        embed = discord.Embed(
            title       = "✅ Correct",
            description = "Your knowledge holds. A high outcome is locked in — now choose your action.",
            color       = 0x63992A,
        )
    else:
        embed = discord.Embed(
            title       = "❌ Wrong",
            description = "The summit doesn't forgive ignorance. A bad outcome is locked in — but you still have to choose.",
            color       = 0xA32D2D,
        )
    return embed


def build_outcome_embed(run: dict) -> discord.Embed:
    """
    Build the embed showing the result of a choice.
    In hard mode, annotates with question result and trap status.
    """
    outcome  = run["last_outcome"]
    zone     = ZONES[run["zone_num"]]

    tone_colors = {
        "good":    0x63992A,
        "neutral": 0x888780,
        "bad":     0xA32D2D,
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

    # Hard mode annotations
    annotations = []
    if outcome.get("_is_trap"):
        annotations.append("🪤 Trap choice")
    if outcome.get("_hard_mode"):
        if outcome.get("_question_correct") is True:
            annotations.append("✅ Question correct → high outcome")
        elif outcome.get("_question_correct") is False:
            annotations.append("❌ Question wrong → bad outcome")
    if annotations:
        embed.add_field(name="Hard Mode", value="  ·  ".join(annotations), inline=False)

    # Tally for hard mode runs
    if run.get("hard_mode"):
        correct = run.get("hard_mode_correct", 0)
        wrong   = run.get("hard_mode_wrong", 0)
        answered = correct + wrong
        if answered:
            embed.set_author(name=f"Hard Mode: {correct}/{answered} correct so far")

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

    embed.add_field(
        name  = "Run Summary",
        value = (
            f"⚡ **{run['total_cp']} Expedition CP** earned\n"
            f"🪙 **{run['total_tokens']} tokens** collected\n"
            f"📦 Collection bonus: {run['collection_bonus']['label']}"
        ),
        inline=False,
    )

    # Hard mode summary
    if run.get("hard_mode"):
        correct  = run.get("hard_mode_correct", 0)
        wrong    = run.get("hard_mode_wrong", 0)
        answered = correct + wrong
        score_line = f"{correct}/{answered} questions correct" if answered else "No questions answered"
        embed.add_field(
            name  = "⚡ Hard Mode Results",
            value = score_line,
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

    embed.set_footer(
        text=f"{zappy.get('name', 'Your Zappy')} · Use /exprank to see the Expedition leaderboard"
    )
    return embed


# ─────────────────────────────────────────────
# Bot integration reference
# ─────────────────────────────────────────────
#
# COMMAND: /expedition
#
#   Step 1 — Zone select
#       view = ZoneSelectView(eligible_zones, cp_total, on_zone_selected)
#       await interaction.followup.send(embed=zone_select_embed, view=view)
#
#   Step 2 — Hard mode select (on_zone_selected callback)
#       view = HardModeSelectView(zone_num, on_mode_selected)
#       await interaction.followup.send(embed=hard_mode_info_embed, view=view)
#
#   Step 3 — Zappy select (on_mode_selected callback)
#       run_data = {"zone_num": zone_num, "hard_mode": hard_mode}
#       view = ZappySelectView(zappies, on_zappy_selected)
#       await interaction.followup.send(embed=zappy_select_embed, view=view)
#
#   Step 4 — Run starts (on_zappy_selected callback)
#       run = start_run(user_id, zone_num, zappy_data, zappy_count, hard_mode)
#       await send_beat(interaction, run)
#
# FUNCTION: send_beat(interaction, run)
#
#   scene_embed = build_scene_embed(run)
#
#   if beat_needs_question(run):
#       q_embed = build_question_embed(run)
#       q_view  = QuestionView(run, on_question_answered)
#       await interaction.followup.send(embeds=[scene_embed, q_embed], view=q_view)
#   else:
#       choice_view = ExpeditionView(run, on_choice_made)
#       await interaction.followup.send(embed=scene_embed, view=choice_view)
#
# CALLBACK: on_question_answered(interaction, correct: bool)
#
#   run = record_question_answer(user_id, correct)
#   result_embed = build_question_result_embed(correct, run["events"][run["beat"]])
#   choice_view  = ExpeditionView(run, on_choice_made)
#   await interaction.followup.send(embed=result_embed, view=choice_view)
#
# CALLBACK: on_choice_made(interaction, choice_index: int)
#
#   run = advance_beat(user_id, choice_index)
#   outcome_embed = build_outcome_embed(run)
#   await interaction.followup.send(embed=outcome_embed)
#
#   if run["complete"]:
#       nft_drop    = check_nft_drop(run)
#       final_embed = build_run_complete_embed(run, nft_drop)
#       await interaction.followup.send(embed=final_embed)
#       end_run(user_id)
#       # → trigger token transfer, CP save, etc. via on_complete callback
#   else:
#       await send_beat(interaction, run)   # next beat

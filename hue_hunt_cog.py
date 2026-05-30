"""
hue_hunt_cog.py
---------------
Hue Hunt — a solo color-matching game for Zappies Reborn.

Flow:
  - Player clicks "🎨 Hue Hunt" button in the games panel
  - Bot sends an ephemeral embed showing a target color swatch
    and 4 buttons, each a different color choice
  - Correct answer → next round (colors get progressively closer)
  - Wrong answer → game over, small $ZAPP credited to gp_balances,
    score compared against all-time high
  - New all-time high → auto-posts to the games channel publicly
  - /huescores → shows top 10 leaderboard (ephemeral)

Color rendering:
  Discord embeds don't show colored blocks natively, so we use
  embed color (sidebar stripe) for the TARGET color, and the
  4 choice buttons show the color name + hex. Players read the
  sidebar stripe and match it to one of the button labels.

Difficulty curve:
  Round 1: choices are ~120+ hue degrees apart (obviously different)
  Round 5: choices are ~30 degrees apart
  Round 10+: choices are ~10 degrees apart (very subtle)

ZAPP reward: 2 ZAPP per round survived, credited to gp_balances.
"""

import discord
from discord.ext import commands
from discord import app_commands
import random
import colorsys
import asyncio
import os
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
GAMES_CHANNEL_ID  = int(os.environ.get("GAMES_CHANNEL_ID", 0))
SCORES_CHANNEL_ID = int(os.environ.get("SCORES_CHANNEL_ID", 0))
ZAPP_PER_ROUND    = 2         # ZAPP credited per round survived
MAX_CHOICES       = 4         # Number of color buttons shown each round

# ── Color palette pools by difficulty tier ────────────────────────────────────
# Each entry: (name, hex_str, hue_degrees)
# We generate choices dynamically from hue space, but seed with named colors
# for early rounds so they feel recognizable and fun.

NAMED_COLORS = [
    ("Electric Blue",   "#3A86FF", 217),
    ("Zappy Yellow",    "#FFD60A", 51),
    ("Volt Green",      "#57CC99", 153),
    ("Shock Pink",      "#FF006E", 337),
    ("Storm Purple",    "#8338EC", 275),
    ("Thunder Orange",  "#FB5607", 20),
    ("Static Cyan",     "#00F5FF", 185),
    ("Plasma Red",      "#FF4040", 0),
    ("Arc Teal",        "#2EC4B6", 176),
    ("Neon Lime",       "#CCFF00", 74),
    ("Fuzz Magenta",    "#F72585", 322),
    ("Bolt Indigo",     "#4361EE", 231),
    ("Spark Mint",      "#80FFDB", 165),
    ("Overload Coral",  "#FF6B6B", 0),
    ("Ground Slate",    "#6C757D", 210),
    ("Surge Amber",     "#FFAA00", 41),
    ("Pulse Violet",    "#7B2D8B", 288),
    ("Crackle Rose",    "#FF85A1", 347),
    ("Discharge Tan",   "#C9A96E", 35),
    ("Warp Sky",        "#87CEEB", 203),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def hue_to_hex(hue: float, saturation: float = 0.75, lightness: float = 0.55) -> str:
    """Convert HSL hue (0-360) to a hex color string."""
    h = hue / 360.0
    r, g, b = colorsys.hls_to_rgb(h, lightness, saturation)
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


def hex_to_int(hex_str: str) -> int:
    """Convert '#RRGGBB' to an integer for discord.Color."""
    return int(hex_str.lstrip("#"), 16)


def round_spread(round_num: int) -> float:
    """
    Returns how many hue degrees apart the wrong choices should be
    from the correct answer. Decreases as rounds increase.
    """
    if round_num <= 2:
        return 120.0
    elif round_num <= 4:
        return 80.0
    elif round_num <= 6:
        return 45.0
    elif round_num <= 9:
        return 25.0
    elif round_num <= 13:
        return 15.0
    else:
        return 8.0


def generate_round_colors(round_num: int):
    """
    Returns a list of (name, hex_str) tuples for this round.
    Index 0 is always the correct answer.
    The rest are distractors spaced by round_spread() hue degrees.
    """
    spread = round_spread(round_num)

    # Pick correct color
    if round_num <= len(NAMED_COLORS):
        # Early rounds: use recognizable named colors
        correct = NAMED_COLORS[round_num - 1]
        correct_hue = correct[2]
        correct_name = correct[0]
        correct_hex = correct[1]
    else:
        # Later rounds: generate a random hue
        correct_hue = random.uniform(0, 360)
        correct_hex = hue_to_hex(correct_hue)
        correct_name = f"Color #{round_num}"

    # Generate distractor hues, spaced by spread
    distractors = []
    angles = [spread, -spread, spread * 1.6]
    used_names = {correct_name}

    for i, offset in enumerate(angles):
        d_hue = (correct_hue + offset) % 360
        d_hex = hue_to_hex(d_hue, saturation=random.uniform(0.65, 0.85))

        # Try to find a named color near this hue for early rounds
        d_name = None
        if round_num <= len(NAMED_COLORS) + 5:
            for nc in NAMED_COLORS:
                angle_diff = abs(nc[2] - d_hue) % 360
                angle_diff = min(angle_diff, 360 - angle_diff)
                if angle_diff < 25 and nc[0] not in used_names:
                    d_name = nc[0]
                    d_hex = nc[1]
                    break

        if d_name is None:
            d_name = f"Shade {chr(65 + i)}"

        used_names.add(d_name)
        distractors.append((d_name, d_hex))

    choices = [(correct_name, correct_hex)] + distractors
    return choices  # index 0 = correct


# ── In-memory game state ──────────────────────────────────────────────────────
# Keyed by (user_id, message_id) to support multiple concurrent games
active_games: dict[int, dict] = {}
# Structure: { user_id: { "round": int, "correct_name": str, "message_id": int } }


# ── Views ─────────────────────────────────────────────────────────────────────

class HueChoiceButton(discord.ui.Button):
    def __init__(self, color_name: str, color_hex: str, is_correct: bool, cog):
        super().__init__(
            label=f"{color_name}  {color_hex}",
            style=discord.ButtonStyle.secondary,
            custom_id=f"hue_{color_name.replace(' ', '_')}_{color_hex}"
        )
        self.color_name = color_name
        self.color_hex  = color_hex
        self.is_correct = is_correct
        self.cog        = cog

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        state   = active_games.get(user_id)

        if not state:
            await interaction.response.send_message(
                "No active game found. Click **🎨 Hue Hunt** to start a new game.",
                ephemeral=True
            )
            return

        if self.is_correct:
            # Advance round
            state["round"] += 1
            await interaction.response.edit_message(
                embed=self.cog.build_game_embed(state["round"], state["target_hex"]),
                view=self.cog.build_game_view(state["round"], user_id)
            )
            # Update target hex for next round
            next_choices = generate_round_colors(state["round"])
            active_games[user_id]["target_hex"]    = next_choices[0][1]
            active_games[user_id]["correct_name"]  = next_choices[0][0]

        else:
            # Wrong — game over
            score = state["round"] - 1
            del active_games[user_id]

            zapp_earned = score * ZAPP_PER_ROUND
            high_score_beaten = False
            old_high = 0

            # Credit ZAPP to gp_balances
            if zapp_earned > 0:
                try:
                    await asyncio.to_thread(
                        self.cog.db.table("gp_balances").upsert(
                            {"discord_user_id": str(user_id), "zapp_balance": zapp_earned},
                            on_conflict="discord_user_id"
                        ).execute
                    )
                    # Actually do an increment
                    await asyncio.to_thread(
                        lambda: self.cog.db.rpc(
                            "increment_zapp_balance",
                            {"p_user_id": str(user_id), "p_amount": zapp_earned}
                        ).execute()
                    )
                except Exception as e:
                    print(f"[hue_hunt] ZAPP credit error: {e}")

            # Check / update high score
            try:
                result = await asyncio.to_thread(
                    lambda: self.cog.db.table("hue_hunt_scores")
                    .select("score")
                    .eq("discord_user_id", str(user_id))
                    .single()
                    .execute()
                )
                old_high = result.data["score"] if result.data else 0
            except Exception:
                old_high = 0

            if score > old_high:
                high_score_beaten = True
                try:
                    await asyncio.to_thread(
                        lambda: self.cog.db.table("hue_hunt_scores").upsert({
                            "discord_user_id": str(user_id),
                            "username": interaction.user.display_name,
                            "score": score,
                            "achieved_at": datetime.now(timezone.utc).isoformat()
                        }, on_conflict="discord_user_id").execute()
                    )
                except Exception as e:
                    print(f"[hue_hunt] score save error: {e}")

            # Build game-over embed
            embed = discord.Embed(
                title="⚡ Hue Hunt — Game Over",
                color=hex_to_int("#FF4040")
            )
            embed.add_field(
                name="Result",
                value=(
                    f"You survived **{score} round{'s' if score != 1 else ''}**.\n"
                    f"The correct color was **{state['correct_name']}** — you picked **{self.color_name}**."
                ),
                inline=False
            )
            if zapp_earned > 0:
                embed.add_field(
                    name="Reward",
                    value=f"🪙 **+{zapp_earned} ZAPP** added to your balance",
                    inline=False
                )
            if high_score_beaten:
                embed.add_field(
                    name="🏆 New Personal Best!",
                    value=f"Previous best: {old_high} rounds",
                    inline=False
                )

            view = HueGameOverView(score, interaction.user, high_score_beaten, self.cog)
            await interaction.response.edit_message(embed=embed, view=view)

            # Auto-post if all-time high score beaten
            if high_score_beaten:
                await self.cog.maybe_post_new_high_score(interaction, score)


class HueGameView(discord.ui.View):
    def __init__(self, choices, cog):
        super().__init__(timeout=300)
        # choices: list of (name, hex, is_correct)
        for name, hex_str, is_correct in choices:
            self.add_item(HueChoiceButton(name, hex_str, is_correct, cog))


class HueGameOverView(discord.ui.View):
    def __init__(self, score: int, user: discord.Member, is_high_score: bool, cog):
        super().__init__(timeout=120)
        self.score        = score
        self.user         = user
        self.is_high_score = is_high_score
        self.cog          = cog

    @discord.ui.button(label="🔄 Play Again", style=discord.ButtonStyle.primary)
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        embed, view = self.cog.start_new_game(interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="📣 Share Score", style=discord.ButtonStyle.secondary)
    async def share_score(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.guild.get_channel(SCORES_CHANNEL_ID)
        if channel:
            await channel.send(
                f"🎨 **{interaction.user.display_name}** survived **{self.score} round{'s' if self.score != 1 else ''}** "
                f"in Hue Hunt{'! 🏆 New personal best!' if self.is_high_score else '!'}"
            )
        await interaction.response.send_message("Score shared!", ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class HueHuntCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot = bot
        self.db  = db

    def start_new_game(self, user_id: int):
        """Initialize state for round 1 and return (embed, view)."""
        choices = generate_round_colors(1)
        correct_name = choices[0][0]
        correct_hex  = choices[0][1]

        active_games[user_id] = {
            "round":        1,
            "correct_name": correct_name,
            "target_hex":   correct_hex,
        }

        embed = self.build_game_embed(1, correct_hex)
        view  = self.build_game_view(1, user_id, choices)
        return embed, view

    def build_game_embed(self, round_num: int, target_hex: str) -> discord.Embed:
        embed = discord.Embed(
            title=f"🎨 Hue Hunt — Round {round_num}",
            description=(
                "Match the color shown in the **sidebar stripe** on the left.\n"
                "Pick the closest color from the buttons below."
            ),
            color=hex_to_int(target_hex)
        )
        embed.set_footer(text=f"Round {round_num} · Colors get closer as you progress")
        return embed

    def build_game_view(self, round_num: int, user_id: int, choices=None) -> HueGameView:
        if choices is None:
            choices = generate_round_colors(round_num)

        # Shuffle so correct isn't always first button
        correct = choices[0]
        distractors = choices[1:]
        all_choices = [(correct[0], correct[1], True)] + \
                      [(d[0], d[1], False) for d in distractors]
        random.shuffle(all_choices)

        # Store correct name in state
        if user_id in active_games:
            active_games[user_id]["correct_name"] = correct[0]
            active_games[user_id]["target_hex"]   = correct[1]

        return HueGameView(all_choices, self)

    async def maybe_post_new_high_score(self, interaction: discord.Interaction, score: int):
        """Check if this is an all-time server high score and post publicly if so."""
        try:
            result = await asyncio.to_thread(
                lambda: self.db.table("hue_hunt_scores")
                .select("score")
                .order("score", desc=True)
                .limit(1)
                .execute()
            )
            top = result.data[0]["score"] if result.data else 0
            if score >= top:
                channel = interaction.guild.get_channel(SCORES_CHANNEL_ID)
                if channel:
                    embed = discord.Embed(
                        title="🏆 New Hue Hunt High Score!",
                        description=(
                            f"**{interaction.user.display_name}** just set a new all-time record!\n\n"
                            f"⚡ **{score} rounds survived**\n\n"
                            "Think you can beat it? Hit **🎨 Hue Hunt** to try."
                        ),
                        color=hex_to_int("#FFD60A")
                    )
                    embed.set_thumbnail(url=interaction.user.display_avatar.url)
                    await channel.send(embed=embed)
        except Exception as e:
            print(f"[hue_hunt] high score post error: {e}")

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="huescores", description="View the Hue Hunt top 10 leaderboard.")
    async def huescores(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(
                lambda: self.db.table("hue_hunt_scores")
                .select("username, score, achieved_at")
                .order("score", desc=True)
                .limit(10)
                .execute()
            )
            rows = result.data or []
        except Exception as e:
            await interaction.followup.send(f"Error fetching scores: {e}", ephemeral=True)
            return

        if not rows:
            await interaction.followup.send("No scores recorded yet. Be the first!", ephemeral=True)
            return

        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(rows):
            medal = medals[i] if i < 3 else f"**{i+1}.**"
            date  = row["achieved_at"][:10] if row.get("achieved_at") else "—"
            lines.append(f"{medal} **{row['username']}** — {row['score']} rounds  ·  {date}")

        embed = discord.Embed(
            title="🎨 Hue Hunt — Top 10",
            description="\n".join(lines),
            color=hex_to_int("#3A86FF")
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    # db should be passed via bot.db — adjust to match your bot's pattern
    await bot.add_cog(HueHuntCog(bot, bot.db))

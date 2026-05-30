"""
hue_hunt_cog.py
---------------
Hue Hunt — a solo color-matching game for Zappies Reborn.

Flow:
  - Player clicks "🎨 Hue Hunt" in the games panel
  - Bot generates a Pillow image: TARGET swatch on the left,
    4 numbered choice squares on the right
  - Buttons are 1️⃣ 2️⃣ 3️⃣ 4️⃣ — pure visual, no hex codes
  - Correct → next round, colors get progressively closer
  - Wrong → game over, ZAPP credited, high score checked
  - New all-time high → auto-posts to #game-scores

Image layout (800x220px):
  [ MATCH ] | [ 1 ] [ 2 ] [ 3 ] [ 4 ]
  Big target   Four numbered choice squares

Difficulty curve:
  Rounds 1-2:  ~120° hue separation (very obvious)
  Rounds 3-4:  ~80° apart
  Rounds 5-6:  ~45° apart
  Rounds 7-9:  ~25° apart
  Rounds 10-13: ~15° apart
  Round 14+:   ~8° apart (extremely subtle)

ZAPP reward: 2 ZAPP per round survived.
"""

import discord
from discord.ext import commands
from discord import app_commands
import random
import colorsys
import asyncio
import io
import os
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

# ── Config ─────────────────────────────────────────────────────────────────────
GAMES_CHANNEL_ID  = int(os.environ.get("GAMES_CHANNEL_ID", 0))
SCORES_CHANNEL_ID = int(os.environ.get("SCORES_CHANNEL_ID", 0))
ZAPP_PER_ROUND    = 2

# ── Color data ─────────────────────────────────────────────────────────────────
# (name, hex, hue_degrees) — used for early rounds so colors feel vivid/named
NAMED_COLORS = [
    ("Electric Blue",  "#3A86FF", 217),
    ("Zappy Yellow",   "#FFD60A",  51),
    ("Volt Green",     "#57CC99", 153),
    ("Shock Pink",     "#FF006E", 337),
    ("Storm Purple",   "#8338EC", 275),
    ("Thunder Orange", "#FB5607",  20),
    ("Static Cyan",    "#00F5FF", 185),
    ("Plasma Red",     "#FF4040",   0),
    ("Arc Teal",       "#2EC4B6", 176),
    ("Neon Lime",      "#CCFF00",  74),
    ("Fuzz Magenta",   "#F72585", 322),
    ("Bolt Indigo",    "#4361EE", 231),
    ("Spark Mint",     "#80FFDB", 165),
    ("Overload Coral", "#FF6B6B",   5),
    ("Ground Slate",   "#6C757D", 210),
    ("Surge Amber",    "#FFAA00",  41),
    ("Pulse Violet",   "#7B2D8B", 288),
    ("Crackle Rose",   "#FF85A1", 347),
    ("Discharge Tan",  "#C9A96E",  35),
    ("Warp Sky",       "#87CEEB", 203),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def hex_to_rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def hex_to_int(h: str) -> int:
    return int(h.lstrip("#"), 16)


def hue_to_hex(hue: float, sat: float = 0.78, lit: float = 0.54) -> str:
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, lit, sat)
    return "#{:02X}{:02X}{:02X}".format(int(r*255), int(g*255), int(b*255))


def round_spread(round_num: int) -> float:
    if   round_num <= 2:  return 120.0
    elif round_num <= 4:  return 80.0
    elif round_num <= 6:  return 45.0
    elif round_num <= 9:  return 25.0
    elif round_num <= 13: return 15.0
    else:                 return 8.0


def generate_round_colors(round_num: int) -> list:
    """
    Returns [correct_hex, d1_hex, d2_hex, d3_hex] — index 0 is always correct.
    All values are hex strings.
    """
    spread = round_spread(round_num)

    if round_num <= len(NAMED_COLORS):
        nc = NAMED_COLORS[round_num - 1]
        correct_hue = nc[2]
        correct_hex = nc[1]
    else:
        correct_hue = random.uniform(0, 360)
        correct_hex = hue_to_hex(correct_hue)

    offsets = [spread, -spread, spread * 1.55]
    distractors = []
    for offset in offsets:
        d_hue = (correct_hue + offset) % 360
        # Vary sat/lit slightly so colors don't look machine-generated
        d_hex = hue_to_hex(d_hue,
                            sat=random.uniform(0.65, 0.88),
                            lit=random.uniform(0.45, 0.62))
        distractors.append(d_hex)

    return [correct_hex] + distractors  # index 0 = correct


# ── Image generation ───────────────────────────────────────────────────────────

# Image dimensions
IMG_W        = 820
IMG_H        = 220
BG_COLOR     = (43, 45, 49)       # Discord dark
BORDER_COLOR = (70, 72, 80)
TEXT_COLOR   = (180, 184, 190)
LABEL_H      = 28
CORNER_R     = 10
PADDING      = 18
DIVIDER_GAP  = 22
TARGET_W     = 180
CHOICE_W     = 136
CHOICE_GAP   = 12


def _rounded_rect(draw, xy, fill, radius=CORNER_R):
    """Draw a filled rounded rectangle."""
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)


def render_hue_image(target_hex: str, choice_hexes: list, correct_index: int) -> io.BytesIO:
    """
    Render the Hue Hunt round image.
    choice_hexes: list of 4 hex strings in display order (shuffled).
    correct_index: which of the 4 is correct (0-3), stored in state.
    Returns a BytesIO PNG.
    """
    img  = Image.new("RGB", (IMG_W, IMG_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    try:
        # Try to load a font — fall back to default if not available
        font_label  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 15)
        font_number = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    except Exception:
        font_label  = ImageFont.load_default()
        font_number = font_label

    swatch_top    = PADDING
    swatch_bottom = IMG_H - PADDING - LABEL_H - 6
    swatch_h      = swatch_bottom - swatch_top

    # ── Target swatch ──────────────────────────────────────────────────────────
    tx0 = PADDING
    tx1 = tx0 + TARGET_W
    _rounded_rect(draw, [tx0, swatch_top, tx1, swatch_bottom], fill=hex_to_rgb(target_hex))
    # Subtle border
    draw.rounded_rectangle([tx0, swatch_top, tx1, swatch_bottom],
                            radius=CORNER_R, outline=BORDER_COLOR, width=2)
    # "MATCH" label
    draw.text(
        (tx0 + TARGET_W // 2, swatch_bottom + 6),
        "MATCH",
        fill=TEXT_COLOR,
        font=font_label,
        anchor="mt"
    )

    # ── Divider arrow ──────────────────────────────────────────────────────────
    div_x = tx1 + DIVIDER_GAP
    mid_y = IMG_H // 2
    draw.text((div_x - 2, mid_y), "→", fill=(100, 104, 112), font=font_number, anchor="mm")

    # ── Choice squares ─────────────────────────────────────────────────────────
    cx_start = div_x + DIVIDER_GAP + 8
    labels   = ["1", "2", "3", "4"]

    for i, hex_color in enumerate(choice_hexes):
        cx0 = cx_start + i * (CHOICE_W + CHOICE_GAP)
        cx1 = cx0 + CHOICE_W

        _rounded_rect(draw, [cx0, swatch_top, cx1, swatch_bottom], fill=hex_to_rgb(hex_color))
        draw.rounded_rectangle([cx0, swatch_top, cx1, swatch_bottom],
                                radius=CORNER_R, outline=BORDER_COLOR, width=2)

        # Number label below square
        draw.text(
            (cx0 + CHOICE_W // 2, swatch_bottom + 6),
            labels[i],
            fill=TEXT_COLOR,
            font=font_label,
            anchor="mt"
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ── Game state ─────────────────────────────────────────────────────────────────
# { user_id: { "round": int, "correct_index": int, "target_hex": str } }
active_games: dict[int, dict] = {}


# ── Views ──────────────────────────────────────────────────────────────────────

NUMBER_LABELS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]


class HueChoiceButton(discord.ui.Button):
    def __init__(self, display_index: int, is_correct: bool, cog):
        super().__init__(
            label=NUMBER_LABELS[display_index],
            style=discord.ButtonStyle.secondary,
            custom_id=f"hue_choice_{display_index}"
        )
        self.display_index = display_index
        self.is_correct    = is_correct
        self.cog           = cog

    async def callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        state   = active_games.get(user_id)

        if not state:
            await interaction.response.send_message(
                "No active game. Click **🎨 Hue Hunt** to start a new one.",
                ephemeral=True
            )
            return

        if self.is_correct:
            state["round"] += 1
            embed, view, file = await self.cog.build_round(state["round"], user_id)
            await interaction.response.edit_message(
                embed=embed, view=view,
                attachments=[file]
            )
        else:
            score       = state["round"] - 1
            zapp_earned = score * ZAPP_PER_ROUND
            del active_games[user_id]

            # Credit ZAPP to zappy_racers if player is registered
            zapp_credited = False
            if zapp_earned > 0:
                try:
                    racer = await asyncio.to_thread(
                        lambda: self.cog.db.table("zappy_racers")
                        .select("discord_user_id, zapp_balance")
                        .eq("discord_user_id", str(user_id))
                        .order("registered_at")
                        .limit(1)
                        .execute()
                    )
                    if racer.data:
                        current = racer.data[0].get("zapp_balance", 0) or 0
                        await asyncio.to_thread(
                            lambda: self.cog.db.table("zappy_racers")
                            .update({"zapp_balance": current + zapp_earned})
                            .eq("discord_user_id", str(user_id))
                            .execute()
                        )
                        zapp_credited = True
                except Exception as e:
                    print(f"[hue_hunt] ZAPP credit error: {e}")

            # Check personal best
            high_score_beaten = False
            old_high = 0
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
                            "username":        interaction.user.display_name,
                            "score":           score,
                            "achieved_at":     datetime.now(timezone.utc).isoformat()
                        }, on_conflict="discord_user_id").execute()
                    )
                except Exception as e:
                    print(f"[hue_hunt] score save error: {e}")

            embed = discord.Embed(
                title="🎨 Hue Hunt — Game Over",
                color=hex_to_int("#FF4040")
            )
            embed.add_field(
                name="Result",
                value=f"You survived **{score} round{'s' if score != 1 else ''}**.",
                inline=False
            )
            if zapp_earned > 0:
                if zapp_credited:
                    embed.add_field(
                        name="Reward",
                        value=f"🪙 **+{zapp_earned} ZAPP** added to your balance",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="🪙 You earned ZAPP!",
                        value=(
                            f"You would have earned **{zapp_earned} ZAPP** but you're not registered for Grand Prix yet.\n"
                            f"Use `/link` and `/gpregister` to start earning ZAPP from games."
                        ),
                        inline=False
                    )
            if high_score_beaten:
                embed.add_field(
                    name="🏆 New Personal Best!",
                    value=f"Previous best: {old_high} rounds",
                    inline=False
                )

            view = HueGameOverView(score, interaction.user, high_score_beaten, self.cog)
            await interaction.response.edit_message(embed=embed, view=view, attachments=[])

            if high_score_beaten:
                await self.cog.maybe_post_new_high_score(interaction, score)


class HueGameView(discord.ui.View):
    def __init__(self, correct_index: int, cog):
        super().__init__(timeout=300)
        for i in range(4):
            self.add_item(HueChoiceButton(i, i == correct_index, cog))


class HueGameOverView(discord.ui.View):
    def __init__(self, score: int, user: discord.Member, is_high_score: bool, cog):
        super().__init__(timeout=120)
        self.score         = score
        self.user          = user
        self.is_high_score = is_high_score
        self.cog           = cog

    @discord.ui.button(label="🔄 Play Again", style=discord.ButtonStyle.primary)
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        embed, view, file = await self.cog.build_round(1, interaction.user.id, new_game=True)
        await interaction.response.edit_message(embed=embed, view=view, attachments=[file])

    @discord.ui.button(label="📣 Share Score", style=discord.ButtonStyle.secondary)
    async def share_score(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.guild.get_channel(SCORES_CHANNEL_ID)
        if channel:
            await channel.send(
                f"🎨 **{interaction.user.display_name}** survived "
                f"**{self.score} round{'s' if self.score != 1 else ''}** in Hue Hunt"
                f"{'! 🏆 New personal best!' if self.is_high_score else '!'}"
            )
        await interaction.response.send_message("Score shared!", ephemeral=True)


# ── Cog ────────────────────────────────────────────────────────────────────────

class HueHuntCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot = bot
        self.db  = db

    async def build_round(self, round_num: int, user_id: int, new_game: bool = False) -> tuple:
        """
        Build embed + view + Discord file for a round.
        If new_game=True, resets state. Otherwise updates existing state.
        Returns (embed, view, discord.File).
        """
        colors       = generate_round_colors(round_num)
        correct_hex  = colors[0]
        distractors  = colors[1:]

        # Shuffle: place correct hex at a random position among 4
        correct_position = random.randint(0, 3)
        choice_hexes     = distractors[:3]  # 3 wrong ones
        choice_hexes.insert(correct_position, correct_hex)

        # Update/init state
        if new_game or user_id not in active_games:
            active_games[user_id] = {}
        active_games[user_id].update({
            "round":         round_num,
            "correct_index": correct_position,
            "target_hex":    correct_hex,
        })

        # Generate image in thread to avoid blocking
        buf = await asyncio.to_thread(
            render_hue_image, correct_hex, choice_hexes, correct_position
        )
        file = discord.File(buf, filename=f"hue_hunt_r{round_num}.png")

        spread = round_spread(round_num)
        if spread >= 80:   diff_label = "Easy"
        elif spread >= 40: diff_label = "Medium"
        elif spread >= 20: diff_label = "Hard"
        else:              diff_label = "Very Hard 👀"

        embed = discord.Embed(
            title=f"🎨 Hue Hunt — Round {round_num}",
            description="Which numbered square matches **MATCH**? Pick a button below.",
            color=hex_to_int(correct_hex)
        )
        embed.set_image(url=f"attachment://hue_hunt_r{round_num}.png")
        embed.set_footer(text=f"Round {round_num} · {diff_label}")

        view = HueGameView(correct_position, self)
        return embed, view, file

    def start_new_game(self, user_id: int):
        """
        Synchronous wrapper called from games_panel_cog.
        Returns a coroutine — caller must await it.
        """
        return self.build_round(1, user_id, new_game=True)

    async def maybe_post_new_high_score(self, interaction: discord.Interaction, score: int):
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
                            f"🎨 **{score} rounds survived**\n\n"
                            "Think you can beat it? Hit **🎨 Hue Hunt** to try."
                        ),
                        color=hex_to_int("#FFD60A")
                    )
                    embed.set_thumbnail(url=interaction.user.display_avatar.url)
                    await channel.send(embed=embed)
        except Exception as e:
            print(f"[hue_hunt] high score post error: {e}")

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
            await interaction.followup.send("No scores yet. Be the first!", ephemeral=True)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, row in enumerate(rows):
            medal = medals[i] if i < 3 else f"**{i+1}.**"
            date  = row["achieved_at"][:10] if row.get("achieved_at") else "—"
            lines.append(f"{medal} **{row['username']}** — {row['score']} rounds · {date}")

        embed = discord.Embed(
            title="🎨 Hue Hunt — Top 10",
            description="\n".join(lines),
            color=hex_to_int("#3A86FF")
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HueHuntCog(bot, bot.supabase))

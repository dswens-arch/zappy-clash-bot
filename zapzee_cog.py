"""
zapzee_cog.py
-------------
Zapzee — a solo Yahtzee-style dice game for Zappies Reborn.

Flow:
  - Player clicks "🎲 Zapzee" in the games panel
  - Bot renders 5 custom dice as a Pillow image
  - Player has up to 3 rolls per turn:
      • Toggle dice to keep with buttons 1️⃣-5️⃣
      • Click 🎲 Roll to reroll unkept dice
  - After final roll, player picks a scoring category from buttons
  - 13 rounds, then final score + ZAPP reward

Scoring (standard Yahtzee):
  Upper: Ones, Twos, Threes, Fours, Fives, Sixes
  Upper bonus: +35 if upper total >= 63
  Lower: Three of a Kind, Four of a Kind, Full House (25),
         Small Straight (30), Large Straight (40),
         Zapzee (50), Chance

ZAPP rewards:
  300+: 30 ZAPP
  250+: 20 ZAPP
  200+: 12 ZAPP
  150+: 6 ZAPP
  <150:  3 ZAPP

Dice assets: /app/dice/face_1.png through face_6.png
"""

import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
import io
import os
import time
from collections import Counter
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

# ── Config ─────────────────────────────────────────────────────────────────────
SCORES_CHANNEL_ID = int(os.environ.get("SCORES_CHANNEL_ID", 0))

ZAPP_TIERS = [(300, 30), (250, 20), (200, 12), (150, 6), (0, 3)]

CATEGORIES = [
    ("ones",           "1s"),
    ("twos",           "2s"),
    ("threes",         "3s"),
    ("fours",          "4s"),
    ("fives",          "5s"),
    ("sixes",          "6s"),
    ("three_of_a_kind","3 of a Kind"),
    ("four_of_a_kind", "4 of a Kind"),
    ("full_house",     "Full House"),
    ("small_straight", "Sm. Straight"),
    ("large_straight", "Lg. Straight"),
    ("zapzee",         "ZAPZEE ⚡"),
    ("chance",         "Chance"),
]

CAT_KEYS  = [c[0] for c in CATEGORIES]
CAT_NAMES = {c[0]: c[1] for c in CATEGORIES}

# ── Font loader ────────────────────────────────────────────────────────────────

def _get_font(size: int) -> ImageFont.ImageFont:
    search_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/app/Ubuntu-Bold.ttf",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "Ubuntu-Bold.ttf"),
    ]
    for path in search_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass

    font_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Ubuntu-Bold.ttf")
    if not os.path.exists(font_path):
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://github.com/google/fonts/raw/main/ufl/ubuntu/Ubuntu-Bold.ttf",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req) as resp, open(font_path, "wb") as f:
                f.write(resp.read())
            print(f"[zapzee] Downloaded font to {font_path}")
        except Exception as e:
            print(f"[zapzee] Font download failed: {e}")

    if os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass

    return ImageFont.load_default()


# ── Dice image loader ──────────────────────────────────────────────────────────

DICE_CACHE: dict[int, Image.Image] = {}
DICE_W = 180
DICE_H = 152

def _load_dice(base_dir: str):
    """Load all 6 dice face images into cache."""
    for val in range(1, 7):
        path = os.path.join(base_dir, f"face_{val}.png")
        if os.path.exists(path):
            DICE_CACHE[val] = Image.open(path).convert("RGB").resize((DICE_W, DICE_H), Image.LANCZOS)
        else:
            # Fallback: generate a plain colored square with number
            img = Image.new("RGB", (DICE_W, DICE_H), (234, 100, 84))
            draw = ImageDraw.Draw(img)
            font = _get_font(60)
            draw.text((DICE_W//2, DICE_H//2), str(val), fill=(80,40,100), font=font, anchor="mm")
            DICE_CACHE[val] = img


# ── Image renderer ─────────────────────────────────────────────────────────────

GAP     = 20
PADDING = 30
BG      = (30, 31, 34)


def render_dice(dice: list[int], kept: list[bool], rolls_left: int) -> io.BytesIO:
    """Render 5 dice with keep/reroll states."""
    label_h = 50
    img_w   = PADDING*2 + DICE_W*5 + GAP*4
    img_h   = PADDING*2 + DICE_H + label_h + 20

    img  = Image.new("RGB", (img_w, img_h), BG)
    draw = ImageDraw.Draw(img)
    font    = _get_font(24)
    rl_font = _get_font(22)

    for i, (val, keep) in enumerate(zip(dice, kept)):
        x = PADDING + i * (DICE_W + GAP)
        y = PADDING

        if keep:
            draw.rounded_rectangle(
                [x-8, y-8, x+DICE_W+8, y+DICE_H+8],
                radius=14, fill=(87, 204, 153)
            )
        else:
            draw.rounded_rectangle(
                [x-4, y-4, x+DICE_W+4, y+DICE_H+4],
                radius=10, fill=(55, 57, 63)
            )

        face = DICE_CACHE.get(val)
        if face:
            img.paste(face, (x, y))

        lx    = x + DICE_W // 2
        ly    = y + DICE_H + 10
        label = "KEEP" if keep else "reroll"
        color = (87, 204, 153) if keep else (128, 132, 142)
        draw.text((lx, ly), label, fill=color, font=font, anchor="mt")

    # Rolls left indicator
    draw.text(
        (img_w - PADDING, img_h - 10),
        f"{rolls_left} roll{'s' if rolls_left != 1 else ''} left",
        fill=(160, 164, 172),
        font=rl_font,
        anchor="rb"
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ── Scoring engine ─────────────────────────────────────────────────────────────

def score_roll(dice: list[int]) -> dict[str, int]:
    counts  = Counter(dice)
    freq    = sorted(counts.values(), reverse=True)
    total   = sum(dice)
    unique  = set(dice)

    sm = 30 if any(s.issubset(unique) for s in [{1,2,3,4},{2,3,4,5},{3,4,5,6}]) else 0
    lg = 40 if unique in [{1,2,3,4,5},{2,3,4,5,6}] else 0

    return {
        "ones":           counts[1] * 1,
        "twos":           counts[2] * 2,
        "threes":         counts[3] * 3,
        "fours":          counts[4] * 4,
        "fives":          counts[5] * 5,
        "sixes":          counts[6] * 6,
        "three_of_a_kind": total if freq[0] >= 3 else 0,
        "four_of_a_kind":  total if freq[0] >= 4 else 0,
        "full_house":      25 if sorted(freq[:2]) == [2, 3] else 0,
        "small_straight":  sm,
        "large_straight":  lg,
        "zapzee":          50 if freq[0] == 5 else 0,
        "chance":          total,
    }


def calc_total(scorecard: dict) -> tuple[int, int, int]:
    """Returns (upper_total, bonus, grand_total)."""
    upper_keys = ["ones","twos","threes","fours","fives","sixes"]
    upper = sum(scorecard.get(k, 0) for k in upper_keys if scorecard.get(k) is not None)
    bonus = 35 if upper >= 63 else 0
    lower = sum(v for k, v in scorecard.items()
                if k not in upper_keys and v is not None)
    return upper, bonus, upper + bonus + lower


def scorecard_text(scorecard: dict) -> str:
    """Format scorecard as embed field text."""
    upper_keys = ["ones","twos","threes","fours","fives","sixes"]
    lower_keys = [k for k in CAT_KEYS if k not in upper_keys]

    lines = ["**Upper Section**"]
    upper_total = 0
    for k in upper_keys:
        val = scorecard.get(k)
        mark = f"**{val}**" if val is not None else "—"
        lines.append(f"{CAT_NAMES[k]}: {mark}")
        if val is not None:
            upper_total += val

    bonus = 35 if upper_total >= 63 else 0
    lines.append(f"Bonus (≥63): **{bonus}** {'✓' if bonus else f'({63-upper_total} away)'}")
    lines.append("")
    lines.append("**Lower Section**")
    for k in lower_keys:
        val = scorecard.get(k)
        mark = f"**{val}**" if val is not None else "—"
        lines.append(f"{CAT_NAMES[k]}: {mark}")

    _, _, grand = calc_total(scorecard)
    lines.append(f"\n**Total: {grand}**")
    return "\n".join(lines)


# ── Game state ─────────────────────────────────────────────────────────────────
# { user_id: { dice, kept, rolls_left, scorecard, round, start_time } }
active_games: dict[int, dict] = {}


# ── Views ──────────────────────────────────────────────────────────────────────

class ZapzeeRollView(discord.ui.View):
    """View during rolling phase — toggle keep buttons + roll button."""
    def __init__(self, cog, user_id: int):
        super().__init__(timeout=None)
        self.cog     = cog
        self.user_id = user_id
        state = active_games.get(user_id, {})
        self._add_buttons(state)

    def _add_buttons(self, state):
        kept  = state.get("kept", [False]*5)
        rolls = state.get("rolls_left", 2)
        labels = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣"]

        for i in range(5):
            btn = discord.ui.Button(
                label=labels[i],
                style=discord.ButtonStyle.success if kept[i] else discord.ButtonStyle.secondary,
                custom_id=f"zapzee_keep_{i}",
                row=0
            )
            async def make_toggle(idx=i):
                async def toggle(interaction: discord.Interaction):
                    if interaction.user.id != self.user_id:
                        await interaction.response.send_message("Not your game!", ephemeral=True)
                        return
                    s = active_games.get(self.user_id)
                    if not s: return
                    s["kept"][idx] = not s["kept"][idx]
                    buf  = await asyncio.to_thread(render_dice, s["dice"], s["kept"], s["rolls_left"])
                    file = discord.File(buf, filename="zapzee.png")
                    embed = self.cog.build_roll_embed(s)
                    await interaction.response.edit_message(
                        embed=embed, view=ZapzeeRollView(self.cog, self.user_id), attachments=[file]
                    )
                return toggle
            btn.callback = await_or_sync(make_toggle)
            self.add_item(btn)

        # Roll button
        roll_btn = discord.ui.Button(
            label=f"🎲 Roll ({rolls} left)",
            style=discord.ButtonStyle.primary,
            custom_id="zapzee_roll",
            row=1,
            disabled=rolls <= 0
        )
        roll_btn.callback = self._roll_callback
        self.add_item(roll_btn)

        # Score button
        score_btn = discord.ui.Button(
            label="📋 Score This Roll",
            style=discord.ButtonStyle.secondary,
            custom_id="zapzee_score",
            row=1
        )
        score_btn.callback = self._score_callback
        self.add_item(score_btn)

        # Scorecard button
        card_btn = discord.ui.Button(
            label="📊 Scorecard",
            style=discord.ButtonStyle.secondary,
            custom_id="zapzee_scorecard",
            row=1
        )
        card_btn.callback = self._scorecard_callback
        self.add_item(card_btn)

    async def _roll_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your game!", ephemeral=True)
            return
        s = active_games.get(self.user_id)
        if not s or s["rolls_left"] <= 0:
            await interaction.response.send_message("No rolls left — score this roll!", ephemeral=True)
            return

        # Reroll unkept dice
        for i in range(5):
            if not s["kept"][i]:
                s["dice"][i] = random.randint(1, 6)
        s["rolls_left"] -= 1

        buf  = await asyncio.to_thread(render_dice, s["dice"], s["kept"], s["rolls_left"])
        file = discord.File(buf, filename="zapzee.png")
        embed = self.cog.build_roll_embed(s)
        await interaction.response.edit_message(
            embed=embed, view=ZapzeeRollView(self.cog, self.user_id), attachments=[file]
        )

    async def _scorecard_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your game!", ephemeral=True)
            return
        s = active_games.get(self.user_id)
        if not s:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        scorecard = s["scorecard"]
        current_scores = score_roll(s["dice"])
        upper_keys = ["ones","twos","threes","fours","fives","sixes"]

        # Upper section
        upper_lines = []
        upper_total = 0
        for k in upper_keys:
            val = scorecard.get(k)
            if val is not None:
                upper_lines.append(f"~~{CAT_NAMES[k]}~~  **{val}** ✓")
                upper_total += val
            else:
                potential = current_scores[k]
                hint = f"  *(roll: {potential})*" if potential > 0 else ""
                upper_lines.append(f"{CAT_NAMES[k]}{hint}")

        bonus = 35 if upper_total >= 63 else 0
        needed = max(0, 63 - upper_total)
        bonus_line = f"Bonus: **+35** ✓" if bonus else f"Bonus: {needed} more needed"

        # Lower section
        lower_keys = [k for k in CAT_KEYS if k not in upper_keys]
        lower_lines = []
        for k in lower_keys:
            val = scorecard.get(k)
            if val is not None:
                lower_lines.append(f"~~{CAT_NAMES[k]}~~  **{val}** ✓")
            else:
                potential = current_scores[k]
                hint = f"  *(roll: {potential})*" if potential > 0 else ""
                lower_lines.append(f"{CAT_NAMES[k]}{hint}")

        _, _, grand = calc_total(scorecard)
        rounds_done = sum(1 for v in scorecard.values() if v is not None)

        embed = discord.Embed(title="📊 Zapzee — Your Scorecard", color=0xFFD60A)
        embed.add_field(
            name="Upper Section",
            value="\n".join(upper_lines) + f"\n{bonus_line}",
            inline=True
        )
        embed.add_field(
            name="Lower Section",
            value="\n".join(lower_lines),
            inline=True
        )
        embed.add_field(
            name="Progress",
            value=f"Round {s['round']}/13 · **{grand} pts** so far\n*(numbers in italic = potential score for this roll)*",
            inline=False
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _score_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your game!", ephemeral=True)
            return
        s = active_games.get(self.user_id)
        if not s: return
        scores    = score_roll(s["dice"])
        scorecard = s["scorecard"]
        available = [k for k in CAT_KEYS if scorecard.get(k) is None]

        embed = self.cog.build_roll_embed(s)
        embed.add_field(
            name="Choose a Category",
            value="Pick where to score this roll:",
            inline=False
        )
        view = ZapzeeScoringView(self.cog, self.user_id, scores, available)
        await interaction.response.edit_message(embed=embed, view=view)


def await_or_sync(coro_factory):
    """Helper to create button callbacks from async factories."""
    import asyncio as _asyncio
    loop = None
    try:
        loop = _asyncio.get_event_loop()
    except Exception:
        pass

    async def callback(interaction):
        fn = await coro_factory()
        await fn(interaction)
    return callback


class ZapzeeScoringView(discord.ui.View):
    """Category selection view — shown after player hits Score This Roll."""
    def __init__(self, cog, user_id: int, scores: dict, available: list):
        super().__init__(timeout=120)
        self.cog       = cog
        self.user_id   = user_id
        self.scores    = scores
        self.available = available

        row = 0
        count = 0
        for key in CAT_KEYS:
            if key not in available:
                continue
            val  = scores[key]
            name = CAT_NAMES[key]
            btn  = discord.ui.Button(
                label=f"{name}: {val}",
                style=discord.ButtonStyle.primary if val > 0 else discord.ButtonStyle.secondary,
                custom_id=f"zapzee_cat_{key}",
                row=row
            )
            async def make_score_cb(k=key, v=val):
                async def cb(interaction: discord.Interaction):
                    await self.cog.apply_score(interaction, self.user_id, k, v)
                return cb
            btn.callback = await_or_sync(make_score_cb)
            self.add_item(btn)
            count += 1
            if count % 4 == 0:
                row += 1
            if row >= 5:
                break


class ZapzeeEndView(discord.ui.View):
    def __init__(self, user_id, user, total, cog):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.user    = user
        self.total   = total
        self.cog     = cog

    @discord.ui.button(label="🎲 Play Again", style=discord.ButtonStyle.primary)
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Not your game!", ephemeral=True)
            return
        embed, view, file = await self.cog.start_game(self.user_id)
        await interaction.response.edit_message(embed=embed, view=view, attachments=[file])

    @discord.ui.button(label="📣 Share Score", style=discord.ButtonStyle.secondary)
    async def share_score(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.guild.get_channel(SCORES_CHANNEL_ID)
        if not channel:
            await interaction.response.send_message("Scores channel not configured.", ephemeral=True)
            return
        await channel.send(
            f"🎲 **{interaction.user.display_name}** scored **{self.total}** in Zapzee!"
        )
        await interaction.response.send_message("Score shared!", ephemeral=True)


# ── Cog ────────────────────────────────────────────────────────────────────────

class ZapzeeCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot      = bot
        self.db       = db
        self.dice_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dice")
        _load_dice(self.dice_dir)
        print(f"[zapzee] Loaded {len(DICE_CACHE)} dice faces from {self.dice_dir}")

    def build_roll_embed(self, state: dict) -> discord.Embed:
        round_num = state["round"]
        rolls_left = state["rolls_left"]
        _, _, total = calc_total(state["scorecard"])
        rounds_done = sum(1 for v in state["scorecard"].values() if v is not None)

        embed = discord.Embed(
            title=f"🎲 Zapzee — Round {round_num}/13",
            color=0xFFD60A
        )
        embed.add_field(name="Score", value=str(total), inline=True)
        embed.add_field(name="Rounds Left", value=str(13 - rounds_done), inline=True)
        embed.add_field(name="Rolls Left", value=str(rolls_left), inline=True)
        embed.add_field(
            name="Dice",
            value=" ".join(str(d) for d in state["dice"]),
            inline=False
        )
        embed.set_image(url="attachment://zapzee.png")
        embed.set_footer(text="Toggle dice to keep with the number buttons, then Roll or Score")
        return embed

    async def start_game(self, user_id: int):
        dice = [random.randint(1, 6) for _ in range(5)]
        state = {
            "dice":       dice,
            "kept":       [False] * 5,
            "rolls_left": 2,
            "scorecard":  {k: None for k in CAT_KEYS},
            "round":      1,
            "start_time": time.time(),
        }
        active_games[user_id] = state

        buf  = await asyncio.to_thread(render_dice, dice, state["kept"], state["rolls_left"])
        file = discord.File(buf, filename="zapzee.png")
        embed = self.build_roll_embed(state)
        view  = ZapzeeRollView(self, user_id)
        return embed, view, file

    async def apply_score(self, interaction: discord.Interaction, user_id: int, category: str, value: int):
        s = active_games.get(user_id)
        if not s:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        s["scorecard"][category] = value
        rounds_done = sum(1 for v in s["scorecard"].values() if v is not None)

        if rounds_done >= 13:
            # Game over
            upper, bonus, grand = calc_total(s["scorecard"])
            elapsed = int(time.time() - s["start_time"])
            zapp = next(z for threshold, z in ZAPP_TIERS if grand >= threshold)

            # Credit ZAPP
            zapp_credited = False
            try:
                racer = await asyncio.to_thread(
                    lambda: self.db.table("zappy_racers")
                    .select("discord_user_id, zapp_balance")
                    .eq("discord_user_id", str(user_id))
                    .order("registered_at")
                    .limit(1)
                    .execute()
                )
                if racer.data:
                    current = racer.data[0].get("zapp_balance", 0) or 0
                    await asyncio.to_thread(
                        lambda: self.db.table("zappy_racers")
                        .update({"zapp_balance": current + zapp})
                        .eq("discord_user_id", str(user_id))
                        .execute()
                    )
                    zapp_credited = True
            except Exception as e:
                print(f"[zapzee] ZAPP credit error: {e}")

            # Save score — check against server-wide all-time high
            is_best = False
            try:
                # Get current server-wide high score
                server_high = await asyncio.to_thread(
                    lambda: self.db.table("zapzee_scores")
                    .select("total")
                    .order("total", desc=True)
                    .limit(1)
                    .execute()
                )
                prev_server_best = server_high.data[0]["total"] if server_high.data else 0
                if grand > prev_server_best:
                    is_best = True

                await asyncio.to_thread(
                    lambda: self.db.table("zapzee_scores").insert({
                        "discord_user_id": str(user_id),
                        "username":        interaction.user.display_name,
                        "total":           grand,
                        "upper":           upper,
                        "bonus":           bonus,
                        "elapsed":         elapsed,
                        "achieved_at":     datetime.now(timezone.utc).isoformat()
                    }).execute()
                )
            except Exception as e:
                print(f"[zapzee] score save error: {e}")

            del active_games[user_id]

            embed = discord.Embed(title="🎲 Zapzee — Game Over!", color=0xFFD60A)
            embed.add_field(name="Upper Section", value=str(upper),        inline=True)
            embed.add_field(name="Bonus",         value=str(bonus),        inline=True)
            embed.add_field(name="Grand Total",   value=f"**{grand}**",    inline=True)
            embed.add_field(name="Scorecard", value=scorecard_text(s["scorecard"]), inline=False)
            if zapp_credited:
                embed.add_field(name="Reward", value=f"🪙 **+{zapp} ZAPP**", inline=False)
            else:
                embed.add_field(
                    name="🪙 Earn ZAPP",
                    value="Use `/link` and `/gpregister` to earn ZAPP from games.",
                    inline=False
                )
            if is_best:
                embed.add_field(name="🏆 New Personal Best!", value=f"{grand} points!", inline=False)

            view = ZapzeeEndView(user_id, interaction.user, grand, self)
            await interaction.response.edit_message(embed=embed, view=view, attachments=[])

            if is_best:
                await self._post_high_score(interaction, grand)
        else:
            # Next round
            s["round"]      += 1
            s["dice"]        = [random.randint(1, 6) for _ in range(5)]
            s["kept"]        = [False] * 5
            s["rolls_left"]  = 2

            buf  = await asyncio.to_thread(render_dice, s["dice"], s["kept"], s["rolls_left"])
            file = discord.File(buf, filename="zapzee.png")
            embed = self.build_roll_embed(s)
            embed.add_field(
                name=f"✓ {CAT_NAMES[category]} scored",
                value=f"{value} points",
                inline=False
            )
            view = ZapzeeRollView(self, user_id)
            await interaction.response.edit_message(embed=embed, view=view, attachments=[file])

    async def _post_high_score(self, interaction: discord.Interaction, total: int):
        try:
            channel = interaction.guild.get_channel(SCORES_CHANNEL_ID)
            if channel:
                embed = discord.Embed(
                    title="🏆 New Zapzee Server Record!",
                    description=(
                        f"**{interaction.user.display_name}** just set a new all-time high score!\n\n"
                        f"🎲 **{total} points**\n\n"
                        "Think you can beat it? Hit **🎲 Zapzee** to try."
                    ),
                    color=0xFFD60A
                )
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                await channel.send(embed=embed)
        except Exception as e:
            print(f"[zapzee] high score post error: {e}")

    @app_commands.command(name="zapzeescores", description="View the Zapzee top 10 leaderboard.")
    async def zapzeescores(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(
                lambda: self.db.table("zapzee_scores")
                .select("username, total, achieved_at")
                .order("total", desc=True)
                .limit(10)
                .execute()
            )
            rows = result.data or []
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
            return

        if not rows:
            await interaction.followup.send("No scores yet. Be the first!", ephemeral=True)
            return

        medals = ["🥇","🥈","🥉"]
        lines  = []
        for i, row in enumerate(rows):
            medal = medals[i] if i < 3 else f"**{i+1}.**"
            date  = row["achieved_at"][:10] if row.get("achieved_at") else "—"
            lines.append(f"{medal} **{row['username']}** — {row['total']} pts · {date}")

        embed = discord.Embed(
            title="🎲 Zapzee — Top 10",
            description="\n".join(lines),
            color=0xFFD60A
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ZapzeeCog(bot, bot.supabase))

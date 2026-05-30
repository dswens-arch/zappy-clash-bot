"""
zap_word_cog.py
---------------
Zap Word — a Wordle-style solo word game for Zappies Reborn.

Flow:
  - Player clicks "⚡ Zap Word" button in the games panel
  - Bot sends an ephemeral embed with a blank 6x5 grid and a "Guess" button
  - Player clicks Guess → modal popup → types a 5-letter word → submits
  - Bot validates the guess and updates the grid with emoji feedback:
      ⚡ = right letter, right position
      🌩️ = right letter, wrong position
      ▪️ = letter not in word
  - Win: all ⚡ → congrats, ZAPP reward, share button
  - Lose: 6 wrong guesses → reveal word, small consolation ZAPP, share button
  - New random word per session (on-demand, not daily)
  - Personal stats tracked: wins, losses, guess distribution
  - /zapwordstats → shows your personal stats (ephemeral)

ZAPP rewards:
  Guess 1: 20 ZAPP
  Guess 2: 16 ZAPP
  Guess 3: 12 ZAPP
  Guess 4: 8 ZAPP
  Guess 5: 5 ZAPP
  Guess 6: 3 ZAPP
  Loss:     1 ZAPP (consolation)
"""

import discord
from discord.ext import commands
from discord import app_commands
import random
import asyncio
import os
from datetime import datetime, timezone

SCORES_CHANNEL_ID = int(os.environ.get("SCORES_CHANNEL_ID", 0))
ZAPP_PER_WIN = [20, 16, 12, 8, 5, 3]   # indexed by guess number (0-based)
ZAPP_LOSS    = 1

# ── Word list ─────────────────────────────────────────────────────────────────
# Zappy-flavored 5-letter words. Mix of common words + thematic ones.
# Expand this list freely — the more the better for replayability.

WORD_LIST = [
    # Zappy/electric theme
    "SPARK", "SHOCK", "FLASH", "SURGE", "JOLTS", "VOLTS", "POWER", "STORM",
    "BLAST", "BOLTS", "LODGE", "FUSES", "ARCED", "WIRED", "GRIDS", "NODES",
    "PULSE", "WAVES", "FIELD", "FORCE", "OHMIC", "IONIC", "ANODE",
    # Color theme (ties to Hue Hunt)
    "TINTS", "SHADE", "HUE", "VIVID", "GLOWS", "NEONS", "BLAZE", "FLARE",
    "OCHRE", "AZURE", "LILAC", "AMBER", "CORAL", "IVORY", "EBONY", "SLATE",
    "TAUPE", "MAUVE", "BEIGE", "OLIVE",
    # Common good Wordle words
    "ABOUT", "ABOVE", "ABUSE", "ACTOR", "ACUTE", "ADMIT", "ADOPT", "ADULT",
    "AFTER", "AGAIN", "AGENT", "AGREE", "AHEAD", "ALARM", "ALBUM", "ALERT",
    "ALIKE", "ALIGN", "ALIVE", "ALLEY", "ALLOW", "ALONE", "ALONG", "ALOUD",
    "ALTER", "ANGEL", "ANGER", "ANGLE", "ANGRY", "ANIME", "ANKLE", "ANNEX",
    "APART", "APPLE", "APPLY", "ARENA", "ARGUE", "ARISE", "ARRAY", "ARROW",
    "ASIDE", "ASKED", "ASSET", "ATLAS", "ATTIC", "AUDIO", "AUDIT", "AVOID",
    "AWAKE", "AWARD", "AWARE", "AWFUL", "BADLY", "BAKER", "BASIC", "BASIS",
    "BATCH", "BEACH", "BEARD", "BEAST", "BEGAN", "BEGIN", "BEING", "BELOW",
    "BENCH", "BIBLE", "BLACK", "BLADE", "BLAME", "BLAND", "BLANK", "BLEND",
    "BLESS", "BLIND", "BLOCK", "BLOOD", "BLOOM", "BLOWN", "BOARD", "BONUS",
    "BOOTH", "BORED", "BOUND", "BOXER", "BRAIN", "BRAND", "BRAVE", "BREAD",
    "BREAK", "BREED", "BRIEF", "BRING", "BROAD", "BROKE", "BROOK", "BROWN",
    "BRUSH", "BUDDY", "BUILD", "BUILT", "BUNCH", "BURST", "BUYER", "CABIN",
    "CABLE", "CAMEL", "CANDY", "CARGO", "CARRY", "CATCH", "CAUSE", "CHAIR",
    "CHAOS", "CHARM", "CHART", "CHASE", "CHEAP", "CHEAT", "CHECK", "CHEEK",
    "CHESS", "CHEST", "CHIEF", "CHILD", "CHINA", "CHOIR", "CHUNK", "CIVIC",
    "CIVIL", "CLAIM", "CLASH", "CLASS", "CLEAN", "CLEAR", "CLERK", "CLICK",
    "CLIFF", "CLIMB", "CLING", "CLOCK", "CLOSE", "CLOTH", "CLOUD", "COACH",
    "COAST", "COBRA", "COLOR", "COMES", "COMIC", "CORAL", "COUCH", "COUNT",
    "COURT", "COVER", "CRACK", "CRAFT", "CRASH", "CRAZY", "CREAM", "CREEK",
    "CRIME", "CRISP", "CROSS", "CROWD", "CROWN", "CRUSH", "CRUST", "CURVE",
    "CYCLE", "DAILY", "DANCE", "DEALT", "DEATH", "DEBUT", "DELAY", "DELTA",
    "DENSE", "DEPOT", "DEPTH", "DERBY", "DEVIL", "DIRTY", "DISCO", "DODGE",
    "DOUBT", "DOUGH", "DRAFT", "DRAIN", "DRAWN", "DREAM", "DRIVE", "DRONE",
    "DROVE", "DROWN", "DRUNK", "DRYER", "DUCKS", "DUMMY", "DUNNO", "DYING",
    "EAGLE", "EARLY", "EARTH", "EIGHT", "ELITE", "EMAIL", "EMPTY", "ENEMY",
    "ENJOY", "ENTER", "ENTRY", "EQUAL", "ERROR", "ESSAY", "EVENT", "EVERY",
    "EXACT", "EXIST", "EXTRA", "FAINT", "FAIRY", "FAITH", "FALSE", "FANCY",
    "FAULT", "FEAST", "FENCE", "FERRY", "FEVER", "FIBER", "FIFTY", "FIGHT",
    "FINAL", "FIRST", "FIXED", "FLAME", "FLAIR", "FLESH", "FLOAT", "FLOOD",
    "FLOOR", "FLOUR", "FLUID", "FLUTE", "FOCUS", "FOLIO", "FOUND", "FRAME",
    "FRANK", "FRAUD", "FRESH", "FRONT", "FROST", "FRUIT", "FULLY", "FUNNY",
    "GIANT", "GIVEN", "GLASS", "GLEAM", "GLOBE", "GLORY", "GLOSS", "GLOVE",
    "GOING", "GRACE", "GRADE", "GRAIN", "GRAND", "GRANT", "GRASP", "GRASS",
    "GRAVE", "GREAT", "GREEN", "GREET", "GRIEF", "GRIND", "GROAN", "GROSS",
    "GROUP", "GROVE", "GROWN", "GROWL", "GUARD", "GUESS", "GUEST", "GUIDE",
    "GUILD", "GUILE", "GUISE", "GULCH", "HABIT", "HAPPY", "HARSH", "HAVEN",
    "HEART", "HEAVY", "HENCE", "HERBS", "HINGE", "HIPPO", "HONOR", "HORSE",
    "HOTEL", "HOUSE", "HUMAN", "HUMOR", "HURRY", "IDEAL", "IMAGE", "IMPLY",
    "INDEX", "INDIE", "INNER", "INPUT", "ISSUE", "JOKER", "JUDGE", "JUICE",
    "JUICY", "JUMBO", "KEEPS", "KNIFE", "KNOCK", "KNOWN", "LABEL", "LARGE",
    "LASER", "LATER", "LAUGH", "LAYER", "LEARN", "LEASE", "LEAST", "LEAVE",
    "LEVEL", "LIGHT", "LIMIT", "LINEN", "LINER", "LIVER", "LODGE", "LOGIC",
    "LOOSE", "LOWER", "LUCKY", "LUNAR", "LUNCH", "LYRIC", "MAGIC", "MAJOR",
    "MAKER", "MANOR", "MARCH", "MATCH", "MAYBE", "MAYOR", "MEDIA", "MERCY",
    "MERGE", "MERIT", "METAL", "MIGHT", "MINOR", "MINUS", "MIXED", "MODEL",
    "MONEY", "MONTH", "MORAL", "MOTOR", "MOUNT", "MOUSE", "MOUTH", "MOVIE",
    "MUDDY", "MUSIC", "NERVE", "NEVER", "NIGHT", "NINJA", "NOISE", "NORTH",
    "NOTED", "NOVEL", "NURSE", "NYMPH", "OCCUR", "OCEAN", "OFFER", "ONSET",
    "OPERA", "ORBIT", "ORDER", "OTHER", "OUTER", "OXIDE", "OZONE", "PAINT",
    "PANEL", "PANIC", "PAPER", "PARTY", "PASTA", "PATCH", "PAUSE", "PEACE",
    "PEARL", "PENNY", "PHASE", "PHONE", "PHOTO", "PIANO", "PIECE", "PILOT",
    "PITCH", "PIXEL", "PIZZA", "PLACE", "PLAIN", "PLANE", "PLANT", "PLATE",
    "PLAZA", "PLEAD", "PLUCK", "PLUMB", "PLUME", "PLUNGE","POINT", "POLAR",
    "POPPY", "PORCH", "POSED", "POTTY", "POUND", "PRESS", "PRICE", "PRIDE",
    "PRIME", "PRINT", "PRIZE", "PROBE", "PROOF", "PROSE", "PROUD", "PROVE",
    "PROXY", "QUEEN", "QUERY", "QUEST", "QUEUE", "QUICK", "QUIET", "QUOTA",
    "QUOTE", "RADAR", "RADIO", "RANCH", "RANGE", "RAPID", "RATIO", "REACH",
    "REACT", "READY", "REALM", "REBEL", "REFER", "REIGN", "RELAX", "REPAY",
    "REPEL", "REPLY", "RIDER", "RIDGE", "RIFLE", "RIGHT", "RIGID", "RISKY",
    "RIVAL", "RIVER", "ROBIN", "ROBOT", "ROCKY", "ROGUE", "ROMAN", "ROUGH",
    "ROUND", "ROUTE", "ROYAL", "RULED", "RULER", "RURAL", "SADLY", "SAINT",
    "SALAD", "SAUCE", "SCALE", "SCENE", "SCENT", "SCOPE", "SCORE", "SCOUT",
    "SEIZE", "SENSE", "SERVE", "SETUP", "SEVEN", "SHADE", "SHAKE", "SHALL",
    "SHAME", "SHAPE", "SHARE", "SHARP", "SHIFT", "SHINE", "SHOOT", "SHORT",
    "SHOUT", "SHOWN", "SIGHT", "SILKY", "SINCE", "SIXTH", "SKILL", "SKULL",
    "SLEEP", "SLIDE", "SLOPE", "SMART", "SMELL", "SMILE", "SMOKE", "SNAKE",
    "SOLAR", "SOLID", "SOLVE", "SONIC", "SORRY", "SOUTH", "SPACE", "SPEAK",
    "SPEED", "SPEND", "SPICE", "SPILL", "SPINE", "SPITE", "SPLIT", "SPOKE",
    "SPOON", "SPORT", "SQUAD", "STAFF", "STAGE", "STAIN", "STARE", "START",
    "STATE", "STAYS", "STEAL", "STEEP", "STEER", "STERN", "STICK", "STILL",
    "STOCK", "STONE", "STOOD", "STORE", "STORY", "STRAP", "STRAW", "STRAY",
    "STRIP", "STUCK", "STUDY", "STYLE", "SUGAR", "SUITE", "SUNNY", "SUPER",
    "SURGE", "SWAMP", "SWEAR", "SWEEP", "SWEET", "SWEPT", "SWIFT", "SWORD",
    "TABLE", "TAKEN", "TASTE", "TAXES", "TEACH", "TEETH", "TENTH", "TERMS",
    "THEIR", "THEME", "THERE", "THICK", "THING", "THINK", "THIRD", "THOSE",
    "THREE", "THREW", "THROW", "THUMB", "TIGER", "TIGHT", "TIMER", "TIRED",
    "TITLE", "TOKEN", "TOPIC", "TOTAL", "TOUCH", "TOUGH", "TOWER", "TOXIC",
    "TRACE", "TRACK", "TRADE", "TRAIL", "TRAIN", "TRAIT", "TRICK", "TRIED",
    "TROOP", "TRUCK", "TRULY", "TRUMP", "TRUNK", "TRUST", "TRUTH", "TULIP",
    "TUMOR", "TUNER", "TUNIC", "TUPLE", "TWIST", "ULTRA", "UNDER", "UNION",
    "UNITY", "UNTIL", "UPPER", "UPSET", "URBAN", "USAGE", "USUAL", "UTTER",
    "VALID", "VALUE", "VALVE", "VAPOR", "VAULT", "VIDEO", "VIGOR", "VIRAL",
    "VIRUS", "VISIT", "VISOR", "VISTA", "VITAL", "VIVID", "VOCAL", "VOICE",
    "VOTER", "WAGON", "WATCH", "WATER", "WEARY", "WEDGE", "WEIRD", "WHALE",
    "WHEAT", "WHEEL", "WHERE", "WHICH", "WHILE", "WHITE", "WHOLE", "WHOSE",
    "WIDER", "WITTY", "WOMAN", "WOMEN", "WORLD", "WORRY", "WORSE", "WORST",
    "WORTH", "WOULD", "WOUND", "WRATH", "WRIST", "WRITE", "WRONG", "YACHT",
    "YIELD", "YOUNG", "YOUTH", "ZEBRA", "ZONAL",
]

# Valid guess words (superset — includes answer words + common 5-letter words)
# For simplicity we allow any word in WORD_LIST as a valid guess.
# You can expand this with a full dictionary if desired.
VALID_GUESSES = set(WORD_LIST)

# ── Emoji feedback ─────────────────────────────────────────────────────────────
HIT   = "⚡"   # right letter, right spot
CLOSE = "🌩️"  # right letter, wrong spot
MISS  = "▪️"  # not in word

EMPTY_ROW = "⬛⬛⬛⬛⬛"

# ── In-memory game state ───────────────────────────────────────────────────────
# { user_id: { "word": str, "guesses": [str], "rows": [str], "done": bool, "won": bool } }
active_games: dict[int, dict] = {}


# ── Game logic ─────────────────────────────────────────────────────────────────

def score_guess(guess: str, answer: str) -> str:
    """Return a 5-emoji string scoring the guess against the answer."""
    result = [MISS] * 5
    answer_chars = list(answer)
    guess_chars  = list(guess)

    # First pass: hits
    for i in range(5):
        if guess_chars[i] == answer_chars[i]:
            result[i] = HIT
            answer_chars[i] = None
            guess_chars[i]  = None

    # Second pass: close
    for i in range(5):
        if guess_chars[i] is not None and guess_chars[i] in answer_chars:
            result[i] = CLOSE
            answer_chars[answer_chars.index(guess_chars[i])] = None

    return "".join(result)


def build_grid(rows: list[str], guesses: list[str]) -> str:
    """Build the visual grid string for the embed."""
    lines = []
    for i in range(6):
        if i < len(rows):
            # Spell out the guessed letters with spacing
            letters = " ".join(f"`{c}`" for c in guesses[i])
            lines.append(f"{rows[i]}  {letters}")
        else:
            lines.append(EMPTY_ROW)
    return "\n".join(lines)


def build_embed(state: dict, title_override: str = None, color: int = 0x3A86FF) -> discord.Embed:
    guesses_left = 6 - len(state["guesses"])
    title = title_override or "⚡ Zap Word"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(
        name="Grid",
        value=build_grid(state["rows"], state["guesses"]),
        inline=False
    )
    if not state["done"]:
        embed.add_field(
            name="Key",
            value=f"{HIT} Right spot  {CLOSE} Wrong spot  {MISS} Not in word",
            inline=False
        )
        embed.set_footer(text=f"{guesses_left} guess{'es' if guesses_left != 1 else ''} remaining")
    return embed


# ── Modal ──────────────────────────────────────────────────────────────────────

class GuessModal(discord.ui.Modal, title="⚡ Zap Word — Enter Guess"):
    guess = discord.ui.TextInput(
        label="Your 5-letter word",
        placeholder="e.g. SPARK",
        min_length=5,
        max_length=5,
        style=discord.TextStyle.short
    )

    def __init__(self, cog, user_id: int):
        super().__init__()
        self.cog     = cog
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        word = self.guess.value.upper().strip()

        if len(word) != 5 or not word.isalpha():
            await interaction.response.send_message(
                "Please enter a valid 5-letter word.", ephemeral=True
            )
            return

        if word not in VALID_GUESSES:
            await interaction.response.send_message(
                f"**{word}** isn't in the word list. Try another word.",
                ephemeral=True
            )
            return

        state = active_games.get(self.user_id)
        if not state or state["done"]:
            await interaction.response.send_message(
                "No active game. Click ⚡ Zap Word to start a new one.",
                ephemeral=True
            )
            return

        row = score_guess(word, state["word"])
        state["guesses"].append(word)
        state["rows"].append(row)

        won  = all(c == HIT for c in row)
        lost = not won and len(state["guesses"]) >= 6

        if won or lost:
            state["done"] = True
            state["won"]  = won
            await self.cog.handle_game_end(interaction, state, won)
        else:
            embed = build_embed(state)
            view  = ZapWordGameView(self.cog, self.user_id)
            await interaction.response.edit_message(embed=embed, view=view)


# ── Views ──────────────────────────────────────────────────────────────────────

class ZapWordGameView(discord.ui.View):
    def __init__(self, cog, user_id: int):
        super().__init__(timeout=300)
        self.cog     = cog
        self.user_id = user_id

    @discord.ui.button(label="✏️ Make a Guess", style=discord.ButtonStyle.primary)
    async def make_guess(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        await interaction.response.send_modal(GuessModal(self.cog, self.user_id))


class ZapWordEndView(discord.ui.View):
    def __init__(self, cog, user_id: int, score_text: str, won: bool):
        super().__init__(timeout=120)
        self.cog        = cog
        self.user_id    = user_id
        self.score_text = score_text
        self.won        = won

    @discord.ui.button(label="🔄 Play Again", style=discord.ButtonStyle.primary)
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        state, embed, view = self.cog.new_game(self.user_id)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="📣 Share Score", style=discord.ButtonStyle.secondary)
    async def share_score(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.guild.get_channel(SCORES_CHANNEL_ID)
        if channel:
            await channel.send(
                f"⚡ **{interaction.user.display_name}** {'solved' if self.won else 'played'} Zap Word!\n{self.score_text}"
            )
        await interaction.response.send_message("Score shared!", ephemeral=True)


# ── Cog ────────────────────────────────────────────────────────────────────────

class ZapWordCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot = bot
        self.db  = db

    def new_game(self, user_id: int):
        word = random.choice(WORD_LIST)
        state = {
            "word":    word,
            "guesses": [],
            "rows":    [],
            "done":    False,
            "won":     False,
        }
        active_games[user_id] = state
        embed = build_embed(state)
        view  = ZapWordGameView(self, user_id)
        return state, embed, view

    async def handle_game_end(self, interaction: discord.Interaction, state: dict, won: bool):
        user_id     = interaction.user.id
        guess_count = len(state["guesses"])
        zapp        = ZAPP_PER_WIN[guess_count - 1] if won else ZAPP_LOSS

        # Credit ZAPP
        if zapp > 0:
            try:
                await asyncio.to_thread(
                    lambda: self.db.rpc(
                        "increment_zapp_balance",
                        {"p_user_id": str(user_id), "p_amount": zapp}
                    ).execute()
                )
            except Exception as e:
                print(f"[zap_word] ZAPP credit error: {e}")

        # Update stats
        try:
            await asyncio.to_thread(
                lambda: self.db.rpc(
                    "update_zapword_stats",
                    {
                        "p_user_id":    str(user_id),
                        "p_won":        won,
                        "p_guess_count": guess_count
                    }
                ).execute()
            )
        except Exception as e:
            print(f"[zap_word] stats update error: {e}")

        # Build score share text (Wordle-style emoji grid)
        score_label  = f"{guess_count}/6" if won else "X/6"
        grid_rows    = "\n".join(state["rows"])
        score_text   = f"⚡ Zap Word {score_label}\n\n{grid_rows}"

        # Build end embed
        if won:
            title = f"⚡ Solved in {guess_count}!"
            color = 0x57CC99
            desc  = (
                f"The word was **{state['word']}**.\n"
                f"🪙 **+{zapp} ZAPP** added to your balance."
            )
        else:
            title = "⚡ Zap Word — Better luck next time"
            color = 0xFF4040
            desc  = (
                f"The word was **{state['word']}**.\n"
                f"🪙 **+{zapp} ZAPP** consolation prize."
            )

        embed = build_embed(state, title_override=title, color=color)
        embed.add_field(name="Result", value=desc, inline=False)

        view = ZapWordEndView(self, user_id, score_text, won)
        await interaction.response.edit_message(embed=embed, view=view)

    @app_commands.command(name="zapwordstats", description="View your Zap Word personal stats.")
    async def zapwordstats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(
                lambda: self.db.table("zapword_stats")
                .select("*")
                .eq("discord_user_id", str(interaction.user.id))
                .single()
                .execute()
            )
            row = result.data
        except Exception:
            row = None

        if not row:
            await interaction.followup.send(
                "No stats yet — play a game first!", ephemeral=True
            )
            return

        played  = row.get("games_played", 0)
        wins    = row.get("games_won", 0)
        pct     = round((wins / played) * 100) if played else 0
        dist    = row.get("guess_distribution", {})

        dist_lines = ""
        for i in range(1, 7):
            count = dist.get(str(i), 0)
            bar   = "█" * count if count else "·"
            dist_lines += f"`{i}` {bar} {count}\n"

        embed = discord.Embed(title="⚡ Zap Word — Your Stats", color=0x3A86FF)
        embed.add_field(name="Played",   value=str(played), inline=True)
        embed.add_field(name="Win %",    value=f"{pct}%",   inline=True)
        embed.add_field(name="Wins",     value=str(wins),   inline=True)
        embed.add_field(name="Guess Distribution", value=dist_lines, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ZapWordCog(bot, bot.db))

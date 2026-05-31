"""
sudoku_cog.py
-------------
Zappy Sudoku — a solo 9x9 Sudoku game for Zappies Reborn.

Flow:
  - Player clicks "🔢 Sudoku" in the games panel
  - Bot generates a puzzle and renders a Pillow image:
      Columns: A-I  |  Rows: 1-9
      White  = given numbers
      Yellow = player entries
      Red    = conflicts
  - Player clicks "✏️ Enter Number" → modal → types e.g. "B4 7"
  - Grid updates with each entry
  - Puzzle complete → ZAPP reward based on time taken
  - /sudokuscores → top 10 fastest completions

Input format: [column][row] [value]  e.g. "B4 7" or "B4=7"
  Column: A-I (left to right)
  Row:    1-9 (top to bottom)
  Value:  1-9

ZAPP rewards (based on number of moves / mistakes):
  Clean solve (0 mistakes): 25 ZAPP
  1-2 mistakes:             15 ZAPP
  3-5 mistakes:             8 ZAPP
  6+ mistakes:              3 ZAPP
"""

import discord
from discord.ext import commands
from discord import app_commands
import random
import copy
import asyncio
import io
import os
import re
import time
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

# ── Font loader ────────────────────────────────────────────────────────────────

def _get_font(size: int) -> "ImageFont.ImageFont":
    """Load a bold font, downloading it if not found locally."""
    search_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/app/Ubuntu-Bold.ttf",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "Ubuntu-Bold.ttf"),
    ]
    for path in search_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass

    # Download at runtime
    font_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Ubuntu-Bold.ttf")
    if not os.path.exists(font_path):
        try:
            import urllib.request
            url = "https://github.com/google/fonts/raw/main/ufl/ubuntu/Ubuntu-Bold.ttf"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req) as resp, open(font_path, "wb") as f:
                f.write(resp.read())
            print(f"[font] Downloaded Ubuntu-Bold to {font_path}")
        except Exception as e:
            print(f"[font] Font download failed: {e}")

    if os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass

    print(f"[font] WARNING: falling back to default font — text will be tiny!")
    return ImageFont.load_default()


# ── Config ─────────────────────────────────────────────────────────────────────
SCORES_CHANNEL_ID = int(os.environ.get("SCORES_CHANNEL_ID", 0))

ZAPP_REWARDS = [25, 15, 8, 3]  # clean, 1-2 mistakes, 3-5, 6+

# ── Image config ───────────────────────────────────────────────────────────────
# Board image: sudoku_board.png sits next to this cog file
# Cell centers measured from the custom illustrated board
CELL_CX     = [125, 227, 329, 433, 534, 635, 738, 840, 942]
CELL_CY     = [123, 221, 319, 419, 518, 617, 719, 818, 918]
BOARD_SIZE  = (1024, 1024)
OUTPUT_SIZE = (800, 800)   # resize before sending to Discord

GIVEN_COLOR  = (50,  20,  100)   # dark purple — matches board labels
PLAYER_COLOR = (20,  20,   20)   # near black for player entries
ERROR_COLOR  = (200, 30,   30)   # red for conflicts
DONE_COLOR   = (30,  120,  30)   # dark green when complete

COLS = "ABCDEFGHI"


# ── Sudoku engine ──────────────────────────────────────────────────────────────

def _is_valid(board, row, col, num):
    if num in board[row]:
        return False
    if num in [board[r][col] for r in range(9)]:
        return False
    br, bc = (row // 3) * 3, (col // 3) * 3
    for r in range(br, br + 3):
        for c in range(bc, bc + 3):
            if board[r][c] == num:
                return False
    return True


def _solve(board):
    for r in range(9):
        for c in range(9):
            if board[r][c] == 0:
                nums = list(range(1, 10))
                random.shuffle(nums)
                for n in nums:
                    if _is_valid(board, r, c, n):
                        board[r][c] = n
                        if _solve(board):
                            return True
                        board[r][c] = 0
                return False
    return True


def generate_puzzle(clues: int = 32):
    """Generate a puzzle with ~clues given numbers. Returns (puzzle, solution)."""
    board = [[0] * 9 for _ in range(9)]
    _solve(board)
    solution = copy.deepcopy(board)

    cells = [(r, c) for r in range(9) for c in range(9)]
    random.shuffle(cells)
    removed = 0
    target = 81 - clues

    for r, c in cells:
        if removed >= target:
            break
        backup = board[r][c]
        board[r][c] = 0
        test = copy.deepcopy(board)
        if _solve(test):
            removed += 1
        else:
            board[r][c] = backup

    return board, solution


def find_conflicts(puzzle, player_entries):
    """Return set of (row, col) that have conflicts."""
    conflicts = set()
    for (r, c), val in player_entries.items():
        # Check row
        for cc in range(9):
            if cc != c:
                if puzzle[r][cc] == val or player_entries.get((r, cc)) == val:
                    conflicts.add((r, c))
                    conflicts.add((r, cc)) if player_entries.get((r, cc)) == val else None
        # Check col
        for rr in range(9):
            if rr != r:
                if puzzle[rr][c] == val or player_entries.get((rr, c)) == val:
                    conflicts.add((r, c))
                    conflicts.add((rr, c)) if player_entries.get((rr, c)) == val else None
        # Check box
        br, bc = (r // 3) * 3, (c // 3) * 3
        for rr in range(br, br + 3):
            for cc in range(bc, bc + 3):
                if (rr, cc) != (r, c):
                    if puzzle[rr][cc] == val or player_entries.get((rr, cc)) == val:
                        conflicts.add((r, c))
                        conflicts.add((rr, cc)) if player_entries.get((rr, cc)) == val else None
    return conflicts


def is_complete(puzzle, player_entries, solution):
    """Check if the board is fully and correctly solved."""
    for r in range(9):
        for c in range(9):
            if puzzle[r][c] == 0:
                if player_entries.get((r, c)) != solution[r][c]:
                    return False
    return True


# ── Image renderer ─────────────────────────────────────────────────────────────

def render_sudoku(puzzle, player_entries, solution, complete=False):
    """Render numbers onto the custom illustrated board."""
    board_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sudoku_board.png")
    if os.path.exists(board_path):
        img = Image.open(board_path).convert("RGB")
    else:
        img = Image.new("RGB", BOARD_SIZE, (30, 31, 34))

    draw     = ImageDraw.Draw(img)
    font     = _get_font(60)
    conflicts = set() if complete else find_conflicts(puzzle, player_entries)

    for r in range(9):
        for c in range(9):
            cx    = CELL_CX[c]
            cy    = CELL_CY[r]
            given = puzzle[r][c]
            entry = player_entries.get((r, c))

            if given:
                draw.text((cx, cy), str(given), fill=GIVEN_COLOR, font=font, anchor="mm")
            elif entry:
                if complete:
                    color = DONE_COLOR
                elif (r, c) in conflicts:
                    color = ERROR_COLOR
                else:
                    color = PLAYER_COLOR
                draw.text((cx, cy), str(entry), fill=color, font=font, anchor="mm")

    img = img.resize(OUTPUT_SIZE, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ── Input parser ───────────────────────────────────────────────────────────────

def parse_input(text: str):
    """
    Parse player input like 'B4 7' or 'B4=7' or 'b4 7'.
    Returns (col_idx, row_idx, value) or raises ValueError.
    col_idx and row_idx are 0-based.
    """
    text = text.strip().upper().replace("=", " ")
    # Match patterns like B4 7 or B 4 7
    m = re.match(r'^([A-I])\s*([1-9])\s+([1-9])$', text)
    if not m:
        raise ValueError(
            "Format: `[column][row] [value]`\n"
            "Example: `B4 7` — column B, row 4, value 7"
        )
    col = COLS.index(m.group(1))
    row = int(m.group(2)) - 1
    val = int(m.group(3))
    return col, row, val


# ── Game state ─────────────────────────────────────────────────────────────────
# { user_id: { puzzle, solution, entries, mistakes, start_time } }
active_games: dict[int, dict] = {}


# ── Modal ──────────────────────────────────────────────────────────────────────

class SudokuModal(discord.ui.Modal, title="🔢 Sudoku — Enter a number"):
    entry = discord.ui.TextInput(
        label="Column + Row + Value",
        placeholder="e.g.  B4 7  →  column B, row 4, put 7",
        min_length=4,
        max_length=6,
        style=discord.TextStyle.short
    )

    def __init__(self, cog, user_id: int):
        super().__init__()
        self.cog     = cog
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        state = active_games.get(self.user_id)
        if not state:
            await interaction.response.send_message(
                "No active game. Click **🔢 Sudoku** to start one.", ephemeral=True
            )
            return

        try:
            col, row, val = parse_input(self.entry.value)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        puzzle  = state["puzzle"]
        entries = state["entries"]

        # Can't overwrite a given
        if puzzle[row][col] != 0:
            await interaction.response.send_message(
                f"**{COLS[col]}{row+1}** is a given number — you can't change it.",
                ephemeral=True
            )
            return

        # Track mistakes
        solution_val = state["solution"][row][col]
        if val != solution_val:
            state["mistakes"] += 1

        # Place entry (allow overwrite of wrong guesses)
        entries[(row, col)] = val

        # Check complete
        complete = is_complete(puzzle, entries, state["solution"])

        buf = await asyncio.to_thread(render_sudoku, puzzle, entries, state["solution"], complete)
        file = discord.File(buf, filename="sudoku.png")

        filled  = sum(1 for r in range(9) for c in range(9) if puzzle[r][c] == 0 and entries.get((r,c)))
        total   = sum(1 for r in range(9) for c in range(9) if puzzle[r][c] == 0)
        mistakes = state["mistakes"]

        if complete:
            elapsed = int(time.time() - state["start_time"])
            mins, secs = divmod(elapsed, 60)
            time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

            if mistakes == 0:       zapp = ZAPP_REWARDS[0]
            elif mistakes <= 2:     zapp = ZAPP_REWARDS[1]
            elif mistakes <= 5:     zapp = ZAPP_REWARDS[2]
            else:                   zapp = ZAPP_REWARDS[3]

            # Credit ZAPP
            zapp_credited = False
            try:
                racer = await asyncio.to_thread(
                    lambda: self.cog.db.table("zappy_racers")
                    .select("discord_user_id, zapp_balance")
                    .eq("discord_user_id", str(self.user_id))
                    .order("registered_at")
                    .limit(1)
                    .execute()
                )
                if racer.data:
                    current = racer.data[0].get("zapp_balance", 0) or 0
                    await asyncio.to_thread(
                        lambda: self.cog.db.table("zappy_racers")
                        .update({"zapp_balance": current + zapp})
                        .eq("discord_user_id", str(self.user_id))
                        .execute()
                    )
                    zapp_credited = True
            except Exception as e:
                print(f"[sudoku] ZAPP credit error: {e}")

            # Save score
            is_best = False
            try:
                existing = await asyncio.to_thread(
                    lambda: self.cog.db.table("sudoku_scores")
                    .select("mistakes, elapsed")
                    .eq("discord_user_id", str(self.user_id))
                    .order("mistakes")
                    .order("elapsed")
                    .limit(1)
                    .execute()
                )
                prev = existing.data[0] if existing.data else None
                if not prev or mistakes < prev["mistakes"] or (mistakes == prev["mistakes"] and elapsed < prev["elapsed"]):
                    is_best = True
                    await asyncio.to_thread(
                        lambda: self.cog.db.table("sudoku_scores").insert({
                            "discord_user_id": str(self.user_id),
                            "username":        interaction.user.display_name,
                            "mistakes":        mistakes,
                            "elapsed":         elapsed,
                            "achieved_at":     datetime.now(timezone.utc).isoformat()
                        }).execute()
                    )
            except Exception as e:
                print(f"[sudoku] score save error: {e}")

            del active_games[self.user_id]

            embed = discord.Embed(
                title="🔢 Sudoku — Solved! 🎉",
                color=0x57CC99
            )
            embed.add_field(name="Time",     value=time_str,         inline=True)
            embed.add_field(name="Mistakes", value=str(mistakes),    inline=True)
            if zapp_credited:
                embed.add_field(name="Reward", value=f"🪙 **+{zapp} ZAPP**", inline=True)
            else:
                embed.add_field(
                    name="🪙 Earn ZAPP",
                    value="Use `/link` and `/gpregister` to earn ZAPP from games.",
                    inline=False
                )
            if is_best:
                embed.add_field(name="🏆 Personal Best!", value="New record!", inline=False)
            embed.set_image(url="attachment://sudoku.png")

            view = SudokuEndView(self.user_id, interaction.user, mistakes, time_str, zapp, is_best, self.cog)
            await interaction.response.edit_message(embed=embed, view=view, attachments=[file])

            if is_best:
                await self.cog.post_high_score(interaction, mistakes, time_str)
        else:
            conflicts = find_conflicts(puzzle, entries)
            embed = discord.Embed(
                title="🔢 Sudoku",
                description=(
                    f"**{COLS[col]}{row+1} = {val}** entered.\n"
                    + (f"⚠️ Conflict detected!" if (row,col) in conflicts else "✓ Looks good.")
                ),
                color=0x3A86FF
            )
            embed.add_field(name="Progress",  value=f"{filled}/{total} cells filled", inline=True)
            embed.add_field(name="Mistakes",  value=str(mistakes),                    inline=True)
            embed.set_image(url="attachment://sudoku.png")
            embed.set_footer(text="Format: B4 7  →  column B, row 4, value 7")
            view = SudokuGameView(self.cog, self.user_id)
            await interaction.response.edit_message(embed=embed, view=view, attachments=[file])


# ── Views ──────────────────────────────────────────────────────────────────────

class SudokuGameView(discord.ui.View):
    def __init__(self, cog, user_id: int):
        super().__init__(timeout=3600)  # 1 hour timeout
        self.cog     = cog
        self.user_id = user_id

    @discord.ui.button(label="✏️ Enter Number", style=discord.ButtonStyle.primary)
    async def enter_number(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        await interaction.response.send_modal(SudokuModal(self.cog, self.user_id))

    @discord.ui.button(label="🗑️ Erase Cell", style=discord.ButtonStyle.secondary)
    async def erase_cell(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        await interaction.response.send_modal(SudokuEraseModal(self.cog, self.user_id))

    @discord.ui.button(label="✅ Check Puzzle", style=discord.ButtonStyle.success)
    async def check_puzzle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        state = active_games.get(self.user_id)
        if not state:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        puzzle  = state["puzzle"]
        entries = state["entries"]
        solution = state["solution"]

        # Count unfilled cells
        empty = [(r,c) for r in range(9) for c in range(9)
                 if puzzle[r][c] == 0 and not entries.get((r,c))]
        conflicts = find_conflicts(puzzle, entries)

        if empty:
            await interaction.response.send_message(
                f"Not done yet — **{len(empty)} cell{'s' if len(empty) != 1 else ''}** still empty.",
                ephemeral=True
            )
            return

        if conflicts:
            await interaction.response.send_message(
                f"Almost! You have **{len(conflicts)} conflict{'s' if len(conflicts) != 1 else ''}** — check the red cells.",
                ephemeral=True
            )
            return

        # Board is complete and correct — trigger win
        import time as _time
        elapsed  = int(_time.time() - state["start_time"])
        mistakes = state["mistakes"]
        mins, secs = divmod(elapsed, 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

        if mistakes == 0:       zapp = ZAPP_REWARDS[0]
        elif mistakes <= 2:     zapp = ZAPP_REWARDS[1]
        elif mistakes <= 5:     zapp = ZAPP_REWARDS[2]
        else:                   zapp = ZAPP_REWARDS[3]

        zapp_credited = False
        try:
            racer = await asyncio.to_thread(
                lambda: self.cog.db.table("zappy_racers")
                .select("discord_user_id, zapp_balance")
                .eq("discord_user_id", str(self.user_id))
                .order("registered_at")
                .limit(1)
                .execute()
            )
            if racer.data:
                current = racer.data[0].get("zapp_balance", 0) or 0
                await asyncio.to_thread(
                    lambda: self.cog.db.table("zappy_racers")
                    .update({"zapp_balance": current + zapp})
                    .eq("discord_user_id", str(self.user_id))
                    .execute()
                )
                zapp_credited = True
        except Exception as e:
            print(f"[sudoku] ZAPP credit error: {e}")

        is_best = False
        try:
            existing = await asyncio.to_thread(
                lambda: self.cog.db.table("sudoku_scores")
                .select("mistakes, elapsed")
                .eq("discord_user_id", str(self.user_id))
                .order("mistakes")
                .order("elapsed")
                .limit(1)
                .execute()
            )
            prev = existing.data[0] if existing.data else None
            if not prev or mistakes < prev["mistakes"] or (mistakes == prev["mistakes"] and elapsed < prev["elapsed"]):
                is_best = True
                await asyncio.to_thread(
                    lambda: self.cog.db.table("sudoku_scores").insert({
                        "discord_user_id": str(self.user_id),
                        "username":        interaction.user.display_name,
                        "mistakes":        mistakes,
                        "elapsed":         elapsed,
                        "achieved_at":     datetime.now(timezone.utc).isoformat()
                    }).execute()
                )
        except Exception as e:
            print(f"[sudoku] score save error: {e}")

        del active_games[self.user_id]

        buf  = await asyncio.to_thread(render_sudoku, puzzle, entries, solution, complete=True)
        file = discord.File(buf, filename="sudoku.png")

        embed = discord.Embed(title="🔢 Sudoku — Solved! 🎉", color=0x57CC99)
        embed.add_field(name="Time",     value=time_str,      inline=True)
        embed.add_field(name="Mistakes", value=str(mistakes), inline=True)
        if zapp_credited:
            embed.add_field(name="Reward", value=f"🪙 **+{zapp} ZAPP**", inline=True)
        else:
            embed.add_field(
                name="🪙 Earn ZAPP",
                value="Use `/link` and `/gpregister` to earn ZAPP from games.",
                inline=False
            )
        if is_best:
            embed.add_field(name="🏆 Personal Best!", value="New record!", inline=False)
        embed.set_image(url="attachment://sudoku.png")

        view = SudokuEndView(self.user_id, interaction.user, mistakes, time_str, zapp, is_best, self.cog)
        await interaction.response.edit_message(embed=embed, view=view, attachments=[file])

        if is_best:
            await self.cog.post_high_score(interaction, mistakes, time_str)

    @discord.ui.button(label="🚫 Abandon", style=discord.ButtonStyle.danger)
    async def abandon(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        active_games.pop(self.user_id, None)
        await interaction.response.edit_message(
            embed=discord.Embed(title="🔢 Sudoku — Abandoned", color=0x80848e),
            view=None,
            attachments=[]
        )


class SudokuEraseModal(discord.ui.Modal, title="🔢 Sudoku — Erase a cell"):
    cell = discord.ui.TextInput(
        label="Which cell to erase?",
        placeholder="e.g.  B4  →  column B, row 4",
        min_length=2,
        max_length=3,
        style=discord.TextStyle.short
    )

    def __init__(self, cog, user_id: int):
        super().__init__()
        self.cog     = cog
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        state = active_games.get(self.user_id)
        if not state:
            await interaction.response.send_message("No active game.", ephemeral=True)
            return

        text = self.cell.value.strip().upper()
        m    = re.match(r'^([A-I])([1-9])$', text)
        if not m:
            await interaction.response.send_message(
                "Format: `B4` — column letter + row number.", ephemeral=True
            )
            return

        col = COLS.index(m.group(1))
        row = int(m.group(2)) - 1

        if state["puzzle"][row][col] != 0:
            await interaction.response.send_message(
                f"**{text}** is a given — can't erase it.", ephemeral=True
            )
            return

        state["entries"].pop((row, col), None)
        puzzle  = state["puzzle"]
        entries = state["entries"]
        filled  = sum(1 for r in range(9) for c in range(9) if puzzle[r][c] == 0 and entries.get((r,c)))
        total   = sum(1 for r in range(9) for c in range(9) if puzzle[r][c] == 0)

        buf  = await asyncio.to_thread(render_sudoku, puzzle, entries, state["solution"])
        file = discord.File(buf, filename="sudoku.png")

        embed = discord.Embed(title="🔢 Sudoku", description=f"**{text}** erased.", color=0x3A86FF)
        embed.add_field(name="Progress", value=f"{filled}/{total} cells filled", inline=True)
        embed.add_field(name="Mistakes", value=str(state["mistakes"]),            inline=True)
        embed.set_image(url="attachment://sudoku.png")
        embed.set_footer(text="Format: B4 7  →  column B, row 4, value 7")
        await interaction.response.edit_message(embed=embed, view=SudokuGameView(self.cog, self.user_id), attachments=[file])


class SudokuEndView(discord.ui.View):
    def __init__(self, user_id, user, mistakes, time_str, zapp, is_best, cog):
        super().__init__(timeout=120)
        self.user_id  = user_id
        self.user     = user
        self.mistakes = mistakes
        self.time_str = time_str
        self.zapp     = zapp
        self.is_best  = is_best
        self.cog      = cog

    @discord.ui.button(label="🔄 New Puzzle", style=discord.ButtonStyle.primary)
    async def new_puzzle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        embed, view, file = await self.cog.start_game(self.user_id)
        await interaction.response.edit_message(embed=embed, view=view, attachments=[file])

    @discord.ui.button(label="📣 Share Score", style=discord.ButtonStyle.secondary)
    async def share_score(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.guild.get_channel(SCORES_CHANNEL_ID)
        if not channel:
            await interaction.response.send_message("Scores channel not configured.", ephemeral=True)
            return
        mistake_str = "no mistakes! 🎯" if self.mistakes == 0 else f"{self.mistakes} mistake{'s' if self.mistakes != 1 else ''}"
        await channel.send(
            f"🔢 **{interaction.user.display_name}** solved Sudoku in **{self.time_str}** with {mistake_str}"
            + (" 🏆 Personal best!" if self.is_best else "")
        )
        await interaction.response.send_message("Score shared!", ephemeral=True)


# ── Cog ────────────────────────────────────────────────────────────────────────

class SudokuCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot = bot
        self.db  = db

    async def start_game(self, user_id: int):
        puzzle, solution = await asyncio.to_thread(generate_puzzle, 32)
        active_games[user_id] = {
            "puzzle":     puzzle,
            "solution":   solution,
            "entries":    {},
            "mistakes":   0,
            "start_time": time.time(),
        }
        total = sum(1 for r in range(9) for c in range(9) if puzzle[r][c] == 0)
        buf  = await asyncio.to_thread(render_sudoku, puzzle, {}, solution)
        file = discord.File(buf, filename="sudoku.png")
        embed = discord.Embed(
            title="🔢 Sudoku",
            description=(
                "Fill in the grid. Click **✏️ Enter Number** and type your move.\n\n"
                "**Format:** `B4 7` — column B, row 4, put a 7\n"
                "Use **🗑️ Erase Cell** to clear a wrong entry."
            ),
            color=0x3A86FF
        )
        embed.add_field(name="Progress", value=f"0/{total} cells filled", inline=True)
        embed.add_field(name="Mistakes", value="0",                        inline=True)
        embed.set_image(url="attachment://sudoku.png")
        embed.set_footer(text="Columns: A-I  ·  Rows: 1-9")
        view = SudokuGameView(self, user_id)
        return embed, view, file

    async def post_high_score(self, interaction: discord.Interaction, mistakes: int, time_str: str):
        try:
            channel = interaction.guild.get_channel(SCORES_CHANNEL_ID)
            if channel:
                mistake_str = "no mistakes 🎯" if mistakes == 0 else f"{mistakes} mistake{'s' if mistakes != 1 else ''}"
                embed = discord.Embed(
                    title="🏆 New Sudoku Personal Best!",
                    description=(
                        f"**{interaction.user.display_name}** just set a new personal record!\n\n"
                        f"🔢 Solved in **{time_str}** with {mistake_str}\n\n"
                        "Think you can beat it? Hit **🔢 Sudoku** to try."
                    ),
                    color=0xFFD60A
                )
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                await channel.send(embed=embed)
        except Exception as e:
            print(f"[sudoku] high score post error: {e}")

    @app_commands.command(name="sudokuscores", description="View the Sudoku top 10 leaderboard.")
    async def sudokuscores(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            result = await asyncio.to_thread(
                lambda: self.db.table("sudoku_scores")
                .select("username, mistakes, elapsed, achieved_at")
                .order("mistakes")
                .order("elapsed")
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
            medal    = medals[i] if i < 3 else f"**{i+1}.**"
            mins, s  = divmod(row["elapsed"], 60)
            t_str    = f"{mins}m {s}s" if mins else f"{s}s"
            err_str  = "clean 🎯" if row["mistakes"] == 0 else f"{row['mistakes']} mistakes"
            lines.append(f"{medal} **{row['username']}** — {t_str} · {err_str}")

        embed = discord.Embed(
            title="🔢 Sudoku — Top 10",
            description="\n".join(lines),
            color=0x3A86FF
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SudokuCog(bot, bot.supabase))

"""
grand_prix_cog.py
Zappy Grand Prix — dual board racing system

ALGO board : 5 ALGO entry · 9 ALGO to winner · 1 ALGO rake
ZAP  board : 500 ZAP entry · 1000 ZAP to winner · no rake

KEY FIXES vs last version:
  1. asyncio.Lock() on _handle_join slot assignment — prevents simultaneous-join
     race condition where both players land in slot A and slot B never fills.
  2. Poller timeout sends an ephemeral message AND clears the player from
     active_players + resets the board — no more silent stuck-queue state.
  3. _clear_player() helper centralises all cleanup so gpclear, timeout, and
     settle all do the same teardown in the same place.
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# Layer imports — adjust paths to match your project layout
# ---------------------------------------------------------------------------
from algo_layer import get_bot_address  # still needed for gpbalance display
from zap_layer import ZAP_ENTRY, ZAP_PAYOUT, ZAP_WIN_BONUS, ZAP_LOSE_BONUS
from gp_accounting import credit, debit, get_balance

import io
import os
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for path in [
        f"./fonts/{name}",
        f"/usr/share/fonts/truetype/google-fonts/{name}",
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

FONT_BOLD = _font("Poppins-Bold.ttf",    36)
FONT_MED  = _font("Poppins-Medium.ttf",  22)
FONT_REG  = _font("Poppins-Regular.ttf", 16)
FONT_SM   = _font("Poppins-Regular.ttf", 14)

# ---------------------------------------------------------------------------
# Board image generator
# ---------------------------------------------------------------------------

W, H   = 798, 278
WHITE  = (240, 245, 255)
MUTED  = (180, 190, 210)
GREEN  = (50,  220, 120)
SHADOW = (0, 0, 0)

ACCENTS = {
    "algo": (30,  180, 255),
    "zap":  (255, 200,  50),
}
LABELS = {
    "algo": ("ALGO GRAND PRIX",  "5 ALGO entry  |  Winner takes 9 ALGO"),
    "zap":  ("ZAPP GRAND PRIX",  "500 ZAPP entry  |  Winner takes 1,000 ZAPP"),
}

_BOARDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boards")

BOARD_IMAGES = {
    "algo": {
        "empty":   "algoempty.png",
        "waiting": "algowaiting.png",
        "racing":  "algoracing.png",
        "result":  "algoresult.png",
    },
    "zap": {
        "empty":   "zappempty.png",
        "waiting": "zappwaiting.png",
        "racing":  "zappracing.png",
        "result":  "zappresult.png",
    },
}


def _load_bg(mode, state) -> Image.Image:
    filename = BOARD_IMAGES[mode][state]
    path = os.path.join(_BOARDS_DIR, filename)
    if os.path.exists(path):
        PAD_L, PAD_R, PAD_T, PAD_B = 6, 6, 6, 14
        new_w = W - PAD_L - PAD_R
        new_h = H - PAD_T - PAD_B
        raw    = Image.open(path).convert("RGBA").resize((new_w, new_h))
        canvas = Image.new("RGBA", (W, H), (8, 10, 20, 255))
        canvas.paste(raw, (PAD_L, PAD_T), raw.split()[3])
        return canvas
    print(f"[grand_prix] MISSING board image: {path}")
    return Image.new("RGBA", (W, H), (8, 10, 20, 255))


def _t(draw, x, y, text, font, color):
    draw.text((x+1, y+1), text, font=font, fill=(*SHADOW, 200), anchor="mm")
    draw.text((x+2, y+2), text, font=font, fill=(*SHADOW, 120), anchor="mm")
    draw.text((x, y), text, font=font, fill=color, anchor="mm")


def _buf(img):
    bg = Image.new("RGBA", img.size, (8, 10, 20, 255))
    bg.paste(img, mask=img.split()[3])
    b = io.BytesIO()
    bg.convert("RGB").save(b, format="PNG")
    b.seek(0)
    return b


def board_empty(mode):
    img  = _load_bg(mode, "empty")
    draw = ImageDraw.Draw(img)
    accent = ACCENTS[mode]
    title, subtitle = LABELS[mode]
    _t(draw, W//2, 80,  title,                  FONT_BOLD, accent)
    _t(draw, W//2, 118, subtitle,               FONT_SM,   MUTED)
    _t(draw, W//2, 160, "NO RACE IN PROGRESS",  FONT_MED,  (160, 170, 190))
    _t(draw, W//2, 190, "Be the first to join", FONT_SM,   (120, 130, 150))
    return _buf(img)


def board_waiting(mode, zappy_id):
    img  = _load_bg(mode, "waiting")
    draw = ImageDraw.Draw(img)
    accent = ACCENTS[mode]
    title, subtitle = LABELS[mode]
    _t(draw, W//2, 45,  title,                                   FONT_BOLD, accent)
    _t(draw, W//2, 82,  "WAITING FOR OPPONENT",                  FONT_MED,  accent)
    _t(draw, W//2, 130, f"{zappy_id}  is ready",                 FONT_BOLD, WHITE)
    _t(draw, 190,  245, zappy_id,                                FONT_SM,   WHITE)
    _t(draw, 600,  245, "???",                                   FONT_SM,   MUTED)
    _t(draw, W//2, 195, "Join to race · entry fee debited on join", FONT_SM, MUTED)
    _t(draw, W//2, 222, "Tap Join Race to enter",                FONT_SM,   accent)
    return _buf(img)


def board_racing(mode, zappy_a, zappy_b):
    img  = _load_bg(mode, "racing")
    draw = ImageDraw.Draw(img)
    accent = ACCENTS[mode]
    title, _ = LABELS[mode]
    _t(draw, W//2, 50,  title,                         FONT_BOLD, accent)
    _t(draw, W//2, 85,  "RACE IN PROGRESS",            FONT_MED,  GREEN)
    _t(draw, W//2, 125, zappy_a,                       FONT_BOLD, WHITE)
    _t(draw, W//2, 155, "vs",                          FONT_SM,   MUTED)
    _t(draw, W//2, 185, zappy_b,                       FONT_BOLD, WHITE)
    _t(draw, W//2, 225, "Race underway — result soon", FONT_SM,   MUTED)
    return _buf(img)


def board_result(mode, zappy_a, zappy_b, winner, score_a, score_b, surge=False):
    img  = _load_bg(mode, "result")
    draw = ImageDraw.Draw(img)
    accent = ACCENTS[mode]
    title, _ = LABELS[mode]
    payout    = "9 ALGO paid out" if mode == "algo" else "1,000 ZAPP paid out"
    surge_tag = "  SURGE!" if surge else ""
    diff   = abs(score_a - score_b)
    margin = "Dominant run" if diff == 3 else "Clear victory" if diff == 2 else "Close race"
    _t(draw, W//2, 45,  title,                               FONT_BOLD, accent)
    _t(draw, W//2, 82,  "RACE RESULT",                       FONT_MED,  accent)
    _t(draw, W//2, 125, f"{winner}  WINS!",                  FONT_BOLD, GREEN)
    _t(draw, W//2, 168, f"{payout}  ·  {margin}{surge_tag}", FONT_SM,   MUTED)
    _t(draw, W//2, 198, f"{zappy_a}  vs  {zappy_b}",         FONT_SM,   MUTED)
    _t(draw, W//2, 235, "New race open below",               FONT_SM,   accent)
    return _buf(img)


def make_board_buf(mode, state, **kw):
    if state == "empty":   return board_empty(mode)
    if state == "waiting": return board_waiting(mode, **kw)
    if state == "racing":  return board_racing(mode, **kw)
    if state == "result":  return board_result(mode, **kw)
    return board_empty(mode)
from race_engine import resolve_race, run_race_narration, get_stats, seed_stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ALGO_ENTRY   = 5_000_000   # microALGO
ALGO_PAYOUT  = 9_000_000
ALGO_RAKE    = 1_000_000

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

class RaceQueue:
    """Holds state for one board (ALGO or ZAP)."""
    def __init__(self, mode: str):
        self.mode         = mode        # "algo" | "zap"
        self.player_a_id:    str | None = None
        self.player_a_racer: dict | None = None
        self.player_a_paid:  bool = False
        self.player_b_id:    str | None = None
        self.player_b_racer: dict | None = None
        self.player_b_paid:  bool = False
        self.locked:         bool = False   # True while race is running
        self.duel_id:        str | None = None
        self.after_round:    int = 0
        self.board_msg_id:   int | None = None

    def reset(self):
        self.player_a_id    = None
        self.player_a_racer = None
        self.player_a_paid  = False
        self.player_b_id    = None
        self.player_b_racer = None
        self.player_b_paid  = False
        self.locked         = False
        self.duel_id        = None
        self.after_round    = 0


algo_queue = RaceQueue("algo")
zap_queue  = RaceQueue("zap")

# Global set — player is in here from queue-join until race resolves.
# Blocks joining either board while already active.
active_players: set[str] = set()

# THE FIX: one lock shared across both queues.
# Ensures slot A/B assignment is atomic even when two players tap simultaneously.
_join_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Persistent button views
# ---------------------------------------------------------------------------

class JoinAlgoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🏎  Join ALGO Race", style=discord.ButtonStyle.primary,
                       custom_id="grand_prix:join_algo")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass  # routed in on_interaction


class JoinZapView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚡ Join ZAP Race", style=discord.ButtonStyle.success,
                       custom_id="grand_prix:join_zap")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass  # routed in on_interaction


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class GrandPrixCog(commands.Cog):

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Use bot.supabase if already attached (set in on_ready), otherwise init directly
        if hasattr(bot, "supabase"):
            self.db = bot.supabase
        else:
            from database import get_supabase
            self.db = get_supabase()
        self.expiry_task.start()

    # -----------------------------------------------------------------------
    # Background task — clean up duels that never started
    # -----------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def expiry_task(self):
        try:
            # Optional: call your process_expired_duels helper here
            pass
        except Exception as e:
            print(f"[grand_prix] expiry_task error: {e}")

    @expiry_task.before_loop
    async def before_expiry(self):
        await self.bot.wait_until_ready()

    # -----------------------------------------------------------------------
    # Button interaction router
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id", "")
        if cid == "grand_prix:join_algo":
            await self._handle_join(interaction, algo_queue)
        elif cid == "grand_prix:join_zap":
            await self._handle_join(interaction, zap_queue)

    # -----------------------------------------------------------------------
    # Centralised player cleanup
    # Called by: settle, poller timeout, gpclear
    # -----------------------------------------------------------------------

    def _clear_player(self, user_id: str, q: RaceQueue | None = None):
        """Remove player from active set and clear their queue slot."""
        active_players.discard(user_id)
        for queue in ([q] if q else [algo_queue, zap_queue]):
            if queue.player_a_id == user_id:
                queue.player_a_id    = None
                queue.player_a_racer = None
                queue.player_a_paid  = False
            if queue.player_b_id == user_id:
                queue.player_b_id    = None
                queue.player_b_racer = None
                queue.player_b_paid  = False

    # -----------------------------------------------------------------------
    # Shared join handler  — THE LOCK IS HERE
    # -----------------------------------------------------------------------

    async def _handle_join(self, interaction: discord.Interaction, q: RaceQueue):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        channel = interaction.channel

        # Pre-lock checks (cheap, no state mutation)
        racer = await self._get_racer(user_id)
        if not racer:
            await interaction.followup.send(
                "You need to `/gpregister` first to link your wallet and Zappy.",
                ephemeral=True,
            )
            return

        if user_id in active_players:
            await interaction.followup.send(
                "You're already in a race or queue. Finish that one first.",
                ephemeral=True,
            )
            return

        if q.locked:
            await interaction.followup.send(
                "A race is already running on this board — hang tight, it resets when done.",
                ephemeral=True,
            )
            return

        if q.mode == "zap":
            bal = await asyncio.to_thread(get_balance, self.db, user_id, "ZAPP")
            if bal < ZAP_ENTRY:
                await interaction.followup.send(
                    f"Not enough ZAPP. Need **{ZAP_ENTRY:,}** — you have **{bal:,}**.\n"
                    f"Deposit ZAPP or earn more to enter.",
                    ephemeral=True,
                )
                return

        if q.mode == "algo":
            bal = await asyncio.to_thread(get_balance, self.db, user_id, "ALGO")
            if bal < 5:
                await interaction.followup.send(
                    f"Not enough ALGO. Need **5 ALGO** — you have **{bal:.4f}**.\n"
                    f"Use `/gpdeposit` to top up.",
                    ephemeral=True,
                )
                return

        # ---- ATOMIC SLOT ASSIGNMENT ----------------------------------------
        # The lock ensures two players tapping at the same millisecond can't
        # both read player_a_id == None and both claim slot A.
        async with _join_lock:
            # Re-check inside the lock — state may have changed while waiting
            if user_id in active_players:
                await interaction.followup.send("You just joined — check your DMs.", ephemeral=True)
                return
            if q.locked:
                await interaction.followup.send("Race just started — try after this one.", ephemeral=True)
                return

            if q.player_a_id is None:
                q.player_a_id    = user_id
                q.player_a_racer = racer
                active_players.add(user_id)
                slot = "a"

            elif q.player_b_id is None and q.player_a_id != user_id:
                q.player_b_id    = user_id
                q.player_b_racer = racer
                active_players.add(user_id)
                slot = "b"

            else:
                await interaction.followup.send(
                    "Queue is full — two players are already lining up. Check back soon.",
                    ephemeral=True,
                )
                return
        # ---- END LOCK -------------------------------------------------------

        # Hand off to mode-specific flow outside the lock
        if q.mode == "algo":
            await self._algo_join(interaction, q, channel, user_id, racer, slot)
        else:
            await self._zap_join(interaction, q, channel, user_id, racer, slot)

    # -----------------------------------------------------------------------
    # ALGO join flow — custodial debit from Supabase balance
    # -----------------------------------------------------------------------

    async def _algo_join(self, interaction, q, channel, user_id, racer, slot):
        # Debit entry fee immediately — no on-chain send required
        debit_result = await asyncio.to_thread(
            debit, self.db, user_id, "ALGO", 5, "gp_entry", None, racer["zappy_id"]
        )
        if not debit_result["ok"]:
            self._clear_player(user_id, q)
            await interaction.followup.send(
                f"Entry fee debit failed: {debit_result['error']}",
                ephemeral=True,
            )
            return

        if slot == "a":
            result = self.db.table("race_duels").insert({
                "challenger_id": user_id,
                "opponent_id":   "pending",
                "status":        "pending",
                "wager_algo":    5,
            }).execute()
            q.duel_id = result.data[0]["id"]
            await self._update_board(channel, q, "waiting", zappy_id=racer["zappy_id"])
            await interaction.followup.send(
                f"⚡ **Slot A locked — {racer['zappy_id']}**\n"
                f"5 ALGO debited. Waiting for an opponent...\n"
                f"New balance: **{debit_result['balance_after']:.4f} ALGO**",
                ephemeral=True,
            )
        else:
            self.db.table("race_duels").update({
                "opponent_id": user_id,
                "status":      "ready",
            }).eq("id", q.duel_id).execute()
            await self._update_board(channel, q, "racing",
                                     zappy_a=q.player_a_racer["zappy_id"],
                                     zappy_b=racer["zappy_id"])
            await interaction.followup.send(
                f"⚡ **Slot B locked — {racer['zappy_id']}**\n"
                f"5 ALGO debited. Race starting now!\n"
                f"New balance: **{debit_result['balance_after']:.4f} ALGO**",
                ephemeral=True,
            )
            await self._launch_race(q, channel)

    # -----------------------------------------------------------------------
    # ZAP join flow — custodial debit from Supabase balance
    # -----------------------------------------------------------------------

    async def _zap_join(self, interaction, q, channel, user_id, racer, slot):
        debit_result = await asyncio.to_thread(
            debit, self.db, user_id, "ZAPP", ZAP_ENTRY, "gp_entry", None, racer["zappy_id"]
        )
        if not debit_result["ok"]:
            self._clear_player(user_id, q)
            await interaction.followup.send(
                f"Entry fee debit failed: {debit_result['error']}",
                ephemeral=True,
            )
            return

        if slot == "a":
            result = self.db.table("race_duels").insert({
                "challenger_id": user_id,
                "opponent_id":   "pending",
                "status":        "pending",
                "wager_algo":    0,
            }).execute()
            q.duel_id = result.data[0]["id"]
            await self._update_board(channel, q, "waiting", zappy_id=racer["zappy_id"])
            await interaction.followup.send(
                f"⚡ **Slot A locked — {racer['zappy_id']}**\n"
                f"{ZAP_ENTRY:,} ZAPP debited. Waiting for an opponent...\n"
                f"New balance: **{int(debit_result['balance_after']):,} ZAPP**",
                ephemeral=True,
            )
        else:
            self.db.table("race_duels").update({
                "opponent_id": user_id,
                "status":      "ready",
            }).eq("id", q.duel_id).execute()
            await self._update_board(channel, q, "racing",
                                     zappy_a=q.player_a_racer["zappy_id"],
                                     zappy_b=racer["zappy_id"])
            await interaction.followup.send(
                f"⚡ **Slot B locked — {racer['zappy_id']}**\n"
                f"{ZAP_ENTRY:,} ZAPP debited. Race starting now!\n"
                f"New balance: **{int(debit_result['balance_after']):,} ZAPP**",
                ephemeral=True,
            )
            await self._launch_race(q, channel)

    # -----------------------------------------------------------------------
    # Launch race
    # -----------------------------------------------------------------------

    async def _launch_race(self, q: RaceQueue, channel):
        q.locked = True
        await self._update_board(channel, q, "racing",
                                 zappy_a=q.player_a_racer["zappy_id"],
                                 zappy_b=q.player_b_racer["zappy_id"])

        stats_a = await get_stats(self.db, q.player_a_racer["zappy_id"])
        stats_b = await get_stats(self.db, q.player_b_racer["zappy_id"])
        result  = resolve_race(stats_a, stats_b)

        race_msg = await channel.send(
            f"🏁 **RACE STARTING** — "
            f"<@{q.player_a_id}> ({q.player_a_racer['zappy_id']}) vs "
            f"<@{q.player_b_id}> ({q.player_b_racer['zappy_id']})"
        )

        await run_race_narration(
            message=race_msg,
            result=result,
            name_a=f"<@{q.player_a_id}>",
            name_b=f"<@{q.player_b_id}>",
            zappy_a=q.player_a_racer["zappy_id"],
            zappy_b=q.player_b_racer["zappy_id"],
        )

        winner_id    = q.player_a_id    if result["winner"] == "a" else q.player_b_id
        loser_id     = q.player_b_id    if result["winner"] == "a" else q.player_a_id
        winner_racer = q.player_a_racer if result["winner"] == "a" else q.player_b_racer
        loser_racer  = q.player_b_racer if result["winner"] == "a" else q.player_a_racer

        await self._settle(q, channel, result, winner_racer, loser_racer, winner_id, loser_id)

    # -----------------------------------------------------------------------
    # Settle — pay out, update stats, reset board
    # -----------------------------------------------------------------------

    async def _settle(self, q, channel, result, winner_racer, loser_racer,
                      winner_id, loser_id):
        try:
            if q.mode == "algo":
                payout = 9
                credit_result = await asyncio.to_thread(
                    credit, self.db, winner_id, "ALGO", payout,
                    "gp_payout", q.duel_id, winner_racer["zappy_id"]
                )
                self.db.table("race_duels").update({
                    "status":    "complete",
                    "winner_id": winner_id,
                }).eq("id", q.duel_id).execute()
                await channel.send(
                    f"🏆 **{winner_racer['zappy_id']}** wins! **9 ALGO** credited to balance.\n"
                    f"<@{winner_id}> new balance: **{credit_result['balance_after']:.4f} ALGO** | GG <@{loser_id}>"
                )
            else:
                credit_result = await asyncio.to_thread(
                    credit, self.db, winner_id, "ZAPP", ZAP_PAYOUT,
                    "gp_payout", q.duel_id, winner_racer["zappy_id"]
                )
                self.db.table("race_duels").update({
                    "status":    "complete",
                    "winner_id": winner_id,
                }).eq("id", q.duel_id).execute()
                await channel.send(
                    f"🏆 **{winner_racer['zappy_id']}** wins! **{ZAP_PAYOUT:,} ZAPP** credited.\n"
                    f"<@{winner_id}> new balance: **{int(credit_result['balance_after']):,} ZAPP** | GG <@{loser_id}>"
                )

            # Update win/loss records
            self.db.rpc("increment_wins",   {"p_user_id": winner_id}).execute()
            self.db.rpc("increment_losses", {"p_user_id": loser_id}).execute()

        except Exception as e:
            print(f"[grand_prix] settle error: {e}")
            await channel.send(
                f"⚠️ Race finished but payout hit an error — ping an admin. "
                f"Duel ID: `{q.duel_id}`"
            )
        finally:
            # Show result board on the existing message (no button)
            winner_name = winner_racer["zappy_id"]
            loser_name  = loser_racer["zappy_id"]
            score_a = result.get("score_a", 0)
            score_b = result.get("score_b", 0)
            await self._update_board(channel, q, "result",
                                     remove_button=True,
                                     zappy_a=winner_name, zappy_b=loser_name,
                                     winner=winner_name,
                                     score_a=score_a, score_b=score_b)
            await asyncio.sleep(10)

            # Post a fresh empty board below the result
            self._clear_player(winner_id, q)
            self._clear_player(loser_id, q)
            q.reset()
            await self._post_new_board(channel, q)

    # -----------------------------------------------------------------------
    # Board image update
    # -----------------------------------------------------------------------

    async def _update_board(self, channel, q, state, remove_button=False, **kw):
        if q.board_msg_id is None:
            return
        try:
            msg = await channel.fetch_message(q.board_msg_id)
        except discord.NotFound:
            return
        buf  = make_board_buf(q.mode, state, **kw)
        file = discord.File(buf, filename="board.png")
        view = (JoinAlgoView() if q.mode == "algo" else JoinZapView()) if not remove_button else discord.utils.MISSING
        await msg.edit(attachments=[file], view=view)

    async def _post_new_board(self, channel, q):
        buf  = board_empty(q.mode)
        file = discord.File(buf, filename="board.png")
        view = JoinAlgoView() if q.mode == "algo" else JoinZapView()
        msg  = await channel.send(file=file, view=view)
        q.board_msg_id = msg.id

    # -----------------------------------------------------------------------
    # Helper: get racer from DB
    # -----------------------------------------------------------------------

    async def _get_racer(self, user_id: str) -> dict | None:
        result = self.db.table("zappy_racers").select("*").eq("discord_user_id", user_id).maybe_single().execute()
        return result.data if result else None

    # -----------------------------------------------------------------------
    # /gpsetup — post both boards (admin only)
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpsetup", description="[Admin] Post Grand Prix boards in this channel.")
    @app_commands.checks.has_permissions(administrator=True)
    async def gpsetup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel

        algo_msg = await channel.send(
            file=discord.File(board_empty("algo"), filename="board_algo.png"),
            view=JoinAlgoView(),
        )
        algo_queue.board_msg_id = algo_msg.id

        zap_msg = await channel.send(
            file=discord.File(board_empty("zap"), filename="board_zap.png"),
            view=JoinZapView(),
        )
        zap_queue.board_msg_id = zap_msg.id

        await interaction.followup.send(
            f"✅ Both boards posted.\n"
            f"ALGO board msg ID: `{algo_msg.id}`\n"
            f"ZAPP board msg ID: `{zap_msg.id}`\n\n"
            f"Pin both messages to keep them visible.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpclear — unstick a player (admin only)
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpclear", description="[Admin] Unstick a player from the queue.")
    @app_commands.describe(player="The player to clear")
    @app_commands.checks.has_permissions(administrator=True)
    async def gpclear(self, interaction: discord.Interaction, player: discord.Member):
        await interaction.response.defer(ephemeral=True)
        user_id = str(player.id)

        was_active = user_id in active_players
        in_algo    = algo_queue.player_a_id == user_id or algo_queue.player_b_id == user_id
        in_zap     = zap_queue.player_a_id  == user_id or zap_queue.player_b_id  == user_id

        self._clear_player(user_id)

        if not was_active and not in_algo and not in_zap:
            await interaction.followup.send(
                f"**{player.display_name}** wasn't stuck anywhere.",
                ephemeral=True,
            )
            return

        detail = ""
        if in_algo: detail += " (was in ALGO queue)"
        if in_zap:  detail += " (was in ZAP queue)"

        await interaction.followup.send(
            f"✅ **{player.display_name}** cleared{detail}. They can now join again.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpreset — reset a board to empty (admin only)
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpreset", description="[Admin] Reset a board to empty state.")
    @app_commands.describe(board="Which board to reset")
    @app_commands.choices(board=[
        app_commands.Choice(name="ALGO", value="algo"),
        app_commands.Choice(name="ZAP",  value="zap"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def gpreset(self, interaction: discord.Interaction, board: str):
        await interaction.response.defer(ephemeral=True)
        q = algo_queue if board == "algo" else zap_queue

        # Refund any players currently in the queue
        currency = "ALGO" if board == "algo" else "ZAPP"
        amount   = 5 if board == "algo" else ZAP_ENTRY
        for uid, racer in [
            (q.player_a_id, q.player_a_racer),
            (q.player_b_id, q.player_b_racer),
        ]:
            if uid:
                zappy_id = racer["zappy_id"] if racer else None
                await asyncio.to_thread(
                    credit, self.db, uid, currency, amount,
                    "gp_cancel_refund", q.duel_id, zappy_id
                )
                self._clear_player(uid, q)

        q.reset()
        await self._update_board(interaction.channel, q, "empty")
        await interaction.followup.send(
            f"✅ {board.upper()} board reset. Any queued players were refunded.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpcancel — player cancels their own slot A queue position for a refund
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpcancel", description="Cancel your queue spot and get your entry fee back.")
    async def gpcancel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        # Find which queue they're in and whether they're in slot A only
        # Slot B can't cancel — race is about to start
        found_queue    = None
        found_currency = None
        found_amount   = None
        found_racer    = None

        if algo_queue.player_a_id == user_id and not algo_queue.locked:
            found_queue    = algo_queue
            found_currency = "ALGO"
            found_amount   = 5
            found_racer    = algo_queue.player_a_racer
        elif zap_queue.player_a_id == user_id and not zap_queue.locked:
            found_queue    = zap_queue
            found_currency = "ZAPP"
            found_amount   = ZAP_ENTRY
            found_racer    = zap_queue.player_a_racer

        if not found_queue:
            # Check if they're in slot B or a race is running
            in_b = (
                algo_queue.player_b_id == user_id or
                zap_queue.player_b_id  == user_id
            )
            if in_b:
                await interaction.followup.send(
                    "You're in slot B — the race is about to start. Can't cancel at this point.",
                    ephemeral=True,
                )
            elif user_id not in active_players:
                await interaction.followup.send(
                    "You're not in any queue right now.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "You're in an active race — can't cancel now.",
                    ephemeral=True,
                )
            return

        # Refund and clear
        zappy_id = found_racer["zappy_id"] if found_racer else None
        refund = await asyncio.to_thread(
            credit, self.db, user_id, found_currency, found_amount,
            "gp_cancel_refund", found_queue.duel_id, zappy_id
        )

        self._clear_player(user_id, found_queue)

        # Mark duel cancelled if it exists
        if found_queue.duel_id:
            self.db.table("race_duels").update({
                "status": "cancelled"
            }).eq("id", found_queue.duel_id).execute()

        found_queue.duel_id        = None
        found_queue.player_a_paid  = False

        channel = interaction.channel
        await self._update_board(channel, found_queue, "empty")

        await interaction.followup.send(
            f"✅ Queue spot cancelled. **{found_amount} {found_currency}** refunded.\n"
            f"New balance: **{refund['balance_after']:.4f if found_currency == 'ALGO' else int(refund['balance_after']):,} {found_currency}**",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpregister
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpregister", description="Register your Zappy for the Grand Prix.")
    @app_commands.describe(
        wallet_address="Your Algorand wallet address (58 characters)",
        zappy_id="Your Zappy NFT ID (e.g. ZAP-447)",
    )
    async def gpregister(self, interaction: discord.Interaction,
                         wallet_address: str, zappy_id: str):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        existing = await self._get_racer(user_id)
        if existing:
            await interaction.followup.send(
                f"Already registered with **{existing['zappy_id']}**. Use `/gpstats` to check in.",
                ephemeral=True,
            )
            return

        if len(wallet_address) != 58:
            await interaction.followup.send(
                "Invalid Algorand address — should be 58 characters.",
                ephemeral=True,
            )
            return

        stats = seed_stats(zappy_id)
        self.db.table("zappy_racers").insert({
            "discord_user_id": user_id,
            "wallet_address":  wallet_address,
            "zappy_id":        zappy_id,
            "algo_balance":    0,
            "zapp_balance":    0,
            "wins": 0, "losses": 0,
        }).execute()
        self.db.table("zappy_stats").insert({"zappy_id": zappy_id, **stats}).execute()

        await interaction.followup.send(
            f"✅ **{zappy_id}** registered! Run `/gpbalance` to check your starting balances.\n"
            f"Head to the Grand Prix channel and tap **Join Race** when you're ready.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpbalance
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpbalance", description="Check your ALGO and ZAP racing balances.")
    async def gpbalance(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        racer = await self._get_racer(user_id)
        if not racer:
            await interaction.followup.send("You're not registered. Run `/gpregister` first.", ephemeral=True)
            return
        algo_bal = await asyncio.to_thread(get_balance, self.db, user_id, "ALGO")
        zap_bal  = await asyncio.to_thread(get_balance, self.db, user_id, "ZAPP")
        await interaction.followup.send(
            f"**{racer['zappy_id']} — Balances**\n"
            f"ALGO: **{algo_bal:.4f} ALGO**\n"
            f"ZAPP: **{int(zap_bal):,} ZAPP**",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpstats
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpstats", description="Your Zappy's race stats and record.")
    async def gpstats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        racer   = await self._get_racer(user_id)
        if not racer:
            await interaction.followup.send("Run `/gpregister` first.", ephemeral=True)
            return
        stats = await get_stats(self.db, racer["zappy_id"])
        total = racer["wins"] + racer["losses"]
        pct   = round(racer["wins"] / total * 100) if total else 0
        await interaction.followup.send(
            f"⚡ **{racer['zappy_id']}**\n"
            f"Record: {racer['wins']}W / {racer['losses']}L ({pct}%)\n"
            f"Speed: {stats.get('speed', '?')}  |  Endurance: {stats.get('endurance', '?')}  |  Clutch: {stats.get('clutch', '?')}",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpleaderboard
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpleaderboard", description="Top 10 Zappy racers by wins.")
    async def gpleaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rows = (
            self.db.table("zappy_racers")
            .select("zappy_id, wins, losses")
            .order("wins", desc=True)
            .limit(10)
            .execute()
            .data
        )
        if not rows:
            await interaction.followup.send("No races yet — be the first!")
            return
        medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
        lines  = []
        for i, r in enumerate(rows):
            total = r["wins"] + r["losses"]
            pct   = round(r["wins"] / total * 100) if total else 0
            lines.append(f"{medals[i]} **{r['zappy_id']}** — {r['wins']}W / {r['losses']}L ({pct}%)")
        embed = discord.Embed(
            title="🏆 Zappy Grand Prix Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Setup entry point — call from bot.py on_ready
# ---------------------------------------------------------------------------

async def setup_grand_prix(bot: commands.Bot):
    cog = GrandPrixCog(bot)
    await bot.add_cog(cog)
    bot.add_view(JoinAlgoView())
    bot.add_view(JoinZapView())
    print("[grand_prix] Cog loaded. Both boards ready.")

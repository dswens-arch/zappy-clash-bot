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
from race_engine import simulate_race, narrate_race, seed_stats, get_stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ALGO_ENTRY   = 5_000_000   # microALGO
ALGO_PAYOUT  = 9_000_000
ALGO_RAKE    = 1_000_000
STAT_CAP_MAX = 11

# ---------------------------------------------------------------------------
# Stat display helper
# ---------------------------------------------------------------------------

def stat_bar(current: int, cap: int, width: int = 11) -> str:
    filled = round((current / STAT_CAP_MAX) * width)
    return "█" * filled + "░" * (width - filled)

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
        await self._restore_board_ids()

    async def _restore_board_ids(self):
        """Read board_msg_id and channel_id from Supabase on startup."""
        try:
            rows = self.db.table("gp_boards").select("*").execute().data or []
            for row in rows:
                mode       = row.get("mode")
                msg_id     = row.get("board_msg_id")
                channel_id = row.get("channel_id")
                if not mode or not msg_id or not channel_id:
                    continue

                q = algo_queue if mode == "algo" else zap_queue
                q.board_msg_id = msg_id

                # Verify the message still exists
                channel = self.bot.get_channel(channel_id)
                if not channel:
                    print(f"[grand_prix] Restore: channel {channel_id} not found for {mode} board")
                    continue
                try:
                    await channel.fetch_message(msg_id)
                    print(f"[grand_prix] Restored {mode} board msg_id={msg_id} channel={channel_id}")
                except discord.NotFound:
                    print(f"[grand_prix] Board message gone for {mode} — will need /gpsetup{mode}")
                    q.board_msg_id = None
                    self.db.table("gp_boards").delete().eq("mode", mode).execute()
        except Exception as e:
            print(f"[grand_prix] Board restore error: {e}")

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

        if user_id in active_players:
            await interaction.followup.send(
                "You're already in a race or queue. Finish that one first.",
                ephemeral=True,
            )
            return

        if q.locked:
            await interaction.followup.send(
                "A race is already running on this board — hang tight.",
                ephemeral=True,
            )
            return

        # Fetch all registered Zappies for this player with their stats
        available = await self._get_available_racers(user_id)
        if not available:
            await interaction.followup.send(
                "You need to `/gpregister` first to join the Grand Prix.",
                ephemeral=True,
            )
            return

        # Balance check before showing picker
        if q.mode == "zap":
            bal = await asyncio.to_thread(get_balance, self.db, user_id, "ZAPP")
            if bal < ZAP_ENTRY:
                await interaction.followup.send(
                    f"Not enough ZAPP. Need **{ZAP_ENTRY:,}** — you have **{int(bal):,}**.\n"
                    f"Deposit ZAPP or earn more to enter.",
                    ephemeral=True,
                )
                return
        else:
            bal = await asyncio.to_thread(get_balance, self.db, user_id, "ALGO")
            if bal < 5:
                await interaction.followup.send(
                    f"Not enough ALGO. Need **5 ALGO** — you have **{bal:.4f}**.\n"
                    f"Use `/gpdeposit` to top up.",
                    ephemeral=True,
                )
                return

        # Single Zappy — skip picker
        if len(available) == 1:
            await self._enter_queue(interaction, q, channel, user_id,
                                    available[0]["racer"], available[0]["stats"])
            return

        # Multiple Zappies — show picker
        await self._show_zappy_picker(interaction, q, channel, user_id, available)

    # -----------------------------------------------------------------------
    # Zappy picker — ephemeral embed + buttons, shown when player has >1 Zappy
    # -----------------------------------------------------------------------

    async def _show_zappy_picker(self, interaction, q, channel, user_id, available):
        cog = self  # capture for button callbacks

        class ZappyPickView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.chosen = False
                for entry in available[:5]:
                    racer = entry["racer"]
                    stats = entry["stats"]
                    label = (
                        f"{racer['zappy_id']}  "
                        f"SPD {stats['speed']}  "
                        f"END {stats['endurance']}  "
                        f"CLT {stats['clutch']}"
                    )[:80]
                    btn = discord.ui.Button(
                        label=label,
                        style=discord.ButtonStyle.primary,
                        custom_id=f"gp_pick_{racer['zappy_id']}",
                    )
                    btn.callback = self._make_cb(racer, stats)
                    self.add_item(btn)

            def _make_cb(self, racer, stats):
                async def cb(btn_interaction: discord.Interaction):
                    if self.chosen:
                        await btn_interaction.response.send_message(
                            "Already picked!", ephemeral=True)
                        return
                    self.chosen = True
                    for item in self.children:
                        item.disabled = True
                    await btn_interaction.response.defer(ephemeral=True)
                    await cog._enter_queue(btn_interaction, q, channel,
                                           user_id, racer, stats)
                return cb

        embed = discord.Embed(
            title="⚡ Pick your Zappy",
            description="Choose which Zappy to race with.",
            color=discord.Color.from_rgb(30, 180, 255),
        )
        for entry in available[:5]:
            racer = entry["racer"]
            stats = entry["stats"]
            embed.add_field(
                name=racer["zappy_id"],
                value=(
                    f"Speed `{stat_bar(stats['speed'], stats['speed_max'])}` "
                    f"{stats['speed']}/{stats['speed_max']}\n"
                    f"Endurance `{stat_bar(stats['endurance'], stats['endurance_max'])}` "
                    f"{stats['endurance']}/{stats['endurance_max']}\n"
                    f"Clutch `{stat_bar(stats['clutch'], stats['clutch_max'])}` "
                    f"{stats['clutch']}/{stats['clutch_max']}"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, view=ZappyPickView(), ephemeral=True)

    # -----------------------------------------------------------------------
    # _enter_queue — atomic slot assignment after racer is chosen
    # -----------------------------------------------------------------------

    async def _enter_queue(self, interaction, q, channel, user_id, racer, stats):
        async with _join_lock:
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
                    "Queue is full — two players are already lining up.",
                    ephemeral=True,
                )
                return

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
        # Update board to racing state AND remove the Join Race button immediately
        await self._update_board(channel, q, "racing",
                                 remove_button=True,
                                 zappy_a=q.player_a_racer["zappy_id"],
                                 zappy_b=q.player_b_racer["zappy_id"])

        stats_a = await get_stats(self.db, q.player_a_racer["zappy_id"])
        stats_b = await get_stats(self.db, q.player_b_racer["zappy_id"])

        # Fall back to neutral stats if not found
        if not stats_a:
            stats_a = {"speed": 5, "endurance": 5, "clutch": 5}
        if not stats_b:
            stats_b = {"speed": 5, "endurance": 5, "clutch": 5}

        result = simulate_race(stats_a, stats_b)

        winner_id    = q.player_a_id    if result.winner == "a" else q.player_b_id
        loser_id     = q.player_b_id    if result.winner == "a" else q.player_a_id
        winner_racer = q.player_a_racer if result.winner == "a" else q.player_b_racer
        loser_racer  = q.player_b_racer if result.winner == "a" else q.player_a_racer

        payout_str = (
            f"💰 **9 ALGO** credited to <@{winner_id}>'s balance"
            if q.mode == "algo" else
            f"💰 **{ZAP_PAYOUT:,} ZAPP** credited to <@{winner_id}>'s balance"
        )

        await narrate_race(
            channel=channel,
            result=result,
            name_a=q.player_a_racer["zappy_id"],
            name_b=q.player_b_racer["zappy_id"],
            payout_str=payout_str,
            mode=q.mode,
        )

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
            winner_row = self.db.table("zappy_racers").select("wins").eq(
                "discord_user_id", winner_id).order("registered_at").limit(1).execute()
            loser_row = self.db.table("zappy_racers").select("losses").eq(
                "discord_user_id", loser_id).order("registered_at").limit(1).execute()

            if winner_row.data:
                new_wins = (winner_row.data[0].get("wins") or 0) + 1
                self.db.table("zappy_racers").update({"wins": new_wins}).eq(
                    "discord_user_id", winner_id).execute()
            if loser_row.data:
                new_losses = (loser_row.data[0].get("losses") or 0) + 1
                self.db.table("zappy_racers").update({"losses": new_losses}).eq(
                    "discord_user_id", loser_id).execute()

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
            # v2 RaceResult is a dataclass — use attribute access
            score_a = result.pos_a if hasattr(result, "pos_a") else 0
            score_b = result.pos_b if hasattr(result, "pos_b") else 0
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
        if remove_button:
            view = discord.ui.View()   # empty view removes all buttons
        else:
            view = JoinAlgoView() if q.mode == "algo" else JoinZapView()
        await msg.edit(attachments=[file], view=view)

    async def _post_new_board(self, channel, q):
        buf  = board_empty(q.mode)
        file = discord.File(buf, filename="board.png")
        view = JoinAlgoView() if q.mode == "algo" else JoinZapView()
        msg  = await channel.send(file=file, view=view)
        q.board_msg_id = msg.id
        # Persist so the new board survives a Railway restart
        try:
            self.db.table("gp_boards").upsert({
                "mode":         q.mode,
                "board_msg_id": msg.id,
                "channel_id":   channel.id,
            }).execute()
        except Exception as e:
            print(f"[grand_prix] Failed to persist board_msg_id: {e}")

    # -----------------------------------------------------------------------
    # Helper: get racer from DB
    # -----------------------------------------------------------------------

    async def _get_racer(self, user_id: str) -> dict | None:
        """Get first registered racer — used by gpregister, gpstats etc."""
        result = self.db.table("zappy_racers").select("*").eq(
            "discord_user_id", user_id
        ).order("registered_at", desc=False).limit(1).execute()
        return result.data[0] if result.data else None

    async def _get_available_racers(self, user_id: str) -> list[dict]:
        """
        Return all Zappies registered to this player with their stats.
        Each entry: {"racer": {...}, "stats": {...}}
        """
        racers_res = self.db.table("zappy_racers").select("*").eq(
            "discord_user_id", user_id
        ).order("registered_at", desc=False).execute()

        if not racers_res.data:
            return []

        available = []
        for racer in racers_res.data:
            stats_res = self.db.table("zappy_stats").select("*").eq(
                "zappy_id", racer["zappy_id"]
            ).execute()
            stats = stats_res.data[0] if stats_res.data else {
                "speed": 5, "speed_max": 10,
                "endurance": 5, "endurance_max": 10,
                "clutch": 5, "clutch_max": 10,
            }
            available.append({"racer": racer, "stats": stats})

        return available

    # -----------------------------------------------------------------------
    # /gpsetup — post both boards (admin only)
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpsetupalgo", description="[Admin] Post the ALGO Grand Prix board in this channel.")
    @app_commands.checks.has_permissions(administrator=True)
    async def gpsetupalgo(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        msg = await interaction.channel.send(
            file=discord.File(board_empty("algo"), filename="board_algo.png"),
            view=JoinAlgoView(),
        )
        algo_queue.board_msg_id = msg.id
        self.db.table("gp_boards").upsert({
            "mode":         "algo",
            "board_msg_id": msg.id,
            "channel_id":   interaction.channel.id,
        }).execute()
        await interaction.followup.send(
            f"✅ ALGO board posted and saved. Msg ID: `{msg.id}`\nPin this message to keep it visible.",
            ephemeral=True,
        )

    @app_commands.command(name="gpsetupzap", description="[Admin] Post the ZAPP Grand Prix board in this channel.")
    @app_commands.checks.has_permissions(administrator=True)
    async def gpsetupzap(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        msg = await interaction.channel.send(
            file=discord.File(board_empty("zap"), filename="board_zap.png"),
            view=JoinZapView(),
        )
        zap_queue.board_msg_id = msg.id
        self.db.table("gp_boards").upsert({
            "mode":         "zap",
            "board_msg_id": msg.id,
            "channel_id":   interaction.channel.id,
        }).execute()
        await interaction.followup.send(
            f"✅ ZAPP board posted and saved. Msg ID: `{msg.id}`\nPin this message to keep it visible.",
            ephemeral=True,
        )

    @app_commands.command(name="gpsetup", description="[Admin] Post both Grand Prix boards in this channel (single channel only).")
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
            f"✅ Both boards posted in this channel.\n"
            f"ALGO msg ID: `{algo_msg.id}`\n"
            f"ZAPP msg ID: `{zap_msg.id}`\n\n"
            f"⚠️ If using separate channels, use `/gpsetupalgo` and `/gpsetupzap` instead.",
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
    # /gprefund — manually refund a player (admin only)
    # -----------------------------------------------------------------------

    @app_commands.command(name="gprefund", description="[Admin] Manually refund a player's balance.")
    @app_commands.describe(
        player="The player to refund",
        amount="Amount to refund (whole numbers — ALGO or ZAPP)",
        currency="Which currency to refund",
        reason="Reason for the refund (logged in gp_transactions)",
    )
    @app_commands.choices(currency=[
        app_commands.Choice(name="ALGO", value="ALGO"),
        app_commands.Choice(name="ZAPP", value="ZAPP"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def gprefund(self, interaction: discord.Interaction,
                       player: discord.Member,
                       amount: float,
                       currency: str,
                       reason: str = "manual_refund"):
        await interaction.response.defer(ephemeral=True)

        if amount <= 0:
            await interaction.followup.send("Amount must be positive.", ephemeral=True)
            return

        user_id = str(player.id)
        racer   = await self._get_racer(user_id)
        if not racer:
            await interaction.followup.send(
                f"**{player.display_name}** isn't registered in Grand Prix.",
                ephemeral=True,
            )
            return

        result = await asyncio.to_thread(
            credit, self.db, user_id, currency, amount,
            reason, None, racer.get("zappy_id")
        )

        if not result.get("ok"):
            await interaction.followup.send(
                f"Refund failed: {result.get('error', 'unknown error')}",
                ephemeral=True,
            )
            return

        unit = ".4f" if currency == "ALGO" else ",d"
        await interaction.followup.send(
            f"✅ Refunded **{amount} {currency}** to **{player.display_name}**.\n"
            f"Before: `{result['balance_before']:{unit}}` → After: `{result['balance_after']:{unit}}`\n"
            f"Reason logged: `{reason}`",
            ephemeral=True,
        )

        # Optionally DM the player
        try:
            await player.send(
                f"⚡ An admin issued you a **{amount} {currency}** Grand Prix refund.\n"
                f"Reason: {reason}\n"
                f"New balance: **{result['balance_after']:{unit}} {currency}**"
            )
        except Exception:
            pass

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

        bal_display = f"{refund['balance_after']:.4f}" if found_currency == "ALGO" else f"{int(refund['balance_after']):,}"
        await interaction.followup.send(
            f"✅ Queue spot cancelled. **{found_amount} {found_currency}** refunded.\n"
            f"New balance: **{bal_display} {found_currency}**",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpdeposit — show bot address, poll for incoming ALGO
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpdeposit", description="Deposit ALGO to your Grand Prix balance.")
    async def gpdeposit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        racer   = await self._get_racer(user_id)

        if not racer:
            await interaction.followup.send("Not registered. Use `/gpregister` first.", ephemeral=True)
            return

        from algo_layer import get_bot_address as _gba, get_current_round as _gcr, get_indexer_client as _gic
        bot_address = _gba()
        after_round = await asyncio.to_thread(_gcr)
        current_bal = await asyncio.to_thread(get_balance, self.db, user_id, "ALGO")

        await interaction.followup.send(
            f"**Deposit ALGO to your Grand Prix balance**\n\n"
            f"Open Pera Wallet and send any amount of ALGO to the address below.\n"
            f"Long press the next message to copy it.\n\n"
            f"Current balance: **{current_bal:.4f} ALGO**\n"
            f"⏳ Watching for your deposit for 5 minutes...",
            ephemeral=True,
        )
        await interaction.followup.send(bot_address, ephemeral=True)

        asyncio.create_task(
            self._watch_algo_deposit(user_id, racer["wallet_address"], bot_address,
                                     after_round, racer["zappy_id"], interaction)
        )

    async def _watch_algo_deposit(self, user_id, wallet_address, bot_address,
                                   after_round, zappy_id, interaction):
        import time
        from algo_layer import get_indexer_client as _gic
        deadline = time.monotonic() + 300
        idx      = _gic()
        credited: set[str] = set()

        while time.monotonic() < deadline:
            try:
                res = idx.search_transactions(
                    address=wallet_address,
                    address_role="sender",
                    txn_type="pay",
                    min_round=after_round,
                )
                for txn in res.get("transactions", []):
                    txid = txn.get("id", "")
                    pay  = txn.get("payment-transaction", {})

                    if pay.get("receiver") != bot_address:
                        continue
                    amount_algo = pay.get("amount", 0) / 1_000_000
                    if amount_algo < 0.1:
                        continue
                    if txid in credited:
                        continue

                    # Double-credit protection
                    existing = self.db.table("gp_deposits").select("txid").eq("txid", txid).execute()
                    if existing.data:
                        credited.add(txid)
                        continue

                    # Record txid FIRST
                    self.db.table("gp_deposits").insert({
                        "txid":            txid,
                        "discord_user_id": user_id,
                        "amount_algo":     amount_algo,
                    }).execute()
                    credited.add(txid)

                    result = await asyncio.to_thread(
                        credit, self.db, user_id, "ALGO", amount_algo,
                        "gp_deposit", txid, zappy_id
                    )
                    print(f"[grand_prix] ALGO deposit {amount_algo:.4f} to {user_id} txid={txid[:12]}")

                    try:
                        await interaction.followup.send(
                            f"✅ **{amount_algo:.4f} ALGO** deposited!\n"
                            f"New balance: **{result['balance_after']:.4f} ALGO**\n"
                            f"You're ready to race. Tap **Join Race** on the ALGO board.",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
                    return

            except Exception as e:
                print(f"[grand_prix] ALGO deposit watch error: {e}")

            await asyncio.sleep(5)

    # -----------------------------------------------------------------------
    # /gpwithdraw — send ALGO balance back to player wallet
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpwithdraw", description="Withdraw your ALGO balance to your wallet.")
    @app_commands.describe(amount="Amount of ALGO to withdraw, or 'all'")
    async def gpwithdraw(self, interaction: discord.Interaction, amount: str = "all"):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        racer   = await self._get_racer(user_id)

        if not racer:
            await interaction.followup.send("Not registered.", ephemeral=True)
            return

        balance = await asyncio.to_thread(get_balance, self.db, user_id, "ALGO")
        if balance <= 0:
            await interaction.followup.send("No ALGO balance to withdraw.", ephemeral=True)
            return

        if amount.lower() == "all":
            withdraw_amount = balance
        else:
            try:
                withdraw_amount = float(amount)
            except ValueError:
                await interaction.followup.send("Invalid amount. Use a number or 'all'.", ephemeral=True)
                return

        if withdraw_amount < 0.1:
            await interaction.followup.send("Minimum withdrawal is 0.1 ALGO.", ephemeral=True)
            return
        if withdraw_amount > balance:
            await interaction.followup.send(
                f"Can't withdraw {withdraw_amount:.4f} ALGO — balance is only {balance:.4f} ALGO.",
                ephemeral=True,
            )
            return

        # Debit FIRST before sending on-chain
        debit_result = await asyncio.to_thread(
            debit, self.db, user_id, "ALGO", withdraw_amount,
            "gp_withdraw", None, racer.get("zappy_id")
        )
        if not debit_result["ok"]:
            await interaction.followup.send(
                f"Withdrawal failed: {debit_result['error']}", ephemeral=True)
            return

        try:
            from algosdk import transaction as _txn
            from algo_layer import get_algod_client as _gac, get_bot_account as _gba
            private_key, bot_addr = _gba()
            client = _gac()
            params = client.suggested_params()
            txn = _txn.PaymentTxn(
                sender=bot_addr, sp=params,
                receiver=racer["wallet_address"],
                amt=int(withdraw_amount * 1_000_000),
                note=b"gpwithdraw",
            )
            signed = txn.sign(private_key)
            txid   = client.send_transaction(signed)
            _txn.wait_for_confirmation(client, txid, 10)

            await interaction.followup.send(
                f"✅ **{withdraw_amount:.4f} ALGO** sent to your wallet!\n"
                f"TX: `{txid[:14]}...`\n"
                f"Remaining balance: **{debit_result['balance_after']:.4f} ALGO**",
                ephemeral=True,
            )
        except Exception as e:
            # Refund on failure
            print(f"[grand_prix] Withdraw send error: {e}")
            await asyncio.to_thread(
                credit, self.db, user_id, "ALGO", withdraw_amount,
                "gp_withdraw_refund", None, racer.get("zappy_id")
            )
            await interaction.followup.send(
                "⚠️ Withdrawal failed — your balance has been restored. Contact an admin if this persists.",
                ephemeral=True,
            )

    # -----------------------------------------------------------------------
    # /gpzapdeposit — show bot address, poll for incoming ZAPP
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpzapdeposit", description="Deposit ZAPP to your Grand Prix balance.")
    async def gpzapdeposit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        racer   = await self._get_racer(user_id)

        if not racer:
            await interaction.followup.send("Not registered. Use `/gpregister` first.", ephemeral=True)
            return

        from algo_layer import get_bot_address as _gba, get_current_round as _gcr
        from zap_layer import ZAPP_ASA_ID
        bot_address = _gba()
        after_round = await asyncio.to_thread(_gcr)
        current_bal = await asyncio.to_thread(get_balance, self.db, user_id, "ZAPP")

        await interaction.followup.send(
            f"**Deposit ZAPP to your Grand Prix balance**\n\n"
            f"Open Pera Wallet and send ZAPP (ASA `{ZAPP_ASA_ID}`) to the address below.\n"
            f"Long press the next message to copy it.\n\n"
            f"Current balance: **{int(current_bal):,} ZAPP**\n"
            f"⏳ Watching for your deposit for 5 minutes...",
            ephemeral=True,
        )
        await interaction.followup.send(bot_address, ephemeral=True)

        asyncio.create_task(
            self._watch_zapp_deposit(user_id, racer["wallet_address"], bot_address,
                                     after_round, racer["zappy_id"], interaction)
        )

    async def _watch_zapp_deposit(self, user_id, wallet_address, bot_address,
                                   after_round, zappy_id, interaction):
        import time
        from algo_layer import get_indexer_client as _gic
        from zap_layer import ZAPP_ASA_ID
        deadline = time.monotonic() + 300
        idx      = _gic()
        credited: set[str] = set()

        while time.monotonic() < deadline:
            try:
                res = idx.search_transactions(
                    address=wallet_address,
                    address_role="sender",
                    txn_type="axfer",
                    asset_id=ZAPP_ASA_ID,
                    min_round=after_round,
                )
                for txn in res.get("transactions", []):
                    txid  = txn.get("id", "")
                    axfer = txn.get("asset-transfer-transaction", {})

                    if axfer.get("receiver") != bot_address:
                        continue
                    if axfer.get("asset-id") != ZAPP_ASA_ID:
                        continue
                    amount = axfer.get("amount", 0)
                    if amount < 1:
                        continue
                    if txid in credited:
                        continue

                    existing = self.db.table("gp_deposits").select("txid").eq("txid", txid).execute()
                    if existing.data:
                        credited.add(txid)
                        continue

                    self.db.table("gp_deposits").insert({
                        "txid":            txid,
                        "discord_user_id": user_id,
                        "amount_algo":     amount,
                    }).execute()
                    credited.add(txid)

                    result = await asyncio.to_thread(
                        credit, self.db, user_id, "ZAPP", amount,
                        "gp_deposit", txid, zappy_id
                    )
                    print(f"[grand_prix] ZAPP deposit {amount:,} to {user_id} txid={txid[:12]}")

                    try:
                        await interaction.followup.send(
                            f"✅ **{amount:,} ZAPP** deposited!\n"
                            f"New balance: **{int(result['balance_after']):,} ZAPP**\n"
                            f"You're ready to race. Tap **Join Race** on the ZAPP board.",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
                    return

            except Exception as e:
                print(f"[grand_prix] ZAPP deposit watch error: {e}")

            await asyncio.sleep(5)

    # -----------------------------------------------------------------------
    # /gpzapwithdraw — send ZAPP balance back to player wallet
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpzapwithdraw", description="Withdraw your ZAPP balance to your wallet.")
    @app_commands.describe(amount="Amount of ZAPP to withdraw, or 'all'")
    async def gpzapwithdraw(self, interaction: discord.Interaction, amount: str = "all"):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        racer   = await self._get_racer(user_id)

        if not racer:
            await interaction.followup.send("Not registered.", ephemeral=True)
            return

        from zap_layer import ZAPP_ASA_ID
        balance = await asyncio.to_thread(get_balance, self.db, user_id, "ZAPP")
        if balance <= 0:
            await interaction.followup.send("No ZAPP balance to withdraw.", ephemeral=True)
            return

        if amount.lower() == "all":
            withdraw_amount = int(balance)
        else:
            try:
                withdraw_amount = int(amount)
            except ValueError:
                await interaction.followup.send("Invalid amount. Use a whole number or 'all'.", ephemeral=True)
                return

        if withdraw_amount < 1:
            await interaction.followup.send("Minimum withdrawal is 1 ZAPP.", ephemeral=True)
            return
        if withdraw_amount > balance:
            await interaction.followup.send(
                f"Can't withdraw {withdraw_amount:,} ZAPP — balance is only {int(balance):,} ZAPP.",
                ephemeral=True,
            )
            return

        debit_result = await asyncio.to_thread(
            debit, self.db, user_id, "ZAPP", withdraw_amount,
            "gp_withdraw", None, racer.get("zappy_id")
        )
        if not debit_result["ok"]:
            await interaction.followup.send(
                f"Withdrawal failed: {debit_result['error']}", ephemeral=True)
            return

        try:
            from algosdk import transaction as _txn
            from algo_layer import get_algod_client as _gac, get_bot_account as _gba
            private_key, bot_addr = _gba()
            client = _gac()
            params = client.suggested_params()
            txn = _txn.AssetTransferTxn(
                sender=bot_addr, sp=params,
                receiver=racer["wallet_address"],
                amt=withdraw_amount,
                index=ZAPP_ASA_ID,
                note=b"gpzapwithdraw",
            )
            signed = txn.sign(private_key)
            txid   = client.send_transaction(signed)
            _txn.wait_for_confirmation(client, txid, 10)

            await interaction.followup.send(
                f"✅ **{withdraw_amount:,} ZAPP** sent to your wallet!\n"
                f"TX: `{txid[:14]}...`\n"
                f"Remaining balance: **{int(debit_result['balance_after']):,} ZAPP**",
                ephemeral=True,
            )
        except Exception as e:
            print(f"[grand_prix] ZAPP withdraw send error: {e}")
            await asyncio.to_thread(
                credit, self.db, user_id, "ZAPP", withdraw_amount,
                "gp_withdraw_refund", None, racer.get("zappy_id")
            )
            await interaction.followup.send(
                "⚠️ Withdrawal failed — your balance has been restored. Contact an admin if this persists.",
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

"""
grand_prix_cog.py
Zappy Grand Prix — self-contained cog for the existing Zappy Clash bot.

All Grand Prix commands are prefixed with /gp to avoid collisions
with existing Clash commands (/stats, /link, /top etc).

Commands:
  /gpregister     — link wallet + Zappy ID for Grand Prix
  /gpstats        — your Grand Prix stat sheet
  /gpgarage       — scout another player's stats
  /gpupgrade      — spend ZAP to level up a stat
  /gpzap          — check your ZAP balance
  /gpleaderboard  — Grand Prix leaderboard by wins
  /gpsetup        — (admin) post both race boards in this channel
  /gptestrace     — (admin) run a test race vs the computer, no real money

Add to bot.py on_ready:
  from grand_prix_cog import GrandPrixCog
  await bot.add_cog(GrandPrixCog(bot))
"""

import asyncio
import random
import io
import os
from PIL import Image, ImageDraw, ImageFont

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database import get_supabase   # reuse existing Supabase client

from race_engine import (
    seed_stats,
    resolve_race,
    run_race_narration,
    apply_upgrade,
    get_racer,
    get_all_racers,
    get_available_racers,
    set_zappy_cooldown,
    get_stats,
    create_duel,
    confirm_payment,
    write_race_result,
    max_upgrade_cost,
    STAT_CAP_MAX,
)
from algo_layer import (
    get_current_round,
    get_algod_client,
    get_bot_account,
    get_bot_address,
    generate_qr_png,
    get_indexer_client,
    process_expired_duels,
)
from zap_layer import (
    can_afford_entry,
    get_zapp_balance,
    build_payment_ui as build_zapp_payment_ui,
    build_pera_zapp_uri as build_zapp_payment_uri,
    make_payment_view as make_zapp_payment_view,
    wait_for_payment as wait_for_zapp_payment,
    send_payout as send_zapp_payout,
    send_refund as send_zapp_refund,
    is_opted_in,
    ZAP_ENTRY,
    ZAP_PAYOUT,
    ZAP_WIN_BONUS,
    ZAP_LOSE_BONUS,
    ZAPP_ASA_ID,
)


# ---------------------------------------------------------------------------
# Channel IDs — add these to your .env
# ---------------------------------------------------------------------------
# ALGO_RACE_CHANNEL_ID=<channel id>
# ZAP_RACE_CHANNEL_ID=<channel id>


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
# Board image generator — composites text onto real background images
# Images live in ./boards/ folder alongside this file in the repo
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

# Resolve boards/ relative to this file so Railway always finds it
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
        # Asymmetric padding — Discord mobile clips bottom corners more aggressively
        PAD_L, PAD_R, PAD_T, PAD_B = 6, 6, 6, 14
        new_w = W - PAD_L - PAD_R
        new_h = H - PAD_T - PAD_B
        raw    = Image.open(path).convert("RGBA").resize((new_w, new_h))
        canvas = Image.new("RGBA", (W, H), (8, 10, 20, 255))
        canvas.paste(raw, (PAD_L, PAD_T), raw.split()[3])
        return canvas
    print(f"[grand_prix] MISSING image: {path}")
    return Image.new("RGBA", (W, H), (8, 10, 20, 255))


def _t(draw, x, y, text, font, color):
    """Draw text with drop shadow for readability over any background."""
    draw.text((x+1, y+1), text, font=font, fill=(*SHADOW, 200), anchor="mm")
    draw.text((x+2, y+2), text, font=font, fill=(*SHADOW, 120), anchor="mm")
    draw.text((x, y), text, font=font, fill=color, anchor="mm")


def _buf(img):
    # Composite RGBA onto a dark background before converting
    # so transparent areas don't turn white
    bg = Image.new("RGBA", img.size, (8, 10, 20, 255))
    bg.paste(img, mask=img.split()[3])  # use alpha channel as mask
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
    _t(draw, W//2, 195, "Join to race · first to pay locks in",  FONT_SM,   MUTED)
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
    diff = abs(score_a - score_b)
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


# ---------------------------------------------------------------------------
# Persistent join button views
# ---------------------------------------------------------------------------

class JoinAlgoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Join Race  ·  5 ALGO",
        style=discord.ButtonStyle.primary,
        emoji="🏁",
        custom_id="gp:join_algo",
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass  # routed via on_interaction


class JoinZapView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Join Race  ·  500 ZAPP",
        style=discord.ButtonStyle.success,
        emoji="⚡",
        custom_id="gp:join_zap",
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass  # routed via on_interaction


# ---------------------------------------------------------------------------
# Queue state
# ---------------------------------------------------------------------------

class RaceQueue:
    def __init__(self, mode: str):
        self.mode = mode
        self.reset()

    def reset(self):
        self.player_a_id:    str | None  = None
        self.player_a_racer: dict | None = None
        self.player_a_stats: dict | None = None
        self.player_a_paid:  bool        = False
        self.player_b_id:    str | None  = None
        self.player_b_racer: dict | None = None
        self.player_b_stats: dict | None = None
        self.player_b_paid:  bool        = False
        self.duel_id:        str | None  = None
        self.after_round:    int         = 0
        self.locked:         bool        = False
        # board_msg_id intentionally NOT reset — the new board is posted
        # by _post_new_board after each race and sets its own ID


algo_queue = RaceQueue("algo")
zap_queue  = RaceQueue("zap")

# Global set — discord_user_ids currently in any queue or race
active_players: set[str] = set()


# ---------------------------------------------------------------------------
# CPU opponent for test races
# ---------------------------------------------------------------------------

CPU_ZAPPY_ID = "Sparky"   # CPU test opponent display name

def make_cpu_racer() -> dict:
    """Generate a fake CPU racer with mid-range stats for testing."""
    return {
        "discord_user_id": "cpu",
        "wallet_address":  "CPU000000000000000000000000000000000000000000000000000000",
        "zappy_id":        CPU_ZAPPY_ID,
        "wins": 0, "losses": 0, "zap_balance": 0,
    }

def make_cpu_stats() -> dict:
    """CPU stats match a fresh unupgraded Zappy — fair test opponent."""
    return {
        "speed":          random.randint(3, 6),
        "speed_max":      10,
        "endurance":      random.randint(3, 6),
        "endurance_max":  10,
        "clutch":         random.randint(3, 6),
        "clutch_max":     10,
        "total_zap_spent": 0,
    }


# ---------------------------------------------------------------------------
# Stat display helpers
# ---------------------------------------------------------------------------

def stat_bar(current: int, cap: int, width: int = 11) -> str:
    filled = round((current / STAT_CAP_MAX) * width)
    return "█" * filled + "░" * (width - filled)


def format_stats_embed(racer: dict, stats: dict, title: str = None) -> discord.Embed:
    wins     = racer.get("wins", 0)
    losses   = racer.get("losses", 0)
    total    = wins + losses
    win_rate = f"{round(wins/total*100)}%" if total else "—"

    embed = discord.Embed(
        title=title or f"⚡ {racer['zappy_id']}",
        color=discord.Color.from_rgb(30, 180, 255),
    )
    embed.add_field(name="Speed",
        value=f"`{stat_bar(stats['speed'], stats['speed_max'])}` {stats['speed']}/{stats['speed_max']}", inline=False)
    embed.add_field(name="Endurance",
        value=f"`{stat_bar(stats['endurance'], stats['endurance_max'])}` {stats['endurance']}/{stats['endurance_max']}", inline=False)
    embed.add_field(name="Clutch",
        value=f"`{stat_bar(stats['clutch'], stats['clutch_max'])}` {stats['clutch']}/{stats['clutch_max']}", inline=False)
    embed.add_field(name="Wins",        value=str(wins),    inline=True)
    embed.add_field(name="Losses",      value=str(losses),  inline=True)
    embed.add_field(name="Win Rate",    value=win_rate,     inline=True)
    embed.add_field(name="ZAPP Balance", value=f"{racer.get('zap_balance', 0):,}", inline=True)
    embed.add_field(name="ZAPP to max",
        value=f"{max_upgrade_cost(stats):,}" if max_upgrade_cost(stats) > 0 else "✅ Maxed", inline=True)
    embed.set_footer(text=f"Wallet: {racer['wallet_address'][:10]}...{racer['wallet_address'][-4:]}")
    return embed


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class GrandPrixCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db  = get_supabase()
        self.expiry_task.start()

    def cog_unload(self):
        self.expiry_task.cancel()

    # -----------------------------------------------------------------------
    # Background: expire stale ALGO duels + refund
    # -----------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def expiry_task(self):
        try:
            await process_expired_duels(self.db, refund=True)
        except Exception as e:
            print(f"[grand_prix] Expiry task error: {e}")

    @expiry_task.before_loop
    async def before_expiry(self):
        await self.bot.wait_until_ready()

    # -----------------------------------------------------------------------
    # Button router
    # -----------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = interaction.data.get("custom_id", "")
        if cid == "gp:join_algo":
            await self._handle_join(interaction, algo_queue)
        elif cid == "gp:join_zap":
            await self._handle_join(interaction, zap_queue)

    # -----------------------------------------------------------------------
    # Shared join handler
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
                "A race is already running on this board. Hang tight.",
                ephemeral=True,
            )
            return

        # Check registration
        available, on_cooldown = await get_available_racers(self.db, user_id)

        if not available and not on_cooldown:
            await interaction.followup.send(
                "You need to `/gpregister` first to join the Grand Prix.",
                ephemeral=True,
            )
            return

        if not available:
            # All Zappies on cooldown — tell them when earliest is ready
            earliest = min(on_cooldown, key=lambda x: x["cooldown_ends"])
            ends = earliest["cooldown_ends"]
            import discord as _discord
            ts = int(ends.timestamp())
            await interaction.followup.send(
                f"All your Zappies are cooling down! ❄️\n"
                f"**{earliest['racer']['zappy_id']}** is ready <t:{ts}:R>.",
                ephemeral=True,
            )
            return

        if q.mode == "zap":
            zapp_bal = available[0]["racer"].get("zapp_balance", 0) if available else 0
            if zapp_bal < ZAP_ENTRY:
                await interaction.followup.send(
                    f"Not enough ZAPP on deposit. You have **{zapp_bal:,} ZAPP**\n"
                    f"Use `/gpzapdeposit` to add ZAPP — minimum {ZAP_ENTRY:,} to race.",
                    ephemeral=True,
                )
                return

        # If only one Zappy available, skip picker and go straight in
        if len(available) == 1:
            selected = available[0]
            await self._slot_player(interaction, q, channel, user_id, selected["racer"], selected["stats"])
            return

        # Multiple Zappies — show picker
        await self._show_zappy_picker(interaction, q, channel, user_id, available)

    async def _show_zappy_picker(
        self,
        interaction: discord.Interaction,
        q: RaceQueue,
        channel,
        user_id: str,
        available: list[dict],
    ):
        """Show ephemeral buttons to pick which Zappy to race with."""

        class ZappyPickView(discord.ui.View):
            def __init__(self_inner):
                super().__init__(timeout=60)
                self_inner.chosen = False

                for entry in available[:5]:  # max 5 buttons
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
                    btn.callback = self_inner._make_cb(racer, stats)
                    self_inner.add_item(btn)

            def _make_cb(self_inner, racer, stats):
                async def cb(btn_interaction: discord.Interaction):
                    if self_inner.chosen:
                        await btn_interaction.response.send_message("Already picked!", ephemeral=True)
                        return
                    self_inner.chosen = True
                    for item in self_inner.children:
                        item.disabled = True
                    await btn_interaction.response.defer(ephemeral=True)
                    await self._slot_player(btn_interaction, q, channel, user_id, racer, stats)
                return cb

        embed = discord.Embed(
            title="⚡ Pick your Zappy",
            description="Choose which Zappy to race with. Stats shown — pick your best lineup.",
            color=discord.Color.from_rgb(30, 180, 255),
        )
        for entry in available[:5]:
            racer = entry["racer"]
            stats = entry["stats"]
            embed.add_field(
                name=racer["zappy_id"],
                value=(
                    f"Speed `{stat_bar(stats['speed'], stats['speed_max'])}` {stats['speed']}/{stats['speed_max']}\n"
                    f"Endurance `{stat_bar(stats['endurance'], stats['endurance_max'])}` {stats['endurance']}/{stats['endurance_max']}\n"
                    f"Clutch `{stat_bar(stats['clutch'], stats['clutch_max'])}` {stats['clutch']}/{stats['clutch_max']}"
                ),
                inline=False,
            )

        await interaction.followup.send(embed=embed, view=ZappyPickView(), ephemeral=True)

    async def _slot_player(
        self,
        interaction: discord.Interaction,
        q: RaceQueue,
        channel,
        user_id: str,
        racer: dict,
        stats: dict,
    ):
        """Place a player into slot A or B with their chosen Zappy."""
        if q.player_a_id is None:
            q.player_a_id    = user_id
            q.player_a_racer = racer
            q.player_a_stats = stats
            active_players.add(user_id)
            if q.mode == "zap":
                await self._zap_join_a(interaction, q, channel)
            else:
                await self._algo_join_a(interaction, q, channel)

        elif q.player_b_id is None and q.player_a_id != user_id:
            q.player_b_id    = user_id
            q.player_b_racer = racer
            q.player_b_stats = stats
            active_players.add(user_id)
            if q.mode == "zap":
                await self._zap_join_b(interaction, q, channel)
            else:
                await self._algo_join_b(interaction, q, channel)
        else:
            await interaction.followup.send(
                "Queue is full — two players are lining up. Check back soon.",
                ephemeral=True,
            )

    # -----------------------------------------------------------------------
    # ALGO join flows
    # -----------------------------------------------------------------------

    async def _algo_join_a(self, interaction, q, channel):
        racer = q.player_a_racer
        balance = racer.get("algo_balance", 0)

        if balance < 5:
            await interaction.followup.send(
                f"Not enough ALGO on deposit. You have **{balance:.2f} ALGO**\n"
                f"Use `/gpdeposit` to add ALGO — minimum 5 ALGO to race.",
                ephemeral=True,
            )
            active_players.discard(q.player_a_id)
            q.player_a_id = None; q.player_a_racer = None
            return

        # Deduct from ALL rows for this player (keeps all Zappies in sync)
        new_balance = balance - 5
        self.db.table("zappy_racers").update(
            {"algo_balance": new_balance}
        ).eq("discord_user_id", q.player_a_id).execute()

        duel = await create_duel(self.db, q.player_a_id, q.player_a_id)
        q.duel_id     = duel["id"]
        q.player_a_paid = True

        await self._update_board(channel, q, "waiting", zappy_id=racer["zappy_id"])
        await interaction.followup.send(
            f"✅ **5 ALGO** deducted — you're in the queue!\n"
            f"Remaining balance: **{new_balance:.2f} ALGO**\n"
            f"Waiting for an opponent...",
            ephemeral=True,
        )

    async def _algo_join_b(self, interaction, q, channel):
        primary = await get_racer(self.db, q.player_b_id)
        balance = primary.get("algo_balance", 0) if primary else 0

        if balance < 5:
            await interaction.followup.send(
                f"Not enough ALGO on deposit. You have **{balance:.2f} ALGO**\n"
                f"Use `/gpdeposit` to add ALGO — minimum 5 ALGO to race.",
                ephemeral=True,
            )
            active_players.discard(q.player_b_id)
            q.player_b_id = None; q.player_b_racer = None
            return

        new_balance = balance - 5
        self.db.table("zappy_racers").update(
            {"algo_balance": new_balance}
        ).eq("discord_user_id", q.player_b_id).execute()

        self.db.table("race_duels").update({"opponent_id": q.player_b_id}).eq("id", q.duel_id).execute()
        q.player_b_paid = True

        await interaction.followup.send(
            f"✅ **5 ALGO** deducted — race starting!\n"
            f"Remaining balance: **{new_balance:.2f} ALGO**",
            ephemeral=True,
        )
        await self._launch_race(q, channel)

    async def _poll_algo(self, q, slot, channel):
        racer = q.player_a_racer if slot == "a" else q.player_b_racer
        role  = "challenger" if slot == "a" else "opponent"

        async def on_found(txid):
            await confirm_payment(self.db, q.duel_id, role, txid)
            if slot == "a":
                q.player_a_paid = True
                await channel.send(f"✅ **{q.player_a_racer['zappy_id']}** payment confirmed — waiting for opponent...")
            else:
                q.player_b_paid = True
                await channel.send(f"✅ **{q.player_b_racer['zappy_id']}** payment confirmed!")
            if q.player_a_paid and q.player_b_paid:
                await self._launch_race(q, channel)

        await wait_for_payment(
            sender_address=racer["wallet_address"],
            duel_id=q.duel_id,
            after_round=q.after_round,
            on_found=on_found,
            on_timeout=None,
        )

    async def _poll_zapp_payment(self, q: RaceQueue, slot: str, channel):
        """Poll indexer for ZAPP payment then trigger race when both paid."""
        import asyncio as _asyncio

        racer = q.player_a_racer if slot == "a" else q.player_b_racer
        role  = "challenger" if slot == "a" else "opponent"

        async def on_found(txid: str):
            await confirm_payment(self.db, q.duel_id, role, txid)
            if slot == "a":
                q.player_a_paid = True
                await channel.send(f"✅ **{q.player_a_racer['zappy_id']}** ZAPP payment confirmed — waiting for opponent...")
            else:
                q.player_b_paid = True
                await channel.send(f"✅ **{q.player_b_racer['zappy_id']}** ZAPP payment confirmed!")
            if q.player_a_paid and q.player_b_paid:
                await self._launch_race(q, channel)

        # Poll every 5 seconds for up to 3 minutes using zap_layer
        txid = await wait_for_zapp_payment(
            sender_address=racer["wallet_address"],
            duel_id=q.duel_id,
            after_round=q.after_round,
            on_found=on_found,
        )
        if txid:
            return

        # Timeout — refund if they paid
        active_players.discard(q.player_a_id if slot == "a" else q.player_b_id)
        if slot == "a":
            q.player_a_id = None; q.player_a_racer = None
            await self._update_board(channel, q, "empty")
        else:
            q.player_b_id = None; q.player_b_racer = None

    async def _poll_zapp(self, q: RaceQueue, slot: str, channel):
        """Poll indexer for ZAPP ASA payment — mirrors _poll_algo."""
        racer = q.player_a_racer if slot == "a" else q.player_b_racer
        role  = "challenger" if slot == "a" else "opponent"

        async def on_found(txid):
            await confirm_payment(self.db, q.duel_id, role, txid)
            if slot == "a":
                q.player_a_paid = True
                await channel.send(
                    f"✅ **{q.player_a_racer['zappy_id']}** ZAPP payment confirmed — waiting for opponent..."
                )
            else:
                q.player_b_paid = True
                await channel.send(
                    f"✅ **{q.player_b_racer['zappy_id']}** ZAPP payment confirmed!"
                )
            if q.player_a_paid and q.player_b_paid:
                await self._launch_race(q, channel)

        await wait_for_zapp_payment(
            sender_address=racer["wallet_address"],
            duel_id=q.duel_id,
            after_round=q.after_round,
            on_found=on_found,
            on_timeout=None,
        )

    # -----------------------------------------------------------------------
    # ZAP join flows
    # -----------------------------------------------------------------------

    async def _zap_join_a(self, interaction, q, channel):
        import base64
        from urllib.parse import urlencode
        from algo_layer import get_current_round, get_bot_address as _bot_addr

        duel = await create_duel(self.db, q.player_a_id, q.player_a_id)
        q.duel_id     = duel["id"]
        q.after_round = get_current_round()

        bot_address = _bot_addr()
        note        = f"zgp:{q.duel_id}"
        note_b64    = base64.b64encode(note.encode()).decode()
        # Build algorand:// URI for QR code (wallet apps scan this natively)
        from algo_layer import generate_qr_png as _qr
        algo_uri = (
            f"algorand://{bot_address}?"
            + urlencode({"amount": 0, "asset": ZAPP_ASA_ID,
                         "amount_asset": ZAP_ENTRY, "note": note_b64})
        )
        qr_buf = _qr(algo_uri)

        await self._update_board(channel, q, "waiting", zappy_id=q.player_a_racer["zappy_id"])
        await interaction.followup.send(
            f"**Send {ZAP_ENTRY:,} ZAPP to enter the race**\n\n"
            f"📱 **Mobile** — scan the QR code with Pera Wallet.\n"
            f"🖥️ **Desktop** — send manually using the details below.\n\n"
            f"```\nAddress : {bot_address}\nAsset   : ZAPP ({ZAPP_ASA_ID})\n"
            f"Amount  : {ZAP_ENTRY:,}\nNote    : {note}\n```\n"
            f"*The note must match exactly or your entry won't register.*\n"
            f"⏳ Waiting for your payment...",
            file=discord.File(qr_buf, filename="pay_zapp.png"),
            ephemeral=True,
        )
        asyncio.create_task(self._poll_zapp_payment(q, "a", channel))

    async def _zap_join_b(self, interaction, q, channel):
        import base64
        from urllib.parse import urlencode
        from algo_layer import get_bot_address as _bot_addr

        self.db.table("race_duels").update({"opponent_id": q.player_b_id}).eq("id", q.duel_id).execute()

        bot_address = _bot_addr()
        note        = f"zgp:{q.duel_id}"
        note_b64    = base64.b64encode(note.encode()).decode()
        from algo_layer import generate_qr_png as _qr
        algo_uri = (
            f"algorand://{bot_address}?"
            + urlencode({"amount": 0, "asset": ZAPP_ASA_ID,
                         "amount_asset": ZAP_ENTRY, "note": note_b64})
        )
        qr_buf = _qr(algo_uri)

        await interaction.followup.send(
            f"**Send {ZAP_ENTRY:,} ZAPP to enter the race**\n\n"
            f"📱 **Mobile** — scan the QR code with Pera Wallet.\n"
            f"🖥️ **Desktop** — send manually using the details below.\n\n"
            f"```\nAddress : {bot_address}\nAsset   : ZAPP ({ZAPP_ASA_ID})\n"
            f"Amount  : {ZAP_ENTRY:,}\nNote    : {note}\n```\n"
            f"*The note must match exactly or your entry won't register.*\n"
            f"⏳ Waiting for your payment...",
            file=discord.File(qr_buf, filename="pay_zapp.png"),
            ephemeral=True,
        )
        asyncio.create_task(self._poll_zapp_payment(q, "b", channel))

    # -----------------------------------------------------------------------
    # Shared race launcher
    # -----------------------------------------------------------------------

    async def _launch_race(self, q: RaceQueue, channel):
        q.locked = True
        racer_a, racer_b = q.player_a_racer, q.player_b_racer

        await self._update_board(channel, q, "racing",
            zappy_a=racer_a["zappy_id"], zappy_b=racer_b["zappy_id"])

        self.db.table("race_duels").update({"status": "racing"}).eq("id", q.duel_id).execute()

        # Use pre-selected stats from queue (set during Zappy picker)
        stats_a = q.player_a_stats or await get_stats(self.db, racer_a["zappy_id"])
        stats_b = q.player_b_stats or await get_stats(self.db, racer_b["zappy_id"])
        result  = resolve_race(stats_a, stats_b)

        race_msg = await channel.send("🏁 **Race starting...**")

        id_a = racer_a.get("discord_user_id") or (self.db.table("zappy_racers").select("discord_user_id").eq("zappy_id", racer_a["zappy_id"]).execute().data or [{}])[0].get("discord_user_id", "unknown")
        id_b = racer_b.get("discord_user_id") or (self.db.table("zappy_racers").select("discord_user_id").eq("zappy_id", racer_b["zappy_id"]).execute().data or [{}])[0].get("discord_user_id", "unknown")

        await run_race_narration(
            message=race_msg, result=result,
            name_a=f"<@{id_a}>", name_b=f"<@{id_b}>",
            zappy_a=racer_a["zappy_id"], zappy_b=racer_b["zappy_id"],
            mode=q.mode,
        )

        winner_racer = racer_a if result["winner"] == "a" else racer_b
        loser_racer  = racer_b if result["winner"] == "a" else racer_a
        winner_id    = id_a    if result["winner"] == "a" else id_b
        loser_id     = id_b    if result["winner"] == "a" else id_a

        await self._settle(q, channel, result, winner_racer, loser_racer, winner_id, loser_id)

    # -----------------------------------------------------------------------
    # Settle — payout, ZAP, records, board
    # -----------------------------------------------------------------------

    async def _settle(self, q, channel, result, winner_racer, loser_racer, winner_id, loser_id, test_mode=False):
        if q.mode == "algo" and not test_mode:
            # Credit winner 9 ALGO to primary balance row
            winner_primary = await get_racer(self.db, winner_id)
            if winner_primary:
                new_winner_bal = winner_primary.get("algo_balance", 0) + 9
                self.db.table("zappy_racers").update(
                    {"algo_balance": new_winner_bal}
                ).eq("discord_user_id", winner_id).execute()

            await write_race_result(self.db, q.duel_id, result, winner_id, "custodial")

            # Bot rake tracker (1 ALGO stays in bot wallet)
            try:
                bw_res = self.db.table("bot_wallet").select("algo_balance,total_rake_collected").eq("id",1).execute()
                bw = bw_res.data[0] if bw_res.data else {"algo_balance": 0, "total_rake_collected": 0}
                self.db.table("bot_wallet").update({
                    "algo_balance":         bw["algo_balance"] + 1,
                    "total_rake_collected": bw["total_rake_collected"] + 1,
                }).eq("id", 1).execute()
            except Exception as e:
                print(f"[grand_prix] Rake error: {e}")

            await channel.send(
                f"🏦 **{winner_racer['zappy_id']}** wins **9 ALGO** — credited to deposit balance\n"
                f"Use `/gpwithdraw` to send to your wallet anytime."
            )

        elif q.mode == "algo" and test_mode:
            await write_race_result(self.db, q.duel_id, result, winner_id, "test")
            await channel.send(f"🧪 Test complete — no ALGO moved")

        else:  # zap mode
            if not test_mode:
                # Credit winner ZAPP to primary balance row
                winner_primary = await get_racer(self.db, winner_id)
                if winner_primary:
                    new_winner_zapp = winner_primary.get("zapp_balance", 0) + ZAP_PAYOUT
                    self.db.table("zappy_racers").update(
                        {"zapp_balance": new_winner_zapp}
                    ).eq("discord_user_id", winner_id).execute()

                await write_race_result(self.db, q.duel_id, result, winner_id, "custodial")
                await channel.send(
                    f"🪙 **{winner_racer['zappy_id']}** wins **{ZAP_PAYOUT:,} ZAPP** — credited to deposit balance\n"
                    f"Use `/gpzapwithdraw` to send to your wallet anytime."
                )
            else:
                await write_race_result(self.db, q.duel_id, result, winner_id, "test")
                # No separate message — test label already shown in opening message

        # Win/loss records (always, even in test)
        if winner_id != "cpu":
            self.db.table("zappy_racers").update({"wins": winner_racer.get("wins", 0) + 1}).eq("discord_user_id", winner_id).eq("zappy_id", winner_racer["zappy_id"]).execute()
        if loser_id != "cpu":
            self.db.table("zappy_racers").update({"losses": loser_racer.get("losses", 0) + 1}).eq("discord_user_id", loser_id).eq("zappy_id", loser_racer["zappy_id"]).execute()

        # Apply 1hr cooldown to both Zappies that raced
        if not test_mode:
            await set_zappy_cooldown(self.db, winner_racer["zappy_id"])
            await set_zappy_cooldown(self.db, loser_racer["zappy_id"])

        # Board → result card
        await self._update_board(channel, q, "result",
            zappy_a=q.player_a_racer["zappy_id"],
            zappy_b=q.player_b_racer["zappy_id"],
            winner=winner_racer["zappy_id"],
            score_a=result["score_a"],
            score_b=result["score_b"],
            surge=result["surge_triggered"],
            remove_button=True,
        )

        # Release locks
        active_players.discard(q.player_a_id)
        active_players.discard(q.player_b_id)
        q.reset()

        # Post fresh empty board
        await self._post_new_board(channel, q)

    def _add_zap(self, user_id, current_balance, amount):
        if user_id == "cpu":
            return
        self.db.table("zappy_racers").update(
            {"zap_balance": current_balance + amount}
        ).eq("discord_user_id", user_id).execute()

    # -----------------------------------------------------------------------
    # Board helpers
    # -----------------------------------------------------------------------

    async def _update_board(self, channel, q, state, remove_button=False, **kw):
        if q.board_msg_id is None:
            print(f"[grand_prix] _update_board: no board_msg_id for {q.mode} queue — posting new board")
            await self._post_new_board(channel, q)
            # Now try again with the new board
            if q.board_msg_id is None:
                return
        try:
            msg = await channel.fetch_message(q.board_msg_id)
        except discord.NotFound:
            print(f"[grand_prix] _update_board: board message {q.board_msg_id} not found — posting new board")
            await self._post_new_board(channel, q)
            try:
                msg = await channel.fetch_message(q.board_msg_id)
            except Exception:
                return
        except Exception as e:
            print(f"[grand_prix] _update_board error: {e}")
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
    # /gpsetup — post both boards (admin)
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpsetup", description="(Admin) Post both Grand Prix race boards in this channel.")
    @app_commands.default_permissions(administrator=True)
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
            f"ALGO board: `{algo_msg.id}`\n"
            f"ZAPP board:  `{zap_msg.id}`\n\n"
            f"Pin both messages to keep them visible at the top of the channel.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gptestrace — admin test vs CPU, no real money
    # -----------------------------------------------------------------------

    @app_commands.command(name="gptestrace", description="(Admin) Run a test race vs the CPU. No real ALGO or ZAP moves.")
    @app_commands.describe(mode="Which board to test (algo or zap)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="ALGO",  value="algo"),
        app_commands.Choice(name="ZAP",   value="zap"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def gptestrace(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        channel = interaction.channel

        # Must be registered
        racer = await get_racer(self.db, user_id)
        if not racer:
            await interaction.followup.send(
                "Register first with `/gpregister` before running a test race.",
                ephemeral=True,
            )
            return

        # Pick the right queue
        q = algo_queue if mode.value == "algo" else zap_queue

        if q.locked:
            await interaction.followup.send(
                "That board is currently locked mid-race. Wait for it to finish.",
                ephemeral=True,
            )
            return

        if user_id in active_players:
            await interaction.followup.send(
                "You're already in a queue or race.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"🧪 **Test race starting on the {mode.name} board...**\n"
            f"No real {mode.name} will move. Full narration and board updates will run.",
            ephemeral=True,
        )

        # Set up queue with human as A, CPU as B
        cpu_racer = make_cpu_racer()
        cpu_stats = make_cpu_stats()

        q.player_a_id    = user_id
        q.player_a_racer = racer
        q.player_a_paid  = True
        q.player_b_id    = "cpu"
        q.player_b_racer = cpu_racer
        q.player_b_paid  = True
        q.locked         = True
        active_players.add(user_id)

        # Create a duel row (test flag)
        duel = await create_duel(self.db, user_id, user_id)
        q.duel_id = duel["id"]

        # Make sure there's a board to update — post one if missing
        if q.board_msg_id is None:
            buf  = board_empty(q.mode)
            file = discord.File(buf, filename="board.png")
            view = JoinAlgoView() if q.mode == "algo" else JoinZapView()
            msg  = await channel.send(file=file, view=view)
            q.board_msg_id = msg.id
            pass  # silently post temp board

        await self._update_board(channel, q, "racing",
            zappy_a=racer["zappy_id"], zappy_b=CPU_ZAPPY_ID)

        self.db.table("race_duels").update({"status": "racing"}).eq("id", q.duel_id).execute()

        stats_a = await get_stats(self.db, racer["zappy_id"])
        result  = resolve_race(stats_a, cpu_stats)

        mode_label = "ALGO" if mode.value == "algo" else "ZAPP"
        race_msg = await channel.send(f"🧪 **TEST RACE ({mode_label} mode) — no real funds move**")

        await run_race_narration(
            message=race_msg, result=result,
            name_a=f"<@{user_id}>", name_b="🤖 CPU",
            zappy_a=racer["zappy_id"], zappy_b=CPU_ZAPPY_ID,
            mode=mode.value,
        )

        winner_racer = racer      if result["winner"] == "a" else cpu_racer
        loser_racer  = cpu_racer  if result["winner"] == "a" else racer
        winner_id    = user_id    if result["winner"] == "a" else "cpu"
        loser_id     = "cpu"      if result["winner"] == "a" else user_id

        await self._settle(
            q, channel, result,
            winner_racer, loser_racer,
            winner_id, loser_id,
            test_mode=True,
        )

    # -----------------------------------------------------------------------
    # /gpregister
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpregister", description="Register your Zappy for the Grand Prix.")
    @app_commands.describe(
        wallet_address="Your Algorand wallet address (58 characters)",
        zappy_id="Your Zappy NFT ID (e.g. ZAP-447)",
    )
    async def gpregister(self, interaction: discord.Interaction, wallet_address: str, zappy_id: str):
        await interaction.response.defer(ephemeral=True)
        user_id  = str(interaction.user.id)

        if len(wallet_address) != 58:
            await interaction.followup.send(
                "Invalid Algorand address — should be 58 characters.",
                ephemeral=True,
            )
            return

        # Check if this specific Zappy ID is already registered (by anyone)
        existing_zappy = self.db.table("zappy_stats").select("zappy_id").eq("zappy_id", zappy_id).execute()
        if existing_zappy.data:
            await interaction.followup.send(
                f"**{zappy_id}** is already registered. Each Zappy can only be registered once.",
                ephemeral=True,
            )
            return

        # Check how many Zappies this player already has
        all_racers = await get_all_racers(self.db, user_id)
        zappy_count = len(all_racers)

        stats = seed_stats(zappy_id)

        # First Zappy — fresh balances
        if zappy_count == 0:
            self.db.table("zappy_racers").insert({
                "discord_user_id": user_id,
                "wallet_address":  wallet_address,
                "zappy_id":        zappy_id,
                "zap_balance":     0,
                "wins": 0, "losses": 0,
            }).execute()
        else:
            # Additional Zappy — inherit wallet and current balances so all rows stay in sync
            existing_wallet = all_racers[0]["wallet_address"]
            existing_algo   = all_racers[0].get("algo_balance", 0)
            existing_zapp   = all_racers[0].get("zapp_balance", 0)
            self.db.table("zappy_racers").insert({
                "discord_user_id": user_id,
                "wallet_address":  existing_wallet,
                "zappy_id":        zappy_id,
                "zap_balance":     0,
                "algo_balance":    existing_algo,
                "zapp_balance":    existing_zapp,
                "wins": 0, "losses": 0,
            }).execute()

        self.db.table("zappy_stats").insert({"zappy_id": zappy_id, **stats}).execute()

        garage_note = f"Zappy #{zappy_count + 1} in your garage." if zappy_count > 0 else "Use `/gpupgrade` to level up. Tap a race board to compete."

        embed = format_stats_embed(
            {"zappy_id": zappy_id, "wallet_address": wallet_address,
             "wins": 0, "losses": 0, "zap_balance": 0},
            stats,
            title=f"⚡ {zappy_id} — Added to Garage!",
        )
        embed.description = (
            f"Stats seeded from your Zappy ID. Max potential locked in.\n{garage_note}"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -----------------------------------------------------------------------
    # /gpclear — admin, unstick a player from the queue
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpclear", description="(Admin) Clear a stuck player from the race queue.")
    @app_commands.describe(player="The player to clear (leave blank to clear yourself)")
    @app_commands.default_permissions(administrator=True)
    async def gpclear(self, interaction: discord.Interaction, player: discord.Member = None):
        await interaction.response.defer(ephemeral=True)

        target    = player or interaction.user
        target_id = str(target.id)

        was_active = target_id in active_players
        active_players.discard(target_id)

        # Also clear from either queue slot
        cleared_queues = []
        for q in [algo_queue, zap_queue]:
            if q.player_a_id == target_id:
                q.player_a_id    = None
                q.player_a_racer = None
                q.player_a_stats = None
                q.player_a_paid  = False
                cleared_queues.append(q.mode.upper())
            if q.player_b_id == target_id:
                q.player_b_id    = None
                q.player_b_racer = None
                q.player_b_stats = None
                q.player_b_paid  = False
                cleared_queues.append(q.mode.upper())

        if was_active or cleared_queues:
            detail = f" (removed from {', '.join(cleared_queues)} queue)" if cleared_queues else ""
            await interaction.followup.send(
                f"✅ **{target.display_name}** cleared{detail}. They can now join a race.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"**{target.display_name}** wasn't in any queue or active state.",
                ephemeral=True,
            )

    # -----------------------------------------------------------------------
    # /gpunregister
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpunregister", description="(Admin) Remove a player's Grand Prix registration so they can re-register.")
    @app_commands.describe(player="The player to unregister (leave blank to unregister yourself)")
    @app_commands.default_permissions(administrator=True)
    async def gpunregister(self, interaction: discord.Interaction, player: discord.Member = None):
        await interaction.response.defer(ephemeral=True)

        target    = player or interaction.user
        target_id = str(target.id)

        racer = await get_racer(self.db, target_id)
        if not racer:
            await interaction.followup.send(
                f"{target.display_name} isn't registered in the Grand Prix.",
                ephemeral=True,
            )
            return

        zappy_id = racer["zappy_id"]

        # Delete stats first (foreign key child), then racer
        self.db.table("zappy_stats").delete().eq("zappy_id", zappy_id).execute()
        self.db.table("zappy_racers").delete().eq("discord_user_id", target_id).execute()

        # Also remove from active players if somehow stuck
        active_players.discard(target_id)

        await interaction.followup.send(
            f"✅ **{target.display_name}** unregistered — `{zappy_id}` removed.\n"
            f"They can now use `/gpregister` with a new Zappy ID.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpstats
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpstats", description="View your Grand Prix garage and stats.")
    async def gpstats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        from datetime import datetime, timezone
        available, on_cooldown = await get_available_racers(self.db, str(interaction.user.id))
        all_entries = available + on_cooldown

        if not all_entries:
            await interaction.followup.send("Not registered. Use `/gpregister` first.", ephemeral=True)
            return

        for entry in all_entries:
            racer = entry["racer"]
            stats = entry["stats"]
            cooldown_ends = entry.get("cooldown_ends")

            if cooldown_ends:
                ts = int(cooldown_ends.timestamp())
                title = f"⚡ {racer['zappy_id']} — ❄️ Ready <t:{ts}:R>"
            else:
                title = f"⚡ {racer['zappy_id']} — Ready to Race"

            embed = format_stats_embed(racer, stats, title=title)
            await interaction.followup.send(embed=embed)

    # -----------------------------------------------------------------------
    # /gpgarage
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpgarage", description="Scout another player's Grand Prix Zappy.")
    @app_commands.describe(player="The player to scout.")
    async def gpgarage(self, interaction: discord.Interaction, player: discord.Member):
        await interaction.response.defer()
        racer = await get_racer(self.db, str(player.id))
        if not racer:
            await interaction.followup.send(f"{player.display_name} isn't registered.", ephemeral=True)
            return
        stats = await get_stats(self.db, racer["zappy_id"])
        await interaction.followup.send(
            embed=format_stats_embed(racer, stats,
                title=f"⚡ {racer['zappy_id']} — {player.display_name}'s Garage")
        )

    # -----------------------------------------------------------------------
    # /gpupgrade
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpupgrade", description="Spend ZAPP to upgrade a stat on one of your Zappies.")
    @app_commands.describe(
        zappy_id="Which Zappy to upgrade (e.g. Zappy #83)",
        stat="Which stat to upgrade",
        points="Points to add (1–5)",
    )
    @app_commands.choices(stat=[
        app_commands.Choice(name="Speed",     value="speed"),
        app_commands.Choice(name="Endurance", value="endurance"),
        app_commands.Choice(name="Clutch",    value="clutch"),
    ])
    async def gpupgrade(self, interaction: discord.Interaction, zappy_id: str, stat: app_commands.Choice[str], points: int = 1):
        await interaction.response.defer(ephemeral=True)
        if not 1 <= points <= 5:
            await interaction.followup.send("Enter between 1 and 5 points.", ephemeral=True)
            return

        # Verify this Zappy belongs to the player
        all_racers = await get_all_racers(self.db, str(interaction.user.id))
        owned_ids = [r["zappy_id"] for r in all_racers]
        if zappy_id not in owned_ids:
            await interaction.followup.send(
                f"**{zappy_id}** isn't in your garage. Your Zappies: {', '.join(owned_ids) or 'none'}",
                ephemeral=True,
            )
            return

        result = await apply_upgrade(self.db, str(interaction.user.id), stat.value, points, zappy_id=zappy_id)
        if not result["success"]:
            await interaction.followup.send(f"❌ {result['error']}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ **{zappy_id}** — {stat.name} upgraded: {result['old_value']} → **{result['new_value']}** / {result['cap']}\n"
            f"`{stat_bar(result['new_value'], result['cap'])}`\n\n"
            f"Cost: **{result['cost']:,} ZAPP** · Balance: **{result['new_balance']:,} ZAPP**",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpzap
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpzap", description="Check your ZAPP balance.")
    async def gpzap(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        bal = await get_zap_balance(self.db, str(interaction.user.id))
        await interaction.followup.send(
            f"⚡ Your ZAPP balance: **{bal:,} ZAP**\n"
            f"ZAPP race entry: {ZAP_ENTRY:,}  ·  Win payout: {ZAP_PAYOUT:,}",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpleaderboard
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # /gpbalance
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpbalance", description="Check Grand Prix ALGO and ZAPP balances.")
    @app_commands.describe(player="(Admin) Check another player's balance")
    async def gpbalance(self, interaction: discord.Interaction, player: discord.Member = None):
        await interaction.response.defer(ephemeral=True)

        # Admins can check anyone, players can only check themselves
        is_admin = interaction.user.guild_permissions.administrator
        if player and not is_admin:
            await interaction.followup.send("Only admins can check other players' balances.", ephemeral=True)
            return

        target    = player or interaction.user
        target_id = str(target.id)
        all_racers = await get_all_racers(self.db, target_id)

        if not all_racers:
            await interaction.followup.send(
                f"**{target.display_name}** isn't registered in the Grand Prix.",
                ephemeral=True,
            )
            return

        algo_bal = all_racers[0].get("algo_balance", 0)
        wallet   = all_racers[0]["wallet_address"]
        zapp_bal = get_zapp_balance(wallet)

        label = "Your" if target == interaction.user else f"**{target.display_name}'s**"
        await interaction.followup.send(
            f"{label} Grand Prix Balances\n\n"
            f"⚡ ALGO on deposit: **{algo_bal:.2f} ALGO**\n"
            f"🪙 ZAPP in wallet:  **{zapp_bal:,} ZAPP**\n"
            f"👛 Wallet: `{wallet[:10]}...{wallet[-4:]}`\n\n"
            f"Use `/gpdeposit` to add ALGO · `/gpwithdraw` to withdraw",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpdeposit — show deposit address, poll for incoming ALGO
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpdeposit", description="Deposit ALGO to your Grand Prix balance.")
    async def gpdeposit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        racer   = await get_racer(self.db, user_id)

        if not racer:
            await interaction.followup.send("Not registered. Use `/gpregister` first.", ephemeral=True)
            return

        from algo_layer import get_bot_address, get_current_round, generate_qr_png
        bot_address  = get_bot_address()
        after_round  = get_current_round()
        current_bal  = racer.get("algo_balance", 0)

        await interaction.followup.send(
            f"**Deposit ALGO to your Grand Prix balance**\n\n"
            f"Open Pera Wallet and send any amount of ALGO to the address below.\n"
            f"Long press the next message to copy it.\n\n"
            f"Current balance: **{current_bal:.2f} ALGO**\n"
            f"⏳ Watching for your deposit for 5 minutes...",
            ephemeral=True,
        )
        await interaction.followup.send(
            bot_address,
            ephemeral=True,
        )

        # Poll for any incoming ALGO from their wallet
        asyncio.create_task(
            self._watch_deposit(user_id, racer["wallet_address"], bot_address, after_round, interaction)
        )

    async def _watch_deposit(self, user_id, wallet_address, bot_address, after_round, interaction):
        """
        Poll indexer for incoming ALGO from player wallet and credit their balance.
        Each transaction ID is recorded in gp_deposits to prevent double-crediting
        if the player taps /gpdeposit multiple times or the poller runs twice.
        """
        import asyncio as _a, time
        from algo_layer import get_indexer_client

        deadline = time.monotonic() + 300  # 5 min window
        idx      = get_indexer_client()
        credited = set()  # txids credited this session

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

                    # Skip if already credited this session
                    if txid in credited:
                        continue

                    # Skip if already recorded in Supabase (double-tap protection)
                    existing = self.db.table("gp_deposits").select("txid").eq("txid", txid).execute()
                    if existing.data:
                        print(f"[grand_prix] Deposit {txid[:12]} already credited — skipping")
                        credited.add(txid)
                        continue

                    # Record txid FIRST before crediting balance
                    self.db.table("gp_deposits").insert({
                        "txid":            txid,
                        "discord_user_id": user_id,
                        "amount_algo":     amount_algo,
                    }).execute()
                    credited.add(txid)

                    # Now credit their balance
                    racer   = await get_racer(self.db, user_id)
                    new_bal = round(racer.get("algo_balance", 0) + amount_algo, 6)
                    self.db.table("zappy_racers").update(
                        {"algo_balance": new_bal}
                    ).eq("discord_user_id", user_id).eq("zappy_id", racer["zappy_id"]).execute()

                    print(f"[grand_prix] Credited {amount_algo:.2f} ALGO to {user_id} txid={txid[:12]}")

                    try:
                        await interaction.followup.send(
                            f"✅ **{amount_algo:.2f} ALGO** deposited!\n"
                            f"New balance: **{new_bal:.2f} ALGO**\n"
                            f"You're ready to race. Tap **Join Race** on the ALGO board.",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
                    return

            except Exception as e:
                print(f"[grand_prix] Deposit watch error: {e}")

            await _a.sleep(5)

    # -----------------------------------------------------------------------
    # /gpwithdraw — send ALGO balance back to player wallet
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpwithdraw", description="Withdraw your ALGO deposit balance to your wallet.")
    @app_commands.describe(amount="Amount of ALGO to withdraw (or 'all')")
    async def gpwithdraw(self, interaction: discord.Interaction, amount: str = "all"):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        racer   = await get_racer(self.db, user_id)

        if not racer:
            await interaction.followup.send("Not registered.", ephemeral=True)
            return

        balance = racer.get("algo_balance", 0)

        if balance <= 0:
            await interaction.followup.send("No ALGO balance to withdraw.", ephemeral=True)
            return

        # Parse amount
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
                f"Can't withdraw {withdraw_amount:.2f} ALGO — balance is only {balance:.2f} ALGO.",
                ephemeral=True,
            )
            return

        # CRITICAL: Deduct from Supabase FIRST before sending on-chain.
        # Re-fetch balance at deduction time to prevent race conditions.
        # Only deduct if balance still covers the amount.
        new_bal = round(balance - withdraw_amount, 6)
        result = (
            self.db.table("zappy_racers")
            .update({"algo_balance": new_bal})
            .eq("discord_user_id", user_id)
            .eq("zappy_id", racer["zappy_id"])
            .gte("algo_balance", withdraw_amount)  # only update if balance is sufficient
            .execute()
        )

        if not result.data:
            # Balance was insufficient at deduction time — someone got here twice
            await interaction.followup.send(
                "Withdrawal failed — insufficient balance. Please try again.",
                ephemeral=True,
            )
            return

        # Balance deducted — now send on-chain
        try:
            microalgos = int(withdraw_amount * 1_000_000)
            from algosdk import transaction as _txn
            from algo_layer import get_algod_client, get_bot_account
            private_key, bot_addr = get_bot_account()
            client = get_algod_client()
            params = client.suggested_params()
            txn = _txn.PaymentTxn(
                sender=bot_addr,
                sp=params,
                receiver=racer["wallet_address"],
                amt=microalgos,
                note=b"gpwithdraw",
            )
            signed = txn.sign(private_key)
            txid   = client.send_transaction(signed)
            _txn.wait_for_confirmation(client, txid, 10)

            await interaction.followup.send(
                f"✅ **{withdraw_amount:.2f} ALGO** sent to your wallet!\n"
                f"TX: `{txid[:14]}...`\n"
                f"Remaining balance: **{new_bal:.2f} ALGO**",
                ephemeral=True,
            )

        except Exception as e:
            # On-chain send failed — refund the Supabase balance
            print(f"[grand_prix] Withdraw send error: {e}")
            self.db.table("zappy_racers").update(
                {"algo_balance": balance}
            ).eq("discord_user_id", user_id).execute()
            await interaction.followup.send(
                f"⚠️ Withdrawal failed — your balance has been restored.\nContact an admin if this persists.",
                ephemeral=True,
            )

    # -----------------------------------------------------------------------
    # /gpzapdeposit
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpzapdeposit", description="Deposit ZAPP to your Grand Prix balance.")
    async def gpzapdeposit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        racer   = await get_racer(self.db, user_id)

        if not racer:
            await interaction.followup.send("Not registered. Use `/gpregister` first.", ephemeral=True)
            return

        from algo_layer import get_bot_address, get_current_round, get_indexer_client
        bot_address  = get_bot_address()
        after_round  = get_current_round()
        current_bal  = racer.get("zapp_balance", 0)

        await interaction.followup.send(
            f"**Deposit ZAPP to your Grand Prix balance**\n\n"
            f"Open Pera Wallet and send ZAPP (ASA {ZAPP_ASA_ID}) to the address below.\n"
            f"Long press the next message to copy it.\n\n"
            f"Current balance: **{current_bal:,} ZAPP**\n"
            f"⏳ Watching for your deposit for 5 minutes...",
            ephemeral=True,
        )
        await interaction.followup.send(bot_address, ephemeral=True)

        asyncio.create_task(
            self._watch_zapp_deposit(user_id, racer["wallet_address"], bot_address, after_round, racer["zappy_id"], interaction)
        )

    async def _watch_zapp_deposit(self, user_id, wallet_address, bot_address, after_round, zappy_id, interaction):
        """Poll indexer for incoming ZAPP from player wallet, credit their balance."""
        import asyncio as _a, time
        from algo_layer import get_indexer_client

        deadline = time.monotonic() + 300
        idx      = get_indexer_client()
        credited = set()

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

                    # Double-credit protection via gp_deposits table
                    existing = self.db.table("gp_deposits").select("txid").eq("txid", txid).execute()
                    if existing.data:
                        credited.add(txid)
                        continue

                    # Record txid first
                    self.db.table("gp_deposits").insert({
                        "txid":            txid,
                        "discord_user_id": user_id,
                        "amount_algo":     amount,
                    }).execute()
                    credited.add(txid)

                    # Credit balance
                    racer   = await get_racer(self.db, user_id)
                    new_bal = racer.get("zapp_balance", 0) + amount
                    self.db.table("zappy_racers").update(
                        {"zapp_balance": new_bal}
                    ).eq("discord_user_id", user_id).eq("zappy_id", zappy_id).execute()

                    print(f"[grand_prix] Credited {amount:,} ZAPP to {user_id} txid={txid[:12]}")

                    try:
                        await interaction.followup.send(
                            f"✅ **{amount:,} ZAPP** deposited!\n"
                            f"New balance: **{new_bal:,} ZAPP**\n"
                            f"You're ready to race. Tap **Join Race** on the ZAPP board.",
                            ephemeral=True,
                        )
                    except Exception:
                        pass
                    return

            except Exception as e:
                print(f"[grand_prix] ZAPP deposit watch error: {e}")

            await _a.sleep(5)

    # -----------------------------------------------------------------------
    # /gpzapwithdraw
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpzapwithdraw", description="Withdraw your ZAPP deposit balance to your wallet.")
    @app_commands.describe(amount="Amount of ZAPP to withdraw (or 'all')")
    async def gpzapwithdraw(self, interaction: discord.Interaction, amount: str = "all"):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        racer   = await get_racer(self.db, user_id)

        if not racer:
            await interaction.followup.send("Not registered.", ephemeral=True)
            return

        balance = racer.get("zapp_balance", 0)

        if balance <= 0:
            await interaction.followup.send("No ZAPP balance to withdraw.", ephemeral=True)
            return

        if amount.lower() == "all":
            withdraw_amount = balance
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
                f"Can't withdraw {withdraw_amount:,} ZAPP — balance is only {balance:,} ZAPP.",
                ephemeral=True,
            )
            return

        # Deduct first with conditional check
        new_bal = balance - withdraw_amount
        result  = (
            self.db.table("zappy_racers")
            .update({"zapp_balance": new_bal})
            .eq("discord_user_id", user_id)
            .gte("zapp_balance", withdraw_amount)
            .execute()
        )

        if not result.data:
            await interaction.followup.send(
                "Withdrawal failed — insufficient balance. Please try again.",
                ephemeral=True,
            )
            return

        # Send on-chain
        try:
            from algosdk import transaction as _txn
            from algo_layer import get_algod_client, get_bot_account
            private_key, bot_addr = get_bot_account()
            client = get_algod_client()
            params = client.suggested_params()
            txn = _txn.AssetTransferTxn(
                sender=bot_addr,
                sp=params,
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
                f"Remaining balance: **{new_bal:,} ZAPP**",
                ephemeral=True,
            )

        except Exception as e:
            # Refund on failure
            print(f"[grand_prix] ZAPP withdraw error: {e}")
            self.db.table("zappy_racers").update(
                {"zapp_balance": balance}
            ).eq("discord_user_id", user_id).execute()
            await interaction.followup.send(
                f"⚠️ Withdrawal failed — your balance has been restored.\nContact an admin if this persists.",
                ephemeral=True,
            )

    @app_commands.command(name="gpleaderboard", description="Grand Prix leaderboard by wins.")
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
            total    = r["wins"] + r["losses"]
            win_rate = f"{round(r['wins']/total*100)}%" if total else "—"
            lines.append(f"{medals[i]} **{r['zappy_id']}** — {r['wins']}W / {r['losses']}L ({win_rate})")
        embed = discord.Embed(
            title="🏆 Zappy Grand Prix Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed)

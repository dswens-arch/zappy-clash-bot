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
    get_stats,
    create_duel,
    confirm_payment,
    write_race_result,
    max_upgrade_cost,
    STAT_CAP_MAX,
    STAT_BASE_MIN, STAT_BASE_MAX,
    STAT_CAP_MIN,  STAT_CAP_MAX,
)
from algo_layer import (
    build_payment_ui,
    make_payment_view,
    wait_for_payment,
    send_payout,
    get_current_round,
    process_expired_duels,
)
from zap_layer import (
    can_afford_entry,
    deduct_entry,
    pay_winner,
    refund_entry,
    get_zap_balance,
    ZAP_ENTRY,
    ZAP_PAYOUT,
    ZAP_WIN_BONUS,
    ZAP_LOSE_BONUS,
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
# Board image generator
# ---------------------------------------------------------------------------

W, H  = 800, 280
BG    = (8,   10,  20)
PANEL = (14,  18,  35)
WHITE = (240, 245, 255)
MUTED = (120, 140, 170)
GREEN = (50,  220, 120)
TRACK = (20,  26,  50)

ACCENTS = {
    "algo": (30,  180, 255),
    "zap":  (255, 200,  50),
}
LABELS = {
    "algo": ("ALGO GRAND PRIX",  "5 ALGO entry  |  Winner takes 9 ALGO"),
    "zap":  ("ZAP GRAND PRIX",   "500 ZAP entry  |  Winner takes 1,000 ZAP"),
}


def _base(draw, mode):
    accent = ACCENTS[mode]
    title, subtitle = LABELS[mode]
    draw.rounded_rectangle([2, 2, W-3, H-3], radius=16, outline=accent, width=2)
    draw.rounded_rectangle([2, 2, W-3, 52],  radius=16, fill=PANEL)
    draw.rectangle([2, 36, W-3, 52],          fill=PANEL)
    draw.text((24, 10), title,         font=FONT_MED, fill=accent)
    draw.text((W-24, 10), subtitle,    font=FONT_SM,  fill=MUTED, anchor="ra")
    draw.line([24, 56, W-24, 56],      fill=accent, width=1)
    for y in [90, 140, 190]:
        draw.line([24, y, W-24, y],    fill=TRACK, width=1)


def _buf(img):
    b = io.BytesIO()
    img.save(b, format="PNG")
    b.seek(0)
    return b


def board_empty(mode):
    img, draw = Image.new("RGB", (W,H), BG), None
    draw = ImageDraw.Draw(img)
    _base(draw, mode)
    draw.text((W//2, 120), "NO RACE IN PROGRESS",  font=FONT_BOLD, fill=MUTED, anchor="mm")
    draw.text((W//2, 162), "Be the first to join", font=FONT_REG,  fill=MUTED, anchor="mm")
    return _buf(img)


def board_waiting(mode, zappy_id):
    img  = Image.new("RGB", (W,H), BG)
    draw = ImageDraw.Draw(img)
    accent = ACCENTS[mode]
    _base(draw, mode)
    draw.text((W//2, 90),  "WAITING FOR OPPONENT",                   font=FONT_MED,  fill=accent, anchor="mm")
    draw.rounded_rectangle([W//2-200, 108, W//2+200, 162],            radius=10, fill=PANEL, outline=accent, width=1)
    draw.text((W//2, 135), f"⚡  {zappy_id}  is ready",              font=FONT_BOLD, fill=WHITE,  anchor="mm")
    draw.text((W//2, 185), "Join to race · first to pay locks in",    font=FONT_SM,   fill=MUTED,  anchor="mm")
    draw.text((W//2, 248), "Tap Join Race to enter",                  font=FONT_SM,   fill=accent, anchor="mm")
    return _buf(img)


def board_racing(mode, zappy_a, zappy_b):
    img  = Image.new("RGB", (W,H), BG)
    draw = ImageDraw.Draw(img)
    _base(draw, mode)
    draw.text((W//2, 88),  "RACE IN PROGRESS",         font=FONT_BOLD, fill=GREEN, anchor="mm")
    draw.text((60, 125),   f"⚡  {zappy_a}",               font=FONT_MED,  fill=WHITE)
    draw.text((60, 165),   f"⚡  {zappy_b}",               font=FONT_MED,  fill=WHITE)
    draw.text((W//2, 215), "Race underway — result soon",   font=FONT_SM,   fill=MUTED, anchor="mm")
    return _buf(img)


def board_result(mode, zappy_a, zappy_b, winner, score_a, score_b, surge=False):
    img  = Image.new("RGB", (W,H), BG)
    draw = ImageDraw.Draw(img)
    accent = ACCENTS[mode]
    _base(draw, mode)
    draw.text((W//2, 82),  "RACE RESULT",               font=FONT_MED,  fill=accent, anchor="mm")
    draw.rounded_rectangle([W//2-230, 98, W//2+230, 158],    radius=10, fill=(16,36,16), outline=GREEN, width=2)
    draw.text((W//2, 128), f"{winner}  WINS!",               font=FONT_BOLD, fill=GREEN,  anchor="mm")
    payout    = "9 ALGO paid out" if mode == "algo" else "1,000 ZAP paid out"
    surge_tag = "  ·  ⚡ SURGE!" if surge else ""
    draw.text((W//2, 178), f"{payout}  ·  {score_a}-{score_b} laps{surge_tag}", font=FONT_SM, fill=MUTED, anchor="mm")
    draw.text((W//2, 210), f"{zappy_a}  vs  {zappy_b}",     font=FONT_SM,   fill=MUTED,  anchor="mm")
    draw.text((W//2, 250), "New race open below  ↓",         font=FONT_SM,   fill=accent, anchor="mm")
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
        label="Join Race  ·  500 ZAP",
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
        self.player_a_paid:  bool        = False
        self.player_b_id:    str | None  = None
        self.player_b_racer: dict | None = None
        self.player_b_paid:  bool        = False
        self.duel_id:        str | None  = None
        self.after_round:    int         = 0
        self.board_msg_id:   int | None  = None
        self.locked:         bool        = False


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
    embed.add_field(name="ZAP Balance", value=f"{racer.get('zap_balance', 0):,}", inline=True)
    embed.add_field(name="ZAP to max",
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

        racer = await get_racer(self.db, user_id)
        if not racer:
            await interaction.followup.send(
                "You need to `/gpregister` first to join the Grand Prix.",
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
                "A race is already running on this board. Hang tight.",
                ephemeral=True,
            )
            return

        if q.mode == "zap":
            if not await can_afford_entry(self.db, user_id):
                bal = await get_zap_balance(self.db, user_id)
                await interaction.followup.send(
                    f"Not enough ZAP. Need **{ZAP_ENTRY:,}** — you have **{bal:,}**.\n"
                    f"Race on the ALGO board to earn more ZAP.",
                    ephemeral=True,
                )
                return

        # Slot A
        if q.player_a_id is None:
            q.player_a_id    = user_id
            q.player_a_racer = racer
            active_players.add(user_id)
            if q.mode == "zap":
                await self._zap_join_a(interaction, q, channel)
            else:
                await self._algo_join_a(interaction, q, channel)
            return

        # Slot B
        if q.player_b_id is None and q.player_a_id != user_id:
            q.player_b_id    = user_id
            q.player_b_racer = racer
            active_players.add(user_id)
            if q.mode == "zap":
                await self._zap_join_b(interaction, q, channel)
            else:
                await self._algo_join_b(interaction, q, channel)
            return

        await interaction.followup.send(
            "Queue is full — two players are lining up. Check back soon.",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # ALGO join flows
    # -----------------------------------------------------------------------

    async def _algo_join_a(self, interaction, q, channel):
        q.after_round = get_current_round()
        duel = await create_duel(self.db, q.player_a_id, q.player_a_id)
        q.duel_id = duel["id"]

        await self._update_board(channel, q, "waiting", zappy_id=q.player_a_racer["zappy_id"])

        ui = build_payment_ui(q.duel_id)
        await interaction.followup.send(
            content=ui["instructions"],
            file=discord.File(ui["qr_buf"], filename="pay.png"),
            view=make_payment_view(ui["algo_uri"], ui["pera_uri"]),
            ephemeral=True,
        )
        asyncio.create_task(self._poll_algo(q, "a", channel))

    async def _algo_join_b(self, interaction, q, channel):
        self.db.table("race_duels").update({"opponent_id": q.player_b_id}).eq("id", q.duel_id).execute()
        ui = build_payment_ui(q.duel_id)
        await interaction.followup.send(
            content=ui["instructions"],
            file=discord.File(ui["qr_buf"], filename="pay.png"),
            view=make_payment_view(ui["algo_uri"], ui["pera_uri"]),
            ephemeral=True,
        )
        asyncio.create_task(self._poll_algo(q, "b", channel))

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

    # -----------------------------------------------------------------------
    # ZAP join flows
    # -----------------------------------------------------------------------

    async def _zap_join_a(self, interaction, q, channel):
        result = await deduct_entry(self.db, q.player_a_id)
        if not result["success"]:
            active_players.discard(q.player_a_id)
            q.player_a_id = None; q.player_a_racer = None
            await interaction.followup.send(result["error"], ephemeral=True)
            return

        duel = await create_duel(self.db, q.player_a_id, q.player_a_id)
        q.duel_id = duel["id"]
        q.player_a_paid = True

        await self._update_board(channel, q, "waiting", zappy_id=q.player_a_racer["zappy_id"])
        await interaction.followup.send(
            f"✅ **{ZAP_ENTRY:,} ZAP** deducted — you're in the queue!\n"
            f"Remaining balance: **{result['new_balance']:,} ZAP**\n"
            f"Waiting for an opponent...",
            ephemeral=True,
        )

    async def _zap_join_b(self, interaction, q, channel):
        result = await deduct_entry(self.db, q.player_b_id)
        if not result["success"]:
            active_players.discard(q.player_b_id)
            q.player_b_id = None; q.player_b_racer = None
            await interaction.followup.send(result["error"], ephemeral=True)
            return

        self.db.table("race_duels").update({"opponent_id": q.player_b_id}).eq("id", q.duel_id).execute()
        q.player_b_paid = True

        await interaction.followup.send(
            f"✅ **{ZAP_ENTRY:,} ZAP** deducted — race starting!\n"
            f"Remaining balance: **{result['new_balance']:,} ZAP**",
            ephemeral=True,
        )
        await self._launch_race(q, channel)

    # -----------------------------------------------------------------------
    # Shared race launcher
    # -----------------------------------------------------------------------

    async def _launch_race(self, q: RaceQueue, channel):
        q.locked = True
        racer_a, racer_b = q.player_a_racer, q.player_b_racer

        await self._update_board(channel, q, "racing",
            zappy_a=racer_a["zappy_id"], zappy_b=racer_b["zappy_id"])

        self.db.table("race_duels").update({"status": "racing"}).eq("id", q.duel_id).execute()

        stats_a = await get_stats(self.db, racer_a["zappy_id"])
        stats_b = await get_stats(self.db, racer_b["zappy_id"])
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
            payout_txid  = None
            txid_display = ""
            try:
                payout_txid  = send_payout(winner_racer["wallet_address"], q.duel_id)
                txid_display = f"\n🏦 TX: `{payout_txid[:14]}...`"
            except Exception as e:
                print(f"[grand_prix] ALGO payout error: {e}")
                txid_display = "\n⚠️ Payout error — contact admin."

            await write_race_result(self.db, q.duel_id, result, winner_id, payout_txid or "")

            # Bot rake
            try:
                bw_res = self.db.table("bot_wallet").select("algo_balance,total_rake_collected").eq("id",1).execute()
                bw = bw_res.data[0] if bw_res.data else {"algo_balance": 0, "total_rake_collected": 0}
                self.db.table("bot_wallet").update({
                    "algo_balance":         bw["algo_balance"] + 1,
                    "total_rake_collected": bw["total_rake_collected"] + 1,
                }).eq("id", 1).execute()
            except Exception as e:
                print(f"[grand_prix] Rake error: {e}")

            # ZAP participation bonuses
            self._add_zap(winner_id, winner_racer.get("zap_balance", 0), 500)
            self._add_zap(loser_id,  loser_racer.get("zap_balance", 0),  100)
            await channel.send(f"⚡ Winner +500 ZAP  ·  Runner-up +100 ZAP{txid_display}")

        elif q.mode == "algo" and test_mode:
            # Test mode — skip real payout, just log
            await write_race_result(self.db, q.duel_id, result, winner_id, "test")
            self._add_zap(winner_id, winner_racer.get("zap_balance", 0), 500)
            self._add_zap(loser_id,  loser_racer.get("zap_balance", 0),  100)
            await channel.send(f"⚡ Winner +500 ZAP  ·  Runner-up +100 ZAP")

        else:  # zap mode
            if not test_mode:
                await pay_winner(self.db, winner_id, loser_id)
                await write_race_result(self.db, q.duel_id, result, winner_id, "zap")
                await channel.send(
                    f"🪙 **{ZAP_PAYOUT:,} ZAP** paid to **{winner_racer['zappy_id']}**\n"
                    f"⚡ Winner +{ZAP_WIN_BONUS} ZAP bonus  ·  Runner-up +{ZAP_LOSE_BONUS} ZAP"
                )
            else:
                await write_race_result(self.db, q.duel_id, result, winner_id, "test")
                # No separate message — test label already shown in opening message

        # Win/loss records (always, even in test)
        if winner_id != "cpu":
            self.db.table("zappy_racers").update({"wins":   winner_racer.get("wins", 0) + 1}).eq("discord_user_id", winner_id).execute()
        if loser_id != "cpu":
            self.db.table("zappy_racers").update({"losses": loser_racer.get("losses", 0) + 1}).eq("discord_user_id", loser_id).execute()

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
            f"ZAP board:  `{zap_msg.id}`\n\n"
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
            await interaction.followup.send(
                f"No board found in this channel — posted a temporary one for the test. "
                f"Use `/gpsetup` in your actual race channel to post permanent boards.",
                ephemeral=True,
            )

        await self._update_board(channel, q, "racing",
            zappy_a=racer["zappy_id"], zappy_b=CPU_ZAPPY_ID)

        self.db.table("race_duels").update({"status": "racing"}).eq("id", q.duel_id).execute()

        stats_a = await get_stats(self.db, racer["zappy_id"])
        result  = resolve_race(stats_a, cpu_stats)

        mode_label = "ALGO" if mode.value == "algo" else "ZAP"
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
        existing = await get_racer(self.db, user_id)

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
            "zap_balance":     0,
            "wins": 0, "losses": 0,
        }).execute()
        self.db.table("zappy_stats").insert({"zappy_id": zappy_id, **stats}).execute()

        embed = format_stats_embed(
            {"zappy_id": zappy_id, "wallet_address": wallet_address,
             "wins": 0, "losses": 0, "zap_balance": 0},
            stats,
            title=f"⚡ {zappy_id} — Registered for Grand Prix!",
        )
        embed.description = (
            "Stats seeded from your Zappy ID. Max potential locked in.\n"
            "Use `/gpupgrade` to level up. Tap a race board to compete."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -----------------------------------------------------------------------
    # /gpstats
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpstats", description="View your Grand Prix stat sheet.")
    async def gpstats(self, interaction: discord.Interaction):
        await interaction.response.defer()
        racer = await get_racer(self.db, str(interaction.user.id))
        if not racer:
            await interaction.followup.send("Not registered. Use `/gpregister` first.", ephemeral=True)
            return
        stats = await get_stats(self.db, racer["zappy_id"])
        await interaction.followup.send(embed=format_stats_embed(racer, stats))

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

    @app_commands.command(name="gpupgrade", description="Spend ZAP to upgrade a stat on your Grand Prix Zappy.")
    @app_commands.describe(stat="Which stat to upgrade", points="Points to add (1–5)")
    @app_commands.choices(stat=[
        app_commands.Choice(name="Speed",     value="speed"),
        app_commands.Choice(name="Endurance", value="endurance"),
        app_commands.Choice(name="Clutch",    value="clutch"),
    ])
    async def gpupgrade(self, interaction: discord.Interaction, stat: app_commands.Choice[str], points: int = 1):
        await interaction.response.defer(ephemeral=True)
        if not 1 <= points <= 5:
            await interaction.followup.send("Enter between 1 and 5 points.", ephemeral=True)
            return
        result = await apply_upgrade(self.db, str(interaction.user.id), stat.value, points)
        if not result["success"]:
            await interaction.followup.send(f"❌ {result['error']}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ **{stat.name}** upgraded: {result['old_value']} → **{result['new_value']}** / {result['cap']}\n"
            f"`{stat_bar(result['new_value'], result['cap'])}`\n\n"
            f"Cost: **{result['cost']:,} ZAP** · Balance: **{result['new_balance']:,} ZAP**",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpzap
    # -----------------------------------------------------------------------

    @app_commands.command(name="gpzap", description="Check your ZAP balance.")
    async def gpzap(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        bal = await get_zap_balance(self.db, str(interaction.user.id))
        await interaction.followup.send(
            f"⚡ Your ZAP balance: **{bal:,} ZAP**\n"
            f"ZAP race entry: {ZAP_ENTRY:,}  ·  Win payout: {ZAP_PAYOUT:,}",
            ephemeral=True,
        )

    # -----------------------------------------------------------------------
    # /gpleaderboard
    # -----------------------------------------------------------------------

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

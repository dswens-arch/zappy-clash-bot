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
from algo_layer import (
    create_duel_record,
    confirm_payment,
    find_algo_payment_sync,
    pay_winner_algo,
    refund_algo,
    write_race_result,
    get_algo_balance,
)
from zap_layer import (
    can_afford_entry,
    deduct_entry,
    pay_winner as pay_winner_zap,
    get_zap_balance,
    ZAP_ENTRY,
    ZAP_PAYOUT,
    ZAP_WIN_BONUS,
    ZAP_LOSE_BONUS,
)
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
        self.db  = bot.supabase   # adjust if your attribute name differs
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
            if not await can_afford_entry(self.db, user_id):
                bal = await get_zap_balance(self.db, user_id)
                await interaction.followup.send(
                    f"Not enough ZAP. Need **{ZAP_ENTRY:,}** — you have **{bal:,}**.",
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
    # ALGO join flow
    # -----------------------------------------------------------------------

    async def _algo_join(self, interaction, q, channel, user_id, racer, slot):
        import algosdk
        from algo_layer import get_bot_wallet_address, get_current_round

        bot_address  = await get_bot_wallet_address(self.db)
        after_round  = await get_current_round()
        q.after_round = after_round

        if slot == "a":
            # Create duel record, post deposit instructions
            duel_id = await create_duel_record(
                self.db, user_id, racer["wallet_address"], racer["zappy_id"]
            )
            q.duel_id = duel_id
            await self._update_board(channel, q, "waiting_a", zappy_a=racer["zappy_id"])
            await interaction.followup.send(
                f"⚡ **Slot A locked — {racer['zappy_id']}**\n"
                f"Send exactly **5 ALGO** to:\n```\n{bot_address}\n```\n"
                f"Include your duel ID `{duel_id}` in the note field.\n"
                f"You have **3 minutes** to send payment.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚡ **Slot B locked — {racer['zappy_id']}**\n"
                f"Send exactly **5 ALGO** to:\n```\n{bot_address}\n```\n"
                f"Include duel ID `{q.duel_id}` in the note field.\n"
                f"You have **3 minutes**.",
                ephemeral=True,
            )
            await self._update_board(channel, q, "waiting_b",
                                     zappy_a=q.player_a_racer["zappy_id"],
                                     zappy_b=racer["zappy_id"])

        asyncio.create_task(
            self._poll_algo_payment(q, channel, user_id, racer, slot, after_round)
        )

    # -----------------------------------------------------------------------
    # ALGO payment poller  — WITH TIMEOUT CLEANUP
    # -----------------------------------------------------------------------

    async def _poll_algo_payment(self, q, channel, user_id, racer, slot, after_round):
        import time
        deadline = time.monotonic() + 180   # 3-minute window

        while time.monotonic() < deadline:
            txid = await asyncio.to_thread(
                find_algo_payment_sync,
                racer["wallet_address"],
                q.duel_id,
                after_round,
            )
            if txid:
                role = "challenger" if slot == "a" else "opponent"
                await confirm_payment(self.db, q.duel_id, role, txid)
                if slot == "a":
                    q.player_a_paid = True
                    await channel.send(
                        f"✅ **{racer['zappy_id']}** payment confirmed — waiting for opponent..."
                    )
                else:
                    q.player_b_paid = True
                    await channel.send(
                        f"✅ **{racer['zappy_id']}** payment confirmed!"
                    )
                if q.player_a_paid and q.player_b_paid:
                    await self._launch_race(q, channel)
                return
            await asyncio.sleep(5)

        # ---- TIMEOUT — clear the player and notify -------------------------
        # This is the fix for the silent stuck-queue state.
        timed_out_racer = racer["zappy_id"]
        self._clear_player(user_id, q)

        # If slot A timed out, the board goes back to empty.
        # If slot B timed out, slot A is still valid — board goes back to waiting_a.
        if slot == "a":
            other_id = q.player_b_id  # may already be None if B also timed out
            if other_id:
                self._clear_player(other_id, q)
            await self._update_board(channel, q, "empty")
            await channel.send(
                f"⏱ **Payment timeout** — **{timed_out_racer}** didn't send in time. "
                f"Board reset. Try again when ready."
            )
        else:
            # Slot A stays in the queue — board reverts to waiting_a
            await self._update_board(channel, q, "waiting_a",
                                     zappy_a=q.player_a_racer["zappy_id"] if q.player_a_racer else "?")
            await channel.send(
                f"⏱ **{timed_out_racer}** didn't send payment in time — slot B is open again."
            )

        # Try to notify the timed-out player directly
        try:
            member = channel.guild.get_member(int(user_id))
            if member:
                await member.send(
                    f"Your **Zappy Grand Prix** entry timed out — payment for "
                    f"**{timed_out_racer}** wasn't detected within 3 minutes. "
                    f"No ALGO was taken. Tap Join Race to try again."
                )
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # ZAP join flow  — instant, no polling needed
    # -----------------------------------------------------------------------

    async def _zap_join(self, interaction, q, channel, user_id, racer, slot):
        # Deduct entry fee immediately
        await deduct_entry(self.db, user_id)

        if slot == "a":
            duel_id = await create_duel_record(
                self.db, user_id, None, racer["zappy_id"], mode="zap"
            )
            q.duel_id = duel_id
            q.player_a_paid = True
            await self._update_board(channel, q, "waiting_a", zappy_a=racer["zappy_id"])
            await interaction.followup.send(
                f"⚡ **{ZAP_ENTRY:,} ZAP deducted — slot A locked for {racer['zappy_id']}**\n"
                f"Waiting for an opponent...",
                ephemeral=True,
            )
        else:
            q.player_b_paid = True
            await interaction.followup.send(
                f"⚡ **{ZAP_ENTRY:,} ZAP deducted — slot B locked for {racer['zappy_id']}**\n"
                f"Race starting now!",
                ephemeral=True,
            )
            await self._update_board(channel, q, "waiting_b",
                                     zappy_a=q.player_a_racer["zappy_id"],
                                     zappy_b=racer["zappy_id"])
            # Both paid instantly — launch
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
                await pay_winner_algo(self.db, winner_id, loser_id, q.duel_id)
                await write_race_result(self.db, q.duel_id, result, winner_id, "algo")
                await channel.send(
                    f"🏆 **{winner_racer['zappy_id']}** wins! **9 ALGO** sent to wallet.\n"
                    f"<@{winner_id}> | GG <@{loser_id}>"
                )
            else:
                balances = await pay_winner_zap(self.db, winner_id, loser_id)
                await write_race_result(self.db, q.duel_id, result, winner_id, "zap")
                await channel.send(
                    f"🏆 **{winner_racer['zappy_id']}** wins! "
                    f"**{ZAP_PAYOUT:,} ZAP** credited.\n"
                    f"<@{winner_id}> +{ZAP_WIN_BONUS} bonus  ·  <@{loser_id}> +{ZAP_LOSE_BONUS} consolation\n"
                    f"New balances — winner: **{balances['winner_balance']:,}** · "
                    f"runner-up: **{balances['loser_balance']:,}**"
                )

            # Update win/loss records
            self.db.table("zappy_racers").update({"wins": self.db.rpc("increment", {"row_id": winner_id, "field": "wins"})}).eq("discord_user_id", winner_id).execute()
            self.db.table("zappy_racers").update({"losses": self.db.rpc("increment", {"row_id": loser_id, "field": "losses"})}).eq("discord_user_id", loser_id).execute()

        except Exception as e:
            print(f"[grand_prix] settle error: {e}")
            await channel.send(
                f"⚠️ Race finished but payout hit an error — ping an admin. "
                f"Duel ID: `{q.duel_id}`"
            )
        finally:
            # Always clean up regardless of payout success
            self._clear_player(winner_id, q)
            self._clear_player(loser_id, q)
            q.reset()
            await asyncio.sleep(8)
            await self._update_board(channel, q, "empty")

    # -----------------------------------------------------------------------
    # Board image update
    # Swap in your Pillow compositing function here.
    # -----------------------------------------------------------------------

    async def _update_board(self, channel, q: RaceQueue, state: str,
                            zappy_a: str = "", zappy_b: str = ""):
        """
        state options: "empty" | "waiting_a" | "waiting_b" | "racing"
        Posts or edits the pinned board message.
        """
        mode_label = "ALGO" if q.mode == "algo" else "ZAP"
        entry_label = "5 ALGO" if q.mode == "algo" else f"{ZAP_ENTRY:,} ZAP"

        lines = {
            "empty":     f"⚡ **ZAPPY GRAND PRIX — {mode_label}**\n"
                         f"Entry: **{entry_label}**\n"
                         f"🟢 Slot A — open\n🟢 Slot B — open\n\n"
                         f"Tap **Join Race** to enter.",
            "waiting_a": f"⚡ **ZAPPY GRAND PRIX — {mode_label}**\n"
                         f"🟡 Slot A — **{zappy_a}** (waiting for payment)\n"
                         f"🟢 Slot B — open\n\n"
                         f"Tap **Join Race** to race them!",
            "waiting_b": f"⚡ **ZAPPY GRAND PRIX — {mode_label}**\n"
                         f"🟡 Slot A — **{zappy_a}**\n"
                         f"🟡 Slot B — **{zappy_b}**\n\n"
                         f"🏁 Both in — race starting soon...",
            "racing":    f"⚡ **ZAPPY GRAND PRIX — {mode_label}**\n"
                         f"🏎 **{zappy_a}** vs **{zappy_b}**\n\n"
                         f"🔴 Race in progress — join after this one.",
        }
        content = lines.get(state, lines["empty"])
        view = JoinAlgoView() if q.mode == "algo" else JoinZapView()

        try:
            if q.board_msg_id:
                msg = await channel.fetch_message(q.board_msg_id)
                await msg.edit(content=content, view=view)
            else:
                msg = await channel.send(content=content, view=view)
                q.board_msg_id = msg.id
        except discord.NotFound:
            # Board message was deleted — post a new one
            msg = await channel.send(content=content, view=view)
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
            "⚡ **ZAPPY GRAND PRIX — ALGO**\nEntry: **5 ALGO**\n"
            "🟢 Slot A — open\n🟢 Slot B — open\n\nTap **Join Race** to enter.",
            view=JoinAlgoView(),
        )
        zap_msg = await channel.send(
            f"⚡ **ZAPPY GRAND PRIX — ZAP**\nEntry: **{ZAP_ENTRY:,} ZAP**\n"
            "🟢 Slot A — open\n🟢 Slot B — open\n\nTap **Join Race** to enter.",
            view=JoinZapView(),
        )
        algo_queue.board_msg_id = algo_msg.id
        zap_queue.board_msg_id  = zap_msg.id

        await interaction.followup.send(
            f"✅ Both boards posted. Pin them and you're done.\n"
            f"ALGO board: {algo_msg.jump_url}\n"
            f"ZAP board: {zap_msg.jump_url}",
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

        # Clear both players if present
        for uid in [q.player_a_id, q.player_b_id]:
            if uid:
                self._clear_player(uid, q)
        q.reset()

        await self._update_board(interaction.channel, q, "empty")
        await interaction.followup.send(f"✅ {board.upper()} board reset to empty.", ephemeral=True)

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
            "zap_balance":     0,
            "algo_balance":    0,
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
        algo_bal = await get_algo_balance(self.db, user_id)
        zap_bal  = await get_zap_balance(self.db, user_id)
        await interaction.followup.send(
            f"**{racer['zappy_id']} — Balances**\n"
            f"ALGO: **{algo_bal / 1_000_000:.4f} ALGO**\n"
            f"ZAP:  **{zap_bal:,} ZAP**",
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

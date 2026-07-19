"""
spark_office.py
----------------
Tier-2 promotion system on top of Spark Jobs. A Spark that's gotten
genuinely lucky in base Jobs (not just grindy — see check_office_eligibility
in database.py) can be sponsored into one of 20 Office seats. Office Sparks
work the same 21 jobs, but at boosted odds/payouts, once a day, with an
active daily check-in requirement.

Promotion is luck-gated both ways: eligibility requires recent hits, not
shift volume, and demotion is a cold streak (consecutive misses), not
inactivity — showing up every day doesn't save a seat that's stopped
hitting. A separate no-show check handles the "you have to actually show
up" side of that.

When all 20 seats are full, a newly-eligible Spark can challenge the seat
with the lowest lifetime hit rate to an async best-of-7 rock/paper/scissors
duel — each side privately submits all 7 picks within an hour (unsubmitted
picks are auto-rolled), then the bot resolves and posts a dramatized
round-by-round reveal.

Env vars required:
  PROMOTION_CHANNEL_ID — where seatings, demotions, and duels get posted
Reuses everything else Spark Jobs already depends on (BOT_WALLET_MNEMONIC,
ALGOD_TOKEN, ALGOD_URL, INDEXER_URL, HOLDER_CHANNEL_ID for tier-ups).
"""

import os
import random
import asyncio
from datetime import datetime, timezone, timedelta, time as dtime

import discord
from discord.ext import commands, tasks
from discord import app_commands

from database import (
    get_wallet,
    get_spark,
    get_sparks_for_wallet,
    push_spark_arc19_upgrade,
    award_spark_job_xp,
    check_office_eligibility,
    get_office_seat_count,
    get_office_seat,
    get_office_seats,
    get_lowest_hitrate_seat,
    seat_spark,
    vacate_seat,
    create_office_shift,
    get_due_office_jobs,
    complete_office_job,
    get_seats_for_cold_streak_demotion,
    get_seats_for_noshow_demotion,
    get_seats_needing_reminder,
    mark_seat_reminded,
    get_unpaid_office_algo_jobs,
    mark_office_jobs_paid,
    create_office_payout,
    create_office_duel,
    get_pending_duel_for_spark,
    submit_duel_picks,
    get_expired_pending_duels,
    resolve_office_duel,
    get_office_seats_for_wallet,
    get_working_office_shift,
    get_working_office_shifts_map,
    get_all_office_candidates,
    set_office_shift_time,
    OFFICE_SEAT_CAP,
    OFFICE_SPONSOR_ZAPPY_COUNT,
    OFFICE_ALGO_HIT_CHANCE,
    OFFICE_NFT_HIT_CHANCE,
    OFFICE_PAYOUT_RANGE,
    OFFICE_MAX_SHIFT_PAYOUT,
    OFFICE_MIN_SHIFTS_FOR_DUEL,
    OFFICE_DUEL_SUBMIT_HOURS,
    OFFICE_NO_SHOW_GRACE_HOURS,
    OFFICE_DEMOTION_MISS_DAYS,
)

# Reuse the existing flavor-line banks and small helpers instead of
# duplicating them — Office jobs tell the same 21 stories, just with better
# odds attached.
from spark_jobs import (
    JOBS, JOB_NAMES, JOB_EMOJIS,
    _chunk_lines, _color_for_user, _build_flavor_line,
    TIER_NAMES,
)
from spark_admin import admin_check

PROMOTION_CHANNEL_ID = int(os.environ["PROMOTION_CHANNEL_ID"]) if os.environ.get("PROMOTION_CHANNEL_ID") else None
HOLDER_CHANNEL_ID    = int(os.environ.get("HOLDER_CHANNEL_ID", "1314066280592052244"))

RESOLVER_INTERVAL_MINUTES = 5

# Live board — a manual /office-board command always works; this is the
# "shows up on its own" half. Checked every 30 min, ~6% chance each check,
# which averages out to roughly 3-4 unprompted posts a day at random times
# rather than a predictable fixed schedule.
AMBIENT_BOARD_CHECK_MINUTES = 30
AMBIENT_BOARD_CHANCE        = 0.06

# Twice daily, 12h apart — spaced away from the Clash bracket times (2PM/12AM
# UTC) so a heavy Clash resolution and a promotion sweep don't land at once.
PROMOTION_SWEEP_TIMES = [dtime(hour=6, minute=0, tzinfo=timezone.utc), dtime(hour=18, minute=0, tzinfo=timezone.utc)]

RPS_CHOICES = ["rock", "paper", "scissors"]
RPS_BEATS   = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
RPS_EMOJI   = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}

INELIGIBLE_REASONS = {
    "already_seated":     "already holds an Office seat",
    "not_enough_shifts":  "hasn't worked enough base shifts yet",
    "not_lucky_enough":   "hasn't hit enough recently — needs 3+ hits in its last 40 shifts",
}

SHIFT_SKIP_REASONS = {
    "not_due_yet":      "not due yet",
    "already_working":  "already on shift",
    "in_duel":          "seat tied up in a duel",
}

# Office shifts reuse the same per-job story flavor text as base Jobs, but
# every outcome also gets one of these appended — this is what makes the
# Office actually read like a 9-to-5 rather than just "Jobs with better odds."
# Misses send you home for the day; wins get real workplace praise.
OFFICE_MISS_LINES = [
    "Nothing today — clock out, head home, and get some rest. Same time tomorrow.",
    "Quiet one. Go home, eat some dinner, and recharge — the desk will still be there.",
    "That's a wrap for today. Log off, put your feet up, come back sharp tomorrow.",
    "Slow shift. Head home and decompress — tomorrow's a new day.",
    "No dice this time. Go grab dinner and get some sleep — you've earned the evening off.",
    "Punch out. Rest up tonight and try again tomorrow.",
    "One of those days. Shut the laptop, order something good, and call it early.",
    "Nothing on the books today. Head home — no sense staying late for this.",
    "Coffee didn't help today. Go home, unwind, and reset for tomorrow.",
    "Not every shift's a winner. Go home, get some real rest, and shake it off.",
    "Quiet on the floor. Head out, eat well, and come back fresh.",
    "That one just wasn't in the cards. Go home, relax, try again tomorrow.",
    "Nothing to show for today. Log off early and take the evening for yourself.",
    "Dry spell continues. Go home, get some sleep, don't overthink it.",
    "Long day, short results. Head home and let it go till tomorrow.",
]
OFFICE_ALGO_WIN_LINES = [
    "Outstanding work — that's going in the performance review.",
    "Employee of the Month energy right there.",
    "Management's taking notice. Keep this up.",
    "That's the kind of quarter that gets you a corner office.",
    "Solid close — drinks are on the company tonight.",
    "Someone's getting a raise after that.",
    "Now that's a number worth putting in the newsletter.",
    "That's the kind of day that gets you name-dropped in the town hall.",
    "Numbers like that don't go unnoticed upstairs.",
    "Chalk that up as a career highlight.",
    "That's a bonus-worthy shift if there ever was one.",
    "Someone's putting your name up for the leaderboard.",
    "That's the kind of close that gets applause in the Monday meeting.",
    "Textbook performance. HR's going to want a quote for the newsletter.",
    "That's the shift people talk about at the water cooler.",
]
OFFICE_NFT_WIN_LINES = [
    "That's a career-defining win — frame it for the office wall.",
    "That's the deal that gets you a keynote at the next all-hands.",
    "Landed the big one. That's a corner-office kind of day.",
    "That's how you make partner.",
    "That's the kind of win they name a conference room after.",
    "Legendary quarter. That one's going on the office plaque.",
    "That's the deal of the fiscal year, full stop.",
    "Someone's getting a corner office out of that one.",
    "That's the kind of close that ends up in the annual report.",
]

# Promotion is a bigger moment than any single shift — it's the reward for
# a genuine hot streak, not a grind milestone, so it gets its own bank and
# its own embed treatment rather than a one-line channel post.
OFFICE_PROMOTION_LINES = [
    "The streak got noticed. Welcome to the Office.",
    "That kind of luck doesn't go unnoticed upstairs.",
    "Someone upstairs pulled some strings. New desk, new odds.",
    "Hot hand, corner office. That's how it works around here.",
    "The board took one look at that run and made the call.",
    "Turns out being lucky is a hireable skill.",
    "That's the kind of run that gets you headhunted internally.",
    "Management doesn't ask how, just how often. Welcome aboard.",
    "Somebody pulled a badge and a keycard together real fast.",
]


def _append_office_line(base: str, addition: str) -> str:
    """
    Appends office-flavor text after the base job-story line, making sure
    there's real sentence-ending punctuation between them first. Without
    this, a base line ending mid-clause (no trailing period) runs straight
    into the next sentence with no separation — e.g. "...no luck Quiet on
    the floor." instead of "...no luck. Quiet on the floor."
    """
    base = base.rstrip()
    if base and base[-1] not in ".!?":
        base += "."
    return f"{base} {addition}"


def _roll_office_hits(spark_tier: int) -> tuple[bool, bool, float | None]:
    algo_hit = random.random() < OFFICE_ALGO_HIT_CHANCE.get(spark_tier, 0)
    nft_hit  = random.random() < OFFICE_NFT_HIT_CHANCE.get(spark_tier, 0)
    amount = None
    if algo_hit:
        lo, hi = OFFICE_PAYOUT_RANGE.get(spark_tier, (0.1, 0.3))
        amount = round(min(random.uniform(lo, hi), OFFICE_MAX_SHIFT_PAYOUT), 3)
    return algo_hit, nft_hit, amount


def _judge_round(a: str, b: str) -> int:
    """1 if a beats b, -1 if b beats a, 0 if tie."""
    if a == b:
        return 0
    return 1 if RPS_BEATS[a] == b else -1


def _resolve_rps(challenger_picks: list, defender_picks: list) -> tuple[str, list]:
    """
    Best of 7. Returns ("challenger" | "defender", rounds_detail). If tied
    after 7 (possible when several rounds tie), plays sudden-death rounds
    with fresh random picks on both sides until it's decided.
    """
    rounds = []
    c_wins = d_wins = 0
    for i in range(7):
        a, b = challenger_picks[i], defender_picks[i]
        result = _judge_round(a, b)
        if result == 1:
            c_wins += 1
        elif result == -1:
            d_wins += 1
        rounds.append({"round": i + 1, "challenger": a, "defender": b, "result": result})

    while c_wins == d_wins:
        a, b = random.choice(RPS_CHOICES), random.choice(RPS_CHOICES)
        result = _judge_round(a, b)
        if result == 1:
            c_wins += 1
        elif result == -1:
            d_wins += 1
        rounds.append({"round": len(rounds) + 1, "challenger": a, "defender": b, "result": result, "sudden_death": True})

    return ("challenger" if c_wins > d_wins else "defender"), rounds


class DuelPickView(discord.ui.View):
    """Ephemeral RPS picker — click one of 3 buttons per round, 7 rounds total."""

    def __init__(self, duel_id: int, side: str, spark_name: str):
        super().__init__(timeout=OFFICE_DUEL_SUBMIT_HOURS * 3600)
        self.duel_id    = duel_id
        self.side       = side
        self.spark_name = spark_name
        self.picks: list[str] = []

    def _progress_text(self) -> str:
        made = " ".join(RPS_EMOJI[p] for p in self.picks) or "—"
        return f"**{self.spark_name}** — round {len(self.picks) + 1} of 7\nSo far: {made}"

    async def _handle_pick(self, interaction: discord.Interaction, choice: str):
        self.picks.append(choice)
        if len(self.picks) < 7:
            await interaction.response.edit_message(content=self._progress_text(), view=self)
            return

        await asyncio.to_thread(submit_duel_picks, self.duel_id, self.side, self.picks)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"✅ All 7 picks locked in for **{self.spark_name}**. Waiting on the other side...",
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Rock", emoji="🪨", style=discord.ButtonStyle.secondary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_pick(interaction, "rock")

    @discord.ui.button(label="Paper", emoji="📄", style=discord.ButtonStyle.secondary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_pick(interaction, "paper")

    @discord.ui.button(label="Scissors", emoji="✂️", style=discord.ButtonStyle.secondary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_pick(interaction, "scissors")


class ShiftTimeSelect(discord.ui.Select):
    """24 fixed hourly UTC slots — Discord has no native time picker, and
    this avoids free-text parsing/validation entirely. Applies the chosen
    time to every seat passed in, in one shot — no need to pick per-Spark."""

    def __init__(self, seats: list[tuple[int, str]]):
        options = [discord.SelectOption(label=f"{h:02d}:00 UTC", value=str(h)) for h in range(24)]
        super().__init__(placeholder="Choose your daily shift time (UTC)", options=options)
        self.seats = seats

    async def callback(self, interaction: discord.Interaction):
        hour = int(self.values[0])
        for asa, _ in self.seats:
            await asyncio.to_thread(set_office_shift_time, asa, hour, 0)
        names = ", ".join(f"**{name}**" for _, name in self.seats)
        await interaction.response.edit_message(
            content=f"✅ Daily shift time set to **{hour:02d}:00 UTC** for {names}. "
                    f"Shifts open at this time every day from now on.",
            view=None,
        )


class ShiftTimeSelectView(discord.ui.View):
    def __init__(self, seats: list[tuple[int, str]]):
        super().__init__(timeout=300)
        self.add_item(ShiftTimeSelect(seats))


class OfficeReminderClockInView(discord.ui.View):
    """
    Persistent button attached to shift-reminder alarms — clicking it
    clocks in every due Office seat the wallet holds (not just the one
    Spark that triggered this specific reminder), same bulk behavior as
    /office-shift. Persistent (timeout=None, fixed custom_id) so it keeps
    working even if the bot restarts before someone clicks it.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Clock In", emoji="🕐", style=discord.ButtonStyle.primary, custom_id="office_reminder_clock_in")
    async def clock_in(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = str(interaction.user.id)
        wallet = await asyncio.to_thread(get_wallet, user_id)
        if not wallet:
            await interaction.followup.send("❌ Link your wallet first with `/link`.", ephemeral=True)
            return
        cog = interaction.client.get_cog("SparkOfficeCog")
        if not cog:
            await interaction.followup.send("❌ Office system isn't loaded right now — try again shortly.", ephemeral=True)
            return
        result = await cog._clock_in_all_due(user_id, wallet)
        await interaction.followup.send(cog._format_clock_in_result(result), ephemeral=True)


class SparkOfficeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.resolver.start()
        self.promotion_sweep.start()
        self.ambient_board.start()

    async def cog_load(self):
        self.bot.add_view(OfficeReminderClockInView())

    def cog_unload(self):
        self.resolver.cancel()
        self.promotion_sweep.cancel()
        self.ambient_board.cancel()

    def _promotion_channel(self) -> discord.TextChannel | None:
        if not PROMOTION_CHANNEL_ID:
            return None
        return self.bot.get_channel(PROMOTION_CHANNEL_ID)

    @staticmethod
    def _shift_time_picker_message(seats: list[tuple]) -> str:
        """
        Message shown alongside the shift-time picker. UTC is confusing to
        pick blind, so this lists every hour slot next to Discord's <t:...>
        timestamp tag — Discord renders that in each viewer's own local
        time automatically, so this works correctly for anyone regardless
        of timezone without us needing to know or ask what it is.
        """
        names = ", ".join(f"**{name}**" for _, name in seats)
        now = datetime.now(timezone.utc)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        ref_lines = [
            f"`{h:02d}:00 UTC` → <t:{int((today_midnight + timedelta(hours=h)).timestamp())}:t> your time"
            for h in range(24)
        ]
        return (
            f"Pick a daily shift time for {names}.\n"
            f"-# Each UTC slot below, shown in your own local time:\n"
            + "\n".join(ref_lines)
        )

    @staticmethod
    def _promotion_celebration_embed(spark_name: str, discord_user_id: str | None, hits_seen: int | None = None) -> discord.Embed:
        """
        Shared celebration embed for both the manual /office-promote path
        and the auto-sweep — a promotion is the payoff for a genuine hot
        streak, so it earns real fanfare instead of a plain one-line post.
        """
        who = f"<@{discord_user_id}>" if discord_user_id else "Someone"
        line = random.choice(OFFICE_PROMOTION_LINES)
        stat = f"\n\n🔥 {hits_seen} hits in its last 40 shifts." if hits_seen is not None else ""
        embed = discord.Embed(
            title="🎉 PROMOTED TO THE OFFICE!",
            description=f"**{spark_name}** just earned a seat. {who}'s Spark is moving up.\n\n*{line}*{stat}",
            color=0xFFD700,
        )
        embed.set_footer(text="🏢 The Office · run /office-set-shift-time to pick a clock-in time")
        return embed

    # ──────────────────────────────────────────
    # /office-promote — the entry point. Seats directly if a spot is open,
    # otherwise challenges the lowest hit-rate seat on your behalf. asset_id
    # is optional — omit it and every Spark in your wallet gets checked and
    # attempted at once, same "don't make me look up an ID" philosophy as
    # /office-shift.
    # ──────────────────────────────────────────
    @app_commands.command(name="office-promote", description="Try to promote your eligible Spark(s) into the Office")
    @app_commands.describe(asset_id="Optional — ASA ID of one specific Spark. Omit to check your whole wallet.")
    async def office_promote(self, interaction: discord.Interaction, asset_id: int | None = None):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)

        wallet = await asyncio.to_thread(get_wallet, user_id)
        if not wallet:
            await interaction.followup.send("❌ Link your wallet first with `/link`.", ephemeral=True)
            return

        if asset_id is not None:
            spark = await asyncio.to_thread(get_spark, asset_id)
            if not spark or spark.get("wallet") != wallet:
                await interaction.followup.send("❌ That Spark isn't registered to your wallet.", ephemeral=True)
                return
            candidates = [spark]
        else:
            candidates = await asyncio.to_thread(get_sparks_for_wallet, wallet)
            if not candidates:
                await interaction.followup.send("❌ No Sparks found on your wallet.", ephemeral=True)
                return
            # get_sparks_for_wallet only selects asset_id/name/spark_type/tier/xp —
            # wallet and discord_user_id aren't in that row, so stamp them on
            # explicitly since every downstream function expects them.
            for spark in candidates:
                spark["wallet"] = wallet
                spark["discord_user_id"] = user_id

        # Filter down to not-already-seated + currently eligible, luckiest first.
        eligible, skipped = [], {}
        for spark in candidates:
            asa = spark["asset_id"]
            if await asyncio.to_thread(get_office_seat, asa):
                skipped[asa] = "already_seated"
                continue
            check = await asyncio.to_thread(check_office_eligibility, asa)
            if not check["eligible"]:
                skipped[asa] = check["reason"]
                continue
            eligible.append((spark, check))
        eligible.sort(key=lambda pair: pair[1]["hits_seen"], reverse=True)

        if not eligible:
            if asset_id is not None:
                reason = INELIGIBLE_REASONS.get(skipped.get(asset_id, "not_eligible"), "not eligible right now")
                await interaction.followup.send(f"❌ Not eligible — {reason}.", ephemeral=True)
            else:
                await interaction.followup.send("❌ None of your Sparks are currently eligible for promotion.", ephemeral=True)
            return

        # Sponsorship is wallet-level — one check covers every eligible Spark this run.
        from algorand_lookup import link_wallet as verify_wallet
        ownership = await verify_wallet(user_id, wallet)
        if ownership.get("error"):
            await interaction.followup.send(f"❌ Couldn't verify wallet holdings: {ownership['error']}", ephemeral=True)
            return
        zappy_count = len(ownership.get("zappies", [])) + len(ownership.get("heroes", [])) + len(ownership.get("collabs", []))
        if zappy_count < OFFICE_SPONSOR_ZAPPY_COUNT:
            await interaction.followup.send(
                f"❌ Needs {OFFICE_SPONSOR_ZAPPY_COUNT} Zappies in your wallet to sponsor a promotion "
                f"— you have {zappy_count}. ({len(eligible)} Spark(s) otherwise ready to go.)",
                ephemeral=True,
            )
            return

        results = []
        newly_seated = []  # (asa, spark_name) — used to offer the shift-time picker after
        for spark, check in eligible:
            asa = spark["asset_id"]
            spark_name = spark.get("name") or spark["spark_type"].capitalize()
            seat_count = await asyncio.to_thread(get_office_seat_count)

            if seat_count < OFFICE_SEAT_CAP:
                await asyncio.to_thread(seat_spark, {**spark, "asset_id": asa})
                newly_seated.append((asa, spark_name))
                results.append(f"🎉 **{spark_name}** promoted into an open seat!")
                await self._post_promotion_channel(
                    embed=self._promotion_celebration_embed(spark_name, user_id, check["hits_seen"])
                )
                continue

            target = await asyncio.to_thread(get_lowest_hitrate_seat)
            target = target if (target and target["wallet"] != wallet) else None
            if not target:
                results.append(f"⏳ **{spark_name}** is eligible, but there's no valid seat to challenge right now.")
                continue

            duel = await asyncio.to_thread(create_office_duel, {**spark, "asset_id": asa}, target)
            results.append(
                f"⚔️ **{spark_name}** is challenging **{target.get('spark_name') or target['spark_type']}** for their seat!"
            )
            await self._post_duel_challenge(duel)

        if skipped:
            results.append(
                f"\nSkipped ({len(skipped)}): "
                + ", ".join(f"`{asa}` ({INELIGIBLE_REASONS.get(r, r)})" for asa, r in skipped.items())
            )

        for chunk in _chunk_lines(results):
            await interaction.followup.send(chunk, ephemeral=True)

        if newly_seated:
            await interaction.followup.send(
                self._shift_time_picker_message(newly_seated),
                view=ShiftTimeSelectView(newly_seated),
                ephemeral=True,
            )

    # ──────────────────────────────────────────
    # /office-shift — clocks in EVERY due, active Office seat you hold in
    # one shot. No asset_id needed — same "no picker, send everything"
    # philosophy as /spark-job.
    # ──────────────────────────────────────────
    async def _clock_in_all_due(self, user_id: str, wallet: str) -> dict:
        """Returns {"clocked": [lines...], "skipped": {asa: reason}, "count": n, "no_seats": bool}."""
        seats = await asyncio.to_thread(get_office_seats_for_wallet, wallet)
        if not seats:
            return {"clocked": [], "skipped": {}, "count": 0, "no_seats": True}

        now = datetime.now(timezone.utc)
        clocked_lines = []
        skipped = {}

        for seat in seats:
            if seat["status"] != "active":
                skipped[seat["spark_asa"]] = "in_duel"
                continue

            due = datetime.fromisoformat(seat["next_shift_due_at"])
            if now < due:
                skipped[seat["spark_asa"]] = "not_due_yet"
                continue

            working = await asyncio.to_thread(get_working_office_shift, seat["spark_asa"])
            if working:
                skipped[seat["spark_asa"]] = "already_working"
                continue

            job = random.choice(JOB_NAMES)
            spark_name = seat.get("spark_name") or seat["spark_type"].capitalize()
            emoji = JOB_EMOJIS.get(job, "🔧")
            line = f"{emoji} **{spark_name}** (Office) " + random.choice(JOBS[job]["clock_in"])
            await asyncio.to_thread(create_office_shift, seat, job, line)
            clocked_lines.append(line)

        if clocked_lines:
            channel = self._promotion_channel()
            if channel:
                bullet_lines = [f"• {l}" for l in clocked_lines]
                chunks = _chunk_lines(bullet_lines)
                color = _color_for_user(user_id)
                for i, chunk in enumerate(chunks):
                    embed = discord.Embed(
                        title=f"🕐 Clocking In — {len(clocked_lines)} Office Spark(s)" if i == 0 else "🕐 Clocking In (cont.)",
                        description=chunk,
                        color=color,
                    )
                    await channel.send(content=f"<@{user_id}>" if i == 0 else None, embed=embed)

        return {"clocked": clocked_lines, "skipped": skipped, "count": len(clocked_lines), "no_seats": False}

    def _format_clock_in_result(self, result: dict) -> str:
        if result["no_seats"]:
            return "❌ You don't hold any Office seats."
        if result["count"] == 0 and not result["skipped"]:
            return "❌ You don't hold any Office seats."
        lines = [f"✅ Clocked in **{result['count']}** Office Spark(s)." if result["count"]
                  else "⏳ Nothing to clock in right now."]
        if result["skipped"]:
            lines.append(f"\nSkipped ({len(result['skipped'])}):")
            for asa, reason in result["skipped"].items():
                lines.append(f"  · ASA `{asa}` — {SHIFT_SKIP_REASONS.get(reason, reason)}")
        return "\n".join(lines)

    @app_commands.command(name="office-shift", description="Clock in all your due Office Spark(s) for today")
    async def office_shift(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = str(interaction.user.id)
        wallet  = await asyncio.to_thread(get_wallet, user_id)
        if not wallet:
            await interaction.followup.send("❌ Link your wallet first with `/link`.", ephemeral=True)
            return
        result = await self._clock_in_all_due(user_id, wallet)
        for chunk in _chunk_lines(self._format_clock_in_result(result).split("\n")):
            await interaction.followup.send(chunk, ephemeral=True)

    # ──────────────────────────────────────────
    # /office-duel-respond — submit picks for a pending duel
    # ──────────────────────────────────────────
    @app_commands.command(name="office-duel-respond", description="Submit your picks for a pending Office duel")
    @app_commands.describe(asset_id="ASA ID of the Spark on either side of the duel")
    async def office_duel_respond(self, interaction: discord.Interaction, asset_id: int):
        duel = await asyncio.to_thread(get_pending_duel_for_spark, asset_id)
        if not duel:
            await interaction.response.send_message("❌ No pending duel found for that Spark.", ephemeral=True)
            return

        if asset_id == duel["challenger_asa"]:
            side, name, already = "challenger", duel["challenger_name"], duel.get("challenger_picks")
        else:
            side, name, already = "defender", duel["defender_name"], duel.get("defender_picks")

        if already:
            await interaction.response.send_message("✅ You've already submitted picks for this duel.", ephemeral=True)
            return

        view = DuelPickView(duel["id"], side, name or f"ASA {asset_id}")
        await interaction.response.send_message(view._progress_text(), view=view, ephemeral=True)

    # ──────────────────────────────────────────
    # /office-set-shift-time — pick/change a seat's fixed daily anchor.
    # Covers both existing seat-holders (their time was auto-backfilled
    # from the old rolling system and they may want to actually choose
    # one) and anyone who skipped the inline picker after promotion.
    # ──────────────────────────────────────────
    @app_commands.command(name="office-set-shift-time", description="Choose your daily Office clock-in time for all your seated Sparks")
    async def office_set_shift_time(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        wallet = await asyncio.to_thread(get_wallet, user_id)
        if not wallet:
            await interaction.response.send_message("❌ Link your wallet first with `/link`.", ephemeral=True)
            return

        seats = await asyncio.to_thread(get_office_seats_for_wallet, wallet)
        seats = [s for s in seats if s.get("status") == "active"]
        if not seats:
            await interaction.response.send_message("❌ You don't hold any Office seats.", ephemeral=True)
            return

        seat_list = [(s["spark_asa"], s.get("spark_name") or s["spark_type"].capitalize()) for s in seats]
        await interaction.response.send_message(
            self._shift_time_picker_message(seat_list),
            view=ShiftTimeSelectView(seat_list),
            ephemeral=True,
        )

    # ──────────────────────────────────────────
    # /office-board — live snapshot of who's seated and what they're doing.
    # Same embed also posts on its own at random intervals (ambient_board,
    # below) so the Office has visible activity without anyone having to ask.
    # ──────────────────────────────────────────
    @app_commands.command(name="office-board", description="See who's working the Office right now")
    async def office_board(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self._build_office_board_embed()
        await interaction.followup.send(embed=embed)

    @staticmethod
    def _progress_bar(filled: int, total: int, length: int = 10) -> str:
        filled_blocks = round(filled / total * length) if total else 0
        return "▰" * filled_blocks + "▱" * (length - filled_blocks)

    def _next_sweep_time(self, now: datetime) -> datetime:
        candidates = []
        for t in PROMOTION_SWEEP_TIMES:
            candidate = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            candidates.append(candidate)
        return min(candidates)

    async def _build_office_board_embed(self) -> discord.Embed:
        seats = await asyncio.to_thread(get_office_seats)  # already sorted lowest hit-rate first
        working_map = await asyncio.to_thread(get_working_office_shifts_map)
        now = datetime.now(timezone.utc)
        seat_count = len(seats)
        bar = self._progress_bar(seat_count, OFFICE_SEAT_CAP)

        if not seats:
            embed = discord.Embed(
                title="🏢 The Office — Live Board",
                description=f"{bar}  **0/{OFFICE_SEAT_CAP}** seats filled\n\nEmpty right now — be the first with `/office-promote`.",
                color=0x5865F2,
            )
            return embed

        board_lines = [f"{bar}  **{seat_count}/{OFFICE_SEAT_CAP}** seats filled", ""]

        for seat in seats:
            name  = seat.get("spark_name") or seat["spark_type"].capitalize()
            tier  = seat.get("spark_tier")
            shifts, hits, misses = seat["shifts_completed"], seat["hits"], seat["consecutive_misses"]
            rate  = f"{(hits / shifts * 100):.0f}%" if shifts else "—"
            cold_flag = " ❄️" if misses >= max(OFFICE_DEMOTION_MISS_DAYS - 2, 1) else ""

            if seat["status"] == "in_duel":
                status = "⚔️ In a duel"
            elif seat["spark_asa"] in working_map:
                resolve_at = datetime.fromisoformat(working_map[seat["spark_asa"]]["resolve_at"])
                status = f"🔧 On shift — back <t:{int(resolve_at.timestamp())}:R>"
            else:
                due = datetime.fromisoformat(seat["next_shift_due_at"]) if seat.get("next_shift_due_at") else now
                status = f"💤 Resting — due <t:{int(due.timestamp())}:R>" if now < due else "⏳ Due now"

            board_lines.append(f"🪑 **{name}** T{tier} — {status} · `{hits}/{shifts}` hits ({rate}){cold_flag}")

        # Embed description caps at 4096 chars — 20 short seat lines never
        # comes close, so no chunking/fields needed (embed fields cap at
        # 1024 chars each, which _chunk_lines' default 1800 could exceed).
        embed = discord.Embed(title="🏢 The Office — Live Board", description="\n".join(board_lines), color=0x5865F2)

        lowest = seats[0]
        lowest_name = lowest.get("spark_name") or lowest["spark_type"].capitalize()
        lowest_rate = f"{(lowest['hits'] / lowest['shifts_completed'] * 100):.0f}%" if lowest["shifts_completed"] else "—"
        next_sweep = self._next_sweep_time(now)
        footer = f"⚔️ Duel target: {lowest_name} ({lowest_rate})"
        if seat_count >= OFFICE_SEAT_CAP:
            footer += f" · Next auto-sweep <t:{int(next_sweep.timestamp())}:R>"
        embed.set_footer(text=footer)

        return embed

    # ──────────────────────────────────────────
    # Admin — troubleshooting
    # ──────────────────────────────────────────
    @app_commands.command(name="office-force-resolve", description="[Admin] Force-resolve a working Office shift with a chosen outcome")
    @app_commands.describe(
        asset_id="Spark ASA currently on an Office shift",
        outcome="Force this outcome",
        amount="Override ALGO amount (optional — only used for 'algo')",
    )
    @app_commands.choices(outcome=[
        app_commands.Choice(name="ALGO hit", value="algo"),
        app_commands.Choice(name="NFT hit", value="nft"),
        app_commands.Choice(name="Miss", value="miss"),
    ])
    async def office_force_resolve(
        self, interaction: discord.Interaction, asset_id: int,
        outcome: app_commands.Choice[str], amount: float | None = None,
    ):
        if not await admin_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        row = await asyncio.to_thread(get_working_office_shift, asset_id)
        if not row:
            await interaction.followup.send("❌ No working Office shift found for that Spark right now.", ephemeral=True)
            return

        resolved = await self._resolve_and_settle([row], forced_outcome=outcome.value, forced_amount=amount)
        if resolved:
            await self._process_algo_payouts()
            await self._post_digest(resolved)
        await interaction.followup.send(f"✅ Force-resolved ASA `{asset_id}` as **{outcome.value}**.", ephemeral=True)

    # ──────────────────────────────────────────
    # Promotion channel helpers
    # ──────────────────────────────────────────
    async def _post_promotion_channel(self, content: str = None, embed: discord.Embed = None):
        channel = self._promotion_channel()
        if channel:
            await channel.send(content=content, embed=embed)

    async def _post_duel_challenge(self, duel: dict):
        embed = discord.Embed(
            title="⚔️ Office Duel — Challenge Issued",
            description=(
                f"**{duel['challenger_name']}** (<@{duel['challenger_discord_id']}>) challenges "
                f"**{duel['defender_name']}** (<@{duel['defender_discord_id']}>) for their Office seat.\n\n"
                f"Best of 7, rock/paper/scissors. Both sides run `/office-duel-respond` to submit — "
                f"{OFFICE_DUEL_SUBMIT_HOURS}h to lock in picks or the bot rolls for you."
            ),
            color=0xE67E22,
        )
        await self._post_promotion_channel(embed=embed)

    # ──────────────────────────────────────────
    # Resolver — shifts, reminders, no-shows, cold-streak demotions, duel expiry
    # ──────────────────────────────────────────
    @tasks.loop(minutes=RESOLVER_INTERVAL_MINUTES)
    async def resolver(self):
        try:
            due = await asyncio.to_thread(get_due_office_jobs)
            resolved = await self._resolve_and_settle(due)
            if resolved:
                await self._process_algo_payouts()
                await self._post_digest(resolved)

            await self._process_shift_reminders()
            await self._process_noshow_demotions()
            await self._process_coldstreak_demotions()
            await self._process_expired_duels()

        except Exception as e:
            print(f"[spark_office] resolver error: {e}")

    @resolver.before_loop
    async def before_resolver(self):
        await self.bot.wait_until_ready()
        # Office and base Jobs both resolve on a 5-min cadence. Firing at the
        # exact same instant every cycle doubles simultaneous AlgoNode load
        # (payouts + wallet lookups) — a direct contributor to the July 17
        # quota-exhaustion incident. This offset alone doesn't fix quota
        # exhaustion (algo_quota_guard does that), but there's no reason to
        # keep spiking both loops together on top of it.
        await asyncio.sleep(150)  # 2.5 minutes

    # ──────────────────────────────────────────
    # Auto-promotion sweep — twice daily. Checks every Spark, luckiest
    # first: open seats get auto-filled, and once seats are full, an
    # eligible candidate auto-spawns a duel against the lowest hit-rate
    # seat. Neither side chooses the moment, so both get equal footing —
    # this mirrors /office-promote exactly, just run on everyone at once
    # instead of one Spark at a time on request.
    # ──────────────────────────────────────────
    @tasks.loop(time=PROMOTION_SWEEP_TIMES)
    async def promotion_sweep(self):
        try:
            await self._run_promotion_sweep()
        except Exception as e:
            print(f"[spark_office] promotion sweep error: {e}")

    @promotion_sweep.before_loop
    async def before_promotion_sweep(self):
        await self.bot.wait_until_ready()

    # ──────────────────────────────────────────
    # Ambient board — same embed as /office-board, but shows up on its own.
    # Checked every 30 min with a small independent chance each time, so
    # posts land at random points through the day rather than a predictable
    # schedule. Skips quietly if the Office is empty — no point announcing
    # nothing.
    # ──────────────────────────────────────────
    @tasks.loop(minutes=AMBIENT_BOARD_CHECK_MINUTES)
    async def ambient_board(self):
        try:
            if random.random() >= AMBIENT_BOARD_CHANCE:
                return
            seat_count = await asyncio.to_thread(get_office_seat_count)
            if seat_count == 0:
                return
            embed = await self._build_office_board_embed()
            await self._post_promotion_channel(embed=embed)
        except Exception as e:
            print(f"[spark_office] ambient board error: {e}")

    @ambient_board.before_loop
    async def before_ambient_board(self):
        await self.bot.wait_until_ready()

    async def _run_promotion_sweep(self):
        from algorand_lookup import verify_wallet_owns_zappy

        candidates = await asyncio.to_thread(get_all_office_candidates)
        if not candidates:
            return

        for c in candidates:
            wallet = c["wallet"]
            spark_name = c.get("name") or c["spark_type"].capitalize()

            # Re-check seat count fresh each iteration — an earlier candidate
            # in this same sweep may have just filled the last open seat.
            seat_count = await asyncio.to_thread(get_office_seat_count)

            ownership = await verify_wallet_owns_zappy(wallet)
            if ownership.get("error"):
                continue  # couldn't verify this pass, will retry next sweep
            zappy_count = (
                len(ownership.get("zappies", []))
                + len(ownership.get("heroes", []))
                + len(ownership.get("collabs", []))
            )
            if zappy_count < OFFICE_SPONSOR_ZAPPY_COUNT:
                continue  # not sponsored — skip silently, eligible again next sweep

            if seat_count < OFFICE_SEAT_CAP:
                await asyncio.to_thread(seat_spark, c)
                await self._post_promotion_channel(
                    embed=self._promotion_celebration_embed(spark_name, c.get("discord_user_id"), c["hits_seen"])
                )
                continue

            # Seats are full — spawn a duel against the current lowest
            # hit-rate seat. Can't challenge your own seat.
            target = await asyncio.to_thread(get_lowest_hitrate_seat)
            if not target or target["wallet"] == wallet:
                continue  # no valid target this pass — try again next sweep

            duel = await asyncio.to_thread(create_office_duel, c, target)
            await self._post_duel_challenge(duel)

    async def _resolve_and_settle(self, rows: list, forced_outcome: str | None = None, forced_amount: float | None = None) -> list:
        """
        forced_outcome ('algo' | 'nft' | 'miss'), if given, skips the random
        roll — used only by /office-force-resolve for testing. The normal
        resolver loop never passes it, so live play always rolls for real.
        """
        from nft_rewards import award_nft_prize

        resolved = []
        for row in rows:
            spark_name = row.get("spark_name") or row["spark_type"]

            if forced_outcome is not None:
                algo_hit = forced_outcome == "algo"
                nft_hit  = forced_outcome == "nft"
                amount = None
                if algo_hit:
                    lo, hi = OFFICE_PAYOUT_RANGE.get(row["spark_tier"], (0.1, 0.3))
                    amount = forced_amount if forced_amount is not None else round(min(random.uniform(lo, hi), OFFICE_MAX_SHIFT_PAYOUT), 3)
            else:
                algo_hit, nft_hit, amount = _roll_office_hits(row["spark_tier"])

            nft_asa, nft_name = None, None
            if nft_hit:
                prize = await award_nft_prize(row.get("discord_user_id"), row["wallet"], source="spark_office")
                if prize.get("success"):
                    nft_asa, nft_name = prize["asset_id"], prize["name"]

            outcome = "nft" if nft_asa else ("algo" if algo_hit else "miss")
            flavor_line = _build_flavor_line(row["job"], spark_name, algo_hit, amount, nft_name)
            if nft_name:
                flavor_line = _append_office_line(flavor_line, random.choice(OFFICE_NFT_WIN_LINES))
                flavor_line += " Opt in to the ASA and run `/claimnft` to collect it!"
            elif algo_hit:
                flavor_line = _append_office_line(flavor_line, random.choice(OFFICE_ALGO_WIN_LINES))
            else:
                flavor_line = _append_office_line(flavor_line, random.choice(OFFICE_MISS_LINES))

            await asyncio.to_thread(
                complete_office_job, row["id"], row["spark_asa"], outcome, amount, nft_asa, flavor_line
            )

            xp_result = await asyncio.to_thread(award_spark_job_xp, row["spark_asa"])
            if xp_result.get("upgraded"):
                await asyncio.to_thread(
                    push_spark_arc19_upgrade, row["spark_asa"], xp_result["spark_type"], xp_result["tier_after"]
                )
                await self._post_tier_upgrade(row, xp_result)

            resolved.append({**row, "outcome": outcome, "amount": amount, "nft_asa": nft_asa, "flavor_line": flavor_line})

        return resolved

    async def _post_tier_upgrade(self, row: dict, xp_result: dict):
        channel = self.bot.get_channel(HOLDER_CHANNEL_ID)
        if not channel or not row.get("discord_user_id"):
            return
        new_tier  = xp_result["tier_after"]
        tier_name = TIER_NAMES.get(new_tier, f"T{new_tier}")
        spark_name = row.get("spark_name") or row["spark_type"]
        await channel.send(
            f"🌟 **SPARK UPGRADE!** <@{row['discord_user_id']}>'s **{spark_name}** has evolved to "
            f"**T{new_tier} {tier_name}** working an Office shift! ({xp_result['new_xp']} XP total)"
        )

    async def _process_algo_payouts(self):
        from algo_layer import _send_algo
        from algo_quota_guard import is_quota_blocked

        if is_quota_blocked():
            print("[spark_office] Skipping ALGO payout pass — quota block active.")
            # _send_algo would catch this per-wallet anyway (it self-guards),
            # but skipping the whole pass here avoids N redundant "will
            # retry" log lines and Supabase reads every 5-min tick while
            # blocked. Unpaid rows stay unpaid in Supabase — nothing lost,
            # next unblocked pass pays out everything that piled up.
            return

        unpaid = await asyncio.to_thread(get_unpaid_office_algo_jobs)
        if not unpaid:
            return

        by_wallet: dict[str, list] = {}
        for row in unpaid:
            by_wallet.setdefault(row["wallet"], []).append(row)

        for wallet, rows in by_wallet.items():
            total = round(sum(r["amount"] for r in rows), 3)
            if total <= 0:
                continue
            job_ids = [r["id"] for r in rows]
            micro = int(round(total * 1_000_000))
            try:
                txid = await asyncio.to_thread(_send_algo, wallet, micro, f"sparkoffice:payout:{wallet[:8]}")
                await asyncio.to_thread(mark_office_jobs_paid, job_ids)
                await asyncio.to_thread(create_office_payout, wallet, total, len(rows), job_ids, txid)
                print(f"[spark_office] Paid {total} ALGO to {wallet[:8]}... ({len(rows)} shifts) txid={txid}")
            except Exception as e:
                print(f"[spark_office] ALGO payout failed for {wallet}: {e} — will retry next pass")

    async def _post_digest(self, resolved: list):
        channel = self._promotion_channel()
        if not channel:
            return

        misses = [r for r in resolved if r["outcome"] == "miss"]
        wins   = [r for r in resolved if r["outcome"] in ("algo", "nft")]

        for r in wins:
            await self._post_win_embed(r)

        if misses:
            no_ping = discord.AllowedMentions(users=False)
            by_owner: dict[str | None, list] = {}
            for r in misses:
                by_owner.setdefault(r.get("discord_user_id"), []).append(r)
            for owner_id, rows in by_owner.items():
                lines = [f"{JOB_EMOJIS.get(r['job'], '🔧')} {r['flavor_line']}" for r in rows]
                color = _color_for_user(owner_id)
                for i, chunk in enumerate(_chunk_lines(lines)):
                    embed = discord.Embed(
                        title="💤 Office — End of Shift" if i == 0 else "💤 Office — End of Shift (cont.)",
                        description=chunk,
                        color=color,
                    )
                    content = f"<@{owner_id}>" if owner_id else None
                    await channel.send(content=content, embed=embed, allowed_mentions=no_ping)

    async def _post_win_embed(self, r: dict):
        channel = self._promotion_channel()
        if not channel:
            return
        job_emoji  = JOB_EMOJIS.get(r["job"], "🔧")
        spark_name = r.get("spark_name") or r["spark_type"]

        if r["outcome"] == "nft":
            embed = discord.Embed(title=f"🎁 OFFICE NFT DROP — {spark_name}", description=r["flavor_line"], color=0xB833FF)
        else:
            embed = discord.Embed(title=f"💰 OFFICE ALGO HIT — {spark_name}", description=r["flavor_line"], color=0xFFD700)
        embed.set_footer(text=f"{job_emoji} {r['job']} shift · The Office")

        content = f"<@{r['discord_user_id']}>" if r.get("discord_user_id") else None
        await channel.send(content=content, embed=embed)

    async def _process_shift_reminders(self):
        seats = await asyncio.to_thread(get_seats_needing_reminder)
        for seat in seats:
            await self._send_shift_reminder(seat)
            await asyncio.to_thread(mark_seat_reminded, seat["spark_asa"], datetime.now(timezone.utc))

    async def _send_shift_reminder(self, seat: dict):
        name = seat.get("spark_name") or seat["spark_type"].capitalize()
        discord_id = seat.get("discord_user_id")
        mention = f"<@{discord_id}>" if discord_id else None

        embed = discord.Embed(
            title="⏰ Alarm — time to clock in!",
            description=(
                f"**{name}**'s Office shift is open.\n"
                f"Tap below within **{OFFICE_NO_SHOW_GRACE_HOURS}h** or the seat opens up."
            ),
            color=0xE67E22,
        )
        channel = self._promotion_channel()
        if channel:
            await channel.send(content=mention, embed=embed, view=OfficeReminderClockInView())

    async def _process_noshow_demotions(self):
        no_shows = await asyncio.to_thread(get_seats_for_noshow_demotion)
        for seat in no_shows:
            await asyncio.to_thread(vacate_seat, seat["spark_asa"])
            name = seat.get("spark_name") or seat["spark_type"]
            embed = discord.Embed(
                title="🚪 Seat Vacated — No-Show",
                description=f"**{name}** (<@{seat.get('discord_user_id')}>) didn't clock in in time. A seat just opened up.",
                color=0xE74C3C,
            )
            await self._post_promotion_channel(embed=embed)

    async def _process_coldstreak_demotions(self):
        cold = await asyncio.to_thread(get_seats_for_cold_streak_demotion)
        for seat in cold:
            await asyncio.to_thread(vacate_seat, seat["spark_asa"])
            name = seat.get("spark_name") or seat["spark_type"]
            embed = discord.Embed(
                title="❄️ Seat Vacated — Cold Streak",
                description=(
                    f"**{name}** (<@{seat.get('discord_user_id')}>) ran cold — "
                    f"{seat['consecutive_misses']} shifts with no hit. A seat just opened up."
                ),
                color=0xE74C3C,
            )
            await self._post_promotion_channel(embed=embed)

    async def _process_expired_duels(self):
        expired = await asyncio.to_thread(get_expired_pending_duels)
        for duel in expired:
            c_picks = duel.get("challenger_picks") or [random.choice(RPS_CHOICES) for _ in range(7)]
            d_picks = duel.get("defender_picks") or [random.choice(RPS_CHOICES) for _ in range(7)]

            winner_side, rounds = _resolve_rps(c_picks, d_picks)
            winner_asa = duel["challenger_asa"] if winner_side == "challenger" else duel["defender_asa"]
            await asyncio.to_thread(resolve_office_duel, duel["id"], winner_asa, rounds)

            if winner_side == "challenger":
                # Challenger takes the seat — defender is vacated, challenger seated in its place.
                # Duel rows only carry names, not type/tier, so pull the Spark's current
                # record fresh rather than guessing — it may have tiered up since the challenge.
                challenger_spark = await asyncio.to_thread(get_spark, duel["challenger_asa"])
                await asyncio.to_thread(vacate_seat, duel["defender_asa"])
                await asyncio.to_thread(seat_spark, {
                    "asset_id":        duel["challenger_asa"],
                    "wallet":          duel["challenger_wallet"],
                    "discord_user_id": duel["challenger_discord_id"],
                    "name":            duel["challenger_name"],
                    "spark_type":      challenger_spark.get("spark_type", "") if challenger_spark else "",
                    "tier":            challenger_spark.get("tier", 1) if challenger_spark else 1,
                })
                winner_name, loser_name = duel["challenger_name"], duel["defender_name"]
            else:
                # Defender keeps the seat — just clear its in_duel status.
                from database import get_supabase
                db = await asyncio.to_thread(get_supabase)
                await asyncio.to_thread(
                    lambda: db.table("spark_office_seats").update({"status": "active"}).eq("spark_asa", duel["defender_asa"]).execute()
                )
                winner_name, loser_name = duel["defender_name"], duel["challenger_name"]

            round_lines = " ".join(
                f"{RPS_EMOJI[r['challenger']]}{RPS_EMOJI[r['defender']]}" for r in rounds
            )
            embed = discord.Embed(
                title="⚔️ Office Duel Resolved",
                description=(
                    f"**{winner_name}** defeats **{loser_name}** and takes the seat!\n\n"
                    f"Rounds (challenger vs defender): {round_lines}"
                ),
                color=0xFFD700,
            )
            await self._post_promotion_channel(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(SparkOfficeCog(bot))

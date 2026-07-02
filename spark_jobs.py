"""
spark_jobs.py
-------------
Daily opt-in work system for Sparks. Holders run /spark-job to send every
eligible Spark they hold out on a random shift; each shift resolves 8 hours
later with an independent ALGO-hit / NFT-hit roll, tier-weighted like the
Clash reward system.

Two-part storytelling:
  1. Clock-in — immediate response when /spark-job runs.
  2. Payday   — resolved automatically by the background loop and posted
                to the jobs channel as a live trickle throughout the day.

Payouts go out as real on-chain Algorand transactions to each holder's
linked wallet ADDRESS (spark_holdings.wallet / the same address /link
stores) — there is no internal Supabase balance ledger involved. ALGO
goes out via algo_layer's bot wallet sender (BOT_WALLET_MNEMONIC), the
same wallet that already funds Clash/GP payouts. NFT hits pick a random
NFT currently sitting in that same wallet on-chain (nft_rewards.pick_random_nft)
and send it directly — no tagging step, any NFT actually held by the
reward wallet is eligible, and once sent it naturally drops out of future
picks because the wallet's own balance reflects it's gone.

Env vars required:
  SPARK_JOBS_CHANNEL_ID — channel /spark-job posts clock-ins and the resolver
                           posts payday results to
Existing env vars this depends on (already set for token/nft rewards):
  BOT_WALLET_MNEMONIC, ALGOD_TOKEN, ALGOD_URL, INDEXER_URL
"""

import os
import random
import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands

from database import (
    get_wallet,
    get_eligible_sparks_for_job,
    create_spark_job,
    get_due_jobs,
    complete_job,
    get_unpaid_algo_jobs,
    get_unpaid_nft_jobs,
    mark_jobs_paid,
    create_job_payout,
    award_spark_job_xp,
    push_spark_arc19_upgrade,
)

JOBS_CHANNEL_ID = int(os.environ["SPARK_JOBS_CHANNEL_ID"]) if os.environ.get("SPARK_JOBS_CHANNEL_ID") else None

RESOLVER_INTERVAL_MINUTES = 5

# ─────────────────────────────────────────────
# Odds / payout table (tier-weighted, same target as before:
# 6 Sparks ~25% chance of at least one ALGO hit, ~2% NFT hit per batch)
# ─────────────────────────────────────────────
ALGO_HIT_CHANCE  = {1: 0.040, 2: 0.047, 3: 0.055}
NFT_HIT_CHANCE   = {1: 0.0025, 2: 0.0034, 3: 0.0045}
PAYOUT_RANGE     = {1: (0.05, 0.12), 2: (0.10, 0.20), 3: (0.15, 0.30)}
MAX_SHIFT_PAYOUT = 1.0  # hard cap, ALGO

TIER_NAMES = {1: "Spark", 2: "Flare", 3: "Blaze"}

# ─────────────────────────────────────────────
# Jobs — flavor line banks
# ─────────────────────────────────────────────
JOBS = {
    "Electrical": {
        "clock_in": [
            "heads out to run cable for the day",
            "grabs a tool belt and heads for the junction box",
            "clocks in for a wiring job across town",
        ],
        "hit": [
            "hit a live wire, clocks out with {amt} ALGO",
            "found a fat severance bonus in the breaker panel, {amt} ALGO",
            "got hazard pay for the day — {amt} ALGO",
        ],
        "miss": [
            "rewired a breaker box, quiet shift",
            "spent the day untangling conduit, nothing to show",
            "flipped switches until sundown, no bonus this time",
        ],
    },
    "The Forge": {
        "clock_in": [
            "reports to the forge",
            "heads down to stoke the coals",
            "clocks in at the forge floor",
        ],
        "hit": [
            "pulls a glowing ingot worth {amt} ALGO",
            "strikes something valuable in the slag, {amt} ALGO",
            "the foreman slips them a bonus — {amt} ALGO",
        ],
        "miss": [
            "kept the coals hot, nothing to show",
            "hammered scrap all day, no luck",
            "quiet shift at the anvil",
        ],
    },
    "Fortune Table": {
        "clock_in": [
            "sits down at the fortune table",
            "pulls up a chair at the fortune table",
            "joins the game at the fortune table",
        ],
        "hit": [
            "calls the right card, walks with {amt} ALGO",
            "reads the table perfectly, cashes out {amt} ALGO",
            "the deck runs hot tonight — {amt} ALGO",
        ],
        "miss": [
            "the cards stayed quiet all shift",
            "folded early, nothing gained",
            "the table ran cold tonight",
        ],
    },
    "The Garden": {
        "clock_in": [
            "heads to tend the garden",
            "grabs a trowel and heads for the garden",
            "clocks in for a shift in the garden",
        ],
        "hit": [
            "digs up {amt} ALGO buried in the roots",
            "finds something shiny under the soil, {amt} ALGO",
            "the harvest pays out — {amt} ALGO",
        ],
        "miss": [
            "watered the garden, quiet day",
            "pulled weeds all afternoon, nothing buried today",
            "tended the rows, no luck digging",
        ],
    },
    "The Mainframe": {
        "clock_in": [
            "clocks into the mainframe",
            "jacks into the mainframe for the shift",
            "reports to the server room",
        ],
        "hit": [
            "finds an exploit worth {amt} ALGO",
            "cracks a stray wallet key, {amt} ALGO",
            "the exploit pays out clean — {amt} ALGO",
        ],
        "miss": [
            "chased a null pointer all shift, no luck",
            "patched firewalls all day, nothing found",
            "ran diagnostics all shift, quiet",
        ],
    },
    "Night Security": {
        "clock_in": [
            "takes the night security shift",
            "clocks in for the night watch",
            "heads out on night security",
        ],
        "hit": [
            "shakes down a shadow for {amt} ALGO",
            "catches something worth {amt} ALGO in the dark",
            "the night pays off — {amt} ALGO",
        ],
        "miss": [
            "patrolled all night, nothing moved",
            "walked the perimeter, quiet shift",
            "kept watch till dawn, no incidents",
        ],
    },
}

JOB_NAMES = list(JOBS.keys())


def _roll_hits(spark_tier: int) -> tuple[bool, bool, float | None]:
    """Roll ALGO + NFT independently, tier-weighted. Returns (algo_hit, nft_hit, amount)."""
    algo_hit = random.random() < ALGO_HIT_CHANCE.get(spark_tier, 0)
    nft_hit  = random.random() < NFT_HIT_CHANCE.get(spark_tier, 0)

    amount = None
    if algo_hit:
        lo, hi = PAYOUT_RANGE.get(spark_tier, (0.05, 0.12))
        amount = round(min(random.uniform(lo, hi), MAX_SHIFT_PAYOUT), 3)

    return algo_hit, nft_hit, amount


def _build_flavor_line(job: str, spark_name: str, algo_hit: bool, amount: float | None, nft_name: str | None) -> str:
    bank = JOBS[job]
    if nft_name:
        # NFT hit overrides the line regardless of whether ALGO also hit — but the
        # ALGO amount (if any) is still paid separately; this only changes the text.
        return f"{spark_name} stumbles onto something on the {job} shift and walks away with **{nft_name}**."
    if algo_hit:
        return f"{spark_name} " + random.choice(bank["hit"]).format(amt=amount)
    return f"{spark_name} " + random.choice(bank["miss"])


class SparkJobsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.resolver.start()

    def cog_unload(self):
        self.resolver.cancel()

    def _jobs_channel(self) -> discord.TextChannel | None:
        if not JOBS_CHANNEL_ID:
            return None
        return self.bot.get_channel(JOBS_CHANNEL_ID)

    # ──────────────────────────────────────────
    # /spark-job
    # ──────────────────────────────────────────
    @app_commands.command(name="spark-job", description="Send your Sparks to work for the day")
    async def spark_job(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        wallet = await asyncio.to_thread(get_wallet, user_id)
        if not wallet:
            await interaction.followup.send("❌ Link your wallet first with `/link`.", ephemeral=True)
            return

        result = await asyncio.to_thread(get_eligible_sparks_for_job, wallet)
        eligible, skipped = result["eligible"], result["skipped"]

        if not eligible and not skipped:
            await interaction.followup.send("You don't have any registered Sparks. Use `/spark-register` first.", ephemeral=True)
            return

        if not eligible:
            reasons = set(skipped.values())
            if reasons == {"already_working"}:
                msg = "All your Sparks are already on shift — check back later for payday."
            elif reasons == {"already_paid_today"}:
                msg = "All your Sparks already worked a shift in the last 24h. Come back tomorrow."
            else:
                msg = "None of your Sparks are eligible to work right now."
            await interaction.followup.send(f"⏳ {msg}", ephemeral=True)
            return

        clock_in_lines = []
        for spark in eligible:
            job = random.choice(JOB_NAMES)
            display_name = spark.get("name") or spark["spark_type"].capitalize()
            line = f"**{display_name}** " + random.choice(JOBS[job]["clock_in"])
            await asyncio.to_thread(create_spark_job, spark, job, line)
            clock_in_lines.append(line)

        channel = self._jobs_channel()
        if channel:
            header = f"🕐 <@{user_id}> sends **{len(eligible)}** Spark(s) to work:"
            await channel.send(header + "\n" + "\n".join(f"• {l}" for l in clock_in_lines))

        summary = [f"✅ Sent **{len(eligible)}** Spark(s) to work — back in 8 hours."]
        if skipped:
            skip_lines = []
            for asa, reason in skipped.items():
                readable = {
                    "wallet_transfer_cooldown": "recently transferred, on cooldown",
                    "already_working": "already on shift",
                    "already_paid_today": "already worked today",
                }.get(reason, reason)
                skip_lines.append(f"  · ASA `{asa}` — {readable}")
            summary.append(f"\nSkipped ({len(skipped)}):\n" + "\n".join(skip_lines))

        await interaction.followup.send("\n".join(summary), ephemeral=True)

    # ──────────────────────────────────────────
    # Resolver — runs every few minutes, resolves due jobs, awards XP,
    # pays out, posts digest
    # ──────────────────────────────────────────
    @tasks.loop(minutes=RESOLVER_INTERVAL_MINUTES)
    async def resolver(self):
        from nft_rewards import pick_random_nft

        try:
            due = await asyncio.to_thread(get_due_jobs)
            resolved = []

            for row in due:
                spark_name = row.get("spark_name") or row["spark_type"]
                algo_hit, nft_hit, amount = _roll_hits(row["spark_tier"])

                nft_asa, nft_name = None, None
                if nft_hit:
                    # Pulls from whatever NFTs are actually sitting in the reward
                    # wallet right now — no tagging/reservation list to maintain.
                    nft = await pick_random_nft()
                    if nft:
                        nft_asa, nft_name = nft["asset_id"], nft["name"]
                    # If the wallet's NFT inventory is empty, this just quietly
                    # doesn't award an NFT — the ALGO roll (if any) still stands.

                outcome = "nft" if nft_asa else ("algo" if algo_hit else "miss")
                flavor_line = _build_flavor_line(row["job"], spark_name, algo_hit, amount, nft_name)

                await asyncio.to_thread(complete_job, row["id"], outcome, amount, nft_asa, flavor_line)

                # Flat XP for every completed shift, win or miss.
                xp_result = await asyncio.to_thread(award_spark_job_xp, row["spark_asa"])
                if xp_result.get("upgraded"):
                    await asyncio.to_thread(
                        push_spark_arc19_upgrade, row["spark_asa"], xp_result["spark_type"], xp_result["tier_after"]
                    )

                resolved.append({**row, "outcome": outcome, "amount": amount, "nft_asa": nft_asa, "flavor_line": flavor_line})

            if resolved:
                await self._process_algo_payouts()
                await self._process_nft_sends()
                await self._post_digest(resolved)

        except Exception as e:
            print(f"[spark_jobs] resolver error: {e}")

    @resolver.before_loop
    async def before_resolver(self):
        await self.bot.wait_until_ready()

    async def _process_algo_payouts(self):
        """
        Group unpaid ALGO-hit rows by wallet, send ONE real on-chain ALGO
        payment per wallet straight from the bot's reward wallet to the
        holder's Algorand address — same mechanism as GP/Clash payouts,
        not a Supabase balance field. `wallet` here is the address stored
        on spark_holdings (the same address /link records).
        """
        from algo_layer import _send_algo

        unpaid = await asyncio.to_thread(get_unpaid_algo_jobs)
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
                txid = await asyncio.to_thread(_send_algo, wallet, micro, f"sparkjobs:payout:{wallet[:8]}")
                await asyncio.to_thread(mark_jobs_paid, job_ids)
                await asyncio.to_thread(create_job_payout, wallet, total, len(rows), job_ids, txid)
                print(f"[spark_jobs] Paid {total} ALGO to {wallet[:8]}... ({len(rows)} shifts) txid={txid}")
            except Exception as e:
                print(f"[spark_jobs] ALGO payout failed for {wallet}: {e} — will retry next pass")

    async def _process_nft_sends(self):
        """Individually send each unpaid NFT hit directly to the holder's wallet address."""
        from nft_rewards import send_nft, check_nft_opt_in

        unpaid = await asyncio.to_thread(get_unpaid_nft_jobs)
        for row in unpaid:
            wallet, asa = row["wallet"], row["nft_asa"]

            opted_in = await check_nft_opt_in(wallet, asa)
            if not opted_in:
                # Leave unpaid — picked up again next resolver pass once they opt in.
                continue

            try:
                txid = await asyncio.to_thread(send_nft, wallet, asa, f"Spark Jobs prize — ASA {asa}")
                if txid:
                    await asyncio.to_thread(mark_jobs_paid, [row["id"]])
                    print(f"[spark_jobs] Sent NFT {asa} to {wallet[:8]}... txid={txid}")
            except Exception as e:
                print(f"[spark_jobs] NFT send failed for {wallet} ASA {asa}: {e} — will retry next pass")

    async def _post_digest(self, resolved: list):
        channel = self._jobs_channel()
        if not channel:
            return

        lines = [f"💰 {r['flavor_line']}" for r in resolved]
        # Post in chunks to stay under Discord's message length limit
        chunk, length = [], 0
        for line in lines:
            if length + len(line) > 1800:
                await channel.send("\n".join(chunk))
                chunk, length = [], 0
            chunk.append(line)
            length += len(line)
        if chunk:
            await channel.send("\n".join(chunk))


async def setup(bot: commands.Bot):
    await bot.add_cog(SparkJobsCog(bot))

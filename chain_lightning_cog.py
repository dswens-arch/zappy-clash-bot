"""
chain_lightning_cog.py
----------------------
Chain Lightning — a community relay for Zappies Reborn.

Flow:
  /arc  -> a player fires a bolt with one of their Zappies
  bot   -> tags up to 3 holders (2 active, 1 lurker). First to solve the puzzle wins the catch
  catch -> solve the puzzle (wire circuit / odd Zappy out / trivia), then pick a
           Zappy to pass with. That Zappy's Hero/trait sets the NEXT puzzle type
  miss  -> if nobody catches before the timer ends, the chain loses a Ground
           (3 to start). At 0 Grounds the chain ends.

All state lives in Supabase (see chain_lightning_setup.sql), so a Railway
restart loses nothing. Puzzles are generated in chain_puzzles.py.

Payouts are ZAPP, credited through gp_accounting (same ledger as the other
games). The amounts below are PLACEHOLDERS — tune them to taste.
"""

import asyncio
import os
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import chain_puzzles as puzzles
import gp_accounting
from algorand_lookup import (
    verify_wallet_owns_zappy, HERO_ASSET_IDS, HERO_IMAGES,
    _get_cached_wallet_result,
)
from database import get_wallet
from zappy_collection import ZAPPY_COLLECTION

# ── Config ─────────────────────────────────────────────────────────────────────

CHAIN_CHANNEL_ID = int(os.environ.get("CHAIN_CHANNEL_ID", 0) or os.environ.get("GAMES_CHANNEL_ID", 0) or 0)

START_GROUNDS        = 3        # lives
BASE_TIMER_HOURS     = 4        # first timer; shrinks 1h per lost Ground
MIN_TIMER_HOURS      = 1
TARGETS_PER_ARC      = 3        # 2 active + 1 lurker when both pools have people
ACTIVE_DAYS          = 14       # no activity for this long = lurker
MAX_CATCHES_PER_USER = 3        # per chain
TRIES_PER_BOLT       = 2        # wrong answers allowed per player per bolt
PASS_WINDOW_MINUTES  = 15       # catcher must pick a Zappy in this time, else auto-pass
START_COOLDOWN_HOURS = 12       # between chains (admins bypass)
HARD_AFTER_LINK      = 20       # active players get "hard" puzzles past this chain length

# ZAPP payouts (placeholders)
CATCH_BONUS_ZAPP        = 2
LURKER_CATCH_BONUS_ZAPP = 6
MILESTONE_ZAPP          = {10: 10, 25: 25, 50: 50, 100: 100}   # paid to every catcher
MILESTONE_MIN_CATCHERS  = 5     # stops two friends ping-ponging for the pot

POOL_LOW_WATER = 300            # refill the OpenTDB pool below this many rows

PUZZLE_NAMES = {"circuit": "Wire the Circuit", "odd": "Odd Zappy Out", "trivia": "Trivia"}
GOLD = 0xFFD60A


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def zappy_label(asset_id: int) -> str:
    if asset_id in HERO_ASSET_IDS:
        return f"{HERO_ASSET_IDS[asset_id]} Hero"
    entry = ZAPPY_COLLECTION.get(asset_id)
    return entry["name"] if entry else f"ASA {asset_id}"


def zappy_image_url(asset_id: int) -> str | None:
    if asset_id in HERO_ASSET_IDS:
        return HERO_IMAGES.get(HERO_ASSET_IDS[asset_id])
    entry = ZAPPY_COLLECTION.get(asset_id)
    return entry.get("image_url") if entry else None


def grounds_bar(n: int) -> str:
    n = max(0, n)
    return "🟢" * n + "⚫" * max(0, START_GROUNDS - n)


# ── Views ──────────────────────────────────────────────────────────────────────

class ChainBoltView(discord.ui.View):
    """Persistent buttons on every bolt card. Registered once at startup."""

    def __init__(self, cog: "ChainLightningCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="⚡ Catch", style=discord.ButtonStyle.success, custom_id="chain:catch")
    async def catch(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_catch(interaction)

    @discord.ui.button(label="📣 Nudge", style=discord.ButtonStyle.secondary, custom_id="chain:nudge")
    async def nudge(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_nudge(interaction)


class AnswerButton(discord.ui.Button):
    def __init__(self, idx: int, label: str):
        super().__init__(label=label[:80], style=discord.ButtonStyle.primary)
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        await self.view.cog.handle_answer(interaction, self.view, self)


class PuzzleAnswerView(discord.ui.View):
    def __init__(self, cog: "ChainLightningCog", bolt_id: int, labels: list[str]):
        super().__init__(timeout=900)
        self.cog = cog
        self.bolt_id = bolt_id
        for i, label in enumerate(labels):
            self.add_item(AnswerButton(i, label))


class ZappyPickView(discord.ui.View):
    """Pick one of your Zappies — used to start a chain and to pass the bolt."""

    def __init__(self, cog: "ChainLightningCog", user_id: int, items: list[tuple], on_pick):
        super().__init__(timeout=PASS_WINDOW_MINUTES * 60)
        self.cog = cog
        self.user_id = user_id
        self.items = items            # [(asset_id, label), ...]
        self.on_pick = on_pick        # async (interaction, asset_id)
        self._sample()

    def _sample(self):
        self.clear_items()
        shown = self.items if len(self.items) <= 25 else random.sample(self.items, 25)
        select = discord.ui.Select(
            placeholder="Choose a Zappy…",
            options=[discord.SelectOption(label=label[:100], value=str(aid)) for aid, label in shown],
        )

        async def _cb(interaction: discord.Interaction):
            await self.on_pick(interaction, int(select.values[0]))

        select.callback = _cb
        self.add_item(select)
        if len(self.items) > 25:
            btn = discord.ui.Button(label="🔀 Show others", style=discord.ButtonStyle.secondary)

            async def _shuffle(interaction: discord.Interaction):
                self._sample()
                await interaction.response.edit_message(view=self)

            btn.callback = _shuffle
            self.add_item(btn)


# ── Cog ────────────────────────────────────────────────────────────────────────

class ChainLightningCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot = bot
        self.db = db
        self._touch_ts: dict[int, float] = {}
        self._last_refill = 0.0
        self._lock = asyncio.Lock()          # serialises chain advances

    async def cog_load(self):
        self.bot.add_view(ChainBoltView(self))
        if not self.worker.is_running():
            self.worker.start()

    async def cog_unload(self):
        self.worker.cancel()

    # ── tiny DB helpers (supabase-py is sync -> run in a thread) ───────────────

    async def _run(self, fn):
        return await asyncio.to_thread(fn)

    async def get_live_run(self, guild_id) -> dict | None:
        res = await self._run(lambda: self.db.table("chain_runs").select("*")
                              .eq("guild_id", str(guild_id)).eq("status", "live").limit(1).execute())
        return res.data[0] if res.data else None

    async def get_run(self, run_id) -> dict | None:
        res = await self._run(lambda: self.db.table("chain_runs").select("*").eq("id", run_id).limit(1).execute())
        return res.data[0] if res.data else None

    async def get_bolt(self, bolt_id) -> dict | None:
        res = await self._run(lambda: self.db.table("chain_bolts").select("*").eq("id", bolt_id).limit(1).execute())
        return res.data[0] if res.data else None

    async def get_bolt_by_message(self, message_id) -> dict | None:
        res = await self._run(lambda: self.db.table("chain_bolts").select("*")
                              .eq("message_id", str(message_id)).limit(1).execute())
        return res.data[0] if res.data else None

    async def get_attempt(self, bolt_id, user_id) -> dict | None:
        res = await self._run(lambda: self.db.table("chain_attempts").select("*")
                              .eq("bolt_id", bolt_id).eq("discord_user_id", str(user_id)).limit(1).execute())
        return res.data[0] if res.data else None

    async def _claim(self, bolt_id, from_status: str, to_status: str, extra: dict | None = None,
                     caught_by: str | None = None) -> bool:
        """Compare-and-set a bolt's status. True only for the single caller that wins."""
        upd = {"status": to_status, **(extra or {})}

        def go():
            q = self.db.table("chain_bolts").update(upd).eq("id", bolt_id).eq("status", from_status)
            if caught_by is not None:
                q = q.eq("caught_by", caught_by)
            return q.execute()

        res = await self._run(go)
        return bool(res.data)

    async def _touch(self, user_id: int):
        """Record that a user was active (drives the active/lurker split)."""
        try:
            await self._run(lambda: self.db.table("chain_prefs").upsert(
                {"discord_user_id": str(user_id), "last_active": _iso(_now())},
                on_conflict="discord_user_id").execute())
        except Exception as e:
            print(f"[chain] touch error: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        now = asyncio.get_event_loop().time()
        if now - self._touch_ts.get(message.author.id, 0) < 600:   # at most every 10 min
            return
        self._touch_ts[message.author.id] = now
        await self._touch(message.author.id)

    # ── Ownership (cache-first: no extra Algorand calls) ───────────────────────

    async def owned_items(self, user_id) -> tuple[list[tuple], str | None]:
        """[(asset_id, label)] for the user's Zappies/Heroes, or ([], error)."""
        wallet = await self._run(lambda: get_wallet(str(user_id)))
        if not wallet:
            return [], "no_wallet"
        result = await verify_wallet_owns_zappy(wallet)
        if result.get("error"):
            return [], "lookup_error"
        items = [(h["asset_id"], f"{h['hero_type']} Hero") for h in result.get("heroes", [])]
        items += [(z["asset_id"], z["name"]) for z in result.get("zappies", [])]
        return items, None

    async def _cache_says_empty(self, wallet: str) -> bool:
        """True only if the cache positively says this wallet owns nothing."""
        try:
            r = await self._run(lambda: _get_cached_wallet_result(wallet))
        except Exception:
            return False
        return bool(r) and not r.get("error") and not r.get("owns")

    # ── Targeting ──────────────────────────────────────────────────────────────

    async def pick_targets(self, guild: discord.Guild, run: dict, exclude: set[str]) -> tuple[list[str], list[str]]:
        """Returns (target_ids, lurker_ids_among_them)."""
        wallets = (await self._run(lambda: self.db.table("wallets")
                                   .select("discord_user_id, wallet_address").execute())).data or []
        prefs = (await self._run(lambda: self.db.table("chain_prefs").select("*").execute())).data or []
        pref_by = {p["discord_user_id"]: p for p in prefs}
        catches = run.get("catches") or {}
        cutoff = _now() - timedelta(days=ACTIVE_DAYS)

        active, lurk = [], []
        for w in wallets:
            uid = w["discord_user_id"]
            if uid in exclude or catches.get(uid, 0) >= MAX_CATCHES_PER_USER:
                continue
            p = pref_by.get(uid, {})
            if p.get("opted_out"):
                continue
            member = guild.get_member(int(uid))
            if not member or member.bot:
                continue
            is_active = bool(p.get("last_active")) and _parse(p["last_active"]) >= cutoff
            (active if is_active else lurk).append((uid, w["wallet_address"]))
        random.shuffle(active)
        random.shuffle(lurk)

        async def take(pool, n):
            out = []
            for uid, wallet in pool:
                if len(out) >= n:
                    break
                if await self._cache_says_empty(wallet):
                    continue
                out.append(uid)
            return out

        want_lurk = 1 if lurk else 0
        want_active = TARGETS_PER_ARC - want_lurk
        a = await take(active, want_active)
        l = await take(lurk, want_lurk + max(0, want_active - len(a)))   # lurkers fill any shortfall
        a += await take([x for x in active if x[0] not in a], max(0, TARGETS_PER_ARC - len(a) - len(l)))
        return a + l, l

    # ── Bolt creation ──────────────────────────────────────────────────────────

    def _timer_hours(self, run: dict) -> int:
        lost = START_GROUNDS - run["grounds_left"]
        return max(MIN_TIMER_HOURS, BASE_TIMER_HOURS - lost)

    def _bolt_embed(self, run: dict, bolt_no: int, ptype: str, targets: list[str], deadline: datetime,
                    last_zappy: int | None, notice: str = "") -> discord.Embed:
        mentions = " ".join(f"<@{t}>" for t in targets)
        desc = (
            f"{notice}"
            f"{mentions}\n\n"
            f"A bolt is looking for a catcher! Press **⚡ Catch** and solve **{PUZZLE_NAMES[ptype]}** "
            f"to keep the chain alive. First to solve it wins the catch."
        )
        e = discord.Embed(title=f"⚡ Chain Lightning — Link {run['chain_count']}", description=desc, color=GOLD)
        e.add_field(name="Chain", value=f"**{run['chain_count']}**", inline=True)
        e.add_field(name="Grounds", value=grounds_bar(run["grounds_left"]), inline=True)
        e.add_field(name="Time left", value=f"<t:{int(deadline.timestamp())}:R>", inline=True)
        if last_zappy:
            e.add_field(name="Last Zappy", value=zappy_label(last_zappy), inline=False)
            url = zappy_image_url(last_zappy)
            if url:
                e.set_thumbnail(url=url)
        e.set_footer(text="Only tagged players can Catch · Everyone else can 📣 Nudge")
        return e

    async def _get_channel(self, run: dict):
        ch = self.bot.get_channel(int(run["channel_id"]))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(run["channel_id"]))
            except Exception:
                ch = None
        return ch

    async def post_bolt(self, run: dict, ptype: str, exclude: set[str], last_zappy: int | None,
                        notice: str = "") -> bool:
        """Pick targets, post the card, store the bolt. False if nobody can be tagged."""
        guild = self.bot.get_guild(int(run["guild_id"]))
        channel = await self._get_channel(run)
        if not guild or not channel:
            return False

        targets, lurkers = await self.pick_targets(guild, run, exclude)
        if not targets:                                    # relax: only the last catcher is excluded
            targets, lurkers = await self.pick_targets(guild, run, {run.get("last_catcher") or ""})
        if not targets:
            return False

        # Bolt numbers count every arc (including re-arcs), separate from the chain count
        last = await self._run(lambda: self.db.table("chain_bolts").select("bolt_no")
                               .eq("run_id", run["id"]).order("bolt_no", desc=True).limit(1).execute())
        bolt_no = (last.data[0]["bolt_no"] + 1) if last.data else 1
        deadline = _now() + timedelta(hours=self._timer_hours(run))

        row = (await self._run(lambda: self.db.table("chain_bolts").insert({
            "run_id": run["id"], "bolt_no": bolt_no, "targets": targets, "lurkers": lurkers,
            "channel_id": run["channel_id"], "puzzle_type": ptype, "deadline": _iso(deadline),
        }).execute())).data[0]

        embed = self._bolt_embed(run, bolt_no, ptype, targets, deadline, last_zappy, notice)
        try:
            msg = await channel.send(
                content=" ".join(f"<@{t}>" for t in targets),
                embed=embed, view=ChainBoltView(self),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except Exception as e:
            print(f"[chain] could not post bolt: {e}")
            await self._run(lambda: self.db.table("chain_bolts").delete().eq("id", row["id"]).execute())
            return False
        await self._run(lambda: self.db.table("chain_bolts").update({"message_id": str(msg.id)})
                        .eq("id", row["id"]).execute())

        # Lurkers often miss channel pings — send them a DM too (silently ignore closed DMs)
        for uid in lurkers:
            try:
                user = await self.bot.fetch_user(int(uid))
                await user.send(f"⚡ A Chain Lightning bolt just landed on **you**! Catch it here: {msg.jump_url}")
            except Exception:
                pass
        return True

    async def _retire_card(self, bolt: dict, text: str, color: int = 0x555555):
        """Disable the old bolt card and say what happened to it."""
        try:
            ch = self.bot.get_channel(int(bolt["channel_id"])) or await self.bot.fetch_channel(int(bolt["channel_id"]))
            msg = await ch.fetch_message(int(bolt["message_id"]))
            e = msg.embeds[0] if msg.embeds else discord.Embed(title="⚡ Chain Lightning")
            e.color = color
            e.description = text
            await msg.edit(embed=e, view=None)
        except Exception as ex:
            print(f"[chain] retire card error: {ex}")

    # ── Payouts ────────────────────────────────────────────────────────────────

    async def _is_racer(self, user_id) -> bool:
        res = await self._run(lambda: self.db.table("zappy_racers").select("discord_user_id")
                              .eq("discord_user_id", str(user_id)).limit(1).execute())
        return bool(res.data)

    async def pay(self, user_id, amount: int, reason: str, run_id: int) -> bool:
        """Credit ZAPP through the shared ledger. False if the player isn't registered."""
        if amount <= 0 or not await self._is_racer(user_id):
            return False
        try:
            r = await self._run(lambda: gp_accounting.credit(
                self.db, str(user_id), "ZAPP", amount, reason, ref_id=f"chain:{run_id}"))
            return bool(r.get("ok"))
        except Exception as e:
            print(f"[chain] pay error: {e}")
            return False

    # ── Chain end ──────────────────────────────────────────────────────────────

    async def end_run(self, run: dict, why: str):
        await self._run(lambda: self.db.table("chain_runs").update(
            {"status": "ended", "ended_at": _iso(_now())}).eq("id", run["id"]).execute())
        channel = await self._get_channel(run)
        if not channel:
            return

        best = await self._run(lambda: self.db.table("chain_runs").select("chain_count")
                               .eq("status", "ended").neq("id", run["id"])
                               .order("chain_count", desc=True).limit(1).execute())
        prev_best = best.data[0]["chain_count"] if best.data else 0
        count = run["chain_count"]
        catches = run.get("catches") or {}

        e = discord.Embed(title="⚡ Chain Lightning — Chain Over", color=0xFF4040,
                          description=f"{why}\n\nFinal chain: **{count}**")
        if catches:
            top = sorted(catches.items(), key=lambda kv: -kv[1])
            e.add_field(name=f"Catchers ({len(catches)})",
                        value="\n".join(f"<@{u}> × {n}" for u, n in top[:15]), inline=False)
        if count > prev_best and count > 1:
            e.add_field(name="🏆 New record!", value=f"Previous best: {prev_best}", inline=False)
        e.set_footer(text=f"Start another with /arc · Next chain opens in {START_COOLDOWN_HOURS}h")
        await channel.send(embed=e)

    # ── Core: advance / burn a Ground ──────────────────────────────────────────

    async def advance_chain(self, bolt: dict, user_id: str, asset_id: int) -> dict | None:
        """Catcher passed with `asset_id`. Returns a summary dict, or None if the bolt moved on."""
        async with self._lock:
            if not await self._claim(bolt["id"], "caught", "passed", caught_by=user_id):
                return None
            run = await self.get_run(bolt["run_id"])
            if not run or run["status"] != "live":
                return None

            att = await self.get_attempt(bolt["id"], user_id)
            was_lurker = bool(att and att["puzzle"].get("lurker"))
            hero = HERO_ASSET_IDS.get(asset_id)

            catches = dict(run.get("catches") or {})
            catches[user_id] = catches.get(user_id, 0) + 1
            used = list(run.get("used_zappy_ids") or []) + [asset_id]
            run["chain_count"] += 1
            run["catches"], run["used_zappy_ids"], run["last_catcher"] = catches, used, user_id
            await self._run(lambda: self.db.table("chain_runs").update({
                "chain_count": run["chain_count"], "catches": catches,
                "used_zappy_ids": used, "last_catcher": user_id}).eq("id", run["id"]).execute())
            await self._run(lambda: self.db.table("chain_links").insert({
                "run_id": run["id"], "link_no": run["chain_count"], "caught_by": user_id,
                "zappy_id": asset_id, "hero": hero, "puzzle_type": bolt["puzzle_type"]}).execute())

            bonus = LURKER_CATCH_BONUS_ZAPP if was_lurker else CATCH_BONUS_ZAPP
            paid = await self.pay(user_id, bonus, "chain_lightning_catch", run["id"])

            milestone_lines = await self._pay_milestones(run)

            await self._retire_card(
                bolt, f"✅ Caught by <@{user_id}> with **{zappy_label(asset_id)}**!", color=0x2ECC71)

            ptype = puzzles.puzzle_type_for_zappy(hero)
            notice = f"✅ <@{user_id}> caught it with **{zappy_label(asset_id)}**!\n"
            if milestone_lines:
                notice += "\n".join(milestone_lines) + "\n"
            notice += "\n"
            posted = await self.post_bolt(
                run, ptype, exclude={user_id} | set(bolt["targets"]), last_zappy=asset_id, notice=notice)
            if not posted:
                await self.end_run(run, "The bolt couldn't find anyone else to land on.")

            return {"count": run["chain_count"], "bonus": bonus, "paid": paid, "ended": not posted}

    async def _pay_milestones(self, run: dict) -> list[str]:
        paid_ms = list(run.get("paid_milestones") or [])
        catches = run.get("catches") or {}
        lines = []
        for ms, amount in sorted(MILESTONE_ZAPP.items()):
            if ms > run["chain_count"] or ms in paid_ms or len(catches) < MILESTONE_MIN_CATCHERS:
                continue
            for uid in catches:
                await self.pay(uid, amount, f"chain_lightning_milestone_{ms}", run["id"])
            paid_ms.append(ms)
            lines.append(f"🎉 **Chain {ms}!** Every catcher earns **{amount} ZAPP**.")
        if lines:
            run["paid_milestones"] = paid_ms
            await self._run(lambda: self.db.table("chain_runs").update(
                {"paid_milestones": paid_ms}).eq("id", run["id"]).execute())
        return lines

    async def _rearc(self, bolt: dict, burn: bool, status: str, reason: str):
        """Retire a bolt and send the chain on to new targets, optionally burning a Ground."""
        async with self._lock:
            if not await self._claim(bolt["id"], "open", status):
                return
            run = await self.get_run(bolt["run_id"])
            if not run or run["status"] != "live":
                return
            if burn:
                run["grounds_left"] -= 1
                await self._run(lambda: self.db.table("chain_runs").update(
                    {"grounds_left": run["grounds_left"]}).eq("id", run["id"]).execute())
            await self._retire_card(bolt, f"💨 {reason}")
            if run["grounds_left"] <= 0:
                await self.end_run(run, "The chain ran out of Grounds.")
                return
            lasts = await self._run(lambda: self.db.table("chain_links").select("zappy_id")
                                    .eq("run_id", run["id"]).order("link_no", desc=True).limit(1).execute())
            last_zappy = lasts.data[0]["zappy_id"] if lasts.data else None
            lost = "A Ground was lost! " if burn else ""
            ok = await self.post_bolt(
                run, random.choice(puzzles.PUZZLE_TYPES), exclude=set(bolt["targets"]),
                last_zappy=last_zappy, notice=f"💨 {reason} {lost}\n\n")
            if not ok:
                await self.end_run(run, "The bolt couldn't find anyone to land on.")

    # ── Interaction handlers ───────────────────────────────────────────────────

    async def handle_catch(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = str(interaction.user.id)
        await self._touch(interaction.user.id)

        bolt = await self.get_bolt_by_message(interaction.message.id)
        if not bolt or bolt["status"] != "open":
            return await interaction.followup.send("⚡ This bolt has already moved on.", ephemeral=True)
        if uid not in bolt["targets"]:
            return await interaction.followup.send(
                "This bolt wasn't aimed at you. Use **📣 Nudge** to help wake up the tagged players!",
                ephemeral=True)

        att = await self.get_attempt(bolt["id"], uid)
        if att and att["wrong_count"] >= TRIES_PER_BOLT:
            return await interaction.followup.send("You've used your tries on this bolt.", ephemeral=True)

        items, err = await self.owned_items(uid)
        if err == "no_wallet":
            return await interaction.followup.send("Link your wallet first with `/link`.", ephemeral=True)
        if err:
            return await interaction.followup.send("Couldn't check your wallet right now — try again soon.", ephemeral=True)
        if not items:
            return await interaction.followup.send("You need at least one Zappy to catch a bolt.", ephemeral=True)

        # Re-use the same puzzle if they already opened one (no free rerolls)
        if att:
            puzzle = att["puzzle"]
        else:
            run = await self.get_run(bolt["run_id"])
            lurker = uid in (bolt.get("lurkers") or [])
            difficulty = "easy" if lurker else ("hard" if run["chain_count"] >= HARD_AFTER_LINK else "medium")
            puzzle = await puzzles.build_puzzle(bolt["puzzle_type"], difficulty, self.db)
            puzzle["lurker"] = lurker
            await self._run(lambda: self.db.table("chain_attempts").upsert(
                {"bolt_id": bolt["id"], "discord_user_id": uid, "puzzle": puzzle, "wrong_count": 0},
                on_conflict="bolt_id,discord_user_id", ignore_duplicates=True).execute())
            att = await self.get_attempt(bolt["id"], uid)      # whichever row won the race
            puzzle = att["puzzle"]

        labels, option_text = puzzles.answer_labels(puzzle)
        question = puzzle["question"] + (f"\n\n{option_text}" if option_text else "")
        e = discord.Embed(title=puzzle.get("title", "⚡ Puzzle"), description=question, color=GOLD)
        e.set_footer(text=puzzle.get("footer") or f"{TRIES_PER_BOLT} tries · first correct answer wins the catch")

        file = None
        buf = await asyncio.to_thread(puzzles.render_puzzle_image, puzzle)
        if buf:
            file = discord.File(buf, filename="puzzle.png")
            e.set_image(url="attachment://puzzle.png")

        view = PuzzleAnswerView(self, bolt["id"], labels)
        kwargs = {"embed": e, "view": view, "ephemeral": True}
        if file:
            kwargs["file"] = file
        await interaction.followup.send(**kwargs)

    async def handle_answer(self, interaction: discord.Interaction, view: PuzzleAnswerView, button: AnswerButton):
        uid = str(interaction.user.id)
        bolt = await self.get_bolt(view.bolt_id)
        att = await self.get_attempt(view.bolt_id, uid)
        if not bolt or not att:
            return await interaction.response.edit_message(content="⚡ This bolt has moved on.", view=None)
        if bolt["status"] != "open":
            return await interaction.response.edit_message(content="⚡ Too slow — someone else caught it first!", view=None)

        if button.idx == att["puzzle"]["correct"]:
            won = await self._claim(
                bolt["id"], "open", "caught",
                extra={"caught_by": uid, "caught_at": _iso(_now()),
                       "pass_deadline": _iso(_now() + timedelta(minutes=PASS_WINDOW_MINUTES))})
            if not won:
                return await interaction.response.edit_message(content="⚡ Too slow — someone else caught it first!", view=None)
            return await self.show_pass_picker(interaction, bolt, uid)

        wrong = att["wrong_count"] + 1
        await self._run(lambda: self.db.table("chain_attempts").update({"wrong_count": wrong})
                        .eq("bolt_id", bolt["id"]).eq("discord_user_id", uid).execute())
        if wrong >= TRIES_PER_BOLT:
            await interaction.response.edit_message(
                content="❌ That's both tries — you're out for this bolt. The chain keeps going!",
                embed=None, view=None, attachments=[])
            return await self._fizzle_if_all_locked(bolt)
        button.disabled = True
        button.style = discord.ButtonStyle.danger
        await interaction.response.edit_message(
            content=f"❌ Not that one — **{TRIES_PER_BOLT - wrong}** try left.", view=view)

    async def _fizzle_if_all_locked(self, bolt: dict):
        rows = (await self._run(lambda: self.db.table("chain_attempts").select("discord_user_id, wrong_count")
                                .eq("bolt_id", bolt["id"]).execute())).data or []
        locked = {r["discord_user_id"] for r in rows if r["wrong_count"] >= TRIES_PER_BOLT}
        if set(bolt["targets"]) <= locked:
            await self._rearc(bolt, burn=False, status="fizzled",
                              reason="Everyone tagged missed the puzzle, so the bolt jumped on.")

    async def show_pass_picker(self, interaction: discord.Interaction, bolt: dict, uid: str):
        run = await self.get_run(bolt["run_id"])
        used = set(run.get("used_zappy_ids") or [])
        items, err = await self.owned_items(uid)
        items = [(a, l) for a, l in items if a not in used]
        if not items:
            # Nothing unused left to pass with — treat as a miss for this player
            await interaction.response.edit_message(
                content="You caught it, but every Zappy you own has already been used in this chain! "
                        "The bolt will auto-pass shortly.", embed=None, view=None, attachments=[])
            return
        e = discord.Embed(
            title="✅ Caught it!",
            description="Now **pass the bolt** — choose a Zappy to send it on.\n"
                        "Its Hero or type decides the next puzzle.",
            color=0x2ECC71)
        e.set_footer(text=f"Pick within {PASS_WINDOW_MINUTES} minutes or one will be chosen for you.")

        async def on_pick(i: discord.Interaction, asset_id: int):
            await self.handle_pass(i, bolt["id"], uid, asset_id)

        await interaction.response.edit_message(
            embed=e, view=ZappyPickView(self, interaction.user.id, items, on_pick), attachments=[], content=None)

    async def handle_pass(self, interaction: discord.Interaction, bolt_id: int, uid: str, asset_id: int):
        await interaction.response.defer()
        bolt = await self.get_bolt(bolt_id)
        if not bolt or bolt["status"] != "caught" or bolt["caught_by"] != uid:
            return await interaction.edit_original_response(content="This bolt has already been passed.", embed=None, view=None)
        items, _ = await self.owned_items(uid)
        if asset_id not in {a for a, _ in items}:
            return await interaction.edit_original_response(content="You don't hold that Zappy right now.", embed=None, view=None)
        run = await self.get_run(bolt["run_id"])
        if asset_id in set(run.get("used_zappy_ids") or []):
            return await interaction.edit_original_response(content="That Zappy was already used in this chain — pick another.", view=None)

        res = await self.advance_chain(bolt, uid, asset_id)
        if not res:
            return await interaction.edit_original_response(content="This bolt has already moved on.", embed=None, view=None)

        if res["paid"]:
            reward = f"🪙 **+{res['bonus']} ZAPP** added to your balance."
        else:
            reward = (f"You'd have earned **{res['bonus']} ZAPP**, but you aren't registered for Grand Prix yet. "
                      "Use `/link` and `/gpregister` to start earning.")
        e = discord.Embed(title="⚡ Bolt passed!", color=GOLD,
                          description=f"You sent it on with **{zappy_label(asset_id)}**.\n"
                                      f"Chain is now at **{res['count']}**.\n\n{reward}")
        await interaction.edit_original_response(embed=e, view=None, content=None)

    async def handle_nudge(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid = str(interaction.user.id)
        await self._touch(interaction.user.id)
        bolt = await self.get_bolt_by_message(interaction.message.id)
        if not bolt or bolt["status"] != "open":
            return await interaction.followup.send("⚡ This bolt has already moved on.", ephemeral=True)
        if uid in bolt["targets"]:
            return await interaction.followup.send("You're tagged — press **Catch**!", ephemeral=True)
        if uid in (bolt.get("nudged") or []):
            return await interaction.followup.send("You already nudged for this bolt.", ephemeral=True)
        nudged = list(bolt.get("nudged") or []) + [uid]
        await self._run(lambda: self.db.table("chain_bolts").update({"nudged": nudged}).eq("id", bolt["id"]).execute())
        sent = 0
        for t in bolt["targets"]:
            try:
                user = await self.bot.fetch_user(int(t))
                await user.send(f"📣 **{interaction.user.display_name}** says a Chain Lightning bolt "
                                f"is waiting for you! {interaction.message.jump_url}")
                sent += 1
            except Exception:
                pass
        await interaction.followup.send(f"📣 Nudge sent to {sent} tagged player{'s' if sent != 1 else ''}!", ephemeral=True)

    # ── Background worker ──────────────────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def worker(self):
        try:
            now = _iso(_now())
            expired = (await self._run(lambda: self.db.table("chain_bolts").select("*")
                       .eq("status", "open").lt("deadline", now).execute())).data or []
            for bolt in expired:
                await self._rearc(bolt, burn=True, status="expired",
                                  reason="Nobody caught the bolt in time.")

            stale = (await self._run(lambda: self.db.table("chain_bolts").select("*")
                     .eq("status", "caught").lt("pass_deadline", now).execute())).data or []
            for bolt in stale:
                await self._auto_pass(bolt)

            if asyncio.get_event_loop().time() - self._last_refill > 6 * 3600:
                self._last_refill = asyncio.get_event_loop().time()
                if await puzzles.pool_size(self.db) < POOL_LOW_WATER:
                    n = await puzzles.refill_trivia_pool(self.db, batches=3)
                    print(f"[chain] trivia pool refill added {n} questions")
        except Exception as e:
            print(f"[chain] worker error: {e}")

    @worker.before_loop
    async def _before_worker(self):
        await self.bot.wait_until_ready()

    async def _auto_pass(self, bolt: dict):
        """The catcher never picked a Zappy — pass with a random unused one so the chain doesn't stall."""
        run = await self.get_run(bolt["run_id"])
        if not run or run["status"] != "live":
            return
        used = set(run.get("used_zappy_ids") or [])
        items, _ = await self.owned_items(bolt["caught_by"])
        choices = [a for a, _ in items if a not in used]
        if choices:
            await self.advance_chain(bolt, bolt["caught_by"], random.choice(choices))
        else:
            # Convert to a normal miss: reopen briefly so _rearc can claim it
            await self._claim(bolt["id"], "caught", "open")
            await self._rearc(bolt, burn=True, status="expired",
                              reason="The catcher had no Zappies left to pass with.")

    # ── Slash commands ─────────────────────────────────────────────────────────

    @app_commands.command(name="arc", description="Fire a Chain Lightning bolt with one of your Zappies.")
    async def arc(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            return await interaction.followup.send("Use this in the server.", ephemeral=True)

        if await self.get_live_run(interaction.guild.id):
            return await interaction.followup.send("A chain is already live — check `/chain` and jump in!", ephemeral=True)

        if not interaction.user.guild_permissions.manage_guild:
            last = (await self._run(lambda: self.db.table("chain_runs").select("started_at")
                    .eq("guild_id", str(interaction.guild.id)).order("started_at", desc=True).limit(1).execute())).data
            if last:
                ready = _parse(last[0]["started_at"]) + timedelta(hours=START_COOLDOWN_HOURS)
                if ready > _now():
                    return await interaction.followup.send(
                        f"The next chain can start <t:{int(ready.timestamp())}:R>.", ephemeral=True)

        items, err = await self.owned_items(interaction.user.id)
        if err == "no_wallet":
            return await interaction.followup.send("Link your wallet first with `/link`.", ephemeral=True)
        if err:
            return await interaction.followup.send("Couldn't check your wallet right now — try again soon.", ephemeral=True)
        if not items:
            return await interaction.followup.send("You need at least one Zappy to start a chain.", ephemeral=True)

        async def on_pick(i: discord.Interaction, asset_id: int):
            await self._start_chain(i, asset_id)

        e = discord.Embed(title="⚡ Start a chain",
                          description="Choose the Zappy that fires the first bolt.\n"
                                      "Its Hero or type decides the first puzzle.", color=GOLD)
        await interaction.followup.send(
            embed=e, view=ZappyPickView(self, interaction.user.id, items, on_pick), ephemeral=True)

    async def _start_chain(self, interaction: discord.Interaction, asset_id: int):
        await interaction.response.defer()
        uid = str(interaction.user.id)
        items, _ = await self.owned_items(uid)
        if asset_id not in {a for a, _ in items}:
            return await interaction.edit_original_response(content="You don't hold that Zappy right now.", embed=None, view=None)

        channel = interaction.guild.get_channel(CHAIN_CHANNEL_ID) if CHAIN_CHANNEL_ID else interaction.channel
        channel = channel or interaction.channel
        try:
            run = (await self._run(lambda: self.db.table("chain_runs").insert({
                "guild_id": str(interaction.guild.id), "channel_id": str(channel.id),
                "grounds_left": START_GROUNDS, "chain_count": 1, "started_by": uid,
                "used_zappy_ids": [asset_id], "catches": {}, "last_catcher": uid,
            }).execute())).data[0]
        except Exception:
            # The one-live-chain unique index fired — someone else started first
            return await interaction.edit_original_response(content="A chain just started — jump in with `/chain`!", embed=None, view=None)

        await self._touch(interaction.user.id)
        hero = HERO_ASSET_IDS.get(asset_id)
        ptype = puzzles.puzzle_type_for_zappy(hero)
        notice = f"🌩️ <@{uid}> fired the first bolt with **{zappy_label(asset_id)}**!\n\n"
        ok = await self.post_bolt(run, ptype, exclude={uid}, last_zappy=asset_id, notice=notice)
        if not ok:
            await self._run(lambda: self.db.table("chain_runs").delete().eq("id", run["id"]).execute())
            return await interaction.edit_original_response(
                content="Couldn't find anyone to tag yet — not enough linked players in the server.",
                embed=None, view=None)
        await interaction.edit_original_response(
            content=f"⚡ Bolt fired in {channel.mention}!", embed=None, view=None)

    @app_commands.command(name="chain", description="See the live Chain Lightning chain.")
    async def chain(self, interaction: discord.Interaction):
        run = await self.get_live_run(interaction.guild.id) if interaction.guild else None
        if not run:
            return await interaction.response.send_message("No chain is live. Start one with `/arc`!", ephemeral=True)
        bolts = (await self._run(lambda: self.db.table("chain_bolts").select("*").eq("run_id", run["id"])
                 .eq("status", "open").limit(1).execute())).data
        e = discord.Embed(title="⚡ Chain Lightning — Live", color=GOLD)
        e.add_field(name="Chain", value=f"**{run['chain_count']}**", inline=True)
        e.add_field(name="Grounds", value=grounds_bar(run["grounds_left"]), inline=True)
        e.add_field(name="Catchers", value=str(len(run.get("catches") or {})), inline=True)
        if bolts:
            b = bolts[0]
            e.add_field(name="Waiting on",
                        value=" ".join(f"<@{t}>" for t in b["targets"]) +
                              f"\n<t:{int(_parse(b['deadline']).timestamp())}:R>", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="chainboard", description="Longest Chain Lightning chains.")
    async def chainboard(self, interaction: discord.Interaction):
        rows = (await self._run(lambda: self.db.table("chain_runs").select("chain_count, ended_at, catches")
                .eq("status", "ended").order("chain_count", desc=True).limit(10).execute())).data or []
        if not rows:
            return await interaction.response.send_message("No finished chains yet — start one with `/arc`!", ephemeral=True)
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            medal = medals[i] if i < 3 else f"**{i + 1}.**"
            date = (r["ended_at"] or "")[:10] or "—"
            lines.append(f"{medal} **{r['chain_count']}** links · {len(r.get('catches') or {})} catchers · {date}")
        await interaction.response.send_message(
            embed=discord.Embed(title="⚡ Chain Lightning — Top 10", description="\n".join(lines), color=GOLD),
            ephemeral=True)

    @app_commands.command(name="chainoptout", description="Toggle whether Chain Lightning bolts can be aimed at you.")
    async def chainoptout(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        cur = (await self._run(lambda: self.db.table("chain_prefs").select("opted_out")
               .eq("discord_user_id", uid).limit(1).execute())).data
        new = not (cur and cur[0].get("opted_out"))
        await self._run(lambda: self.db.table("chain_prefs").upsert(
            {"discord_user_id": uid, "opted_out": new}, on_conflict="discord_user_id").execute())
        await interaction.response.send_message(
            "🔕 You won't be tagged in chains anymore." if new else "⚡ You're back in the chain pool!",
            ephemeral=True)

    @app_commands.command(name="chainrefill", description="(Admin) Refill the OpenTDB trivia pool now.")
    @app_commands.default_permissions(manage_guild=True)
    async def chainrefill(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        before = await puzzles.pool_size(self.db)
        added = await puzzles.refill_trivia_pool(self.db, batches=3)
        await interaction.followup.send(f"Trivia pool: {before} → {before + added} questions (+{added}).", ephemeral=True)

    @app_commands.command(name="chainend", description="(Admin) End the live chain.")
    @app_commands.default_permissions(manage_guild=True)
    async def chainend(self, interaction: discord.Interaction):
        run = await self.get_live_run(interaction.guild.id)
        if not run:
            return await interaction.response.send_message("No live chain.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            await self._run(lambda: self.db.table("chain_bolts").update({"status": "expired"})
                            .eq("run_id", run["id"]).in_("status", ["open", "caught"]).execute())
            await self.end_run(run, "An admin ended the chain.")
        await interaction.followup.send("Chain ended.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChainLightningCog(bot, bot.supabase))

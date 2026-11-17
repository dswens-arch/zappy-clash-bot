"""
zappy_news_cog.py
-----------------
Zappy Agent Wire — autonomous Zappy AGENTS. Random Zappies from the WHOLE
collection (plus the 13 Heroes) take turns on duty, each filing a short report
in the main channel on a steady, randomized drip. No holder input, no payouts,
no LLM: the facts come from the bot's own data (or free RSS / CoinGecko) and
each agent wraps them in a templated voice.

When a report is about one specific Zappy (the Clash winner, a record holder, a
prize) the card features THAT Zappy and shows the filing agent as a badge.

Segments (each returns None when it has nothing fresh to say, and the cog just
tries another):
    clash_recap      last 24h of Clash results: upsets, bracket winner, Hero signature
    winners_circle   recently claimed NFT prizes
    market_minute    ALGO price + 24h change
    headlines        one tech / AI / sports headline (Google News RSS + Hacker News)
    chain_report     Chain Lightning: live chain, weekly best, hot hands
    voltball_scores  most recent Voltball results (only while a season is running)
    office_gossip    Spark Office duels, seatings, demotions
    hall_of_records  all-time Zappy records as sports-desk trivia
    breaking         event-triggered (not scheduled): big upsets, chain records /
                     milestones, fresh NFT drops

Drip: every NEWS_MIN_MINUTES..NEWS_MAX_MINUTES (random), skipping quiet hours,
capped per day. Agents come from a persistent shuffle bag, so every Zappy
takes a shift before anyone repeats.

Tables: see zappy_news_setup.sql. Loaded from bot.py like the other cogs.
All Supabase calls are sync -> run in a thread.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import zappy_news_templates as T
from algorand_lookup import HERO_ASSET_IDS, HERO_IMAGES
from zappy_collection import ZAPPY_COLLECTION
from zappy_news_card import render_news_card
from zappy_news_feeds import NewsFeeds
from zappy_news_sparks import spark_color, spark_image_url

# ── Config ─────────────────────────────────────────────────────────────────────

NEWS_CHANNEL_ID = int(os.environ.get("NEWS_CHANNEL_ID")
                      or os.environ.get("HOLDER_CHANNEL_ID")
                      or 1314066280592052244)
NEWS_ENABLED_DEFAULT = os.environ.get("NEWS_ENABLED", "1") not in ("0", "false", "False", "")
NEWS_TZ        = ZoneInfo(os.environ.get("NEWS_TZ", "America/Chicago"))
QUIET_START    = int(os.environ.get("NEWS_QUIET_START", 23))     # local hour, inclusive
QUIET_END      = int(os.environ.get("NEWS_QUIET_END", 7))        # local hour, exclusive
MIN_MINUTES    = int(os.environ.get("NEWS_MIN_MINUTES", 45))
MAX_MINUTES    = int(os.environ.get("NEWS_MAX_MINUTES", 150))
DAILY_CAP      = int(os.environ.get("NEWS_DAILY_CAP", 9))        # scheduled broadcasts / local day
BREAKING_CAP   = int(os.environ.get("NEWS_BREAKING_CAP", 5))     # breaking / local day
BREAKING_GAP_MIN = int(os.environ.get("NEWS_BREAKING_GAP_MIN", 20))
BREAKING_UPSET_ROUND = int(os.environ.get("NEWS_BREAKING_UPSET_ROUND", 3))
BREAKING_LOOKBACK_H  = 3          # never report events older than this
BREAKING_POLL_MIN    = 3
STARTUP_DELAY_MIN    = (8, 30)    # first scheduled broadcast after a (re)start
BREAKING_BOOT_QUIET_MIN = 5       # breaking poller stays silent this long after a (re)start

# segment -> (weight, min minutes between two posts of that segment)
SEGMENTS: dict[str, tuple[int, int]] = {
    "clash_recap":     (14, 180),
    "winners_circle":  (8,  240),
    "market_minute":   (11, 240),
    "headlines":       (28, 30),
    "chain_report":    (10, 360),
    "voltball_scores": (10, 240),
    "office_gossip":   (9,  180),
    "hall_of_records": (8,  300),
}
HEADLINE_KIND_WEIGHTS = {"tech": 4, "ai": 4, "sports": 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mention(uid) -> str:
    return f"<@{uid}>" if uid else "someone"


# ── Anchors ────────────────────────────────────────────────────────────────────

@dataclass
class Anchor:
    asset_id: int
    name: str
    hero: str | None = None
    traits: dict = field(default_factory=dict)
    image_path: str | None = None
    image_url: str | None = None

    @property
    def voice(self) -> str:
        return T.voice_for(self.asset_id, self.hero)


def build_pool() -> dict[int, Anchor]:
    pool: dict[int, Anchor] = {}
    for aid, e in ZAPPY_COLLECTION.items():
        pool[aid] = Anchor(aid, e.get("name", f"Zappy ASA {aid}"), None, e,
                           f"zappy-images-full/{aid}.jpg", e.get("image_url"))
    for aid, hero in HERO_ASSET_IDS.items():
        if aid in pool:
            pool[aid].hero, pool[aid].name = hero, f"{hero} Hero"
        else:
            pool[aid] = Anchor(aid, f"{hero} Hero", hero, {},
                               f"zappy-images-full/hero_{hero}.jpg", HERO_IMAGES.get(hero))
    return pool


# ── Cog ────────────────────────────────────────────────────────────────────────

class ZappyNewsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db):
        self.bot = bot
        self.db = db
        self.pool = build_pool()
        self.feeds = NewsFeeds()
        self.enabled = NEWS_ENABLED_DEFAULT
        self.seen: set[str] = set()
        self.bag_used: set[int] = set()
        self.last_posted: dict[str, datetime] = {}
        self.last_segment: str | None = None
        self.last_breaking_at: datetime | None = None
        self.next_post_at: datetime | None = None
        self.boot_quiet_until = _now() + timedelta(minutes=BREAKING_BOOT_QUIET_MIN)
        self._post_lock = asyncio.Lock()
        self._sparks: dict[int, tuple[datetime, dict]] = {}
        self._registry = {
            "clash_recap":     self.seg_clash_recap,
            "winners_circle":  self.seg_winners_circle,
            "market_minute":   self.seg_market_minute,
            "headlines":       self.seg_headlines,
            "chain_report":    self.seg_chain_report,
            "voltball_scores": self.seg_voltball_scores,
            "office_gossip":   self.seg_office_gossip,
            "hall_of_records": self.seg_hall_of_records,
        }

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def cog_load(self):
        await self._load_state()
        lo, hi = STARTUP_DELAY_MIN
        self.next_post_at = _now() + timedelta(minutes=random.randint(lo, hi))
        if not self.scheduler.is_running():
            self.scheduler.start()
        if not self.breaking_watch.is_running():
            self.breaking_watch.start()

    async def cog_unload(self):
        self.scheduler.cancel()
        self.breaking_watch.cancel()

    async def _q(self, fn):
        return await asyncio.to_thread(fn)

    async def _fetch_all(self, table: str, cols: str, *, since_col: str | None = None, since: str | None = None):
        """Page through a table 1000 rows at a time (Supabase's per-request cap)."""
        out, start, page = [], 0, 1000
        while True:
            def run(a=start):
                q = self.db.table(table).select(cols)
                if since_col and since:
                    q = q.gte(since_col, since)
                return q.range(a, a + page - 1).execute()
            batch = (await self._q(run)).data or []
            out.extend(batch)
            if len(batch) < page:
                return out
            start += page

    async def _load_state(self):
        try:
            since = _iso(_now() - timedelta(days=30))
            rows = await self._fetch_all("news_seen", "key", since_col="created_at", since=since)
            self.seen = {r["key"] for r in rows}
            bag = await self._fetch_all("news_anchor_bag", "asset_id")
            self.bag_used = {int(r["asset_id"]) for r in bag}
            logs = (await self._q(lambda: self.db.table("news_log").select("segment,posted_at,breaking")
                                  .gte("posted_at", _iso(_now() - timedelta(days=2)))
                                  .order("posted_at", desc=True).limit(200).execute())).data or []
            for r in logs:
                ts = _parse(r["posted_at"])
                if not ts:
                    continue
                if r.get("breaking"):
                    if not self.last_breaking_at or ts > self.last_breaking_at:
                        self.last_breaking_at = ts
                elif r["segment"] not in self.last_posted:
                    self.last_posted[r["segment"]] = ts
            if logs:
                latest = next((r for r in logs if not r.get("breaking")), None)
                self.last_segment = latest["segment"] if latest else None
            # prune very old dedupe keys now and then
            await self._q(lambda: self.db.table("news_seen").delete()
                          .lt("created_at", _iso(_now() - timedelta(days=45))).execute())
            print(f"[news] state loaded: {len(self.seen)} seen keys, {len(self.bag_used)}/{len(self.pool)} anchors used")
        except Exception as e:
            print(f"[news] state load failed (continuing with empty state): {e}")

    async def mark_seen(self, *keys: str):
        fresh = [k for k in keys if k and k not in self.seen]
        if not fresh:
            return
        self.seen.update(fresh)
        try:
            await self._q(lambda: self.db.table("news_seen")
                          .upsert([{"key": k} for k in fresh], on_conflict="key").execute())
        except Exception as e:
            print(f"[news] mark_seen failed: {e}")

    # ── anchors (shuffle bag) ──────────────────────────────────────────────────

    async def pick_anchor(self) -> Anchor:
        candidates = [a for a in self.pool if a not in self.bag_used]
        if not candidates:
            self.bag_used.clear()
            try:
                await self._q(lambda: self.db.table("news_anchor_bag").delete().gte("asset_id", 0).execute())
            except Exception as e:
                print(f"[news] bag reset failed: {e}")
            candidates = list(self.pool)
            print("[news] anchor bag refilled — new cycle")
        aid = random.choice(candidates)
        self.bag_used.add(aid)
        try:
            await self._q(lambda: self.db.table("news_anchor_bag")
                          .upsert({"asset_id": aid}, on_conflict="asset_id").execute())
        except Exception as e:
            print(f"[news] bag save failed: {e}")
        return self.pool[aid]

    def zname(self, asset_id) -> str:
        try:
            a = self.pool.get(int(asset_id))
        except (TypeError, ValueError):
            a = None
        return a.name if a else f"Zappy ASA {asset_id}"

    async def spark_info(self, asa, fallback: dict | None = None) -> dict:
        """{'name','spark_type','tier'} for a Spark ASA (spark_holdings, else the given seat row)."""
        hit = self._sparks.get(asa)
        if hit and _now() - hit[0] < timedelta(minutes=10):
            return hit[1]
        info = None
        try:
            r = (await self._q(lambda: self.db.table("spark_holdings").select("asset_id,name,spark_type,tier")
                               .eq("asset_id", asa).limit(1).execute())).data
            if r:
                info = {"name": r[0].get("name"), "spark_type": r[0].get("spark_type"), "tier": r[0].get("tier")}
        except Exception:
            pass
        if not info and fallback:
            info = {"name": fallback.get("spark_name"), "spark_type": fallback.get("spark_type"),
                    "tier": fallback.get("spark_tier")}
        info = info or {}
        info["name"] = info.get("name") or f"Spark #{asa}"
        self._sparks[asa] = (_now(), info)
        return info

    @staticmethod
    def spark_art(info: dict, caption: str | None = None) -> dict:
        """Card art item for a Spark: Pinata image by type/tier, coloured stand-in if it can't load."""
        stype, tier = info.get("spark_type"), info.get("tier") or 1
        return {"url": spark_image_url(stype, tier), "caption": caption,
                "fallback": f"{(stype or 'spark').upper()} T{tier}", "color": spark_color(stype)}

    # ── schedule helpers ───────────────────────────────────────────────────────

    def _local_now(self) -> datetime:
        return _now().astimezone(NEWS_TZ)

    def _quiet(self) -> bool:
        h = self._local_now().hour
        if QUIET_START > QUIET_END:
            return h >= QUIET_START or h < QUIET_END
        return QUIET_START <= h < QUIET_END

    async def _count_today(self, breaking: bool) -> int:
        start = self._local_now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        try:
            res = (await self._q(lambda: self.db.table("news_log").select("id", count="exact")
                                 .eq("breaking", breaking).gte("posted_at", _iso(start)).execute()))
            return res.count if res.count is not None else len(res.data or [])
        except Exception as e:
            print(f"[news] count failed: {e}")
            return 0

    def _roll_next(self):
        self.next_post_at = _now() + timedelta(minutes=random.randint(MIN_MINUTES, MAX_MINUTES))

    # ── loops ──────────────────────────────────────────────────────────────────

    @tasks.loop(seconds=60)
    async def scheduler(self):
        try:
            if not self.enabled or self._quiet():
                return
            if self.next_post_at and _now() < self.next_post_at:
                return
            self._roll_next()                       # roll first: a failed post never hot-loops
            if await self._count_today(False) >= DAILY_CAP:
                return
            await self.post_scheduled()
        except Exception as e:
            print(f"[news] scheduler error: {e}")

    @scheduler.before_loop
    async def _before_scheduler(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=BREAKING_POLL_MIN)
    async def breaking_watch(self):
        try:
            if not self.enabled or self._quiet() or _now() < self.boot_quiet_until:
                return
            if self.last_breaking_at and _now() - self.last_breaking_at < timedelta(minutes=BREAKING_GAP_MIN):
                return
            if await self._count_today(True) >= BREAKING_CAP:
                return
            async with self._post_lock:
                story = await self.find_breaking()
                if story:
                    await self.broadcast("breaking", story, breaking=True)
        except Exception as e:
            print(f"[news] breaking watch error: {e}")

    @breaking_watch.before_loop
    async def _before_breaking(self):
        await self.bot.wait_until_ready()

    # ── choose + post ──────────────────────────────────────────────────────────

    def _ordered_segments(self, forced: str | None = None) -> list[str]:
        if forced:
            return [forced]
        now = _now()
        ok = []
        for key, (weight, gap_min) in SEGMENTS.items():
            last = self.last_posted.get(key)
            if last and now - last < timedelta(minutes=gap_min):
                continue
            ok.append((key, weight))
        if not ok:
            ok = [(k, w) for k, (w, _) in SEGMENTS.items()]
        # weighted random order (Efraimidis–Spirakis)
        ordered = sorted(ok, key=lambda kw: random.random() ** (1.0 / kw[1]), reverse=True)
        keys = [k for k, _ in ordered]
        if self.last_segment in keys and len(keys) > 1:      # don't repeat the last segment back to back
            keys.remove(self.last_segment)
            keys.append(self.last_segment)
        return keys

    async def post_scheduled(self, forced: str | None = None) -> str | None:
        """Try segments in weighted-random order until one has something to say."""
        async with self._post_lock:
            for key in self._ordered_segments(forced):
                try:
                    story = await self._registry[key]()
                except Exception as e:
                    print(f"[news] segment {key} failed: {type(e).__name__}: {e}")
                    story = None
                if story:
                    await self.broadcast(story.get("segment") or key, story, breaking=False, log_key=key)
                    return key
            print("[news] no segment had anything to report this round")
            return None

    async def broadcast(self, seg_key: str, story: dict, *, breaking: bool, log_key: str | None = None):
        channel = self.bot.get_channel(NEWS_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(NEWS_CHANNEL_ID)
            except Exception as e:
                print(f"[news] can't reach channel {NEWS_CHANNEL_ID}: {e}")
                return
        anchor = await self.pick_anchor()
        rng = random.Random()
        meta = T.SEGMENTS["breaking" if breaking else story.get("segment") or seg_key]
        label = story.get("label") or meta["label"]
        emoji = story.get("emoji") or meta["emoji"]

        open_line = T.pick(anchor.voice, "breaking" if breaking else "open", anchor.name, rng)
        close_line = T.pick(anchor.voice, "close", anchor.name, rng)
        quip = T.pick(anchor.voice, story["mood"], anchor.name, rng) if story.get("mood") else None
        aside = T.trait_aside(anchor.traits, rng) if rng.random() < 0.35 else None

        parts = [f"{emoji} **{label}** · filed by Agent **{anchor.name}**", open_line]
        if aside:
            parts.append(aside)
        parts += ["", f"**{story['headline']}**"]
        parts += [f"• {line}" for line in story.get("body", [])]
        parts += [f"• {line}" for line in story.get("say", [])]
        if quip:
            parts.append(f"_{quip}_")
        parts.append(close_line)
        if story.get("link"):
            src = f" ({story['source']})" if story.get("source") else ""
            parts.append(f"🔗 <{story['link']}>{src}")
        content = "\n".join(parts)[:1990]

        # Art = whatever the story is ABOUT (Clash winner, Sparks, record holder ...).
        # No subject -> the filing agent's own art. The agent is called out in the card footer.
        art_items = story.get("art_items")
        if not art_items:
            subject = None
            if story.get("art") is not None:
                try:
                    subject = self.pool.get(int(story["art"]))
                except (TypeError, ValueError):
                    subject = None
            if subject:
                art_items = [{"path": subject.image_path, "url": subject.image_url,
                              "caption": story.get("art_caption")}]
            else:
                art_items = [{"path": anchor.image_path, "url": anchor.image_url}]
        card = await render_news_card(
            agent_name=anchor.name, art_items=art_items,
            segment_label="DEVELOPING STORY" if breaking else label,
            headline=story["headline"], lines=story.get("body", []),
            accent=story.get("accent") or meta["accent"], breaking=breaking, footer=story.get("source"),
        )
        msg = await channel.send(
            content=content,
            file=discord.File(card, filename="zappy_news.png"),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        await self.mark_seen(*story.get("seen", []))
        now = _now()
        if breaking:
            self.last_breaking_at = now
        else:
            self.last_posted[log_key or seg_key] = now
            self.last_segment = log_key or seg_key
        try:
            await self._q(lambda: self.db.table("news_log").insert({
                "segment": log_key or seg_key, "anchor_asset_id": anchor.asset_id,
                "channel_id": str(channel.id), "message_id": str(msg.id), "breaking": breaking,
            }).execute())
        except Exception as e:
            print(f"[news] log insert failed: {e}")
        print(f"[news] posted {'BREAKING ' if breaking else ''}{log_key or seg_key} via {anchor.name}")

    # ══════════════════════════════════════════════════════════════════════════
    # SEGMENTS — each returns a story dict or None
    #   headline: str   body: [str]  say: [str] (message-only lines)  link/source
    #   mood: 'up'|'down'|'flat'|None   seen: [dedupe keys]
    # ══════════════════════════════════════════════════════════════════════════

    async def seg_clash_recap(self) -> dict | None:
        since = _iso(_now() - timedelta(hours=24))
        rows = (await self._q(lambda: self.db.table("battles").select("*")
                              .gte("played_at", since).order("played_at", desc=True)
                              .limit(300).execute())).data or []
        if not rows:
            return None
        key = f"clash:{rows[0].get('id')}"
        if key in self.seen:
            return None

        n, upsets = len(rows), [r for r in rows if r.get("is_upset")]
        bracket_id = rows[0].get("bracket_id")
        in_bracket = [r for r in rows if r.get("bracket_id") == bracket_id]
        final = max(in_bracket, key=lambda r: (r.get("bracket_round") or 0, r.get("id") or 0))
        winner = self.zname(final["winner_asset_id"])
        pretty = str(bracket_id or "").split("_")[0].replace("-", " ").strip() or "latest"

        body = [f"{n} match{'es' if n != 1 else ''} and {len(upsets)} upset{'s' if len(upsets) != 1 else ''} in the last 24 hours"]
        hero_pick = None
        if upsets:
            big = max(upsets, key=lambda r: (r.get("bracket_round") or 0, r.get("id") or 0))
            body.append(f"Biggest upset: {self.zname(big['winner_asset_id'])} over "
                        f"{self.zname(big['loser_asset_id'])} (round {big.get('bracket_round', '?')})")
            hero_pick = HERO_ASSET_IDS.get(big["winner_asset_id"])
        hero_pick = HERO_ASSET_IDS.get(final["winner_asset_id"]) or hero_pick
        if hero_pick and hero_pick in T.HERO_SIGNATURE:
            body.append(f"{hero_pick}'s {T.HERO_SIGNATURE[hero_pick]} was in the toolkit")
        say = [f"Congrats {_mention(final.get('winner_discord_id'))}"] if final.get("winner_discord_id") else []
        return {"headline": f"{winner} wins the {pretty} bracket", "body": body[:3], "say": say, "seen": [key],
                "art": final["winner_asset_id"], "art_caption": "WINNER"}

    async def seg_winners_circle(self) -> dict | None:
        since = _iso(_now() - timedelta(hours=48))
        rows = (await self._q(lambda: self.db.table("nft_prizes").select("*")
                              .eq("status", "claimed").gte("claimed_at", since)
                              .order("claimed_at", desc=True).limit(20).execute())).data or []
        for r in rows:
            key = f"prize:claim:{r.get('id', r.get('asset_id'))}"
            if key in self.seen:
                continue
            how = T.SOURCE_LABELS.get(r.get("source"), "playing")
            return {
                "headline": f"{r.get('asset_name') or 'An NFT'} finds a new home",
                "body": [f"Won through {how}"],
                "say": [f"Congrats {_mention(r.get('discord_user_id'))}"],
                "seen": [key], "art": r.get("asset_id"), "art_caption": "PRIZE",
            }
        return None

    async def seg_market_minute(self) -> dict | None:
        price = await self.feeds.algo_price()
        if not price:
            return None
        change = price.get("change_24h")
        body = [f"{change:+.1f}% over 24 hours" if change is not None else "24h change unavailable"]
        return {"headline": f"ALGO ${price['usd']:.4f}", "body": body, "source": "CoinGecko",
                "mood": T.price_mood(change), "seen": []}

    async def seg_headlines(self) -> dict | None:
        kinds = sorted(HEADLINE_KIND_WEIGHTS, key=lambda k: random.random() ** (1.0 / HEADLINE_KIND_WEIGHTS[k]), reverse=True)
        for kind in kinds:
            items = [i for i in await self.feeds.headlines(kind) if i["key"] not in self.seen]
            if not items:
                continue
            it = random.choice(items[:12])
            body = []
            if it.get("extra") and it["extra"].get("score"):
                body.append(f"{it['extra']['score']} points on Hacker News")
            return {"segment": "headlines", "label": T.HEADLINE_LABELS[kind], "headline": it["title"],
                    "body": body, "link": it["link"], "source": it["source"], "seen": [it["key"]]}
        return None

    async def seg_chain_report(self) -> dict | None:
        now = _now()
        options: list[dict] = []
        live = (await self._q(lambda: self.db.table("chain_runs").select("*")
                              .eq("status", "live").limit(1).execute())).data
        if live:
            r = live[0]
            key = f"chain:live:{r['id']}:{r.get('chain_count')}"
            if key not in self.seen and (r.get("chain_count") or 0) >= 2:
                options.append({
                    "headline": f"A chain is live: {r['chain_count']} links and counting",
                    "body": [f"{r.get('grounds_left', '?')} Grounds left"],
                    "say": [f"Last catch: {_mention(r.get('last_catcher'))}"] if r.get("last_catcher") else [],
                    "seen": [key]})
        since = _iso(now - timedelta(days=7))
        best = (await self._q(lambda: self.db.table("chain_runs").select("*")
                              .eq("status", "ended").gte("ended_at", since)
                              .order("chain_count", desc=True).limit(1).execute())).data
        if best and (best[0].get("chain_count") or 0) >= 3:
            r = best[0]
            key = f"chain:best:{r['id']}"
            if key not in self.seen:
                catches = r.get("catches") or {}
                top = max(catches.items(), key=lambda kv: kv[1]) if catches else None
                say = [f"Started by {_mention(r.get('started_by'))}"] if r.get("started_by") else []
                if top:
                    say.append(f"Top catcher: {_mention(top[0])} with {top[1]}")
                options.append({"headline": f"Longest chain this week: {r['chain_count']} links",
                                "body": [], "say": say, "seen": [key]})
        links = (await self._q(lambda: self.db.table("chain_links").select("caught_by")
                               .gte("caught_at", since).limit(2000).execute())).data or []
        tally = Counter(l["caught_by"] for l in links if l.get("caught_by"))
        if tally:
            uid, cnt = tally.most_common(1)[0]
            key = f"chain:hot:{now.date()}:{uid}"
            if cnt >= 3 and key not in self.seen:
                options.append({"headline": f"Hot hand on the chain: {cnt} catches this week",
                                "body": [], "say": [f"That's {_mention(uid)}"], "seen": [key]})
        return random.choice(options) if options else None

    async def seg_voltball_scores(self) -> dict | None:
        since = _iso(_now() - timedelta(hours=36))
        ms = (await self._q(lambda: self.db.table("voltball_matches").select("*")
                            .gte("recap_posted_at", since).order("recap_posted_at", desc=True)
                            .limit(8).execute())).data or []
        if not ms:
            return None
        key = f"volt:{ms[0].get('id')}"
        if key in self.seen:
            return None
        ids = list({m[k] for m in ms for k in ("team_a_id", "team_b_id") if m.get(k)})
        teams = (await self._q(lambda: self.db.table("voltball_teams").select("id,team_name")
                               .in_("id", ids).execute())).data or []
        tname = {t["id"]: t.get("team_name") or "Mystery Team" for t in teams}
        week = ms[0].get("week_number")
        playoff = ms[0].get("is_playoff")

        def line(m):
            return (f"{tname.get(m['team_a_id'], '?')} {float(m['team_a_score']):.1f} – "
                    f"{float(m['team_b_score']):.1f} {tname.get(m['team_b_id'], '?')}")

        lead = ms[0]
        winner = tname.get(lead.get("winner_team_id"), "the winners")
        return {"headline": f"Voltball {'playoffs' if playoff else f'Week {week}'}: {winner} take it",
                "body": [line(m) for m in ms[:3]], "seen": [key]}

    async def seg_office_gossip(self) -> dict | None:
        """Office news, shown with the Sparks involved (not the filing agent)."""
        since = _iso(_now() - timedelta(hours=24))
        options: list[dict] = []
        try:
            duels = (await self._q(lambda: self.db.table("spark_office_duels").select("*")
                                   .eq("status", "resolved").gte("resolved_at", since)
                                   .limit(10).execute())).data or []
            for d in duels:
                key = f"office:duel:{d.get('id')}"
                if key in self.seen or not d.get("winner_asa"):
                    continue
                win_asa = d["winner_asa"]
                lose_asa = d["defender_asa"] if win_asa == d["challenger_asa"] else d["challenger_asa"]
                win, lose = await self.spark_info(win_asa), await self.spark_info(lose_asa)
                challenger = win if win_asa == d["challenger_asa"] else lose
                defender = lose if win_asa == d["challenger_asa"] else win
                options.append({
                    "headline": f"Office duel: {win['name']} wins the day",
                    "body": [f"{challenger['name']} challenged {defender['name']} for the seat"],
                    "art_items": [self.spark_art(win, "WINNER"), self.spark_art(lose, "BEATEN")],
                    "accent": spark_color(win.get("spark_type")), "seen": [key]})
            dems = (await self._q(lambda: self.db.table("spark_office_demotions").select("*")
                                  .gte("demoted_at", since).limit(10).execute())).data or []
            for d in dems:
                key = f"office:dem:{d.get('id', d.get('spark_asa'))}:{d.get('demoted_at')}"
                if key in self.seen:
                    continue
                info = await self.spark_info(d["spark_asa"])
                reason = str(d.get("reason") or "").replace("_", " ").strip()
                options.append({
                    "headline": f"{info['name']} has lost an Office seat",
                    "body": [f"Reason on file: {reason}"] if reason else [],
                    "art_items": [self.spark_art(info, "DEMOTED")],
                    "accent": spark_color(info.get("spark_type")), "seen": [key]})
            seats = (await self._q(lambda: self.db.table("spark_office_seats").select("*")
                                   .gte("seated_at", since).limit(10).execute())).data or []
            for st in seats:
                key = f"office:seat:{st.get('spark_asa')}:{st.get('seated_at')}"
                if key in self.seen:
                    continue
                info = await self.spark_info(st["spark_asa"], fallback=st)
                options.append({
                    "headline": f"{info['name']} just got a seat in the Office",
                    "body": [f"Promoted to a Tier {info['tier']} desk" if info.get("tier")
                             else "Promotion approved by the board"],
                    "art_items": [self.spark_art(info, "PROMOTED")],
                    "accent": spark_color(info.get("spark_type")), "seen": [key]})
        except Exception as e:
            print(f"[news] office gossip query failed: {e}")
        return random.choice(options) if options else None

    async def seg_hall_of_records(self) -> dict | None:
        rows = (await self._q(lambda: self.db.table("zappy_records").select("*")
                              .limit(3000).execute())).data or []
        rows = [r for r in rows if (r.get("wins", 0) + r.get("losses", 0)) > 0]
        if not rows:
            return None
        picks = []   # (headline, detail, asset_id, caption)
        w = max(rows, key=lambda r: r.get("wins", 0))
        if w.get("wins", 0) > 0:
            picks.append((f"{self.zname(w['asset_id'])} holds the Clash wins record",
                          f"{w['wins']} wins · {w.get('losses', 0)} losses all-time", w["asset_id"], "MOST WINS"))
        c = max(rows, key=lambda r: r.get("champ_wins", 0))
        if c.get("champ_wins", 0) > 0:
            picks.append((f"Most bracket titles: {self.zname(c['asset_id'])}",
                          f"{c['champ_wins']} championship{'s' if c['champ_wins'] != 1 else ''} on the shelf",
                          c["asset_id"], "CHAMPION"))
        seasoned = [r for r in rows if r.get("wins", 0) + r.get("losses", 0) >= 10]
        if seasoned:
            b = max(seasoned, key=lambda r: r["wins"] / (r["wins"] + r["losses"]))
            g = b["wins"] + b["losses"]
            picks.append((f"Best win rate (10+ fights): {self.zname(b['asset_id'])}",
                          f"{b['wins']}-{b['losses']} · {100 * b['wins'] / g:.0f}% over {g} fights",
                          b["asset_id"], "BEST WIN RATE"))
        l = max(rows, key=lambda r: r.get("losses", 0))
        if l.get("losses", 0) > 0:
            picks.append((f"Hardest luck in the league: {self.zname(l['asset_id'])}",
                          f"{l['losses']} losses and still showing up", l["asset_id"], "HARD LUCK"))
        if not picks:
            return None
        headline, line, aid, cap = random.choice(picks)
        return {"headline": headline, "body": [line], "seen": [], "art": aid, "art_caption": cap}

    # ══════════════════════════════════════════════════════════════════════════
    # BREAKING NEWS — event-triggered, polled every few minutes
    # ══════════════════════════════════════════════════════════════════════════

    async def find_breaking(self) -> dict | None:
        since = _iso(_now() - timedelta(hours=BREAKING_LOOKBACK_H))
        candidates: list[dict] = []

        # 1. big upsets (late-round)
        try:
            ups = (await self._q(lambda: self.db.table("battles").select("*")
                                 .eq("is_upset", True).gte("bracket_round", BREAKING_UPSET_ROUND)
                                 .gte("played_at", since).order("played_at", desc=True)
                                 .limit(10).execute())).data or []
            for r in ups:
                key = f"brk:upset:{r.get('id')}"
                if key not in self.seen:
                    hero = HERO_ASSET_IDS.get(r["winner_asset_id"])
                    body = [f"Round {r.get('bracket_round')} shocker"]
                    if hero in T.HERO_SIGNATURE:
                        body.append(f"{hero}'s {T.HERO_SIGNATURE[hero]} was in the toolkit")
                    candidates.append({"headline": f"UPSET! {self.zname(r['winner_asset_id'])} stuns "
                                                   f"{self.zname(r['loser_asset_id'])}",
                                       "body": body, "seen": [key],
                                       "art": r["winner_asset_id"], "art_caption": "UPSET WINNER"})
        except Exception as e:
            print(f"[news] breaking/upset query failed: {e}")

        # 2. chain records + live milestones
        try:
            ended = (await self._q(lambda: self.db.table("chain_runs").select("*")
                                   .eq("status", "ended").gte("ended_at", since)
                                   .order("chain_count", desc=True).limit(3).execute())).data or []
            for r in ended:
                key = f"brk:chainend:{r['id']}"
                if key in self.seen or (r.get("chain_count") or 0) < 10:
                    continue
                prev = (await self._q(lambda: self.db.table("chain_runs").select("chain_count")
                                      .eq("status", "ended").neq("id", r["id"])
                                      .order("chain_count", desc=True)
                                      .limit(1).execute())).data
                prev_best = prev[0]["chain_count"] if prev else 0
                record = r["chain_count"] > (prev_best or 0)
                candidates.append({"headline": ("NEW CHAIN RECORD: " if record else "Epic chain ends at ")
                                               + f"{r['chain_count']} links",
                                   "body": ([f"Previous best was {prev_best}"] if record and prev_best else []),
                                   "seen": [key]})
            live = (await self._q(lambda: self.db.table("chain_runs").select("*")
                                  .eq("status", "live").limit(1).execute())).data
            if live:
                r = live[0]
                for ms in (100, 50, 25, 10):
                    if (r.get("chain_count") or 0) >= ms:
                        key = f"brk:chainms:{r['id']}:{ms}"
                        if key not in self.seen:
                            candidates.append({"headline": f"The chain just hit {ms} links!",
                                               "body": [f"{r.get('grounds_left', '?')} Grounds left"],
                                               "seen": [key]})
                        break
        except Exception as e:
            print(f"[news] breaking/chain query failed: {e}")

        # 3. fresh NFT drops
        try:
            drops = (await self._q(lambda: self.db.table("nft_prizes").select("*")
                                   .gte("awarded_at", since).order("awarded_at", desc=True)
                                   .limit(10).execute())).data or []
            for r in drops:
                key = f"brk:drop:{r.get('id', r.get('asset_id'))}"
                if key not in self.seen:
                    candidates.append({"headline": f"NFT DROP: {r.get('asset_name') or 'a prize'} has a new owner-to-be",
                                       "body": [f"Earned through {T.SOURCE_LABELS.get(r.get('source'), 'play')}"],
                                       "say": [f"Congrats {_mention(r.get('discord_user_id'))}"],
                                       "seen": [key], "art": r.get("asset_id"), "art_caption": "PRIZE"})
        except Exception as e:
            print(f"[news] breaking/drop query failed: {e}")

        if not candidates:
            return None
        story = candidates[0] if len(candidates) == 1 else random.choice(candidates)
        # everything else we found stays unseen and is considered next poll (if still fresh)
        return story

    # ══════════════════════════════════════════════════════════════════════════
    # ADMIN COMMANDS
    # ══════════════════════════════════════════════════════════════════════════

    @app_commands.command(name="news_now", description="(Admin) Post a Zappy Agent Wire report right now.")
    @app_commands.describe(segment="Which segment to run (default: random)")
    @app_commands.choices(segment=[app_commands.Choice(name=k.replace("_", " "), value=k) for k in SEGMENTS]
                          + [app_commands.Choice(name="breaking check", value="breaking")])
    @app_commands.default_permissions(manage_guild=True)
    async def news_now(self, interaction: discord.Interaction, segment: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        forced = segment.value if segment else None
        if forced == "breaking":
            async with self._post_lock:
                story = await self.find_breaking()
                if story:
                    await self.broadcast("breaking", story, breaking=True)
            if not story:
                return await interaction.followup.send("No breaking-news events in the last "
                                                       f"{BREAKING_LOOKBACK_H}h.", ephemeral=True)
            return await interaction.followup.send("🚨 Breaking broadcast posted.", ephemeral=True)
        posted = await self.post_scheduled(forced)
        await interaction.followup.send(
            f"✅ Posted **{posted}**." if posted else
            "Nothing to report for that segment right now (no fresh data, or already covered).",
            ephemeral=True)

    @app_commands.command(name="news_status", description="(Admin) Zappy Agent Wire status.")
    @app_commands.default_permissions(manage_guild=True)
    async def news_status(self, interaction: discord.Interaction):
        nxt = self.next_post_at.astimezone(NEWS_TZ).strftime("%a %I:%M %p") if self.next_post_at else "unscheduled"
        today = await self._count_today(False)
        brk = await self._count_today(True)
        feeds = " · ".join(f"{k}: {len(self.feeds._items.get(k, []))}" for k in ("tech", "ai", "sports"))
        await interaction.response.send_message(
            f"**Zappy Agent Wire** — {'🟢 on' if self.enabled else '⏸️ paused'}\n"
            f"Next broadcast: {nxt} (quiet hours {QUIET_START}:00–{QUIET_END}:00 {NEWS_TZ.key})\n"
            f"Today: {today}/{DAILY_CAP} scheduled · {brk}/{BREAKING_CAP} breaking\n"
            f"Agent rotation: {len(self.bag_used)}/{len(self.pool)} Zappies have taken a shift\n"
            f"Cached headlines — {feeds}",
            ephemeral=True)

    @app_commands.command(name="news_toggle", description="(Admin) Pause or resume the Zappy Agent Wire.")
    @app_commands.default_permissions(manage_guild=True)
    async def news_toggle(self, interaction: discord.Interaction):
        self.enabled = not self.enabled
        await interaction.response.send_message(
            "🟢 Agent Wire resumed." if self.enabled else "⏸️ Agent Wire paused (until restart or toggled back).",
            ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ZappyNewsCog(bot, bot.supabase))

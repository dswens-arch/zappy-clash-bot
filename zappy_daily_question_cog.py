"""
zappy_daily_question_cog.py
---------------------------
Zappy Question of the Day: ONE post a day that gives people something to answer.

A Zappy agent drops a single question in the main channel around midday (Central).
It is one of three shapes:
    vs    A-or-B poll (people tap 🅰️ / 🅱️)
    take  a friendly hot take (people tap 🔥 agree / 🧊 disagree)
    open  an open question (people just reply)
Each post gets a thread so replies don't flood the channel, and the next day's post
shows how yesterday's poll split.

Questions are written by a cheap model (DAILYQ_MODEL, default claude-haiku-5-5) from a
rotating angle (Zappies traits, Clash strategy, Voltball lineups, Spark Office, expeditions,
collecting, hypotheticals, a stat from the server) and the same server-wide facts the chat
bot uses. If the model is unavailable it falls back to a built-in bank, so a day is never
skipped. No payouts, no pings, zero Algorand traffic.

No new tables: "already posted today" and "what did we ask recently" are read back from the
channel itself (the footer marker below), so redeploys never double-post.

Env (all optional)
  DAILYQ_ENABLED (1), DAILYQ_CHANNEL_ID (-> NEWS_CHANNEL_ID -> HOLDER_CHANNEL_ID),
  DAILYQ_TZ (America/Chicago), DAILYQ_HOUR (12), DAILYQ_LATEST_HOUR (20, late catch-up
  window after a redeploy), DAILYQ_MODEL, DAILYQ_SKIP_TRIVIA_DAY (1: no question on the
  weekly Trivia night when the Trivia cog is loaded; TRIVIA_DAY mirrors that cog, default Fri).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

API_URL = "https://api.anthropic.com/v1/messages"

ENABLED_DEFAULT = os.environ.get("DAILYQ_ENABLED", "1") not in ("0", "false", "False", "")
CHANNEL_ID = int(os.environ.get("DAILYQ_CHANNEL_ID")
                 or os.environ.get("NEWS_CHANNEL_ID")
                 or os.environ.get("HOLDER_CHANNEL_ID")
                 or 1314066280592052244)
TZ = ZoneInfo(os.environ.get("DAILYQ_TZ", "America/Chicago"))
POST_HOUR = int(os.environ.get("DAILYQ_HOUR", 12))
LATEST_HOUR = int(os.environ.get("DAILYQ_LATEST_HOUR", 20))
MODEL = os.environ.get("DAILYQ_MODEL", "claude-haiku-5-5")
SKIP_TRIVIA_DAY = os.environ.get("DAILYQ_SKIP_TRIVIA_DAY", "1") not in ("0", "false", "False", "")
TRIVIA_DAY = int(os.environ.get("TRIVIA_DAY", 4))          # Monday=0 ... Friday=4
BOOT_DELAY_S = 90
HISTORY_LIMIT = 200
RECENT_KEEP = 14
MARKER = "Zappy Question of the Day"

KINDS = {"vs": 4, "take": 3, "open": 3}                    # weights
VS_EMOJI = ("🅰️", "🅱️")
TAKE_EMOJI = ("🔥", "🧊")

ANGLES: list[tuple[str, int]] = [
    ("Zappies traits and looks: favorites, most underrated, what's secretly the best", 3),
    ("Clash Battle strategy: which Zappy or Hero you'd back and why", 3),
    ("Voltball: formations (OFFENSE 1/5/1/1, BALANCED 1/3/2/2, DEFENSE 1/1/2/4), QB picks, tempo", 3),
    ("Spark Office and Spark Jobs: desk life, who'd be promoted, who'd get demoted", 2),
    ("Expeditions: what a Zappy would find out there, what you'd bring", 2),
    ("Collecting: dream pull, which prize NFT everybody wants, how you'd build a collection", 2),
    ("A playful hypothetical starring Zappies (PG-13)", 2),
    ("Turn ONE stat from the SERVER FACTS into a debate (only if facts are provided)", 2),
    ("General fun a gaming community argues about (food, games, tech, hot-weather-vs-cold)", 1),
]

SYSTEM_PROMPT = """You write the "Question of the Day" for the Zappies Discord, a friendly community around the Zappies Reborn NFT collection (Algorand), the Zappy Clash battle bot, Voltball and the Spark Office.

Write exactly ONE question that people will WANT to answer. Make it specific, a little funny or spicy, and easy to answer in one line. Today is {today}.

Format requested: {kind_rules}

Rules:
- Output ONLY a JSON object, no prose, no code fences.
- PG-13, friendly, no politics, no religion, no real-person drama, nothing about money or prices.
- Never @mention anyone and don't include links.
- Only use facts that appear under SERVER FACTS; never invent stats, rankings or names. Mention a member only to celebrate them.
- Don't repeat or closely paraphrase any RECENT question.
- Keep it answerable by anyone, even a new member who owns no Zappies.

JSON shape: {{"question": "...", "options": ["...", "..."]}}  (omit "options" unless the format asks for two).
Question max 200 characters. Each option max 60 characters."""

KIND_RULES = {
    "vs": 'an A-or-B poll. "question" is the setup, "options" is exactly two short, distinct choices.',
    "take": 'a friendly hot take stated as a claim people can agree or disagree with. "question" is the take itself. No "options".',
    "open": 'an open question that invites a short story or opinion in the replies. No "options".',
}

# ── Fallback bank (used when the model is unavailable) ─────────────────────────

FALLBACKS: dict[str, list[dict]] = {
    "vs": [
        {"question": "Your Clash Zappy is picked for one battle. Which hits harder?", "options": ["Raw power", "Perfect timing"]},
        {"question": "Voltball formation of the day:", "options": ["OFFENSE 1/5/1/1 (all gas)", "DEFENSE 1/1/2/4 (brick wall)"]},
        {"question": "Spark Office desk assignment. Where do you want your Zappy?", "options": ["Corner office, quiet", "Open floor, front row"]},
        {"question": "Expedition loot. Which would you rather find?", "options": ["One rare prize", "A big pile of small rewards"]},
        {"question": "Your Zappy joins a fight night. Which entrance?", "options": ["Slow walk, lights off", "Full sprint, confetti"]},
        {"question": "Pick a Voltball QB philosophy:", "options": ["Safe hands, never fumbles", "Big arm, chaos is a feature"]},
        {"question": "Best way to start the day?", "options": ["Clash first, coffee second", "Coffee first, Clash second"]},
        {"question": "Collection goals:", "options": ["Own one perfect favorite", "Own a wall of variety"]},
    ],
    "take": [
        {"question": "Hot take: defense wins Voltball games, offense just sells tickets."},
        {"question": "Hot take: the best Zappy is the one with the weirdest traits, not the rarest."},
        {"question": "Hot take: a daily streak matters more than a lucky Clash win."},
        {"question": "Hot take: the Spark Office is secretly the best part of the server."},
        {"question": "Hot take: underdogs make Clash worth watching, and upsets should pay out in bragging rights forever."},
        {"question": "Hot take: every Voltball QB should pick tempo 10 and live with the consequences."},
        {"question": "Hot take: pineapple belongs on pizza, and Zappies would agree."},
        {"question": "Hot take: expeditions should be longer and weirder."},
    ],
    "open": [
        {"question": "What's the first thing you noticed about the Zappy you'd never sell?"},
        {"question": "Name a Zappy you'd put in charge of the Spark Office, and what is their first rule?"},
        {"question": "What's your best Clash moment so far: an upset, a streak, anything?"},
        {"question": "If your Zappy wrote a one-line LinkedIn bio, what would it say?"},
        {"question": "What would make Voltball even more fun to play?"},
        {"question": "Which trait combo do you think is the most underrated, and why?"},
        {"question": "What's the best thing a Zappy could find on an expedition?"},
        {"question": "Give your favorite Zappy a job title."},
    ],
}


# ── Pure helpers (unit-testable) ───────────────────────────────────────────────

_MENTION_RE = re.compile(r"<@[!&]?\d+>|@everyone|@here", re.I)
_URL_RE = re.compile(r"https?://\S+", re.I)


def _clean(text: str, limit: int) -> str:
    text = _MENTION_RE.sub("", str(text or ""))
    text = _URL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def parse_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply (tolerates fences / chatter)."""
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def validate(kind: str, data: dict | None, recent: list[str] | None = None) -> dict | None:
    """Return a clean {'question', 'options'?} or None if the model output isn't usable."""
    if not isinstance(data, dict):
        return None
    q = _clean(data.get("question"), 220)
    if len(q) < 12:
        return None
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    if any(norm(q) == norm(r) for r in (recent or [])):
        return None
    out = {"question": q}
    if kind == "vs":
        opts = data.get("options")
        if not isinstance(opts, list) or len(opts) != 2:
            return None
        a, b = _clean(opts[0], 70), _clean(opts[1], 70)
        if not a or not b or norm(a) == norm(b):
            return None
        out["options"] = [a, b]
    return out


def pick_kind(rng=random) -> str:
    return rng.choices(list(KINDS), weights=list(KINDS.values()), k=1)[0]


def pick_angle(last: str | None, rng=random) -> str:
    pool = [(a, w) for a, w in ANGLES if a != last] or ANGLES
    return rng.choices([a for a, _ in pool], weights=[w for _, w in pool], k=1)[0]


def pick_fallback(kind: str, recent: list[str], rng=random) -> dict:
    bank = FALLBACKS[kind]
    fresh = [x for x in bank if x["question"] not in recent] or bank
    return dict(rng.choice(fresh))


def build_embed(kind: str, q: dict, yesterday: str | None = None) -> discord.Embed:
    if kind == "vs":
        body = (f"**{q['question']}**\n\n{VS_EMOJI[0]}  {q['options'][0]}\n{VS_EMOJI[1]}  {q['options'][1]}\n\n"
                f"*Tap a reaction, then argue about it in the thread.*")
    elif kind == "take":
        body = (f"**{q['question']}**\n\n{TAKE_EMOJI[0]} agree · {TAKE_EMOJI[1]} disagree\n\n"
                f"*Tap a reaction, then defend yourself in the thread.*")
    else:
        body = f"**{q['question']}**\n\n*Answer in the thread below.*"
    emb = discord.Embed(title="⚡ Question of the Day", description=body, color=0xFFC933)
    if yesterday:
        emb.add_field(name="Yesterday", value=yesterday[:1000], inline=False)
    emb.set_footer(text=f"{MARKER} · {kind}")
    return emb


def is_qotd(msg, bot_id: int | None) -> bool:
    if bot_id is not None and getattr(msg.author, "id", None) != bot_id:
        return False
    for e in getattr(msg, "embeds", None) or []:
        f = getattr(e, "footer", None)
        if f and f.text and f.text.startswith(MARKER):
            return True
    return False


def qotd_kind(msg) -> str:
    for e in msg.embeds:
        if e.footer and e.footer.text and e.footer.text.startswith(MARKER):
            parts = e.footer.text.split("·")
            return parts[1].strip() if len(parts) > 1 else "open"
    return "open"


def qotd_question(msg) -> str:
    for e in msg.embeds:
        if e.footer and e.footer.text and e.footer.text.startswith(MARKER):
            first = (e.description or "").split("\n")[0]
            return first.strip("* ").strip()
    return ""


def tally_line(msg) -> str | None:
    """'🅰️ 14 – 9 🅱️ …' for yesterday's poll (bot's own reaction not counted). None if too quiet."""
    kind = qotd_kind(msg)
    emojis = VS_EMOJI if kind == "vs" else TAKE_EMOJI if kind == "take" else None
    if not emojis:
        return None
    counts = {}
    for r in getattr(msg, "reactions", None) or []:
        counts[str(r.emoji)] = max(0, r.count - (1 if getattr(r, "me", False) else 0))
    a, b = counts.get(emojis[0], 0), counts.get(emojis[1], 0)
    if a + b < 2:
        return None
    q = qotd_question(msg)
    if kind == "vs":
        names = []
        for line in (msg.embeds[0].description or "").split("\n"):
            for em in VS_EMOJI:
                if line.startswith(em):
                    names.append(line[len(em):].strip())
        lead = (names[0] if a > b else names[1]) if len(names) == 2 and a != b else None
        verdict = f"**{lead}** won" if lead else "a dead heat"
        return f"{emojis[0]} {a} – {b} {emojis[1]} ({verdict}) on: {q}"
    verdict = "the room agreed" if a > b else "the room disagreed" if b > a else "split down the middle"
    return f"{emojis[0]} {a} – {b} {emojis[1]} ({verdict}) on: {q}"


# ── Cog ────────────────────────────────────────────────────────────────────────

class ZappyDailyQuestionCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db=None):
        self.bot = bot
        self.db = db
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.enabled = ENABLED_DEFAULT
        self._lock = asyncio.Lock()
        self._posted_date = None           # local date of today's post (memory; channel is the source of truth)
        self._retry_after: datetime | None = None
        self._last_angle: str | None = None
        self.recent: list[str] = []

    async def cog_load(self):
        if not self.scheduler.is_running():
            self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.cancel()

    # ── scheduling ─────────────────────────────────────────────────────────────

    def _skip_today(self, now: datetime) -> bool:
        return (SKIP_TRIVIA_DAY and now.weekday() == TRIVIA_DAY
                and self.bot.get_cog("ZappyTriviaCog") is not None)

    def _due(self, now: datetime) -> bool:
        if not self.enabled or self._posted_date == now.date() or self._skip_today(now):
            return False
        if self._retry_after and now < self._retry_after:
            return False
        return POST_HOUR <= now.hour < LATEST_HOUR

    @tasks.loop(seconds=60)
    async def scheduler(self):
        try:
            now = datetime.now(TZ)
            if not self._due(now):
                return
            async with self._lock:
                if not self._due(now):
                    return
                msg = await self.post_question(respect_today=True)
                if msg is None and self._posted_date != now.date():
                    self._retry_after = now + timedelta(minutes=10)
        except Exception as e:
            print(f"[dailyq] scheduler error: {type(e).__name__}: {e}")

    @scheduler.before_loop
    async def _before_scheduler(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(BOOT_DELAY_S)

    # ── channel history (the only persistence) ────────────────────────────────

    async def _get_channel(self):
        ch = self.bot.get_channel(CHANNEL_ID)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(CHANNEL_ID)
            except Exception as e:
                print(f"[dailyq] can't reach channel {CHANNEL_ID}: {e}")
        return ch

    async def _scan(self, channel) -> list:
        """Our recent Question-of-the-Day messages, newest first."""
        out = []
        try:
            after = datetime.now(TZ) - timedelta(days=20)
            async for m in channel.history(limit=HISTORY_LIMIT, after=after):
                if is_qotd(m, getattr(self.bot.user, "id", None)):
                    out.append(m)
        except Exception as e:                       # no read-history permission, etc.
            print(f"[dailyq] history scan unavailable ({type(e).__name__}); using memory only")
            return []
        out.sort(key=lambda m: m.created_at, reverse=True)
        return out

    # ── LLM ───────────────────────────────────────────────────────────────────

    async def _facts(self, guild) -> str:
        chat = self.bot.get_cog("ZappyChatCog")
        if not chat or not guild:
            return ""
        try:
            return await chat.server_facts(guild)
        except Exception:
            return ""

    async def _ask_llm(self, system: str, user: str) -> str | None:
        if not self.api_key:
            return None
        payload = {"model": MODEL, "max_tokens": 400, "system": system,
                   "messages": [{"role": "user", "content": user}]}
        for _ in range(2):
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                    async with s.post(API_URL, json=payload, headers={
                            "x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                            "content-type": "application/json"}) as r:
                        data = await r.json()
                        if r.status != 200:
                            print(f"[dailyq] API {r.status}: {str(data)[:200]}")
                            continue
                text = "".join(b.get("text", "") for b in data.get("content", [])
                               if isinstance(b, dict) and b.get("type") == "text")
                if text.strip():
                    return text
            except Exception as e:
                print(f"[dailyq] API call failed: {type(e).__name__}: {e}")
        return None

    async def generate(self, kind: str, guild, recent: list[str]) -> tuple[dict, str]:
        """Returns (question dict, source) where source is 'llm' or 'fallback'."""
        angle = pick_angle(self._last_angle)
        system = SYSTEM_PROMPT.format(today=datetime.now(TZ).strftime("%A, %B %d, %Y"),
                                      kind_rules=KIND_RULES[kind])
        facts = await self._facts(guild)
        user = (f"ANGLE for today: {angle}\n\n"
                f"SERVER FACTS (optional, use only if it fits the angle):\n{facts or '(none available)'}\n\n"
                f"RECENT questions (do not repeat):\n" + ("\n".join(f"- {r}" for r in recent[:RECENT_KEEP]) or "- (none)"))
        for _ in range(2):
            q = validate(kind, parse_json(await self._ask_llm(system, user) or ""), recent)
            if q:
                self._last_angle = angle
                return q, "llm"
        print("[dailyq] model gave nothing usable; using the fallback bank")
        return pick_fallback(kind, recent), "fallback"

    # ── posting ───────────────────────────────────────────────────────────────

    async def post_question(self, kind: str | None = None, respect_today: bool = False):
        channel = await self._get_channel()
        if channel is None:
            return None
        history = await self._scan(channel)
        now = datetime.now(TZ)
        if respect_today and history and history[0].created_at.astimezone(TZ).date() == now.date():
            self._posted_date = now.date()           # already asked today (e.g. before a redeploy)
            return None
        recent = [qotd_question(m) for m in history][:RECENT_KEEP] or self.recent
        yesterday = None
        for m in history:
            if m.created_at.astimezone(TZ).date() < now.date():
                try:
                    m = await channel.fetch_message(m.id)        # fresh reaction counts
                    yesterday = tally_line(m)
                except Exception:
                    yesterday = tally_line(m)
                break

        kind = kind if kind in KINDS else pick_kind()
        q, source = await self.generate(kind, getattr(channel, "guild", None), recent)
        msg = await channel.send(embed=build_embed(kind, q, yesterday),
                                 allowed_mentions=discord.AllowedMentions.none())
        self._posted_date = now.date()
        self.recent = ([q["question"]] + self.recent)[:RECENT_KEEP]
        print(f"[dailyq] posted {kind} ({source}): {q['question'][:80]}")

        emojis = VS_EMOJI if kind == "vs" else TAKE_EMOJI if kind == "take" else ()
        for em in emojis:
            try:
                await msg.add_reaction(em)
            except Exception as e:
                print(f"[dailyq] reaction failed: {e}")
                break
        try:
            await msg.create_thread(name=f"QOTD · {now.strftime('%b %d')}", auto_archive_duration=1440)
        except Exception as e:
            print(f"[dailyq] thread not created ({type(e).__name__}); replies stay in channel")
        return msg

    # ── admin ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="dailyq_now", description="(Admin) Post a Question of the Day right now.")
    @app_commands.describe(kind="Format (default: random)")
    @app_commands.choices(kind=[app_commands.Choice(name="A or B poll", value="vs"),
                                app_commands.Choice(name="Hot take", value="take"),
                                app_commands.Choice(name="Open question", value="open")])
    @app_commands.default_permissions(manage_guild=True)
    async def dailyq_now(self, interaction: discord.Interaction, kind: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            msg = await self.post_question(kind.value if kind else None)
        await interaction.followup.send(
            "Posted. That counts as today's question." if msg else "Couldn't post (see the logs).", ephemeral=True)

    @app_commands.command(name="dailyq_toggle", description="(Admin) Pause or resume the Question of the Day.")
    @app_commands.default_permissions(manage_guild=True)
    async def dailyq_toggle(self, interaction: discord.Interaction):
        self.enabled = not self.enabled
        await interaction.response.send_message(
            f"{'🟢 Question of the Day resumed' if self.enabled else '⏸️ Question of the Day paused'}"
            f" (posts ~{POST_HOUR}:00 {TZ.key}).", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ZappyDailyQuestionCog(bot, getattr(bot, "supabase", None)))

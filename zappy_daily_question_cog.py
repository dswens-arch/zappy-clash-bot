"""
zappy_daily_question_cog.py
---------------------------
Zappy Question of the Day: ONE pondering question a day that people answer by
REPLYING to the bot.

Around midday (Central) a Zappy agent posts a single reflective question, the kind you
chew on for a second and then want to answer ("Which Zappy trait would you trade away,
and why?"). No polls, no buttons, no thread: people reply straight to the post, and the
Zappy Chat cog answers each reply in character, with the question as context
(see ZappyChatCog: replies to a Question-of-the-Day post count as talking to the bot even
if the reply ping is off).

Questions are written by a cheap model (DAILYQ_MODEL, default claude-haiku-5-5) from a
rotating angle (memory, risk vs safety, streaks, the Spark Office, expeditions, collecting,
thought experiments, a stat from the server) and the same server facts the chat bot uses.
If the model is unavailable it falls back to a built-in bank, so a day is never skipped.
No payouts, no pings, zero Algorand traffic.

No new tables: "already posted today" and "what did we ask recently" are read back from the
channel itself (the footer marker), so redeploys never double-post.

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
MARKER = "Zappy Question of the Day"                      # footer prefix; the chat cog looks for it too

ANGLES: list[tuple[str, int]] = [
    ("Firsts and memories: first Zappy, first win, first moment you got hooked", 3),
    ("Risk vs safety: the bold play or the safe one (Clash, Voltball formations and tempo)", 3),
    ("Underdogs, upsets and what makes a win feel earned", 2),
    ("Streaks and consistency: showing up every day vs one lucky moment", 2),
    ("What a Zappy thinks about when nobody is battling; their secret inner life", 3),
    ("Life in the Spark Office or out on an expedition: what it is really like for a Zappy", 2),
    ("Collecting and ownership: what makes one favorite, what you'd never let go", 2),
    ("A quirky thought experiment starring Zappies (PG-13)", 2),
    ("Turn ONE stat from the SERVER FACTS into a musing (only if facts are provided)", 2),
    ("The community: what you'd tell a brand-new member, what you'd change, what you'd keep", 1),
]

SYSTEM_PROMPT = """You write the "Question of the Day" for the Zappies Discord, a friendly community around the Zappies Reborn NFT collection (Algorand), the Zappy Clash battle bot, Voltball and the Spark Office.

Write exactly ONE pondering question: reflective, a little whimsical or thought-provoking, the kind of thing people stop and think about for a second and then want to answer. It should invite a personal answer in a sentence or two, not a yes/no and not a trivia fact. Today is {today}.

Rules:
- Output ONLY a JSON object, no prose, no code fences: {{"question": "..."}}
- One question, max 200 characters, ends with a question mark. Don't add a preamble.
- PG-13, friendly, no politics, no religion, no real-person drama, nothing about money or prices.
- Never @mention anyone and don't include links.
- Only use facts that appear under SERVER FACTS; never invent stats, rankings or names. Mention a member only to celebrate them.
- Don't repeat or closely paraphrase any RECENT question.
- Keep it answerable by anyone, even a new member who owns no Zappies."""

# ── Fallback bank (used when the model is unavailable) ─────────────────────────

FALLBACKS: list[str] = [
    "What do you think your favorite Zappy thinks about when nobody is watching?",
    "If a Zappy could keep only one memory from every battle, which would it keep?",
    "Is it braver to take the risky play or to trust the safe one when the whole room is watching?",
    "What makes a win feel earned instead of lucky?",
    "Which would you rather have: one legendary moment, or a long quiet streak?",
    "If the Spark Office had a break-room rumor everyone believed, what would it be?",
    "What's something an expedition could bring back that would change how you see your collection?",
    "What was the exact moment you went from just looking at Zappies to actually caring about them?",
    "If you could give every new member one piece of advice, what would it be?",
    "What would your Zappy's secret talent be, the one they'd never show in Clash?",
    "When does a favorite become a keeper, and how do you know?",
    "What's a small habit, like a daily streak, that quietly changed how you play?",
    "If a Zappy could ask you one question, what do you think it would be?",
    "What is the underdog's real advantage?",
    "Which is the better Voltball story: the perfect lineup that works, or the strange one that shouldn't?",
    "What's the most underrated thing about this server?",
    "If the Zappies ran the server for a day, what would be the first thing they'd change?",
    "What would you want a Zappy to say about you after a long season together?",
]


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


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def validate(data: dict | None, recent: list[str] | None = None) -> str | None:
    """Return a clean question string, or None if the model output isn't usable."""
    if not isinstance(data, dict):
        return None
    q = _clean(data.get("question"), 220)
    if len(q) < 15 or "?" not in q:
        return None
    if any(_norm(q) == _norm(r) for r in (recent or [])):
        return None
    return q


def pick_angle(last: str | None, rng=random) -> str:
    pool = [(a, w) for a, w in ANGLES if a != last] or ANGLES
    return rng.choices([a for a, _ in pool], weights=[w for _, w in pool], k=1)[0]


def pick_fallback(recent: list[str], rng=random) -> str:
    seen = {_norm(r) for r in recent}
    fresh = [x for x in FALLBACKS if _norm(x) not in seen] or FALLBACKS
    return rng.choice(fresh)


def build_embed(question: str) -> discord.Embed:
    emb = discord.Embed(title="⚡ Question of the Day",
                        description=f"**{question}**\n\n*Reply to this message. I read every answer.*",
                        color=0xFFC933)
    emb.set_footer(text=f"{MARKER} · reply to answer")
    return emb


def is_qotd(msg, bot_id: int | None) -> bool:
    if bot_id is not None and getattr(msg.author, "id", None) != bot_id:
        return False
    for e in getattr(msg, "embeds", None) or []:
        f = getattr(e, "footer", None)
        if f and f.text and f.text.startswith(MARKER):
            return True
    return False


def qotd_question(msg) -> str:
    for e in getattr(msg, "embeds", None) or []:
        f = getattr(e, "footer", None)
        if f and f.text and f.text.startswith(MARKER):
            first = (e.description or "").split("\n")[0]
            return first.strip("* ").strip()
    return ""


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

    async def generate(self, guild, recent: list[str]) -> tuple[str, str]:
        """Returns (question, source) where source is 'llm' or 'fallback'."""
        angle = pick_angle(self._last_angle)
        system = SYSTEM_PROMPT.format(today=datetime.now(TZ).strftime("%A, %B %d, %Y"))
        facts = await self._facts(guild)
        user = (f"ANGLE for today: {angle}\n\n"
                f"SERVER FACTS (optional, use only if it fits the angle):\n{facts or '(none available)'}\n\n"
                f"RECENT questions (do not repeat):\n" + ("\n".join(f"- {r}" for r in recent[:RECENT_KEEP]) or "- (none)"))
        for _ in range(2):
            q = validate(parse_json(await self._ask_llm(system, user) or ""), recent)
            if q:
                self._last_angle = angle
                return q, "llm"
        print("[dailyq] model gave nothing usable; using the fallback bank")
        return pick_fallback(recent), "fallback"

    # ── posting ───────────────────────────────────────────────────────────────

    async def post_question(self, respect_today: bool = False):
        channel = await self._get_channel()
        if channel is None:
            return None
        history = await self._scan(channel)
        now = datetime.now(TZ)
        if respect_today and history and history[0].created_at.astimezone(TZ).date() == now.date():
            self._posted_date = now.date()           # already asked today (e.g. before a redeploy)
            return None
        recent = [qotd_question(m) for m in history][:RECENT_KEEP] or self.recent
        q, source = await self.generate(getattr(channel, "guild", None), recent)
        msg = await channel.send(embed=build_embed(q), allowed_mentions=discord.AllowedMentions.none())
        self._posted_date = now.date()
        self.recent = ([q] + self.recent)[:RECENT_KEEP]
        print(f"[dailyq] posted ({source}): {q[:80]}")
        return msg

    # ── admin ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="dailyq_now", description="(Admin) Post a Question of the Day right now.")
    @app_commands.default_permissions(manage_guild=True)
    async def dailyq_now(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            msg = await self.post_question()
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

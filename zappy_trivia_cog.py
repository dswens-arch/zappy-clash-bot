"""
zappy_trivia_cog.py — Zappy Trivia Night.

Every week a Zappy agent hosts a live trivia round in the channel:
  * 10 questions, one every minute, each from a RANDOM category
    (10 popular trivia categories + Zappies lore),
  * every question is a message with 4 answer buttons and a 15 second clock,
  * a correct answer scores more the faster you click (100 down to 10),
  * recognition only: host shout-outs, a champion callout, an all-time board.
    No CP, no ZAPP, no payouts.

Questions
  * 10 popular categories are written by the model once per round (accuracy
    prompt, no time-sensitive facts, no repeats of recent rounds). Option order
    is shuffled locally so the right answer isn't biased toward one letter.
  * "Zappies Lore" questions are generated from our own data (collection traits,
    Voltball rules, coach signature plays) so they are always correct.

No Algorand calls. Supabase tables: see zappy_trivia_setup.sql (optional: if the
tables are missing, the game still runs, it just can't keep history).

Env (all optional): TRIVIA_ENABLED (1), TRIVIA_CHANNEL_ID, TRIVIA_DAY (0=Mon..6=Sun,
default 4 = Friday), TRIVIA_HOUR (Central hour, default 19), TRIVIA_MODEL
(default claude-sonnet-5-5), TRIVIA_CHAMPION_ROLE_ID (optional), ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from zappy_collection import ZAPPY_COLLECTION

TZ = ZoneInfo("America/Chicago")
CHANNEL_ID = int(os.environ.get("TRIVIA_CHANNEL_ID") or os.environ.get("NEWS_CHANNEL_ID")
                 or os.environ.get("HOLDER_CHANNEL_ID") or 1314066280592052244)
ENABLED_DEFAULT = os.environ.get("TRIVIA_ENABLED", "1") not in ("0", "false", "False", "")
TRIVIA_DAY = int(os.environ.get("TRIVIA_DAY", "4"))
TRIVIA_HOUR = int(os.environ.get("TRIVIA_HOUR", "19"))
MODEL = os.environ.get("TRIVIA_MODEL", "claude-sonnet-5-5")
CHAMPION_ROLE_ID = int(os.environ.get("TRIVIA_CHAMPION_ROLE_ID") or 0)

N_QUESTIONS = 10
ANSWER_SECONDS = 15
INTERVAL_SECONDS = 60
LOBBY_SECONDS = 120
LATENCY_GRACE = 0.8          # seconds of slack for Discord round-trip on the last click
MAX_POINTS, MIN_POINTS = 100, 10
MIN_QUESTIONS_TO_RUN = 7
IMG = "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full/{}.jpg"

CATEGORIES = [
    ("general", "General Knowledge", "🧠"), ("history", "History", "🏛️"),
    ("science", "Science & Nature", "🔬"), ("geography", "Geography", "🌍"),
    ("movies_tv", "Movies & TV", "🎬"), ("music", "Music", "🎵"),
    ("sports", "Sports", "🏅"), ("technology", "Technology", "💻"),
    ("food", "Food & Drink", "🍕"), ("pop_culture", "Pop Culture", "✨"),
    ("zappies", "Zappies Lore", "⚡"),
]
CAT = {k: (name, emoji) for k, name, emoji in CATEGORIES}
LETTERS = "ABCD"

HOST_OPEN = [
    "Welcome to Zappy Trivia Night! Fast fingers beat big brains in here.",
    "The Wire is live and the questions are hot. Let's see who's sharpest.",
    "Agent on the mic! Ten questions, fifteen seconds each, zero mercy.",
]
HOST_CHAMP = [
    "Take a bow, {name}. Sharpest mind in the server tonight.",
    "{name} just ran the board. The Wire has its champion.",
    "Trivia champion of the night: {name}. Somebody check them for a cheat sheet.",
]
HOST_EMPTY = ["Not a single player tonight. The questions are going back in the vault."]


def points_for(elapsed: float) -> int:
    """100 for an instant answer down to 10 at the buzzer; 0 if outside the window."""
    if elapsed < 0 or elapsed > ANSWER_SECONDS + LATENCY_GRACE:
        return 0
    frac = min(1.0, elapsed / ANSWER_SECONDS)
    return max(MIN_POINTS, round(MAX_POINTS - (MAX_POINTS - MIN_POINTS) * frac))


def due(now_local: datetime, last_run_date) -> bool:
    """True during the first 10 minutes of the scheduled hour on the scheduled day, once per day."""
    return (now_local.weekday() == TRIVIA_DAY and now_local.hour == TRIVIA_HOUR
            and now_local.minute < 10 and last_run_date != now_local.date())


# ── question building ──────────────────────────────────────────────────────────

def validate_question(q: dict) -> dict | None:
    try:
        opts = [str(o).strip() for o in q["options"]]
        ans = int(q["answer"])
        text = str(q["question"]).strip()
        cat = q.get("category", "general")
    except (KeyError, TypeError, ValueError):
        return None
    if cat not in CAT or len(opts) != 4 or len(set(o.lower() for o in opts)) != 4:
        return None
    if not (0 <= ans < 4) or not text or any(not o or len(o) > 100 for o in opts) or len(text) > 300:
        return None
    correct = opts[ans]
    random.shuffle(opts)                                   # no letter bias
    return {"category": cat, "question": text, "options": opts, "correct": opts.index(correct),
            "fun_fact": str(q.get("fun_fact", "")).strip()[:200]}


def _numeric_options(correct: int, lo: int = 1) -> list[str]:
    pool = {correct}
    spread = max(2, correct // 4)
    while len(pool) < 4:
        v = correct + random.choice([-1, 1]) * random.randint(1, spread + 2)
        if v >= lo:
            pool.add(v)
    return [str(v) for v in pool]


def zappies_lore_question() -> dict:
    """One accurate question built from our own data."""
    import voltball_engine as E
    kind = random.choice(["trait", "trait", "formation", "coach", "size", "roster"])
    if kind == "trait":
        key = random.choice(["eyewear", "head", "mouth", "eyes", "body", "background", "earring", "skin"])
        aid = random.choice(list(ZAPPY_COLLECTION))
        entry = ZAPPY_COLLECTION[aid]
        vals = sorted({str(e.get(key)) for e in ZAPPY_COLLECTION.values() if e.get(key)})
        correct = str(entry.get(key))
        distract = random.sample([v for v in vals if v != correct], 3)
        label = {"eyewear": "eyewear", "head": "headwear", "mouth": "mouth", "eyes": "eyes",
                 "body": "body", "background": "background", "earring": "earring", "skin": "skin"}[key]
        opts = distract + [correct]
        random.shuffle(opts)
        return {"category": "zappies", "question": f"What is {entry['name']}'s {label} trait?",
                "options": opts, "correct": opts.index(correct), "fun_fact": ""}
    if kind == "formation":
        form = random.choice(list(E.FORMATIONS))
        n = E.FORMATIONS[form]["Striker"]
        opts = _numeric_options(n)
        random.shuffle(opts)
        return {"category": "zappies", "question": f"In Voltball, how many Surge Backs does the {form} formation field?",
                "options": opts, "correct": opts.index(str(n)),
                "fun_fact": "Every formation also fields exactly one QB."}
    if kind == "coach":
        heroes = sorted(E.HERO_SIGNATURES)
        hero = random.choice(heroes)
        sig = E.HERO_SIGNATURES[hero]["label"].replace(" (Coach)", "")
        opts = random.sample([h for h in heroes if h != hero], 3) + [hero]
        random.shuffle(opts)
        return {"category": "zappies", "question": f"Which Voltball coach has the signature play \"{sig}\"?",
                "options": opts, "correct": opts.index(hero), "fun_fact": E.HERO_SIGNATURES[hero]["desc"]}
    if kind == "size":
        n = len(ZAPPY_COLLECTION)
        pool = {n}
        while len(pool) < 4:
            pool.add(n + random.choice([-1, 1]) * random.choice([50, 100, 150, 200, 250, 322]))
        opts = [f"{v:,}" for v in sorted(pool)]
        return {"category": "zappies", "question": "Roughly how many Zappies are in the main collection?",
                "options": opts, "correct": opts.index(f"{n:,}"), "fun_fact": ""}
    n = E.ROSTER_SIZE
    opts = _numeric_options(n, lo=4)
    random.shuffle(opts)
    return {"category": "zappies", "question": "How many Zappies does a Voltball team field in a match?",
            "options": opts, "correct": opts.index(str(n)), "fun_fact": "One QB plus the rest split across the formation."}


LLM_SYSTEM = """You write multiple-choice trivia for a general-audience Discord community. Accuracy is paramount.
Rules:
- Exactly one clearly correct option; the other three must be plausible but definitely wrong.
- Only well-established, easily verifiable facts. NO time-sensitive facts (current records, who currently holds a job, latest releases, prices).
- Vary difficulty: roughly 40% easy, 40% medium, 20% hard. Avoid obscure trick questions and ambiguous wording.
- No "all of the above", "none of the above", or "which is NOT" questions.
- Keep the question under 200 characters and each option under 60 characters. PG-13.
- fun_fact is one short, true sentence that adds a little context to the answer.
Return ONLY a JSON array, no prose, no markdown fences. Each item:
{"category": "<category key>", "question": "...", "options": ["...","...","...","..."], "answer": <0-3 index of the correct option>, "fun_fact": "..."}"""


class Session:
    """State for one running round."""

    def __init__(self, host: dict, questions: list[dict]):
        self.host, self.questions = host, questions
        self.answers: dict[int, tuple[int, float]] = {}
        self.q_index = -1
        self.q_start = 0.0
        self.open = False
        self.scores: dict[int, dict] = {}

    def player(self, uid: int, name: str) -> dict:
        return self.scores.setdefault(uid, {"name": name, "points": 0, "correct": 0, "answered": 0, "time": 0.0})


class ZappyTriviaCog(commands.Cog):
    def __init__(self, bot: commands.Bot, db=None):
        self.bot, self.db = bot, db
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.enabled = ENABLED_DEFAULT
        self.running = False
        self.last_run_date = None
        self.session: Session | None = None
        # timing knobs (tests / /trivia_now quick mode override these)
        self.lobby, self.interval, self.answer_secs = LOBBY_SECONDS, INTERVAL_SECONDS, ANSWER_SECONDS

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def cog_load(self):
        if not self.scheduler.is_running():
            self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.cancel()

    @tasks.loop(minutes=1)
    async def scheduler(self):
        try:
            now = datetime.now(TZ)
            if not self.enabled or self.running or not due(now, self.last_run_date):
                return
            self.last_run_date = now.date()               # claim today before anything slow
            if await self._round_exists_today(now):
                return
            ch = self.bot.get_channel(CHANNEL_ID)
            if ch is None:
                print("[trivia] channel not found, skipping tonight's round.")
                return
            asyncio.create_task(self.run_round(ch))
        except Exception as e:
            print(f"[trivia] scheduler error: {e}")

    @scheduler.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ── data ───────────────────────────────────────────────────────────────────

    async def _q(self, fn):
        if self.db is None:
            return None
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:
            print(f"[trivia] db error (history disabled for this call): {e}")
            return None

    async def _round_exists_today(self, now_local: datetime) -> bool:
        start = datetime.combine(now_local.date(), datetime.min.time(), tzinfo=TZ).astimezone(timezone.utc)
        r = await self._q(lambda: self.db.table("trivia_rounds").select("id")
                          .gte("started_at", start.isoformat()).limit(1).execute())
        return bool(r and r.data)

    async def _recent_questions(self) -> list[str]:
        r = await self._q(lambda: self.db.table("trivia_rounds").select("questions")
                          .order("started_at", desc=True).limit(8).execute())
        out: list[str] = []
        for row in (r.data if r else []) or []:
            out.extend(row.get("questions") or [])
        return out[:120]

    # ── question generation ────────────────────────────────────────────────────

    async def generate(self, cats: list[str], avoid: list[str]) -> list[dict]:
        out: list[dict] = []
        llm_cats = [c for c in cats if c != "zappies"]
        if llm_cats and self.api_key:
            raw = await self._ask_llm(llm_cats, avoid)
            for item in raw:
                v = validate_question(item)
                if v and v["category"] in llm_cats and v["question"] not in avoid:
                    out.append(v)
        if "zappies" in cats:
            seen = set()
            for _ in range(10):
                q = zappies_lore_question()
                if q["question"] not in seen and q["question"] not in avoid:
                    seen.add(q["question"])
                    out.append(q)
                    break
        order = {c: i for i, c in enumerate(cats)}
        out.sort(key=lambda q: order.get(q["category"], 99))
        return out

    async def _ask_llm(self, cats: list[str], avoid: list[str]) -> list[dict]:
        want = [{"category": c, "name": CAT[c][0]} for c in cats]
        user = ("Write exactly one question for each of these categories:\n" + json.dumps(want)
                + ("\n\nDo NOT reuse or closely paraphrase any of these recent questions:\n" + json.dumps(avoid[:80]) if avoid else ""))
        payload = {"model": MODEL, "max_tokens": 3000, "system": LLM_SYSTEM,
                   "messages": [{"role": "user", "content": user}]}
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post("https://api.anthropic.com/v1/messages", json=payload, headers={
                        "x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                        "content-type": "application/json"}, timeout=aiohttp.ClientTimeout(total=90)) as r:
                        data = await r.json()
                if "error" in data:
                    print(f"[trivia] API error: {data['error']}")
                    continue
                text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                text = text[text.index("["): text.rindex("]") + 1]
                items = json.loads(text)
                if isinstance(items, list):
                    return items
            except Exception as e:
                print(f"[trivia] generation attempt {attempt + 1} failed: {e}")
        return []

    # ── round ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _pick_host() -> dict:
        aid = random.choice(list(ZAPPY_COLLECTION))
        return {"id": aid, "name": ZAPPY_COLLECTION[aid]["name"], "img": IMG.format(aid)}

    def _embed_question(self, i: int, n: int, q: dict, closes_at: float) -> discord.Embed:
        name, emoji = CAT[q["category"]]
        e = discord.Embed(title=f"{emoji} {name}", description=f"**{q['question']}**",
                          color=0xF5C518)
        e.add_field(name="​", value="\n".join(f"**{LETTERS[k]}.** {o}" for k, o in enumerate(q["options"])), inline=False)
        e.add_field(name="​", value=f"Closes <t:{int(closes_at)}:R> · faster correct answers score more", inline=False)
        e.set_footer(text=f"Question {i}/{n} · Agent {self.session.host['name']} hosting")
        return e

    def _build_view(self, q_index: int, q: dict, reveal: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for k, o in enumerate(q["options"]):
            style = discord.ButtonStyle.secondary
            if reveal and k == q["correct"]:
                style = discord.ButtonStyle.success
            b = discord.ui.Button(label=f"{LETTERS[k]}. {o}"[:80], style=style, disabled=reveal)
            if not reveal:
                b.callback = self._make_cb(q_index, k)
            view.add_item(b)
        return view

    def _make_cb(self, q_index: int, choice: int):
        async def cb(interaction: discord.Interaction):
            s = self.session
            now = time.monotonic()
            if (s is None or s.q_index != q_index or not s.open
                    or now - s.q_start > self.answer_secs + LATENCY_GRACE):
                return await interaction.response.send_message("⏰ Too late, that question is closed!", ephemeral=True)
            uid = interaction.user.id
            if uid in s.answers:
                return await interaction.response.send_message("🔒 You already locked in an answer for this one.", ephemeral=True)
            s.answers[uid] = (choice, now - s.q_start)
            s.player(uid, interaction.user.display_name)["answered"] += 0   # registers the player
            await interaction.response.send_message(f"🔒 Locked in **{LETTERS[choice]}**! Results when the clock runs out.", ephemeral=True)
        return cb

    def _score_question(self, q: dict) -> tuple[int, int, str | None, float]:
        s = self.session
        right, fastest_name, fastest_t = 0, None, 1e9
        for uid, (choice, t) in s.answers.items():
            p = s.scores[uid]
            p["answered"] += 1
            if choice == q["correct"]:
                p["points"] += points_for(t)
                p["correct"] += 1
                p["time"] += t
                right += 1
                if t < fastest_t:
                    fastest_t, fastest_name = t, p["name"]
        return right, len(s.answers), fastest_name, fastest_t

    async def run_round(self, channel, *, n_questions: int = N_QUESTIONS, quick: bool = False) -> dict | None:
        if self.running:
            return None
        self.running = True
        started = datetime.now(timezone.utc)
        try:
            lobby = 20 if quick else self.lobby
            cats = random.sample([c[0] for c in CATEGORIES], min(n_questions, len(CATEGORIES)))
            host = self._pick_host()
            self.session = Session(host, [])
            begin = time.time() + lobby
            intro = discord.Embed(
                title="⚡ ZAPPY TRIVIA NIGHT",
                description=(f"{random.choice(HOST_OPEN)}\n\n**{n_questions} questions** · a new one every "
                             f"**{self.interval // 60 if self.interval >= 60 else self.interval}"
                             f"{' minute' if self.interval >= 60 else ' sec'}** · **{self.answer_secs} seconds** to answer · tap A/B/C/D.\n"
                             f"The faster you answer correctly, the more points you score (up to {MAX_POINTS}).\n"
                             f"Categories are random. Starting <t:{int(begin)}:R>!"),
                color=0xF5C518)
            intro.set_thumbnail(url=host["img"])
            intro.set_footer(text=f"Hosted by Agent {host['name']} · Zappy Agent Wire")
            await channel.send(embed=intro, allowed_mentions=discord.AllowedMentions.none())

            gen_task = asyncio.create_task(self._generate_for_round(cats))
            await asyncio.sleep(lobby)
            questions = await gen_task
            if len(questions) < min(MIN_QUESTIONS_TO_RUN, n_questions):
                await channel.send("📡 The host's question cards got lost in transit. Trivia is off tonight, back next week!",
                                   allowed_mentions=discord.AllowedMentions.none())
                return None
            self.session.questions = questions
            n = len(questions)

            t0 = time.monotonic()
            for i, q in enumerate(questions):
                wait = t0 + i * self.interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                s = self.session
                s.q_index, s.answers = i, {}
                closes = time.time() + self.answer_secs
                msg = await channel.send(embed=self._embed_question(i + 1, n, q, closes),
                                         view=self._build_view(i, q),
                                         allowed_mentions=discord.AllowedMentions.none())
                s.q_start, s.open = time.monotonic(), True
                await asyncio.sleep(self.answer_secs)
                s.open = False
                right, total, fastest, ft = self._score_question(q)
                lines = [f"✅ **{LETTERS[q['correct']]}. {q['options'][q['correct']]}**",
                         f"{right} of {total} got it" + (f" · ⚡ fastest: **{fastest}** ({ft:.1f}s)" if fastest else "")]
                if q.get("fun_fact"):
                    lines.append(f"_{q['fun_fact']}_")
                e = self._embed_question(i + 1, n, q, closes)
                e.clear_fields()
                e.description = f"**{q['question']}**\n\n" + "\n".join(lines)
                e.color = 0x2ECC71
                try:
                    await msg.edit(embed=e, view=self._build_view(i, q, reveal=True))
                except Exception as ex:
                    print(f"[trivia] couldn't edit question {i + 1}: {ex}")

            result = await self._finish(channel, started, questions)
            return result
        except Exception as e:
            print(f"[trivia] round error: {e}")
            return None
        finally:
            self.running = False
            self.session = None

    async def _generate_for_round(self, cats: list[str]) -> list[dict]:
        avoid = await self._recent_questions()
        return await self.generate(cats, avoid)

    async def _finish(self, channel, started: datetime, questions: list[dict]) -> dict:
        s = self.session
        board = sorted(s.scores.items(), key=lambda kv: (-kv[1]["points"], -kv[1]["correct"], kv[1]["time"]))
        board = [(uid, p) for uid, p in board if p["answered"] > 0]
        medals = ["🥇", "🥈", "🥉"]
        e = discord.Embed(title="🏆 TRIVIA NIGHT RESULTS", color=0xF5C518)
        e.set_thumbnail(url=s.host["img"])
        champ_id = None
        if not board:
            e.description = random.choice(HOST_EMPTY)
        else:
            champ_id, champ = board[0]
            lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} **{p['name']}** · {p['points']} pts · {p['correct']}/{len(questions)} correct"
                     for i, (uid, p) in enumerate(board[:10])]
            e.description = random.choice(HOST_CHAMP).format(name=f"<@{champ_id}>") + "\n\n" + "\n".join(lines)
            e.set_footer(text=f"{len(board)} players · Hosted by Agent {s.host['name']}")
        await channel.send(embed=e, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=champ_id)] if champ_id else False))
        await self._persist(started, s, board, champ_id, questions)
        await self._crown(channel, champ_id)
        return {"board": board, "champion": champ_id}

    async def _persist(self, started, s: Session, board, champ_id, questions):
        if self.db is None:
            return
        def write():
            rid = self.db.table("trivia_rounds").insert({
                "started_at": started.isoformat(), "host_zappy": s.host["name"],
                "champion_user_id": str(champ_id) if champ_id else None,
                "question_count": len(questions), "players": len(board),
                "questions": [q["question"] for q in questions]}).execute().data[0]["id"]
            if board:
                self.db.table("trivia_scores").insert([
                    {"round_id": rid, "user_id": str(uid), "display_name": p["name"], "points": p["points"],
                     "correct": p["correct"], "answered": p["answered"]} for uid, p in board]).execute()
        await self._q(write)

    async def _crown(self, channel, champ_id):
        if not (CHAMPION_ROLE_ID and champ_id):
            return
        try:
            guild = channel.guild
            role = guild.get_role(CHAMPION_ROLE_ID)
            if role is None:
                return
            for m in list(role.members):
                if m.id != champ_id:
                    await m.remove_roles(role, reason="New Zappy Trivia champion")
            member = guild.get_member(champ_id)
            if member and role not in member.roles:
                await member.add_roles(role, reason="Zappy Trivia champion")
        except Exception as e:
            print(f"[trivia] couldn't update champion role: {e}")

    # ── commands ───────────────────────────────────────────────────────────────

    @app_commands.command(name="trivia_now", description="(Admin) Start Zappy Trivia Night right now.")
    @app_commands.describe(questions="How many questions (3-10, default 10)", quick="Short 20 second lobby")
    @app_commands.default_permissions(manage_guild=True)
    async def trivia_now(self, interaction: discord.Interaction, questions: app_commands.Range[int, 3, 10] = 10, quick: bool = True):
        if self.running:
            return await interaction.response.send_message("A trivia round is already running.", ephemeral=True)
        ch = self.bot.get_channel(CHANNEL_ID) or interaction.channel
        await interaction.response.send_message(f"Starting trivia in {ch.mention}.", ephemeral=True)
        asyncio.create_task(self.run_round(ch, n_questions=questions, quick=quick))

    @app_commands.command(name="trivia_status", description="(Admin) Zappy Trivia status.")
    @app_commands.default_permissions(manage_guild=True)
    async def trivia_status(self, interaction: discord.Interaction):
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        await interaction.response.send_message(
            f"{'🟢 on' if self.enabled else '⏸️ paused'} · weekly {days[TRIVIA_DAY]} {TRIVIA_HOUR}:00 Central · "
            f"channel <#{CHANNEL_ID}> · {'a round is running' if self.running else 'idle'} · model `{MODEL}`", ephemeral=True)

    @app_commands.command(name="trivia_toggle", description="(Admin) Pause or resume weekly Zappy Trivia.")
    @app_commands.default_permissions(manage_guild=True)
    async def trivia_toggle(self, interaction: discord.Interaction):
        self.enabled = not self.enabled
        await interaction.response.send_message("🟢 Weekly trivia resumed." if self.enabled else "⏸️ Weekly trivia paused (until restart).", ephemeral=True)

    @app_commands.command(name="trivia_leaderboard", description="All-time Zappy Trivia leaderboard.")
    async def trivia_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        def read():
            rows, start = [], 0
            while True:
                chunk = self.db.table("trivia_scores").select("user_id,display_name,points,round_id").range(start, start + 999).execute().data or []
                rows += chunk
                if len(chunk) < 1000:
                    break
                start += 1000
            champs = self.db.table("trivia_rounds").select("champion_user_id").execute().data or []
            return rows, champs
        got = await self._q(read)
        if not got or not got[0]:
            return await interaction.followup.send("No trivia history yet. Come to the next round!")
        rows, champs = got
        tot: dict[str, dict] = {}
        for r in rows:
            t = tot.setdefault(r["user_id"], {"name": r["display_name"], "points": 0, "rounds": 0})
            t["points"] += r["points"]; t["rounds"] += 1; t["name"] = r["display_name"]
        wins: dict[str, int] = {}
        for c in champs:
            if c.get("champion_user_id"):
                wins[c["champion_user_id"]] = wins.get(c["champion_user_id"], 0) + 1
        top = sorted(tot.items(), key=lambda kv: -kv[1]["points"])[:10]
        lines = [f"`{i + 1}.` **{t['name']}** · {t['points']:,} pts · {t['rounds']} rounds · {wins.get(uid, 0)} 🏆"
                 for i, (uid, t) in enumerate(top)]
        await interaction.followup.send(embed=discord.Embed(title="⚡ All-time Zappy Trivia", description="\n".join(lines), color=0xF5C518))


async def setup(bot):  # pragma: no cover
    await bot.add_cog(ZappyTriviaCog(bot, getattr(bot, "supabase", None)))

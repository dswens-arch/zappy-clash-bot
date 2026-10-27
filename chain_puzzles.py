"""
chain_puzzles.py
----------------
Puzzle generation + rendering for Chain Lightning. No Discord code in here,
so every puzzle can be tested offline.

Three puzzle types, all answered with buttons:
  circuit  — 4 tangled wires; which numbered terminal lights the bulb?
  odd      — 4 Zappy images; three share a trait, which one is the odd one out?
  trivia   — multiple choice; 50% Zappy trivia (generated from our own data),
             50% Open Trivia Database (cached in Supabase, never called live
             during a catch).

A puzzle is a plain dict that can be stored as JSON. It always contains:
    type, correct (int index into options), options (list[str])
Rendering is deterministic from the stored data, so puzzles survive restarts.

OpenTDB: https://opentdb.com/api_config.php
  Data is CC BY-SA 4.0 — the cog shows a credit line on those questions.
  Limits: 1 request / 5 s / IP, max 50 questions per call, 1 category per call.
"""

import asyncio
import io
import math
import os
import random
import time
from urllib.parse import unquote

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from zappy_collection import ZAPPY_COLLECTION
from stats_engine import HERO_STATS

HERE       = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR  = os.path.join(HERE, "zappy-images-full")

PUZZLE_TYPES = ("circuit", "odd", "trivia")

# Which puzzle a Hero (as the Zappy you pass with) sends to the next player.
# Heroes not listed — and every non-Hero Zappy — send a random puzzle type.
HERO_PUZZLE = {
    "Cat": "trivia",   "Bat": "trivia",    "Buck": "trivia",  "Bear": "trivia",
    "Eagle": "odd",    "Alien": "odd",     "Snake": "odd",    "Rabbit": "odd",
    "Wolf": "circuit", "Frog": "circuit",  "Shark": "circuit", "Bison": "circuit",
    "Crocodile": "circuit",
}

TRAITS_BY_DIFFICULTY = {
    "easy":   ["background", "skin"],
    "medium": ["background", "skin", "body", "head"],
    "hard":   ["body", "head", "eyes", "mouth", "earring", "eyewear"],
}

OPENTDB_CATEGORIES = {
    9:  "General Knowledge",
    15: "Video Games",
    17: "Science & Nature",
    18: "Computers",
    20: "Mythology",
    32: "Cartoons & Animation",
}
OPENTDB_CREDIT = "Trivia from OpenTDB (CC BY-SA 4.0)"
ZAPPY_TRIVIA_SHARE = 0.5          # 50/50 Zappy vs OpenTDB
MAX_ANSWER_LEN = 200              # drop OpenTDB questions with absurdly long answers


# ── Fonts ──────────────────────────────────────────────────────────────────────

def _font(size: int) -> "ImageFont.ImageFont":
    for path in (
        os.path.join(HERE, "Ubuntu-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ── Collection index (built once, lazily) ──────────────────────────────────────

_trait_index: dict | None = None
_owned_images: set | None = None


def _image_path(asset_id: int) -> str:
    return os.path.join(IMAGE_DIR, f"{asset_id}.jpg")


def _build_index() -> None:
    """trait -> value -> [asset ids that have a local image]"""
    global _trait_index, _owned_images
    if _trait_index is not None:
        return
    idx: dict = {}
    have = set()
    for aid, entry in ZAPPY_COLLECTION.items():
        if not os.path.exists(_image_path(aid)):
            continue
        have.add(aid)
        for trait in ("background", "body", "earring", "eyes", "eyewear", "head", "mouth", "skin"):
            val = entry.get(trait)
            if val is None:
                continue
            idx.setdefault(trait, {}).setdefault(val, []).append(aid)
    _trait_index, _owned_images = idx, have


def has_collection_images() -> bool:
    _build_index()
    return bool(_owned_images)


# ── Puzzle type for a Zappy ────────────────────────────────────────────────────

def puzzle_type_for_zappy(hero_type: str | None) -> str:
    if hero_type and hero_type in HERO_PUZZLE:
        return HERO_PUZZLE[hero_type]
    return random.choice(PUZZLE_TYPES)


# ── Circuit puzzle ─────────────────────────────────────────────────────────────

WIRE_COLORS = ["#FF4D4D", "#FFD60A", "#3A86FF", "#2ECC71"]
CIRCUIT_WAYPOINTS = {"easy": 1, "medium": 3, "hard": 5}


def make_circuit(difficulty: str) -> dict:
    seed = random.randrange(1, 10**9)
    rng = random.Random(seed)
    perm = rng.sample(range(4), 4)          # terminal i -> socket perm[i]
    bulb_socket = rng.randrange(4)
    correct = perm.index(bulb_socket)
    return {
        "type": "circuit", "difficulty": difficulty, "seed": seed,
        "options": ["1", "2", "3", "4"], "correct": correct,
        "question": "Which numbered terminal powers the 💡 bulb? Trace the wire.",
        "title": "⚡ Wire the Circuit",
    }


def _catmull(points, samples=18):
    """Smooth curve through points (Catmull-Rom)."""
    pts = [points[0]] + points + [points[-1]]
    out = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for s in range(samples):
            t = s / samples
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t +
                       (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 +
                       (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t +
                       (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 +
                       (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))
    out.append(points[-1])
    return out


def render_circuit(puzzle: dict) -> io.BytesIO:
    S = 2                                   # supersample for smooth lines
    W, H = 800 * S, 380 * S
    rng = random.Random(puzzle["seed"])
    perm = rng.sample(range(4), 4)
    bulb_socket = rng.randrange(4)
    n_way = CIRCUIT_WAYPOINTS.get(puzzle.get("difficulty", "medium"), 3)

    img = Image.new("RGB", (W, H), "#12141C")
    d = ImageDraw.Draw(img)

    top, bottom = 60 * S, H - 60 * S
    ys = [top + i * (bottom - top) / 3 for i in range(4)]
    left_x, right_x = 120 * S, W - 120 * S

    # Panels
    d.rounded_rectangle((10 * S, 20 * S, 110 * S, H - 20 * S), 18 * S, fill="#1F2330", outline="#3A4156", width=3 * S)
    d.rounded_rectangle((W - 110 * S, 20 * S, W - 10 * S, H - 20 * S), 18 * S, fill="#1F2330", outline="#3A4156", width=3 * S)

    # Wires
    order = list(range(4))
    rng.shuffle(order)
    for i in order:
        pts = [(left_x, ys[i])]
        for k in range(n_way):
            x = left_x + (right_x - left_x) * (k + 1) / (n_way + 1)
            pts.append((x + rng.uniform(-18, 18) * S, rng.uniform(top - 20 * S, bottom + 20 * S)))
        pts.append((right_x, ys[perm[i]]))
        curve = _catmull(pts)
        d.line(curve, fill=WIRE_COLORS[i], width=7 * S, joint="curve")

    # Terminals (left) and sockets (right)
    num_font = _font(26 * S)
    for i in range(4):
        r = 22 * S
        cx, cy = 60 * S, ys[i]
        d.line([(cx, cy), (left_x, cy)], fill=WIRE_COLORS[i], width=7 * S)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WIRE_COLORS[i], outline="#FFFFFF", width=2 * S)
        d.text((cx, cy), str(i + 1), font=num_font, fill="#12141C", anchor="mm")

    for s in range(4):
        cx, cy = W - 60 * S, ys[s]
        r = 22 * S
        d.line([(right_x, cy), (cx, cy)], fill="#6B7390", width=7 * S)
        if s == bulb_socket:
            for glow, alpha in ((46, "#4A3F10"), (34, "#7A6614")):
                g = glow * S
                d.ellipse((cx - g, cy - g, cx + g, cy + g), fill=alpha)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#FFE94D", outline="#FFFFFF", width=2 * S)
            # Lightning bolt drawn as a polygon (emoji glyphs aren't in the bundled fonts)
            bolt = [(2, -14), (-9, 2), (-1, 2), (-4, 14), (9, -3), (1, -3)]
            d.polygon([(cx + x * S, cy + y * S) for x, y in bolt], fill="#12141C")
        else:
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#2A2F40", outline="#6B7390", width=2 * S)

    d.text((60 * S, 24 * S), "IN", font=_font(14 * S), fill="#8A93B0", anchor="mm")
    d.text((W - 60 * S, 24 * S), "OUT", font=_font(14 * S), fill="#8A93B0", anchor="mm")

    img = img.resize((W // S, H // S), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


# ── Odd Zappy out ──────────────────────────────────────────────────────────────

def make_odd(difficulty: str) -> dict | None:
    _build_index()
    if not _trait_index:
        return None
    traits = TRAITS_BY_DIFFICULTY.get(difficulty, TRAITS_BY_DIFFICULTY["medium"])
    for _ in range(40):
        trait = random.choice(traits)
        values = _trait_index.get(trait, {})
        common = [v for v, ids in values.items() if len(ids) >= 3]
        if not common or len(values) < 2:
            continue
        # Weight toward common values so the three matching ones are easy to spot
        value = random.choice(common)
        others = [v for v in values if v != value]
        if not others:
            continue
        odd_value = random.choice(others)
        trio = random.sample(values[value], 3)
        odd = random.choice(values[odd_value])
        ids = trio + [odd]
        order = list(range(4))
        random.shuffle(order)
        ids = [ids[i] for i in order]
        correct = ids.index(odd)
        return {
            "type": "odd", "difficulty": difficulty, "trait": trait,
            "ids": ids, "options": ["1", "2", "3", "4"], "correct": correct,
            "question": f"Three of these Zappies share the same **{trait}**. Which one is the odd one out?",
            "title": "👀 Odd Zappy Out",
        }
    return None


def render_odd(puzzle: dict) -> io.BytesIO:
    tile, gap, pad = 190, 14, 16
    W = pad * 2 + tile * 4 + gap * 3
    H = pad * 2 + tile + 30
    img = Image.new("RGB", (W, H), "#12141C")
    d = ImageDraw.Draw(img)
    f = _font(22)
    for i, aid in enumerate(puzzle["ids"]):
        x = pad + i * (tile + gap)
        try:
            z = Image.open(_image_path(aid)).convert("RGB").resize((tile, tile), Image.LANCZOS)
            img.paste(z, (x, pad))
        except Exception:
            d.rectangle((x, pad, x + tile, pad + tile), fill="#2A2F40")
        d.ellipse((x + 6, pad + 6, x + 40, pad + 40), fill="#FFD60A", outline="#12141C", width=2)
        d.text((x + 23, pad + 23), str(i + 1), font=f, fill="#12141C", anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


# ── Zappy trivia (generated from our own data) ─────────────────────────────────

def _four(correct, pool):
    wrong = [x for x in pool if x != correct]
    if len(wrong) < 3:
        return None
    opts = random.sample(wrong, 3) + [correct]
    random.shuffle(opts)
    return opts, opts.index(correct)


def _hero_trivia() -> dict | None:
    heroes = [(h, d["abilities"][0]) for h, d in HERO_STATS.items()
              if isinstance(d, dict) and d.get("abilities") and h != "ShittyKitties"]
    if len(heroes) < 4:
        return None
    hero, ab = random.choice(heroes)
    kind = random.choice(("ability_to_hero", "desc_to_ability", "hero_to_ability"))
    if kind == "ability_to_hero":
        res = _four(hero, [h for h, _ in heroes])
        q = f"Which Hero has the **{ab['name']}** ability?"
    elif kind == "desc_to_ability":
        res = _four(ab["name"], [a["name"] for _, a in heroes])
        q = f"Which Hero ability does this?\n> {ab['desc']}"
    else:
        res = _four(ab["name"], [a["name"] for _, a in heroes])
        q = f"What is the **{hero}** Hero's ability called?"
    if not res:
        return None
    opts, correct = res
    return {"type": "trivia", "source": "zappy", "question": q,
            "options": opts, "correct": correct, "title": "🧠 Zappy Trivia"}


def _trait_trivia(difficulty: str) -> dict | None:
    _build_index()
    if not _owned_images:
        return None
    traits = TRAITS_BY_DIFFICULTY.get(difficulty, TRAITS_BY_DIFFICULTY["medium"])
    for _ in range(20):
        aid = random.choice(list(_owned_images))
        trait = random.choice(traits)
        entry = ZAPPY_COLLECTION[aid]
        value = entry.get(trait)
        if value is None:
            continue
        res = _four(value, list(_trait_index.get(trait, {}).keys()))
        if not res:
            continue
        opts, correct = res
        return {"type": "trivia", "source": "zappy", "asset_id": aid,
                "question": f"What **{trait}** does {entry['name']} have?",
                "options": opts, "correct": correct, "title": "🧠 Zappy Trivia"}
    return None


def make_zappy_trivia(difficulty: str) -> dict | None:
    # Easy leans on trait-spotting with the picture; harder leans on Hero lore
    p_trait = {"easy": 0.7, "medium": 0.5, "hard": 0.3}.get(difficulty, 0.5)
    makers = [lambda: _trait_trivia(difficulty), _hero_trivia]
    if random.random() >= p_trait:
        makers.reverse()
    return makers[0]() or makers[1]()


def render_trivia_image(puzzle: dict) -> io.BytesIO | None:
    aid = puzzle.get("asset_id")
    if not aid or not os.path.exists(_image_path(aid)):
        return None
    img = Image.open(_image_path(aid)).convert("RGB").resize((320, 320), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


# ── OpenTDB pool ───────────────────────────────────────────────────────────────

_last_opentdb_call = 0.0
_opentdb_token: str | None = None


async def _opentdb_get(session: aiohttp.ClientSession, url: str, params: dict) -> dict | None:
    """Rate-limited GET (1 request / 5 s / IP). Returns parsed JSON or None."""
    global _last_opentdb_call
    wait = 5.5 - (time.monotonic() - _last_opentdb_call)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_opentdb_call = time.monotonic()
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception as e:
        print(f"[chain_puzzles] OpenTDB request failed: {e}")
        return None


async def refill_trivia_pool(db, batches: int = 3) -> int:
    """
    Pull up to `batches` x 50 questions from OpenTDB into trivia_pool.
    Returns how many rows were added. Safe to call repeatedly: questions are
    unique, and a session token keeps OpenTDB from resending ones we already got.
    """
    global _opentdb_token
    added = 0
    async with aiohttp.ClientSession() as session:
        if not _opentdb_token:
            data = await _opentdb_get(session, "https://opentdb.com/api_token.php", {"command": "request"})
            if data and data.get("response_code") == 0:
                _opentdb_token = data.get("token")

        for _ in range(batches):
            cat = random.choice(list(OPENTDB_CATEGORIES))
            params = {"amount": 50, "category": cat, "encode": "url3986"}
            if _opentdb_token:
                params["token"] = _opentdb_token
            data = await _opentdb_get(session, "https://opentdb.com/api.php", params)
            if not data:
                continue
            code = data.get("response_code")
            if code == 4 and _opentdb_token:      # token exhausted -> reset it
                await _opentdb_get(session, "https://opentdb.com/api_token.php",
                                   {"command": "reset", "token": _opentdb_token})
                continue
            if code != 0:
                continue

            rows = []
            for r in data.get("results", []):
                q = unquote(r["question"])
                correct = unquote(r["correct_answer"])
                wrong = [unquote(w) for w in r["incorrect_answers"]]
                if max(len(correct), *(len(w) for w in wrong)) > MAX_ANSWER_LEN:
                    continue
                rows.append({
                    "source": "opentdb", "category": OPENTDB_CATEGORIES[cat],
                    "difficulty": r["difficulty"], "qtype": r["type"],
                    "question": q, "correct": correct, "wrong": wrong,
                })
            if rows:
                try:
                    res = await asyncio.to_thread(
                        lambda: db.table("trivia_pool")
                        .upsert(rows, on_conflict="question", ignore_duplicates=True)
                        .execute()
                    )
                    added += len(res.data or [])
                except Exception as e:
                    print(f"[chain_puzzles] trivia_pool insert error: {e}")
    return added


async def pool_size(db) -> int:
    try:
        res = await asyncio.to_thread(
            lambda: db.table("trivia_pool").select("id", count="exact").limit(1).execute()
        )
        return res.count or 0
    except Exception:
        return 0


async def make_opentdb_trivia(db, difficulty: str) -> dict | None:
    """Least-recently-used question at this difficulty (any difficulty if none)."""
    for diff in (difficulty, None):
        try:
            def q():
                b = db.table("trivia_pool").select("*")
                if diff:
                    b = b.eq("difficulty", diff)
                return b.order("last_used", desc=False, nullsfirst=True).limit(25).execute()
            res = await asyncio.to_thread(q)
        except Exception as e:
            print(f"[chain_puzzles] trivia_pool read error: {e}")
            return None
        if not res.data:
            continue
        row = random.choice(res.data)
        try:
            await asyncio.to_thread(
                lambda: db.table("trivia_pool")
                .update({"last_used": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                .eq("id", row["id"]).execute()
            )
        except Exception:
            pass
        if row["qtype"] == "boolean":
            opts = ["True", "False"]
        else:
            opts = list(row["wrong"]) + [row["correct"]]
            random.shuffle(opts)
        return {
            "type": "trivia", "source": "opentdb", "title": "🧠 Trivia",
            "question": row["question"], "options": opts,
            "correct": opts.index(row["correct"]),
            "footer": f"{row['category']} · {OPENTDB_CREDIT}",
        }
    return None


# ── Public entry points ────────────────────────────────────────────────────────

async def build_puzzle(ptype: str, difficulty: str, db) -> dict:
    """
    Build a puzzle of the requested type. Falls back gracefully so a catch
    never fails because a data source is empty.
    """
    puzzle = None
    if ptype == "circuit":
        puzzle = make_circuit(difficulty)
    elif ptype == "odd":
        puzzle = make_odd(difficulty)
    elif ptype == "trivia":
        if random.random() >= ZAPPY_TRIVIA_SHARE:
            puzzle = await make_opentdb_trivia(db, difficulty)
        if not puzzle:
            puzzle = make_zappy_trivia(difficulty)
    if not puzzle:
        puzzle = make_circuit(difficulty)         # always available, no data needed
    return puzzle


def render_puzzle_image(puzzle: dict) -> io.BytesIO | None:
    t = puzzle["type"]
    if t == "circuit":
        return render_circuit(puzzle)
    if t == "odd":
        return render_odd(puzzle)
    if t == "trivia":
        return render_trivia_image(puzzle)
    return None


def answer_labels(puzzle: dict) -> tuple[list[str], str | None]:
    """
    Button labels for a puzzle. Short answers go straight on the buttons; long
    ones become A/B/C/D with the full text listed in the message body.
    Returns (labels, option_list_text_or_None).
    """
    opts = puzzle["options"]
    if puzzle["type"] != "trivia":
        return opts, None
    if all(len(o) <= 40 for o in opts):
        return opts, None
    letters = ["A", "B", "C", "D"][: len(opts)]
    body = "\n".join(f"**{l}.** {o}" for l, o in zip(letters, opts))
    return letters, body

"""
zappy_news_card.py
------------------
Pillow renderer for Zappy Agent Wire broadcast cards: dark card, yellow header bar,
art on the left, segment label + headline + detail lines on the right.

The art is whatever the story is ABOUT: the Clash winner, a record holder, the
Sparks in an Office duel. When a story has no specific subject (price, headlines,
chain stats) the filing agent's own art is used. Who filed the report is always
called out in text in the footer ticker ("FILED BY AGENT Zappy #…").

`art_items` is a list of 1 or 2 dicts:
    {"path": "zappy-images-full/123.jpg",   # local file (tried first), optional
     "url":  "https://…",                   # fetched if no local file, optional
     "caption": "WINNER",                   # small tag on the tile, optional
     "fallback": "MOSS T2",                 # text for the stand-in tile if no image loads
     "color": (61, 255, 154)}               # stand-in tile colour, optional

One item fills the frame. Two items render as side-by-side tiles with a "VS" disc
(used for Office duels). If an image can't be loaded the tile falls back to a
coloured stand-in, so a card is never blocked by a dead image link.

    buf = await render_news_card(
        agent_name="Zappy #309", art_items=[{"path": "...", "caption": "WINNER"}],
        segment_label="CLASH RECAP", headline="...", lines=["..."],
        accent=(255, 210, 50), breaking=False, footer="CoinGecko")
    await channel.send(file=discord.File(buf, filename="zappy_news.png"))
"""

from __future__ import annotations

import io
import os

import aiohttp
from PIL import Image, ImageDraw, ImageFont

W, H = 1000, 570
BG        = (14, 16, 24)
PANEL     = (24, 28, 42)
YELLOW    = (255, 210, 50)
WHITE     = (245, 247, 255)
MUTED     = (160, 170, 200)
RED       = (255, 70, 70)
DARK      = (20, 20, 20)

BRAND = "ZAPPY AGENT WIRE"

ART = 380
ART_X, ART_Y = 40, 100
TXT_X = ART_X + ART + 40
TXT_W = W - TXT_X - 40

_HERE = os.path.dirname(os.path.abspath(__file__))
_URL_CACHE: dict[str, bytes] = {}       # small in-memory cache so repeat Spark art isn't refetched
_URL_CACHE_MAX = 64


def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        os.path.join(_HERE, "fonts", name),
        f"./fonts/{name}",
        os.path.join(_HERE, "Ubuntu-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and " ".join(lines) != text:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


async def _load_art(image_path: str | None, image_url: str | None) -> Image.Image | None:
    if image_path:
        for p in (image_path, os.path.join(_HERE, image_path)):
            if os.path.exists(p):
                try:
                    return Image.open(p).convert("RGB")
                except Exception:
                    pass
    if image_url:
        data = _URL_CACHE.get(image_url)
        if data is None:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(image_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            data = await r.read()
                        else:
                            print(f"[news-card] image HTTP {r.status}: {image_url}")
            except Exception as e:
                print(f"[news-card] image fetch failed: {type(e).__name__}: {e}")
            if data:
                if len(_URL_CACHE) >= _URL_CACHE_MAX:
                    _URL_CACHE.pop(next(iter(_URL_CACHE)))
                _URL_CACHE[image_url] = data
        if data:
            try:
                return Image.open(io.BytesIO(data)).convert("RGB")
            except Exception:
                _URL_CACHE.pop(image_url, None)
    return None


def _tile(img: Image.Image, d: ImageDraw.ImageDraw, art: Image.Image | None, item: dict,
          x: int, y: int, size: int, radius: int):
    """Draw one art tile (or its coloured stand-in) at x, y."""
    if art:
        art = art.resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, size, size], radius=radius, fill=255)
        img.paste(art, (x, y), mask)
        return
    color = item.get("color") or YELLOW
    dim = tuple(int(c * 0.28) for c in color)
    d.rounded_rectangle([x, y, x + size, y + size], radius=radius, fill=dim, outline=color, width=3)
    bolt = [(0.56, 0.12), (0.27, 0.54), (0.46, 0.54), (0.38, 0.84), (0.73, 0.40), (0.53, 0.40)]
    d.polygon([(x + int(px * size), y + int((py - 0.04) * size)) for px, py in bolt], fill=color)
    label = item.get("fallback")
    if label:
        d.text((x + size // 2, y + size - size // 7), label, font=_font("Poppins-Bold.ttf", max(16, size // 12)),
               fill=WHITE, anchor="mm")


def _pill(d: ImageDraw.ImageDraw, text: str, x: int, y: int, fill, *, center: bool = False, size: int = 22):
    f = _font("Poppins-Bold.ttf", size)
    w = int(d.textlength(text, font=f)) + 32
    h = size + 16
    x0 = x - w // 2 if center else x
    d.rounded_rectangle([x0, y, x0 + w, y + h], radius=h // 2, fill=fill)
    d.text((x0 + w // 2, y + h // 2 + 1), text, font=f, fill=DARK, anchor="mm")


async def render_news_card(
    *, agent_name: str, art_items: list[dict],
    segment_label: str, headline: str, lines: list[str] | None = None,
    accent: tuple = YELLOW, breaking: bool = False, footer: str | None = None,
) -> io.BytesIO:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # ── header: agent-wire branding ───────────────────────────────────────────
    bar_color = RED if breaking else YELLOW
    on_bar = WHITE if breaking else DARK
    d.rectangle([0, 0, W, 70], fill=bar_color)
    title = f"{BRAND}  ·  AGENT ALERT" if breaking else BRAND
    d.text((40, 35), title, font=_font("Poppins-Bold.ttf", 32), fill=on_bar, anchor="lm")
    f_live = _font("Poppins-Bold.ttf", 22)
    live_txt = "AUTONOMOUS · LIVE"
    lw = int(d.textlength(live_txt, font=f_live))
    d.text((W - 40, 35), live_txt, font=f_live, fill=on_bar, anchor="rm")
    d.ellipse([W - 40 - lw - 30, 26, W - 40 - lw - 12, 44], fill=on_bar if breaking else (210, 40, 40))

    # ── art frame ─────────────────────────────────────────────────────────────
    items = (art_items or [{}])[:2]
    d.rounded_rectangle([ART_X - 6, ART_Y - 6, ART_X + ART + 6, ART_Y + ART + 6], radius=18, fill=accent)
    loaded = [await _load_art(it.get("path"), it.get("url")) for it in items]

    if len(items) == 1:
        _tile(img, d, loaded[0], items[0], ART_X, ART_Y, ART, 14)
        if items[0].get("caption"):
            _pill(d, items[0]["caption"], ART_X + 14, ART_Y + 14, accent)
    else:
        d.rounded_rectangle([ART_X, ART_Y, ART_X + ART, ART_Y + ART], radius=14, fill=PANEL)
        tile = 172
        gap = (ART - 2 * tile) // 3
        ty = ART_Y + 70
        for i, (it, art) in enumerate(zip(items, loaded)):
            tx = ART_X + gap + i * (tile + gap)
            _tile(img, d, art, it, tx, ty, tile, 12)
            if it.get("caption"):
                _pill(d, it["caption"], tx + tile // 2, ty + tile + 16, accent, center=True, size=18)
        cx, cy = ART_X + ART // 2, ty + tile // 2
        d.ellipse([cx - 24, cy - 24, cx + 24, cy + 24], fill=BG, outline=accent, width=3)
        d.text((cx, cy + 1), "VS", font=_font("Poppins-Bold.ttf", 20), fill=WHITE, anchor="mm")

    # ── right panel ───────────────────────────────────────────────────────────
    d.rounded_rectangle([TXT_X, 100, W - 40, 100 + ART], radius=18, fill=PANEL)
    inner_x, inner_w = TXT_X + 28, TXT_W - 56
    f_label = _font("Poppins-Bold.ttf", 22)
    f_head  = _font("Poppins-Bold.ttf", 40)
    f_body  = _font("Poppins-Medium.ttf", 24)

    label_w = d.textlength(segment_label, font=f_label) + 36
    d.rounded_rectangle([inner_x, 124, inner_x + label_w, 164], radius=20, fill=accent)
    d.text((inner_x + 18, 144), segment_label, font=f_label, fill=DARK, anchor="lm")

    y = 188
    for ln in _wrap(d, headline, f_head, inner_w, 4):
        d.text((inner_x, y), ln, font=f_head, fill=WHITE)
        y += 52
    y += 8
    for text in (lines or []):
        for ln in _wrap(d, text, f_body, inner_w, 2):
            if y > 100 + ART - 40:
                break
            d.text((inner_x, y), ln, font=f_body, fill=MUTED)
            y += 32
        y += 4

    # ── ticker: who filed it (text callout) ───────────────────────────────────
    d.rectangle([0, H - 56, W, H], fill=PANEL)
    d.rectangle([0, H - 56, 10, H], fill=accent)
    f_filed = _font("Poppins-Medium.ttf", 20)
    d.text((30, H - 28), "FILED BY AGENT", font=f_filed, fill=MUTED, anchor="lm")
    x = 30 + int(d.textlength("FILED BY AGENT", font=f_filed)) + 14
    d.text((x, H - 28), agent_name, font=_font("Poppins-Bold.ttf", 24), fill=YELLOW, anchor="lm")
    if footer:
        d.text((W - 30, H - 28), footer, font=f_filed, fill=MUTED, anchor="rm")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

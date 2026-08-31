"""
clash_flex_card.py
-------------------
Renders a shareable "flex" card for a single Zappy: full art with a
dark footer panel showing name, combo effect (or hero/collab tag),
notable traits, each ability's name + description, and VLT/INS/SPK
stat pills. Self-contained — no external template file, just the
Zappy's own art (unlike clash_winner_card.py, which composites onto
clash_champion_template.png).

The footer height is computed from the actual content (number of
traits/abilities and how much their text wraps), so a Zappy with no
abilities gets a short card and one with two long ability descs gets
a taller one — nothing gets cut off.

Usage:
    buf = await render_flex_card(
        zappy_name="Zappy #84",
        stats={"VLT": 28, "INS": 40, "SPK": 72},
        image_url="https://raw.githubusercontent.com/.../2616130337.jpg",
        badge="Lucky Fool (+20 SPK)",
        trait_line="Cat Hat head · Party Popper earring · Tattooed skin",
        abilities=[{"name": "Mini Nine Lives", "desc": "20% chance, once per battle, to survive a KO at 1 HP."}],
    )
    await channel.send(file=discord.File(buf, filename="flex.png"))
"""

import io
import os
import re
import aiohttp
from PIL import Image, ImageDraw, ImageFont

# Strip emoji/pictographs before drawing text into the PNG — Poppins has no
# emoji glyphs, so they'd render as tofu boxes. Discord renders emoji fine
# in the chat message text itself, so the blurb keeps them.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()

# ─────────────────────────────────────────────
# Output size — square art on top, variable-height footer below
# ─────────────────────────────────────────────
ART_SIZE     = 1024
OUT_W        = ART_SIZE
SIDE_MARGIN  = 60
MAX_FOOTER_H = 1400  # generous ceiling; canvas is cropped to actual content height

# ─────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────
GOLD    = (255, 210,  50, 255)
CYAN    = ( 80, 220, 255, 255)
MUTED   = (180, 190, 220, 255)
WHITE   = (245, 246, 250, 255)
PANEL   = ( 15,  12,  28, 235)
PILL_BG = ( 40,  34,  70, 255)

# ─────────────────────────────────────────────
# Fonts
# ─────────────────────────────────────────────
def _font(name, size):
    for path in [
        f"./fonts/{name}",
        f"/usr/share/fonts/truetype/google-fonts/{name}",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

# ─────────────────────────────────────────────
# Image fetch — same direct-GET approach as clash_winner_card.py
# ─────────────────────────────────────────────
async def _fetch_image(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return Image.open(io.BytesIO(await r.read())).convert("RGBA")
                print(f"[flex_card] {r.status} {url}")
    except Exception as e:
        print(f"[flex_card] fetch failed {url}: {e}")
    return None

# ─────────────────────────────────────────────
# Text helpers
# ─────────────────────────────────────────────
def _fit(draw, text, max_size, min_size, max_width, bold=True):
    fname = "Poppins-Bold.ttf" if bold else "Poppins-Medium.ttf"
    size = max_size
    while size >= min_size:
        f = _font(fname, size)
        if draw.textlength(text, font=f) <= max_width:
            return f
        size -= 6
    return _font(fname, min_size)


def _wrap(draw, text, font, max_width, max_lines=3):
    """Greedy word-wrap. Truncates with an ellipsis past max_lines."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines and draw.textlength(lines[-1] + "…", font=font) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def _pill(draw, cx, y, label, value, fill):
    """Draw a small rounded stat pill centered at cx, top y. Returns its width."""
    f_label = _font("Poppins-Medium.ttf", 30)
    f_value = _font("Poppins-Bold.ttf", 46)
    w = max(
        draw.textlength(label, font=f_label),
        draw.textlength(str(value), font=f_value),
    ) + 60
    h = 118
    x0 = cx - w / 2
    draw.rounded_rectangle([x0, y, x0 + w, y + h], radius=22, fill=PILL_BG)
    draw.text((cx, y + 22), label, font=f_label, fill=MUTED, anchor="mt")
    draw.text((cx, y + 54), str(value), font=f_value, fill=fill, anchor="mt")
    return w

# ─────────────────────────────────────────────
# Main renderer
# ─────────────────────────────────────────────
async def render_flex_card(
    zappy_name: str,
    stats: dict,
    image_url: str = "",
    badge: str = "",
    trait_line: str = "",
    abilities: list | None = None,
) -> io.BytesIO:

    abilities = abilities or []
    text_w = OUT_W - SIDE_MARGIN * 2

    # Oversized canvas — cropped to the real content height at the end.
    canvas = Image.new("RGBA", (OUT_W, ART_SIZE + MAX_FOOTER_H), (20, 15, 40, 255))

    # ── Zappy art, cropped square, filling the top ──────────────────
    zappy_img = await _fetch_image(image_url)
    if zappy_img:
        zw, zh = zappy_img.size
        if zw != zh:
            sq = min(zw, zh)
            zappy_img = zappy_img.crop(((zw - sq) // 2, (zh - sq) // 2, (zw + sq) // 2, (zh + sq) // 2))
        art = zappy_img.resize((ART_SIZE, ART_SIZE), Image.LANCZOS).convert("RGBA")
        canvas.paste(art, (0, 0))

    # ── Footer panel, with a short gradient fade at the top edge ──────
    panel = Image.new("RGBA", (OUT_W, MAX_FOOTER_H), PANEL)
    fade_h = 60
    for i in range(fade_h):
        alpha = int(PANEL[3] * (i / fade_h))
        panel.paste((*PANEL[:3], alpha), (0, i, OUT_W, i + 1))
    canvas.alpha_composite(panel, (0, ART_SIZE))

    draw = ImageDraw.Draw(canvas)
    cx = OUT_W // 2
    left = SIDE_MARGIN
    top = ART_SIZE + 30

    # Name
    clean_name = _strip_emoji(zappy_name) or zappy_name
    f_name = _fit(draw, clean_name, 66, 40, text_w)
    draw.text((cx, top), clean_name, font=f_name, fill=WHITE, anchor="mt")
    top += 64

    # Combo effect / hero / collab badge, if any
    clean_badge = _strip_emoji(badge)
    if clean_badge:
        f_badge = _fit(draw, clean_badge, 36, 24, text_w, bold=False)
        draw.text((cx, top), clean_badge, font=f_badge, fill=GOLD, anchor="mt")
        top += 48

    top += 18

    # Notable traits, left-aligned, wrapped
    if trait_line:
        f_trait = _font("Poppins-Medium.ttf", 30)
        for line in _wrap(draw, trait_line, f_trait, text_w, max_lines=2):
            draw.text((left, top), line, font=f_trait, fill=MUTED, anchor="lt")
            top += 40
        top += 14

    # Abilities — name + wrapped description, each
    if abilities:
        f_ab_name = _font("Poppins-Bold.ttf", 32)
        f_ab_desc = _font("Poppins-Medium.ttf", 27)
        for ab in abilities:
            if not isinstance(ab, dict) or not ab.get("name"):
                continue
            draw.text((left, top), ab["name"], font=f_ab_name, fill=CYAN, anchor="lt")
            top += 42
            for line in _wrap(draw, ab.get("desc", ""), f_ab_desc, text_w, max_lines=2):
                draw.text((left, top), line, font=f_ab_desc, fill=MUTED, anchor="lt")
                top += 34
            top += 16

    top += 14

    # Stat pills — VLT / INS / SPK
    pills = [
        ("VLT", stats.get("VLT", "?"), CYAN),
        ("INS", stats.get("INS", "?"), GOLD),
        ("SPK", stats.get("SPK", "?"), MUTED),
    ]
    gap = 24
    widths = []
    dummy = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    for label, value, _ in pills:
        f_label = _font("Poppins-Medium.ttf", 30)
        f_value = _font("Poppins-Bold.ttf", 46)
        w = max(dummy.textlength(label, font=f_label), dummy.textlength(str(value), font=f_value)) + 60
        widths.append(w)
    total_w = sum(widths) + gap * (len(pills) - 1)
    x = cx - total_w / 2
    for (label, value, fill), w in zip(pills, widths):
        _pill(draw, x + w / 2, top, label, value, fill)
        x += w + gap
    top += 118 + 36  # pill height + bottom padding

    out = canvas.crop((0, 0, OUT_W, top)).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

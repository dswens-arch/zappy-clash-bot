"""
clash_entry_card.py
-------------------
Renders a small Pillow image for each Zappy Clash bracket entry.
Matches the mock: Zappy thumbnail on the left, username + number,
color-coded VLT / INS / SPK stat pills on the right.

Usage:
    buf = await render_entry_card(
        display_name="jasy5221",
        zappy_name="Zappy #88",
        stats={"VLT": 28, "INS": 48, "SPK": 48},
        image_url="https://ipfs.io/ipfs/...",
    )
    await channel.send(file=discord.File(buf, filename="entry.png"))
"""

import io
import asyncio
import aiohttp
from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────
# Dimensions
# ─────────────────────────────────────────────
W, H        = 360, 64       # card size
THUMB_SIZE  = 46            # square thumbnail
THUMB_X     = 10            # left padding
THUMB_Y     = (H - THUMB_SIZE) // 2
THUMB_R     = 8             # corner radius on thumbnail

# ─────────────────────────────────────────────
# Colours  (matching GP cog palette)
# ─────────────────────────────────────────────
BG          = (30,  32,  44, 255)   # dark navy
CARD_BG     = (43,  45,  58, 255)   # slightly lighter card
ACCENT      = (240, 178,  50, 255)  # Zappy gold  ⚡
WHITE       = (240, 245, 255, 255)
MUTED       = (160, 170, 195, 255)
SHADOW      = (0,   0,   0,  180)

# Stat pill colours  (label bg / label text / value text)
STAT_COLORS = {
    "VLT": ((240, 178,  50, 60),  (240, 178,  50, 255), (240, 245, 255, 255)),  # gold
    "INS": (( 87, 242, 135, 60),  ( 87, 242, 135, 255), (240, 245, 255, 255)),  # green
    "SPK": ((  0, 176, 244, 60),  (  0, 176, 244, 255), (240, 245, 255, 255)),  # blue
}

LIGHTNING_EMOJI = "⚡"   # fallback drawn as text if no image

# ─────────────────────────────────────────────
# Fonts  (same helper as grand_prix_cog.py)
# ─────────────────────────────────────────────
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for path in [
        f"./fonts/{name}",
        f"/usr/share/fonts/truetype/google-fonts/{name}",
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        import os
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

_FONT_BOLD  = _font("Poppins-Bold.ttf",    15)
_FONT_MED   = _font("Poppins-Medium.ttf",  15)
_FONT_SM    = _font("Poppins-Regular.ttf", 13)
_FONT_LABEL = _font("Poppins-Bold.ttf",    12)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _rounded_rect(draw: ImageDraw.ImageDraw, xy, radius: int, fill):
    """Draw a filled rounded rectangle on an RGBA draw context."""
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)


def _paste_rounded(canvas: Image.Image, thumb: Image.Image, x: int, y: int, size: int, radius: int):
    """Paste thumb onto canvas with rounded corners via a mask."""
    thumb = thumb.resize((size, size), Image.LANCZOS).convert("RGBA")

    # Build circular/rounded mask
    mask = Image.new("L", (size, size), 0)
    md   = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)

    canvas.paste(thumb, (x, y), mask)


async def _fetch_image(url: str, size: int) -> Image.Image | None:
    """Fetch a remote image and return as RGBA PIL Image, or None on failure."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                img  = Image.open(io.BytesIO(data)).convert("RGBA")
                return img
    except Exception as e:
        print(f"[clash_entry_card] image fetch failed: {e}")
        return None


def _draw_lightning_fallback(canvas: Image.Image, x: int, y: int, size: int):
    """Draw a dark rounded square with a gold ⚡ as fallback thumbnail."""
    draw = ImageDraw.Draw(canvas)
    _rounded_rect(draw, (x, y, x + size, y + size), radius=THUMB_R, fill=(50, 52, 68, 255))
    # Gold accent border
    draw.rounded_rectangle([x, y, x + size, y + size], radius=THUMB_R, outline=(*ACCENT[:3], 160), width=2)
    # Lightning bolt text centred
    fb = _font("Poppins-Bold.ttf", 28)
    draw.text((x + size // 2, y + size // 2), "⚡", font=fb, fill=ACCENT, anchor="mm")


def _draw_stat_pill(draw: ImageDraw.ImageDraw, x: int, y: int, label: str, value: str):
    """
    Draw one stat pill:
      [ LABEL  value ]
    Returns the right edge x so the next pill can be placed.
    """
    pill_bg, label_color, value_color = STAT_COLORS.get(label, (MUTED, WHITE, WHITE))

    # Measure text
    lw = draw.textlength(label, font=_FONT_LABEL)
    vw = draw.textlength(value, font=_FONT_MED)

    PAD_X   = 7
    GAP     = 5
    pill_w  = int(PAD_X + lw + GAP + vw + PAD_X)
    pill_h  = 22
    pill_r  = 4

    # Background
    _rounded_rect(draw, (x, y, x + pill_w, y + pill_h), radius=pill_r, fill=pill_bg)

    # Left accent bar
    bar_color = label_color
    draw.rounded_rectangle([x, y, x + 3, y + pill_h], radius=pill_r, fill=bar_color)

    # Label
    draw.text((x + PAD_X, y + pill_h // 2), label, font=_FONT_LABEL, fill=label_color, anchor="lm")

    # Value
    draw.text((x + PAD_X + lw + GAP, y + pill_h // 2), value, font=_FONT_MED, fill=value_color, anchor="lm")

    return x + pill_w   # right edge


# ─────────────────────────────────────────────
# Main renderer
# ─────────────────────────────────────────────

async def render_entry_card(
    display_name: str,
    zappy_name:   str,
    stats:        dict,
    image_url:    str = "",
) -> io.BytesIO:
    """
    Render and return a BytesIO PNG of the entry card.

    Args:
        display_name: Discord display name of the entering user
        zappy_name:   e.g. "Zappy #88"  or  "Zappy Hero — Wolf"
        stats:        dict with keys VLT, INS, SPK
        image_url:    Zappy image URL (IPFS or gateway); fallback drawn if empty/fails
    """

    # ── Canvas ───────────────────────────────
    canvas = Image.new("RGBA", (W, H), BG)
    draw   = ImageDraw.Draw(canvas)

    # Card background (slightly lighter inset)
    _rounded_rect(draw, (4, 4, W - 4, H - 4), radius=10, fill=CARD_BG)

    # Gold left accent bar
    draw.rounded_rectangle([4, 4, 8, H - 4], radius=4, fill=ACCENT)

    # ── Thumbnail ────────────────────────────
    thumb_img = None
    if image_url:
        thumb_img = await _fetch_image(image_url, THUMB_SIZE)

    if thumb_img:
        _paste_rounded(canvas, thumb_img, THUMB_X + 4, THUMB_Y, THUMB_SIZE, THUMB_R)
    else:
        _draw_lightning_fallback(canvas, THUMB_X + 4, THUMB_Y, THUMB_SIZE)

    # ── Text block ───────────────────────────
    TEXT_X = THUMB_X + 4 + THUMB_SIZE + 12   # right of thumbnail

    # "⚡ display_name  enters with  #88"
    # Split into coloured segments drawn manually
    bolt_w = int(draw.textlength("⚡ ", font=_FONT_BOLD))
    draw.text((TEXT_X, 16), "⚡ ", font=_FONT_BOLD, fill=ACCENT)

    name_w = int(draw.textlength(display_name, font=_FONT_BOLD))
    draw.text((TEXT_X + bolt_w, 16), display_name, font=_FONT_BOLD, fill=WHITE)

    mid_text = "  enters with  "
    mid_w    = int(draw.textlength(mid_text, font=_FONT_MED))
    draw.text((TEXT_X + bolt_w + name_w, 20), mid_text, font=_FONT_MED, fill=MUTED)

    # Extract just the number/label for the gold highlight
    # e.g. "Zappy #88" → highlight "#88"; Heroes → whole name gold
    if "#" in zappy_name:
        prefix, num = zappy_name.rsplit("#", 1)
        draw.text(
            (TEXT_X + bolt_w + name_w + mid_w, 20),
            prefix.strip() + " ",
            font=_FONT_MED,
            fill=MUTED,
        )
        prefix_w = int(draw.textlength(prefix.strip() + " ", font=_FONT_MED))
        draw.text(
            (TEXT_X + bolt_w + name_w + mid_w + prefix_w, 20),
            f"#{num}",
            font=_FONT_BOLD,
            fill=ACCENT,
        )
    else:
        draw.text(
            (TEXT_X + bolt_w + name_w + mid_w, 20),
            zappy_name,
            font=_FONT_BOLD,
            fill=ACCENT,
        )

    # ── Stat pills ───────────────────────────
    PILL_Y   = 46
    PILL_GAP = 8
    pill_x   = TEXT_X

    for stat_key in ("VLT", "INS", "SPK"):
        val    = str(stats.get(stat_key, "?"))
        pill_x = _draw_stat_pill(draw, pill_x, PILL_Y, stat_key, val) + PILL_GAP

    # ── Flatten & return ─────────────────────
    bg = Image.new("RGBA", canvas.size, BG)
    bg.paste(canvas, mask=canvas.split()[3])
    buf = io.BytesIO()
    bg.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return buf

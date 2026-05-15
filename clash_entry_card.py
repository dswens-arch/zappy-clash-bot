"""
clash_entry_card.py
-------------------
Renders a crisp Pillow image for each Zappy Clash bracket entry.
Renders at 2x then downscales for sharp output.
"""

import io
import os
import aiohttp
from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────
# Output dimensions
# ─────────────────────────────────────────────
OUT_W, OUT_H = 420, 76
SCALE        = 2
W            = OUT_W * SCALE
H            = OUT_H * SCALE

THUMB_OUT    = 52
THUMB_SIZE   = THUMB_OUT * SCALE
THUMB_PAD    = 16 * SCALE          # left padding for thumbnail
THUMB_Y      = (H - THUMB_SIZE) // 2
THUMB_R      = 8 * SCALE
TEXT_X       = THUMB_PAD + THUMB_SIZE + 16 * SCALE

# ─────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────
BG      = (28,  30,  42)
CARD_BG = (40,  42,  56)
ACCENT  = (240, 178,  50, 255)   # gold
WHITE   = (240, 245, 255, 255)
MUTED   = (140, 150, 175, 255)

# pill: (background, label colour)
STAT_COLORS = {
    "VLT": ((240, 178,  50, 255), (28, 30, 42, 255)),   # bg=gold, text=dark
    "INS": (( 87, 242, 135, 255), (28, 30, 42, 255)),   # bg=green, text=dark  
    "SPK": ((  0, 176, 244, 255), (28, 30, 42, 255)),   # bg=blue, text=dark
}

# ─────────────────────────────────────────────
# Fonts
# ─────────────────────────────────────────────
def _font(name, size):
    for path in [
        f"./fonts/{name}",
        f"/usr/share/fonts/truetype/google-fonts/{name}",
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

_FB = _font("Poppins-Bold.ttf",    14 * SCALE)   # bold — name, number
_FM = _font("Poppins-Medium.ttf",  13 * SCALE)   # medium — "enters with"
_FL = _font("Poppins-Bold.ttf",    10 * SCALE)   # pill label
_FV = _font("Poppins-Bold.ttf",    12 * SCALE)   # pill value

# ─────────────────────────────────────────────
# Gateway fetch with fallbacks
# ─────────────────────────────────────────────
_GATEWAYS = [
    "https://ipfs-pera.algonode.dev/ipfs/{cid}?optimizer=image&width=512&quality=90",
    "https://cloudflare-ipfs.com/ipfs/{cid}",
    "https://gateway.pinata.cloud/ipfs/{cid}",
]

async def _fetch_image(url: str) -> Image.Image | None:
    if not url:
        return None
    cid  = url.split("/ipfs/")[-1].split("?")[0].strip() if "/ipfs/" in url else None
    urls = [g.format(cid=cid) for g in _GATEWAYS] if cid else [url]
    for u in urls:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(u, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        return Image.open(io.BytesIO(await r.read())).convert("RGBA")
                    print(f"[entry_card] {r.status} {u}")
        except Exception as e:
            print(f"[entry_card] failed {u}: {e}")
    return None

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _rr(draw, xy, r, fill=None, outline=None, width=1):
    draw.rounded_rectangle(xy, radius=r, fill=fill, outline=outline, width=width)

def _draw_fallback(canvas, x, y, size):
    draw = ImageDraw.Draw(canvas)
    _rr(draw, [x, y, x+size, y+size], r=THUMB_R, fill=(48, 50, 66))
    _rr(draw, [x, y, x+size, y+size], r=THUMB_R, outline=(*ACCENT[:3], 140), width=3)
    cx, cy = x + size//2, y + size//2
    s = size * 0.26
    draw.polygon([
        (cx - s*0.15, cy - s*1.0),
        (cx + s*0.55, cy - s*0.05),
        (cx + s*0.1,  cy + s*0.05),
        (cx + s*0.15, cy + s*1.0),
        (cx - s*0.55, cy + s*0.05),
        (cx - s*0.1,  cy - s*0.05),
    ], fill=ACCENT)

def _draw_pill(draw, x, y, label, value):
    """Solid colored pill: [LABEL  VALUE] on colored background. Returns right edge x."""
    pill_bg, text_color = STAT_COLORS.get(label, ((100,100,100,255), (0,0,0,255)))

    lw  = draw.textlength(label, font=_FL)
    vw  = draw.textlength(value, font=_FV)

    PAD = 8  * SCALE
    GAP = 6  * SCALE
    ph  = 20 * SCALE
    pr  = 4  * SCALE
    pw  = int(PAD + lw + GAP + vw + PAD)
    my  = y + ph // 2

    # Solid colored background
    _rr(draw, [x, y, x+pw, y+ph], r=pr, fill=pill_bg)

    # Label — dark text on colored bg
    draw.text((x + PAD, my), label, font=_FL, fill=text_color, anchor="lm")

    # Value — dark text on colored bg
    draw.text((x + PAD + lw + GAP, my), value, font=_FV, fill=text_color, anchor="lm")

    return x + pw

# ─────────────────────────────────────────────
# Main renderer
# ─────────────────────────────────────────────
async def render_entry_card(
    display_name: str,
    zappy_name:   str,
    stats:        dict,
    image_url:    str = "",
) -> io.BytesIO:

    canvas = Image.new("RGBA", (W, H), BG)
    draw   = ImageDraw.Draw(canvas)

    # Card background + gold left bar
    I = 4 * SCALE
    _rr(draw, [I, I, W-I, H-I], r=10*SCALE, fill=CARD_BG)
    _rr(draw, [I, I, I+5*SCALE, H-I], r=3*SCALE, fill=ACCENT)

    # Thumbnail
    thumb_img = await _fetch_image(image_url)
    if thumb_img:
        thumb = thumb_img.resize((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        mask  = Image.new("L", (THUMB_SIZE, THUMB_SIZE), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, THUMB_SIZE-1, THUMB_SIZE-1], radius=THUMB_R, fill=255
        )
        canvas.paste(thumb, (THUMB_PAD, THUMB_Y), mask)
    else:
        _draw_fallback(canvas, THUMB_PAD, THUMB_Y, THUMB_SIZE)

    # ── Line 1: "displayname  enters with  Zappy #1474" ──
    L1 = int(H * 0.30)
    L2 = int(H * 0.67)

    x = TEXT_X

    # Username bold white
    nw = int(draw.textlength(display_name, font=_FB))
    draw.text((x, L1), display_name, font=_FB, fill=WHITE, anchor="lm")
    x += nw

    # "  enters with  " muted
    mid = "  enters with  "
    mw  = int(draw.textlength(mid, font=_FM))
    draw.text((x, L1), mid, font=_FM, fill=MUTED, anchor="lm")
    x += mw

    # "Zappy " gold, "#number" bold gold
    if "#" in zappy_name:
        prefix, num = zappy_name.rsplit("#", 1)
        prefix_str  = prefix.strip() + " " if prefix.strip() else "Zappy "
        pw = int(draw.textlength(prefix_str, font=_FB))
        draw.text((x, L1), prefix_str, font=_FB, fill=ACCENT, anchor="lm")
        x += pw
        draw.text((x, L1), f"#{num}", font=_FB, fill=ACCENT, anchor="lm")
    else:
        draw.text((x, L1), zappy_name, font=_FB, fill=ACCENT, anchor="lm")

    # ── Line 2: stat pills ──
    px = TEXT_X
    for key in ("VLT", "INS", "SPK"):
        px = _draw_pill(draw, px, L2 - 11*SCALE, key, str(stats.get(key, "?"))) + 8*SCALE

    # Downscale 2x → 1x
    out = canvas.resize((OUT_W, OUT_H), Image.LANCZOS)
    buf = io.BytesIO()
    out.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

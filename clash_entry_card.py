"""
clash_entry_card.py
-------------------
Renders a crisp Pillow image for each Zappy Clash bracket entry.
Renders at 2x then downscales for anti-aliased, sharp output.
"""

import io
import os
import aiohttp
from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────
# Output dimensions (final size Discord sees)
# ─────────────────────────────────────────────
OUT_W, OUT_H = 400, 72
SCALE        = 2
W            = OUT_W * SCALE
H            = OUT_H * SCALE

THUMB_OUT    = 52
THUMB_SIZE   = THUMB_OUT * SCALE
THUMB_PAD    = 10 * SCALE
THUMB_Y      = (H - THUMB_SIZE) // 2
THUMB_R      = 8 * SCALE
TEXT_X       = THUMB_PAD + THUMB_SIZE + 14 * SCALE

# ─────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────
BG       = (28,  30,  42)
CARD_BG  = (40,  42,  56)
ACCENT   = (240, 178,  50, 255)
WHITE    = (240, 245, 255, 255)
MUTED    = (140, 150, 175, 255)

STAT_COLORS = {
    "VLT": ((240, 178,  50, 50),  (240, 178,  50, 255)),
    "INS": (( 87, 242, 135, 50),  ( 87, 242, 135, 255)),
    "SPK": ((  0, 176, 244, 50),  (  0, 176, 244, 255)),
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

_FB = _font("Poppins-Bold.ttf",   14 * SCALE)
_FM = _font("Poppins-Medium.ttf", 13 * SCALE)
_FL = _font("Poppins-Bold.ttf",   11 * SCALE)

# ─────────────────────────────────────────────
# Gateway fallback fetch
# ─────────────────────────────────────────────
_GATEWAYS = [
    "https://ipfs-pera.algonode.dev/ipfs/{cid}?optimizer=image&width=512&quality=90",
    "https://cloudflare-ipfs.com/ipfs/{cid}",
    "https://gateway.pinata.cloud/ipfs/{cid}",
]

async def _fetch_image(url: str) -> Image.Image | None:
    if not url:
        return None
    cid = url.split("/ipfs/")[-1].split("?")[0].strip() if "/ipfs/" in url else None
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
    pill_bg, label_color = STAT_COLORS.get(label, ((100,100,100,50), WHITE))
    lw = draw.textlength(label, font=_FL)
    vw = draw.textlength(value, font=_FM)
    PAD, GAP, BAR = 8*SCALE, 6*SCALE, 4*SCALE
    pw = int(PAD + lw + GAP + vw + PAD)
    ph = int(20 * SCALE)
    pr = 4 * SCALE
    my = y + ph // 2
    _rr(draw, [x, y, x+pw, y+ph], r=pr, fill=pill_bg)
    _rr(draw, [x, y, x+BAR, y+ph], r=pr, fill=label_color)
    draw.text((x+PAD,          my), label, font=_FL, fill=label_color, anchor="lm")
    draw.text((x+PAD+lw+GAP,   my), value, font=_FM, fill=WHITE,       anchor="lm")
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

    # Card + accent bar
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

    # Text positions — vertically centred in upper/lower halves
    L1 = int(H * 0.32)
    L2 = int(H * 0.68)

    x = TEXT_X

    # ⚡
    bw = int(draw.textlength("⚡ ", font=_FB))
    draw.text((x, L1), "⚡ ", font=_FB, fill=ACCENT, anchor="lm")
    x += bw

    # Username
    nw = int(draw.textlength(display_name, font=_FB))
    draw.text((x, L1), display_name, font=_FB, fill=WHITE, anchor="lm")
    x += nw

    # "  enters with  "
    mid = "  enters with  "
    mw  = int(draw.textlength(mid, font=_FM))
    draw.text((x, L1), mid, font=_FM, fill=MUTED, anchor="lm")
    x += mw

    # Zappy name — #number in gold
    if "#" in zappy_name:
        prefix, num = zappy_name.rsplit("#", 1)
        ps = (prefix.strip() + " ") if prefix.strip() else ""
        if ps:
            pw = int(draw.textlength(ps, font=_FM))
            draw.text((x, L1), ps, font=_FM, fill=MUTED, anchor="lm")
            x += pw
        draw.text((x, L1), f"#{num}", font=_FB, fill=ACCENT, anchor="lm")
    else:
        draw.text((x, L1), zappy_name, font=_FB, fill=ACCENT, anchor="lm")

    # Stat pills on line 2
    px = TEXT_X
    for key in ("VLT", "INS", "SPK"):
        px = _draw_pill(draw, px, L2 - 10*SCALE, key, str(stats.get(key, "?"))) + 8*SCALE

    # Downscale 2x → 1x
    out = canvas.resize((OUT_W, OUT_H), Image.LANCZOS)
    buf = io.BytesIO()
    out.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

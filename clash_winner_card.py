"""
clash_winner_card.py
--------------------
Renders the bracket champion card by compositing the winner's
Zappy art onto the template, then adding name and record text
in the bottom panel.

Usage:
    buf = await render_winner_card(
        zappy_name="Zappy #2124",
        display_name="BeeeeeeeeeeRad",
        record={"wins": 62, "losses": 13, "champ_wins": 4},
        image_url="https://ipfs.io/ipfs/...",
    )
    await channel.send(file=discord.File(buf, filename="champion.png"))
"""

import io
import os
import aiohttp
from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────
# Template path
# ─────────────────────────────────────────────
TEMPLATE_PATH = "./clash_champion_template.png"

# ─────────────────────────────────────────────
# Zone coordinates (measured from card2.png)
# ─────────────────────────────────────────────
ART_X1, ART_Y1 = 721,  1292
ART_X2, ART_Y2 = 2858, 3429
ART_SIZE        = 2137

TXT_X1, TXT_Y1 = 379,  3709
TXT_X2, TXT_Y2 = 3217, 4447
TXT_CX          = (TXT_X1 + TXT_X2) // 2
TXT_H           = TXT_Y2 - TXT_Y1
TXT_W           = TXT_X2 - TXT_X1

# ─────────────────────────────────────────────
# Output size
# ─────────────────────────────────────────────
OUT_W, OUT_H = 896, 1200

# ─────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────
GOLD  = (255, 210,  50, 255)
CYAN  = ( 80, 220, 255, 255)
MUTED = (180, 190, 220, 255)

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

# ─────────────────────────────────────────────
# IPFS gateway fetch with fallbacks
# ─────────────────────────────────────────────
_GATEWAYS = [
    "https://ipfs-pera.algonode.dev/ipfs/{cid}?optimizer=image&width=1024&quality=90",
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
                async with s.get(u, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        return Image.open(io.BytesIO(await r.read())).convert("RGBA")
                    print(f"[winner_card] {r.status} {u}")
        except Exception as e:
            print(f"[winner_card] fetch failed {u}: {e}")
    return None

# ─────────────────────────────────────────────
# Text fitting helper
# ─────────────────────────────────────────────
def _fit(draw, text, max_size, min_size, max_width, bold=True):
    fname = "Poppins-Bold.ttf" if bold else "Poppins-Medium.ttf"
    size  = max_size
    while size >= min_size:
        f = _font(fname, size)
        if draw.textlength(text, font=f) <= max_width:
            return f
        size -= 8
    return _font(fname, min_size)

# ─────────────────────────────────────────────
# Main renderer
# ─────────────────────────────────────────────
async def render_winner_card(
    zappy_name:   str,
    display_name: str,
    record:       dict,
    image_url:    str = "",
) -> io.BytesIO:

    # Load template
    try:
        canvas = Image.open(TEMPLATE_PATH).convert("RGBA")
    except Exception as e:
        print(f"[winner_card] template load failed: {e}")
        canvas = Image.new("RGBA", (3584, 4800), (20, 15, 40, 255))

    # ── Paste Zappy art ──────────────────────
    zappy_img = await _fetch_image(image_url)
    if zappy_img:
        # Crop to square from center if needed
        zw, zh = zappy_img.size
        if zw != zh:
            sq = min(zw, zh)
            zappy_img = zappy_img.crop(((zw-sq)//2, (zh-sq)//2, (zw+sq)//2, (zh+sq)//2))
        zappy_sq = zappy_img.resize((ART_SIZE, ART_SIZE), Image.LANCZOS)
        if zappy_sq.mode == "RGBA":
            canvas.paste(zappy_sq, (ART_X1, ART_Y1), zappy_sq.split()[3])
        else:
            canvas.paste(zappy_sq.convert("RGBA"), (ART_X1, ART_Y1))

    # ── Text in bottom panel ─────────────────
    draw = ImageDraw.Draw(canvas)

    # Line 1: "Zappy #2124 wins!"
    line1 = f"{zappy_name} wins!"
    f1 = _fit(draw, line1, 220, 100, TXT_W)
    y1 = TXT_Y1 + int(TXT_H * 0.10)
    draw.text((TXT_CX, y1), line1, font=f1, fill=CYAN, anchor="mt")

    # Line 2: "(DisplayName)"
    line2 = f"({display_name})"
    f2 = _fit(draw, line2, 190, 90, TXT_W)
    y2 = y1 + int(TXT_H * 0.35)
    draw.text((TXT_CX, y2), line2, font=f2, fill=GOLD, anchor="mt")

    # Line 3: record
    wins   = record.get("wins", 0)
    losses = record.get("losses", 0)
    champs = record.get("champ_wins", 0)
    line3  = f"{wins}W  {losses}L"
    if champs > 0:
        line3 += f"   {champs}x Bracket Champion"
    f3 = _fit(draw, line3, 130, 70, TXT_W, bold=False)
    y3 = y2 + int(TXT_H * 0.34)
    draw.text((TXT_CX, y3), line3, font=f3, fill=MUTED, anchor="mt")

    # ── Downscale to output size ─────────────
    out = canvas.convert("RGB").resize((OUT_W, OUT_H), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

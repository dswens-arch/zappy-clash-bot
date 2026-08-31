"""
clash_flex_card.py
-------------------
Prepares the shareable "flex" image for a Zappy: just its own art,
cropped to a clean square. No overlay, no stats — the caption text
(clash_flex_blurb.py) carries what makes the Zappy special.

Usage:
    buf = await render_flex_card(image_url="https://raw.githubusercontent.com/.../2616130337.jpg")
    await channel.send(file=discord.File(buf, filename="flex.png"))
"""

import io
import aiohttp
from PIL import Image

ART_SIZE = 1024

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


async def render_flex_card(image_url: str = "", **_ignored) -> io.BytesIO | None:
    """
    Returns a cropped-square PNG of the Zappy's own art, or None if the
    image couldn't be fetched. **_ignored absorbs any leftover kwargs
    (stats/badge/trait_line/abilities) from callers so bot.py doesn't
    need to change its call site.
    """
    zappy_img = await _fetch_image(image_url)
    if not zappy_img:
        return None

    zw, zh = zappy_img.size
    if zw != zh:
        sq = min(zw, zh)
        zappy_img = zappy_img.crop(((zw - sq) // 2, (zh - sq) // 2, (zw + sq) // 2, (zh + sq) // 2))
    art = zappy_img.resize((ART_SIZE, ART_SIZE), Image.LANCZOS).convert("RGB")

    buf = io.BytesIO()
    art.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf

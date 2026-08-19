"""
download_zappy_images.py  (zappy-clash-bot repo edition)
----------------------------------------------------------
Self-hosts every Zappy/Hero/Collab image in THIS repo instead of depending
on any IPFS gateway (or AlgoNode's IPFS gateway, which also counts against
the same daily quota Spark Jobs/Office/Clash payouts use) at runtime.

Adapted from the voltball-site version of this script — see
CLASH_BOT_HANDOFF.md for the full history of why this exists. Differences
from the voltball-site version:
  - Single output tier only (1000px) — voltball-site needed a separate
    small tier for its own roster cards, but nothing in this repo needs
    that; card compositing (clash_entry_card.py, clash_winner_card.py,
    expedition_engine.py) all want the larger size.
  - Also processes Heroes and the ShittyKitties collab (keys "hero_*" /
    "collab_*" in zappy_image_sources.json), not just the main collection.
  - Rewrites TWO source files in place instead of one JSON file:
      * zappy_collection.py  — main collection's image_url fields
      * algorand_lookup.py   — HERO_IMAGES / COLLAB_IMAGES dicts
    Both are live Python files, not JSON, so the rewrite is a careful
    per-entry text substitution rather than a json.dump.

SOURCE OF TRUTH FOR CIDs: zappy_image_sources.json, NOT the files this
script rewrites. Same reasoning as the voltball-site version — once this
runs once, the original IPFS URLs are gone from zappy_collection.py, so
if we ever need to re-derive a source URL (bump resolution, retry a
failure, etc.) later, this file is the only place it still exists.

Designed to run in GitHub Actions (see .github/workflows/sync-zappy-images.yml),
NOT locally — needs `requests` and `pillow`, a full outbound internet
connection, and takes a few minutes for ~1690 images.

Safe to re-run later (e.g. after new Zappies mint, or to bump FULL_SIZE) —
only re-downloads what's missing, only commits if something changed.
"""

import json
import os
import re
import sys
import concurrent.futures
import requests
from PIL import Image
from io import BytesIO

SOURCES_PATH    = "zappy_image_sources.json"
COLLECTION_PATH = "zappy_collection.py"
LOOKUP_PATH     = "algorand_lookup.py"

OUTPUT_DIR = "zappy-images-full"
SIZE       = (1000, 1000)
QUALITY    = 90

# Where the final files will be publicly reachable once committed & pushed.
# Update the owner/repo/branch here if the repo ever moves.
RAW_BASE = "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full"

CONCURRENCY = 12       # parallel downloads; polite, not hammering any one gateway
REQUEST_TIMEOUT = 15   # seconds per gateway attempt

# Tried in order per image. dweb.link is the gateway Cloudflare itself
# named as the successor when it shut cloudflare-ipfs.com down.
GATEWAYS = [
    "https://dweb.link/ipfs/{cid}",
    "https://ipfs.io/ipfs/{cid}",
]


def extract_cid(image_url: str) -> str | None:
    """
    Pull the bare CID out of an IPFS URL, stripping any query string.
    NOTE: the voltball-site version of this function didn't strip query
    params — harmless there since none of its source URLs had any, but
    the Hero images here do (?optimizer=image&width=...), so this version
    strips everything after '?' to avoid mangling the CID.
    """
    if "/ipfs/" not in image_url:
        return None
    return image_url.split("/ipfs/", 1)[1].split("?")[0].strip()


def fetch_image_bytes(cid: str) -> bytes | None:
    for template in GATEWAYS:
        url = template.format(cid=cid)
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except requests.RequestException:
            continue
    return None


def already_done(key: str) -> bool:
    return os.path.exists(os.path.join(OUTPUT_DIR, f"{key}.jpg"))


def process_one(key: str, ipfs_url: str) -> tuple[str, bool]:
    """Downloads and saves one image. Returns (key, success)."""
    cid = extract_cid(ipfs_url)
    if not cid:
        return key, False

    raw = fetch_image_bytes(cid)
    if raw is None:
        return key, False

    try:
        img = Image.open(BytesIO(raw)).convert("RGB")
        img.thumbnail(SIZE, Image.LANCZOS)
        img.save(os.path.join(OUTPUT_DIR, f"{key}.jpg"), "JPEG", quality=QUALITY)
        return key, True
    except Exception as e:
        print(f"  [{key}] downloaded but failed to process: {e}")
        return key, False


# ─────────────────────────────────────────────
# Rewriting zappy_collection.py in place
# ─────────────────────────────────────────────

def rewrite_collection_image_urls(asset_ids_done: list[str]):
    """
    For each numeric asset_id that now has a local image, replaces that
    entry's 'image_url': '...' line with the new self-hosted URL.
    Operates on the raw text rather than re-serializing the dict, so
    formatting/comments/ordering elsewhere in the file are untouched.
    """
    if not asset_ids_done:
        return
    with open(COLLECTION_PATH, "r") as f:
        content = f.read()

    changed = 0
    for asset_id in asset_ids_done:
        new_url = f"{RAW_BASE}/{asset_id}.jpg"
        # Match this specific asset's block only, up to its image_url line,
        # so we never touch a different entry that happens to share text.
        pattern = re.compile(
            rf"({re.escape(asset_id)}:\s*\{{[^}}]*?'image_url':\s*)'[^']*'",
            re.S,
        )
        new_content, n = pattern.subn(rf"\1'{new_url}'", content, count=1)
        if n:
            content = new_content
            changed += 1

    with open(COLLECTION_PATH, "w") as f:
        f.write(content)
    print(f"  zappy_collection.py: rewrote {changed}/{len(asset_ids_done)} image_url fields")


def rewrite_hero_collab_urls(keys_done: list[str]):
    """
    For each 'hero_<Name>' / 'collab_<Name>' key that now has a local
    image, rewrites the matching entry in algorand_lookup.py's
    HERO_IMAGES / COLLAB_IMAGES dicts.
    """
    if not keys_done:
        return
    with open(LOOKUP_PATH, "r") as f:
        content = f.read()

    changed = 0
    for key in keys_done:
        if key.startswith("hero_"):
            name = key[len("hero_"):]
        elif key.startswith("collab_"):
            name = key[len("collab_"):]
        else:
            continue

        new_url = f"{RAW_BASE}/{key}.jpg"
        pattern = re.compile(rf'("{re.escape(name)}":\s*)"[^"]*"')
        new_content, n = pattern.subn(rf'\1"{new_url}"', content, count=1)
        if n:
            content = new_content
            changed += 1

    with open(LOOKUP_PATH, "w") as f:
        f.write(content)
    print(f"  algorand_lookup.py: rewrote {changed}/{len(keys_done)} Hero/Collab image URLs")


def main():
    with open(SOURCES_PATH) as f:
        sources = json.load(f)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    to_process = {k: v for k, v in sources.items() if not already_done(k)}
    already_done_count = len(sources) - len(to_process)

    if already_done_count:
        print(f"Skipping {already_done_count} already downloaded.")
    print(f"Processing {len(to_process)} remaining images with {CONCURRENCY} parallel workers...")

    succeeded, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(process_one, key, url): key for key, url in to_process.items()}
        done_count = 0
        for future in concurrent.futures.as_completed(futures):
            key, ok = future.result()
            (succeeded if ok else failed).append(key)
            done_count += 1
            if done_count % 100 == 0:
                print(f"  ...{done_count}/{len(to_process)} processed ({len(failed)} failures so far)")

    # Rewrite source files for EVERYTHING that has a local file now,
    # including from earlier runs (covers first-run-after-rewrite and
    # any partial-failure re-runs).
    all_done_keys = [k for k in sources if already_done(k)]
    numeric_keys  = [k for k in all_done_keys if k.isdigit()]
    hero_collab_keys = [k for k in all_done_keys if k.startswith("hero_") or k.startswith("collab_")]

    print(f"\nRewriting source files ({len(numeric_keys)} Zappies, {len(hero_collab_keys)} Hero/Collab)...")
    rewrite_collection_image_urls(numeric_keys)
    rewrite_hero_collab_urls(hero_collab_keys)

    print(f"\nDone. {len(succeeded)} succeeded, {len(failed)} failed this run ({already_done_count} already done before this run).")
    if failed:
        print("Failed keys (both gateways failed this run -- will retry automatically next run):")
        for key in failed:
            print(f"  {key}")

    if to_process and len(failed) > len(to_process) * 0.05:
        print(f"\nWARNING: {len(failed)} failures is more than 5% of this run -- check gateway health before trusting this run.")
        sys.exit(1)


if __name__ == "__main__":
    main()

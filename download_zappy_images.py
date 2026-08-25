"""
download_zappy_images.py  (zappy-clash-bot repo edition)
----------------------------------------------------------
Self-hosts every Zappy/Hero/Collab image in THIS repo instead of depending
on any IPFS gateway at runtime.

TWO sources of truth, handled differently:

  1. zappy_image_sources.json — the original ~1,690 mints. Static file,
     rewrites zappy_collection.py / algorand_lookup.py in place. Unchanged
     from before.

  2. The `extra_zappies` Supabase table — every Zappy added later via
     /addzappies or /settraits in the Discord bot. THIS IS NEW. Previously
     this script had no idea these existed, so new mints never got
     self-hosted unless someone manually added an entry to the JSON file.
     Now: pulls every row, downloads/self-hosts anything not already
     local, and PATCHes image_url back to Supabase directly — no manual
     file editing required for new mints, ever.

Needs SUPABASE_URL and SUPABASE_SERVICE_KEY as env vars (repo secrets in
the workflow) — must be the SERVICE ROLE key, not the anon key, since it
needs write access to PATCH image_url.

Designed to run in GitHub Actions (see .github/workflows/sync-zappy-images.yml).
Safe to re-run any time — only touches what's missing/outdated.
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

RAW_BASE = "https://raw.githubusercontent.com/dswens-arch/zappy-clash-bot/main/zappy-images-full"

SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

CONCURRENCY = 12
REQUEST_TIMEOUT = 15

# ipfs-pera.algonode.dev goes first — confirmed to reliably serve this
# collection's content without the bot-detection blocking seen on
# dweb.link, and without the reliability issues seen on plain ipfs.io.
GATEWAYS = [
    "https://ipfs-pera.algonode.dev/ipfs/{cid}",
    "https://ipfs.algonode.dev/ipfs/{cid}",
    "https://dweb.link/ipfs/{cid}",
    "https://ipfs.io/ipfs/{cid}",
]


def extract_cid(image_url: str) -> str | None:
    if "/ipfs/" not in image_url:
        return None
    return image_url.split("/ipfs/", 1)[1].split("?")[0].strip()


def fetch_image_bytes(cid: str) -> bytes | None:
    headers = {"User-Agent": "Mozilla/5.0 (zappy-clash-bot image sync)"}
    for template in GATEWAYS:
        url = template.format(cid=cid)
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except requests.RequestException:
            continue
    return None


def already_done(key: str) -> bool:
    return os.path.exists(os.path.join(OUTPUT_DIR, f"{key}.jpg"))


def process_one(key: str, ipfs_url: str) -> tuple[str, bool]:
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
# Rewriting zappy_collection.py / algorand_lookup.py (original ~1,690)
# ─────────────────────────────────────────────

def rewrite_collection_image_urls(asset_ids_done: list[str]):
    if not asset_ids_done:
        return
    with open(COLLECTION_PATH, "r") as f:
        content = f.read()

    changed = 0
    for asset_id in asset_ids_done:
        new_url = f"{RAW_BASE}/{asset_id}.jpg"
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


# ─────────────────────────────────────────────
# NEW: syncing extra_zappies (mints added via /addzappies or /settraits)
# ─────────────────────────────────────────────

def fetch_extra_zappies() -> list[dict]:
    """Pull every row from the extra_zappies Supabase table via REST."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY not set — skipping extra_zappies sync.")
        return []

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    url = f"{SUPABASE_URL}/rest/v1/extra_zappies?select=asset_id,image_url"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"Failed to fetch extra_zappies from Supabase: {e}")
        return []


def update_extra_zappy_image_url(asset_id: int, new_url: str) -> bool:
    """PATCH a single row's image_url once it's been self-hosted."""
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{SUPABASE_URL}/rest/v1/extra_zappies?asset_id=eq.{asset_id}"
    try:
        resp = requests.patch(url, headers=headers, json={"image_url": new_url}, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"  [{asset_id}] Supabase PATCH failed: {e}")
        return False


def sync_extra_zappies():
    """
    Downloads/self-hosts any extra_zappies row that isn't already pointed
    at RAW_BASE, then writes the new URL straight back to Supabase.
    Returns (succeeded_count, failed_count).
    """
    rows = fetch_extra_zappies()
    if not rows:
        return 0, 0

    to_process = {}
    for row in rows:
        asset_id  = str(row["asset_id"])
        image_url = row.get("image_url") or ""
        if image_url.startswith(RAW_BASE):
            continue  # already self-hosted
        if not image_url:
            continue  # nothing to fetch yet (traits added but no image)
        if already_done(asset_id):
            # File exists locally but Supabase wasn't updated yet (e.g. a
            # previous run got interrupted before the PATCH step) — just
            # fix the DB pointer, no need to re-download.
            update_extra_zappy_image_url(int(asset_id), f"{RAW_BASE}/{asset_id}.jpg")
            continue
        to_process[asset_id] = image_url

    if not to_process:
        print("extra_zappies: nothing new to self-host.")
        return 0, 0

    print(f"extra_zappies: self-hosting {len(to_process)} new mint(s)...")
    succeeded, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {pool.submit(process_one, aid, url): aid for aid, url in to_process.items()}
        for future in concurrent.futures.as_completed(futures):
            aid, ok = future.result()
            (succeeded if ok else failed).append(aid)

    for asset_id in succeeded:
        new_url = f"{RAW_BASE}/{asset_id}.jpg"
        if update_extra_zappy_image_url(int(asset_id), new_url):
            print(f"  [{asset_id}] self-hosted -> {new_url}")

    if failed:
        print(f"  extra_zappies: {len(failed)} failed (will retry next run): {failed}")

    return len(succeeded), len(failed)


def main():
    with open(SOURCES_PATH) as f:
        sources = json.load(f)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Part 1: original ~1,690-mint static collection ──
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

    all_done_keys = [k for k in sources if already_done(k)]
    numeric_keys  = [k for k in all_done_keys if k.isdigit()]
    hero_collab_keys = [k for k in all_done_keys if k.startswith("hero_") or k.startswith("collab_")]

    print(f"\nRewriting source files ({len(numeric_keys)} Zappies, {len(hero_collab_keys)} Hero/Collab)...")
    rewrite_collection_image_urls(numeric_keys)
    rewrite_hero_collab_urls(hero_collab_keys)

    print(f"\nStatic collection done. {len(succeeded)} succeeded, {len(failed)} failed this run "
          f"({already_done_count} already done before this run).")
    if failed:
        print("Failed keys (will retry automatically next run):")
        for key in failed:
            print(f"  {key}")

    # ── Part 2: new mints from extra_zappies (Supabase) ──
    print("\n--- Syncing extra_zappies (new mints) ---")
    extra_succeeded, extra_failed = sync_extra_zappies()

    total_failed = len(failed) + extra_failed
    total_attempted = len(to_process) + extra_succeeded + extra_failed
    if total_attempted and total_failed > total_attempted * 0.05:
        print(f"\nWARNING: {total_failed} failures is more than 5% of this run -- check gateway health before trusting this run.")
        sys.exit(1)


if __name__ == "__main__":
    main()

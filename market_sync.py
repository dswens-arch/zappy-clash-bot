"""
market_sync.py
----------------
Periodic sync of live Downbad marketplace listings into Supabase, so the
site's Free Agents page can read cached data instead of calling Downbad
directly. Downbad now requires the API key on every request and blocks
unauthenticated traffic -- per their own integration rules, the key can
never reach a browser, so this bot-side sync (Railway env var) plus a
plain Supabase read on the static site is the only architecture that
keeps the key server-side while still letting Free Agents show
near-live prices.

Deliberately NOT an Edge Function proxy: with ~85 active listings and a
fantasy-sport use case, this data doesn't need to be fresher than a
periodic sync -- polling on a slow interval and caching is exactly what
Downbad's own docs ask integrations to do ("poll on the slowest interval
your product can live with"). This way the site never makes a live call
to Downbad at all, not even through a proxy, and there's no per-pageview
traffic hitting their API.

Setup required:
  1. Add DOWNBAD_API_KEY as a Railway environment variable (the key from
     Stein's doc -- never commit it, never put it in a public env var).
  2. Run the migration below against Supabase before this loads.
  3. Load this cog from bot.py -- see the bottom of this file for both
     common wiring patterns; adjust to whichever bot.py actually uses,
     since that file wasn't available when this was written.

Migration (run once):
    CREATE TABLE IF NOT EXISTS voltball_market_listings (
      asset_id bigint PRIMARY KEY,
      price integer NOT NULL,
      unit text NOT NULL DEFAULT 'ALGO',
      seller text,
      app_id bigint,
      updated_at timestamptz NOT NULL DEFAULT now()
    );
    ALTER TABLE voltball_market_listings ENABLE ROW LEVEL SECURITY;
    CREATE POLICY "Public read access" ON voltball_market_listings
      FOR SELECT USING (true);
"""

import os
import aiohttp
from discord.ext import commands, tasks

from database import get_supabase

DOWNBAD_API_KEY = os.environ["DOWNBAD_API_KEY"]
DOWNBAD_LISTINGS_URL = "https://market-api.downbad.farm/v1/collection/zappies-reborn/listings"

# Listings don't need to be fresher than this for a fantasy-sport site --
# see the module docstring on "poll on the slowest interval your product
# can live with." Adjust if that turns out to feel too slow/fast in
# practice; nothing else depends on this specific number.
SYNC_INTERVAL_MINUTES = 60


class MarketSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sync_market_listings.start()

    def cog_unload(self):
        self.sync_market_listings.cancel()

    @tasks.loop(minutes=SYNC_INTERVAL_MINUTES)
    async def sync_market_listings(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    DOWNBAD_LISTINGS_URL,
                    headers={"X-API-Key": DOWNBAD_API_KEY},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        # Back off on errors rather than retrying immediately
                        # -- a 4xx won't fix itself by hammering it, a 5xx
                        # just amplifies whatever's already wrong on their
                        # end. The next scheduled tick tries again naturally.
                        print(f"[market_sync] Downbad listings returned {resp.status}, skipping this sync")
                        return
                    data = await resp.json()
        except Exception as e:
            print(f"[market_sync] Failed to fetch Downbad listings: {e}")
            return

        # The authenticated /v1/ endpoint returns a bare list, unlike the
        # old unauthenticated /public/v1/ one which wrapped it in
        # {"listings": [...]}. Handle both shapes defensively rather than
        # assuming this stays a bare list forever.
        listings = data if isinstance(data, list) else data.get("listings", [])
        rows = [
            {
                "asset_id": l["asset_id"],
                "price": l["price"],
                "unit": l.get("unit", "ALGO"),
                "seller": l.get("seller"),
                "app_id": l.get("app_id"),
            }
            for l in listings
            if "asset_id" in l and "price" in l
        ]

        db = get_supabase()
        try:
            # Full replace each sync -- simplest correct way to make sure a
            # delisted/sold asset actually disappears from the cache, not
            # just goes stale and keeps showing a dead "Buy" link. ~85 rows,
            # cheap either way.
            db.table("voltball_market_listings").delete().neq("asset_id", -1).execute()
            if rows:
                db.table("voltball_market_listings").insert(rows).execute()
            print(f"[market_sync] Synced {len(rows)} active listings")
        except Exception as e:
            print(f"[market_sync] Failed to write listings to Supabase: {e}")

    @sync_market_listings.before_loop
    async def before_sync_market_listings(self):
        await self.bot.wait_until_ready()


# ── Wiring into bot.py ──
# Confirmed against the real file: cogs are added directly in on_ready(),
# no extension loader. Add the import near the other cog imports:
#     from market_sync import MarketSyncCog
# and add this line in on_ready(), right after VoltballCog is added:
#     await bot.add_cog(MarketSyncCog(bot))
async def setup(bot: commands.Bot):
    await bot.add_cog(MarketSyncCog(bot))

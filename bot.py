# -*- coding: utf-8 -*-
"""
bot.py
------
Zappy Clash Discord bot - main entry point.

Commands:
  /link     - Connect your Algorand wallet
  /clash    - Register for the current bracket
  /stats    - View your Zappy's stats
  /rank     - View your CP and rank
  /top      - Leaderboard
  /streak   - View your daily streak

Scheduled sessions:
  2:00 PM UTC - Morning Bracket
  12:00 AM UTC - Night Bracket

Setup:
  1. Copy .env.example to .env and fill in your keys
  2. Run: pip install -r requirements.txt
  3. Run: python bot.py
"""

import os
import random
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timezone, time as dtime
from dotenv import load_dotenv
from clash_auction import setup_auction_commands, auction_checker
import grand_prix_cog
from grand_prix_cog import GrandPrixCog

# Our modules
from algorand_lookup import link_wallet as verify_wallet, fetch_zappy_traits
from battle_engine   import build_fighter, resolve_battle
from token_rewards   import award_win_tokens, award_streak_tokens
from expedition_engine import (
    start_run, get_run, end_run, advance_beat, check_nft_drop,
    build_scene_embed, build_outcome_embed, build_run_complete_embed,
    ExpeditionView, ZoneSelectView, ZappySelectView, get_collection_bonus,
)
from expedition_events import ZONES, get_eligible_zones, get_highest_zone
from nft_rewards       import award_nft_prize, claim_nft_prize
from buddy_rewards     import check_buddy_drop, award_buddy, claim_buddy
from clash_chaos_modifiers import apply_all_modifiers, freaky_friday_reveal
from database        import (
    link_wallet as db_link_wallet,
    get_wallet,
    register_for_bracket,
    get_bracket_entries,
    is_registered,
    close_registration,
    save_battle_result,
    award_cp, get_leaderboard, get_player_rank,
    update_streak, get_streak,
    seed_bracket,
    CP_WIN, CP_LOSS, CP_UPSET_BONUS, CP_BRACKET_WIN,
)

# ---------------------------------------------
# Config
# ---------------------------------------------
load_dotenv()
BOT_TOKEN       = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID        = int(os.environ["DISCORD_GUILD_ID"])
CLASH_CHANNEL      = int(os.environ["CLASH_CHANNEL_ID"])    # #zappy-clash channel ID
ANNOUNCE_CHANNEL   = int(os.environ.get("ANNOUNCE_CHANNEL_ID", CLASH_CHANNEL))
EXPEDITION_CHANNEL = int(os.environ["EXPEDITION_CHANNEL_ID"]) if os.environ.get("EXPEDITION_CHANNEL_ID") else None   # Optional - if not set, works anywhere

# Session timing (UTC)
MORNING_OPEN    = dtime(14,  0, tzinfo=timezone.utc)
MORNING_CLOSE   = dtime(14, 30, tzinfo=timezone.utc)   # 30 min registration window
MORNING_RESOLVE = dtime(14, 35, tzinfo=timezone.utc)

EVENING_OPEN    = dtime(0,   0, tzinfo=timezone.utc)
EVENING_CLOSE   = dtime(0,  30, tzinfo=timezone.utc)
EVENING_RESOLVE = dtime(0,  35, tzinfo=timezone.utc)

# ---------------------------------------------
# Bot setup
# ---------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─────────────────────────────────────────────
# Discord Role IDs
# ─────────────────────────────────────────────
ROLE_ON_A_ROLL      = 1487259866031456356   # 3 day streak
ROLE_CHARGED_UP     = 1487260106721857698   # 7 day streak
ROLE_VETERAN_ZAPPY  = 1487260202368499853   # 14 day streak
ROLE_HALL_OF_FAME   = 1487260318852710551   # 30 day streak
ROLE_CLASH_CHAMPION = 1487260496951509114   # current bracket winner
ROLE_CP_1000        = 1487260807371690114   # 1,000 CP milestone
ROLE_CP_5000        = 1487260907615551729   # 5,000 CP milestone
ROLE_CP_10000       = 1487261099685445702   # 10,000 CP milestone
ROLE_BUDDY_FINDER   = 1487261184565448886   # found a Zappy buddy

# Streak roles in order — only highest is kept
STREAK_ROLES = [
    (3,  ROLE_ON_A_ROLL),
    (7,  ROLE_CHARGED_UP),
    (14, ROLE_VETERAN_ZAPPY),
    (30, ROLE_HALL_OF_FAME),
]

# CP milestone roles in order
CP_ROLES = [
    (1000,  ROLE_CP_1000),
    (5000,  ROLE_CP_5000),
    (10000, ROLE_CP_10000),
]

# Expedition entry fees by zone (Zone 1 is free)
EXPEDITION_FEES = {
    1: 0,
    2: 100,
    3: 250,
    4: 500,
    5: 1000,
}



def check_clash_channel(interaction: discord.Interaction) -> bool:
    """Returns True if the interaction is in the clash channel."""
    return interaction.channel_id == CLASH_CHANNEL


def check_expedition_channel(interaction: discord.Interaction) -> bool:
    """Returns True if expedition channel is set and matches, or if no channel is set (works anywhere)."""
    if EXPEDITION_CHANNEL is None:
        return True
    return interaction.channel_id == EXPEDITION_CHANNEL

# Track active bracket state
active_bracket_id: str | None = None
registration_open: bool = False


# ---------------------------------------------
# Helper: get current bracket ID
# ---------------------------------------------
def get_bracket_id(session: str) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    return f"{session}_{today}"


# ---------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------

@tree.command(name="link", description="Connect your Algorand wallet to play Zappy Clash")
@app_commands.describe(wallet="Your Algorand wallet address (starts with A-Z)")
async def cmd_link(interaction: discord.Interaction, wallet: str):
    """Link a wallet and verify Zappy ownership."""
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)

    # Basic address validation
    if len(wallet) != 58 or not wallet.isupper():
        await interaction.followup.send(
            "❌ That doesn't look like a valid Algorand address. "
            "It should be 58 uppercase characters.",
            ephemeral=True
        )
        return

    # Verify wallet on-chain
    await interaction.followup.send("🔍 Checking your wallet on Algorand...", ephemeral=True)
    result = await verify_wallet(user_id, wallet)

    if result.get("error"):
        await interaction.followup.send(
            f"❌ Couldn't reach the Algorand network: {result['error']}\nTry again in a moment.",
            ephemeral=True
        )
        return

    if not result["owns"]:
        await interaction.followup.send(
            "❌ No Zappies found in that wallet. Make sure you're using the wallet "
            "that holds your Zappy ASA.",
            ephemeral=True
        )
        return

    # Save to database
    db_link_wallet(user_id, wallet)

    # Build response
    zappies   = result["zappies"]
    heroes    = result["heroes"]
    collabs   = result["collabs"]

    lines = [f"✅ Wallet linked! Found **{len(zappies)} Zappy/Zappies**"]
    if heroes:
        lines.append(f"🦸 **{len(heroes)} Hero(es):** {', '.join(h['hero_type'] for h in heroes)}")
    if collabs:
        lines.append(f"🐱 **Collab token detected:** ShittyKitties crossover!")

    lines.append("")
    lines.append("Use `/clash` when registration opens to enter the next bracket.")
    lines.append("Use `/stats` to preview your Zappy's battle stats.")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@tree.command(name="clash", description="Enter your Zappy into the current bracket")
async def cmd_clash(interaction: discord.Interaction):
    """Register for the active bracket — shows top 5 Zappies as buttons."""
    if not check_clash_channel(interaction):
        await interaction.response.send_message(
            f"❌ Use <#{CLASH_CHANNEL}> for Clash commands.", ephemeral=True
        )
        return
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return  # Interaction expired — user can try again

    user_id = str(interaction.user.id)

    # Check registration is open
    if not registration_open or not active_bracket_id:
        await interaction.followup.send(
            "⏳ Registration isn't open right now. Watch for the announcement in "
            f"<#{CLASH_CHANNEL}> when the next bracket opens!",
            ephemeral=True
        )
        return

    # Check wallet is linked
    wallet = await asyncio.to_thread(get_wallet, user_id)
    if not wallet:
        await interaction.followup.send(
            "❌ You haven't linked your wallet yet. Use `/link` first!",
            ephemeral=True
        )
        return

    # Check if already registered
    if await asyncio.to_thread(is_registered, user_id, active_bracket_id):
        await interaction.followup.send(
            "✅ You're already registered for this bracket! Check "
            f"<#{CLASH_CHANNEL}> when fights start.",
            ephemeral=True
        )
        return

    # Verify ownership
    from algorand_lookup import _wallet_cache, _wallet_cache_ts, WALLET_CACHE_TTL, HERO_ASSET_IDS, COLLAB_ASSET_IDS
    from stats_engine import calculate_stats, get_hero_stats, get_collab_stats
    from zappy_collection import ZAPPY_COLLECTION
    import time as _t
    _now = _t.monotonic()
    if wallet in _wallet_cache and _now - _wallet_cache_ts.get(wallet, 0) < WALLET_CACHE_TTL:
        ownership = _wallet_cache[wallet]
    else:
        await interaction.followup.send("⚡ Verifying your Zappies...", ephemeral=True)
        ownership = await verify_wallet(user_id, wallet)

    if not ownership["owns"]:
        await interaction.followup.send(
            "❌ No Zappies found in your linked wallet. "
            "Use `/link` to update your wallet address.",
            ephemeral=True
        )
        return

    all_assets = (
        ownership["zappies"] +
        [{"asset_id": h["asset_id"], "unit_name": h["hero_type"]} for h in ownership["heroes"]] +
        [{"asset_id": c["asset_id"], "unit_name": "ShittyKitties"} for c in ownership["collabs"]]
    )

    # Score each Zappy by total stats (VLT+INS+SPK) to rank them
    scored = []
    for z in all_assets:
        asset_id = z["asset_id"]

        # Skip if on champion cooldown
        cooldown = await asyncio.to_thread(check_champion_cooldown, asset_id)
        if cooldown["on_cooldown"]:
            continue

        if asset_id in HERO_ASSET_IDS:
            hero_type = HERO_ASSET_IDS[asset_id]
            data = get_hero_stats(hero_type)
            if data:
                scored.append({
                    "asset_id":  asset_id,
                    "name":      f"Hero — {hero_type}",
                    "stats":     data,
                    "score":     data["VLT"] + data["INS"] + data["SPK"],
                    "image_url": "",
                })
        elif asset_id in COLLAB_ASSET_IDS:
            collab_type = COLLAB_ASSET_IDS[asset_id]
            data = get_collab_stats(collab_type)
            if data:
                scored.append({
                    "asset_id":  asset_id,
                    "name":      collab_type,
                    "stats":     data,
                    "score":     data["VLT"] + data["INS"] + data["SPK"],
                    "image_url": "",
                })
        else:
            entry = ZAPPY_COLLECTION.get(asset_id)
            if entry:
                traits = {
                    "background": entry.get("background",""),
                    "body":       entry.get("body",""),
                    "earring":    entry.get("earring","None"),
                    "eyes":       entry.get("eyes",""),
                    "eyewear":    entry.get("eyewear","None"),
                    "head":       entry.get("head",""),
                    "mouth":      entry.get("mouth",""),
                    "skin":       entry.get("skin",""),
                }
                stats = calculate_stats(traits)
                scored.append({
                    "asset_id":  asset_id,
                    "name":      entry.get("name", f"Zappy #{asset_id}"),
                    "stats":     stats,
                    "score":     stats["VLT"] + stats["INS"] + stats["SPK"],
                    "image_url": entry.get("image_url",""),
                })

    if not scored:
        await interaction.followup.send(
            "❌ No eligible Zappies found (they may all be on cooldown or not in collection).",
            ephemeral=True
        )
        return

    # Sort by score, take top 5
    top5 = sorted(scored, key=lambda x: x["score"], reverse=True)[:5]

    # Build selection embed
    embed = discord.Embed(
        title="⚡ Choose your Zappy for the Clash!",
        description="Your top Zappies ranked by combined stats. Pick one to enter the bracket.",
        color=0xF5E642,
    )
    for i, z in enumerate(top5, 1):
        s = z["stats"]
        embed.add_field(
            name=f"{i}. {z['name']}",
            value=f"⚡ VLT {s.get('VLT','?')} · 🛡️ INS {s.get('INS','?')} · 🎲 SPK {s.get('SPK','?')}",
            inline=False,
        )

    # Build button view
    class ClashPickView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.chosen = False
            for z in top5:
                s = z["stats"]
                btn = discord.ui.Button(
                    label=z["name"][:60],
                    style=discord.ButtonStyle.primary,
                    custom_id=f"clash_pick_{z['asset_id']}",
                )
                btn.callback = self._make_callback(z)
                self.add_item(btn)

        def _make_callback(self, zappy: dict):
            async def callback(inter: discord.Interaction):
                if self.chosen:
                    await inter.response.send_message("Already picked!", ephemeral=True)
                    return
                try:
                    await inter.response.defer(ephemeral=True)
                except discord.errors.NotFound:
                    return  # Interaction expired — user can try again
                self.chosen = True
                for item in self.children:
                    item.disabled = True

                asset_id = zappy["asset_id"]
                stats    = zappy["stats"]
                name     = zappy["name"]

                # Register
                await asyncio.to_thread(register_for_bracket, user_id, asset_id, active_bracket_id)

                # Confirmation embed
                confirm = discord.Embed(
                    title=f"✅ {name} is in the bracket!",
                    description=f"⚡ VLT {stats.get('VLT','?')} · 🛡️ INS {stats.get('INS','?')} · 🎲 SPK {stats.get('SPK','?')}",
                    color=0xF5E642,
                )
                if stats.get("combo"):
                    confirm.add_field(name="Combo", value=stats["combo"], inline=False)
                if stats.get("ability") and isinstance(stats["ability"], dict):
                    ab = stats["ability"]
                    confirm.add_field(name=f"⚡ {ab.get('name','Ability')}", value=ab.get("desc",""), inline=False)
                if zappy.get("image_url"):
                    confirm.set_thumbnail(url=zappy["image_url"])
                confirm.set_footer(text=f"Fights start when registration closes · Watch #{CLASH_CHANNEL}")
                await inter.followup.send(embed=confirm, ephemeral=True)

                # Public announcement
                clash_ch = bot.get_channel(CLASH_CHANNEL)
                if clash_ch:
                    await clash_ch.send(
                        f"⚡ **{inter.user.display_name}** enters the bracket with "
                        f"**{name}** — VLT {stats.get('VLT')} · INS {stats.get('INS')} · SPK {stats.get('SPK')}"
                    )
            return callback

    await interaction.followup.send(embed=embed, view=ClashPickView(), ephemeral=True)


@tree.command(name="stats", description="Preview your Zappy's battle stats")
@app_commands.describe(asset_id="Your Zappy's ASA ID (optional if you only have one)")
async def cmd_stats(interaction: discord.Interaction, asset_id: int | None = None):
    """Show a Zappy's stats without registering."""
    if not check_clash_channel(interaction):
        await interaction.response.send_message(
            f"❌ Use <#{CLASH_CHANNEL}> for Clash commands.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)
    wallet  = get_wallet(user_id)

    if not wallet:
        await interaction.followup.send("❌ Link your wallet first with `/link`.", ephemeral=True)
        return

    # Use local collection table - no indexer call needed
    from zappy_collection import ZAPPY_COLLECTION, ZAPPY_ASSET_IDS
    from algorand_lookup import HERO_ASSET_IDS

    if asset_id:
        chosen_id = asset_id
    else:
        # Find all Zappies in local table that might belong to this wallet
        # We can't know without indexer, so prompt for asset_id if ambiguous
        await interaction.followup.send(
            "Use `/stats asset_id:XXXXX` with your Zappy's ASA ID, "
            "or use `/myzappies` to see all your Zappies and their IDs.",
            ephemeral=True
        )
        return

    zappy = await fetch_zappy_traits(chosen_id)
    if not zappy:
        await interaction.followup.send("❌ Couldn't load traits. Try again.", ephemeral=True)
        return

    stats = zappy.get("stats", {})
    traits = zappy.get("traits", {})
    name   = zappy.get("name", f"ASA {chosen_id}")

    embed = discord.Embed(title=name, color=0xF5E642)

    embed.add_field(
        name="Battle Stats",
        value=f"⚡ **VLT** {stats.get('VLT','?')} - Attack\n"
              f"🛡️ **INS** {stats.get('INS','?')} - Defense\n"
              f"🎲 **SPK** {stats.get('SPK','?')} - Crit chance",
        inline=True,
    )
    embed.add_field(
        name="Traits",
        value=f"🎨 {traits.get('background','?')} bg\n"
              f"👕 {traits.get('body','?')}\n"
              f"💍 {traits.get('earring','None')}\n"
              f"👁️ {traits.get('eyes','?')}\n"
              f"🕶️ {traits.get('eyewear','None')}\n"
              f"🎩 {traits.get('head','?')}\n"
              f"👄 {traits.get('mouth','?')}\n"
              f"🎨 {traits.get('skin','?')} skin",
        inline=True,
    )
    if stats.get("combo"):
        embed.add_field(name="Combo", value=stats["combo"], inline=False)
    if stats.get("ability") and isinstance(stats["ability"], dict):
        ab = stats["ability"]
        embed.add_field(name=f"⚡ Ability: {ab.get('name', 'Ability')}", value=ab.get("desc", ""), inline=False)

    image_url = zappy.get("image_url", "")
    if image_url:
        embed.set_thumbnail(url=image_url)

    embed.set_footer(text=f"ASA {chosen_id}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="rank", description="Check your Clash Points and rank")
async def cmd_rank(interaction: discord.Interaction):
    """Show a player's rank and CP."""
    if not check_clash_channel(interaction):
        await interaction.response.send_message(
            f"❌ Use <#{CLASH_CHANNEL}> for Clash commands.", ephemeral=True
        )
        return
    user_id = str(interaction.user.id)
    rank_data = get_player_rank(user_id)
    streak_data = get_streak(user_id)

    lines = [
        f"**{interaction.user.display_name}**",
        f"",
        f"🏆 Rank: **#{rank_data.get('rank', '?')}**",
        f"⚡ Clash Points: **{rank_data.get('cp_total', 0):,} CP**",
        f"",
        f"🔥 Daily streak: **{streak_data.get('current_streak', 0)} days**",
        f"⚔️ Total wins: **{streak_data.get('total_wins', 0)}**",
        f"🎮 Total played: **{streak_data.get('total_played', 0)}**",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="top", description="View the Zappy Clash leaderboard")
async def cmd_top(interaction: discord.Interaction):
    """Show top 10 players by CP."""
    if not check_clash_channel(interaction):
        await interaction.response.send_message(
            f"❌ Use <#{CLASH_CHANNEL}> for Clash commands.", ephemeral=True
        )
        return

    top = get_leaderboard(10)

    if not top:
        await interaction.response.send_message("No players on the board yet - be the first!", ephemeral=False)
        return

    lines = ["**⚡ Zappy Clash Leaderboard**", ""]
    medals = ["🥇", "🥈", "🥉"] + ["  " for _ in range(10)]

    for i, player in enumerate(top):
        user_id = player["discord_user_id"]
        cp = player["cp_total"]
        try:
            member = interaction.guild.get_member(int(user_id))
            name = member.display_name if member else f"Player {user_id[:6]}"
        except Exception:
            name = f"Player {user_id[:6]}"
        lines.append(f"{medals[i]} **{name}** - {cp:,} CP")

    await interaction.response.send_message("\n".join(lines), ephemeral=False)


@tree.command(name="myzappies", description="List all your Zappies with names and ASA IDs")
async def cmd_myzappies(interaction: discord.Interaction):
    """Show all Zappies in the linked wallet with names."""
    if not check_clash_channel(interaction):
        await interaction.response.send_message(
            f"❌ Use <#{CLASH_CHANNEL}> for Clash commands.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)
    wallet  = get_wallet(user_id)

    if not wallet:
        await interaction.followup.send("❌ Link your wallet first with `/link`.", ephemeral=True)
        return

    from algorand_lookup import _wallet_cache, _wallet_cache_ts, WALLET_CACHE_TTL
    import time as _t
    _now = _t.monotonic()
    if wallet in _wallet_cache and _now - _wallet_cache_ts.get(wallet, 0) < WALLET_CACHE_TTL:
        ownership = _wallet_cache[wallet]
    else:
        ownership = await verify_wallet(user_id, wallet)

    if not ownership["owns"]:
        await interaction.followup.send("❌ No Zappies found in your linked wallet.", ephemeral=True)
        return

    zappies = ownership["zappies"]
    heroes  = ownership["heroes"]
    collabs = ownership["collabs"]

    embed = discord.Embed(
        title=f"⚡ Your Zappies ({len(zappies) + len(heroes) + len(collabs)} total)",
        color=0xF5E642,
    )

    # Main collection - paginate into chunks of 20 per field (Discord limit)
    if zappies:
        chunk_size = 20
        for i in range(0, len(zappies), chunk_size):
            chunk = zappies[i:i+chunk_size]
            lines = [
                f"**{z.get('name', z.get('unit_name', f'ASA {z["asset_id"]}'))}** `{z['asset_id']}`"
                for z in chunk
            ]
            field_name = "Zappies" if i == 0 else f"Zappies (cont.)"
            embed.add_field(name=field_name, value="\n".join(lines), inline=False)

    if heroes:
        hero_lines = [f"🦸 **Zappy Hero - {h['hero_type']}** `{h['asset_id']}`" for h in heroes]
        embed.add_field(name="Heroes", value="\n".join(hero_lines), inline=False)

    if collabs:
        collab_lines = [f"🐱 **Shitty Zappy Kitty** `{c['asset_id']}`" for c in collabs]
        embed.add_field(name="Collabs", value="\n".join(collab_lines), inline=False)

    embed.set_footer(text="Use /stats asset_id:XXXXX to see a Zappy's battle stats")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="debug", description="ADMIN ONLY - debug a Zappy's image URL")
@app_commands.describe(asset_id="The Zappy ASA ID to debug")
async def cmd_debug(interaction: discord.Interaction, asset_id: int):
    """Show raw image URL for debugging. Owner only."""
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    zappy = await fetch_zappy_traits(asset_id)

    if not zappy:
        await interaction.followup.send("❌ Could not fetch Zappy.", ephemeral=True)
        return

    image_url = zappy.get("image_url", "NONE")
    traits    = zappy.get("traits", {})

    lines = [
        f"**Name:** {zappy.get('name')}",
        f"**Image URL:** `{image_url}`",
        f"**Raw image from metadata:** `{traits.get('image_url', 'not in traits')}`",
        f"**Traits loaded:** {bool(traits)}",
    ]

    # Also try posting the image directly
    embed = discord.Embed(title="Image test", color=0xF5E642)
    if image_url and image_url != "NONE":
        embed.set_image(url=image_url)
        lines.append("*(image embed attempted below)*")

    await interaction.followup.send("\n".join(lines), embed=embed, ephemeral=True)


@tree.command(name="testbracket", description="ADMIN ONLY - trigger a test bracket right now")
async def cmd_testbracket(interaction: discord.Interaction):
    """Manually trigger a bracket for testing. Only works for the server owner."""
    global active_bracket_id, registration_open

    # Only server owner can run this
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    await interaction.response.send_message("⚡ Starting test bracket - registration open for 2 minutes!", ephemeral=True)

    bracket_id = f"test_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    active_bracket_id = bracket_id
    registration_open = True

    channel = bot.get_channel(CLASH_CHANNEL)

    await channel.send(
        "⚡ **ZAPPY CLASH - TEST BRACKET**\n"
        "\n"
        "Registration is open for **2 minutes**.\n"
        "Use `/clash` to enter your Zappy!"
    )

    # Wait 2 minutes
    await asyncio.sleep(120)

    # Close and resolve
    await close_and_resolve(channel)


@tree.command(name="streak", description="Check your daily play streak")
async def cmd_streak(interaction: discord.Interaction):
    """Show streak details and milestones."""
    if not check_clash_channel(interaction):
        await interaction.response.send_message(
            f"❌ Use <#{CLASH_CHANNEL}> for Clash commands.", ephemeral=True
        )
        return
    user_id = str(interaction.user.id)
    streak_data = get_streak(user_id)
    current = streak_data.get("current_streak", 0)

    lines = [
        f"**🔥 Daily Streak: {current} days**",
        f"",
        f"Longest ever: {streak_data.get('longest_streak', 0)} days",
        f"",
        "**Milestone rewards:**",
        f"  3 days  → +50 CP + \"On a Roll 🔥\" role {'✅' if current >= 3 else ''}",
        f"  7 days  → +200 CP + \"Charged Up ⚡\" role {'✅' if current >= 7 else ''}",
        f"  14 days → \"Veteran Zappy 🏆\" role + early drop access {'✅' if current >= 14 else ''}",
        f"  30 days → Hall of Fame ⭐ nameplate {'✅' if current >= 30 else ''}",
        f"",
        f"Play both sessions daily to keep your streak alive!",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------
# SCHEDULED SESSIONS
# ---------------------------------------------



# ---------------------------------------------
# Expedition DB helpers
# ---------------------------------------------

def expedition_already_ran_today(discord_user_id: str) -> bool:
    """Check if a user has already completed an expedition today."""
    from database import get_supabase
    from datetime import date
    db   = get_supabase()
    today = date.today().isoformat()
    result = (
        db.table("expedition_runs")
        .select("id")
        .eq("discord_user_id", discord_user_id)
        .eq("run_date", today)
        .execute()
    )
    return len(result.data) > 0


def save_expedition_run(discord_user_id: str, zone_num: int, cp: int, tokens: int, nft: bool):
    """Save completed expedition run and update leaderboard."""
    from database import get_supabase, award_cp
    from datetime import date, timezone
    from datetime import datetime
    db    = get_supabase()
    today = date.today().isoformat()

    db.table("expedition_runs").insert({
        "discord_user_id": discord_user_id,
        "zone_num":        zone_num,
        "cp_earned":       cp,
        "tokens_earned":   tokens,
        "nft_dropped":     nft,
        "run_date":        today,
        "completed_at":    datetime.now(timezone.utc).isoformat(),
    }).execute()

    # Update expedition leaderboard
    existing = db.table("expedition_leaderboard").select("*").eq(
        "discord_user_id", discord_user_id
    ).execute()

    if existing.data:
        row = existing.data[0]
        db.table("expedition_leaderboard").update({
            "exp_cp_total":  row["exp_cp_total"] + cp,
            "runs_completed": row["runs_completed"] + 1,
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        }).eq("discord_user_id", discord_user_id).execute()
    else:
        db.table("expedition_leaderboard").insert({
            "discord_user_id": discord_user_id,
            "exp_cp_total":    cp,
            "runs_completed":  1,
        }).execute()

    # Also award CP to the main leaderboard (zones unlock from combined CP)
    award_cp(discord_user_id, cp, f"expedition_zone{zone_num}")


def get_expedition_leaderboard(limit: int = 10) -> list:
    from database import get_supabase
    db = get_supabase()
    result = (
        db.table("expedition_leaderboard")
        .select("*")
        .order("exp_cp_total", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


# ---------------------------------------------
# /expedition command
# ---------------------------------------------



# ─────────────────────────────────────────────
# Champion cooldown — 48 hours after winning
# ─────────────────────────────────────────────

def set_champion_cooldown(asset_id: int):
    """Record a Zappy as champion with a 48-hour cooldown."""
    try:
        from database import get_supabase
        from datetime import datetime, timezone
        db = get_supabase()
        db.table("champion_cooldowns").upsert({
            "asset_id":   asset_id,
            "won_at":     datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        print(f"Error setting champion cooldown: {e}")


def check_champion_cooldown(asset_id: int) -> dict:
    """
    Check if a Zappy is on cooldown.
    Returns {"on_cooldown": bool, "eligible_at": str or None}
    """
    try:
        from database import get_supabase
        from datetime import datetime, timezone, timedelta
        db = get_supabase()
        result = (
            db.table("champion_cooldowns")
            .select("won_at")
            .eq("asset_id", asset_id)
            .order("won_at", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return {"on_cooldown": False, "eligible_at": None}

        won_at      = datetime.fromisoformat(result.data[0]["won_at"])
        eligible_at = won_at + timedelta(hours=48)
        now         = datetime.now(timezone.utc)

        if now < eligible_at:
            return {
                "on_cooldown":  True,
                "eligible_at":  eligible_at.strftime("%B %d at %I:%M %p UTC"),
                "eligible_ts":  int(eligible_at.timestamp()),
            }
        return {"on_cooldown": False, "eligible_at": None}
    except Exception as e:
        print(f"Error checking champion cooldown: {e}")
        return {"on_cooldown": False, "eligible_at": None}



@tree.command(name="expedition", description="Send your Zappy on a solo expedition")
async def cmd_expedition(interaction: discord.Interaction):
    """Start an expedition run."""
    if not check_expedition_channel(interaction):
        await interaction.response.send_message(
            f"❌ Use <#{EXPEDITION_CHANNEL}> for Expedition commands." if EXPEDITION_CHANNEL else "❌ Expedition commands are restricted.",
            ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)

    # Check already ran today
    try:
        already_ran = await asyncio.to_thread(expedition_already_ran_today, user_id)
    except Exception as e:
        print(f"⚠️ Supabase error in /expedition already_ran check: {e}")
        await interaction.followup.send(
            "⚠️ Couldn't reach the database right now. Try again in a moment!",
            ephemeral=True
        )
        return
    if already_ran:
        await interaction.followup.send(
            "⏳ You've already run an expedition today. Come back tomorrow!",
            ephemeral=True
        )
        return

    # Check wallet linked
    try:
        wallet = await asyncio.to_thread(get_wallet, user_id)
    except Exception as e:
        print(f"⚠️ Supabase error in /expedition get_wallet: {e}")
        await interaction.followup.send(
            "⚠️ Couldn't reach the database right now. Try again in a moment!",
            ephemeral=True
        )
        return
    if not wallet:
        await interaction.followup.send("❌ Link your wallet first with `/link`.", ephemeral=True)
        return

    # Use cached wallet verification - fast, no indexer call
    from algorand_lookup import _wallet_cache, _wallet_cache_ts, WALLET_CACHE_TTL
    import time as _t
    now = _t.monotonic()
    if wallet in _wallet_cache and now - _wallet_cache_ts.get(wallet, 0) < WALLET_CACHE_TTL:
        ownership = _wallet_cache[wallet]
    else:
        # Cache miss - do the indexer call
        ownership = await verify_wallet(user_id, wallet)

    if not ownership["owns"]:
        await interaction.followup.send(
            "❌ No Zappies found. If you just linked your wallet, wait a moment and try again.",
            ephemeral=True
        )
        return

    # Get combined CP for zone unlock
    from database import get_player_rank
    try:
        rank_data = await asyncio.to_thread(get_player_rank, user_id)
    except Exception as e:
        print(f"⚠️ Supabase error in /expedition get_player_rank: {e}")
        await interaction.followup.send(
            "⚠️ Couldn't reach the database right now. Try again in a moment!",
            ephemeral=True
        )
        return
    cp_total    = rank_data.get("cp_total", 0)
    eligible    = get_eligible_zones(cp_total)
    zappy_count = len(ownership["zappies"]) + len(ownership["heroes"]) + len(ownership["collabs"])
    bonus       = get_collection_bonus(zappy_count)

    # Build Zappy list
    all_zappies = ownership["zappies"] + [
        {"asset_id": h["asset_id"], "name": h["name"], "unit_name": "Hero"}
        for h in ownership["heroes"]
    ]

    # Go straight to zone selection - Zappy pick happens after zone choice
    await _start_expedition_zone_select(interaction, user_id, all_zappies, eligible, cp_total, zappy_count, bonus, wallet)


async def _start_expedition_zone_select(
    interaction: discord.Interaction,
    user_id: str,
    all_zappies: list,
    eligible: list,
    cp_total: int,
    zappy_count: int,
    bonus: dict,
    wallet: str,
):
    """Show zone selection - Zappy pick happens after zone choice."""

    async def on_zone_selected(inter: discord.Interaction, zone_num: int):
        fee = EXPEDITION_FEES.get(zone_num, 0)
        if fee > 0:
            from token_rewards import REWARD_TOKEN_ID
            import aiohttp
            from algorand_lookup import INDEXER_URL
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"{INDEXER_URL}/v2/accounts/{wallet}/assets"
                    async with session.get(url, params={"asset-id": REWARD_TOKEN_ID},
                                           timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        data    = await resp.json() if resp.status == 200 else {}
                        assets  = data.get("assets", [])
                        balance = next((a.get("amount", 0) for a in assets
                                       if a["asset-id"] == REWARD_TOKEN_ID), 0)
            except Exception:
                balance = 0

            if balance < fee:
                await inter.followup.send(
                    f"❌ You need **{fee:,} ZAPP tokens** to enter {ZONES[zone_num]['name']}. "
                    f"You have {balance:,}. Play Clash or Zone 1 to earn more!",
                    ephemeral=True
                )
                return

        # Show smart Zappy picker for this zone
        await _show_smart_zappy_select(inter, user_id, zone_num, all_zappies, zappy_count, wallet, fee)

    zone_lines = []
    for z in eligible:
        zone = ZONES[z]
        fee  = EXPEDITION_FEES.get(z, 0)
        fee_str = f" · **{fee:,} ZAPP entry**" if fee > 0 else " · **Free**"
        zone_lines.append(f"{zone['emoji']} **{zone['name']}**{fee_str}")

    locked_lines = []
    for z in range(1, 6):
        if z not in eligible:
            zone = ZONES[z]
            locked_lines.append(f"🔒 {zone['name']} - need {zone['cp_required']:,} CP (you have {cp_total:,})")

    embed = discord.Embed(
        title       = "🗺️ Choose a Zone",
        description = f"You have **{zappy_count} Zappy/Zappies**. Pick a zone - the bot will show your best Zappies for it.",
        color       = 0xF5E642,
    )
    if zone_lines:
        embed.add_field(name="Available", value="\n".join(zone_lines), inline=False)
    if locked_lines:
        embed.add_field(name="Locked", value="\n".join(locked_lines), inline=False)
    embed.set_footer(text=f"CP: {cp_total:,} · {bonus['label']}")

    view = ZoneSelectView(eligible, cp_total, on_zone_selected)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)





# ---------------------------------------------
# Zone stat priorities and Zappy ranking
# ---------------------------------------------

ZONE_STAT_PRIORITY = {
    1: ("SPK", "VLT", "INS"),   # Mixed - SPK edges out for hidden paths
    2: ("SPK", "VLT", "INS"),   # Voltage Bay - luck and crits rule coastal events
    3: ("VLT", "INS", "SPK"),   # Molten Circuit - raw power breaks through
    4: ("SPK", "INS", "VLT"),   # Null Space - luck navigates the strange
    5: ("INS", "VLT", "SPK"),   # Apex Summit - survival first
}

ZONE_STAT_REASON = {
    1: {
        "SPK": "High SPK finds hidden paths and lucky breaks in the Fields.",
        "VLT": "Strong VLT powers through obstacles.",
        "INS": "Good INS absorbs minor hazards.",
    },
    2: {
        "SPK": "High SPK rides the surge tides and wins the coastal bets.",
        "VLT": "Strong VLT forces open wrecks and breaks barriers.",
        "INS": "Good INS weathers the storm cells.",
    },
    3: {
        "VLT": "High VLT is essential - Molten Circuit rewards raw power above all.",
        "INS": "Strong INS survives the heat and the rogue automaton.",
        "SPK": "Good SPK helps with timing the thermal vents.",
    },
    4: {
        "SPK": "High SPK navigates probability storms and strange frequencies.",
        "INS": "Strong INS survives the Null Space's unpredictable dangers.",
        "VLT": "Good VLT handles the gravity zones.",
    },
    5: {
        "INS": "High INS is critical - the Apex Storm Crown hits hard.",
        "VLT": "Strong VLT powers through the Infinite Generator.",
        "SPK": "Good SPK finds hidden paths at the summit.",
    },
}


def rank_zappies_for_zone(zappies: list, zone_num: int) -> list:
    """
    Rank a list of Zappies by their suitability for a specific zone.
    Returns top 5 with scores and explanations.
    Handles regular Zappies, Heroes, and Collabs.
    """
    priority = ZONE_STAT_PRIORITY.get(zone_num, ("SPK", "VLT", "INS"))
    primary, secondary, tertiary = priority

    from zappy_collection import ZAPPY_COLLECTION
    from algorand_lookup import HERO_ASSET_IDS, COLLAB_ASSET_IDS
    from stats_engine import calculate_stats, get_hero_stats, get_collab_stats

    ranked = []
    for z in zappies:
        asset_id = z["asset_id"]
        stats    = None
        traits   = {}
        image_url = ""

        # Hero
        if asset_id in HERO_ASSET_IDS:
            hero_type = HERO_ASSET_IDS[asset_id]
            hero_data = get_hero_stats(hero_type)
            if hero_data:
                stats     = {k: hero_data[k] for k in ("VLT", "INS", "SPK")}
                traits    = {"hero_type": hero_type}
                image_url = ""

        # Collab
        elif asset_id in COLLAB_ASSET_IDS:
            collab_type = COLLAB_ASSET_IDS[asset_id]
            collab_data = get_collab_stats(collab_type)
            if collab_data:
                stats     = {k: collab_data[k] for k in ("VLT", "INS", "SPK")}
                traits    = {"collab_type": collab_type}
                image_url = ""

        # Regular Zappy
        else:
            entry = ZAPPY_COLLECTION.get(asset_id)
            if not entry:
                continue
            traits = {
                "background": entry["background"],
                "body":       entry["body"],
                "earring":    entry["earring"],
                "eyes":       entry["eyes"],
                "eyewear":    entry["eyewear"],
                "head":       entry["head"],
                "mouth":      entry["mouth"],
                "skin":       entry["skin"],
            }
            stats     = calculate_stats(traits)
            image_url = entry.get("image_url", "")

        if not stats:
            continue

        score = (
            stats[primary]   * 3 +
            stats[secondary] * 2 +
            stats[tertiary]  * 1
        )
        ranked.append({
            **z,
            "stats":     stats,
            "score":     score,
            "traits":    traits,
            "image_url": image_url,
        })

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked[:5]


def build_zappy_reason(zappy: dict, zone_num: int) -> str:
    """Build a short explanation of why this Zappy is good for this zone."""
    priority = ZONE_STAT_PRIORITY.get(zone_num, ("SPK", "VLT", "INS"))
    primary, secondary, tertiary = priority
    stats    = zappy["stats"]
    reasons  = ZONE_STAT_REASON.get(zone_num, {})

    # Find their strongest stat from the priority list
    best_stat = max(priority, key=lambda s: stats[s])
    reason    = reasons.get(best_stat, "")

    return (
        f"⚡ {stats['VLT']} · 🛡️ {stats['INS']} · 🎲 {stats['SPK']}"
        + (f"\n_{reason}_" if reason else "")
    )


class SmartZappyView(discord.ui.View):
    """Button view showing top 5 Zappies for a zone with explanations."""

    def __init__(self, ranked_zappies: list, zone_num: int, on_zappy_callback):
        super().__init__(timeout=120)
        self.callback = on_zappy_callback
        self.chosen   = False

        for z in ranked_zappies:
            name = z.get("name", z.get("unit_name", f"ASA {z['asset_id']}"))
            btn  = discord.ui.Button(
                label     = name[:80],
                style     = discord.ButtonStyle.primary,
                custom_id = f"smart_zappy_{z['asset_id']}",
            )
            btn.callback = self._make_callback(z["asset_id"])
            self.add_item(btn)

    def _make_callback(self, asset_id: int):
        async def button_callback(interaction: discord.Interaction):
            if self.chosen:
                return
            self.chosen = True
            for item in self.children:
                item.disabled = True
            await interaction.response.defer(ephemeral=True)
            await self.callback(interaction, asset_id)
        return button_callback


async def _show_smart_zappy_select(
    inter: discord.Interaction,
    user_id: str,
    zone_num: int,
    all_zappies: list,
    zappy_count: int,
    wallet: str,
    fee: int,
):
    """Show the top 5 Zappies for a zone with stat explanations."""
    zone = ZONES[zone_num]
    priority = ZONE_STAT_PRIORITY.get(zone_num, ("SPK", "VLT", "INS"))
    primary  = priority[0]

    stat_labels = {"VLT": "Voltage (attack)", "INS": "Insulation (defense)", "SPK": "Spark (luck)"}

    # Rank Zappies
    ranked = rank_zappies_for_zone(all_zappies, zone_num)
    if not ranked:
        await inter.followup.send("❌ Couldn't load Zappy stats.", ephemeral=True)
        return

    embed = discord.Embed(
        title       = f"{zone['emoji']} {zone['name']} - Pick your Zappy",
        description = (
            f"**Key stat for this zone: {stat_labels.get(primary, primary)}**\n"
            f"Here are your top 5 Zappies ranked for this zone. "
            f"Stats and reasons shown below."
            + (f"\n\n💰 Entry fee: **{fee:,} ZAPP** (deducted from rewards)" if fee > 0 else "")
        ),
        color = zone["color"],
    )

    for i, z in enumerate(ranked):
        medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i]
        reason = build_zappy_reason(z, zone_num)
        embed.add_field(
            name   = f"{medal} {z.get('name', z.get('unit_name', 'Zappy'))}",
            value  = reason,
            inline = False,
        )

    embed.set_footer(text=f"You have {zappy_count} Zappies · Showing best 5 for {zone['name']}")

    # Show image of top Zappy
    if ranked[0].get("image_url"):
        embed.set_thumbnail(url=ranked[0]["image_url"])

    async def on_zappy_chosen(chosen_inter: discord.Interaction, asset_id: int):
        chosen = next((z for z in ranked if z["asset_id"] == asset_id), None)
        if not chosen:
            await chosen_inter.followup.send("❌ Zappy not found.", ephemeral=True)
            return
        # Build full zappy data dict compatible with run system
        zappy_data = {
            "asset_id":  chosen["asset_id"],
            "name":      chosen.get("name", chosen.get("unit_name", "")),
            "unit_name": chosen.get("unit_name", ""),
            "is_hero":   False,
            "is_collab": False,
            "traits":    chosen["traits"],
            "stats":     chosen["stats"],
            "image_url": chosen.get("image_url", ""),
        }
        await _run_expedition_beat(chosen_inter, user_id, zone_num, zappy_data, zappy_count, entry_fee=fee)

    view = SmartZappyView(ranked, zone_num, on_zappy_chosen)
    await inter.followup.send(embed=embed, view=view, ephemeral=True)


async def _run_expedition_beat(
    interaction: discord.Interaction,
    user_id: str,
    zone_num: int,
    zappy: dict,
    zappy_count: int,
    entry_fee: int = 0,
):
    """Start or continue an expedition beat."""
    # Start fresh run
    run = start_run(user_id, zone_num, zappy, zappy_count)
    run['entry_fee'] = entry_fee

    async def on_choice(inter: discord.Interaction, choice_index: int):
        updated_run = advance_beat(user_id, choice_index)
        if not updated_run:
            await inter.followup.send("Something went wrong with your run.", ephemeral=True)
            return

        # Post outcome
        outcome_embed = build_outcome_embed(updated_run)
        await inter.followup.send(embed=outcome_embed, ephemeral=True)

        if updated_run["complete"]:
            # Run is done
            nft_drop   = check_nft_drop(updated_run)
            buddy_drop = check_buddy_drop(zone_num)

            nft_prize_result   = None
            buddy_prize_result = None

            # Fetch wallet early — needed for drop rewards and token payout
            wallet = get_wallet(user_id)

            if buddy_drop:
                buddy_prize_result = await award_buddy(user_id, wallet, zone_num)
            if nft_drop:
                nft_prize_result = await award_nft_prize(user_id, wallet)

            has_any_drop = nft_drop or buddy_drop
            final_embed = build_run_complete_embed(updated_run, has_any_drop)

            # Save to DB
            save_expedition_run(
                discord_user_id = user_id,
                zone_num        = zone_num,
                cp              = updated_run["total_cp"],
                tokens          = updated_run["total_tokens"],
                nft             = nft_drop,
            )

            # Check CP milestone after expedition
            from database import get_player_rank
            exp_cp_total = get_player_rank(user_id).get("cp_total", 0)
            await assign_cp_role(user_id, exp_cp_total)

            # Send token rewards minus entry fee
            entry_fee = updated_run.get("entry_fee", 0)
            net_tokens = max(0, updated_run["total_tokens"] - entry_fee)
            if wallet and net_tokens > 0:
                from token_rewards import check_opted_in, send_token_reward, REWARD_TOKEN_ID
                import asyncio
                if await check_opted_in(wallet, REWARD_TOKEN_ID):
                    note = f"Zappy Expedition reward - Zone {zone_num}"
                    await asyncio.to_thread(
                        send_token_reward, wallet, net_tokens, note
                    )
            elif wallet and entry_fee > 0 and updated_run["total_tokens"] == 0:
                # Bad run - no tokens earned, fee already notified, nothing to send
                pass

            # Post to expedition channel
            exp_channel = bot.get_channel(EXPEDITION_CHANNEL) if EXPEDITION_CHANNEL else bot.get_channel(CLASH_CHANNEL)
            if exp_channel:
                token_line = f"🪙 +{net_tokens} tokens"
                public_embed = discord.Embed(
                    title       = f"{ZONES[zone_num]['emoji']} Expedition Complete!",
                    description = (
                        f"<@{user_id}> completed a **{ZONES[zone_num]['name']}** run "
                        f"with **{zappy.get('name', 'their Zappy')}**!\n"
                        f"⚡ +{updated_run['total_cp']} Exp CP · "
                        + token_line
                        + (" · 🐾 **ZAPPY BUDDY FOUND!**" if buddy_drop else "")
                        + (" · 🎉 **NFT DROP!**" if nft_drop else "")
                    ),
                    color = ZONES[zone_num]["color"],
                )
                if zappy.get("image_url"):
                    public_embed.set_image(url=zappy["image_url"])
                await exp_channel.send(embed=public_embed)

            # Final summary
            final_embed.description = (
                f"⚡ **{updated_run['total_cp']} Expedition CP** earned\n"
                f"🪙 **{net_tokens} tokens** sent to your wallet\n"
                f"📦 Collection bonus: {updated_run['collection_bonus']['label']}"
            )

            await inter.followup.send(embed=final_embed, ephemeral=True)
            if buddy_prize_result and buddy_prize_result.get("success"):
                await inter.followup.send(buddy_prize_result["message"], ephemeral=True)
                await assign_buddy_finder_role(user_id)
            if nft_prize_result and nft_prize_result.get("success"):
                await inter.followup.send(nft_prize_result["message"], ephemeral=True)
            end_run(user_id)
        else:
            # Next beat
            scene_embed = build_scene_embed(updated_run)
            image_path  = f"./images/{updated_run['events'][updated_run['beat']].get('image', '')}.png"
            view        = ExpeditionView(updated_run, on_choice)

            files = []
            if os.path.exists(image_path):
                files.append(discord.File(image_path))

            if files:
                await inter.followup.send(embed=scene_embed, view=view, files=files, ephemeral=True)
            else:
                await inter.followup.send(embed=scene_embed, view=view, ephemeral=True)

    # Post first beat
    scene_embed = build_scene_embed(run)
    image_path  = f"./images/{run['events'][0].get('image', '')}.png"
    view        = ExpeditionView(run, on_choice)

    files = []
    if os.path.exists(image_path):
        files.append(discord.File(image_path))

    if files:
        await interaction.followup.send(embed=scene_embed, view=view, files=files, ephemeral=True)
    else:
        await interaction.followup.send(embed=scene_embed, view=view, ephemeral=True)


@tree.command(name="claimnft", description="Claim your pending NFT prize from a Zone 5 expedition")
async def cmd_claimnft(interaction: discord.Interaction):
    """Claim a pending NFT prize once you have opted in to the asset."""
    if not check_expedition_channel(interaction):
        await interaction.response.send_message(
            f"❌ Use <#{EXPEDITION_CHANNEL}> for Expedition commands." if EXPEDITION_CHANNEL else "❌ Expedition commands are restricted.",
            ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    user_id = str(interaction.user.id)

    wallet = get_wallet(user_id)
    if not wallet:
        await interaction.followup.send("❌ Link your wallet first with `/link`.", ephemeral=True)
        return

    # Check buddy pool first, then NFT prizes
    result = await claim_buddy(user_id, wallet)
    if result is None:
        result = await claim_nft_prize(user_id, wallet)

    await interaction.followup.send(result["message"], ephemeral=True)

    # Announce publicly if successful
    if result.get("success"):
        channel = bot.get_channel(EXPEDITION_CHANNEL) if EXPEDITION_CHANNEL else bot.get_channel(CLASH_CHANNEL)
        if channel:
            if result.get("is_buddy"):
                await channel.send(
                    f"🐾 <@{user_id}> just claimed their Zappy Buddy: "
                    f"**{result['name']}**! They found a friend on their expedition. ⚡"
                )
            elif result.get("source") == "clash":
                await channel.send(
                    f"🎁 <@{user_id}> just claimed their Clash champion NFT prize: "
                    f"**{result['name']}**! ⚡🏆"
                )
            else:
                await channel.send(
                    f"🎉 <@{user_id}> just claimed their Zone 5 NFT prize: "
                    f"**{result['name']}**! 🏔️⚡"
                )






@tree.command(name="addzappies", description="ADMIN - add newly minted Zappies to the collection")
@app_commands.describe(
    ids="Comma-separated ASA IDs e.g. 12345678,87654321",
    metadata_url="Optional: paste the IPFS metadata URL directly (for single ASA only)"
)
async def cmd_addzappies(interaction: discord.Interaction, ids: str, metadata_url: str | None = None):
    """Fetch and register new Zappy ASA IDs into the live collection."""
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    raw = [s.strip() for s in ids.replace(" ", "").split(",") if s.strip()]
    try:
        asset_ids = [int(x) for x in raw]
    except ValueError:
        await interaction.followup.send("Invalid format - use comma-separated ASA IDs.", ephemeral=True)
        return

    if len(asset_ids) > 50:
        await interaction.followup.send("Max 50 ASA IDs at once.", ephemeral=True)
        return

    if metadata_url and len(asset_ids) > 1:
        await interaction.followup.send("metadata_url can only be used with a single ASA ID.", ephemeral=True)
        return

    await interaction.followup.send(
        f"Processing {len(asset_ids)} Zappy/Zappies in the background. "
        f"Results will be posted here when done.", ephemeral=True
    )

    asyncio.create_task(_addzappies_background(interaction.channel, asset_ids, metadata_url))


async def _addzappies_background(channel, asset_ids: list, direct_metadata_url: str | None = None):
    """Background task for /addzappies — runs IPFS fetches without blocking Discord."""
    import aiohttp, base64, re
    from algorand_lookup import INDEXER_URL
    from zappy_collection import ZAPPY_COLLECTION, ZAPPY_ASSET_IDS

    IPFS_GATEWAYS = [
        "https://ipfs.io/ipfs/",
        "https://dweb.link/ipfs/",
        "https://cloudflare-ipfs.com/ipfs/",
        "https://nftstorage.link/ipfs/",
        "https://w3s.link/ipfs/",
        "https://gateway.pinata.cloud/ipfs/",
    ]

    def _encode_varint(n):
        buf = []
        while True:
            towrite = n & 0x7f
            n >>= 7
            if n:
                buf.append(towrite | 0x80)
            else:
                buf.append(towrite)
                break
        return bytes(buf)

    def _decode_arc19(asset_url, reserve_address):
        try:
            from algosdk import encoding as algo_encoding
            match = re.search(r'\{ipfscid:(\d+):([^:]+):([^:]+):([^}]+)\}', asset_url)
            if not match:
                return None
            version   = int(match.group(1))
            codec_str = match.group(2)
            hash_type = match.group(4)
            digest    = algo_encoding.decode_address(reserve_address)
            if version == 0:
                import base58
                return base58.b58encode(bytes([0x12, 0x20]) + digest).decode()
            codec_map = {"raw": 0x55, "dag-pb": 0x70}
            hash_map  = {"sha2-256": 0x12}
            multihash = _encode_varint(hash_map.get(hash_type, 0x12)) + _encode_varint(len(digest)) + digest
            cid_bytes = _encode_varint(1) + _encode_varint(codec_map.get(codec_str, 0x55)) + multihash
            b32 = base64.b32encode(cid_bytes).decode().lower()
            return 'b' + b32.rstrip('=')
        except Exception as e:
            print(f"ARC19 decode error: {e}")
            return None

    added   = []
    skipped = []
    failed  = []

    async with aiohttp.ClientSession() as session:
        for asset_id in asset_ids:
            # Skip if already has traits
            if asset_id in ZAPPY_ASSET_IDS:
                existing = ZAPPY_COLLECTION.get(asset_id, {})
                if any([existing.get("background"), existing.get("body"), existing.get("skin")]):
                    skipped.append(asset_id)
                    continue

            try:
                url = f"{INDEXER_URL}/v2/assets/{asset_id}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        failed.append((asset_id, f"indexer {resp.status}"))
                        continue
                    data   = await resp.json()
                    params = data.get("asset", {}).get("params", {})

                name      = params.get("name", f"Zappy #{asset_id}")
                unit_name = params.get("unit-name", "")
                asset_url = params.get("url", "")
                reserve   = params.get("reserve", "")

                traits    = {}
                image_url = ""
                metadata_url = None

                # Use directly provided URL if given (bypasses gateway issues)
                if direct_metadata_url and len(asset_ids) == 1:
                    metadata_url = direct_metadata_url.split("#")[0].split("?")[0]
                elif asset_url.startswith("template-ipfs://") and reserve:
                    cid = _decode_arc19(asset_url, reserve)
                    if cid:
                        metadata_url = f"https://ipfs.io/ipfs/{cid}"
                elif asset_url.startswith("ipfs://"):
                    cid = asset_url.replace("ipfs://", "").split("#")[0]
                    metadata_url = f"https://ipfs.io/ipfs/{cid}"
                elif asset_url.startswith("https://") or asset_url.startswith("http://"):
                    # Strip fragment AND query params — just want the base metadata URL
                    base_url = asset_url.split("#")[0].split("?")[0]
                    metadata_url = base_url

                if metadata_url:
                    cid_part = metadata_url.split("/ipfs/")[-1] if "/ipfs/" in metadata_url else ""
                    # For direct HTTPS URLs, try original first then gateways
                    # For template-ipfs, try all gateways
                    if asset_url.startswith("https://") or asset_url.startswith("http://"):
                        fetch_urls = [metadata_url] + ([gw + cid_part for gw in IPFS_GATEWAYS if cid_part] if cid_part else [])
                    else:
                        fetch_urls = [gw + cid_part for gw in IPFS_GATEWAYS if cid_part] if cid_part else [metadata_url]

                    for fetch_url in fetch_urls:
                        try:
                            async with session.get(
                                fetch_url,
                                timeout=aiohttp.ClientTimeout(total=30),
                                headers={"User-Agent": "Mozilla/5.0"}
                            ) as resp:
                                if resp.status == 200:
                                    metadata = await resp.json(content_type=None)
                                    props = metadata.get("properties", {})
                                    if isinstance(props, list):
                                        props = {p["trait_type"]: p["value"] for p in props if "trait_type" in p}
                                    traits = {
                                        "background": props.get("Background", ""),
                                        "body":       props.get("Body", ""),
                                        "earring":    props.get("Earring", "None"),
                                        "eyes":       props.get("Eyes", ""),
                                        "eyewear":    props.get("Eyewear", "None"),
                                        "head":       props.get("Head", ""),
                                        "mouth":      props.get("Mouth", ""),
                                        "skin":       props.get("Skin", ""),
                                    }
                                    raw_img = metadata.get("image", "")
                                    if raw_img.startswith("ipfs://"):
                                        image_url = "https://ipfs.io/ipfs/" + raw_img.replace("ipfs://", "").split("/")[0]
                                    elif raw_img.startswith("https://"):
                                        image_url = raw_img
                                    if not image_url and "ipfs" in asset_url:
                                        image_url = asset_url.split("#")[0]
                                    break
                        except Exception:
                            continue

                entry = {
                    "name": name, "unit_name": unit_name, "image_url": image_url,
                    "background": traits.get("background", ""),
                    "body":       traits.get("body", ""),
                    "earring":    traits.get("earring", "None"),
                    "eyes":       traits.get("eyes", ""),
                    "eyewear":    traits.get("eyewear", "None"),
                    "head":       traits.get("head", ""),
                    "mouth":      traits.get("mouth", ""),
                    "skin":       traits.get("skin", ""),
                }
                ZAPPY_COLLECTION[asset_id] = entry
                ZAPPY_ASSET_IDS.add(asset_id)
                added.append((asset_id, name, entry))

            except Exception as e:
                failed.append((asset_id, str(e)[:80]))

    # Persist to Supabase
    if added:
        try:
            from database import get_supabase
            db = get_supabase()
            for asset_id, name, entry in added:
                db.table("extra_zappies").upsert({
                    "asset_id":   asset_id,
                    "name":       entry["name"],
                    "unit_name":  entry["unit_name"],
                    "image_url":  entry["image_url"],
                    "background": entry["background"],
                    "body":       entry["body"],
                    "earring":    entry["earring"],
                    "eyes":       entry["eyes"],
                    "eyewear":    entry["eyewear"],
                    "head":       entry["head"],
                    "mouth":      entry["mouth"],
                    "skin":       entry["skin"],
                }).execute()
        except Exception as e:
            await channel.send(f"⚠️ Supabase persist failed: {e}")

    # Post results
    lines = ["**Zappy Import Complete**"]
    if added:
        lines.append(f"Added {len(added)}:")
        for asset_id, name, _ in added:
            has_traits = bool(ZAPPY_COLLECTION.get(asset_id, {}).get("background"))
            lines.append(f"  {name} ({asset_id}) {'✅' if has_traits else '⚠️ no traits'}")
    if skipped:
        lines.append(f"Already in collection: {len(skipped)}")
    if failed:
        lines.append(f"Failed: {len(failed)}")
        for asset_id, reason in failed:
            lines.append(f"  {asset_id} - {reason}")

    await channel.send("\n".join(lines))


# ─── dummy placeholder so the old addzappies body below gets replaced ───
async def _addzappies_dummy():
    pass

    import aiohttp
    from algorand_lookup import INDEXER_URL
    import base64, re

    IPFS_GATEWAYS = [
        "https://crustipfs.xyz/ipfs/",
        "https://ipfs.io/ipfs/",
        "https://dweb.link/ipfs/",
        "https://cloudflare-ipfs.com/ipfs/",
        "https://nftstorage.link/ipfs/",
        "https://w3s.link/ipfs/",
        "https://gateway.pinata.cloud/ipfs/",
        "https://ipfs.algonode.dev/ipfs/",
    ]

    def _encode_varint(n):
        buf = []
        while True:
            towrite = n & 0x7f
            n >>= 7
            if n:
                buf.append(towrite | 0x80)
            else:
                buf.append(towrite)
                break
        return bytes(buf)

    def _decode_arc19(asset_url, reserve_address):
        try:
            from algosdk import encoding as algo_encoding
            match = re.search(r'\{ipfscid:(\d+):([^:]+):([^:]+):([^}]+)\}', asset_url)
            if not match:
                return None
            version   = int(match.group(1))
            codec_str = match.group(2)
            hash_type = match.group(4)
            digest    = algo_encoding.decode_address(reserve_address)
            if version == 0:
                import base58
                return base58.b58encode(bytes([0x12, 0x20]) + digest).decode()
            codec_map = {"raw": 0x55, "dag-pb": 0x70}
            hash_map  = {"sha2-256": 0x12}
            multihash = _encode_varint(hash_map.get(hash_type, 0x12)) + _encode_varint(len(digest)) + digest
            cid_bytes = _encode_varint(1) + _encode_varint(codec_map.get(codec_str, 0x55)) + multihash
            # CIDv1 base32 lower — must pad to multiple of 8 before decoding
            b32 = base64.b32encode(cid_bytes).decode().lower()
            return 'b' + b32.rstrip('=')
        except Exception as e:
            return None





@tree.command(name="settraits", description="ADMIN - manually set traits for a Zappy")
@app_commands.describe(
    asset_id   = "The ASA ID",
    background = "Background trait",
    body       = "Body trait",
    earring    = "Earring trait (or None)",
    eyes       = "Eyes trait",
    eyewear    = "Eyewear trait (or None)",
    head       = "Head trait",
    mouth      = "Mouth trait",
    skin       = "Skin trait",
    image_url  = "Full IPFS image URL (optional)"
)
async def cmd_settraits(
    interaction: discord.Interaction,
    asset_id:    int,
    background:  str,
    body:        str,
    eyes:        str,
    head:        str,
    mouth:       str,
    skin:        str,
    earring:     str = "None",
    eyewear:     str = "None",
    image_url:   str = "",
):
    """Manually set traits for a Zappy — bypasses IPFS."""
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    from zappy_collection import ZAPPY_COLLECTION, ZAPPY_ASSET_IDS
    from database import get_supabase

    # Get name from indexer if not already known
    name = ZAPPY_COLLECTION.get(asset_id, {}).get("name", f"Zappy #{asset_id}")
    unit_name = ZAPPY_COLLECTION.get(asset_id, {}).get("unit_name", "")

    entry = {
        "name":       name,
        "unit_name":  unit_name,
        "image_url":  image_url,
        "background": background,
        "body":       body,
        "earring":    earring,
        "eyes":       eyes,
        "eyewear":    eyewear,
        "head":       head,
        "mouth":      mouth,
        "skin":       skin,
    }

    # Update live collection
    ZAPPY_COLLECTION[asset_id] = entry
    ZAPPY_ASSET_IDS.add(asset_id)

    # Persist to Supabase
    try:
        db = get_supabase()
        db.table("extra_zappies").upsert({
            "asset_id": asset_id,
            **entry,
        }).execute()
        await interaction.followup.send(
            f"✅ Traits set for **{name}** (`{asset_id}`)\n"
            f"Background: {background} | Body: {body} | Skin: {skin}\n"
            f"Eyes: {eyes} | Head: {head} | Mouth: {mouth}\n"
            f"Earring: {earring} | Eyewear: {eyewear}",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"Live cache updated but Supabase failed: {e}", ephemeral=True)



@tree.command(name="exprank", description="View the Expedition leaderboard")
async def cmd_exprank(interaction: discord.Interaction):
    """Show top expedition players."""
    top = get_expedition_leaderboard(10)

    if not check_expedition_channel(interaction):
        await interaction.response.send_message(
            f"❌ Use <#{EXPEDITION_CHANNEL}> for Expedition commands." if EXPEDITION_CHANNEL else "❌ Expedition commands are restricted.",
            ephemeral=True
        )
        return

    if not top:
        await interaction.response.send_message(
            "No expeditions completed yet - be the first!", ephemeral=False
        )
        return

    medals = ["🥇", "🥈", "🥉"] + ["  " for _ in range(10)]
    lines  = ["**⚡ Expedition Leaderboard**", ""]

    for i, player in enumerate(top):
        uid = player["discord_user_id"]
        cp  = player["exp_cp_total"]
        runs = player["runs_completed"]
        try:
            member = interaction.guild.get_member(int(uid))
            name   = member.display_name if member else f"Explorer {uid[:6]}"
        except Exception:
            name = f"Explorer {uid[:6]}"
        lines.append(f"{medals[i]} **{name}** - {cp:,} Exp CP · {runs} runs")

    await interaction.response.send_message("\n".join(lines), ephemeral=False)





# ─────────────────────────────────────────────
# Role management helpers
# ─────────────────────────────────────────────

async def assign_streak_role(discord_user_id: str, streak_days: int):
    """Assign the highest earned streak role, remove lower ones."""
    try:
        guild  = bot.get_guild(GUILD_ID)
        member = guild.get_member(int(discord_user_id))
        if not member:
            return

        # Find the highest role they qualify for
        earned_role_id = None
        for days, role_id in STREAK_ROLES:
            if streak_days >= days:
                earned_role_id = role_id

        # Remove all streak roles first
        all_streak_role_ids = [r for _, r in STREAK_ROLES]
        roles_to_remove = [guild.get_role(r) for r in all_streak_role_ids
                           if guild.get_role(r) in member.roles]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Streak role update")

        # Assign the earned role
        if earned_role_id:
            role = guild.get_role(earned_role_id)
            if role:
                await member.add_roles(role, reason=f"{streak_days}-day streak milestone")
    except Exception as e:
        print(f"Error assigning streak role: {e}")


async def assign_cp_role(discord_user_id: str, cp_total: int):
    """Assign the highest earned CP milestone role."""
    try:
        guild  = bot.get_guild(GUILD_ID)
        member = guild.get_member(int(discord_user_id))
        if not member:
            return

        earned_role_id = None
        for threshold, role_id in CP_ROLES:
            if cp_total >= threshold:
                earned_role_id = role_id

        # Remove all CP roles first
        all_cp_role_ids = [r for _, r in CP_ROLES]
        roles_to_remove = [guild.get_role(r) for r in all_cp_role_ids
                           if guild.get_role(r) in member.roles]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="CP role update")

        if earned_role_id:
            role = guild.get_role(earned_role_id)
            if role:
                await member.add_roles(role, reason=f"{cp_total} CP milestone")
    except Exception as e:
        print(f"Error assigning CP role: {e}")


async def assign_champion_role(discord_user_id: str):
    """Assign Clash Champion role, stripping it from any current holder first."""
    try:
        guild  = bot.get_guild(GUILD_ID)
        member = guild.get_member(int(discord_user_id))
        if not member:
            return
        role = guild.get_role(ROLE_CLASH_CHAMPION)
        if role:
            # Strip from anyone who currently holds it
            for existing in list(role.members):
                if existing.id != member.id:
                    await existing.remove_roles(role, reason="New Clash Champion crowned")
            await member.add_roles(role, reason="Bracket champion")
    except Exception as e:
        print(f"Error assigning champion role: {e}")


async def remove_champion_role(discord_user_id: str):
    """Remove Clash Champion role when cooldown expires."""
    try:
        guild  = bot.get_guild(GUILD_ID)
        member = guild.get_member(int(discord_user_id))
        if not member:
            return
        role = guild.get_role(ROLE_CLASH_CHAMPION)
        if role and role in member.roles:
            await member.remove_roles(role, reason="Champion cooldown expired")
    except Exception as e:
        print(f"Error removing champion role: {e}")


async def assign_buddy_finder_role(discord_user_id: str):
    """Assign Zappy Buddy Finder role."""
    try:
        guild  = bot.get_guild(GUILD_ID)
        member = guild.get_member(int(discord_user_id))
        if not member:
            return
        role = guild.get_role(ROLE_BUDDY_FINDER)
        if role and role not in member.roles:
            await member.add_roles(role, reason="Found a Zappy Buddy on expedition")
    except Exception as e:
        print(f"Error assigning buddy finder role: {e}")



@tasks.loop(minutes=1)
async def session_scheduler():
    """Checks every minute and triggers session events at the right time."""
    global active_bracket_id, registration_open

    now          = datetime.now(timezone.utc)
    current_hour = now.hour
    current_min  = now.minute
    today        = now.date().isoformat()

    channel = bot.get_channel(CLASH_CHANNEL)
    if not channel:
        return

    # Use a set to track which events have fired today so they don't double-trigger
    if not hasattr(session_scheduler, "_fired"):
        session_scheduler._fired = set()

    # Reset fired set each new day
    fire_key_prefix = today

    def already_fired(key):
        return f"{fire_key_prefix}:{key}" in session_scheduler._fired

    def mark_fired(key):
        session_scheduler._fired.add(f"{fire_key_prefix}:{key}")
        # Clean up old keys
        session_scheduler._fired = {k for k in session_scheduler._fired if k.startswith(today)}

    # Morning open - 3:00 AM UTC
    if current_hour == 14 and current_min == 0 and not already_fired("morning_open"):
        mark_fired("morning_open")
        await open_registration("morning", channel)

    # Morning close - 3:30 AM UTC
    elif current_hour == 14 and current_min == 30 and not already_fired("morning_close"):
        mark_fired("morning_close")
        await close_and_resolve(channel)

    # Evening open - 3:00 PM UTC (15:00)
    elif current_hour == 0 and current_min == 0 and not already_fired("evening_open"):
        mark_fired("evening_open")
        await open_registration("evening", channel)

    # Evening close - 3:30 PM UTC (15:30)
    elif current_hour == 0 and current_min == 30 and not already_fired("evening_close"):
        mark_fired("evening_close")
        await close_and_resolve(channel)


async def open_registration(session: str, channel: discord.TextChannel):
    """Open bracket registration for a session."""
    global active_bracket_id, registration_open

    bracket_id = get_bracket_id(session)
    active_bracket_id = bracket_id
    registration_open = True

    session_name = "☀️ Morning" if session == "morning" else "🌙 Night"
    emoji_time = "2:00 PM UTC" if session == "morning" else "12:00 AM UTC"

    if session == "evening":
        await channel.send(
            f"@everyone\n"
            f"⚡ **ZAPPY CLASH - {session_name} Bracket is OPEN!**\n"
            f"\n"
            f"Registration is open for **30 minutes** ({emoji_time}).\n"
            f"Use `/clash` to enter your Zappy!\n"
            f"\n"
            f"Tonight's Night session has a **1.25x CP multiplier** on all wins. 🔥"
        )
    else:
        await channel.send(
            f"@everyone\n"
            f"⚡ **ZAPPY CLASH - {session_name} Bracket is OPEN!**\n"
            f"\n"
            f"Registration is open for **30 minutes** ({emoji_time}).\n"
            f"Use `/clash` to enter your Zappy!"
        )


async def close_and_resolve(channel: discord.TextChannel):
    """Close registration and run the full bracket."""
    global active_bracket_id, registration_open

    registration_open = False
    bracket_id = active_bracket_id

    entries = await asyncio.to_thread(get_bracket_entries, bracket_id)
    n = len(entries)

    if n < 2:
        await channel.send(
            f"⚠️ Not enough players registered ({n}/2 minimum). Bracket cancelled. "
            f"Come back next session!"
        )
        active_bracket_id = None
        return

    await channel.send(
        f"🔒 **Registration closed!** {n} Zappies entered the bracket.\n"
        f"⚡ Fights starting in 30 seconds..."
    )

    await asyncio.sleep(30)

    # ── Chaos Modifiers ───────────────────────────────────────────────────────
    active_modifiers  = []
    oracle_embed_data = None
    freaky_swap       = None
    entries_for_seeding = entries  # fallback: use original order if chaos fails

    try:
        from database import get_supabase, get_player_rank as _get_rank
        db = get_supabase()

        participant_list = []
        for e in entries:
            cp = _get_rank(e["discord_user_id"]).get("cp_total", 0)
            participant_list.append({
                "user_id":    e["discord_user_id"],
                "asset_id":   e["asset_id"],
                "zappy_name": e.get("unit_name") or e.get("name") or f"ASA {e['asset_id']}",
                "cp":         cp,
            })

        chaos = apply_all_modifiers(db, participant_list)
        active_modifiers  = chaos["active_modifiers"]
        oracle_embed_data = chaos["oracle_embed"]
        freaky_swap       = chaos["freaky_friday_swap"]
        modified_parts    = chaos["participants"]

        # Post oracle embed BEFORE bracket seedings drop
        if oracle_embed_data:
            oracle_embed = discord.Embed(
                title       = oracle_embed_data["title"],
                description = oracle_embed_data["description"],
                color       = oracle_embed_data["color"],
            )
            oracle_embed.set_footer(text=oracle_embed_data["footer"]["text"])
            await channel.send(embed=oracle_embed)
            await asyncio.sleep(4)

        # Post modifier announcements (Gravity Flip, Equalizer)
        for announcement in chaos["announcements"]:
            await channel.send(announcement)
            await asyncio.sleep(2)

        # Freaky Friday reveal — posted NOW so people can react before fights start
        if freaky_swap and freaky_swap[0]:
            await channel.send(freaky_friday_reveal(freaky_swap[0], freaky_swap[1]))
            await asyncio.sleep(2)

        # Remap entries to use chaos-modified CP for seeding
        cp_override = {p["asset_id"]: p["cp"] for p in modified_parts}
        for e in entries:
            e["_chaos_cp"] = cp_override.get(e["asset_id"], 0)
        entries_for_seeding = sorted(entries, key=lambda x: x["_chaos_cp"], reverse=True)

    except Exception as chaos_err:
        print(f"⚠️ Chaos modifiers skipped due to error: {chaos_err}")
        entries_for_seeding = entries  # bracket runs normally, no modifiers
    # ─────────────────────────────────────────────────────────────────────────

    # Build display name lookup
    guild = bot.get_guild(GUILD_ID)
    def get_display_name(discord_user_id):
        try:
            member = guild.get_member(int(discord_user_id))
            return member.display_name if member else f"Player {str(discord_user_id)[:4]}"
        except Exception:
            return f"Player {str(discord_user_id)[:4]}"

    # Seed bracket using (possibly chaos-modified) ordering
    matchups = seed_bracket(entries_for_seeding)

    # Show bracket with modifier tags
    modifier_tag = ""
    if "gravity_flip" in active_modifiers:
        modifier_tag = " · 🌀 GRAVITY FLIP"
    elif "equalizer" in active_modifiers:
        modifier_tag = " · ⚖️ EQUALIZER"
    if "freaky_friday" in active_modifiers:
        modifier_tag += " · 🔀 FREAKY FRIDAY"

    bracket_lines = [f"⚡ **BRACKET START** - {n} fighters, {len(matchups)} first-round matchups!{modifier_tag}\n"]
    for i, entry in enumerate(entries_for_seeding):
        zappy_name  = entry.get("unit_name") or entry.get("name") or f"ASA {entry['asset_id']}"
        player_name = get_display_name(entry["discord_user_id"])
        bracket_lines.append(f"#{i+1} **{zappy_name}** · {player_name}")
    await channel.send("\n".join(bracket_lines))

    # Run all rounds
    current_round = matchups
    round_num = 1
    cp_multiplier = 1.25 if "evening" in bracket_id else 1.0

    while len(current_round) > 0:
        next_round = []
        # Count actual players remaining (byes count as 1 each)
        players_left = sum(2 if m[1] is not None else 1 for m in current_round)
        if players_left > 8:
            round_label = f"Round of {players_left}"
        elif players_left > 4:
            round_label = "Quarterfinals"
        elif players_left > 2:
            round_label = "Semifinals"
        else:
            round_label = "FINAL"
        await channel.send(f"\n🔔 **{round_label}**\n")
        await asyncio.sleep(3)

        for matchup in current_round:
            player_a, player_b = matchup

            # Handle byes
            if player_b is None:
                try:
                    bye_member = interaction.guild.get_member(int(player_a['discord_user_id'])) if hasattr(interaction, 'guild') else None
                    bye_name = bye_member.display_name if bye_member else f"<@{player_a['discord_user_id']}>"
                except Exception:
                    bye_name = f"<@{player_a['discord_user_id']}>"
                await channel.send(f"🎯 **{bye_name}** advances with a bye.")
                next_round.append(player_a)
                continue

            # Fetch Zappy data — use collection table for consistent stats
            from zappy_collection import ZAPPY_COLLECTION
            from algorand_lookup import HERO_ASSET_IDS, COLLAB_ASSET_IDS, HERO_IMAGES, COLLAB_IMAGES
            from stats_engine import calculate_stats, get_hero_stats, get_collab_stats

            def _get_fighter_data(asset_id):
                # ── Heroes — hardcoded stats, not in ZAPPY_COLLECTION ──
                if asset_id in HERO_ASSET_IDS:
                    hero_type = HERO_ASSET_IDS[asset_id]
                    hero_data = get_hero_stats(hero_type)
                    if hero_data:
                        return {
                            "asset_id":  asset_id,
                            "name":      f"Zappy Hero — {hero_type}",
                            "unit_name": hero_type,
                            "image_url": HERO_IMAGES.get(hero_type, ""),
                            "stats":     hero_data,
                            "traits":    {"hero_type": hero_type},
                        }

                # ── Collabs — hardcoded stats, not in ZAPPY_COLLECTION ──
                if asset_id in COLLAB_ASSET_IDS:
                    collab_type = COLLAB_ASSET_IDS[asset_id]
                    collab_data = get_collab_stats(collab_type)
                    if collab_data:
                        return {
                            "asset_id":  asset_id,
                            "name":      f"{collab_type}",
                            "unit_name": collab_type,
                            "image_url": COLLAB_IMAGES.get(collab_type, ""),
                            "stats":     collab_data,
                            "traits":    {"collab_type": collab_type},
                        }

                # ── Regular Zappies — look up in collection ──
                entry = ZAPPY_COLLECTION.get(asset_id)
                if not entry:
                    return None
                traits = {
                    "background": entry.get("background",""),
                    "body":       entry.get("body",""),
                    "earring":    entry.get("earring","None"),
                    "eyes":       entry.get("eyes",""),
                    "eyewear":    entry.get("eyewear","None"),
                    "head":       entry.get("head",""),
                    "mouth":      entry.get("mouth",""),
                    "skin":       entry.get("skin",""),
                }
                stats = calculate_stats(traits)
                return {
                    "asset_id":  asset_id,
                    "name":      entry.get("name", f"Zappy #{asset_id}"),
                    "unit_name": entry.get("unit_name",""),
                    "image_url": entry.get("image_url",""),
                    "stats":     stats,
                    "traits":    traits,
                }

            zappy_a = _get_fighter_data(player_a["asset_id"])
            zappy_b = _get_fighter_data(player_b["asset_id"])

            # Fallback to live fetch if not in collection
            if not zappy_a:
                zappy_a = await fetch_zappy_traits(player_a["asset_id"])
            if not zappy_b:
                zappy_b = await fetch_zappy_traits(player_b["asset_id"])

            if not zappy_a or not zappy_b:
                await channel.send("⚠️ Couldn't load one fighter's stats - skipping this matchup.")
                continue

            fighter_a = build_fighter(zappy_a)
            fighter_b = build_fighter(zappy_b)

            # Run the battle
            result = resolve_battle(fighter_a, fighter_b)

            # Get player display names for this matchup
            name_a = get_display_name(player_a["discord_user_id"])
            name_b = get_display_name(player_b["discord_user_id"])

            # -- Pre-fight embed: both Zappies side by side --
            pre_embed = discord.Embed(
                title="⚡ BRACKET MATCH",
                color=0xF5E642,
            )
            pre_embed.add_field(
                name=f"{fighter_a.display_name} · {name_a}",
                value=f"⚡ VLT {fighter_a.VLT} · 🛡️ INS {fighter_a.INS} · 🎲 SPK {fighter_a.SPK}"
                      + (f"\n✨ {fighter_a.combo}" if fighter_a.combo else ""),
                inline=True,
            )
            pre_embed.add_field(name="vs.", value="⚡", inline=True)
            pre_embed.add_field(
                name=f"{fighter_b.display_name} · {name_b}",
                value=f"⚡ VLT {fighter_b.VLT} · 🛡️ INS {fighter_b.INS} · 🎲 SPK {fighter_b.SPK}"
                      + (f"\n✨ {fighter_b.combo}" if fighter_b.combo else ""),
                inline=True,
            )
            # Show both images - A as thumbnail, B as image
            if fighter_a.image_url:
                pre_embed.set_thumbnail(url=fighter_a.image_url)
            if fighter_b.image_url:
                pre_embed.set_image(url=fighter_b.image_url)
            await channel.send(embed=pre_embed)
            await asyncio.sleep(2)

            # -- Play-by-play text (skip header lines and final win line) --
            log_lines = result["log"]
            # Skip first 6 lines (stat header shown in embed)
            # Skip last line (win announcement shown in embed)
            play_lines = log_lines[6:-1]
            play_by_play = "\n".join(play_lines)
            chunks = [play_by_play[i:i+1800] for i in range(0, len(play_by_play), 1800)]
            for chunk in chunks:
                if chunk.strip():
                    await channel.send(chunk)
                    await asyncio.sleep(1)

            # Award CP
            winner_id = player_a["discord_user_id"] if result["winner"].asset_id == player_a["asset_id"] else player_b["discord_user_id"]
            loser_id  = player_b["discord_user_id"] if winner_id == player_a["discord_user_id"] else player_a["discord_user_id"]

            win_cp   = int(CP_WIN * cp_multiplier)
            lose_cp  = int(CP_LOSS * cp_multiplier)
            upset_cp = int(CP_UPSET_BONUS * cp_multiplier) if result["is_upset"] else 0

            await asyncio.to_thread(award_cp, winner_id, win_cp + upset_cp, f"bracket_win_{bracket_id}")
            await asyncio.to_thread(award_cp, loser_id,  lose_cp,            f"bracket_loss_{bracket_id}")

            # Check CP milestones for both players
            from database import get_player_rank
            winner_cp = (await asyncio.to_thread(get_player_rank, winner_id)).get("cp_total", 0)
            loser_cp  = (await asyncio.to_thread(get_player_rank, loser_id)).get("cp_total", 0)
            await assign_cp_role(winner_id, winner_cp)
            await assign_cp_role(loser_id, loser_cp)

            # Update streaks
            winner_streak = await asyncio.to_thread(update_streak, winner_id, True)
            await asyncio.to_thread(update_streak, loser_id, False)

            # Get winner wallet early — needed for streak and token rewards
            winner_wallet = await asyncio.to_thread(get_wallet, winner_id)

            # Assign streak role if milestone hit
            current_streak = winner_streak.get("current_streak", 0)
            if current_streak in (3, 7, 14, 30):
                await assign_streak_role(winner_id, current_streak)

            # Streak milestone token rewards
            if winner_wallet and winner_streak.get("rewards"):
                for reward in winner_streak["rewards"]:
                    days = reward.get("days")
                    if days in (7, 30):
                        streak_token = await award_streak_tokens(winner_wallet, days)
                        if streak_token.get("success"):
                            await channel.send(
                                f"🔥 <@{winner_id}> hit a **{days}-day streak!** "
                                f"{streak_token['message']}"
                            )

            # Save result
            await asyncio.to_thread(save_battle_result,
                bracket_id=bracket_id,
                winner_discord_id=winner_id,
                loser_discord_id=loser_id,
                winner_asset_id=result["winner"].asset_id,
                loser_asset_id=result["loser"].asset_id,
                is_upset=result["is_upset"],
                round_num=round_num,
            )

            # -- Token rewards --
            loser_wallet  = await asyncio.to_thread(get_wallet, loser_id)
            is_evening    = "evening" in bracket_id
            token_msg     = ""

            if winner_wallet:
                token_result = await award_win_tokens(
                    discord_user_id = winner_id,
                    wallet_address  = winner_wallet,
                    is_upset        = result["is_upset"],
                    is_champion     = False,
                    is_evening      = is_evening,
                )
                if token_result["success"]:
                    token_msg = f"\n{token_result['message']}"
                elif token_result.get("reason") == "not_opted_in":
                    token_msg = f"\n⚠️ <@{winner_id}>: {token_result['message']}"

            # -- Winner embed with image --
            winner   = result["winner"]
            win_desc = f"💰 **+{win_cp + upset_cp} CP** → <@{winner_id}>"
            if result["is_upset"]:
                win_desc += f" *(+{upset_cp} upset bonus!)*"
            win_desc += f"\n💰 **+{lose_cp} CP** → <@{loser_id}>"
            if token_msg:
                win_desc += token_msg

            winner_name = name_a if result["winner"].asset_id == player_a["asset_id"] else name_b
            win_embed = discord.Embed(
                title=f"🏆 {winner.display_name} wins! ({winner_name})",
                description=win_desc,
                color=0xF5E642,
            )
            if winner.image_url:
                win_embed.set_image(url=winner.image_url)
            win_embed.set_footer(text="Use /rank to check your CP · /streak for daily streak")
            await channel.send(embed=win_embed)

            # Determine who advances
            if result["winner"].asset_id == player_a["asset_id"]:
                next_round.append(player_a)
            else:
                next_round.append(player_b)

            await asyncio.sleep(3)

        # Deduplicate — remove any player that appears more than once
        seen_ids = set()
        deduped = []
        for p in next_round:
            pid = p["discord_user_id"]
            if pid not in seen_ids:
                seen_ids.add(pid)
                deduped.append(p)
        next_round = deduped

        # Pair next round: highest seed vs lowest seed
        current_round_pairs = []
        lo, hi = 0, len(next_round) - 1
        while lo < hi:
            current_round_pairs.append((next_round[lo], next_round[hi]))
            lo += 1
            hi -= 1
        # If odd number, last player gets a bye
        if len(next_round) % 2 == 1:
            current_round_pairs.append((next_round[len(next_round) // 2], None))

        current_round = current_round_pairs
        round_num += 1

        if len(next_round) == 1:
            # We have a bracket champion
            champion_id = next_round[0]["discord_user_id"]
            champ_asset = await fetch_zappy_traits(next_round[0]["asset_id"])
            champ_name = champ_asset["name"] if champ_asset else f"ASA {next_round[0]['asset_id']}"

            bonus_cp = int(CP_BRACKET_WIN * cp_multiplier)
            await asyncio.to_thread(award_cp, champion_id, bonus_cp, f"bracket_champion_{bracket_id}")

            # Set 48-hour cooldown on champion Zappy
            await asyncio.to_thread(set_champion_cooldown, next_round[0]["asset_id"])

            # Assign Clash Champion role
            await assign_champion_role(champion_id)

            # Schedule role removal after 48 hours
            async def remove_champ_role_later(uid):
                await asyncio.sleep(48 * 3600)
                await remove_champion_role(uid)
            asyncio.create_task(remove_champ_role_later(champion_id))

            # Champion token reward
            champ_wallet = await asyncio.to_thread(get_wallet, champion_id)
            champ_token_msg = ""
            if champ_wallet:
                champ_token = await award_win_tokens(
                    discord_user_id = champion_id,
                    wallet_address  = champ_wallet,
                    is_upset        = False,
                    is_champion     = True,
                    is_evening      = is_evening,
                )
                if champ_token["success"]:
                    champ_token_msg = f"\n{champ_token['message']}"
                elif champ_token.get("reason") == "not_opted_in":
                    champ_token_msg = f"\n⚠️ Opt in to ASA {os.environ.get('REWARD_TOKEN_ID', '2572874483')} to receive token rewards!"

            # -- NFT Drop (5% chance, champion only) --
            nft_drop_msg = ""
            if random.random() < 0.05 and champ_wallet:
                nft_drop_result = await award_nft_prize(champion_id, champ_wallet, source="clash")
                if nft_drop_result and nft_drop_result.get("success"):
                    nft_drop_msg = (
                        f"\n\n🎁 **NFT DROP!** The prize wallet is feeling generous — "
                        f"<@{champion_id}> won a Zappies NFT!\n"
                        f"Use `/claimnft` to collect your prize. ⚡"
                    )

            await channel.send(
                f"\n🏆 **BRACKET CHAMPION!**\n"
                f"<@{champion_id}> wins it all with **{champ_name}**!\n"
                f"💰 **+{bonus_cp} CP** bracket champion bonus!"
                f"{champ_token_msg}"
                f"{nft_drop_msg}\n"
                f"\n"
                f"⚡ Use `/top` to see the updated leaderboard."
            )
            break

    active_bracket_id = None


# ---------------------------------------------
# Bot events
# ---------------------------------------------

@bot.event
async def on_ready():
    print(f"⚡ Zappy Clash bot online as {bot.user}")
    setup_auction_commands(bot, tree, GUILD_ID)

    # Grand Prix
    await bot.add_cog(GrandPrixCog(bot))
    bot.add_view(grand_prix_cog.JoinAlgoView())
    bot.add_view(grand_prix_cog.JoinZapView())
    print("⚡ Grand Prix cog loaded")

    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"✅ Slash commands synced to guild {GUILD_ID}")
    await tree.sync()
    print(f"✅ Slash commands synced globally")
    _load_extra_zappies()
    session_scheduler.start()
    print("⏰ Session scheduler running")
    auction_checker.start()

def _load_extra_zappies():
    """Load Zappies added via /addzappies from Supabase into the live collection."""
    try:
        from database import get_supabase
        from zappy_collection import ZAPPY_COLLECTION, ZAPPY_ASSET_IDS
        db     = get_supabase()
        result = db.table("extra_zappies").select("*").execute()
        loaded = 0
        for row in result.data or []:
            asset_id = int(row["asset_id"])
            if asset_id not in ZAPPY_ASSET_IDS:
                ZAPPY_COLLECTION[asset_id] = {
                    "name":       row["name"],
                    "unit_name":  row["unit_name"],
                    "image_url":  row["image_url"],
                    "background": row["background"],
                    "body":       row["body"],
                    "earring":    row["earring"],
                    "eyes":       row["eyes"],
                    "eyewear":    row["eyewear"],
                    "head":       row["head"],
                    "mouth":      row["mouth"],
                    "skin":       row["skin"],
                }
                ZAPPY_ASSET_IDS.add(asset_id)
                loaded += 1
        if loaded:
            print(f"✅ Loaded {loaded} extra Zappies from Supabase")
    except Exception as e:
        print(f"⚠️ Could not load extra Zappies from Supabase: {e}")


# ---------------------------------------------
# Run the bot
# ---------------------------------------------
if __name__ == "__main__":
    bot.run(BOT_TOKEN)

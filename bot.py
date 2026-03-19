"""
bot.py
------
Zappy Clash Discord bot — main entry point.

Commands:
  /link     — Connect your Algorand wallet
  /clash    — Register for the current bracket
  /stats    — View your Zappy's stats
  /rank     — View your CP and rank
  /top      — Leaderboard
  /streak   — View your daily streak

Scheduled sessions:
  9:00 AM UTC — Morning Bracket
  9:00 PM UTC — Evening Bracket

Setup:
  1. Copy .env.example to .env and fill in your keys
  2. Run: pip install -r requirements.txt
  3. Run: python bot.py
"""

import os
import asyncio
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timezone, time as dtime
from dotenv import load_dotenv

# Our modules
from algorand_lookup import link_wallet as verify_wallet, fetch_zappy_traits
from battle_engine   import build_fighter, resolve_battle
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

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
load_dotenv()
BOT_TOKEN       = os.environ["DISCORD_BOT_TOKEN"]
GUILD_ID        = int(os.environ["DISCORD_GUILD_ID"])
CLASH_CHANNEL   = int(os.environ["CLASH_CHANNEL_ID"])    # #zappy-clash channel ID
ANNOUNCE_CHANNEL= int(os.environ.get("ANNOUNCE_CHANNEL_ID", CLASH_CHANNEL))

# Session timing (UTC)
MORNING_OPEN    = dtime(3,  0,  tzinfo=timezone.utc)
MORNING_CLOSE   = dtime(3, 30,  tzinfo=timezone.utc)
MORNING_RESOLVE = dtime(3, 35,  tzinfo=timezone.utc)

EVENING_OPEN    = dtime(15,  0, tzinfo=timezone.utc)
EVENING_CLOSE   = dtime(15, 30, tzinfo=timezone.utc)
EVENING_RESOLVE = dtime(15, 35, tzinfo=timezone.utc)

# ─────────────────────────────────────────────
# Bot setup
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Track active bracket state
active_bracket_id: str | None = None
registration_open: bool = False


# ─────────────────────────────────────────────
# Helper: get current bracket ID
# ─────────────────────────────────────────────
def get_bracket_id(session: str) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    return f"{session}_{today}"


# ─────────────────────────────────────────────
# SLASH COMMANDS
# ─────────────────────────────────────────────

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
@app_commands.describe(asset_id="Your Zappy's ASA ID (optional if you only have one)")
async def cmd_clash(interaction: discord.Interaction, asset_id: int | None = None):
    """Register for the active bracket."""
    await interaction.response.defer(ephemeral=True)

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
    wallet = get_wallet(user_id)
    if not wallet:
        await interaction.followup.send(
            "❌ You haven't linked your wallet yet. Use `/link` first!",
            ephemeral=True
        )
        return

    # Check if already registered
    if is_registered(user_id, active_bracket_id):
        await interaction.followup.send(
            "✅ You're already registered for this bracket! Check "
            f"<#{CLASH_CHANNEL}> when fights start.",
            ephemeral=True
        )
        return

    # Verify ownership and get Zappy
    await interaction.followup.send("⚡ Verifying your Zappy...", ephemeral=True)
    ownership = await verify_wallet(user_id, wallet)

    if not ownership["owns"]:
        await interaction.followup.send(
            "❌ No Zappies found in your linked wallet. "
            "Use `/link` to update your wallet address.",
            ephemeral=True
        )
        return

    # Determine which asset to use
    all_assets = (
        ownership["zappies"] +
        [{"asset_id": h["asset_id"], "unit_name": h["hero_type"]} for h in ownership["heroes"]] +
        [{"asset_id": c["asset_id"], "unit_name": "ShittyKitties"} for c in ownership["collabs"]]
    )

    if asset_id:
        # Validate the specified asset belongs to them
        found = next((a for a in all_assets if a["asset_id"] == asset_id), None)
        if not found:
            await interaction.followup.send(
                f"❌ ASA {asset_id} not found in your wallet.",
                ephemeral=True
            )
            return
        chosen_asset_id = asset_id
    elif len(all_assets) == 1:
        chosen_asset_id = all_assets[0]["asset_id"]
    else:
        # Multiple — ask them to specify
        # Fetch names for all assets so we show friendly names not just IDs
        lines = []
        for a in all_assets:
            display = a.get('name') or a.get('unit_name') or f"ASA {a['asset_id']}"
            lines.append(f"  • **{display}** — `/clash asset_id:{a['asset_id']}`")
        names = "\n".join(lines)
        await interaction.followup.send(
            f"You have **{len(all_assets)} Zappies**! Reply with the one you want to enter:\n\n{names}",
            ephemeral=True
        )
        return

    # Fetch stats for the chosen Zappy
    zappy = await fetch_zappy_traits(chosen_asset_id)
    if not zappy:
        await interaction.followup.send(
            "❌ Couldn't load your Zappy's traits from IPFS. Try again in a moment.",
            ephemeral=True
        )
        return

    # Register
    register_for_bracket(user_id, chosen_asset_id, active_bracket_id)

    # Show their stats as an embed with image
    stats     = zappy.get("stats", {})
    name      = zappy.get("name", f"ASA {chosen_asset_id}")
    image_url = zappy.get("image_url", "")

    embed = discord.Embed(
        title=f"✅ {name} is in the bracket!",
        description=f"⚡ VLT {stats.get('VLT','?')} · 🛡️ INS {stats.get('INS','?')} · 🎲 SPK {stats.get('SPK','?')}",
        color=0xF5E642,
    )
    if stats.get("combo"):
        embed.add_field(name="Combo", value=stats["combo"], inline=False)
    if stats.get("ability"):
        ab = stats["ability"]
        embed.add_field(name=f"⚡ {ab['name']}", value=ab["desc"], inline=False)
    if image_url:
        embed.set_thumbnail(url=image_url)
    embed.set_footer(text=f"Fights start when registration closes · Watch #{CLASH_CHANNEL}")
    await interaction.followup.send(embed=embed, ephemeral=True)

    # Announce in clash channel
    clash_ch = bot.get_channel(CLASH_CHANNEL)
    if clash_ch:
        await clash_ch.send(
            f"⚡ **{interaction.user.display_name}** enters the bracket with "
            f"**{name}** — VLT {stats.get('VLT')} · INS {stats.get('INS')} · SPK {stats.get('SPK')}"
        )


@tree.command(name="stats", description="Preview your Zappy's battle stats")
@app_commands.describe(asset_id="Your Zappy's ASA ID (optional if you only have one)")
async def cmd_stats(interaction: discord.Interaction, asset_id: int | None = None):
    """Show a Zappy's stats without registering."""
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)
    wallet  = get_wallet(user_id)

    if not wallet:
        await interaction.followup.send("❌ Link your wallet first with `/link`.", ephemeral=True)
        return

    ownership = await verify_wallet(user_id, wallet)
    if not ownership["owns"]:
        await interaction.followup.send("❌ No Zappies found in your linked wallet.", ephemeral=True)
        return

    all_assets = ownership["zappies"] + [
        {"asset_id": h["asset_id"]} for h in ownership["heroes"]
    ]

    if asset_id:
        chosen_id = asset_id
    elif len(all_assets) == 1:
        chosen_id = all_assets[0]["asset_id"]
    else:
        names = "\n".join(
            f"  • **{a.get('name', a.get('unit_name', f'ASA {a["asset_id"]}'))}** — `{a['asset_id']}`"
            for a in all_assets
        )
        await interaction.followup.send(
            f"You have {len(all_assets)} Zappies! Use `/stats asset_id:XXXXX` to pick one, "
            f"or check `/clash` to see them all.\n\n{names}",
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
        value=f"⚡ **VLT** {stats.get('VLT','?')} — Attack\n"
              f"🛡️ **INS** {stats.get('INS','?')} — Defense\n"
              f"🎲 **SPK** {stats.get('SPK','?')} — Crit chance",
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
    if stats.get("ability"):
        ab = stats["ability"]
        embed.add_field(name=f"⚡ Ability: {ab['name']}", value=ab["desc"], inline=False)

    image_url = zappy.get("image_url", "")
    if image_url:
        embed.set_thumbnail(url=image_url)

    embed.set_footer(text=f"ASA {chosen_id}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="rank", description="Check your Clash Points and rank")
async def cmd_rank(interaction: discord.Interaction):
    """Show a player's rank and CP."""
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
    top = get_leaderboard(10)

    if not top:
        await interaction.response.send_message("No players on the board yet — be the first!", ephemeral=False)
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
        lines.append(f"{medals[i]} **{name}** — {cp:,} CP")

    await interaction.response.send_message("\n".join(lines), ephemeral=False)


@tree.command(name="myzappies", description="List all your Zappies with names and ASA IDs")
async def cmd_myzappies(interaction: discord.Interaction):
    """Show all Zappies in the linked wallet with names."""
    await interaction.response.defer(ephemeral=True)

    user_id = str(interaction.user.id)
    wallet  = get_wallet(user_id)

    if not wallet:
        await interaction.followup.send("❌ Link your wallet first with `/link`.", ephemeral=True)
        return

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

    # Main collection — paginate into chunks of 20 per field (Discord limit)
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
        hero_lines = [f"🦸 **Zappy Hero — {h['hero_type']}** `{h['asset_id']}`" for h in heroes]
        embed.add_field(name="Heroes", value="\n".join(hero_lines), inline=False)

    if collabs:
        collab_lines = [f"🐱 **Shitty Zappy Kitty** `{c['asset_id']}`" for c in collabs]
        embed.add_field(name="Collabs", value="\n".join(collab_lines), inline=False)

    embed.set_footer(text="Use /stats asset_id:XXXXX to see a Zappy's battle stats")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="debug", description="ADMIN ONLY — debug a Zappy's image URL")
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


@tree.command(name="testbracket", description="ADMIN ONLY — trigger a test bracket right now")
async def cmd_testbracket(interaction: discord.Interaction):
    """Manually trigger a bracket for testing. Only works for the server owner."""
    global active_bracket_id, registration_open

    # Only server owner can run this
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    await interaction.response.send_message("⚡ Starting test bracket — registration open for 2 minutes!", ephemeral=True)

    bracket_id = f"test_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    active_bracket_id = bracket_id
    registration_open = True

    channel = bot.get_channel(CLASH_CHANNEL)

    await channel.send(
        "⚡ **ZAPPY CLASH — TEST BRACKET**\n"
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


# ─────────────────────────────────────────────
# SCHEDULED SESSIONS
# ─────────────────────────────────────────────

@tasks.loop(minutes=1)
async def session_scheduler():
    """Checks every minute and triggers session events at the right time."""
    global active_bracket_id, registration_open

    now = datetime.now(timezone.utc)
    current_time = now.time().replace(second=0, microsecond=0)

    channel = bot.get_channel(CLASH_CHANNEL)
    if not channel:
        return

    # Morning open
    if current_time == MORNING_OPEN.replace(second=0):
        await open_registration("morning", channel)

    # Morning close + resolve
    elif current_time == MORNING_CLOSE.replace(second=0):
        await close_and_resolve(channel)

    # Evening open
    elif current_time == EVENING_OPEN.replace(second=0):
        await open_registration("evening", channel)

    # Evening close + resolve
    elif current_time == EVENING_CLOSE.replace(second=0):
        await close_and_resolve(channel)


async def open_registration(session: str, channel: discord.TextChannel):
    """Open bracket registration for a session."""
    global active_bracket_id, registration_open

    bracket_id = get_bracket_id(session)
    active_bracket_id = bracket_id
    registration_open = True

    session_name = "☀️ Morning" if session == "morning" else "🌙 Evening"
    emoji_time = "9:00 AM UTC" if session == "morning" else "9:00 PM UTC"

    await channel.send(
        f"⚡ **ZAPPY CLASH — {session_name} Bracket is OPEN!**\n"
        f"\n"
        f"Registration is open for **30 minutes** ({emoji_time}).\n"
        f"Use `/clash` to enter your Zappy!\n"
        f"\n"
        f"Tonight's session has a **1.25× CP multiplier** on all wins. 🔥"
        if session == "evening" else
        f"⚡ **ZAPPY CLASH — {session_name} Bracket is OPEN!**\n"
        f"\n"
        f"Registration is open for **30 minutes** ({emoji_time}).\n"
        f"Use `/clash` to enter your Zappy!"
    )


async def close_and_resolve(channel: discord.TextChannel):
    """Close registration and run the full bracket."""
    global active_bracket_id, registration_open

    registration_open = False
    bracket_id = active_bracket_id

    entries = get_bracket_entries(bracket_id)
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

    # Seed bracket
    matchups = seed_bracket(entries)

    await channel.send(f"⚡ **BRACKET START** — {n} fighters, {len(matchups)} first-round matchups!\n")

    # Run all rounds
    current_round = matchups
    round_num = 1
    cp_multiplier = 1.25 if "evening" in bracket_id else 1.0

    while len(current_round) > 0:
        next_round = []
        round_label = {1: "Round of 16", 2: "Quarterfinals", 3: "Semifinals", 4: "FINAL"}.get(round_num, f"Round {round_num}")
        await channel.send(f"\n🔔 **{round_label}**\n")
        await asyncio.sleep(3)

        for matchup in current_round:
            player_a, player_b = matchup

            # Handle byes
            if player_b is None:
                await channel.send(f"🎯 **{player_a['discord_user_id']}** advances with a bye.")
                next_round.append(player_a)
                continue

            # Fetch Zappy data
            zappy_a = await fetch_zappy_traits(player_a["asset_id"])
            zappy_b = await fetch_zappy_traits(player_b["asset_id"])

            if not zappy_a or not zappy_b:
                await channel.send("⚠️ Couldn't load one fighter's stats — skipping this matchup.")
                continue

            fighter_a = build_fighter(zappy_a)
            fighter_b = build_fighter(zappy_b)

            # Run the battle
            result = resolve_battle(fighter_a, fighter_b)

            # ── Pre-fight embed: both Zappies side by side ──
            pre_embed = discord.Embed(
                title="⚡ BRACKET MATCH",
                color=0xF5E642,
            )
            pre_embed.add_field(
                name=fighter_a.display_name,
                value=f"⚡ VLT {fighter_a.VLT} · 🛡️ INS {fighter_a.INS} · 🎲 SPK {fighter_a.SPK}"
                      + (f"\n✨ {fighter_a.combo}" if fighter_a.combo else ""),
                inline=True,
            )
            pre_embed.add_field(name="vs.", value="⚡", inline=True)
            pre_embed.add_field(
                name=fighter_b.display_name,
                value=f"⚡ VLT {fighter_b.VLT} · 🛡️ INS {fighter_b.INS} · 🎲 SPK {fighter_b.SPK}"
                      + (f"\n✨ {fighter_b.combo}" if fighter_b.combo else ""),
                inline=True,
            )
            # Show both images — A as thumbnail, B as image
            if fighter_a.image_url:
                pre_embed.set_thumbnail(url=fighter_a.image_url)
            if fighter_b.image_url:
                pre_embed.set_image(url=fighter_b.image_url)
            await channel.send(embed=pre_embed)
            await asyncio.sleep(2)

            # ── Play-by-play text (skip the header lines, already in embed) ──
            log_lines = result["log"]
            # Skip first 6 lines (the stat header we already showed in embed)
            play_by_play = "\n".join(log_lines[6:])
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

            award_cp(winner_id, win_cp + upset_cp, f"bracket_win_{bracket_id}")
            award_cp(loser_id,  lose_cp,            f"bracket_loss_{bracket_id}")

            # Update streaks
            update_streak(winner_id, won=True)
            update_streak(loser_id,  won=False)

            # Save result
            save_battle_result(
                bracket_id=bracket_id,
                winner_discord_id=winner_id,
                loser_discord_id=loser_id,
                winner_asset_id=result["winner"].asset_id,
                loser_asset_id=result["loser"].asset_id,
                is_upset=result["is_upset"],
                round_num=round_num,
            )

            # ── Winner embed with image ──
            winner   = result["winner"]
            win_desc = f"💰 **+{win_cp + upset_cp} CP** → <@{winner_id}>"
            if result["is_upset"]:
                win_desc += f" *(+{upset_cp} upset bonus!)*"
            win_desc += f"\n💰 **+{lose_cp} CP** → <@{loser_id}>"

            win_embed = discord.Embed(
                title=f"🏆 {winner.display_name} wins!",
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

        current_round_pairs = []
        for i in range(0, len(next_round) - 1, 2):
            current_round_pairs.append((next_round[i], next_round[i+1]))
        if len(next_round) % 2 == 1:
            current_round_pairs.append((next_round[-1], None))

        current_round = current_round_pairs
        round_num += 1

        if len(next_round) == 1:
            # We have a bracket champion
            champion_id = next_round[0]["discord_user_id"]
            champ_asset = await fetch_zappy_traits(next_round[0]["asset_id"])
            champ_name = champ_asset["name"] if champ_asset else f"ASA {next_round[0]['asset_id']}"

            bonus_cp = int(CP_BRACKET_WIN * cp_multiplier)
            award_cp(champion_id, bonus_cp, f"bracket_champion_{bracket_id}")

            await channel.send(
                f"\n🏆 **BRACKET CHAMPION!**\n"
                f"<@{champion_id}> wins it all with **{champ_name}**!\n"
                f"💰 **+{bonus_cp} CP** bracket champion bonus!\n"
                f"\n"
                f"⚡ Use `/top` to see the updated leaderboard."
            )
            break

    active_bracket_id = None


# ─────────────────────────────────────────────
# Bot events
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"⚡ Zappy Clash bot online as {bot.user}")
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"✅ Slash commands synced to guild {GUILD_ID}")
    await tree.sync()
    print(f"✅ Slash commands synced globally")
    session_scheduler.start()
    print("⏰ Session scheduler running")


# ─────────────────────────────────────────────
# Run the bot
# ─────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(BOT_TOKEN)

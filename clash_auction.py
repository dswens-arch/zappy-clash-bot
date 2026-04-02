"""
clash_auction.py
────────────────
NFT auction functionality for Zappy Clash.

Drop this file in your project root alongside bot.py.

In bot.py, add near your other imports:
    from clash_auction import setup_auction_commands, auction_checker

In your on_ready event, add:
    setup_auction_commands(bot, tree, GUILD_ID)
    auction_checker.start()

Commands added:
    /auction create  — admin only
    /auction bid     — any holder
    /auction info    — anyone
    /auction history — anyone
    /auction close   — admin only (manual early close)

Background task:
    auction_checker — polls every 60s, auto-closes expired auctions
"""

import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, timezone
import os

# ─────────────────────────────────────────────
# Config — pulled from env, falls back to None
# ─────────────────────────────────────────────
ADMIN_ROLE_NAME  = os.environ.get("ADMIN_ROLE_NAME", "Admin")
AUCTION_CHANNEL  = int(os.environ.get("AUCTION_CHANNEL", 0)) or None  # Optional dedicated channel

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def get_db():
    from database import get_supabase
    return get_supabase()


def is_admin(interaction: discord.Interaction) -> bool:
    """Check if the user has the admin role."""
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.name == ADMIN_ROLE_NAME for r in interaction.user.roles)


def check_auction_channel(interaction: discord.Interaction) -> bool:
    """If AUCTION_CHANNEL is set, restrict to that channel."""
    if AUCTION_CHANNEL and interaction.channel_id != AUCTION_CHANNEL:
        return False
    return True


def format_time_remaining(ends_at_str: str) -> str:
    """Return a human-readable time remaining string."""
    ends_at = datetime.fromisoformat(ends_at_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    delta = ends_at - now
    if delta.total_seconds() <= 0:
        return "Ended"
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"


def get_active_auction(db):
    """Return the most recently created open auction, or None."""
    result = db.table("clash_auctions") \
        .select("*") \
        .eq("status", "open") \
        .order("created_at", desc=True) \
        .limit(1) \
        .execute()
    return result.data[0] if result.data else None


def get_highest_bid(db, auction_id: int) -> dict | None:
    """Return the highest bid row for an auction, or None."""
    result = db.table("clash_bids") \
        .select("*") \
        .eq("auction_id", auction_id) \
        .order("bid_amount", desc=True) \
        .limit(1) \
        .execute()
    return result.data[0] if result.data else None


def get_bid_history(db, auction_id: int, limit: int = 10) -> list:
    """Return the most recent bids for an auction, newest first."""
    result = db.table("clash_bids") \
        .select("*") \
        .eq("auction_id", auction_id) \
        .order("placed_at", desc=True) \
        .limit(limit) \
        .execute()
    return result.data or []


def build_auction_embed(auction: dict, db, closed: bool = False) -> discord.Embed:
    """Build the standard auction info embed."""
    top_bid = get_highest_bid(db, auction["id"])

    if closed:
        color = discord.Color.red()
        status_text = "🔴 Closed"
    else:
        color = discord.Color.gold()
        status_text = f"🟢 Live · {format_time_remaining(auction['ends_at'])} remaining"

    embed = discord.Embed(
        title=f"🏷️ Auction — {auction['nft_name']}",
        description=auction.get("description") or "",
        color=color,
    )

    if auction.get("image_url"):
        embed.set_thumbnail(url=auction["image_url"])

    if top_bid:
        embed.add_field(
            name="💰 Highest Bid",
            value=f"**{top_bid['bid_amount']:,} ZAPP**\nby **{top_bid['discord_username']}**",
            inline=True,
        )
    else:
        embed.add_field(
            name="💰 Highest Bid",
            value=f"No bids yet · Opening at **{auction['starting_bid']:,} ZAPP**",
            inline=True,
        )

    embed.add_field(
        name="⬆️ Min Increment",
        value=f"{auction['min_increment']:,} ZAPP",
        inline=True,
    )

    embed.add_field(name="Status", value=status_text, inline=False)

    if auction.get("asset_id"):
        embed.set_footer(text=f"ASA ID: {auction['asset_id']}")

    return embed


def build_history_embed(auction: dict, db) -> discord.Embed:
    """Build the bid history embed."""
    bids = get_bid_history(db, auction["id"], limit=15)

    embed = discord.Embed(
        title=f"📋 Bid History — {auction['nft_name']}",
        color=discord.Color.blurple(),
    )

    if not bids:
        embed.description = "No bids placed yet."
        return embed

    lines = []
    for i, bid in enumerate(bids):
        placed = datetime.fromisoformat(bid["placed_at"].replace("Z", "+00:00"))
        ts = f"<t:{int(placed.timestamp())}:R>"
        medal = "🥇" if i == 0 else "  "
        lines.append(f"{medal} **{bid['bid_amount']:,}** — {bid['discord_username']} · {ts}")

    embed.description = "\n".join(lines)
    return embed


async def close_auction(auction: dict, db, bot: discord.Client, channel_id: int | None = None):
    """
    Close an auction: mark it settled, record the winner, post the closing embed.
    Called by the background task or /auction close.
    """
    auction_id = auction["id"]
    top_bid = get_highest_bid(db, auction_id)

    update_data = {
        "status": "closed",
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }

    if top_bid:
        update_data["winner_user_id"]  = top_bid["discord_user_id"]
        update_data["winner_username"] = top_bid["discord_username"]
        update_data["winning_bid"]     = top_bid["bid_amount"]

    db.table("clash_auctions").update(update_data).eq("id", auction_id).execute()

    # Build closing embed
    embed = discord.Embed(
        title=f"🔔 Auction Closed — {auction['nft_name']}",
        color=discord.Color.red(),
    )

    if auction.get("image_url"):
        embed.set_thumbnail(url=auction["image_url"])

    if top_bid:
        embed.add_field(
            name="🏆 Winner",
            value=f"**{top_bid['discord_username']}**",
            inline=True,
        )
        embed.add_field(
            name="💰 Winning Bid",
            value=f"**{top_bid['bid_amount']:,} ZAPP**",
            inline=True,
        )
        embed.add_field(
            name="Next Steps",
            value="The admin will reach out to arrange NFT delivery. Congrats! ⚡",
            inline=False,
        )
    else:
        embed.description = "The auction ended with no bids."

    if auction.get("asset_id"):
        embed.set_footer(text=f"ASA ID: {auction['asset_id']}")

    # Post to channel
    target_channel_id = channel_id or AUCTION_CHANNEL
    if target_channel_id:
        try:
            channel = bot.get_channel(target_channel_id)
            if channel:
                await channel.send(embed=embed)
        except Exception as e:
            print(f"[Auction] Could not post close embed: {e}")


# ─────────────────────────────────────────────
# Background task — auto-close expired auctions
# ─────────────────────────────────────────────

_bot_ref = None  # set during setup

@tasks.loop(seconds=60)
async def auction_checker():
    """Poll every 60s for auctions that have expired and close them."""
    try:
        db = get_db()
        now = datetime.now(timezone.utc).isoformat()

        # Find all open auctions whose end time has passed
        result = db.table("clash_auctions") \
            .select("*") \
            .eq("status", "open") \
            .lte("ends_at", now) \
            .execute()

        for auction in (result.data or []):
            print(f"[Auction] Auto-closing auction {auction['id']} — {auction['nft_name']}")
            await close_auction(auction, db, _bot_ref)

    except Exception as e:
        print(f"[Auction] Checker error: {e}")


# ─────────────────────────────────────────────
# Command setup — call from bot.py on_ready
# ─────────────────────────────────────────────

def setup_auction_commands(bot: discord.Client, tree: app_commands.CommandTree, guild_id: int):
    """Register all /auction subcommands onto the provided command tree."""

    global _bot_ref
    _bot_ref = bot

    guild = discord.Object(id=guild_id)
    auction_group = app_commands.Group(name="auction", description="NFT auction commands")

    # ── /auction create ──────────────────────────────────────────────────────
    @auction_group.command(name="create", description="[Admin] Create a new NFT auction")
    @app_commands.describe(
        nft_name    = "Name of the NFT being auctioned",
        duration_hours = "How long the auction runs (in hours)",
        starting_bid   = "Minimum opening bid in ZAPP",
        min_increment  = "Minimum raise over current high bid",
        asset_id       = "Algorand ASA ID (optional, for your records)",
        image_url      = "Image URL for the embed thumbnail (optional)",
        description    = "Flavor text for the embed (optional)",
    )
    async def auction_create(
        interaction: discord.Interaction,
        nft_name: str,
        duration_hours: int,
        starting_bid: int,
        min_increment: int = 1,
        asset_id: str = None,
        image_url: str = None,
        description: str = None,
    ):
        await interaction.response.defer(ephemeral=True)

        if not is_admin(interaction):
            await interaction.followup.send("❌ Admin only.", ephemeral=True)
            return

        if duration_hours < 1:
            await interaction.followup.send("❌ Duration must be at least 1 hour.", ephemeral=True)
            return

        db = get_db()

        # Check for already-open auction
        existing = get_active_auction(db)
        if existing:
            await interaction.followup.send(
                f"❌ There's already an active auction for **{existing['nft_name']}**. "
                f"Close it first with `/auction close`.",
                ephemeral=True,
            )
            return

        from datetime import timedelta
        ends_at = (datetime.now(timezone.utc) + timedelta(hours=duration_hours)).isoformat()

        row = {
            "nft_name":      nft_name,
            "asset_id":      int(asset_id) if asset_id else None,
            "image_url":     image_url,
            "description":   description,
            "starting_bid":  starting_bid,
            "min_increment": min_increment,
            "status":        "open",
            "created_by":    str(interaction.user.id),
            "ends_at":       ends_at,
        }

        result = db.table("clash_auctions").insert(row).execute()
        auction = result.data[0]

        embed = build_auction_embed(auction, db)

        # Post publicly to auction channel (or current channel)
        target = interaction.channel
        if AUCTION_CHANNEL:
            ch = interaction.guild.get_channel(AUCTION_CHANNEL)
            if ch:
                target = ch

        await target.send(content="🏷️ **A new auction has started!**", embed=embed)
        await interaction.followup.send("✅ Auction created.", ephemeral=True)

    # ── /auction bid ─────────────────────────────────────────────────────────
    @auction_group.command(name="bid", description="Place a bid on the active auction")
    @app_commands.describe(amount="Your bid in ZAPP tokens")
    async def auction_bid(interaction: discord.Interaction, amount: int):
        await interaction.response.defer(ephemeral=True)

        if not check_auction_channel(interaction):
            await interaction.followup.send(
                f"❌ Use <#{AUCTION_CHANNEL}> for auction commands.", ephemeral=True
            )
            return

        db = get_db()
        auction = get_active_auction(db)

        if not auction:
            await interaction.followup.send("❌ No active auction right now.", ephemeral=True)
            return

        top_bid = get_highest_bid(db, auction["id"])

        # Validate bid amount
        if top_bid:
            min_bid = top_bid["bid_amount"] + auction["min_increment"]
            if amount < min_bid:
                await interaction.followup.send(
                    f"❌ Bid too low. Current high is **{top_bid['bid_amount']:,} ZAPP** "
                    f"— minimum next bid is **{min_bid:,} ZAPP**.",
                    ephemeral=True,
                )
                return
            # Can't outbid yourself
            if top_bid["discord_user_id"] == str(interaction.user.id):
                await interaction.followup.send(
                    "❌ You're already the highest bidder!", ephemeral=True
                )
                return
        else:
            if amount < auction["starting_bid"]:
                await interaction.followup.send(
                    f"❌ Opening bid must be at least **{auction['starting_bid']:,} ZAPP**.",
                    ephemeral=True,
                )
                return

        # Record the bid
        db.table("clash_bids").insert({
            "auction_id":       auction["id"],
            "discord_user_id":  str(interaction.user.id),
            "discord_username": interaction.user.display_name,
            "bid_amount":       amount,
        }).execute()

        # Notify previous high bidder (ephemeral DM-style — best effort)
        if top_bid and top_bid["discord_user_id"] != str(interaction.user.id):
            try:
                prev_user = await interaction.guild.fetch_member(int(top_bid["discord_user_id"]))
                if prev_user:
                    await prev_user.send(
                        f"⚡ You've been outbid on **{auction['nft_name']}**!\n"
                        f"New high bid: **{amount:,} ZAPP** by **{interaction.user.display_name}**\n"
                        f"Head back to place a new bid before the auction closes."
                    )
            except Exception:
                pass  # DMs may be closed — not a hard failure

        # Post public outbid announcement to channel
        target = interaction.channel
        if AUCTION_CHANNEL:
            ch = interaction.guild.get_channel(AUCTION_CHANNEL)
            if ch:
                target = ch

        public_embed = discord.Embed(
            title=f"💰 New High Bid — {auction['nft_name']}",
            description=(
                f"**{interaction.user.display_name}** has taken the lead!\n"
                f"Current high bid: **{amount:,} ZAPP**\n"
                f"⏱️ {format_time_remaining(auction['ends_at'])} remaining"
            ),
            color=discord.Color.gold(),
        )
        if auction.get("image_url"):
            public_embed.set_thumbnail(url=auction["image_url"])

        await target.send(embed=public_embed)
        await interaction.followup.send(
            f"✅ Bid of **{amount:,} ZAPP** placed. You're the high bidder!", ephemeral=True
        )

    # ── /auction info ─────────────────────────────────────────────────────────
    @auction_group.command(name="info", description="Show the current auction status")
    async def auction_info(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        db = get_db()
        auction = get_active_auction(db)

        if not auction:
            await interaction.followup.send("No active auction right now.")
            return

        embed = build_auction_embed(auction, db)
        await interaction.followup.send(embed=embed)

    # ── /auction history ──────────────────────────────────────────────────────
    @auction_group.command(name="history", description="Show bid history for the current auction")
    async def auction_history(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        db = get_db()
        auction = get_active_auction(db)

        if not auction:
            # Fall back to most recently closed auction
            result = db.table("clash_auctions") \
                .select("*") \
                .eq("status", "closed") \
                .order("closed_at", desc=True) \
                .limit(1) \
                .execute()
            if result.data:
                auction = result.data[0]
            else:
                await interaction.followup.send("No auction history yet.")
                return

        embed = build_history_embed(auction, db)
        await interaction.followup.send(embed=embed)

    # ── /auction close ────────────────────────────────────────────────────────
    @auction_group.command(name="close", description="[Admin] Manually close the active auction early")
    async def auction_close(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not is_admin(interaction):
            await interaction.followup.send("❌ Admin only.", ephemeral=True)
            return

        db = get_db()
        auction = get_active_auction(db)

        if not auction:
            await interaction.followup.send("❌ No active auction to close.", ephemeral=True)
            return

        await close_auction(auction, db, _bot_ref, interaction.channel_id)
        await interaction.followup.send(
            f"✅ Auction for **{auction['nft_name']}** has been closed.", ephemeral=True
        )

    # Register the group
    tree.add_command(auction_group, guild=guild)
    print("✅ Auction commands registered")

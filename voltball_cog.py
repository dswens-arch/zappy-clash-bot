"""
voltball_cog.py
----------------
Discord commands + weekly resolution job for Voltball.

Fully wired: wallet ownership/stats via algorand_lookup.py (through
voltball_lineup_service.py), team/season/standings via voltball_db.py
(get_supabase() from database.py), and the lineup picker UI via
voltball_lineup_view.py.

The one remaining stub is the weekly resolution job's schedule/pairing
logic (round-robin schedule generation) and the specific bot/guild
config for which day + channel to post to — those depend on decisions
(schedule format, announcement channel ID) I don't have, flagged below.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone

from voltball_engine import resolve_match, FORMATIONS, HERO_SIGNATURES
from voltball_lineup_service import (
    get_wallet_zappies, get_hero_ownership, get_locked_lineup_team, build_fallback_team, build_cpu_team, LineupValidationError,
)
from algorand_lookup import fetch_zappy_traits
from voltball_lineup_view import build_lineup_picker
from voltball_db import (
    get_active_season, get_upcoming_season, get_team_by_owner, get_team_by_id, get_teams_for_season,
    get_lineup, get_week_lineups, create_team, create_cpu_team, get_standings, update_standings_after_match,
    get_guild_config, set_guild_config, create_season, list_seasons, wipe_season,
)
from voltball_schedule import save_schedule, get_week_pairings, get_bye_team
from voltball_embeds import build_match_embed, build_standings_embed, build_lineups_embed, build_matchup_preview_embed
from voltball_position_fit import get_position_fit, rank_collection_for_position, label_for_held_zappy
from database import get_supabase, get_wallet


class VoltballCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weekly_resolution.start()

    def cog_unload(self):
        self.weekly_resolution.cancel()

    # ─────────────────────────────────────────────
    # /voltball_season_start (admin) — generates the full schedule ONCE
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_season_start", description="[Admin] Lock in the schedule and start the season.")
    @app_commands.checks.has_permissions(administrator=True)
    async def voltball_season_start(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        season = get_upcoming_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("There's no active season found to start for this guild.", ephemeral=True)
            return

        db = get_supabase()
        existing_schedule = db.table("voltball_schedule").select("id").eq("season_id", season["id"]).execute().data
        if existing_schedule:
            await interaction.followup.send("This season's schedule has already been generated — it isn't regenerated after teams start playing.", ephemeral=True)
            return

        teams = get_teams_for_season(season["id"])
        if len(teams) < 2:
            await interaction.followup.send(f"Only {len(teams)} team(s) registered — need at least 2 to build a schedule.", ephemeral=True)
            return

        team_ids = [t["id"] for t in teams]
        rows = save_schedule(season["id"], team_ids, season["week_count"])
        db.table("voltball_seasons").update({"status": "active", "current_week": 1}).eq("id", season["id"]).execute()

        await interaction.followup.send(
            f"🏈 Schedule locked in — {len(teams)} teams, {season['week_count']} weeks, {len(rows)} total matchups. Season is live.",
            ephemeral=True,
        )

    # ─────────────────────────────────────────────
    # /voltball_team_register
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_team_register", description="Register a Voltball team — requires holding a Hero or collab NFT.")
    @app_commands.describe(team_name="Your team's name", wallet_address="Your Algorand wallet address (defaults to your linked wallet if you have one)")
    async def voltball_team_register(self, interaction: discord.Interaction, team_name: str, wallet_address: str = None):
        await interaction.response.defer(ephemeral=True)

        if not wallet_address:
            wallet_address = get_wallet(str(interaction.user.id))
            if not wallet_address:
                await interaction.followup.send(
                    "No linked wallet found — provide a `wallet_address`, or link your wallet first.",
                    ephemeral=True,
                )
                return

        # Teams register during the "upcoming" window, before the schedule is generated.
        season = get_upcoming_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("There's no season currently open for registration.", ephemeral=True)
            return

        existing = get_team_by_owner(str(interaction.guild_id), str(interaction.user.id), season["id"])
        if existing:
            await interaction.followup.send(f"You already have a team this season: **{existing['team_name']}**.", ephemeral=True)
            return

        try:
            heroes = await get_hero_ownership(wallet_address)
        except LineupValidationError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return

        if not heroes:
            await interaction.followup.send(
                "No Hero or collab NFT found in that wallet — Hero ownership is required to coach a Voltball team.",
                ephemeral=True,
            )
            return

        if len(heroes) > 1:
            options = [discord.SelectOption(label=f"{h['hero_type']} (#{h['asset_id']})", value=str(h["asset_id"])) for h in heroes[:25]]
            view = _HeroPickView(options, heroes, team_name, wallet_address, str(interaction.guild_id), season["id"], interaction.user.id)
            await interaction.followup.send("You hold multiple Heroes — which one coaches this team?", view=view, ephemeral=True)
            return

        await self._register_team(interaction, team_name, wallet_address, heroes[0], str(interaction.guild_id), season["id"])

    async def _register_team(self, interaction: discord.Interaction, team_name: str, wallet_address: str,
                              hero: dict, guild_id: str, season_id: str):
        try:
            create_team(
                guild_id=guild_id,
                owner_discord_id=str(interaction.user.id),
                wallet_address=wallet_address,
                team_name=team_name,
                hero_asset_id=hero["asset_id"],
                hero_type=hero["hero_type"],
                is_collab_hero=hero.get("is_collab", False),
                season_id=season_id,
            )
        except Exception as e:
            # Most likely one of the UNIQUE constraints (dupe coach or dupe Hero this season)
            await interaction.followup.send(
                f"Couldn't register that team — {hero['hero_type']} may already be coaching a team this season, "
                f"or you already have one registered.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"🏈 **{team_name}** registered — coached by **{hero['hero_type']}**! "
            f"Use `/voltball_lineup` before the weekly deadline to set your formation.",
            ephemeral=True,
        )

    # ─────────────────────────────────────────────
    # /voltball_add_cpu_team (admin) — a standing solo-test opponent.
    # No wallet, no real coach: auto-fields a fresh random roster and
    # formation every time it's resolved, so someone with only one real
    # team can still test full matches/seasons.
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_add_cpu_team", description="[Admin] Add a CPU opponent for solo testing — fields a random roster weekly.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(team_name="Name for the CPU team", hero_type="Optional — pins its coach signature (random if omitted)")
    async def voltball_add_cpu_team(self, interaction: discord.Interaction, team_name: str, hero_type: str = None):
        await interaction.response.defer(ephemeral=True)

        season = get_upcoming_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("There's no season open for registration — create one first with `/voltball_season_create`.", ephemeral=True)
            return

        team = create_cpu_team(str(interaction.guild_id), season["id"], team_name, hero_type)
        await interaction.followup.send(
            f"🤖 CPU team **{team_name}** added — coached by **{team['hero_type']}**. "
            f"It doesn't need `/voltball_lineup` — it auto-fields a fresh random roster and formation every week.",
            ephemeral=True,
        )

    # ─────────────────────────────────────────────
    # /voltball_lineup
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_lineup", description="Set your Voltball formation and position assignments for this week.")
    @app_commands.describe(formation="OFFENSE / BALANCED / DEFENSE")
    @app_commands.choices(formation=[
        app_commands.Choice(name="Offense (5 Striker / 1 Mid / 1 Guard)", value="OFFENSE"),
        app_commands.Choice(name="Balanced (3 Striker / 2 Mid / 2 Guard)", value="BALANCED"),
        app_commands.Choice(name="Defense (1 Striker / 2 Mid / 4 Guard)", value="DEFENSE"),
    ])
    async def voltball_lineup(self, interaction: discord.Interaction, formation: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        season = get_active_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("There's no active Voltball season right now.", ephemeral=True)
            return

        team_row = get_team_by_owner(str(interaction.guild_id), str(interaction.user.id), season["id"])
        if not team_row:
            await interaction.followup.send("You don't have a registered Voltball team yet — use `/voltball_team_register` first.", ephemeral=True)
            return

        try:
            held = await get_wallet_zappies(team_row["wallet_address"])
        except LineupValidationError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return

        if len(held) < 7:
            await interaction.followup.send(
                f"You need at least 7 Zappies in your wallet to field a Voltball roster — you currently hold {len(held)}.",
                ephemeral=True,
            )
            return

        content, view = build_lineup_picker(
            guild_id=str(interaction.guild_id),
            team_id=team_row["id"],
            season_id=season["id"],
            week_number=season["current_week"],
            formation=formation.value,
            wallet_address=team_row["wallet_address"],
            held_zappies=held,
            user_id=interaction.user.id,
        )
        await interaction.followup.send(content, view=view, ephemeral=True)

    # ─────────────────────────────────────────────
    # ─────────────────────────────────────────────
    # /voltball_config (admin)
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_config", description="[Admin] Set the announcement channel and/or resolution weekday.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(channel="Where match results and standings get posted", weekday="Day matches resolve (0=Monday ... 6=Sunday)")
    async def voltball_config(self, interaction: discord.Interaction, channel: discord.TextChannel = None, weekday: int = None):
        await interaction.response.defer(ephemeral=True)

        if weekday is not None and not (0 <= weekday <= 6):
            await interaction.followup.send("Weekday must be 0 (Monday) through 6 (Sunday).", ephemeral=True)
            return

        row = set_guild_config(
            str(interaction.guild_id),
            announcement_channel_id=str(channel.id) if channel else None,
            resolution_weekday=weekday,
        )
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        parts = []
        if channel:
            parts.append(f"announcements → {channel.mention}")
        if weekday is not None:
            parts.append(f"resolution day → {day_names[weekday]}")
        await interaction.followup.send(f"✅ Voltball config updated: {', '.join(parts) if parts else 'no changes'}.", ephemeral=True)

    # ─────────────────────────────────────────────
    # /voltball_season_create (admin)
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_season_create", description="[Admin] Create a new Voltball season (teams can register once this exists).")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(name="Season name, e.g. 'Voltball Season 1' or 'Test Season'",
                            week_count="How many regular-season weeks",
                            is_test="Test seasons are meant to be wiped with /voltball_season_wipe — not a permanent record")
    async def voltball_season_create(self, interaction: discord.Interaction, name: str, week_count: int = 16, is_test: bool = False):
        await interaction.response.defer(ephemeral=True)

        existing = get_upcoming_season(str(interaction.guild_id)) or get_active_season(str(interaction.guild_id))
        if existing:
            await interaction.followup.send(
                f"There's already a season in progress: **{existing['name']}** ({existing['status']}). "
                f"Wipe it with `/voltball_season_wipe` first if you want to start fresh.",
                ephemeral=True,
            )
            return

        season = create_season(str(interaction.guild_id), name, week_count, is_test=is_test)
        test_note = " (marked as a **test season** — wipeable, not a permanent record)" if is_test else ""
        await interaction.followup.send(
            f"🏈 Season **{name}** created{test_note}. Teams can now register with `/voltball_team_register`. "
            f"Run `/voltball_season_start` once everyone's in to lock the schedule and begin.",
            ephemeral=True,
        )

    # ─────────────────────────────────────────────
    # /voltball_resolve_week (admin) — manual trigger, bypasses the
    # weekday gate. Same underlying logic as the scheduled job, so a
    # test season plays out exactly like a real one would, on demand.
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_resolve_week", description="[Admin] Manually resolve the current week right now (for testing, or to override the schedule).")
    @app_commands.checks.has_permissions(administrator=True)
    async def voltball_resolve_week(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        season = get_active_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("No active season to resolve — run `/voltball_season_start` first.", ephemeral=True)
            return

        week_before = season["current_week"]
        config = get_guild_config(str(interaction.guild_id))
        await self._resolve_season_week(season, config)

        note = "" if config["announcement_channel_id"] else " (no announcement channel configured — results saved but nothing posted; set one with `/voltball_config` if you want to see it play out)"
        await interaction.followup.send(f"✅ Week {week_before} resolved.{note}", ephemeral=True)

    # ─────────────────────────────────────────────
    # /voltball_season_wipe (admin) — destructive, requires confirmation
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_season_wipe", description="[Admin] Permanently delete a season and everything tied to it (teams, lineups, matches, standings).")
    @app_commands.checks.has_permissions(administrator=True)
    async def voltball_season_wipe(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        seasons = list_seasons(str(interaction.guild_id))
        if not seasons:
            await interaction.followup.send("No seasons found for this server.", ephemeral=True)
            return

        options = [
            discord.SelectOption(
                label=f"{s['name']} ({s['status']}, week {s['current_week']}/{s['week_count']}){' [TEST]' if s.get('is_test') else ''}",
                value=s["id"],
            )
            for s in seasons[:25]
        ]
        view = _SeasonWipeConfirmView(options, interaction.user.id)
        await interaction.followup.send(
            "⚠️ This permanently deletes the season and every team, lineup, match, and standing tied to it. Choose which one:",
            view=view, ephemeral=True,
        )

    # ─────────────────────────────────────────────
    # /voltball_zappy_lookup — check any ASA by number, owned or not
    # (marketplace research: see a listing's fit before buying)
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_zappy_lookup", description="Look up any Zappy by its ASA number — see its stats and position fit, owned or not.")
    @app_commands.describe(asa="The Zappy's ASA (asset) number")
    async def voltball_zappy_lookup(self, interaction: discord.Interaction, asa: int):
        await interaction.response.defer()  # public — this is marketplace research

        result = await fetch_zappy_traits(asa)
        if result is None:
            await interaction.followup.send(f"ASA `{asa}` isn't in the Zappy collection — double-check the number.")
            return

        if result.get("is_hero"):
            hero_type = result["hero_type"]
            sig = HERO_SIGNATURES.get(hero_type)
            embed = discord.Embed(
                title=f"🦸 {result['name']} (#{asa})",
                description=f"This is a **Hero** — a coach, not a roster Zappy, so it doesn't fill a Striker/Mid/Guard slot.",
                color=discord.Color.gold(),
            )
            s = result["stats"]
            embed.add_field(name="Base Stats", value=f"VLT {s['VLT']} · INS {s['INS']} · SPK {s['SPK']}", inline=False)
            if sig:
                embed.add_field(name="Voltball Coach Signature", value=f"**{sig['label']}** — {sig['desc']}", inline=False)
            else:
                embed.add_field(name="Voltball Coach Signature", value="*Not yet assigned — this Hero has no coach bonus configured.*", inline=False)
            await interaction.followup.send(embed=embed)
            return

        if result.get("is_collab"):
            embed = discord.Embed(
                title=f"🦸 {result['name']} (#{asa})",
                description="This is the **collab coach** asset — a coach, not a roster Zappy.",
                color=discord.Color.gold(),
            )
            s = result["stats"]
            embed.add_field(name="Base Stats", value=f"VLT {s['VLT']} · INS {s['INS']} · SPK {s['SPK']}", inline=False)
            await interaction.followup.send(embed=embed)
            return

        s = result["stats"]
        fit = get_position_fit(s["VLT"], s["INS"], s["SPK"])
        lines = [
            f"**{pos}** ({info['stat']} {info['value']}) — {info['tier']}, {info['percentile']}th percentile"
            for pos, info in fit["positions"].items()
        ]
        embed = discord.Embed(
            title=f"{result['name']} (#{asa})",
            description=f"Best fit: **{fit['positions'][fit['best_position']]['tier']} {fit['best_position']}**\n\n" + "\n".join(lines),
            color=discord.Color.blurple(),
        )
        if result.get("image_url"):
            embed.set_thumbnail(url=result["image_url"])
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────
    # /voltball_scout — browse the full collection by position, public
    # (marketplace research, not tied to what you hold)
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_scout", description="See the best Zappies collection-wide for a position — useful before buying.")
    @app_commands.describe(position="Which position to rank Zappies for")
    @app_commands.choices(position=[
        app_commands.Choice(name="Striker (VLT offense)", value="Striker"),
        app_commands.Choice(name="Mid (SPK playmaking)", value="Mid"),
        app_commands.Choice(name="Guard (INS defense)", value="Guard"),
    ])
    async def voltball_scout(self, interaction: discord.Interaction, position: app_commands.Choice[str]):
        await interaction.response.defer()  # public — this is marketplace research, not personal info

        top = rank_collection_for_position(position.value, top_n=15)
        stat_key = {"Striker": "VLT", "Mid": "SPK", "Guard": "INS"}[position.value]

        lines = [f"{i+1}. **{z['name']}** (#{z['asset_id']}) — {z[stat_key]} ({z['percentile']}th percentile)" for i, z in enumerate(top)]
        embed = discord.Embed(
            title=f"🔍 Top Zappies for {position.value}",
            description="\n".join(lines) + "\n\n*Cross-reference these asset IDs on your marketplace of choice.*",
            color=discord.Color.teal(),
        )
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────
    # /voltball_my_zappies — personal scouting report for held Zappies
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_my_zappies", description="See your held Zappies ranked by position fit — helps you set your lineup.")
    @app_commands.describe(wallet_address="Wallet to check (defaults to your registered team's wallet if you have one)")
    async def voltball_my_zappies(self, interaction: discord.Interaction, wallet_address: str = None):
        await interaction.response.defer(ephemeral=True)

        if not wallet_address:
            season = get_active_season(str(interaction.guild_id)) or get_upcoming_season(str(interaction.guild_id))
            team_row = get_team_by_owner(str(interaction.guild_id), str(interaction.user.id), season["id"]) if season else None
            if team_row:
                wallet_address = team_row["wallet_address"]
            else:
                wallet_address = get_wallet(str(interaction.user.id))
            if not wallet_address:
                await interaction.followup.send("No linked wallet or registered team found — provide a `wallet_address`.", ephemeral=True)
                return

        try:
            held = await get_wallet_zappies(wallet_address)
        except LineupValidationError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return

        if not held:
            await interaction.followup.send("No roster-eligible Zappies found in that wallet.", ephemeral=True)
            return

        lines = []
        for z in held[:25]:  # embed field/description length limits — full list beyond 25 needs pagination if it comes up
            fit = get_position_fit(z["VLT"], z["INS"], z["SPK"])
            best = fit["positions"][fit["best_position"]]
            lines.append(f"**{z['name']}** — VLT {z['VLT']} · INS {z['INS']} · SPK {z['SPK']} → *{best['tier']} {fit['best_position']}*")

        embed = discord.Embed(
            title=f"📋 Your Zappies ({len(held)} held)",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        if len(held) > 25:
            embed.set_footer(text=f"Showing first 25 of {len(held)} — full list not yet paginated here.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────
    # /voltball_lineups — public scouting report
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_lineups", description="See every team's formation and roster for the current week.")
    async def voltball_lineups(self, interaction: discord.Interaction):
        await interaction.response.defer()  # NOT ephemeral — this is public by design

        season = get_active_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("There's no active Voltball season right now.")
            return

        week_lineups = get_week_lineups(season["id"], season["current_week"])
        embed = build_lineups_embed(season, season["current_week"], week_lineups)
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────
    # /voltball_standings
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_standings", description="View the current Voltball season standings.")
    async def voltball_standings(self, interaction: discord.Interaction):
        await interaction.response.defer()

        season = get_active_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("There's no active Voltball season right now.")
            return

        rows = get_standings(season["id"])
        embed = build_standings_embed(season, rows)
        await interaction.followup.send(embed=embed)

    # ─────────────────────────────────────────────
    # Weekly resolution job — thin wrapper around _resolve_season_week,
    # which is also callable directly by /voltball_resolve_week for
    # testing (bypasses the weekday gate, resolves right now).
    # ─────────────────────────────────────────────
    @tasks.loop(hours=24)
    async def weekly_resolution(self):
        """Runs daily; only actually resolves on each guild's configured weekly deadline day."""
        now = datetime.now(timezone.utc)
        db = get_supabase()
        seasons = db.table("voltball_seasons").select("*").eq("status", "active").execute().data or []

        for season in seasons:
            config = get_guild_config(season["guild_id"])
            if now.weekday() != config["resolution_weekday"]:
                continue
            await self._resolve_season_week(season, config)

    async def _resolve_season_week(self, season: dict, config: dict):
        """
        Resolves the current week for one season: posts the matchup
        preview, scores every pairing (auto-fielding no-lineup teams with
        the penalty, handling true forfeits separately), updates
        standings, and posts match + standings embeds if a channel is
        configured. Shared by the scheduled loop and the manual
        /voltball_resolve_week admin command — same logic either way.
        """
        db = get_supabase()
        week = season["current_week"]
        pairings = get_week_pairings(season["id"], week)
        channel = None
        if config["announcement_channel_id"]:
            channel = self.bot.get_channel(int(config["announcement_channel_id"]))

        if channel and pairings:
            teams = get_teams_for_season(season["id"])
            team_lookup = {t["id"]: t for t in teams}
            week_lineups = get_week_lineups(season["id"], week)
            lineup_lookup = {e["team_id"]: e["lineup"] for e in week_lineups}
            preview_embed = build_matchup_preview_embed(season, week, pairings, team_lookup, lineup_lookup)
            await channel.send(embed=preview_embed)

        for pairing in pairings:
            team_a_row = get_team_by_id(pairing["team_a_id"])
            team_b_row = get_team_by_id(pairing["team_b_id"])

            try:
                if team_a_row.get("is_cpu"):
                    team_a = build_cpu_team(team_a_row["hero_type"])
                else:
                    lineup_a = get_lineup(pairing["team_a_id"], week)
                    if lineup_a:
                        team_a = await get_locked_lineup_team(lineup_a, team_a_row["hero_type"], team_a_row["wallet_address"])
                    else:
                        team_a = await build_fallback_team(team_a_row["wallet_address"], team_a_row["hero_type"])

                if team_b_row.get("is_cpu"):
                    team_b = build_cpu_team(team_b_row["hero_type"])
                else:
                    lineup_b = get_lineup(pairing["team_b_id"], week)
                    if lineup_b:
                        team_b = await get_locked_lineup_team(lineup_b, team_b_row["hero_type"], team_b_row["wallet_address"])
                    else:
                        team_b = await build_fallback_team(team_b_row["wallet_address"], team_b_row["hero_type"])
            except LineupValidationError as e:
                print(f"[voltball] Week {week}: error building teams for {team_a_row['team_name']} vs {team_b_row['team_name']}: {e}")
                continue

            if team_a is None and team_b is None:
                print(f"[voltball] Week {week}: {team_a_row['team_name']} vs {team_b_row['team_name']} — both sides forfeit (fewer than 7 Zappies held), no match recorded.")
                continue
            if team_a is None:
                print(f"[voltball] Week {week}: {team_a_row['team_name']} forfeits (fewer than 7 Zappies held) — {team_b_row['team_name']} advances, no match recorded.")
                update_standings_after_match(season["id"], team_b_row["id"], team_a_row["id"], 0, 0)
                continue
            if team_b is None:
                print(f"[voltball] Week {week}: {team_b_row['team_name']} forfeits (fewer than 7 Zappies held) — {team_a_row['team_name']} advances, no match recorded.")
                update_standings_after_match(season["id"], team_a_row["id"], team_b_row["id"], 0, 0)
                continue

            team_a.name = team_a_row["team_name"]
            team_b.name = team_b_row["team_name"]

            result = resolve_match(team_a, team_b)

            winner_id = team_a_row["id"] if result["winner"] == team_a.name else team_b_row["id"]
            loser_id = team_b_row["id"] if winner_id == team_a_row["id"] else team_a_row["id"]

            db.table("voltball_matches").insert({
                "season_id": season["id"],
                "week_number": week,
                "team_a_id": team_a_row["id"],
                "team_b_id": team_b_row["id"],
                "team_a_score": result["score_a"],
                "team_b_score": result["score_b"],
                "winner_team_id": winner_id,
                "log_text": result["log_text"],
            }).execute()

            update_standings_after_match(
                season["id"], winner_id, loser_id,
                max(result["score_a"], result["score_b"]), min(result["score_a"], result["score_b"]),
            )

            if channel:
                embed = build_match_embed(result, week, is_playoff=season["status"] == "playoffs")
                await channel.send(embed=embed)

        bye_team_id = get_bye_team(season["id"], week)
        if bye_team_id:
            print(f"[voltball] Week {week}: {bye_team_id} has the bye.")

        new_week = week + 1
        new_status = "playoffs" if new_week > season["week_count"] else "active"
        db.table("voltball_seasons").update({"current_week": new_week, "status": new_status}).eq("id", season["id"]).execute()

        if channel:
            updated_season = {**season, "current_week": new_week, "status": new_status}
            standings_rows = get_standings(season["id"])
            standings_embed = build_standings_embed(updated_season, standings_rows)
            await channel.send(embed=standings_embed)

    @weekly_resolution.before_loop
    async def before_weekly_resolution(self):
        await self.bot.wait_until_ready()


class _SeasonWipeConfirmView(discord.ui.View):
    """Two-step confirmation for /voltball_season_wipe — select a season, then confirm the delete explicitly."""
    def __init__(self, options, user_id):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.selected_season_id = None
        self.selected_label = None
        self.select = discord.ui.Select(placeholder="Choose a season to wipe", options=options)
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your confirmation to make.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        self.selected_season_id = self.select.values[0]
        self.selected_label = next(o.label for o in self.select.options if o.value == self.selected_season_id)
        self.clear_items()
        confirm_btn = discord.ui.Button(label=f"Permanently delete '{self.selected_label}'", style=discord.ButtonStyle.danger)
        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm_btn.callback = self._on_confirm
        cancel_btn.callback = self._on_cancel
        self.add_item(confirm_btn)
        self.add_item(cancel_btn)
        await interaction.response.edit_message(content=f"⚠️ Confirm: permanently delete **{self.selected_label}** and all its data?", view=self)

    async def _on_confirm(self, interaction: discord.Interaction):
        wipe_season(self.selected_season_id)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=f"🗑️ **{self.selected_label}** and all associated data deleted.", view=self)

    async def _on_cancel(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Cancelled — nothing was deleted.", view=self)


class _HeroPickView(discord.ui.View):
    """Shown when a coach holds multiple Heroes — lets them pick which one coaches this team."""
    def __init__(self, options, heroes, team_name, wallet_address, guild_id, season_id, user_id):
        super().__init__(timeout=120)
        self.heroes_by_id = {str(h["asset_id"]): h for h in heroes}
        self.team_name = team_name
        self.wallet_address = wallet_address
        self.guild_id = guild_id
        self.season_id = season_id
        self.user_id = user_id
        self.select = discord.ui.Select(placeholder="Choose your coach", options=options)
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your team registration.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        hero = self.heroes_by_id[self.select.values[0]]

        try:
            create_team(
                guild_id=self.guild_id,
                owner_discord_id=str(interaction.user.id),
                wallet_address=self.wallet_address,
                team_name=self.team_name,
                hero_asset_id=hero["asset_id"],
                hero_type=hero["hero_type"],
                is_collab_hero=hero.get("is_collab", False),
                season_id=self.season_id,
            )
        except Exception:
            await interaction.followup.send(
                f"Couldn't register that team — {hero['hero_type']} may already be coaching a team this season.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"🏈 **{self.team_name}** registered — coached by **{hero['hero_type']}**!",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(VoltballCog(bot))

"""
voltball_cog.py
----------------
Discord commands + weekly resolution job for Voltball.

Team registration and weekly lineup-setting are website-only now
(register-team.ts / submit-lineup.ts + lineup.html) — /voltball_team_register
and /voltball_lineup were removed from here once the site's equivalents
were verified working end to end, including Hero-optional registration,
the Tempo dial, and QB. voltball_lineup_view.py (the old interactive
picker UI) is now unused by this file — left in place, not deleted,
since nothing currently requires removing it.

Fully wired: wallet ownership/stats via algorand_lookup.py (through
voltball_lineup_service.py), team/season/standings via voltball_db.py
(get_supabase() from database.py).

The one remaining stub is the weekly resolution job's schedule/pairing
logic (round-robin schedule generation) and the specific bot/guild
config for which day + channel to post to — those depend on decisions
(schedule format, announcement channel ID) I don't have, flagged below.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone

from voltball_engine import resolve_match, HERO_SIGNATURES
from voltball_lineup_service import (
    get_wallet_zappies, get_locked_lineup_team, build_fallback_team, build_cpu_team, LineupValidationError,
)
from algorand_lookup import fetch_zappy_traits
from voltball_db import (
    get_active_or_playoff_season, get_upcoming_season, get_open_season, get_team_by_owner, get_team_by_id, get_teams_for_season,
    get_lineup, get_week_lineups, create_cpu_team, get_standings, get_playoff_round_winners, update_standings_after_match, record_injuries,
    get_guild_config, set_guild_config, create_season, list_seasons, wipe_season, save_season_zappy_stats,
)
from voltball_schedule import save_schedule, save_playoff_round, get_week_pairings, get_bye_team
from voltball_season_stats import allocate_season_stats
from voltball_rarity import get_rarity_tier
from voltball_embeds import build_match_embed, build_standings_embed, build_champion_embed, build_lineups_embed, build_matchup_preview_embed
from voltball_position_fit import get_position_fit, rank_collection_for_position, label_for_held_zappy
from database import get_supabase, get_wallet


def _lineup_snapshot(team) -> dict:
    """
    Captures exactly what a team played this match -- formation, tempo,
    and full roster with stats -- straight from the resolved Team
    object. Used to populate voltball_matches.team_a_lineup/
    team_b_lineup for the site's Results/recap page. Deliberately NOT
    derived from voltball_lineups after the fact -- see the schema
    comment on why (CPU teams never write a row there at all, and an
    auto-filled no-lineup-penalty team played something different from
    whatever they may have submitted).
    """
    return {
        "formation": team.formation,
        "tempo": team.tempo,
        "assignments": {
            pos: [{"asset_id": z.asset_id, "name": z.name, "VLT": z.VLT, "INS": z.INS, "SPK": z.SPK} for z in players]
            for pos, players in team.assignments.items()
        },
    }


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

        # Season-wide stat allocation -- see voltball_season_stats.py for
        # the full design. Runs once here, for the ENTIRE real collection
        # (not just held/roster-eligible Zappies), same one-shot timing as
        # the schedule itself. ~1,678 Zappies -- a real but bounded amount
        # of work, acceptable to run synchronously here since this is a
        # once-per-season operation, not a hot path.
        allocations = allocate_season_stats()
        save_season_zappy_stats(season["id"], allocations)

        db.table("voltball_seasons").update({"status": "active", "current_week": 1}).eq("id", season["id"]).execute()

        await interaction.followup.send(
            f"🏈 Schedule locked in — {len(teams)} teams, {season['week_count']} weeks, {len(rows)} total matchups. "
            f"Season stats allocated for all {len(allocations)} Zappies. Season is live.",
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

        season = get_open_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("There's no season to add a CPU team to — create one first with `/voltball_season_create`.", ephemeral=True)
            return

        team = create_cpu_team(str(interaction.guild_id), season["id"], team_name, hero_type)

        if season["status"] == "upcoming":
            schedule_note = ""
        else:
            # The schedule is generated ONCE by /voltball_season_start, from
            # whoever was registered at that moment — a team added after that
            # point isn't retroactively inserted into voltball_schedule, so
            # it won't get an actual weekly pairing until the next season.
            schedule_note = (
                "\n⚠️ This season is already **" + season["status"] + "** — the schedule was locked in at "
                "`/voltball_season_start` and isn't regenerated. This CPU team is registered, but it won't "
                "show up in `/voltball_resolve_week` pairings this season."
            )

        await interaction.followup.send(
            f"🤖 CPU team **{team_name}** added — coached by **{team['hero_type']}**. "
            f"It doesn't need a lineup set on the site — it auto-fields a fresh random roster and formation every week."
            f"{schedule_note}",
            ephemeral=True,
        )

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

        existing = get_upcoming_season(str(interaction.guild_id)) or get_active_or_playoff_season(str(interaction.guild_id))
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
            f"🏈 Season **{name}** created{test_note}. Teams can now register on the site. "
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

        season = get_active_or_playoff_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("No active or playoff season to resolve — run `/voltball_season_start` first.", ephemeral=True)
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
                description=f"This is a **Hero** — a coach, not a roster Zappy, so it doesn't fill a QB/Striker/Mid/Guard slot.",
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
        try:
            tier = get_rarity_tier(asa)
            tier_label = {1: "Tier 1 (rarest ~10%)", 2: "Tier 2 (mid ~30%)", 3: "Tier 3 (common ~60%)"}[tier]
            lines.append(f"\n**Rarity:** {tier_label}")
        except KeyError:
            pass  # not in the roster-eligible collection scope get_rarity_tier covers -- shouldn't happen for a real roster Zappy, but don't fail the whole lookup over it
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
        app_commands.Choice(name="QB (SPK multiplier)", value="QB"),
        app_commands.Choice(name="Striker (VLT offense)", value="Striker"),
        app_commands.Choice(name="Mid (SPK playmaking)", value="Mid"),
        app_commands.Choice(name="Guard (INS defense)", value="Guard"),
    ])
    async def voltball_scout(self, interaction: discord.Interaction, position: app_commands.Choice[str]):
        await interaction.response.defer()  # public — this is marketplace research, not personal info

        top = rank_collection_for_position(position.value, top_n=15)
        stat_key = {"QB": "SPK", "Striker": "VLT", "Mid": "SPK", "Guard": "INS"}[position.value]
        tier_short = {1: "T1", 2: "T2", 3: "T3"}

        lines = []
        for i, z in enumerate(top):
            try:
                tier_tag = f" · {tier_short[get_rarity_tier(z['asset_id'])]}"
            except KeyError:
                tier_tag = ""
            lines.append(f"{i+1}. **{z['name']}** (#{z['asset_id']}) — {z[stat_key]} ({z['percentile']}th percentile){tier_tag}")
        embed = discord.Embed(
            title=f"🔍 Top Zappies for {position.value}",
            description="\n".join(lines) + "\n\n*T1/T2/T3 = rarity tier (T1 rarest ~10%). Cross-reference these asset IDs on your marketplace of choice.*",
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

        season = get_active_or_playoff_season(str(interaction.guild_id)) or get_upcoming_season(str(interaction.guild_id))

        if not wallet_address:
            team_row = get_team_by_owner(str(interaction.guild_id), str(interaction.user.id), season["id"]) if season else None
            if team_row:
                wallet_address = team_row["wallet_address"]
            else:
                wallet_address = get_wallet(str(interaction.user.id))
            if not wallet_address:
                await interaction.followup.send("No linked wallet or registered team found — provide a `wallet_address`.", ephemeral=True)
                return

        # season stats only apply once a season has actually started (active/playoffs) --
        # an "upcoming" season hasn't run allocation yet (that happens at /voltball_season_start).
        season_id = season["id"] if season and season["status"] in ("active", "playoffs") else None

        try:
            held = await get_wallet_zappies(wallet_address, season_id=season_id)
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

        season = get_active_or_playoff_season(str(interaction.guild_id))
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

        season = get_active_or_playoff_season(str(interaction.guild_id))
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
        seasons = db.table("voltball_seasons").select("*").in_("status", ["active", "playoffs"]).execute().data or []

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

        Also drives the playoff state machine: once the regular season's
        last week resolves, seeds a top-4 bracket (1v4 / 2v3) from
        standings; once the semifinal week resolves, seeds the
        championship from the two winners; once the championship
        resolves, marks the season complete. See week_count math below
        — playoff weeks are always week_count+1 (semis) and week_count+2
        (final), never regenerated, never overlapping regular-season
        week numbers.
        """
        db = get_supabase()
        week = season["current_week"]
        week_count = season["week_count"]
        is_playoff_week = season["status"] == "playoffs"
        round_label = None
        if is_playoff_week:
            round_label = "Semifinal" if week == week_count + 1 else "Championship"

        pairings = get_week_pairings(season["id"], week)
        channel = None
        if config["announcement_channel_id"]:
            channel = self.bot.get_channel(int(config["announcement_channel_id"]))

        if channel and pairings:
            teams = get_teams_for_season(season["id"])
            team_lookup = {t["id"]: t for t in teams}
            week_lineups = get_week_lineups(season["id"], week)
            lineup_lookup = {e["team_id"]: e["lineup"] for e in week_lineups}
            preview_embed = build_matchup_preview_embed(season, week, pairings, team_lookup, lineup_lookup, round_label=round_label)
            await channel.send(embed=preview_embed)

        is_championship_week = is_playoff_week and week == week_count + 2
        champion_name = None  # captured below if this is the championship and it resolves cleanly

        # Duplicate-match guard: if a bot restart (e.g. mid-deploy)
        # interrupted a previous resolution attempt partway through this
        # exact week, some pairings may already have a recorded match --
        # re-resolving them here would double-count standings for both
        # teams. Fetched once up front rather than once per pairing.
        already_resolved_rows = (
            db.table("voltball_matches")
            .select("team_a_id, team_b_id")
            .eq("season_id", season["id"])
            .eq("week_number", week)
            .execute()
            .data
        ) or []
        already_resolved = {(m["team_a_id"], m["team_b_id"]) for m in already_resolved_rows}

        for pairing in pairings:
            if (pairing["team_a_id"], pairing["team_b_id"]) in already_resolved:
                print(f"[voltball] Week {week}: {pairing['team_a_id']} vs {pairing['team_b_id']} already has a recorded match this week — skipping (duplicate-match guard, likely an interrupted prior resolution).")
                continue

            team_a_row = get_team_by_id(pairing["team_a_id"])
            team_b_row = get_team_by_id(pairing["team_b_id"])

            try:
                if team_a_row.get("is_cpu"):
                    team_a = build_cpu_team(team_a_row["hero_type"], season_id=season["id"])
                else:
                    lineup_a = get_lineup(pairing["team_a_id"], week)
                    if lineup_a:
                        team_a = await get_locked_lineup_team(lineup_a, team_a_row["hero_type"], team_a_row["wallet_address"], season_id=season["id"])
                    else:
                        team_a = await build_fallback_team(team_a_row["wallet_address"], team_a_row["hero_type"], team_id=team_a_row["id"], week_number=week, season_id=season["id"])

                if team_b_row.get("is_cpu"):
                    team_b = build_cpu_team(team_b_row["hero_type"], season_id=season["id"])
                else:
                    lineup_b = get_lineup(pairing["team_b_id"], week)
                    if lineup_b:
                        team_b = await get_locked_lineup_team(lineup_b, team_b_row["hero_type"], team_b_row["wallet_address"], season_id=season["id"])
                    else:
                        team_b = await build_fallback_team(team_b_row["wallet_address"], team_b_row["hero_type"], team_id=team_b_row["id"], week_number=week, season_id=season["id"])
            except LineupValidationError as e:
                print(f"[voltball] Week {week}: error building teams for {team_a_row['team_name']} vs {team_b_row['team_name']}: {e}")
                continue

            if team_a is None and team_b is None:
                print(f"[voltball] Week {week}: {team_a_row['team_name']} vs {team_b_row['team_name']} — both sides forfeit (fewer than 8 Zappies held), no match recorded.")
                continue
            if team_a is None:
                print(f"[voltball] Week {week}: {team_a_row['team_name']} forfeits (fewer than 8 Zappies held) — {team_b_row['team_name']} advances, no match recorded.")
                update_standings_after_match(season["id"], team_b_row["id"], team_a_row["id"], 0, 0)
                if is_championship_week:
                    champion_name = team_b_row["team_name"]
                continue
            if team_b is None:
                print(f"[voltball] Week {week}: {team_b_row['team_name']} forfeits (fewer than 8 Zappies held) — {team_a_row['team_name']} advances, no match recorded.")
                update_standings_after_match(season["id"], team_a_row["id"], team_b_row["id"], 0, 0)
                if is_championship_week:
                    champion_name = team_a_row["team_name"]
                continue

            team_a.name = team_a_row["team_name"]
            team_b.name = team_b_row["team_name"]

            result = resolve_match(team_a, team_b)

            winner_id = team_a_row["id"] if result["winner"] == team_a.name else team_b_row["id"]
            loser_id = team_b_row["id"] if winner_id == team_a_row["id"] else team_a_row["id"]

            if is_championship_week:
                champion_name = team_a_row["team_name"] if winner_id == team_a_row["id"] else team_b_row["team_name"]

            db.table("voltball_matches").insert({
                "season_id": season["id"],
                "week_number": week,
                "is_playoff": is_playoff_week,
                "team_a_id": team_a_row["id"],
                "team_b_id": team_b_row["id"],
                "team_a_score": result["score_a"],
                "team_b_score": result["score_b"],
                "winner_team_id": winner_id,
                "log_text": result["log_text"],
                "quarters": result["quarters"],
                "log_lines": result["log"],
                "team_a_lineup": _lineup_snapshot(team_a),
                "team_b_lineup": _lineup_snapshot(team_b),
            }).execute()

            update_standings_after_match(
                season["id"], winner_id, loser_id,
                max(result["score_a"], result["score_b"]), min(result["score_a"], result["score_b"]),
            )

            record_injuries(team_a_row["id"], season["id"], result["injured_a"], week)
            record_injuries(team_b_row["id"], season["id"], result["injured_b"], week)

            if channel:
                embed = build_match_embed(result, week, is_playoff=is_playoff_week)
                await channel.send(embed=embed)

        bye_team_id = get_bye_team(season["id"], week)
        if bye_team_id:
            print(f"[voltball] Week {week}: {bye_team_id} has the bye.")

        # ── Playoff bracket state machine ──
        if not is_playoff_week:
            new_week = week + 1
            if new_week > week_count:
                # Regular season just finished -- seed the top-4 bracket from standings.
                standings_rows = get_standings(season["id"])
                if len(standings_rows) < 4:
                    new_status = "complete"
                    print(f"[voltball] Season {season['id']}: regular season ended with only {len(standings_rows)} team(s) — skipping playoffs (need 4), marking complete.")
                else:
                    seed = [r["team_id"] for r in standings_rows[:4]]
                    save_playoff_round(season["id"], new_week, [(seed[0], seed[3]), (seed[1], seed[2])])  # 1v4, 2v3
                    new_status = "playoffs"
            else:
                new_status = "active"
        elif week == week_count + 1:
            # Semifinals just resolved -- seed the championship from the two winners.
            winners = get_playoff_round_winners(season["id"], week)
            new_week = week_count + 2
            new_status = "playoffs"
            if len(winners) == 2:
                save_playoff_round(season["id"], new_week, [(winners[0], winners[1])])
            else:
                # Shouldn't normally happen (a forfeited semifinal never writes
                # a voltball_matches row, so it wouldn't produce a winner here)
                # -- don't generate a broken championship pairing if it does;
                # leave the season sitting in "playoffs" for manual review.
                print(f"[voltball] Season {season['id']}: expected 2 semifinal winners, got {len(winners)} — not generating a championship pairing. Needs manual review.")
        else:
            # This was the championship (week_count + 2) -- season is over.
            new_week = week + 1
            new_status = "complete"

        if new_status == "complete" and not champion_name:
            # The championship pairing was skipped by the duplicate-match
            # guard above (already resolved in an earlier, interrupted
            # run) -- champion_name only gets set when a match resolves
            # DURING this run's loop, so look it up from the actual
            # recorded match instead of silently dropping the announcement.
            champ_match = (
                db.table("voltball_matches")
                .select("winner_team_id")
                .eq("season_id", season["id"])
                .eq("week_number", week)
                .eq("is_playoff", True)
                .execute()
                .data
            )
            if champ_match:
                champ_team = get_team_by_id(champ_match[0]["winner_team_id"])
                if champ_team:
                    champion_name = champ_team["team_name"]

        db.table("voltball_seasons").update({"current_week": new_week, "status": new_status}).eq("id", season["id"]).execute()

        if channel:
            if new_status == "complete" and champion_name:
                await channel.send(embed=build_champion_embed(season, champion_name))
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


async def setup(bot: commands.Bot):
    await bot.add_cog(VoltballCog(bot))

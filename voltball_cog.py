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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from voltball_engine import resolve_match, HERO_SIGNATURES
from voltball_recap import build_recap
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
from voltball_embeds import (
    build_match_embed, build_standings_embed, build_champion_embed, build_lineups_embed,
    build_matchup_preview_embed, build_kickoff_embed, build_recap_post_embed,
)
from voltball_position_fit import get_position_fit, rank_collection_for_position, label_for_held_zappy
from database import get_supabase, get_wallet

# Public site root -- used to build the "Watch Live" / "Watch Replay" links
# posted to Discord. Update here if the GitHub Pages URL ever changes.
SITE_BASE_URL = "https://dswens-arch.github.io/voltball-site/"

# How long after a match's kickoff post it actually starts airing --
# this is a REAL wait, not decorative: it's the same value used to derive
# playback_starts_at on the match row, which the site's live playback
# gates on so every viewer sees the same beats at the same time.
PLAYBACK_KICKOFF_DELAY_SECONDS = 300

# Matches air one at a time, not all at once. Team count varies week to
# week (season size isn't locked in yet), so instead of a fixed number of
# broadcast slots, each week's matches are scheduled back-to-back starting
# at DAY_START_HOUR_LOCAL: match 1 airs first, match 2 starts only once
# match 1's estimated playback (see estimate_playback_seconds) has
# finished plus this gap, and so on -- guaranteed non-overlapping no
# matter how many matches resolve that week.
#
# LEAGUE_TIMEZONE is hardcoded for now since there's no per-guild timezone
# setting yet -- if this bot ever serves guilds outside this timezone,
# this needs to become a config value alongside resolution_weekday.
LEAGUE_TIMEZONE = ZoneInfo("America/Chicago")
DAY_START_HOUR_LOCAL = 8
MATCH_SLOT_GAP_SECONDS = 3600  # 1 hour between match slots

# Mirrors results.html's step() pacing constants exactly, so the delayed
# recap post lands roughly when the site's playback actually finishes.
# If you tune the pacing in results.html, update these too -- same
# "must stay in sync" pattern as the highlight-marker lists elsewhere in
# this codebase (see the module docstring gotchas). A few seconds of
# drift either way is harmless; this only decides when to post, it
# never affects what gets posted.
_QHEADER_SECONDS = 0.7
_QEND_SECONDS = 0.6
_EVENT_SECONDS = 3.8
_SIGNATURE_EVENT_SECONDS = 5.2
_PLAYBACK_BUFFER_SECONDS = 8.0  # safety margin so the recap never beats the last event onscreen


def estimate_playback_seconds(result: dict) -> float:
    """How long the site's live playback will take to run through this
    match's events at 1x speed -- see the pacing-constants comment above."""
    events = result.get("events", [])
    total = 0.0
    for q in range(1, 5):
        total += _QHEADER_SECONDS
        for e in events:
            if e.get("quarter") == q:
                total += _SIGNATURE_EVENT_SECONDS if e.get("kind") == "signature" else _EVENT_SECONDS
        total += _QEND_SECONDS
    post_events = [e for e in events if e.get("quarter") is None]
    if post_events:
        total += _QHEADER_SECONDS
        for e in post_events:
            total += _SIGNATURE_EVENT_SECONDS if e.get("kind") == "signature" else _EVENT_SECONDS
    return total + _PLAYBACK_BUFFER_SECONDS


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
        self.post_ready_kickoffs.start()
        self.post_ready_recaps.start()

    def cog_unload(self):
        self.weekly_resolution.cancel()
        self.post_ready_kickoffs.cancel()
        self.post_ready_recaps.cancel()

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

        # Season-wide stat allocation -- see voltball_season_stats.py.
        # Runs here, at CREATE time, not at season_start -- so coaches
        # can see their real season-allocated stats the moment they
        # register, and actually prepare before the season locks in,
        # rather than only finding out once week 1 already starts.
        allocations = allocate_season_stats()
        save_season_zappy_stats(season["id"], allocations)

        test_note = " (marked as a **test season** — wipeable, not a permanent record)" if is_test else ""
        await interaction.followup.send(
            f"🏈 Season **{name}** created{test_note}. Season stats allocated for all {len(allocations)} Zappies — "
            f"teams can register on the site now and see their real numbers right away. "
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

        # season stats now get allocated at /voltball_season_create, so
        # they exist (and should show) from 'upcoming' onward, not just
        # once the season is active/playoffs.
        season_id = season["id"] if season and season["status"] in ("upcoming", "active", "playoffs") else None

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
        """Runs daily; only actually resolves on each guild's configured weekly deadline day (checked in LEAGUE_TIMEZONE, not UTC)."""
        now_local = datetime.now(timezone.utc).astimezone(LEAGUE_TIMEZONE)
        db = get_supabase()
        seasons = db.table("voltball_seasons").select("*").in_("status", ["active", "playoffs"]).execute().data or []

        for season in seasons:
            config = get_guild_config(season["guild_id"])
            if now_local.weekday() != config["resolution_weekday"]:
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

        # Broadcast slot cursor for this week's matches -- see the
        # LEAGUE_TIMEZONE / DAY_START_HOUR_LOCAL / MATCH_SLOT_GAP_SECONDS
        # comment above. Anchored to 8am local on the day resolution runs;
        # if resolution happens to run after 8am (or the very first
        # match's real "kicks off in 5 min" promise would land later than
        # 8am), start from whichever is later so nothing is scheduled in
        # the past or breaks the kickoff post's promised wait.
        resolution_now = datetime.now(timezone.utc)
        local_today = resolution_now.astimezone(LEAGUE_TIMEZONE).date()
        day_start_local = datetime(
            local_today.year, local_today.month, local_today.day,
            DAY_START_HOUR_LOCAL, tzinfo=LEAGUE_TIMEZONE,
        )
        next_slot_start = max(
            day_start_local.astimezone(timezone.utc),
            resolution_now + timedelta(seconds=PLAYBACK_KICKOFF_DELAY_SECONDS),
        )

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
            recap = build_recap(result, team_a, team_b)

            winner_id = team_a_row["id"] if result["winner"] == team_a.name else team_b_row["id"]
            loser_id = team_b_row["id"] if winner_id == team_a_row["id"] else team_a_row["id"]

            if is_championship_week:
                champion_name = team_a_row["team_name"] if winner_id == team_a_row["id"] else team_b_row["team_name"]

            playback_starts_at = next_slot_start
            recap_post_at = playback_starts_at + timedelta(seconds=estimate_playback_seconds(result))
            kickoff_post_at = playback_starts_at - timedelta(seconds=PLAYBACK_KICKOFF_DELAY_SECONDS)
            # Advance the cursor so the next match in this week's loop
            # gets the next open slot -- starts only after this match's
            # estimated playback is done, plus the gap. Never overlaps,
            # regardless of how many teams are in the season.
            next_slot_start = recap_post_at + timedelta(seconds=MATCH_SLOT_GAP_SECONDS)

            match_row = db.table("voltball_matches").insert({
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
                "events": result["events"],
                "quarter_totals": result["quarter_totals"],
                "recap": recap,
                "team_a_lineup": _lineup_snapshot(team_a),
                "team_b_lineup": _lineup_snapshot(team_b),
                "playback_starts_at": playback_starts_at.isoformat(),
                "recap_post_at": recap_post_at.isoformat(),
                "recap_posted_at": None,
                "kickoff_post_at": kickoff_post_at.isoformat(),
                "kickoff_posted_at": None,
            }).execute().data[0]

            update_standings_after_match(
                season["id"], winner_id, loser_id,
                max(result["score_a"], result["score_b"]), min(result["score_a"], result["score_b"]),
            )

            record_injuries(team_a_row["id"], season["id"], result["injured_a"], week)
            record_injuries(team_b_row["id"], season["id"], result["injured_b"], week)

            # The kickoff post ("Watch Live", match-specific link -- not
            # the shared "?live=current" this used before, since that
            # assumed one game airs at a time) is NOT sent here anymore.
            # Now that matches are staggered across the day instead of
            # all starting ~5 minutes after resolution, posting it
            # immediately would make its "kicks off in 5 minutes" promise
            # false for every match after the first. Instead it's posted
            # by post_ready_kickoffs once kickoff_post_at actually
            # arrives -- same durable, restart-safe polling pattern as
            # the recap post below, just one step earlier in the chain.

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

        # Standings/champion announcement is deliberately NOT sent here.
        # This function returns as soon as matches are resolved (seconds
        # after the kickoff posts), but a match's actual outcome isn't
        # supposed to be visible in Discord until its "watch live" window
        # has played out. Standings (wins, PF/PA) leak the outcome just as
        # much as the recap does, so it waits for the same signal the
        # recap post waits for -- see post_ready_recaps, which posts
        # standings/champion once every match this week has actually aired.

    @weekly_resolution.before_loop
    async def before_weekly_resolution(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────
    # Delayed "Watch Live" kickoff post -- one broadcast slot at a time.
    #
    # Matches resolve for the whole week in one batch (see
    # _resolve_season_week's slot cursor), but with team count varying
    # week to week we don't want every kickoff post to land in Discord
    # at once, and each one's "kicks off in 5 minutes" promise needs to
    # actually be true when it posts. So kickoff_post_at is set per
    # match at resolution time (playback_starts_at minus the 5-minute
    # promise) and this loop polls for whichever match's turn has come,
    # same durable DB-driven pattern as post_ready_recaps below --
    # survives a bot restart without dropping or double-posting a
    # kickoff, and needs no in-memory timer per match.
    # ─────────────────────────────────────────────
    @tasks.loop(seconds=20)
    async def post_ready_kickoffs(self):
        db = get_supabase()
        now_iso = datetime.now(timezone.utc).isoformat()

        due = (
            db.table("voltball_matches")
            .select("*")
            .is_("kickoff_posted_at", "null")
            .not_.is_("kickoff_post_at", "null")
            .lte("kickoff_post_at", now_iso)
            .execute()
            .data
        ) or []

        for match in due:
            try:
                season_row = db.table("voltball_seasons").select("guild_id").eq("id", match["season_id"]).execute().data
                if not season_row:
                    continue
                config = get_guild_config(season_row[0]["guild_id"])
                if not config or not config.get("announcement_channel_id"):
                    continue
                channel = self.bot.get_channel(int(config["announcement_channel_id"]))
                if not channel:
                    continue

                team_a_row = get_team_by_id(match["team_a_id"])
                team_b_row = get_team_by_id(match["team_b_id"])
                if not team_a_row or not team_b_row:
                    continue

                link = f"{SITE_BASE_URL}results.html?live={match['id']}"
                kickoff_embed = build_kickoff_embed(
                    team_a_row["team_name"], team_b_row["team_name"],
                    match["week_number"], match["is_playoff"], link,
                )
                await channel.send(embed=kickoff_embed)
            except Exception as e:
                # Same rationale as post_ready_recaps below: one bad match
                # shouldn't wedge the whole batch or get retried forever.
                print(f"[voltball] Failed to post kickoff for match {match.get('id')}: {e}")
            finally:
                db.table("voltball_matches").update({"kickoff_posted_at": now_iso}).eq("id", match["id"]).execute()

    @post_ready_kickoffs.before_loop
    async def before_post_ready_kickoffs(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────
    # Delayed "after the game" recap post.
    #
    # Deliberately NOT an asyncio.sleep() timer started right after
    # resolution -- that approach loses the scheduled post silently on
    # any bot restart/redeploy during the wait (Railway redeploys happen;
    # this bot restarts more often than a 5-minute-plus wait can safely
    # assume it won't). Instead, timing lives in the DB
    # (recap_post_at, set once at resolution time) and this loop polls
    # for matches whose time has come -- same durable-state pattern as
    # the duplicate-match guard in _resolve_season_week, and it survives
    # a restart at any point without double-posting or dropping a post.
    # ─────────────────────────────────────────────
    @tasks.loop(seconds=20)
    async def post_ready_recaps(self):
        db = get_supabase()
        now_iso = datetime.now(timezone.utc).isoformat()

        due = (
            db.table("voltball_matches")
            .select("*")
            .is_("recap_posted_at", "null")
            .not_.is_("recap_post_at", "null")
            .lte("recap_post_at", now_iso)
            .execute()
            .data
        ) or []

        for match in due:
            try:
                season_row = db.table("voltball_seasons").select("guild_id").eq("id", match["season_id"]).execute().data
                if not season_row:
                    continue
                config = get_guild_config(season_row[0]["guild_id"])
                if not config or not config.get("announcement_channel_id"):
                    continue
                channel = self.bot.get_channel(int(config["announcement_channel_id"]))
                if not channel:
                    continue

                team_a_row = get_team_by_id(match["team_a_id"])
                team_b_row = get_team_by_id(match["team_b_id"])
                if not team_a_row or not team_b_row:
                    continue

                link = f"{SITE_BASE_URL}results.html?live={match['id']}"
                recap = match.get("recap") or {"recap_text": "Full recap unavailable for this match."}
                embed = build_recap_post_embed(
                    recap, team_a_row["team_name"], team_b_row["team_name"],
                    match["team_a_score"], match["team_b_score"],
                    match["week_number"], match["is_playoff"], link,
                )
                await channel.send(embed=embed)

                # Standings (and a champion announcement) reveal the
                # outcome just as much as the recap does -- wins, PF/PA
                # all shift the moment they post. So they wait for the
                # SAME thing the recap waited for, plus one more
                # condition: every other match in this same
                # (season, week) has also finished airing, not just this
                # one. A week with two simultaneous games shouldn't have
                # its standings spoiled by whichever game's replay
                # finishes first.
                still_pending = (
                    db.table("voltball_matches")
                    .select("id")
                    .eq("season_id", match["season_id"])
                    .eq("week_number", match["week_number"])
                    .is_("recap_posted_at", "null")
                    .execute()
                    .data
                ) or []
                # (`still_pending` includes this match, since we haven't
                # set its recap_posted_at yet -- that happens in `finally`
                # below. So "only this one left" means the count is 1.)
                if len(still_pending) <= 1:
                    full_season = db.table("voltball_seasons").select("*").eq("id", match["season_id"]).execute().data
                    if full_season and full_season[0].get("last_standings_posted_week") != match["week_number"]:
                        full_season = full_season[0]
                        if full_season["status"] == "complete":
                            champ_match = (
                                db.table("voltball_matches")
                                .select("winner_team_id")
                                .eq("season_id", match["season_id"])
                                .eq("is_playoff", True)
                                .eq("week_number", match["week_number"])
                                .execute()
                                .data
                            )
                            if champ_match:
                                champ_team = get_team_by_id(champ_match[0]["winner_team_id"])
                                if champ_team:
                                    await channel.send(embed=build_champion_embed(full_season, champ_team["team_name"]))
                        standings_rows = get_standings(match["season_id"])
                        await channel.send(embed=build_standings_embed(full_season, standings_rows))
                        db.table("voltball_seasons").update(
                            {"last_standings_posted_week": match["week_number"]}
                        ).eq("id", match["season_id"]).execute()
            except Exception as e:
                # One bad match shouldn't block the rest of the due batch,
                # and shouldn't get silently retried forever either -- log
                # and mark it posted so it doesn't wedge the loop.
                print(f"[voltball] Failed to post delayed recap for match {match.get('id')}: {e}")
            finally:
                db.table("voltball_matches").update({"recap_posted_at": now_iso}).eq("id", match["id"]).execute()

    @post_ready_recaps.before_loop
    async def before_post_ready_recaps(self):
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

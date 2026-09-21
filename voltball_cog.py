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
from datetime import datetime, timedelta, timezone, time as dt_time
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
from voltball_schedule import (
    save_schedule, save_playoff_round, get_week_pairings, get_bye_team,
    week_is_open, open_week, get_due_pairings, mark_pairing_resolved, count_unresolved_pairings,
)
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
SITE_BASE_URL = "https://voltball.xyz/"

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
# 9am local on game day -- the first broadcast slot. No longer tied to
# a same-day resolution trigger (resolution is per-match now, gated by
# each pairing's own scheduled_kickoff_at -- see resolve_ready_matches),
# just a fixed, predictable start-of-day time for the first match of
# the week regardless of team count.
DAY_START_HOUR_LOCAL = 9
MATCH_SLOT_GAP_SECONDS = 3600  # 1 hour between match slots

# A forfeit (opponent couldn't field 8 Zappies) used to record a flat
# 0-0 into both teams' cumulative points_for/points_against on
# voltball_standings -- harmless for W-L, but get_standings sorts by
# points_for as the wins tiebreaker, so a forfeit-heavy team's real
# ranking was quietly dragged down relative to teams that won for
# real. These sit well inside the league's actual scoring range
# (real winning scores have been running ~200-300) rather than at the
# extremes, and 0 for the loser reflects that they never fielded a
# team at all -- there's nothing more specific to credit them with.
FORFEIT_WINNER_SCORE = 200
FORFEIT_LOSER_SCORE = 0


def _next_weekday_date(now_local: datetime, target_weekday: int):
    """
    The next calendar date (today included) landing on target_weekday
    (Python's Monday=0..Sunday=6), given now_local already carries the
    right tzinfo. Used by _open_season_week to find the upcoming game
    day (resolution_weekday, still Sunday) from whatever day the open
    step actually runs on (normally the day before, but a manual
    /voltball_open_week catch-up can run any day of the week). A
    days_ahead of 0 means today IS the target day -- correct for a
    same-day catch-up run, not a bug.
    """
    days_ahead = (target_weekday - now_local.weekday()) % 7
    return now_local.date() + timedelta(days=days_ahead)


def _fmt_kickoff_time(iso_str: str | None) -> str | None:
    """'8:00 AM' in LEAGUE_TIMEZONE from a stored UTC timestamp -- None
    passes through as None (e.g. a forfeit/bye pairing with no real
    broadcast slot), so callers can decide how to display "no time"."""
    if not iso_str:
        return None
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LEAGUE_TIMEZONE).strftime("%-I:%M %p")

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
        self.open_ready_weeks.start()
        self.resolve_ready_matches.start()
        self.post_ready_kickoffs.start()
        self.post_ready_recaps.start()

    def cog_unload(self):
        self.open_ready_weeks.cancel()
        self.resolve_ready_matches.cancel()
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
    # ─────────────────────────────────────────────
    # /voltball_open_week (admin) — manual trigger for the daily
    # open_ready_weeks job. Assigns this week's real pairings their
    # Sunday hourly slots and posts the matchup preview/reminder.
    # Refuses if the week's already open (same "generated once, not
    # regenerated" guard /voltball_season_start uses for the schedule
    # itself) UNLESS repost=True, which re-sends the announcement using
    # the times that already exist -- no reassignment, so it can't
    # shuffle a kickoff time that's already been announced. That's the
    # one legitimate reason to run this on an already-open week: the
    # original post got deleted, or you just want it re-sent.
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_open_week", description="[Admin] Open the current week (assign game times, post the reminder).")
    @app_commands.describe(repost="Already open — just re-send the announcement with the existing times, don't reassign anything.")
    @app_commands.checks.has_permissions(administrator=True)
    async def voltball_open_week(self, interaction: discord.Interaction, repost: bool = False):
        await interaction.response.defer(ephemeral=True)

        season = get_active_or_playoff_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("No active or playoff season to open — run `/voltball_season_start` first.", ephemeral=True)
            return

        config = get_guild_config(str(interaction.guild_id))
        week = season["current_week"]
        is_open = week_is_open(season["id"], week)

        if repost:
            if not is_open:
                await interaction.followup.send(f"Week {week} isn't open yet — run `/voltball_open_week` without `repost` first.", ephemeral=True)
                return
            week_count = season["week_count"]
            is_playoff_week = season["status"] == "playoffs"
            round_label = None
            if is_playoff_week:
                round_label = "Semifinal" if week == week_count + 1 else "Championship"
            pairings = sorted(get_week_pairings(season["id"], week), key=lambda p: p["scheduled_kickoff_at"])
            match_time_labels = {
                (p["team_a_id"], p["team_b_id"]): _fmt_kickoff_time(p["scheduled_kickoff_at"]) for p in pairings
            }
            posted = await self._post_week_announcement(season, config, week, pairings, match_time_labels, round_label)
            note = "" if posted else " (no announcement channel configured — set one with `/voltball_config`)"
            await interaction.followup.send(f"🔁 Week {week} announcement reposted with its existing times.{note}", ephemeral=True)
            return

        if is_open:
            await interaction.followup.send(f"Week {week} is already open — game times are already assigned. Use `repost:True` to re-send the announcement without reassigning times.", ephemeral=True)
            return

        posted = await self._open_season_week(season, config)
        note = "" if posted else " (no announcement channel configured — times saved but nothing posted; set one with `/voltball_config`)"
        await interaction.followup.send(f"📅 Week {week} opened — game times assigned.{note}", ephemeral=True)

    # ─────────────────────────────────────────────
    # /voltball_resolve_week (admin) — manual override, bypasses BOTH
    # the open-day gate and each pairing's own scheduled_kickoff_at.
    # Opens the week first if it isn't already (so posting still
    # staggers sensibly), then force-resolves every pairing that hasn't
    # resolved yet, right now. Same role as before: a test season plays
    # out exactly like a real one would, on demand.
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_resolve_week", description="[Admin] Force-resolve every remaining match this week right now (testing/catch-up).")
    @app_commands.checks.has_permissions(administrator=True)
    async def voltball_resolve_week(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        season = get_active_or_playoff_season(str(interaction.guild_id))
        if not season:
            await interaction.followup.send("No active or playoff season to resolve — run `/voltball_season_start` first.", ephemeral=True)
            return

        week_before = season["current_week"]
        config = get_guild_config(str(interaction.guild_id))

        if not week_is_open(season["id"], week_before):
            await self._open_season_week(season, config)

        pairings = [p for p in get_week_pairings(season["id"], week_before) if not p.get("resolved_at")]
        resolved_count = 0
        for pairing in pairings:
            await self._resolve_one_pairing(pairing)
            resolved_count += 1

        note = "" if config["announcement_channel_id"] else " (no announcement channel configured — results saved but nothing posted; set one with `/voltball_config` if you want to see it play out)"
        await interaction.followup.send(f"✅ Week {week_before}: {resolved_count} match(es) force-resolved.{note}", ephemeral=True)

    # ─────────────────────────────────────────────
    # /voltball_post_pending (admin) — flushes any due-but-unposted
    # kickoff/recap right now, instead of waiting for the next 20s poll.
    # This is a safety valve for exactly the kind of thing we just hit:
    # the automatic loops missed something (a bug, a restart at the
    # wrong moment, whatever) and it needs to go out NOW rather than
    # waiting to see if the poller sorts itself out. Calls the exact
    # same code the two tasks.loop jobs call every 20 seconds -- not a
    # separate reimplementation that could drift out of sync with them.
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_post_pending", description="[Admin] Post any due kickoff/recap right now instead of waiting for the poller.")
    @app_commands.checks.has_permissions(administrator=True)
    async def voltball_post_pending(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        kickoffs_posted = await self._post_ready_kickoffs_once()
        recaps_posted = await self._post_ready_recaps_once()
        await interaction.followup.send(f"📤 Posted {kickoffs_posted} kickoff(s) and {recaps_posted} recap(s) that were due.", ephemeral=True)

    # ─────────────────────────────────────────────
    # /voltball_post_standings (admin) — manually posts the current
    # season's standings (and a champion embed, if the season just
    # wrapped) to the announcement channel. Refuses by default if this
    # week's standings already posted (same "generated once, not
    # regenerated" shape as /voltball_open_week's guard) -- force:True
    # reposts anyway, e.g. if the automatic post genuinely never fired.
    # ─────────────────────────────────────────────
    @app_commands.command(name="voltball_post_standings", description="[Admin] Manually post the season standings to the announcement channel.")
    @app_commands.describe(force="Already posted for this week — post again anyway.")
    @app_commands.checks.has_permissions(administrator=True)
    async def voltball_post_standings(self, interaction: discord.Interaction, force: bool = False):
        await interaction.response.defer(ephemeral=True)

        season = get_active_or_playoff_season(str(interaction.guild_id))
        if not season:
            # Standings can still be worth posting once a season is
            # fully complete (status no longer active/playoffs), so
            # fall back to the most recently completed one rather than
            # refusing outright.
            db = get_supabase()
            recent = (
                db.table("voltball_seasons")
                .select("*")
                .eq("guild_id", str(interaction.guild_id))
                .eq("status", "complete")
                .order("current_week", desc=True)
                .limit(1)
                .execute()
                .data
            )
            season = recent[0] if recent else None
        if not season:
            await interaction.followup.send("No season found for this server.", ephemeral=True)
            return

        config = get_guild_config(str(interaction.guild_id))
        if not config or not config.get("announcement_channel_id"):
            await interaction.followup.send("No announcement channel configured — set one with `/voltball_config` first.", ephemeral=True)
            return
        channel = self.bot.get_channel(int(config["announcement_channel_id"]))
        if not channel:
            await interaction.followup.send("Couldn't find the configured announcement channel — it may have been deleted.", ephemeral=True)
            return

        week = season["current_week"]
        posted = await self._post_standings_for_week(season["id"], week, channel, force=force)
        if posted:
            await interaction.followup.send(f"✅ Standings posted for week {week}.", ephemeral=True)
        else:
            await interaction.followup.send(f"Standings for week {week} were already posted. Use `force:True` to post again anyway.", ephemeral=True)

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
    # Week-open job — assigns this week's real pairings their Sunday
    # hourly slots and posts the matchup preview (doubling as the
    # "set your lineup" reminder) as soon as it's known, instead of
    # waiting until resolution. Runs the day BEFORE game day (one day
    # before resolution_weekday) so there's a real window for coaches
    # to see their kickoff time and lock a lineup in before it passes.
    #
    # Same fixed-wall-clock-time pattern the old weekly_resolution job
    # used (see its removed comment, preserved in spirit here): tz-aware,
    # DST-safe, and immune to bot-restart drift. Checked daily; the
    # weekday gate below (and week_is_open's own guard) is what makes
    # it only actually act once a week per guild.
    # ─────────────────────────────────────────────
    @tasks.loop(time=dt_time(hour=9, minute=0, tzinfo=LEAGUE_TIMEZONE))
    async def open_ready_weeks(self):
        """Runs daily at 9am Chicago; only actually opens a week on each guild's configured open day (the day before resolution_weekday)."""
        now_local = datetime.now(timezone.utc).astimezone(LEAGUE_TIMEZONE)
        db = get_supabase()
        seasons = db.table("voltball_seasons").select("*").in_("status", ["active", "playoffs"]).execute().data or []

        for season in seasons:
            config = get_guild_config(season["guild_id"])
            open_weekday = (config["resolution_weekday"] - 1) % 7
            if now_local.weekday() != open_weekday:
                continue
            if week_is_open(season["id"], season["current_week"]):
                continue
            await self._open_season_week(season, config)

    @open_ready_weeks.before_loop
    async def before_open_ready_weeks(self):
        await self.bot.wait_until_ready()

    async def _open_season_week(self, season: dict, config: dict) -> bool:
        """
        Assigns scheduled_kickoff_at to every real pairing in the
        season's current week (Sunday hourly slots, same cadence
        _resolve_season_week used to compute at resolution time —
        just computed here, up to a week earlier) and posts the
        matchup preview to the announcement channel if one's
        configured. Returns whether it actually posted, for the
        manual /voltball_open_week command's confirmation message.

        Does NOT check week_is_open itself — callers (the daily job
        above, both admin commands below) are responsible for that
        guard, since they want different behavior on an already-open
        week (silently skip vs. tell the admin).
        """
        db = get_supabase()
        week = season["current_week"]
        week_count = season["week_count"]
        is_playoff_week = season["status"] == "playoffs"
        round_label = None
        if is_playoff_week:
            round_label = "Semifinal" if week == week_count + 1 else "Championship"

        pairings = get_week_pairings(season["id"], week)
        if not pairings:
            print(f"[voltball] Week {week}: no real pairings to open (season {season['id']}).")
            return False

        # Slot cursor for this week's matches -- see LEAGUE_TIMEZONE /
        # DAY_START_HOUR_LOCAL / MATCH_SLOT_GAP_SECONDS above. Anchored
        # to 9am local on the upcoming game day (resolution_weekday),
        # not "today" -- open normally runs the day before. The max()
        # guard only matters for a very-late manual /voltball_open_week
        # catch-up (e.g. run mid-Sunday), so the first slot still can't
        # land in the past.
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(LEAGUE_TIMEZONE)
        game_date = _next_weekday_date(now_local, config["resolution_weekday"])
        day_start_local = datetime(
            game_date.year, game_date.month, game_date.day,
            DAY_START_HOUR_LOCAL, tzinfo=LEAGUE_TIMEZONE,
        )
        next_slot_start = max(
            day_start_local.astimezone(timezone.utc),
            now_utc + timedelta(seconds=PLAYBACK_KICKOFF_DELAY_SECONDS),
        )

        # Sorted for determinism before assigning slots -- Supabase gives
        # no ordering guarantee otherwise. This ALSO ends up being the
        # display order passed to _post_week_announcement below, so it
        # doubles as making the posted matchup list read chronologically
        # (earliest kickoff first) instead of in arbitrary DB order.
        sorted_pairings = sorted(pairings, key=lambda p: (p["team_a_id"], p["team_b_id"]))

        kickoff_times = {}
        match_time_labels = {}
        for pairing in sorted_pairings:
            kickoff_times[(pairing["team_a_id"], pairing["team_b_id"])] = next_slot_start.isoformat()
            match_time_labels[(pairing["team_a_id"], pairing["team_b_id"])] = _fmt_kickoff_time(next_slot_start.isoformat())
            next_slot_start = next_slot_start + timedelta(seconds=MATCH_SLOT_GAP_SECONDS)

        open_week(season["id"], week, kickoff_times)

        bye_team_id = get_bye_team(season["id"], week)
        if bye_team_id:
            print(f"[voltball] Week {week}: {bye_team_id} has the bye.")

        return await self._post_week_announcement(season, config, week, sorted_pairings, match_time_labels, round_label)

    async def _post_week_announcement(self, season: dict, config: dict, week: int, pairings: list[dict],
                                       match_time_labels: dict, round_label: str | None) -> bool:
        """
        Posts (or reposts) the matchup-preview embed + @everyone
        reminder to the announcement channel. Pulled out of
        _open_season_week so /voltball_open_week's repost option can
        call it directly with times that already exist, WITHOUT going
        through open_week() again -- reposting must never reassign or
        shuffle anyone's already-announced kickoff time.
        """
        channel = None
        if config["announcement_channel_id"]:
            channel = self.bot.get_channel(int(config["announcement_channel_id"]))
        if channel:
            teams = get_teams_for_season(season["id"])
            team_lookup = {t["id"]: t for t in teams}
            standings_lookup = {r["team_id"]: r for r in get_standings(season["id"])}
            preview_embed = build_matchup_preview_embed(season, week, pairings, team_lookup, match_time_labels, standings_lookup=standings_lookup, round_label=round_label)
            # @everyone -- this post is the only heads-up coaches get
            # that this week's real kickoff times exist before those
            # times start passing (lineups lock per-match now, not all
            # at once at a single weekly deadline -- see
            # resolve_ready_matches), so it's worth the ping rather than
            # relying on someone happening to scroll past the embed.
            # allowed_mentions scoped to JUST everyone=True here (not
            # users/roles) -- deliberately narrower than the kickoff/
            # recap posts' AllowedMentions(users=True), since this is
            # the one message that's supposed to reach the whole room.
            week_label = round_label if round_label else f"Week {week}"
            await channel.send(
                content=f"@everyone ⏰ **{week_label} is open** — game times are set, go lock in your lineup before your matchup kicks off!",
                embed=preview_embed,
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
            return True
        return False

    # ─────────────────────────────────────────────
    # Per-match resolver -- replaces the old weekly batch job. Polls for
    # pairings whose real lock instant (scheduled_kickoff_at minus
    # PLAYBACK_KICKOFF_DELAY_SECONDS -- see get_due_pairings) has
    # arrived, and resolves ONLY that one match: reads and locks in
    # whatever lineup exists for it right now, same as the old batch
    # loop did for everyone at once at 8am. This is the actual fix for
    # "everyone locks at the same time" -- a 2pm game's lineup is read
    # at 2pm (minus the 5-minute kickoff-post lead), not whenever some
    # other match's slot happens to be.
    #
    # Same durable, restart-safe polling shape as post_ready_kickoffs/
    # post_ready_recaps below: resolved_at (on voltball_schedule) is
    # the persisted state, not an in-memory timer, so a bot restart
    # mid-week just resumes -- already-resolved pairings drop out of
    # get_due_pairings on their own.
    # ─────────────────────────────────────────────
    @tasks.loop(seconds=20)
    async def resolve_ready_matches(self):
        cutoff = (datetime.now(timezone.utc) + timedelta(seconds=PLAYBACK_KICKOFF_DELAY_SECONDS)).isoformat()
        due = get_due_pairings(cutoff)
        for pairing in due:
            await self._resolve_one_pairing(pairing)

    @resolve_ready_matches.before_loop
    async def before_resolve_ready_matches(self):
        await self.bot.wait_until_ready()

    async def _resolve_one_pairing(self, pairing: dict):
        """
        Resolves exactly one pairing: builds both teams (locked lineup,
        auto-fallback, or CPU), simulates the match, writes the
        voltball_matches row (or applies a forfeit), and marks the
        pairing resolved on voltball_schedule. Playback/kickoff/recap
        post timing all key off the pairing's OWN scheduled_kickoff_at
        -- the time already announced in the matchup preview -- not a
        freshly-computed slot, so the "kicks off in 5 minutes" promise
        made days earlier stays true.

        On a LineupValidationError (a locked-in Zappy no longer held --
        see get_locked_lineup_team's docstring), this pairing is left
        unresolved and will simply be retried on the next 20s poll.
        That's intentional, not a bug: this should be vanishingly rare,
        and silently giving up would need a human to notice and rerun
        it manually anyway -- retrying costs nothing and self-heals if
        the underlying data issue gets fixed.
        """
        db = get_supabase()
        now_iso = datetime.now(timezone.utc).isoformat()

        season = db.table("voltball_seasons").select("*").eq("id", pairing["season_id"]).execute().data
        if not season:
            mark_pairing_resolved(pairing["id"], now_iso)  # season is gone (wiped?) -- nothing to resolve against
            return
        season = season[0]
        config = get_guild_config(season["guild_id"])

        week = pairing["week_number"]
        week_count = season["week_count"]
        is_playoff_week = pairing["is_playoff"]
        is_championship_week = is_playoff_week and week == week_count + 2
        champion_name = None

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
            return

        if team_a is None and team_b is None:
            print(f"[voltball] Week {week}: {team_a_row['team_name']} vs {team_b_row['team_name']} — both sides forfeit (fewer than 8 Zappies held), no match recorded.")
        elif team_a is None:
            print(f"[voltball] Week {week}: {team_a_row['team_name']} forfeits (fewer than 8 Zappies held) — {team_b_row['team_name']} advances, no match recorded.")
            update_standings_after_match(season["id"], team_b_row["id"], team_a_row["id"], FORFEIT_WINNER_SCORE, FORFEIT_LOSER_SCORE)
            if is_championship_week:
                champion_name = team_b_row["team_name"]
        elif team_b is None:
            print(f"[voltball] Week {week}: {team_b_row['team_name']} forfeits (fewer than 8 Zappies held) — {team_a_row['team_name']} advances, no match recorded.")
            update_standings_after_match(season["id"], team_a_row["id"], team_b_row["id"], FORFEIT_WINNER_SCORE, FORFEIT_LOSER_SCORE)
            if is_championship_week:
                champion_name = team_a_row["team_name"]
        else:
            team_a.name = team_a_row["team_name"]
            team_b.name = team_b_row["team_name"]

            result = resolve_match(team_a, team_b)
            recap = build_recap(result, team_a, team_b)
            winner_id = team_a_row["id"] if result["winner"] == team_a.name else team_b_row["id"]
            if is_championship_week:
                champion_name = team_a_row["team_name"] if winner_id == team_a_row["id"] else team_b_row["team_name"]

            # Pinned to the pairing's OWN announced time, not "now" --
            # this is the whole point: what was promised in the
            # matchup preview days ago is exactly what airs.
            playback_starts_at = datetime.fromisoformat(pairing["scheduled_kickoff_at"].replace("Z", "+00:00"))
            recap_post_at = playback_starts_at + timedelta(seconds=estimate_playback_seconds(result))
            kickoff_post_at = playback_starts_at - timedelta(seconds=PLAYBACK_KICKOFF_DELAY_SECONDS)

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
                "standings_applied_at": None,
                "injuries_applied_at": None,
                "injured_a": result["injured_a"],
                "injured_b": result["injured_b"],
            }).execute()

            # Standings/injuries are deliberately NOT applied here -- see
            # post_ready_recaps, which applies them once this match's
            # recap actually airs (same spoiler-prevention reasoning as
            # before, unchanged by this refactor).
            #
            # The kickoff post ("Watch Live") is also NOT sent here --
            # post_ready_kickoffs picks it up once kickoff_post_at
            # arrives, same as before.

        mark_pairing_resolved(pairing["id"], now_iso)

        if count_unresolved_pairings(season["id"], week) == 0:
            await self._finalize_season_week(season, week, week_count, is_playoff_week, champion_name)

    async def _finalize_season_week(self, season: dict, week: int, week_count: int, is_playoff_week: bool, champion_name: str | None):
        """
        Runs once, whichever pairing happens to be the LAST one to
        resolve for this week (spread across the day now instead of
        all finishing at once) -- advances current_week, seeds the
        next playoff round or marks the season complete. Extracted
        unchanged from the old batch job's tail end; only the trigger
        changed (was "the whole week's loop just finished", now "the
        count of this week's unresolved pairings just hit zero").
        """
        db = get_supabase()

        if not is_playoff_week:
            new_week = week + 1
            if new_week > week_count:
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
            winners = get_playoff_round_winners(season["id"], week)
            new_week = week_count + 2
            new_status = "playoffs"
            if len(winners) == 2:
                save_playoff_round(season["id"], new_week, [(winners[0], winners[1])])
            else:
                print(f"[voltball] Season {season['id']}: expected 2 semifinal winners, got {len(winners)} — not generating a championship pairing. Needs manual review.")
        else:
            new_week = week + 1
            new_status = "complete"

        if new_status == "complete" and not champion_name:
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

        # Standings/champion announcement still deliberately NOT sent
        # here -- see post_ready_recaps, unchanged reasoning.

    # ─────────────────────────────────────────────
    # Delayed "Watch Live" kickoff post -- one broadcast slot at a time.
    #
    # Each match now resolves individually, right at its own real
    # kickoff time (see resolve_ready_matches / _resolve_one_pairing),
    # but the "kicks off in 5 minutes" post still can't be sent at the
    # exact moment of resolution -- kickoff_post_at is pinned to
    # playback_starts_at minus the 5-minute promise, same as before,
    # and this loop polls for whichever match's turn has come, same
    # durable DB-driven pattern as post_ready_recaps below -- survives
    # a bot restart without dropping or double-posting a kickoff, and
    # needs no in-memory timer per match.
    # ─────────────────────────────────────────────
    @tasks.loop(seconds=20)
    async def post_ready_kickoffs(self):
        await self._post_ready_kickoffs_once()

    async def _post_ready_kickoffs_once(self) -> int:
        """Posts every currently-due kickoff. Returns how many it processed, so /voltball_post_pending can report a real count instead of a blind 'done'."""
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
                # Tag both coaches so they actually see their kickoff go
                # live -- CPU teams (is_cpu, no owner_discord_id) are
                # skipped rather than mentioning "None". AllowedMentions
                # pinned to users=True so this can never fan out into an
                # accidental @everyone/@role ping if a bad row ever slips
                # through.
                mentions = " ".join(
                    f"<@{t['owner_discord_id']}>"
                    for t in (team_a_row, team_b_row)
                    if not t.get("is_cpu") and t.get("owner_discord_id")
                )
                await channel.send(
                    content=mentions or None,
                    embed=kickoff_embed,
                    allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
                )
            except Exception as e:
                # Same rationale as post_ready_recaps below: one bad match
                # shouldn't wedge the whole batch or get retried forever.
                print(f"[voltball] Failed to post kickoff for match {match.get('id')}: {e}")
            finally:
                db.table("voltball_matches").update({"kickoff_posted_at": now_iso}).eq("id", match["id"]).execute()

        return len(due)

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
    # the resolved_at guard in get_due_pairings, and it survives
    # a restart at any point without double-posting or dropping a post.
    # ─────────────────────────────────────────────
    @tasks.loop(seconds=20)
    async def post_ready_recaps(self):
        await self._post_ready_recaps_once()

    async def _post_ready_recaps_once(self) -> int:
        """Posts every currently-due recap (and standings/champion once a week fully wraps). Returns how many recaps it processed."""
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
                # Standings write moved here from resolution time -- this
                # is the actual moment a match is allowed to affect the
                # public standings table, since it's the same gate the
                # recap itself waits on. Guarded by its own
                # standings_applied_at (written immediately, not in the
                # shared `finally` below) so a crash between this call and
                # the rest of the loop can't double-apply the same result
                # to a team's win/loss and PF/PA on the next poll.
                if not match.get("standings_applied_at"):
                    winner_id = match["winner_team_id"]
                    loser_id = match["team_b_id"] if winner_id == match["team_a_id"] else match["team_a_id"]
                    update_standings_after_match(
                        match["season_id"], winner_id, loser_id,
                        max(match["team_a_score"], match["team_b_score"]),
                        min(match["team_a_score"], match["team_b_score"]),
                    )
                    db.table("voltball_matches").update({"standings_applied_at": now_iso}).eq("id", match["id"]).execute()

                if not match.get("injuries_applied_at"):
                    record_injuries(match["team_a_id"], match["season_id"], match.get("injured_a") or [], match["week_number"])
                    record_injuries(match["team_b_id"], match["season_id"], match.get("injured_b") or [], match["week_number"])
                    db.table("voltball_matches").update({"injuries_applied_at": now_iso}).eq("id", match["id"]).execute()

                season_row = db.table("voltball_seasons").select("*").eq("id", match["season_id"]).execute().data
                if not season_row:
                    continue
                season_row = season_row[0]

                # Whole-week-done signal for the SITE (lineup.html) --
                # deliberately unconditional, computed before the
                # channel-gated Discord logic below, so it works even for
                # a guild with no announcement channel configured. Without
                # this, lineup.html had nothing but current_week to know
                # which week is open, and current_week advances the
                # instant /voltball_resolve_week runs -- letting next
                # week's lineup (and injury reports) unlock hours before
                # this week's matches have actually finished airing.
                #
                # Counts TWO things, not just voltball_matches rows:
                # pairings that haven't resolved AT ALL yet (unresolved,
                # from voltball_schedule -- fixed at week-open time, so
                # it's accurate even hours before most of the week's
                # matches have been created), plus real matches that HAVE
                # resolved but haven't posted their recap yet (including
                # this one, since its own recap_posted_at isn't set until
                # the `finally` below). Counting only existing
                # voltball_matches rows (the old check) broke the moment
                # resolution stopped happening in one batch -- early in
                # the day only 1 match exists at all, so "0 still
                # pending" was true after just the FIRST game, and
                # last_standings_posted_week then blocked it from ever
                # firing again later.
                unresolved_count = count_unresolved_pairings(match["season_id"], match["week_number"])
                matches_without_recap = (
                    db.table("voltball_matches")
                    .select("id")
                    .eq("season_id", match["season_id"])
                    .eq("week_number", match["week_number"])
                    .is_("recap_posted_at", "null")
                    .execute()
                    .data
                ) or []
                still_pending_count = unresolved_count + len(matches_without_recap)

                if still_pending_count <= 1 and season_row.get("last_completed_week") != match["week_number"]:
                    db.table("voltball_seasons").update(
                        {"last_completed_week": match["week_number"]}
                    ).eq("id", match["season_id"]).execute()

                config = get_guild_config(season_row["guild_id"])
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
                # Same tagging as the kickoff post above -- coaches get
                # pinged again when their result actually lands.
                mentions = " ".join(
                    f"<@{t['owner_discord_id']}>"
                    for t in (team_a_row, team_b_row)
                    if not t.get("is_cpu") and t.get("owner_discord_id")
                )
                await channel.send(
                    content=mentions or None,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
                )

                # Standings (and a champion announcement) reveal the
                # outcome just as much as the recap does -- wins, PF/PA
                # all shift the moment they post. So they wait for the
                # SAME thing the recap waited for, plus one more
                # condition: every other pairing in this same
                # (season, week) has also fully resolved AND aired, not
                # just this one. A week with two simultaneous games
                # shouldn't have its standings spoiled by whichever game's
                # replay finishes first -- and, per the fix above, "every
                # other pairing" now correctly includes ones that haven't
                # even kicked off yet this same day, not just ones
                # already sitting in voltball_matches.
                if still_pending_count <= 1:
                    await self._post_standings_for_week(match["season_id"], match["week_number"], channel)
            except Exception as e:
                # One bad match shouldn't block the rest of the due batch,
                # and shouldn't get silently retried forever either -- log
                # and mark it posted so it doesn't wedge the loop.
                print(f"[voltball] Failed to post delayed recap for match {match.get('id')}: {e}")
            finally:
                db.table("voltball_matches").update({"recap_posted_at": now_iso}).eq("id", match["id"]).execute()

        return len(due)

    async def _post_standings_for_week(self, season_id: str, week: int, channel, force: bool = False) -> bool:
        """
        Posts the standings embed (and a champion embed, if the season
        just completed) to `channel`, gated by last_standings_posted_week
        so the automatic post_ready_recaps call and a manual
        /voltball_post_standings run can't double-post the same week.
        force=True (manual command only) bypasses that gate -- returns
        whether it actually posted.
        """
        db = get_supabase()
        full_season = db.table("voltball_seasons").select("*").eq("id", season_id).execute().data
        if not full_season:
            return False
        full_season = full_season[0]
        if not force and full_season.get("last_standings_posted_week") == week:
            return False

        if full_season["status"] == "complete":
            champ_match = (
                db.table("voltball_matches")
                .select("winner_team_id")
                .eq("season_id", season_id)
                .eq("is_playoff", True)
                .eq("week_number", week)
                .execute()
                .data
            )
            if champ_match:
                champ_team = get_team_by_id(champ_match[0]["winner_team_id"])
                if champ_team:
                    await channel.send(embed=build_champion_embed(full_season, champ_team["team_name"]))
        standings_rows = get_standings(season_id)
        await channel.send(embed=build_standings_embed(full_season, standings_rows))
        db.table("voltball_seasons").update({"last_standings_posted_week": week}).eq("id", season_id).execute()
        return True

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

"""
voltball_embeds.py
--------------------
Broadcast-style embeds for match results and standings. Kept separate
from voltball_cog.py so the same builders serve both the weekly
auto-post and the on-demand /voltball_standings command without
duplicating formatting logic.
"""

import discord
from voltball_engine import tempo_label

QUARTER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]


def _coach_label(hero_type: str | None) -> str:
    """'No Coach' for a Hero-less team — same label used site-wide (website + Discord)."""
    return hero_type if hero_type else "No Coach"


def _tempo_display(lineup: dict) -> str:
    """'6.5 (Standard)' — numeric value plus human bucket, defaulting for pre-Tempo rows."""
    t = lineup.get("tempo", 5.5)
    return f"{t:g} ({tempo_label(t)})"


def build_match_embed(result: dict, week: int, is_playoff: bool = False) -> discord.Embed:
    """
    Broadcast-style embed for a single resolved match: final score,
    quarter-by-quarter progression, and a condensed highlight reel pulled
    from the match log (random events + coach signature plays only —
    the routine per-quarter running-total lines are left out here since
    the full log_text is still available via the match record if someone
    wants the play-by-play).
    """
    team_a, team_b = result["team_a"], result["team_b"]
    score_a, score_b = result["score_a"], result["score_b"]
    winner = result["winner"]

    title_prefix = "🏆 PLAYOFF MATCH" if is_playoff else "🏈 Voltball"
    embed = discord.Embed(
        title=f"{title_prefix} — Week {week}",
        description=f"**{team_a} {score_a} — {score_b} {team_b}**\n🏆 **{winner}** wins!",
        color=discord.Color.gold() if is_playoff else discord.Color.blue(),
    )

    quarter_lines = []
    for q in result["quarters"]:
        emoji = QUARTER_EMOJI[q["quarter"] - 1] if q["quarter"] <= 4 else f"Q{q['quarter']}"
        quarter_lines.append(f"{emoji} {q['team_a_voltage']} — {q['team_b_voltage']}")
    embed.add_field(name="Quarter-by-Quarter", value="\n".join(quarter_lines), inline=True)

    highlights = [
        line.strip() for line in result["log"]
        if any(marker in line for marker in ["⚡", "❄️", "🎭", "📈", "🛑", "🥊", "🦸", "📋", "🤕", "🐊", "TIE"])
    ]
    if highlights:
        # Cap length so one wild match doesn't blow past Discord's field limit
        highlight_text = "\n".join(highlights[:10])
        if len(highlight_text) > 1000:
            highlight_text = highlight_text[:1000] + "\n…"
        embed.add_field(name="Highlights", value=highlight_text, inline=False)

    return embed


POSITION_ORDER = ["QB", "Striker", "Mid", "Guard"]


def _format_roster_names(lineup: dict) -> str:
    """Formats a lineup's assignments as 'Striker: name, name, name' lines."""
    lines = []
    for pos in POSITION_ORDER:
        entries = lineup["assignments"].get(pos, [])
        names = [e["name"] if isinstance(e, dict) else str(e) for e in entries]
        lines.append(f"**{pos}:** {', '.join(names) if names else '—'}")
    return "\n".join(lines)


def build_lineups_embed(season: dict, week: int, week_lineups: list[dict]) -> discord.Embed:
    """
    Public scouting report — every team's formation and roster for the
    current week, visible to the whole server as soon as it's submitted
    (not hidden until matches resolve). Teams that haven't set a lineup
    yet still show up, flagged as pending.

    Discord hard-caps embeds at 25 fields, and this adds one field per
    team — capped at MAX_FIELDS with a "+N more, see the site" note if
    exceeded, rather than letting a big season silently throw a Discord
    API error. scout.html has no such limit, so it stays the authoritative
    full list regardless of season size; this command is a quick-glance
    convenience, not the only place to check.
    """
    MAX_FIELDS = 24  # leaves room for the "+N more" field itself

    embed = discord.Embed(
        title=f"📋 Week {week} Lineups — {season['name']}",
        description="Every coach's formation and roster this week — set before the deadline, visible the moment it's locked in.",
        color=discord.Color.blurple(),
    )

    shown, overflow = week_lineups[:MAX_FIELDS], week_lineups[MAX_FIELDS:]

    for entry in shown:
        lineup = entry["lineup"]
        coach = _coach_label(entry["hero_type"])
        if entry.get("is_cpu"):
            embed.add_field(name=f"🤖 {entry['team_name']} ({coach})", value="*CPU team — fields a fresh random roster at resolution time.*", inline=False)
            continue
        if not lineup:
            embed.add_field(name=f"⏳ {entry['team_name']} ({coach})", value="*No lineup submitted yet.*", inline=False)
            continue
        tempo = _tempo_display(lineup)
        embed.add_field(
            name=f"🏈 {entry['team_name']} ({coach}) — {lineup['formation']} / {tempo}",
            value=_format_roster_names(lineup),
            inline=False,
        )

    if overflow:
        embed.add_field(
            name=f"+ {len(overflow)} more team{'s' if len(overflow) > 1 else ''}",
            value="See the full breakdown on Scout — no size limit there.",
            inline=False,
        )

    return embed


def build_matchup_preview_embed(season: dict, week: int, pairings: list[dict],
                                  team_lookup: dict, lineup_lookup: dict, round_label: str | None = None) -> discord.Embed:
    """
    "This Week's Matchups" — posted automatically once the weekly
    deadline passes and lineups lock, BEFORE match results are resolved.
    Shows each pairing's formation side by side so the reveal has real
    stakes, same spirit as a real fantasy league's pre-game matchup card.

    team_lookup: {team_id: team_row}
    lineup_lookup: {team_id: lineup_row or None}
    round_label: "Semifinal" / "Championship" for playoff rounds, None
    for a regular-season week — swaps the title so a playoff round reads
    as the occasion it is, not just "Week 17."
    """
    title = f"🏆 {round_label}" if round_label else f"⚔️ Week {week} Matchups"
    embed = discord.Embed(
        title=title,
        description="Lineups are locked — here's who's facing who.",
        color=discord.Color.gold() if round_label else discord.Color.orange(),
    )

    for pairing in pairings:
        team_a = team_lookup[pairing["team_a_id"]]
        team_b = team_lookup[pairing["team_b_id"]]
        lineup_a = lineup_lookup.get(pairing["team_a_id"])
        lineup_b = lineup_lookup.get(pairing["team_b_id"])

        if team_a.get("is_cpu"):
            form_a = "CPU — random roster/formation/tempo"
        else:
            form_a = f"{lineup_a['formation']} / {_tempo_display(lineup_a)}" if lineup_a else "No lineup (auto-fielded, penalized)"

        if team_b.get("is_cpu"):
            form_b = "CPU — random roster/formation/tempo"
        else:
            form_b = f"{lineup_b['formation']} / {_tempo_display(lineup_b)}" if lineup_b else "No lineup (auto-fielded, penalized)"

        embed.add_field(
            name=f"{team_a['team_name']} vs {team_b['team_name']}",
            value=f"**{team_a['team_name']}** ({_coach_label(team_a['hero_type'])}): {form_a}\n**{team_b['team_name']}** ({_coach_label(team_b['hero_type'])}): {form_b}",
            inline=False,
        )

    return embed


def build_champion_embed(season: dict, champion_name: str) -> discord.Embed:
    """
    Posted once, the moment the championship game resolves — distinct
    from the routine weekly standings post, since "we just crowned a
    champion" deserves its own announcement, not just another line in
    an ordinary-looking standings table that happens to be the last one.
    """
    embed = discord.Embed(
        title=f"🏆 {season['name']} Champion",
        description=f"# 👑 {champion_name}\n\nCongratulations — the season is complete!",
        color=discord.Color.gold(),
    )
    return embed


def build_standings_embed(season: dict, rows: list[dict]) -> discord.Embed:
    """
    Standings embed — same builder used by both the weekly auto-post
    (after a resolution batch) and the on-demand /voltball_standings command.
    """
    if not rows:
        return discord.Embed(title=f"🏆 {season['name']} Standings", description="No standings yet.", color=discord.Color.gold())

    lines = []
    for i, r in enumerate(rows, start=1):
        streak = r.get("streak", 0)
        streak_str = f"W{streak}" if streak > 0 else (f"L{abs(streak)}" if streak < 0 else "—")
        lines.append(
            f"{i}. **{r['team_name']}** — {r['wins']}-{r['losses']} "
            f"({round(r['points_for'], 1)} PF / {round(r['points_against'], 1)} PA) [{streak_str}]"
        )

    status_label = {"upcoming": "Not Started", "active": "In Progress", "playoffs": "Playoffs", "complete": "Final"}.get(season["status"], season["status"])
    embed = discord.Embed(
        title=f"🏆 {season['name']} Standings",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Week {season['current_week']} of {season['week_count']} · {status_label}")
    return embed

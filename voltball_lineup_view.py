"""
voltball_lineup_view.py
------------------------
Interactive picker for /voltball_lineup: one multi-select dropdown per
position (sized to the chosen formation's counts), built from the
coach's LIVE held Zappies, with Prev/Next pagination if they hold more
than 25 (Discord's per-select option cap).

PAGINATION DESIGN NOTE: Discord select menus are exact-count-per-submission
— each time a select fires, `self.values` is that submission's full pick,
not merged with previous picks. That's fine with no pagination (all
options fit in one page, pick-once-and-done), but breaks once someone
needs to choose across multiple pages of options. So paging here works
by ACCUMULATION instead of replace: each select's max_values is capped
to however many slots remain unfilled for that position, and every
submission ADDS to the running total rather than overwriting it. Already-
selected Zappies (in any position) are excluded from later pages' option
lists so the same Zappy can't be picked twice. A "Clear All" button is
included since accumulation makes mistakes harder to undo any other way.
"""

import discord
from voltball_engine import FORMATIONS, POSITIONS
from voltball_lineup_service import submit_lineup, LineupValidationError
from voltball_position_fit import POSITION_STAT

PER_PAGE = 25


class _PositionSelect(discord.ui.Select):
    def __init__(self, position: str, options: list[discord.SelectOption], max_values: int):
        self.position = position
        theme = {"Striker": "VLT offense", "Mid": "SPK playmaking", "Guard": "INS defense"}[position]
        super().__init__(
            placeholder=f"Add to {position} ({theme}) — {max_values} slot(s) left",
            min_values=1,
            max_values=max_values,
            options=options,
            custom_id=f"voltball_pos_{position}",
            disabled=(len(options) == 0),
        )

    async def callback(self, interaction: discord.Interaction):
        view: LineupPickerView = self.view
        newly_picked = [int(v) for v in self.values]
        remaining = view.needed[self.position] - len(view.selections[self.position])
        for aid in newly_picked[:remaining]:
            view.selections[self.position].add(aid)
        await view._refresh(interaction)


class _NavButton(discord.ui.Button):
    def __init__(self, label: str, delta: int, style=discord.ButtonStyle.secondary):
        super().__init__(label=label, style=style)
        self.delta = delta

    async def callback(self, interaction: discord.Interaction):
        view: LineupPickerView = self.view
        view.page = max(0, min(view.max_page, view.page + self.delta))
        await view._refresh(interaction)


class _ClearButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Clear All", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        view: LineupPickerView = self.view
        for pos in POSITIONS:
            view.selections[pos].clear()
        await view._refresh(interaction)


class _SubmitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Lock In Lineup", style=discord.ButtonStyle.success, disabled=True)

    async def callback(self, interaction: discord.Interaction):
        view: LineupPickerView = self.view
        await interaction.response.defer(ephemeral=True)

        position_map = {aid: pos for pos, ids in view.selections.items() for aid in ids}

        try:
            await submit_lineup(
                guild_id=view.guild_id,
                team_id=view.team_id,
                season_id=view.season_id,
                week_number=view.week_number,
                formation=view.formation,
                position_map=position_map,
                wallet_address=view.wallet_address,
            )
        except LineupValidationError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return

        for item in view.children:
            item.disabled = True
        # NOTE: interaction.message.edit() fails with a 404 "Unknown Message"
        # after response.defer(ephemeral=True) — an ephemeral/component
        # interaction's message can only be edited through the interaction's
        # own webhook, not a raw message.edit() call. edit_original_response()
        # is the correct method here.
        await interaction.edit_original_response(view=view)
        await interaction.followup.send(f"✅ Lineup locked in — running **{view.formation}** this week.", ephemeral=True)


class LineupPickerView(discord.ui.View):
    def __init__(self, guild_id: str, team_id: str, season_id: str, week_number: int,
                 formation: str, wallet_address: str, held_zappies: list[dict], user_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.team_id = team_id
        self.season_id = season_id
        self.week_number = week_number
        self.formation = formation
        self.wallet_address = wallet_address
        self.held_zappies = held_zappies
        self.user_id = user_id
        self.page = 0
        self.max_page = max(0, (len(held_zappies) - 1) // PER_PAGE)
        self.needed = FORMATIONS[formation]
        self.selections: dict[str, set] = {pos: set() for pos in POSITIONS}

        self._build_components()

    def interaction_check_user(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your lineup to set.", ephemeral=True)
            return False
        return True

    def _build_components(self):
        self.clear_items()

        already_used = set().union(*self.selections.values())
        page_slice = self.held_zappies[self.page * PER_PAGE: (self.page + 1) * PER_PAGE]
        page_available = [z for z in page_slice if z["asset_id"] not in already_used]

        for position, count in self.needed.items():
            remaining = count - len(self.selections[position])
            if remaining <= 0:
                continue  # this position is already full — no select needed
            # Sort this position's candidates by the stat it actually cares
            # about (VLT for Striker, SPK for Mid, INS for Guard), highest
            # first — was previously left in wallet-return order, so the
            # best fits for a given position could be buried anywhere.
            stat_key = POSITION_STAT[position]
            sorted_for_position = sorted(page_available, key=lambda z: z[stat_key], reverse=True)
            options = [
                discord.SelectOption(
                    label=z["name"][:100],
                    description=f"VLT {z['VLT']} · INS {z['INS']} · SPK {z['SPK']}",
                    value=str(z["asset_id"]),
                )
                for z in sorted_for_position[:25]
            ]
            max_values = min(remaining, len(options)) if options else 1
            self.add_item(_PositionSelect(position, options, max_values))

        if self.max_page > 0:
            self.add_item(_NavButton("◀ Prev", -1))
            self.add_item(_NavButton("Next ▶", 1))

        self.add_item(_ClearButton())

        self.submit_button = _SubmitButton()
        total_selected = sum(len(v) for v in self.selections.values())
        total_needed = sum(self.needed.values())
        self.submit_button.disabled = total_selected != total_needed
        self.add_item(self.submit_button)

    async def _refresh(self, interaction: discord.Interaction):
        self._build_components()
        total_selected = sum(len(v) for v in self.selections.values())
        total_needed = sum(self.needed.values())
        page_info = f" (page {self.page + 1}/{self.max_page + 1})" if self.max_page > 0 else ""
        breakdown = " · ".join(f"{pos} {len(self.selections[pos])}/{count}" for pos, count in self.needed.items())
        await interaction.response.edit_message(
            content=f"**{self.formation}** formation{page_info} — {total_selected}/{total_needed} assigned ({breakdown}).",
            view=self,
        )


def build_lineup_picker(guild_id: str, team_id: str, season_id: str, week_number: int,
                         formation: str, wallet_address: str, held_zappies: list[dict],
                         user_id: int) -> tuple[str, "LineupPickerView"]:
    """
    Convenience constructor — returns (initial_message_content, view).

    Sorts held_zappies by each Zappy's single strongest stat (its best
    fit for ANY position) before pagination, so if someone holds more
    than 25 Zappies, the most broadly useful ones land on page 1 instead
    of whatever order the wallet API happened to return. This is an
    approximation, not a per-position sort (a Zappy's best-overall stat
    might not be the one a specific position needs) — within any given
    page, each position's own dropdown is re-sorted by its own relevant
    stat (see LineupPickerView._build_components), so the practical
    effect is: strong Zappies surface early, and whichever page they're
    on, each dropdown still ranks them correctly for that position.
    """
    total = sum(FORMATIONS[formation].values())
    ranked = sorted(held_zappies, key=lambda z: max(z["VLT"], z["INS"], z["SPK"]), reverse=True)
    view = LineupPickerView(guild_id, team_id, season_id, week_number, formation, wallet_address, ranked, user_id)
    page_info = f" (page 1/{view.max_page + 1})" if view.max_page > 0 else ""
    warning = f"\n⚠️ You hold {len(ranked)} Zappies — use Prev/Next to see them all." if view.max_page > 0 else ""
    content = f"**{formation}** formation{page_info} — 0/{total} assigned.{warning}"
    return content, view

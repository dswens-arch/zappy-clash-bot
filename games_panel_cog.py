"""
games_panel_cog.py
------------------
Posts and manages the persistent games panel in the games channel.

Usage:
  Admin runs /postgamespanel in the games channel once.
  The panel message persists with buttons for each available game.
  Buttons trigger ephemeral game sessions — the panel itself never goes away.

Current games:
  🎨 Hue Hunt — solo color-matching puzzle
  ⚡ Zap Word  — Wordle-style word game
"""

import discord
from discord.ext import commands
from discord import app_commands
import os

GAMES_CHANNEL_ID = int(os.environ.get("GAMES_CHANNEL_ID", 0))


class GamesPanelView(discord.ui.View):
    """
    Persistent view — timeout=None means buttons survive bot restarts
    as long as the cog re-registers this view on startup via
    bot.add_view(GamesPanelView(bot)).
    """
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="🎨 Hue Hunt",
        style=discord.ButtonStyle.primary,
        custom_id="games_panel:hue_hunt"
    )
    async def hue_hunt_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.cogs.get("HueHuntCog")
        if not cog:
            await interaction.response.send_message(
                "Hue Hunt isn't available right now. Try again later.",
                ephemeral=True
            )
            return
        embed, view, file = await cog.build_round(1, interaction.user.id, new_game=True)
        await interaction.response.send_message(embed=embed, view=view, file=file, ephemeral=True)

    @discord.ui.button(
        label="⚡ Zap Word",
        style=discord.ButtonStyle.primary,
        custom_id="games_panel:zap_word"
    )
    async def zap_word_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.cogs.get("ZapWordCog")
        if not cog:
            await interaction.response.send_message(
                "Zap Word isn't available right now. Try again later.",
                ephemeral=True
            )
            return
        _state, embed, view = cog.new_game(interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(
        label="🔢 Sudoku",
        style=discord.ButtonStyle.primary,
        custom_id="games_panel:sudoku"
    )
    async def sudoku_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.cogs.get("SudokuCog")
        if not cog:
            await interaction.response.send_message(
                "Sudoku isn't available right now. Try again later.",
                ephemeral=True
            )
            return
        embed, view, file = await cog.start_game(interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, file=file, ephemeral=True)

    @discord.ui.button(
        label="🎲 Zapzee",
        style=discord.ButtonStyle.primary,
        custom_id="games_panel:zapzee"
    )
    async def zapzee_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.cogs.get("ZapzeeCog")
        if not cog:
            await interaction.response.send_message(
                "Zapzee isn't available right now. Try again later.",
                ephemeral=True
            )
            return
        embed, view, file = await cog.start_game(interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, file=file, ephemeral=True)


class GamesPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Re-register the persistent view so buttons work after restarts
        bot.add_view(GamesPanelView(bot))

    @app_commands.command(
        name="postgamespanel",
        description="(Admin) Post the persistent games panel to this channel."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def postgamespanel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="⚡ Zappies Game Room",
            description=(
                "Pick a game below to play. All games are just for you — "
                "no one else sees your session.\n\n"
                "🎨 **Hue Hunt** — Match colors as they get harder. "
                "Earn ZAPP for every round you survive.\n\n"
                "⚡ **Zap Word** — Guess the hidden 5-letter word in 6 tries. "
                "Earn ZAPP for solving it fast.\n\n"
                "🔢 **Sudoku** — Solve a 9x9 puzzle. Earn ZAPP for clean solves.\n\n"
                "🎲 **Zapzee** — Roll 5 dice, build the best scorecard over 13 rounds. Earn ZAPP for high scores.\n\n"
                "More games coming soon."
            ),
            color=0x3A86FF
        )
        embed.set_footer(text="Solo games · Earn ZAPP · No pressure")

        channel = interaction.guild.get_channel(GAMES_CHANNEL_ID) or interaction.channel
        await channel.send(embed=embed, view=GamesPanelView(self.bot))
        await interaction.followup.send("Games panel posted!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GamesPanelCog(bot))

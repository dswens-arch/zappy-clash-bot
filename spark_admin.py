"""
spark_admin.py
--------------
Admin-only cog for testing the Spark companion system before launch.
All commands restricted to the authorized admin user and post to a
private test channel.

Commands:
  /spark-test-battle  — Simulate a Clash battle with a Spark equipped (no DB writes)
  /spark-set-xp       — Manually set a Spark's XP (for upgrade threshold testing)
  /spark-reset-xp     — Zero out a Spark's XP and reset tier to 1
  /spark-rollback     — Push T1 metadata back to a Spark ASA on-chain
  /spark-status       — Show current DB state for a Spark ASA
"""

import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
ADMIN_USER_ID   = 652930531935125506
TEST_CHANNEL_ID = 1520134630173315244

# T1 image CIDs for rollback — one per type
T1_CIDS = {
    "zolt":   "bafkreign5ydt5zj4mltsays47lsp7d6stdzje756zvzehzx7tryqxmqv6q",
    "scorch": "bafkreihpdkwbg6gvn4zutkohrmig67zalqtprg4wln624zkukw5nlo4f3q",
    "jinx":   "bafkreierm7ti75i2hdpngztupgqpr5w7lbjull262outivdaanz4lshd7u",
    "moss":   "bafkreib2iprbja2zhfdxflulcgqyav2m5i4z5behc3n6kt4qglus36dqqm",
    "glitch": "bafkreidtjpiu4k44u5soltmqsfo6oyykef2p7mytw4jte6tn47dedrwhye",
    "null":   "bafkreif7otb4ifgcgkqqsvxu3jgxe62yrvdwrgnbe37b5vwr4l5ypnmnpe",
}


# ─────────────────────────────────────────────
# Auth guard
# ─────────────────────────────────────────────
def is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id == ADMIN_USER_ID


async def admin_check(interaction: discord.Interaction) -> bool:
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return False
    return True


# ─────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────
class SparkAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _test_channel(self) -> discord.TextChannel | None:
        return self.bot.get_channel(TEST_CHANNEL_ID)

    async def _send(self, interaction: discord.Interaction, content: str = None, embed: discord.Embed = None):
        """Send output to the test channel."""
        ch = self._test_channel()
        if ch:
            if embed:
                await ch.send(content=content, embed=embed)
            else:
                await ch.send(content)
        # Always respond to the interaction
        try:
            await interaction.followup.send("✅ Output sent to test channel.", ephemeral=True)
        except Exception:
            pass

    # ──────────────────────────────────────────
    # /spark-test-battle
    # ──────────────────────────────────────────
    @app_commands.command(name="spark-test-battle", description="[Admin] Simulate a Spark battle — no DB writes")
    @app_commands.describe(
        zappy_a_asa="ASA ID of fighter A",
        zappy_b_asa="ASA ID of fighter B",
        spark_type="Spark type to equip on fighter A (zolt/scorch/jinx/moss/glitch/null)",
        spark_tier="Spark tier (1, 2, or 3)",
    )
    async def spark_test_battle(
        self,
        interaction: discord.Interaction,
        zappy_a_asa: int,
        zappy_b_asa: int,
        spark_type: str,
        spark_tier: int = 1,
    ):
        if not await admin_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        try:
            from algorand_lookup import get_zappy_for_battle
            from battle_engine import build_fighter, resolve_battle

            # Fetch both Zappies
            zappy_a = await asyncio.to_thread(get_zappy_for_battle, zappy_a_asa)
            zappy_b = await asyncio.to_thread(get_zappy_for_battle, zappy_b_asa)

            if not zappy_a:
                return await interaction.followup.send(f"❌ Zappy A ({zappy_a_asa}) not found.", ephemeral=True)
            if not zappy_b:
                return await interaction.followup.send(f"❌ Zappy B ({zappy_b_asa}) not found.", ephemeral=True)

            # Build fighters
            fighter_a = build_fighter(zappy_a)
            fighter_b = build_fighter(zappy_b)

            # Equip Spark on fighter A
            spark_type = spark_type.lower().strip()
            if spark_type not in T1_CIDS:
                return await interaction.followup.send(
                    f"❌ Invalid spark type. Must be one of: {', '.join(T1_CIDS.keys())}", ephemeral=True
                )
            fighter_a.spark_type     = spark_type
            fighter_a.spark_tier     = max(1, min(3, spark_tier))
            fighter_a.spark_asset_id = 0  # Test — no real ASA needed

            # Run battle — NO DB writes
            result = resolve_battle(fighter_a, fighter_b)

            # Build output embed
            winner = result["winner"]
            log    = result["log_text"]

            embed = discord.Embed(
                title=f"🧪 TEST BATTLE — {fighter_a.display_name} vs {fighter_b.display_name}",
                color=0xA855F7,
            )
            embed.add_field(
                name="Fighter A",
                value=f"**{fighter_a.display_name}** · VLT {zappy_a['stats']['VLT']} · INS {zappy_a['stats']['INS']} · SPK {zappy_a['stats']['SPK']}\nSpark: **{spark_type.capitalize()} T{spark_tier}**",
                inline=True,
            )
            embed.add_field(
                name="Fighter B",
                value=f"**{fighter_b.display_name}** · VLT {zappy_b['stats']['VLT']} · INS {zappy_b['stats']['INS']} · SPK {zappy_b['stats']['SPK']}\nNo Spark",
                inline=True,
            )
            embed.add_field(
                name="Result",
                value=f"🏆 **{winner['name']}** wins\nSpark triggered: {'✅' if result.get('spark_a_triggered') else '❌'}",
                inline=False,
            )
            embed.add_field(name="Battle Log", value=log[:1000] if len(log) > 1000 else log, inline=False)
            embed.set_footer(text="TEST ONLY — nothing saved to DB")

            await self._send(interaction, embed=embed)

            # If log is long, send remainder as plain text
            if len(log) > 1000:
                ch = self._test_channel()
                if ch:
                    await ch.send(f"```{log[1000:2000]}```")

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            raise

    # ──────────────────────────────────────────
    # /spark-status
    # ──────────────────────────────────────────
    @app_commands.command(name="spark-status", description="[Admin] Show current DB state for a Spark ASA")
    @app_commands.describe(asset_id="The Spark ASA ID to inspect")
    async def spark_status(self, interaction: discord.Interaction, asset_id: int):
        if not await admin_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        try:
            from database import get_spark
            spark = await asyncio.to_thread(get_spark, asset_id)

            if not spark:
                return await interaction.followup.send(f"❌ Spark ASA {asset_id} not found in DB.", ephemeral=True)

            embed = discord.Embed(
                title=f"🔍 Spark Status — {spark['name']}",
                color=0x60a5fa,
            )
            embed.add_field(name="ASA ID",      value=str(spark["asset_id"]),  inline=True)
            embed.add_field(name="Type",         value=spark["spark_type"].capitalize(), inline=True)
            embed.add_field(name="Tier",         value=f"T{spark['tier']}",    inline=True)
            embed.add_field(name="XP",           value=str(spark["xp"]),       inline=True)
            embed.add_field(name="Wallet",       value=spark.get("wallet") or "unclaimed", inline=True)
            embed.add_field(name="Discord User", value=spark.get("discord_user_id") or "—", inline=True)
            embed.add_field(name="Reserve",      value=f"`{spark.get('reserve_address', '—')[:30]}...`", inline=False)

            # XP progress bar
            thresholds = {1: 1000, 2: 5000}
            tier = spark["tier"]
            xp   = spark["xp"]
            if tier < 3:
                threshold = thresholds[tier]
                pct = min(100, int((xp / threshold) * 100))
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                embed.add_field(
                    name=f"Progress to T{tier + 1}",
                    value=f"`{bar}` {xp}/{threshold} XP ({pct}%)",
                    inline=False,
                )
            else:
                embed.add_field(name="Progress", value="✅ Max tier reached", inline=False)

            ch = self._test_channel()
            if ch:
                await ch.send(embed=embed)
            await interaction.followup.send("✅ Output sent to test channel.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ──────────────────────────────────────────
    # /spark-set-xp
    # ──────────────────────────────────────────
    @app_commands.command(name="spark-set-xp", description="[Admin] Set a Spark's XP to a specific value")
    @app_commands.describe(
        asset_id="The Spark ASA ID",
        xp="XP value to set",
    )
    async def spark_set_xp(self, interaction: discord.Interaction, asset_id: int, xp: int):
        if not await admin_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        try:
            from database import get_supabase
            db = get_supabase()

            result = await asyncio.to_thread(
                lambda: db.table("spark_holdings")
                .update({"xp": xp})
                .eq("asset_id", asset_id)
                .execute()
            )

            if not result.data:
                return await interaction.followup.send(f"❌ Spark ASA {asset_id} not found.", ephemeral=True)

            ch = self._test_channel()
            if ch:
                await ch.send(f"🔧 **[TEST]** Spark `{asset_id}` XP set to **{xp}**")
            await interaction.followup.send("✅ Done.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ──────────────────────────────────────────
    # /spark-reset-xp
    # ──────────────────────────────────────────
    @app_commands.command(name="spark-reset-xp", description="[Admin] Reset a Spark's XP to 0 and tier to 1")
    @app_commands.describe(asset_id="The Spark ASA ID to reset")
    async def spark_reset_xp(self, interaction: discord.Interaction, asset_id: int):
        if not await admin_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        try:
            from database import get_supabase
            db = get_supabase()

            result = await asyncio.to_thread(
                lambda: db.table("spark_holdings")
                .update({"xp": 0, "tier": 1, "upgraded_at": None})
                .eq("asset_id", asset_id)
                .execute()
            )

            if not result.data:
                return await interaction.followup.send(f"❌ Spark ASA {asset_id} not found.", ephemeral=True)

            ch = self._test_channel()
            if ch:
                await ch.send(f"🔄 **[TEST]** Spark `{asset_id}` reset — XP: 0, Tier: T1")
            await interaction.followup.send("✅ Done.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ──────────────────────────────────────────
    # /spark-rollback
    # ──────────────────────────────────────────
    @app_commands.command(name="spark-rollback", description="[Admin] Push T1 metadata back to a Spark ASA on-chain")
    @app_commands.describe(asset_id="The Spark ASA ID to roll back to T1")
    async def spark_rollback(self, interaction: discord.Interaction, asset_id: int):
        if not await admin_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        try:
            from supabase import create_client
            from database import get_spark

            spark = await asyncio.to_thread(get_spark, asset_id)
            if not spark:
                return await interaction.followup.send(f"❌ Spark ASA {asset_id} not found in DB.", ephemeral=True)

            spark_type = spark["spark_type"]
            t1_cid     = T1_CIDS.get(spark_type)
            if not t1_cid:
                return await interaction.followup.send(f"❌ No T1 CID configured for type '{spark_type}'.", ephemeral=True)

            ch = self._test_channel()
            if ch:
                await ch.send(f"🔄 **[TEST]** Rolling back Spark `{asset_id}` ({spark_type}) to T1 on-chain...")

            # Push ARC-19 update back to T1 CID
            success = await asyncio.to_thread(
                _push_arc19_to_cid, asset_id, t1_cid
            )

            # Reset DB to T1
            from database import get_supabase
            db = get_supabase()
            await asyncio.to_thread(
                lambda: db.table("spark_holdings")
                .update({"tier": 1, "xp": 0, "upgraded_at": None})
                .eq("asset_id", asset_id)
                .execute()
            )

            if ch:
                if success:
                    await ch.send(f"✅ Spark `{asset_id}` rolled back to T1 — on-chain and in DB.")
                else:
                    await ch.send(f"⚠️ DB reset to T1 but on-chain update failed — check Railway logs.")

            await interaction.followup.send("✅ Done.", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            raise


# ─────────────────────────────────────────────
# ARC-19 push helper (raw CID version for rollback)
# ─────────────────────────────────────────────
def _push_arc19_to_cid(asset_id: int, cid: str) -> bool:
    """
    Push an ARC-19 update using a raw CID string.
    Used for rollback — pushes T1 CID back to the ASA.
    """
    try:
        import base64
        from algosdk import mnemonic, account
        from algosdk.v2client import algod
        from algosdk.transaction import AssetConfigTxn, wait_for_confirmation

        # Derive reserve address from CID
        cid_stripped = cid.lstrip("b")
        cid_bytes    = base64.b32decode(
            cid_stripped.upper() + "=" * (-len(cid_stripped) % 8)
        )
        digest = cid_bytes[4:][:32].ljust(32, b"\x00")
        new_reserve = account.address_from_public_key(digest)

        # Connect
        algod_token   = os.environ.get("ALGOD_TOKEN", "")
        algod_address = "https://mainnet-api.algonode.cloud"
        headers       = {"X-Algo-API-Token": algod_token} if algod_token else {}
        algod_client  = algod.AlgodClient(algod_token, algod_address, headers=headers)

        # Sign
        manager_mnemonic = os.environ["SPARK_MANAGER_MNEMONIC"]
        private_key      = mnemonic.to_private_key(manager_mnemonic)
        manager_address  = account.address_from_private_key(private_key)

        asset_info = algod_client.asset_info(asset_id)
        params     = asset_info["params"]

        sp  = algod_client.suggested_params()
        txn = AssetConfigTxn(
            sender   = manager_address,
            sp       = sp,
            index    = asset_id,
            manager  = manager_address,
            reserve  = new_reserve,
            freeze   = params.get("freeze"),
            clawback = params.get("clawback"),
            strict_empty_address_check=False,
        )
        signed = txn.sign(private_key)
        tx_id  = algod_client.send_transaction(signed)
        wait_for_confirmation(algod_client, tx_id, 4)

        print(f"[SPARK ROLLBACK] ASA {asset_id} → T1 | tx {tx_id}")
        return True

    except Exception as e:
        print(f"[SPARK ROLLBACK] Failed for ASA {asset_id}: {e}")
        return False


# ─────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────
async def setup(bot: commands.Bot):
    await bot.add_cog(SparkAdminCog(bot))

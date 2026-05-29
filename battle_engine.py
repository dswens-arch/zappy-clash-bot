"""
battle_engine.py
----------------
Resolves a Zappy Clash battle between two Zappies.
Returns a full play-by-play log with Discord-ready flavor text.

Three rounds. Both Zappies attack each round.
First to 0 HP loses. Ties go to the higher VLT Zappy.
"""

import random
from dataclasses import dataclass, field
from typing import Optional

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
STARTING_HP     = 100
CRIT_MULTIPLIER = 2.0    # Default crit = 2x damage
ROUNDS          = 3


@dataclass
class Fighter:
    """Represents one Zappy in a battle."""
    asset_id:    int
    name:        str
    unit_name:   str
    VLT:         int
    INS:         int
    SPK:         int
    image_url:   str            = ""
    ability:     Optional[dict] = None
    combo:       Optional[str]  = None
    is_hero:     bool           = False
    is_collab:   bool           = False
    hero_type:   Optional[str]  = None
    collab_type: Optional[str]  = None

    # Battle state
    hp:              int   = field(default=STARTING_HP, init=False)
    crit_multiplier: float = field(default=CRIT_MULTIPLIER, init=False)
    ability_used:    bool  = field(default=False, init=False)
    survived_zero:   bool  = field(default=False, init=False)   # Nine Lives tracker
    iron_shell_used:    bool  = field(default=False, init=False)   # Iron Shell one-time shield tracker
    skip_next_attack:   bool  = field(default=False, init=False)   # Royal Decree skip flag
    shield_active:      bool  = field(default=False, init=False)   # Divine Shield block flag

    @property
    def display_name(self) -> str:
        return self.name or self.unit_name

    @property
    def crit_chance(self) -> float:
        """SPK / 5 = crit % per round roll (SPK 100 = 20% chance)."""
        return self.SPK / 500.0


def build_fighter(zappy_data: dict) -> Fighter:
    """Build a Fighter from the output of algorand_lookup.get_zappy_for_battle()"""
    stats = zappy_data.get("stats", {})
    return Fighter(
        asset_id   = zappy_data["asset_id"],
        name       = zappy_data.get("name", ""),
        image_url  = zappy_data.get("image_url", ""),
        unit_name  = zappy_data.get("unit_name", ""),
        VLT        = stats.get("VLT", 50),
        INS        = stats.get("INS", 50),
        SPK        = stats.get("SPK", 50),
        ability    = stats.get("ability"),
        combo      = stats.get("combo"),
        is_hero    = zappy_data.get("is_hero", False),
        is_collab  = zappy_data.get("is_collab", False),
        hero_type  = zappy_data.get("hero_type"),
        collab_type= zappy_data.get("collab_type"),
    )


def calculate_damage(attacker: Fighter, defender: Fighter, round_num: int) -> tuple[int, bool, str]:
    """
    Calculate damage dealt by attacker to defender.
    Returns: (damage, is_crit, flavor_note)
    """
    # Base damage: VLT * roll modifier (0.8 - 1.2) - defender INS reduction
    roll = random.uniform(0.8, 1.2)
    raw_damage = attacker.VLT * roll
    ins_reduction = defender.INS * 0.3   # INS reduces ~30% of damage
    damage = max(1, raw_damage - ins_reduction)

    # Crit check
    is_crit = random.random() < attacker.crit_chance
    flavor_note = ""

    if is_crit:
        damage *= attacker.crit_multiplier
        flavor_note = "CRITICAL SURGE"

    return int(damage), is_crit, flavor_note


def apply_ability(fighter: Fighter, opponent: Fighter, round_num: int) -> tuple[bool, str]:
    """
    Try to trigger a fighter's special ability.
    Returns (triggered, message)
    """
    ability = fighter.ability
    if not ability or fighter.ability_used:
        return False, ""

    # Guard against legacy string format
    if isinstance(ability, str):
        return False, ""

    trigger = ability.get("trigger_round")

    # Determine if this round triggers the ability
    should_trigger = False
    if trigger == round_num:
        should_trigger = True
    elif trigger == "random" and random.random() < 0.40:   # 40% chance per round
        should_trigger = True
    elif trigger == "passive":
        should_trigger = True   # Passive abilities always active, handled elsewhere

    if not should_trigger:
        return False, ""

    fighter.ability_used = True
    name = ability["name"]
    desc = ability["desc"]

    # Apply ability effects
    if name == "Inferno Surge":
        fighter.VLT = min(100, fighter.VLT * 2)
        return True, f"🔥 **INFERNO SURGE!** {fighter.display_name}'s VLT doubles this round!"

    elif name == "Divine Shield":
        fighter.shield_active = True
        return True, f"😇 **DIVINE SHIELD!** {fighter.display_name} blocks all incoming damage this round!"

    elif name == "Soul Deal":
        steal = 10
        opponent.INS = max(10, opponent.INS - steal)
        fighter.INS = min(100, fighter.INS + steal)
        return True, f"😈 **SOUL DEAL!** {fighter.display_name} steals {steal} INS from {opponent.display_name}!"

    elif name == "Nine Lives":
        fighter.survived_zero = True
        return True, f"😼 **NINE LIVES** locked in — {fighter.display_name} will survive one KO!"

    elif name == "Beardown":
        fighter.INS = min(100, fighter.INS + 30)
        return True, f"🐻 **BEARDOWN!** {fighter.display_name}'s INS surges — absorbs all damage this round!"

    elif name == "Death Roll":
        opponent.SPK = max(10, opponent.SPK - 50)
        return True, f"🐊 **DEATH ROLL!** {opponent.display_name} is locked — no crits possible this round!"

    elif name == "Lucky Foot":
        fighter.crit_multiplier = 3.0
        return True, f"🐇 **LUCKY FOOT!** {fighter.display_name}'s crits now deal 3x damage this battle!"

    elif name == "Chaos Mode":
        # Triple a random stat temporarily
        stat = random.choice(["VLT", "INS", "SPK"])
        if stat == "VLT":
            fighter.VLT = min(100, fighter.VLT * 3)
        elif stat == "INS":
            fighter.INS = min(100, fighter.INS * 3)
        else:
            fighter.SPK = min(100, fighter.SPK * 3)
        return True, f"🐱 **CHAOS MODE!** The Shitty Kitty goes feral — {stat} tripled! Nobody expected that."

    elif name == "Chroma Shift":
        stats = {"VLT": fighter.VLT, "INS": fighter.INS, "SPK": fighter.SPK}
        highest = max(stats, key=stats.get)
        lowest  = min(stats, key=stats.get)
        stats[highest], stats[lowest] = stats[lowest], stats[highest]
        fighter.VLT, fighter.INS, fighter.SPK = stats["VLT"], stats["INS"], stats["SPK"]
        return True, f"🌈 **CHROMA SHIFT!** {fighter.display_name} swaps {highest} and {lowest} — stats scrambled!"

    elif name == "Halo":
        # Block opponent crits by reducing their SPK to near 0 this round
        original_spk = opponent.SPK
        opponent.SPK = 5
        return True, f"😇 **HOLY GROUND!** {opponent.display_name}'s crits are blocked. {fighter.display_name}'s Spark fires guaranteed!"

    elif name == "Royal Decree":
        opponent.skip_next_attack = True
        return True, f"👑 **ROYAL DECREE!** {fighter.display_name} raises a hand. {opponent.display_name} is forbidden from attacking this round!"

    elif name == "Magic Burst":
        fighter.crit_multiplier = 3.0
        return True, f"🦄 **MAGIC BURST!** {fighter.display_name}'s horn crackles — next crit deals 3x!"

    elif name == "Bird Strike":
        # Force a crit next hit — we'll handle in damage calc
        return True, f"🐦 **BIRD STRIKE!** {fighter.display_name}'s bird swoops — guaranteed crit incoming!"

    elif name == "Siren Call":
        opponent.INS = max(10, opponent.INS // 2)
        return True, f"🎵 **SIREN CALL!** {opponent.display_name} is confused — INS halved this round!"

    elif name == "See-Through":
        # Passive: handled pre-battle in resolve_battle(), skip here
        return False, ""

    elif name == "Talon Strike":
        import random as _rand
        if _rand.random() < 0.5:
            bonus = int(opponent.hp * 0.8)
            opponent.hp -= bonus
            return True, f"🦅 **TALON STRIKE!** {fighter.display_name} dives — {bonus} bonus damage! {opponent.display_name} is reeling!"
        else:
            return True, f"🦅 **TALON STRIKE MISSES!** {fighter.display_name} overcommits — {opponent.display_name} sidesteps the dive!"

    elif name == "Zappy Spirit":
        fighter.VLT += 5
        fighter.INS += 5
        fighter.SPK += 5
        return True, f"⚡ **ZAPPY SPIRIT** — Brand loyalty pays off. All stats +5."

    return False, ""


# ─────────────────────────────────────────────
# Flavor text generators
# ─────────────────────────────────────────────

ROUND_OPENERS = [
    "The arena crackles with static.",
    "Electricity fills the air.",
    "The crowd goes silent.",
    "Both Zappies square up.",
    "The voltage is palpable.",
]

NORMAL_HIT = [
    "lands a clean hit",
    "connects with a solid strike",
    "finds the gap in the defense",
    "sparks fly on contact",
    "gets through the armor",
]

WEAK_HIT = [
    "barely scratches",
    "grazes",
    "taps",
    "the hit glances off",
]

CRIT_LINES = [
    "⚡ CRITICAL SURGE",
    "⚡ VOLTAGE SPIKE",
    "⚡ FULL POWER",
    "⚡ MAXIMUM CHARGE",
]

WIN_LINES = [
    "The crowd erupts! ⚡",
    "Victory! The arena shakes.",
    "Dominant performance.",
    "That's how it's done.",
    "Lightning strikes twice.",
]

UPSET_LINES = [
    "Nobody saw that coming! 🔥",
    "The underdog rises!",
    "Stats don't tell the whole story.",
    "Pure Spark energy!",
]


def resolve_battle(fighter_a: Fighter, fighter_b: Fighter) -> dict:
    """
    Main battle resolution function.
    Returns full battle log with Discord-formatted messages.
    """
    log = []
    round_logs = []

    # ── Pre-battle stat display ──
    log.append(f"⚡ **BRACKET MATCH**")
    log.append(f"**{fighter_a.display_name}** — VLT {fighter_a.VLT} · INS {fighter_a.INS} · SPK {fighter_a.SPK}")
    if fighter_a.combo:
        log.append(f"  ↳ {fighter_a.combo}")
    log.append(f"vs. **{fighter_b.display_name}** — VLT {fighter_b.VLT} · INS {fighter_b.INS} · SPK {fighter_b.SPK}")
    if fighter_b.combo:
        log.append(f"  ↳ {fighter_b.combo}")
    log.append("")
    # ── See-Through passive: pre-battle counter-read ──
    for fighter, opponent in [(fighter_a, fighter_b), (fighter_b, fighter_a)]:
        ability = fighter.ability
        if ability and isinstance(ability, dict) and ability.get("name") == "See-Through":
            opp_stats = {"VLT": opponent.VLT, "INS": opponent.INS, "SPK": opponent.SPK}
            dominant = max(opp_stats, key=opp_stats.get)
            dominant_val = opp_stats[dominant]

            # Counter map: VLT → +INS, INS → +VLT, SPK → +VLT (aggression)
            BONUS_PCT = 0.18  # 18% of opponent's dominant stat
            bonus = int(dominant_val * BONUS_PCT)

            if dominant == "VLT":
                counter_stat = "INS"
                fighter.INS = min(100, fighter.INS + bonus)
                counter_label = "braces for the assault"
            elif dominant == "INS":
                counter_stat = "VLT"
                fighter.VLT = min(100, fighter.VLT + bonus)
                counter_label = "targets the soft spots"
            else:  # SPK
                counter_stat = "VLT"
                fighter.VLT = min(100, fighter.VLT + bonus)
                counter_label = "stays aggressive"

            log.append(
                f"🩻 **SEE-THROUGH** — {fighter.display_name} reads {opponent.display_name}'s dominant stat "
                f"(**{dominant} {dominant_val}**) and {counter_label}. "
                f"+{bonus} {counter_stat} applied before the battle begins."
            )
            fighter.ability_used = True

    log.append("---PLAY_BY_PLAY_START---")

    # Track if it's an upset (lower total stats wins)
    a_total = fighter_a.VLT + fighter_a.INS + fighter_a.SPK
    b_total = fighter_b.VLT + fighter_b.INS + fighter_b.SPK
    a_is_underdog = a_total < b_total

    # ── Three rounds ──
    for round_num in range(1, ROUNDS + 1):
        round_msg = []
        round_msg.append(f"🥊 **Round {round_num}** — {random.choice(ROUND_OPENERS)}")

        # Try to trigger abilities (both fighters)
        for attacker, defender in [(fighter_a, fighter_b), (fighter_b, fighter_a)]:
            ability_triggered, ability_msg = apply_ability(attacker, defender, round_num)
            if ability_triggered and ability_msg:
                round_msg.append(ability_msg)

        # ── Fighter A attacks Fighter B ──
        if fighter_a.skip_next_attack:
            fighter_a.skip_next_attack = False
            round_msg.append(f"  👑 **{fighter_a.display_name}** is forbidden from attacking this round!")
            dmg_a, crit_a = 0, False
        elif fighter_b.shield_active:
            fighter_b.shield_active = False
            round_msg.append(f"  😇 **{fighter_b.display_name}**'s Divine Shield absorbs the hit — 0 damage!")
            dmg_a, crit_a = 0, False
        else:
            dmg_a, crit_a, _ = calculate_damage(fighter_a, fighter_b, round_num)

        # Iron Shell: one-time full absorb, only if fighter_b has the combo
        if fighter_b.combo == "Iron Shell" and not fighter_b.iron_shell_used and fighter_b.hp <= dmg_a:
            dmg_a = 0
            fighter_b.iron_shell_used = True
            round_msg.append(f"  🛡️ {fighter_b.display_name}'s Iron Shell absorbs everything — survives on 1 HP!")
            fighter_b.hp = 1
        else:
            if crit_a:
                round_msg.append(f"  {random.choice(CRIT_LINES)}! **{fighter_a.display_name}** — {dmg_a} damage!")
            elif dmg_a > fighter_a.VLT * 0.8:
                round_msg.append(f"  **{fighter_a.display_name}** {random.choice(NORMAL_HIT)} — {dmg_a} damage.")
            else:
                round_msg.append(f"  **{fighter_a.display_name}** {random.choice(WEAK_HIT)} — {dmg_a} damage.")
            fighter_b.hp -= dmg_a

        # ── Fighter B attacks Fighter A ──
        if fighter_b.skip_next_attack:
            fighter_b.skip_next_attack = False
            round_msg.append(f"  👑 **{fighter_b.display_name}** is forbidden from attacking this round!")
            dmg_b, crit_b = 0, False
        elif fighter_a.shield_active:
            fighter_a.shield_active = False
            round_msg.append(f"  😇 **{fighter_a.display_name}**'s Divine Shield absorbs the hit — 0 damage!")
            dmg_b, crit_b = 0, False
        else:
            dmg_b, crit_b, _ = calculate_damage(fighter_b, fighter_a, round_num)

        # Iron Shell: one-time full absorb, only if fighter_a has the combo
        if fighter_a.combo == "Iron Shell" and not fighter_a.iron_shell_used and fighter_a.hp <= dmg_b:
            dmg_b = 0
            fighter_a.iron_shell_used = True
            round_msg.append(f"  🛡️ {fighter_a.display_name}'s Iron Shell absorbs everything — survives on 1 HP!")
            fighter_a.hp = 1
        else:
            if crit_b:
                round_msg.append(f"  {random.choice(CRIT_LINES)}! **{fighter_b.display_name}** — {dmg_b} damage!")
            elif dmg_b > fighter_b.VLT * 0.8:
                round_msg.append(f"  **{fighter_b.display_name}** {random.choice(NORMAL_HIT)} — {dmg_b} damage.")
            else:
                round_msg.append(f"  **{fighter_b.display_name}** {random.choice(WEAK_HIT)} — {dmg_b} damage.")
            fighter_a.hp -= dmg_b

        # Nine Lives check
        for f in [fighter_a, fighter_b]:
            if f.hp <= 0 and f.survived_zero == True and not hasattr(f, '_nine_lives_used'):
                f.hp = 1
                f._nine_lives_used = True
                round_msg.append(f"  😼 **NINE LIVES activates!** {f.display_name} survives on 1 HP!")

        # Clamp HP
        fighter_a.hp = max(0, fighter_a.hp)
        fighter_b.hp = max(0, fighter_b.hp)

        # HP status
        round_msg.append(f"  HP: **{fighter_a.display_name}** {fighter_a.hp} · **{fighter_b.display_name}** {fighter_b.hp}")

        round_logs.append("\n".join(round_msg))
        log.extend(round_msg)
        log.append("")

        # Early KO check
        if fighter_a.hp <= 0 or fighter_b.hp <= 0:
            break

    # ── Determine winner ──
    if fighter_a.hp > fighter_b.hp:
        winner, loser = fighter_a, fighter_b
    elif fighter_b.hp > fighter_a.hp:
        winner, loser = fighter_b, fighter_a
    else:
        # Tie — weighted random roll based on total stats
        total_a = fighter_a.VLT + fighter_a.INS + fighter_a.SPK
        total_b = fighter_b.VLT + fighter_b.INS + fighter_b.SPK
        roll_a  = random.randint(1, total_a)
        roll_b  = random.randint(1, total_b)
        log.append(
            f"⚡ **TIE — Fate decides!**\n"
            f"  🎲 **{fighter_a.display_name}** rolls **{roll_a}** (out of {total_a})\n"
            f"  🎲 **{fighter_b.display_name}** rolls **{roll_b}** (out of {total_b})"
        )
        if roll_a >= roll_b:
            winner, loser = fighter_a, fighter_b
        else:
            winner, loser = fighter_b, fighter_a

    # ── Result message ──
    is_upset = (winner == fighter_a and a_is_underdog) or (winner == fighter_b and not a_is_underdog)
    log.append(f"🏆 **{winner.display_name} wins!** {random.choice(UPSET_LINES if is_upset else WIN_LINES)}")

    return {
        "winner": winner,
        "loser":  loser,
        "is_upset": is_upset,
        "log": log,
        "log_text": "\n".join(log),
        "fighter_a_final_hp": fighter_a.hp,
        "fighter_b_final_hp": fighter_b.hp,
    }


# ─────────────────────────────────────────────
# Quick test
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # Simulate a battle with two test fighters
    a = Fighter(asset_id=340, name="Zappy #340", unit_name="ZAPP0340",
                VLT=55, INS=48, SPK=62,
                ability={"name": "See-Through", "desc": "Reads opponent dominant stat", "trigger_round": "passive"})
    b = Fighter(asset_id=1002, name="Zappy #241", unit_name="ZAPP0241",
                VLT=72, INS=65, SPK=45,
                combo="⚡ Storm Caller")

    result = resolve_battle(a, b)
    print(result["log_text"])
    print(f"\nWinner: {result['winner'].display_name}")
    print(f"Upset: {result['is_upset']}")

"""
stats_engine.py
---------------
Takes a Zappy's trait dict and returns VLT / INS / SPK stats.

Stat ranges: each stat is 10-100.
  VLT (Voltage)   = Attack power
  INS (Insulation) = Defense / damage reduction
  SPK (Spark)     = Crit chance / luck

Called by: algorand_lookup.py -> battle_engine.py
"""

# ─────────────────────────────────────────────
# BACKGROUND — sets the stat FLOOR for all stats
# Rarer = higher floor
# ─────────────────────────────────────────────
BACKGROUND_FLOOR = {
    "Chroma":  50,   # Rarest (~6%)  — all stats start at 50+
    "Blue":    20,
    "Purple":  20,
    "Red":     18,
    "Green":   18,
    "Orange":  16,
    "Yellow":  16,
}

# ─────────────────────────────────────────────
# BODY — maps to INS (defense)
# Armored/heavy = high INS, casual/naked = low
# ─────────────────────────────────────────────
BODY_INS_BONUS = {
    # Heavy armor
    "Armor":              30,
    "Space Suit":         28,
    "Muscle Suit":        25,
    "Suit":               22,
    "Trench Coat":        20,
    "Fur Coat":           20,
    "Leather Vest":       18,
    "Wool Collar Jacket": 18,
    "Hooded Jacket":      17,
    "Kimono":             17,
    "Funky Coat":         17,
    "Crocodile Suit":     22,
    "Bear Suit":          22,
    "Rabbit Suit":        20,
    "Dino Suit":          20,
    "Cat Suit":           18,
    "Royal Robe":         25,  # Ultra rare
    # Mid
    "Hoodie":             15,
    "Polo Shirt":         14,
    "Sweater":            14,
    "Turtleneck Sweater": 14,
    "Oversize Sweater":   14,
    "Striped Sweater":    14,
    "Puffer Coat":        18,
    "Bathrobe":           12,
    "Tracksuit Top":      13,
    "Bowling Shirt":      12,
    "Hawaiian Shirt":     11,
    "School Outfit":      11,
    "Polo Overalls":      12,
    "Overalls":           12,
    "Basic Jacket":       13,
    "Colorful Jacket":    12,
    "Daisy Shirt":        11,
    "Ribbon Tie":         10,
    "Scarf":              10,
    "Choker":             10,
    "Armband":            10,
    "Sleeveless Shirt":   10,
    "Striped Shirt":      11,
    "Polo Shirt":         12,
    "Toga":               10,
    "Warning Tape":        8,
    "Trash Can":           8,
    "Clouds":             10,
    "Lifesaver":          15,
    # Low
    "Naked":               5,
    "Tracksuit Top":      13,
    "Fanny Pack":         10,
}

# ─────────────────────────────────────────────
# BODY — some bodies also add VLT
# ─────────────────────────────────────────────
BODY_VLT_BONUS = {
    "Armor":         15,
    "Muscle Suit":   20,
    "Royal Robe":    10,
    "Warning Tape":  12,   # Scrappy fighter
    "Trash Can":     10,
    "Naked":         10,   # Nothing to lose
}

# ─────────────────────────────────────────────
# EARRING — maps to SPK (luck/crit chance)
# ─────────────────────────────────────────────
EARRING_SPK_BONUS = {
    "Lightning":      25,  # Top tier
    "Zappies":        22,  # Brand earring — special
    "Rainbow":        20,
    "Star":           18,
    "Diamond":        18,
    "Double Helix":   16,
    "Twin Rings":     15,
    "Ghost":          15,
    "Skull":          15,
    "Pyramid":        14,
    "Party Popper":   14,
    "Bananas":        12,
    "Strawberry":     12,
    "Daisy":          12,
    "Sunrise":        12,
    "Happy":          12,
    "Sad":            10,
    "Pepper":         10,
    "Hashtag":        10,
    "Knife":          12,
    "Serious":        10,
    "Left Helix":     10,
    "Right Helix":    10,
    "Left Ring":      10,
    "Right Ring":     10,
    "Number One":     10,
    "Carton Cup":     10,
    "Skull":          15,
    "Sunrise":        12,
    "None":            0,
}

# ─────────────────────────────────────────────
# EYES — modifies SPK behavior (variance)
# Some eyes also add flat SPK bonus
# ─────────────────────────────────────────────
EYES_SPK_BONUS = {
    "Dead":       20,   # High risk / high reward
    "Dizzy":      18,
    "Shocked":    16,
    "Coins":      15,   # Money-hungry = lucky
    "Stars":      15,
    "Joy":        12,
    "Love":       10,
    "Wink":       12,
    "Pissed":     14,
    "Nervous":    10,
    "Confused":    8,
    "Irritated":   8,
    "Tired":       6,
    "Asleep":      5,
    "Chill":      10,
    "Standard":    8,
    "Plasters":    6,
    "Daisy":      10,
    "Wet":         8,
}

# ─────────────────────────────────────────────
# EYEWEAR — maps to INS modifier
# Most Zappies have None (~58%), so any eyewear is a bonus
# ─────────────────────────────────────────────
EYEWEAR_INS_BONUS = {
    "None":              0,
    # Rare eyewear
    "Pirate Eyepatch":  25,   # ~1% - top tier
    "Monocle":          22,   # ~1% - top tier
    "Steampunk Glasses": 18,  # ~2%
    "Post-it Note":     15,
    # Mid-rare
    "Deal With It":     15,
    "Shutter Glasses":  12,
    "Oversized Glasses": 12,
    "Nerd Glasses":     12,
    "3D Glasses":       10,
    "Cat Eye Glasses":  10,
    "Triangle Glasses": 10,
    "Round Glasses":    10,
    "Pantos Glasses":   10,
    "Oval Glasses":     10,
    "Sunglasses":       10,
    "Disguise Glasses": 10,
}

# ─────────────────────────────────────────────
# HEAD — maps to VLT (attack power)
# Companion heads, crowns, and power items = highest VLT
# ─────────────────────────────────────────────
HEAD_VLT_BONUS = {
    # Legendary companions
    "Fiery Companion":  30,
    "Devil Companion":  28,
    "Angel Companion":  25,
    "Bird Nest":        20,   # Chaotic neutral
    # Power items
    "Crown":            28,
    "Halo":             25,
    "Laurel Crown":     22,
    "Unicorn Horn":     22,
    "Horns":            20,
    "Antlers":          18,
    "Siren":            20,
    "Dripping Ring":    18,
    # Weapons / intense
    "Spikes":           20,
    "Cables":           16,
    "Brain":            18,   # Big brain = big plays
    "Robot Antenna":    15,
    "Eyes":             15,
    # Hats - mid
    "Samurai Hat":      18,
    "Western Hat":      15,
    "Firefighter Hat":  14,
    "Ninja Headband":   14,
    "Sweatband":        12,
    "Bandana":          12,
    "Headband":         12,
    "Visor Cap":        12,
    "Sideways Cap":     11,
    "Cap":              11,
    "Backwards Cap":    11,
    "Flipped Brim":     11,
    "Short Beanie":     10,
    "Long Beanie":      10,
    "Wool Bucket Hat":  10,
    "Bucket Hat":       10,
    "Beach Hat":        10,
    "Straw Hat":        10,
    "Wrap":             10,
    "Docker Hat":       11,
    "Ushanka":          11,
    "Sombrero":         10,
    "Bald":              8,
    "Bald on Top":       8,
    "Buzzcut":          10,
    "Messy Hair":       10,
    "Curly Hair":       10,
    "Fluffly Hair":     10,
    "Windswept Hair":   10,
    "Basic Hair":       10,
    "Curly Mohawk":     12,
    "Spiky Mohawk":     14,
    "Basic Mohawk":     12,
    "Torn":              8,
    "Stars":            14,
    "Clouds":           10,
    "Sunrise":          12,
    "Banana Peel":       8,
    "Melted Ice Cream":  8,
    "Rubber Ducky":     10,
    "Pot Lid":          12,
    "Trash Can Lid":    12,
    "Dino Hat":         12,
    "Bear Hat":         12,
    "Cat Hat":          12,
    "Rabbit Hat":       12,
    "Crocodile Hat":    14,
    "Devil Companion":  28,
    "Poop":              6,
    "Bird Nest":        20,
}

# ─────────────────────────────────────────────
# MOUTH — expression modifier
# Affects SPK variance (how swingy crits are)
# Also some flat SPK bonuses
# ─────────────────────────────────────────────
MOUTH_SPK_BONUS = {
    "Rawr":           18,   # Aggressive
    "Cigar":          16,
    "Big Smile":      12,
    "Kiss":           12,
    "Lip Bite":       14,
    "Tongue Out":     12,
    "Happy Pill":     15,
    "Bubble Gum":     10,
    "Pizza":          10,
    "Whistle":        10,
    "Yummy":          10,
    "Smirk":          12,
    "Smile":           8,
    "Standard":        8,
    "Sulk":            8,
    "Bored":           6,
    "Sleepy":          5,
    "Tense":           8,
    "Mustache":       10,
    "Long Mustache":  12,
    "Nerd":            8,
    "Party Horn":     12,
    "Burp":           10,
    "Surprised":      12,
    "Colorfall":      14,
    "Pacifier":        6,
    "Piece of Straw":  8,
}

# ─────────────────────────────────────────────
# SKIN — palette-based INS/VLT split
# Rare skins get unique passive bonuses
# ─────────────────────────────────────────────
SKIN_INS_BONUS = {
    # Rare skins
    "Chroma":    25,   # Rarest skin — special passive
    "X-ray":     20,
    "Gold":      18,
    "Infected":  15,   # Mutant = resilient
    "Zebra":     12,
    "Tattooed":  12,
    "Vitiligo":  12,
    "Kisses":    12,
    "Patterned": 10,
    "Crimson":   10,
    "Cloudy":    10,
    "Pastel":     8,
    "Celeste":    8,
    "Chef":      10,
    "Ivory":      6,
    "Sienna":     6,
    "Kisses":    12,
}

SKIN_VLT_BONUS = {
    "Gold":      15,   # High roller
    "Chroma":    10,
    "X-ray":      8,
    "Crimson":   12,
    "Infected":   8,
}

# ─────────────────────────────────────────────
# SPECIAL ASSETS — Heroes and Collab
# ─────────────────────────────────────────────
HERO_STATS = {
    "Bear":       {"VLT": 95, "INS": 90, "SPK": 75, "ability": {"name": "Beardown",   "desc": "Doubles INS for one round. Absorbs all damage that round."}},
    "Crocodile":  {"VLT": 90, "INS": 75, "SPK": 80, "ability": {"name": "Death Roll", "desc": "Locks opponent -- they cannot benefit from SPK crits this round."}},
    "Cat":        {"VLT": 75, "INS": 80, "SPK": 95, "ability": {"name": "Nine Lives", "desc": "If HP hits 0, survives at 1 HP once. Only fires once per battle."}},
    "Rabbit":     {"VLT": 80, "INS": 70, "SPK": 90, "ability": {"name": "Lucky Foot", "desc": "Crit multiplier becomes 3x instead of 2x this battle."}},
}

COLLAB_STATS = {
    "ShittyKitties": {"VLT": 70, "INS": 70, "SPK": 100, "ability": {"name": "Chaos Mode", "desc": "Random stat gets tripled for one random round. Even the bot doesn't know which."}},
}


def calculate_stats(traits: dict) -> dict:
    """
    Input:  traits dict with keys:
              background, body, earring, eyes, eyewear, head, mouth, skin
    Output: dict with VLT, INS, SPK (each clamped 10-100), plus ability info
    """

    bg = traits.get("background", "")
    body = traits.get("body", "")
    earring = traits.get("earring", "None")
    eyes = traits.get("eyes", "")
    ew = traits.get("eyewear", "None")
    head = traits.get("head", "")
    mouth = traits.get("mouth", "")
    skin = traits.get("skin", "")

    # Base floor from background
    floor = BACKGROUND_FLOOR.get(bg, 15)

    # VLT calculation
    vlt = floor
    vlt += BODY_VLT_BONUS.get(body, 0)
    vlt += HEAD_VLT_BONUS.get(head, 10)       # Head is primary VLT driver
    vlt += SKIN_VLT_BONUS.get(skin, 0)

    # INS calculation
    ins = floor
    ins += BODY_INS_BONUS.get(body, 10)       # Body is primary INS driver
    ins += EYEWEAR_INS_BONUS.get(ew, 0)
    ins += SKIN_INS_BONUS.get(skin, 5)

    # SPK calculation
    spk = floor
    spk += EARRING_SPK_BONUS.get(earring, 0)  # Earring is primary SPK driver
    spk += EYES_SPK_BONUS.get(eyes, 8)
    spk += MOUTH_SPK_BONUS.get(mouth, 8)

    # Clamp all stats to 10-100
    vlt = max(10, min(100, vlt))
    ins = max(10, min(100, ins))
    spk = max(10, min(100, spk))

    # Check for combos
    combos = check_combos(traits, vlt, ins, spk)
    vlt = combos["VLT"]
    ins = combos["INS"]
    spk = combos["SPK"]
    combo_name = combos.get("combo_name")

    # Special ability (from rare traits)
    ability = get_ability(traits)

    return {
        "VLT": int(vlt),
        "INS": int(ins),
        "SPK": int(spk),
        "ability": ability,
        "combo": combo_name,
        "traits": traits,
    }


def check_combos(traits: dict, vlt: float, ins: float, spk: float) -> dict:
    """Check for trait combo bonuses and apply them."""
    combo_name = None
    bg = traits.get("background", "")
    body = traits.get("body", "")
    earring = traits.get("earring", "None")
    eyes = traits.get("eyes", "")
    ew = traits.get("eyewear", "None")
    head = traits.get("head", "")
    mouth = traits.get("mouth", "")
    skin = traits.get("skin", "")

    # Storm Caller: Lightning earring + Chroma/electric BG → massive VLT + crit upgrade
    if earring == "Lightning" and bg == "Chroma":
        vlt += 20
        spk += 10
        combo_name = "⚡ Storm Caller"

    # Iron Shell: Armor body + cool skin (Celeste/Cloudy/X-ray) → tank build
    elif body == "Armor" and skin in ("Celeste", "Cloudy", "X-ray", "Pastel"):
        ins += 25
        combo_name = "🛡️ Iron Shell"

    # Lucky Fool: Angry eyes + common BG → massive SPK (upset machine)
    elif eyes in ("Pissed", "Dead", "Dizzy") and bg in ("Green", "Orange", "Yellow"):
        spk = min(100, spk + 20)
        combo_name = "🎲 Lucky Fool"

    # Zappy Prime: Chroma BG + Chroma skin + any rare head companion
    elif bg == "Chroma" and skin == "Chroma":
        vlt += 15
        ins += 15
        spk += 15
        combo_name = "👑 Zappy Prime"

    # Royal Command: Royal Robe + Laurel Crown or Crown
    elif body == "Royal Robe" and head in ("Laurel Crown", "Crown"):
        vlt += 15
        ins += 10
        combo_name = "👑 Royal Command"

    # Chaos Agent: Naked + Dead eyes → wild stats
    elif body == "Naked" and eyes == "Dead":
        spk = min(100, spk + 25)
        combo_name = "💀 Chaos Agent"

    # Firebrand: Fiery Companion head + Red/Crimson skin
    elif head == "Fiery Companion" and (bg == "Red" or skin == "Crimson"):
        vlt += 20
        combo_name = "🔥 Firebrand"

    # Gold Standard: Gold skin + any suit body
    elif skin == "Gold" and "Suit" in body:
        vlt += 10
        ins += 10
        combo_name = "🥇 Gold Standard"

    return {"VLT": max(10, min(100, vlt)), "INS": max(10, min(100, ins)),
            "SPK": max(10, min(100, spk)), "combo_name": combo_name}


def get_ability(traits: dict) -> dict | None:
    """Returns special ability dict if this Zappy has a 1-of ability trigger."""
    head = traits.get("head", "")
    skin = traits.get("skin", "")
    bg = traits.get("background", "")
    earring = traits.get("earring", "")

    # Companion heads always trigger an ability
    companion_abilities = {
        "Fiery Companion": {
            "name": "Inferno Surge",
            "desc": "Doubles VLT for one round. Burns opponent for 5 chip damage next round too.",
            "trigger_round": 2,
        },
        "Devil Companion": {
            "name": "Soul Deal",
            "desc": "Steals 10 INS from opponent temporarily. Your INS rises, theirs drops this round.",
            "trigger_round": 2,
        },
        "Angel Companion": {
            "name": "Divine Shield",
            "desc": "Blocks all damage one round completely. Cannot be critted through.",
            "trigger_round": 2,
        },
        "Bird Nest": {
            "name": "Bird Strike",
            "desc": "Random round, deals 2x SPK crit automatically regardless of dice roll.",
            "trigger_round": "random",
        },
        "Siren": {
            "name": "Siren Call",
            "desc": "Opponent's INS is halved for one round. Confusion debuff.",
            "trigger_round": 2,
        },
        "Unicorn Horn": {
            "name": "Magic Burst",
            "desc": "One random round gets a 3x damage multiplier instead of 2x for crits.",
            "trigger_round": "random",
        },
        "Halo": {
            "name": "Holy Ground",
            "desc": "Prevents opponent crits for one round. Your Spark fires guaranteed.",
            "trigger_round": 1,
        },
        "Crown": {
            "name": "Royal Surge",
            "desc": "VLT doubles for round 1. Momentum starts strong.",
            "trigger_round": 1,
        },
    }

    if head in companion_abilities:
        return companion_abilities[head]

    # Rare skin abilities
    if skin == "Chroma":
        return {
            "name": "Chroma Shift",
            "desc": "Once per battle, Zappy shifts — randomly swaps its highest and lowest stat for one round.",
            "trigger_round": "random",
        }

    if skin == "Gold":
        return {
            "name": "Gold Rush",
            "desc": "Win bonus: earns +50 CP instead of +30 CP on victory. Gold pays out.",
            "trigger_round": "passive",
        }

    if skin == "X-ray":
        return {
            "name": "See-Through",
            "desc": "Stats are revealed to the channel before opponent's — strategic pre-knowledge for the crowd.",
            "trigger_round": "passive",
        }

    # Zappies earring — brand loyalty ability
    if earring == "Zappies":
        return {
            "name": "Zappy Spirit",
            "desc": "All stats get +5 flat bonus. Wearing the brand means something.",
            "trigger_round": "passive",
        }

    return None


def get_hero_stats(hero_type: str) -> dict:
    """Returns stats for a Zappy Hero token."""
    return HERO_STATS.get(hero_type, None)


def get_collab_stats(collab_type: str) -> dict:
    """Returns stats for a Collab token."""
    return COLLAB_STATS.get(collab_type, None)


# ─────────────────────────────────────────────
# Quick test
# ─────────────────────────────────────────────
if __name__ == "__main__":
    # Test: Zappy #1474 — Orange bg, Armband body, Ghost earring,
    #                      Confused eyes, None eyewear, Flipped Brim head, Cigar mouth, Celeste skin
    test_traits = {
        "background": "Orange",
        "body": "Armband",
        "earring": "Ghost",
        "eyes": "Confused",
        "eyewear": "None",
        "head": "Flipped Brim",
        "mouth": "Cigar",
        "skin": "Celeste",
    }
    result = calculate_stats(test_traits)
    print(f"Test Zappy stats: VLT {result['VLT']} | INS {result['INS']} | SPK {result['SPK']}")
    print(f"Ability: {result['ability']}")
    print(f"Combo: {result['combo']}")

    # Test a Chroma Zappy
    chroma_traits = {
        "background": "Chroma",
        "body": "Armor",
        "earring": "Lightning",
        "eyes": "Dead",
        "eyewear": "Monocle",
        "head": "Crown",
        "mouth": "Rawr",
        "skin": "Chroma",
    }
    result2 = calculate_stats(chroma_traits)
    print(f"\nChroma Zappy stats: VLT {result2['VLT']} | INS {result2['INS']} | SPK {result2['SPK']}")
    print(f"Ability: {result2['ability']}")
    print(f"Combo: {result2['combo']}")

"""
zappy_news_templates.py
-----------------------
Templated agent personalities for the Zappy Agent Wire (no LLM involved).

Every agent on duty gets a VOICE. Heroes have a fixed voice; every other Zappy gets a
stable one derived from its asset id, so the same Zappy always sounds the same.
A voice supplies the opening line, the sign-off, price-mood quips, and a
breaking-news stinger. The segment itself (the facts) is built in the cog and
sandwiched between them.

Lines may use {anchor}; nothing else. Keep them short and safe.
"""

from __future__ import annotations

import random

# ── Voices ─────────────────────────────────────────────────────────────────────

VOICES: dict[str, dict[str, list[str]]] = {
    "smug": {
        "open":     ["Naturally, I'm the one reading you this.",
                     "You're welcome for the news.",
                     "Let me know if I'm going too fast."],
        "close":    ["I make this look easy.", "That's the news. You're lucky I'm here.", "Try to keep up."],
        "up":       ["Up. As predicted. By me.", "Green. Obviously.", "Up, and I saw it coming."],
        "down":     ["Down, but it was always part of my plan.", "Red. Temporary. Probably.", "A dip. Beneath my notice."],
        "flat":     ["Flat. Elegant, really.", "Barely moved. Like me at a desk.", "Sideways. Composed."],
        "breaking": ["Stop what you're doing. I said BREAKING.", "Drop everything, this one is mine."],
    },
    "calm": {
        "open":     ["Take a breath. Here's what's happening.", "No rush. This is the news.",
                     "Settle in. We'll go slowly."],
        "close":    ["Breathe in. That's the report.", "Take your time with that one.", "Peace. Back to you."],
        "up":       ["A gentle rise. Lovely.", "Up, softly, like a tide.", "Rising. No need to hurry."],
        "down":     ["A small dip. It passes.", "Down a little. We stay calm.", "Lower today. Breathe."],
        "flat":     ["Perfectly still. Nice.", "Not much moving. That's fine.", "Level as a pond."],
        "breaking": ["Calmly... but this is breaking.", "Deep breath. Breaking news."],
    },
    "baffled": {
        "open":     ["I have been told to read this to you. Humans are strange.",
                     "Greetings, Earth. Here are your events.",
                     "Is this the correct button? Here is the news."],
        "close":    ["I do not understand, but I have reported it.", "Returning to the cables now.",
                     "End of transmission. Wait. Is it over?"],
        "up":       ["It goes up. This is good? I think it is good.", "The number grew. Humans seem pleased.",
                     "Upward. I clap, as you do."],
        "down":     ["The number shrank. Humans seem sad.", "Down? I am told this matters.",
                     "It fell. I will not ask why."],
        "flat":     ["The number did nothing. Remarkable.", "Unchanged. I respect it.", "It stays. Good number."],
        "breaking": ["Alert! I am told this is called 'breaking'.", "Something happened! Quickly!"],
    },
    "gruff": {
        "open":     ["Listen up. Short version.", "Here's the news. Don't make me repeat it.",
                     "Keep it quiet, here it comes."],
        "close":    ["That's it. Move along.", "News over. Get back to work.", "Don't say I didn't tell you."],
        "up":       ["Up. Good. Don't get cocky.", "Green. Fine.", "Rising. About time."],
        "down":     ["Down. Happens. Tough it out.", "Red. Take it on the chin.", "Dipping. Deal with it."],
        "flat":     ["Flat. Quiet day.", "Nothing. Good.", "Steady. As it should be."],
        "breaking": ["Hold it. This is breaking.", "Eyes up. Big news."],
    },
    "hype": {
        "open":     ["OKAY OKAY OKAY, you are NOT ready for this one!",
                     "Pull up a chair, folks, it's NEWS TIME!",
                     "Lightning round! Here we GO!"],
        "close":    ["And THAT'S the news, folks!", "WOW. Absolutely electric. Back to you!",
                     "Stay charged! Stay tuned!"],
        "up":       ["UP UP UP! Let's go!", "Green candles! Green candles!", "We are CLIMBING, folks!"],
        "down":     ["Down! But we ride the lightning back up!", "Dip! Dip! We stay positive!",
                     "A small setback! Pure hype energy remains!"],
        "flat":     ["Flat! Calm before the storm!", "Barely moving! Suspicious!", "Sideways! Tension is building!"],
        "breaking": ["BREAKING! BREAKING! BREAKING!", "STOP EVERYTHING! BIG NEWS!"],
    },
    "deadpan": {
        "open":     ["Here is the news. I am reading it.", "Good day. This is a broadcast.",
                     "News. As scheduled."],
        "close":    ["That was the news.", "Thank you. Do with it what you will.", "End of segment."],
        "up":       ["Up. Noted.", "The number is higher. Congratulations, I suppose.", "Green. Fine."],
        "down":     ["Down. Noted.", "The number is lower. This is the world.", "Red. It happens."],
        "flat":     ["Unchanged. Riveting.", "No movement to report.", "Flat. I am thrilled."],
        "breaking": ["This is breaking. I am being told it is exciting.", "Breaking. Try to look surprised."],
    },
    "chipper": {
        "open":     ["Hi hi hi! Got some news for you!", "Good news time, friends!",
                     "Ooh! Look what I found!"],
        "close":    ["Have a zappy day!", "That's all! Smile!", "Bye for now, you're doing great!"],
        "up":       ["Up! Yay! High five!", "It's going up! Hooray!", "Green! My favorite color today!"],
        "down":     ["Down a bit, but chin up!", "Dip! But tomorrow's a new day!", "A little red. We'll be okay!"],
        "flat":     ["Flat! Cozy and stable!", "Not much change! That's okay!", "Sideways! Like a happy crab!"],
        "breaking": ["Ooh! Breaking news! Gasp!", "Ooh ooh ooh! News just came in!"],
    },
    "conspiracy": {
        "open":     ["I'm only telling you because you're ready.", "Keep this between us. Here's the news.",
                     "They don't want you to hear this. It's mostly fine."],
        "close":    ["Connect the dots yourself.", "Remember: you heard it here.", "Stay sharp. Eyes open."],
        "up":       ["Up. Coincidence? Probably.", "The number rose. Who benefits?", "Green. Suspiciously green."],
        "down":     ["Down. Interesting timing.", "The number fell. Look closer.", "Red. Someone knows something."],
        "flat":     ["Flat. Too flat. Think about that.", "Nothing moved. Almost as if planned.",
                     "Still. Suspiciously still."],
        "breaking": ["This just in. And it's real.", "Breaking. I knew it."],
    },
}

GENERAL_VOICES = list(VOICES.keys())

# Hero Zappies have a fixed on-air personality.
HERO_VOICE = {
    "Cat": "smug", "Snake": "smug", "Frog": "calm", "Buck": "calm", "Alien": "baffled",
    "Bear": "gruff", "Bison": "gruff", "Wolf": "hype", "Eagle": "hype", "Rabbit": "chipper",
    "Bat": "conspiracy", "Shark": "deadpan", "Crocodile": "deadpan",
}

# ── Trait asides ───────────────────────────────────────────────────────────────

def trait_aside(traits: dict, rng: random.Random) -> str | None:
    """A tiny stage-direction built from the anchor's own traits, e.g. '*adjusts the 3D glasses*'."""
    options: list[str] = []
    eyewear = (traits.get("eyewear") or "").strip()
    head = (traits.get("head") or "").strip()
    earring = (traits.get("earring") or "").strip()
    mouth = (traits.get("mouth") or "").strip()
    body = (traits.get("body") or "").strip()
    if eyewear and eyewear.lower() != "none":
        options.append(f"*adjusts the {eyewear.lower()}*")
    if head and head.lower() != "none":
        options.append(f"*the {head.lower()} stays perfectly composed*")
    if earring and earring.lower() != "none":
        options.append(f"*the {earring.lower()} earring catches the studio lights*")
    if mouth and mouth.lower() != "none":
        options.append(f"*clears throat around the {mouth.lower()}*")
    if body:
        options.append(f"*straightens the {body.lower()}*")
    return rng.choice(options) if options else None


# ── Public helpers ─────────────────────────────────────────────────────────────

def voice_for(asset_id: int, hero: str | None) -> str:
    if hero and hero in HERO_VOICE:
        return HERO_VOICE[hero]
    return GENERAL_VOICES[asset_id % len(GENERAL_VOICES)]


def pick(voice: str, slot: str, anchor: str, rng: random.Random | None = None) -> str:
    rng = rng or random
    lines = VOICES.get(voice, VOICES["deadpan"]).get(slot) or VOICES["deadpan"]["open"]
    return rng.choice(lines).format(anchor=anchor)


def price_mood(change_pct: float | None) -> str:
    if change_pct is None or abs(change_pct) < 1.0:
        return "flat"
    return "up" if change_pct > 0 else "down"


# ── Segment metadata (label / emoji / card accent colour) ─────────────────────

SEGMENTS: dict[str, dict] = {
    "clash_recap":     {"label": "CLASH RECAP",       "emoji": "⚔️", "accent": (255, 210, 50)},
    "winners_circle":  {"label": "WINNER'S CIRCLE",   "emoji": "🏆", "accent": (255, 190, 40)},
    "market_minute":   {"label": "MARKET MINUTE",     "emoji": "📈", "accent": (80, 220, 140)},
    "headlines":       {"label": "HEADLINES",         "emoji": "📰", "accent": (90, 190, 255)},
    "chain_report":    {"label": "CHAIN LIGHTNING",   "emoji": "⛓️", "accent": (140, 170, 255)},
    "voltball_scores": {"label": "VOLTBALL SCORES",   "emoji": "🏈", "accent": (255, 150, 60)},
    "office_gossip":   {"label": "OFFICE GOSSIP",     "emoji": "☕", "accent": (230, 130, 210)},
    "hall_of_records": {"label": "HALL OF RECORDS",   "emoji": "📜", "accent": (200, 200, 220)},
    "breaking":        {"label": "BREAKING NEWS",     "emoji": "🚨", "accent": (255, 70, 70)},
}

HEADLINE_LABELS = {"tech": "TECH DESK", "ai": "AI DESK", "sports": "SPORTS DESK"}

SOURCE_LABELS = {  # nft_prizes.source -> friendly text
    "clash": "a Clash championship",
    "expedition": "an expedition",
    "spark_office": "an Office shift",
    "spark_jobs": "a Spark Job",
}

HERO_SIGNATURE = {  # Hero -> signature ability (from stats_engine.HERO_STATS)
    "Bear": "Beardown", "Crocodile": "Death Roll", "Cat": "Nine Lives", "Rabbit": "Lucky Foot",
    "Wolf": "Pack Hunt", "Frog": "Patience", "Eagle": "Talon Strike", "Buck": "Antler Clash",
    "Snake": "Venom Bite", "Shark": "Feeding Frenzy", "Alien": "Abduction",
    "Bat": "Echolocation", "Bison": "Stampede",
}

"""
expedition_events.py
--------------------
All story content for Zappy Expedition.

STRUCTURE CHANGES:
- Each event now has an optional "question" dict for Hard Mode
  { "prompt": str, "answers": [str, str, str], "correct": int (0-indexed) }
  Correct answer → guaranteed "high" outcome tier
  Wrong answer   → forced "bad" outcome (if it exists) or "low"

- Each event now has optional "trait_text" dict:
  { "VLT": str, "INS": str, "SPK": str }
  If the Zappy's highest stat matches, this line is appended to the scene.

- Some choices are marked "trap": True — picking them always resolves "low"
  regardless of stats. Players learn over time. Hard Mode traps always penalize.

- Rival encounter events are tagged "rival": True — engine handles
  pulling the rival Zappy and notifying them separately.

- Zone event pools are much larger (~12-16 per zone) so 5-beat runs rarely repeat.

Image slots: zone{N}_event{M}.png
Tone: electric, playful, a little weird. Zappy world feels alive.
"""

import random

# ─────────────────────────────────────────────
# STAT THRESHOLDS
# Low = under 40, Mid = 40-69, High = 70+
# ─────────────────────────────────────────────
def stat_tier(value: int) -> str:
    if value >= 70:  return "high"
    if value >= 40:  return "mid"
    return "low"


def get_trait_hint(event: dict, stats: dict) -> str | None:
    """
    Returns a trait-reactive line if the Zappy's dominant stat
    matches one in the event's trait_text dict.
    """
    trait_text = event.get("trait_text")
    if not trait_text or not stats:
        return None
    # Filter out None/non-numeric values — some Zappies have incomplete stats
    valid_stats = {k: v for k, v in stats.items() if isinstance(v, (int, float))}
    if not valid_stats:
        return None
    dominant = max(valid_stats, key=lambda k: valid_stats[k])
    return trait_text.get(dominant)


# ═══════════════════════════════════════════════════════
# ZONE 1 — THE STATIC FIELDS
# Tone: curious, gentle, intro energy. Low stakes.
# ═══════════════════════════════════════════════════════

ZONE1_EVENTS = [
    {
        "title": "The Humming Fence",
        "image": "zone1_e1",
        "scene": (
            "A low fence stretches across the path, buzzing faintly with stored electricity. "
            "A sign reads: *DO NOT TOUCH (probably)*. "
            "Beyond it, the Static Fields glow a soft amber."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "Your body crackles sympathetically — this fence knows your kind.",
            "INS": "Your insulation tingles. You could walk right through this if you wanted.",
            "SPK": "Something about the fence's pattern feels like a puzzle with an obvious solution.",
        },
        "question": {
            "prompt": "The fence sign says DO NOT TOUCH (probably). What does the small print underneath read?",
            "answers": ["Unless you are very fast", "Unless you are well insulated", "Unless you really mean it"],
            "correct": 2,
        },
        "choices": [
            {
                "label": "⚡ Charge through it",
                "outcomes": {
                    "high": {"text": "Your VLT surges and you blast right through, sparks flying everywhere. The fence short-circuits behind you. Very cool.", "cp": 30, "tokens": 15, "tone": "good"},
                    "mid":  {"text": "You push through with a crackle. Your fur stands on end and you smell like ozone, but you made it.", "cp": 20, "tokens": 8, "tone": "neutral"},
                    "low":  {"text": "The fence zaps you back hard. You land in a bush. A nearby frog looks at you with what might be pity.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🔍 Look for a gap",
                "outcomes": {
                    "high": {"text": "You spot a clever gap near the third post. You slip through like a ghost. The fence never knew you were there.", "cp": 25, "tokens": 10, "tone": "good"},
                    "mid":  {"text": "You find a small gap and squeeze through, losing a bit of fluff on the wire. Worth it.", "cp": 18, "tokens": 6, "tone": "neutral"},
                    "low":  {"text": "You search for ages but find nothing. Eventually you just climb over and scrape your knee a little.", "cp": 10, "tokens": 3, "tone": "neutral"},
                }
            },
            {
                "label": "🎵 Sing to it",
                "outcomes": {
                    "high": {"text": "Incredibly, the fence responds. It hums back. You harmonize for a moment, and it politely opens a gate. Wild.", "cp": 35, "tokens": 20, "tone": "good"},
                    "mid":  {"text": "The fence doesn't open but you have a nice moment. You go around it feeling weirdly at peace.", "cp": 15, "tokens": 5, "tone": "neutral"},
                    "low":  {"text": "Nothing happens. Someone in the distance starts clapping sarcastically.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Sleeping Sparkmole",
        "image": "zone1_e2",
        "scene": (
            "A large Sparkmole is snoozing across the middle of the path, twitching in its sleep. "
            "It's blocking the way completely. "
            "Every few seconds it crackles with a tiny bolt of lightning."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "You could overpower this thing easily if it wakes up — but you'd rather not have to.",
            "INS": "If it sparks again while you're close, your insulation will handle it.",
            "SPK": "You have a feeling about this one. The timing of those bolts is almost musical.",
        },
        "question": {
            "prompt": "The Sparkmole sparks every few seconds. What do Sparkmoles eat in the wild?",
            "answers": ["Capacitor berries and charged roots", "Small rocks and dust", "Pure electricity from storm clouds"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🤫 Sneak past it",
                "outcomes": {
                    "high": {"text": "You move like electricity through a wire — silent, smooth, instant. The Sparkmole doesn't twitch. You find a shiny coin near its tail.", "cp": 28, "tokens": 12, "tone": "good"},
                    "mid":  {"text": "You make it past, barely. One paw snaps a twig. The Sparkmole's ear flicks but it stays asleep.", "cp": 18, "tokens": 6, "tone": "neutral"},
                    "low":  {"text": "You step on its tail. It wakes up confused and angry. You run in different directions. You both survive.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🍎 Leave it a snack",
                "outcomes": {
                    "high": {"text": "You leave a perfectly charged capacitor berry. It wakes up, sniffs it, and happily rolls aside. It seems grateful.", "cp": 30, "tokens": 15, "tone": "good"},
                    "mid":  {"text": "You leave a berry. It rolls over without waking and eats it in its sleep. You slip past.", "cp": 20, "tokens": 8, "tone": "neutral"},
                    "low":  {"text": "You don't have the right kind of snack. The Sparkmole ignores it and keeps sleeping. You're forced to jump over it and nearly land on its head.", "cp": 10, "tokens": 2, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Voltage Puddle",
        "image": "zone1_e3",
        "scene": (
            "A wide, shallow puddle blocks the trail. "
            "It shimmers faintly blue — either it's full of electricity or it's just a trick of the light. "
            "A small frog sitting next to it looks very smug."
        ),
        "stat": "INS",
        "trait_text": {
            "VLT": "Your charge makes the puddle ripple toward you like it recognizes something.",
            "INS": "Your insulation is basically built for this. The frog doesn't know who it's dealing with.",
            "SPK": "The frog's smugness is a clue. Something about this situation is a trick.",
        },
        "question": {
            "prompt": "The smug frog is sitting next to a possibly-electric puddle. What's the frog actually guarding?",
            "answers": ["The dry path around the back", "Nothing — it just likes puddles", "A hidden cache beneath the water"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🦘 Jump over it",
                "outcomes": {
                    "high": {"text": "You clear it with room to spare. The smug frog looks less smug. You wink at it.", "cp": 25, "tokens": 10, "tone": "good"},
                    "mid":  {"text": "You make it but your back paw clips the edge. Definitely electric. You shake it off.", "cp": 15, "tokens": 5, "tone": "neutral"},
                    "low":  {"text": "You don't make it. The puddle is absolutely electric. You make it across eventually, smelling of burnt hair.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🥾 Wade through it",
                "outcomes": {
                    "high": {"text": "Your insulation handles it perfectly. You walk through like it's nothing. The frog is baffled.", "cp": 30, "tokens": 15, "tone": "good"},
                    "mid":  {"text": "It stings but you power through. A bit of your charge gets drained but you reach the other side.", "cp": 18, "tokens": 6, "tone": "neutral"},
                    "low":  {"text": "Very bad idea. Very electric. You bounce out the other side vibrating like a tuning fork.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🐸 Ask the frog",
                "outcomes": {
                    "high": {"text": "The frog, impressed by your confidence, reveals a stone path just under the surface. Incredible. You cross dry.", "cp": 35, "tokens": 18, "tone": "good"},
                    "mid":  {"text": "The frog says 'ribbit' which you interpret as 'go left.' There IS a shallower section to the left. You get a little wet.", "cp": 20, "tokens": 8, "tone": "neutral"},
                    "low":  {"text": "The frog just stares at you. You stare back. Neither of you learns anything. You wade through anyway.", "cp": 8, "tokens": 2, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Lost Traveler",
        "image": "zone1_e4",
        "scene": (
            "A tiny Zappy is sitting on a rock looking extremely lost. "
            "They have a large backpack and a map that appears to be upside down. "
            "'Excuse me,' they say. 'Is this the way to the Apex?'"
        ),
        "stat": None,
        "trait_text": {
            "VLT": "They look at your charge with a mix of admiration and concern.",
            "INS": "You can see their equipment is poorly insulated. Rookie mistake.",
            "SPK": "Something about their energy feels familiar. Like you've met in a different run.",
        },
        "question": {
            "prompt": "The traveler's map is upside down. Which zone are they actually trying to reach?",
            "answers": ["Voltage Bay", "Molten Circuit", "The Null Space"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🗺️ Help them navigate",
                "outcomes": {
                    "high": {"text": "You help them reorient the map and point them right. They're so grateful they give you a handful of charged coins.", "cp": 30, "tokens": 20, "tone": "good"},
                    "mid":  {"text": "You help as best you can. They head off looking more confident. They leave a small candy as thanks.", "cp": 20, "tokens": 8, "tone": "good"},
                    "low":  {"text": "You help them but honestly you're not sure your directions were right either. You both leave feeling uncertain.", "cp": 12, "tokens": 3, "tone": "neutral"},
                }
            },
            {
                "label": "🏃 Keep moving",
                "outcomes": {
                    "high": {"text": "You nod and keep going. A strange guilt follows you for two minutes then fades. You find a coin on the path.", "cp": 15, "tokens": 5, "tone": "neutral"},
                    "mid":  {"text": "You keep moving. The little Zappy calls after you: 'It's fine! I'll figure it out!' You feel fine about this.", "cp": 10, "tokens": 3, "tone": "neutral"},
                    "low":  {"text": "You ignore them. You immediately trip over a root. Karma is fast in the Static Fields.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Glowing Mushroom",
        "image": "zone1_e5",
        "scene": (
            "A single enormous mushroom pulses with soft electric light at the side of the path. "
            "It doesn't look dangerous. It looks almost friendly. "
            "A small sign stuck in the ground next to it says: *Eat? Maybe.*"
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The mushroom's pulse syncs with your charge cycle. Suspicious.",
            "INS": "Your insulation would protect you from most of whatever that thing is.",
            "SPK": "You've heard stories. Mushrooms like this are either treasure or very bad news — your gut says treasure.",
        },
        "question": {
            "prompt": "The sign says 'Eat? Maybe.' What does a glowing mushroom in the Static Fields typically contain?",
            "answers": ["Stored solar charge and luck spores", "Mild neurotoxin and vitamins", "Just bioluminescent fungus, nothing special"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🍄 Take a bite",
                "outcomes": {
                    "high": {"text": "Delicious. You feel a surge of lucky energy. Everything seems slightly more favorable for the rest of the run.", "cp": 40, "tokens": 25, "tone": "good"},
                    "mid":  {"text": "It tastes like static electricity and strawberries. Weird. You feel fine, maybe even good.", "cp": 25, "tokens": 10, "tone": "neutral"},
                    "low":  {"text": "It tasted great but now you're briefly glowing in a way that attracts small insects. They mean no harm.", "cp": 10, "tokens": 2, "tone": "neutral"},
                }
            },
            {
                "label": "✨ Touch it gently",
                "outcomes": {
                    "high": {"text": "It pulses warmly and leaves a glowing mark on your paw. You feel watched over for the rest of the expedition.", "cp": 35, "tokens": 18, "tone": "good"},
                    "mid":  {"text": "It flickers happily. Nothing dramatic happens but it felt meaningful somehow.", "cp": 20, "tokens": 8, "tone": "neutral"},
                    "low":  {"text": "It zaps you. Not badly, just enough to let you know it has opinions about being touched.", "cp": 8, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "📸 Just look at it",
                "outcomes": {
                    "high": {"text": "You study it carefully and notice it's spelling out coordinates in its pulse pattern. You follow them to a hidden cache.", "cp": 45, "tokens": 30, "tone": "good"},
                    "mid":  {"text": "You observe it for a while. It's beautiful. You leave feeling calm and slightly wiser.", "cp": 18, "tokens": 6, "tone": "neutral"},
                    "low":  {"text": "You look at it for too long and now you see electric patterns everywhere you look. Probably fine.", "cp": 8, "tokens": 2, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Toll Bridge",
        "image": "zone1_e6",
        "scene": (
            "A rickety wooden bridge crosses a small stream. "
            "A very old Zappy in a hat is sitting in a chair at the entrance. "
            "'Toll,' he says, holding out a paw. 'Or a riddle. Your choice.'"
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "You could probably knock this bridge down and ford the stream. But that feels rude.",
            "INS": "The old Zappy eyes your insulation patches and gives a small approving nod.",
            "SPK": "Riddles. You were born for riddles. The old Zappy hasn't met anyone like you.",
        },
        "question": {
            "prompt": "The old Zappy asks: 'What has teeth but cannot eat?'",
            "answers": ["A comb", "A saw", "A key"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🧩 Answer the riddle",
                "outcomes": {
                    "high": {"text": "'What has teeth but cannot eat?' You answer instantly: a comb. He's delighted. He waves you across and tosses you a coin.", "cp": 35, "tokens": 20, "tone": "good"},
                    "mid":  {"text": "You think for a moment and get it right. He nods approvingly and lets you pass.", "cp": 22, "tokens": 8, "tone": "good"},
                    "low":  {"text": "You get it wrong. He sighs and lets you cross anyway because he's actually very kind. 'Work on it,' he says.", "cp": 10, "tokens": 3, "tone": "neutral"},
                }
            },
            {
                "label": "💰 Pay the toll",
                "outcomes": {
                    "high": {"text": "You pay generously. He's touched. He refuses to take the full amount and gives you back most of it plus a blessing.", "cp": 28, "tokens": 15, "tone": "good"},
                    "mid":  {"text": "You pay the toll. He pockets it, tips his hat, and waves you through.", "cp": 15, "tokens": 5, "tone": "neutral"},
                    "low":  {"text": "You pay the toll. It was a lot. You cross feeling slightly poorer but also slightly more mature.", "cp": 8, "tokens": 0, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Sparking Tree",
        "image": "zone1_e7",
        "scene": (
            "A tree at the bend in the path is covered in arcing electricity. "
            "Voltfruit hangs from its branches — rare, charged, valuable. "
            "The arcs look painful but not constant. There's a rhythm."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "The arcs flicker at a frequency your body knows instinctively.",
            "INS": "Your insulation would absorb most of this if you timed it right.",
            "SPK": "You can read the rhythm. There's a three-second window between the big arcs.",
        },
        "question": {
            "prompt": "Voltfruit grows on trees that arc electricity. What's the best time to harvest?",
            "answers": ["During a brief pause between arcs", "At night when the tree is dormant", "When the arcs are weakest near the roots"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⚡ Grab the fruit",
                "outcomes": {
                    "high": {"text": "You read the rhythm perfectly and grab three Voltfruit in a clean sweep during the gap. They crackle with stored energy.", "cp": 35, "tokens": 22, "tone": "good"},
                    "mid":  {"text": "You get one Voltfruit but take a small arc doing it. Sting was worth it.", "cp": 20, "tokens": 10, "tone": "neutral"},
                    "low":  {"text": "The tree zaps you before you're close. You respect it now. Deeply.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🌀 Try to ground it",
                "outcomes": {
                    "high": {"text": "You ground out the tree's charge through your feet and into the earth. The arcs stop. The fruit hangs still, yours for the taking.", "cp": 40, "tokens": 28, "tone": "good"},
                    "mid":  {"text": "Partial ground. Arcs reduce but don't stop. You grab what you can quickly.", "cp": 22, "tokens": 12, "tone": "neutral"},
                    "low":  {"text": "You are not a ground. The tree adds you to its arc pattern. Exit stage left.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "📝 Leave it alone",
                "trap": True,
                "outcomes": {
                    "high": {"text": "You wisely decide the fruit isn't worth the risk. The tree watches you go. It's a little sad.", "cp": 8, "tokens": 2, "tone": "neutral"},
                    "mid":  {"text": "You walk past without trying. The Voltfruit glows after you. Opportunity missed.", "cp": 8, "tokens": 2, "tone": "neutral"},
                    "low":  {"text": "You walk away and step into a hidden root that trips you. The tree seems amused.", "cp": 3, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Friendly Vendor",
        "image": "zone1_e8",
        "scene": (
            "A cheerful vendor has a cart set up in the middle of nowhere. "
            "'Special deals! Very legitimate!' A hand-painted sign lists: *Definitely Not Stolen Goods*. "
            "The vendor smiles too widely."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The vendor's smile falters slightly when they see your voltage readings.",
            "INS": "Your insulation buzzes near the cart. Something in there is electric.",
            "SPK": "Every instinct you have is saying something about this situation is off.",
        },
        "question": {
            "prompt": "The vendor's sign says 'Definitely Not Stolen Goods.' What does a smart Zappy do?",
            "answers": ["Ask for the origin documentation", "Walk away — nothing good here", "Buy something small as a test"],
            "correct": 1,
        },
        "choices": [
            {
                "label": "🛒 Buy something",
                "trap": True,
                "outcomes": {
                    "high": {"text": "You buy a 'charged artifact.' It dissolves in your hands two minutes later. Classic.", "cp": 5, "tokens": 0, "tone": "bad"},
                    "mid":  {"text": "You buy something. It breaks immediately. The vendor is gone when you turn around.", "cp": 5, "tokens": 0, "tone": "bad"},
                    "low":  {"text": "It was a trap. What you thought was a product was just painted air in a box. The vendor is very good at their job.", "cp": 3, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🚶 Keep walking",
                "outcomes": {
                    "high": {"text": "You don't even slow down. The vendor calls after you: 'Smart one!' You find a legitimate cache ten seconds down the path.", "cp": 35, "tokens": 20, "tone": "good"},
                    "mid":  {"text": "You walk past with a polite nod. Good call. You find a small coin on the path ahead.", "cp": 18, "tokens": 8, "tone": "neutral"},
                    "low":  {"text": "You walk past. The vendor sighs. Somewhere behind you a small explosion happens. You don't look back.", "cp": 10, "tokens": 3, "tone": "neutral"},
                }
            },
            {
                "label": "🔍 Inspect the goods",
                "outcomes": {
                    "high": {"text": "Your inspection reveals the goods are actually legitimate rare items, stolen yes, but valuable. You negotiate a fair price and both leave happy.", "cp": 40, "tokens": 25, "tone": "good"},
                    "mid":  {"text": "Your inspection reveals most things are junk but one item is real. You take it. Fair.", "cp": 20, "tokens": 10, "tone": "neutral"},
                    "low":  {"text": "Your inspection takes too long. The vendor gets nervous and packs up before you decide anything.", "cp": 8, "tokens": 0, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Charge Geyser",
        "image": "zone1_e9",
        "scene": (
            "A small geyser in the ground spurts pure electricity every thirty seconds or so. "
            "The spray is intense but brief. "
            "Last time it went off, it launched a small rock about twenty feet into the air."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "The geyser's charge profile matches yours almost exactly. Interesting.",
            "INS": "You could stand right over that thing if you wanted to. Might even be pleasant.",
            "SPK": "The geyser's timing is predictable once you watch it twice. Exactly thirty seconds.",
        },
        "question": {
            "prompt": "The geyser fires every thirty seconds. You've been watching for forty-five seconds and it's fired once. When does it fire next?",
            "answers": ["In about fifteen seconds", "In about thirty seconds", "In about forty-five seconds"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⬆️ Stand in the blast",
                "outcomes": {
                    "high": {"text": "You catch the geyser at full force and absorb every volt. You're launched upward and come down somewhere better, much more charged.", "cp": 42, "tokens": 28, "tone": "good"},
                    "mid":  {"text": "The geyser hits you good. You absorb some, lose some to the air. Net positive.", "cp": 25, "tokens": 12, "tone": "neutral"},
                    "low":  {"text": "The geyser hits you badly. More rock-launch than power-absorb. You land somewhere inconvenient.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🥄 Collect the runoff",
                "outcomes": {
                    "high": {"text": "You collect the runoff charge in a clever improvised container. Smart, safe, effective. The geyser gives generously.", "cp": 38, "tokens": 22, "tone": "good"},
                    "mid":  {"text": "You collect what you can. Not the full blast but a good share.", "cp": 22, "tokens": 10, "tone": "neutral"},
                    "low":  {"text": "Your container isn't good at holding electricity. You learn this quickly.", "cp": 8, "tokens": 2, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Rival Zappy",
        "image": "zone1_e10",
        "scene": (
            "You round a corner and almost walk right into another Zappy mid-expedition. "
            "You stare at each other for an awkward moment. "
            "There's only enough cache here for one of you."
        ),
        "stat": "SPK",
        "rival": True,
        "trait_text": {
            "VLT": "Your charge readout is clearly higher than theirs. They notice.",
            "INS": "You're better insulated. If this turns physical, that matters.",
            "SPK": "You read their intentions before they show them. You have the edge here.",
        },
        "question": {
            "prompt": "Another Zappy is blocking the cache. What's the fastest way to resolve this without a fight?",
            "answers": ["Propose a split", "Stare them down until they move", "Pretend the cache isn't there and walk past"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🤝 Propose a split",
                "outcomes": {
                    "high": {"text": "They agree immediately — turns out they're reasonable. You both get a share. They even share a tip about the next section.", "cp": 32, "tokens": 18, "tone": "good"},
                    "mid":  {"text": "They agree, a little reluctantly. You each take a portion and go your separate ways.", "cp": 20, "tokens": 8, "tone": "neutral"},
                    "low":  {"text": "They don't want to split. Stalemate. You leave with nothing after a very long silence.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "💪 Claim it first",
                "outcomes": {
                    "high": {"text": "Your speed is unmatched. You grab it before they move. They shrug and walk on. Winner.", "cp": 38, "tokens": 22, "tone": "good"},
                    "mid":  {"text": "Close race. You get most of it. They get the rest. Could have been worse.", "cp": 22, "tokens": 10, "tone": "neutral"},
                    "low":  {"text": "They were faster. They take the cache cleanly and walk off. You are humbled.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🚶 Let them have it",
                "outcomes": {
                    "high": {"text": "You walk away with grace. They're so surprised by this that they share half anyway. Unexpected.", "cp": 28, "tokens": 15, "tone": "good"},
                    "mid":  {"text": "You let them have it. They nod respectfully. You feel good about your choices.", "cp": 15, "tokens": 5, "tone": "neutral"},
                    "low":  {"text": "You let them have it. They take it all. You get nothing. Generosity is its own reward. Sometimes.", "cp": 8, "tokens": 0, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Weather Vane",
        "image": "zone1_e11",
        "scene": (
            "A large weather vane sits on a post at a fork in the road. "
            "Both paths look identical. "
            "The weather vane is spinning rapidly and not pointing anywhere useful."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "One path smells faintly of ozone. Your body knows which way the charge flows.",
            "INS": "The warmer path is the safer path. Your insulation picks up the difference.",
            "SPK": "Gut check: left or right. You already know. You just don't trust it yet.",
        },
        "question": {
            "prompt": "The weather vane spins with no direction. In Zappy lore, what does a spinning weather vane at a fork mean?",
            "answers": ["Both paths lead somewhere good", "One path loops back to the start", "The right answer isn't on either path"],
            "correct": 1,
        },
        "choices": [
            {
                "label": "⬅️ Go left",
                "outcomes": {
                    "high": {"text": "Left was correct. The path opens up into a shortcut with a reward cache tucked into the hedgerow.", "cp": 30, "tokens": 16, "tone": "good"},
                    "mid":  {"text": "Left works. Slightly longer route but nothing bad happens.", "cp": 18, "tokens": 7, "tone": "neutral"},
                    "low":  {"text": "Left loops back to where you started. You lose a few minutes.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "➡️ Go right",
                "outcomes": {
                    "high": {"text": "Right was the faster path. It cuts through a clearing with a small energy deposit in the center.", "cp": 30, "tokens": 16, "tone": "good"},
                    "mid":  {"text": "Right is fine. Unremarkable path, nothing good or bad.", "cp": 15, "tokens": 5, "tone": "neutral"},
                    "low":  {"text": "Right dead-ends. You backtrack. The weather vane spins at you mockingly.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🚧 Check the weather vane",
                "outcomes": {
                    "high": {"text": "You examine the base of the vane and find a small marker — a previous Zappy scratched an arrow pointing left. Left it is. Correct.", "cp": 35, "tokens": 20, "tone": "good"},
                    "mid":  {"text": "The vane gives you nothing useful. But you spot a path through the hedge that wasn't obvious. Shortcut.", "cp": 22, "tokens": 10, "tone": "neutral"},
                    "low":  {"text": "The weather vane is just a weather vane. You lose time examining it. Pick a direction.", "cp": 8, "tokens": 2, "tone": "neutral"},
                }
            },
        ]
    },
]


# ═══════════════════════════════════════════════════════
# ZONE 2 — VOLTAGE BAY
# Tone: coastal, energetic, playful danger
# ═══════════════════════════════════════════════════════

ZONE2_EVENTS = [
    {
        "title": "The Surge Tide",
        "image": "zone2_e1",
        "scene": (
            "The path runs right along the coast. "
            "Every few seconds, a wave of pure electrical energy surges up the beach. "
            "The air tastes like lightning and salt."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "Your body is already absorbing ambient charge from the air. The surge could supercharge you.",
            "INS": "The surge is intense but your insulation was built for exactly this.",
            "SPK": "You can feel the rhythm of the surges — there's a pattern here if you're patient.",
        },
        "question": {
            "prompt": "The surge tide arrives every few seconds. What determines the biggest surge?",
            "answers": ["When two wave patterns overlap", "High tide always brings the strongest surge", "The surges are completely random"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🌊 Ride the surge",
                "outcomes": {
                    "high": {"text": "You catch the wave perfectly, riding a current of pure voltage down the coast at incredible speed. You arrive at the next point feeling electric and alive.", "cp": 50, "tokens": 60, "tone": "good"},
                    "mid":  {"text": "You catch the edge of the surge and get flung forward a good distance. Chaotic, but effective.", "cp": 35, "tokens": 30, "tone": "neutral"},
                    "low":  {"text": "The surge catches you sideways and tumbles you up the beach. You're fine, sandy, and somewhat rearranged.", "cp": 10, "tokens": 8, "tone": "bad"},
                }
            },
            {
                "label": "⏱️ Time the gaps",
                "outcomes": {
                    "high": {"text": "You read the rhythm perfectly and slip through three gaps in the surge pattern like you've done it a hundred times. Textbook.", "cp": 45, "tokens": 50, "tone": "good"},
                    "mid":  {"text": "Your timing is decent. You make it through with one wet paw and no serious damage.", "cp": 28, "tokens": 20, "tone": "neutral"},
                    "low":  {"text": "Your timing is off. You get caught by a small surge. It's not the big one but it still stings and you drop your snack.", "cp": 12, "tokens": 8, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Wrecked Voltship",
        "image": "zone2_e2",
        "scene": (
            "Half a vessel is beached in the sand — some kind of electric sailing ship, long abandoned. "
            "Its hull still crackles with residual charge. "
            "Something gleams in the dark of the cargo hold."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "The residual charge in the hull feels like a second heartbeat when you get close.",
            "INS": "You could walk the hull safely — your insulation would handle the residuals.",
            "SPK": "The gleam in the hold is patterned. Someone left something specific in there.",
        },
        "question": {
            "prompt": "The voltship is beached and crackling. What's the safest way to enter a live wreck?",
            "answers": ["Ground yourself before entry", "Enter fast and exit fast", "Wait until the residual charge dissipates"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⚓ Board and explore",
                "outcomes": {
                    "high": {"text": "You board carefully and find the cargo hold packed with expedition supplies — untouched for years. Score.", "cp": 65, "tokens": 140, "tone": "good"},
                    "mid":  {"text": "You find a few useful items amid the wreckage. The hull shocks you twice but the haul was worth it.", "cp": 42, "tokens": 62, "tone": "neutral"},
                    "low":  {"text": "The hull is more live than it looked. You exit quickly with nothing. Alive is good.", "cp": 12, "tokens": 8, "tone": "bad"},
                }
            },
            {
                "label": "🧲 Pull items out remotely",
                "outcomes": {
                    "high": {"text": "Clever. You improvise a long hook and fish items out from the safe zone. You get the best pieces without risk.", "cp": 60, "tokens": 120, "tone": "good"},
                    "mid":  {"text": "Your makeshift hook works for some items. The heavier ones stay put.", "cp": 38, "tokens": 50, "tone": "neutral"},
                    "low":  {"text": "Your hook snags on something and won't release. You lose the hook. Nothing gained.", "cp": 10, "tokens": 8, "tone": "neutral"},
                }
            },
            {
                "label": "💡 Drain the charge first",
                "outcomes": {
                    "high": {"text": "You drain the hull's residual charge through a grounding rod you fashion from scrap. Once it's safe you take everything worth taking.", "cp": 70, "tokens": 155, "tone": "good"},
                    "mid":  {"text": "Partial drain. The hull calms down enough to enter briefly. You grab what you can.", "cp": 45, "tokens": 68, "tone": "neutral"},
                    "low":  {"text": "The drain method doesn't work on a saltwater hull. Physics. You learn.", "cp": 10, "tokens": 8, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Coastal Hermit",
        "image": "zone2_e3",
        "scene": (
            "An ancient Zappy sits on a rock at the cliffside, completely still except for their eyes. "
            "'I've been watching this bay for sixty years,' they say without turning. "
            "'Ask me something. Or don't. Either way you'll leave changed.'"
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The hermit's eyes flick to your charge and back. They seem impressed, or at least interested.",
            "INS": "The hermit nods slightly at your insulation. Old-timer respect.",
            "SPK": "The hermit's energy is strange — familiar, like something you almost remember.",
        },
        "question": {
            "prompt": "The hermit says they've watched this bay for sixty years. What's the most valuable thing a watcher learns?",
            "answers": ["The patterns that repeat", "The things that never change", "The moments that can't be predicted"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "❓ Ask about the bay",
                "outcomes": {
                    "high": {"text": "They tell you things no map shows — a submerged cache path, a surge window, a pattern in the tide that other Zappies have missed for years. Invaluable.", "cp": 70, "tokens": 155, "tone": "good"},
                    "mid":  {"text": "They share one useful thing: a timing tip for the next obstacle. Worth stopping.", "cp": 45, "tokens": 65, "tone": "good"},
                    "low":  {"text": "They speak in riddles you don't quite follow. You nod and leave respectfully confused.", "cp": 18, "tokens": 15, "tone": "neutral"},
                }
            },
            {
                "label": "🧘 Sit with them a while",
                "outcomes": {
                    "high": {"text": "You sit in silence. After a long time, the hermit places a hand on your shoulder and transfers something — a charge, a memory, a feeling. Meaningful.", "cp": 75, "tokens": 165, "tone": "good"},
                    "mid":  {"text": "You sit. The view is extraordinary. You leave calmer and more focused.", "cp": 50, "tokens": 70, "tone": "good"},
                    "low":  {"text": "You sit. It's peaceful but ultimately you're not sure you got anything out of it. The view was nice.", "cp": 20, "tokens": 15, "tone": "neutral"},
                }
            },
            {
                "label": "🏃 Keep moving",
                "outcomes": {
                    "high": {"text": "You nod politely and go. The hermit calls after you: 'The left fork. Always the left fork.' You don't understand yet.", "cp": 30, "tokens": 35, "tone": "neutral"},
                    "mid":  {"text": "You keep going. The hermit watches you go. The bay is beautiful behind you.", "cp": 18, "tokens": 15, "tone": "neutral"},
                    "low":  {"text": "You leave quickly. The hermit shakes their head. Something you could have learned remains unlearned.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Electric Eel Crossing",
        "image": "zone2_e4",
        "scene": (
            "The only bridge across this inlet has electric eels resting on it. "
            "About a dozen of them, coiled loosely. "
            "They look asleep but eels are hard to read."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "Your charge level is higher than theirs. They can sense it and they're cautious of you.",
            "INS": "If they go off, your insulation handles the first hit at least.",
            "SPK": "You can tell which ones are actually asleep and which are pretending. It's in the gills.",
        },
        "question": {
            "prompt": "Electric eels rest on warm surfaces. How do you tell a sleeping eel from an alert one?",
            "answers": ["Alert eels keep their tails curled tight", "Alert eels have their gills slightly flared", "There's no reliable way to tell"],
            "correct": 1,
        },
        "choices": [
            {
                "label": "🐍 Step over them carefully",
                "outcomes": {
                    "high": {"text": "You pick your path precisely, stepping where no eel reaches. You cross in total silence. Not a twitch.", "cp": 55, "tokens": 100, "tone": "good"},
                    "mid":  {"text": "Mostly good. One eel's tail catches your ankle. Minor shock. You make it across.", "cp": 35, "tokens": 45, "tone": "neutral"},
                    "low":  {"text": "You wake three of them immediately. You make it across but not gracefully. Several thousand volts later.", "cp": 10, "tokens": 8, "tone": "bad"},
                }
            },
            {
                "label": "📣 Make a loud noise",
                "trap": True,
                "outcomes": {
                    "high": {"text": "They wake up and scatter — but the scatter sends several off the bridge into the water while you cross quickly. Lucky.", "cp": 20, "tokens": 10, "tone": "neutral"},
                    "mid":  {"text": "They wake up immediately. The bridge becomes briefly electric. Very brief but memorable.", "cp": 8, "tokens": 0, "tone": "bad"},
                    "low":  {"text": "Loud noise was the wrong call. Every eel in a ten-foot radius is now fully awake and very annoyed.", "cp": 3, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🌉 Find another route",
                "outcomes": {
                    "high": {"text": "You locate a hidden rope crossing upstream. It's a bit of a swim but completely eel-free.", "cp": 50, "tokens": 85, "tone": "good"},
                    "mid":  {"text": "The other route is longer but safer. You arrive at the other side just fine.", "cp": 30, "tokens": 35, "tone": "neutral"},
                    "low":  {"text": "You spend a long time looking for another route and find nothing. Back to the bridge, but the eels have rearranged.", "cp": 12, "tokens": 8, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Signal Beacon",
        "image": "zone2_e5",
        "scene": (
            "A tall beacon tower on a cliff is sending out a distress pulse. "
            "No one seems to be responding. "
            "The light blinks in a pattern you can almost understand."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The beacon's output is drawing power from somewhere. You can feel it.",
            "INS": "The pattern is electromagnetic — your insulation dampens it slightly but you can still parse it.",
            "SPK": "You've seen this pattern before. Or something like it.",
        },
        "question": {
            "prompt": "The beacon blinks in a pattern: 3 short, 1 long, 3 short. What distress code is this?",
            "answers": ["SOS — the universal distress signal", "A navigation warning for rocks", "A power beacon for passing ships"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "📡 Decode the signal",
                "outcomes": {
                    "high": {"text": "You decode it perfectly: coordinates to a supply drop three minutes from here. You follow them and find it exactly where promised.", "cp": 65, "tokens": 137, "tone": "good"},
                    "mid":  {"text": "You decode part of it — enough to find a general direction. You search that area and find something useful.", "cp": 40, "tokens": 55, "tone": "neutral"},
                    "low":  {"text": "You stare at the pattern for a long time. It remains mysterious. You leave feeling slightly haunted.", "cp": 10, "tokens": 15, "tone": "neutral"},
                }
            },
            {
                "label": "🔧 Repair the beacon",
                "outcomes": {
                    "high": {"text": "You fix the beacon quickly. A distant ship receives the signal and drops a reward crate from altitude as thanks. It lands nearby.", "cp": 70, "tokens": 150, "tone": "good"},
                    "mid":  {"text": "You patch it up adequately. The signal improves. Someone radios a thanks and tells you where they left spare supplies.", "cp": 42, "tokens": 62, "tone": "good"},
                    "low":  {"text": "You try to repair it and make things slightly worse. The beacon now pulses irregularly. You leave quickly.", "cp": 8, "tokens": 15, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Stormwatch Post",
        "image": "zone2_e6",
        "scene": (
            "A decommissioned stormwatch post sits at a high point on the cliff. "
            "Inside: old equipment, a logbook, and a window with a view of the whole bay. "
            "The last entry in the logbook is from a long time ago."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The old equipment is still live — decades of stored charge in the capacitors.",
            "INS": "This post was designed for Zappies with strong insulation. You feel at home.",
            "SPK": "The logbook entries describe patterns that match what you've already seen today. Someone was paying attention.",
        },
        "question": {
            "prompt": "The last logbook entry reads: 'Cache 7 still active. Southeast corner, below the marker stone.' What do you do?",
            "answers": ["Go find Cache 7", "Update the logbook with your own entry", "Take the logbook as a reference"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "📒 Read the logbook",
                "outcomes": {
                    "high": {"text": "The logbook is a treasure — decades of storm patterns, cache locations, and notes from previous expeditions. You copy the relevant pages.", "cp": 68, "tokens": 145, "tone": "good"},
                    "mid":  {"text": "You find two useful entries in the logbook. One hints at a nearby cache. You follow it.", "cp": 42, "tokens": 60, "tone": "good"},
                    "low":  {"text": "The logbook is mostly weather data. Thorough but not useful to you right now.", "cp": 18, "tokens": 15, "tone": "neutral"},
                }
            },
            {
                "label": "🔭 Use the equipment",
                "outcomes": {
                    "high": {"text": "The old scanner still works. You spot a charge anomaly on the horizon — an unmarked cache location. You plot a course.", "cp": 72, "tokens": 160, "tone": "good"},
                    "mid":  {"text": "The equipment works partially. You get a general read on the zone ahead, enough to plan your approach.", "cp": 45, "tokens": 65, "tone": "good"},
                    "low":  {"text": "The equipment is too old to parse. You turn it on and it produces a sound like a sad trombone.", "cp": 15, "tokens": 8, "tone": "neutral"},
                }
            },
            {
                "label": "💤 Rest for a moment",
                "outcomes": {
                    "high": {"text": "You rest and recharge. The post has ambient electricity. You leave more restored than you arrived.", "cp": 55, "tokens": 90, "tone": "good"},
                    "mid":  {"text": "A brief rest. You feel steadier. The view is remarkable.", "cp": 32, "tokens": 38, "tone": "neutral"},
                    "low":  {"text": "You rest too long and lose your run momentum. Nothing bad happens but nothing good either.", "cp": 12, "tokens": 8, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Rival at the Inlet",
        "image": "zone2_e7",
        "rival": True,
        "scene": (
            "You reach a narrow inlet crossing — good cache on the other side. "
            "Another Zappy is already there, eyeing the same cache. "
            "You both reach the crossing at the same moment."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "Your charge output is measurably higher. They can feel it from here.",
            "INS": "You can take the crossing faster than them — insulation against surge tide means no hesitation.",
            "SPK": "You read their next move before they make it. Three steps ahead minimum.",
        },
        "question": {
            "prompt": "Two Zappies at one crossing. The rival looks ready to sprint. What's the optimal play?",
            "answers": ["Sprint first — speed wins", "Let them go and find a side route", "Challenge them to a fair race"],
            "correct": 2,
        },
        "choices": [
            {
                "label": "🏁 Race them",
                "outcomes": {
                    "high": {"text": "Dead even — you both cross simultaneously. You look at each other, laugh, and split the cache. Clean.", "cp": 60, "tokens": 120, "tone": "good"},
                    "mid":  {"text": "You edge them out. You take the larger share. They take it well.", "cp": 42, "tokens": 65, "tone": "neutral"},
                    "low":  {"text": "They were faster. They take the cache cleanly. You lose the race but gain a rival.", "cp": 10, "tokens": 8, "tone": "bad"},
                }
            },
            {
                "label": "🔀 Take a different route",
                "outcomes": {
                    "high": {"text": "Your alternate route is better — you find a second cache the other Zappy didn't know about. You win by going sideways.", "cp": 65, "tokens": 130, "tone": "good"},
                    "mid":  {"text": "The alternate route works but it's slower. You find a smaller cache on the way.", "cp": 35, "tokens": 45, "tone": "neutral"},
                    "low":  {"text": "The alternate route dead-ends. You lose time and the rival takes the main cache.", "cp": 8, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Sunken Relay Station",
        "image": "zone2_e8",
        "scene": (
            "Partially submerged at low tide: an old relay station, still transmitting faintly. "
            "The water around it crackles. "
            "The door is just barely above the waterline."
        ),
        "stat": "INS",
        "trait_text": {
            "VLT": "The station's transmissions are weak — you could boost them with a direct charge injection.",
            "INS": "The water is electric but your insulation was built for exactly this situation.",
            "SPK": "The relay station is transmitting something specific. If you tuned in you could decode it.",
        },
        "question": {
            "prompt": "A relay station is partially submerged in electrified water. What's the first thing you check before entering?",
            "answers": ["Is the door above the waterline", "Is the power still active inside", "Is there a dry approach path"],
            "correct": 2,
        },
        "choices": [
            {
                "label": "🚪 Enter the station",
                "outcomes": {
                    "high": {"text": "You wade in with full insulation active. The station interior is dry, intact, and stocked with old supplies still perfectly preserved.", "cp": 70, "tokens": 155, "tone": "good"},
                    "mid":  {"text": "You make it inside. It's damp and somewhat live but you find a few salvageable items.", "cp": 45, "tokens": 70, "tone": "neutral"},
                    "low":  {"text": "The water around the door is too active. You take a significant hit before retreating.", "cp": 8, "tokens": 8, "tone": "bad"},
                }
            },
            {
                "label": "📡 Tap into the transmissions",
                "outcomes": {
                    "high": {"text": "From outside, you jack into the antenna output. The transmissions contain coordinates for a nearby cache network. Six locations.", "cp": 75, "tokens": 170, "tone": "good"},
                    "mid":  {"text": "You decode one cache location from the transmissions. Worth the effort.", "cp": 48, "tokens": 72, "tone": "good"},
                    "low":  {"text": "The transmissions are too degraded to decode. Static and fragments.", "cp": 15, "tokens": 15, "tone": "neutral"},
                }
            },
        ]
    },
]


# ═══════════════════════════════════════════════════════
# ZONE 3 — MOLTEN CIRCUIT
# Tone: intense, industrial, hot, chaotic
# ═══════════════════════════════════════════════════════

ZONE3_EVENTS = [
    {
        "title": "The Overloaded Conduit",
        "image": "zone3_e1",
        "scene": (
            "A massive power conduit runs across the path, shaking violently. "
            "It's overloaded — energy crackles off it in waves of heat and light. "
            "The only way forward is through the gap underneath."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": f"Your body reads the conduit's frequency instantly. This is your element.",
            "INS": "Your insulation is rated for this. Barely, but rated.",
            "SPK": "You've seen conduits like this before. The gap is safer in the first third.",
        },
        "question": {
            "prompt": "An overloaded conduit discharges in waves. What's the safest crossing window?",
            "answers": ["Immediately after a discharge — brief calm follows", "At peak load — the system is too busy to notice you", "During a lull before the next buildup"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⚡ Absorb the overload",
                "outcomes": {
                    "high": {"text": "You take the full surge directly and channel it through your body in a controlled discharge on the other side. You feel incredible. The conduit stabilizes.", "cp": 80, "tokens": 162, "tone": "good"},
                    "mid":  {"text": "You absorb what you can. Some gets through you wildly. You make it across supercharged and slightly smoking.", "cp": 55, "tokens": 87, "tone": "neutral"},
                    "low":  {"text": "The overload is too much. It throws you backward with considerable enthusiasm.", "cp": 10, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "🏃 Sprint under it",
                "outcomes": {
                    "high": {"text": "Pure speed. You're under and through in under a second. The conduit doesn't even know you were there.", "cp": 70, "tokens": 137, "tone": "good"},
                    "mid":  {"text": "You sprint through. A bolt clips your ear. You make it and it barely counted.", "cp": 48, "tokens": 70, "tone": "neutral"},
                    "low":  {"text": "Not fast enough. The conduit clips you mid-sprint. You tumble through and land in a heap on the other side, successful but destroyed.", "cp": 20, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "🔌 Find the breaker",
                "outcomes": {
                    "high": {"text": "You locate the emergency breaker panel behind a maintenance cover. You cut the flow, walk through calmly, then restore it. Professional.", "cp": 85, "tokens": 175, "tone": "good"},
                    "mid":  {"text": "You find the panel and reduce the load enough to safely pass. The conduit hums its approval.", "cp": 60, "tokens": 100, "tone": "neutral"},
                    "low":  {"text": "You find the panel but activate the wrong switch. The conduit gets louder somehow. You run.", "cp": 15, "tokens": 15, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Lava Shard Field",
        "image": "zone3_e2",
        "scene": (
            "The ground is covered in cooled lava shards — sharp, black, and still radiating heat. "
            "Walking normally would be very uncomfortable. "
            "A route exists if you can find it."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The shards are conductive. Your charge flickers through them when you get close.",
            "INS": "Your insulation handles the heat. The shards are still sharp though.",
            "SPK": "Cool shards are darker. You can read the safe path like a map if you squint.",
        },
        "question": {
            "prompt": "You need to cross a lava shard field. How do you identify the cooled, safe stones?",
            "answers": ["Cooled stones are darker and matte", "Cooled stones ring hollow when tapped", "Cooled stones are larger and flatter"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🎯 Find the safe path",
                "outcomes": {
                    "high": {"text": "Your instincts are perfect. You hop stone to stone, each one cool, each landing exact. You cross without a scratch.", "cp": 75, "tokens": 150, "tone": "good"},
                    "mid":  {"text": "You find a decent path. A few hot spots but manageable. You arrive warm-pawed but intact.", "cp": 50, "tokens": 75, "tone": "neutral"},
                    "low":  {"text": "Your path is mostly wrong. It's fine. Hot but fine. You have a high pain tolerance probably.", "cp": 18, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "🛡️ Power through",
                "outcomes": {
                    "high": {"text": "Your insulation handles the heat effortlessly. You walk straight across like it's carpet.", "cp": 70, "tokens": 137, "tone": "good"},
                    "mid":  {"text": "Your insulation helps but not perfectly. You make it across singed but successful.", "cp": 45, "tokens": 62, "tone": "neutral"},
                    "low":  {"text": "Very hot. Very painful. You make it but you are not comfortable. Take a moment.", "cp": 12, "tokens": 15, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Rogue Automaton",
        "image": "zone3_e3",
        "scene": (
            "A large mechanical automaton is standing in the path, sparking erratically. "
            "Its eyes glow red then blue then red again. "
            "It looks at you. It raises one arm. It makes a sound like a question."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "The automaton's power output fluctuates when you approach — it knows what you are.",
            "INS": "Its sparking is random but your insulation covers the blast radius.",
            "SPK": "It made a sound like a question. That means it's in diagnostic mode — still responsive.",
        },
        "question": {
            "prompt": "The automaton is sparking erratically with cycling eye colors. What does this indicate?",
            "answers": ["Power loop failure — it's stuck in a restart cycle", "It's fully functional and defending territory", "Low battery — it's harmless soon"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⚔️ Fight it",
                "outcomes": {
                    "high": {"text": "Your VLT overwhelms its systems in three precise strikes. It shuts down and, interestingly, opens its chest cavity to reveal a reward module.", "cp": 90, "tokens": 187, "tone": "good"},
                    "mid":  {"text": "A tough fight. You win but your circuits take a hit. You collect the parts it drops.", "cp": 58, "tokens": 95, "tone": "neutral"},
                    "low":  {"text": "It wins the fight easily. You leave quickly. The automaton watches you go, its eyes cycling yellow.", "cp": 10, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "🤝 Greet it calmly",
                "outcomes": {
                    "high": {"text": "It was just lost and confused. You communicate in blinking patterns and help it find its charging station. It gives you its spare power cell as thanks.", "cp": 85, "tokens": 175, "tone": "good"},
                    "mid":  {"text": "It calms down slightly and lets you pass, keeping its eyes on you the whole time.", "cp": 45, "tokens": 50, "tone": "neutral"},
                    "low":  {"text": "It does not respond to calm. It raises both arms now. You leave.", "cp": 10, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "🔧 Attempt repairs",
                "outcomes": {
                    "high": {"text": "You find its reset panel and stabilize the erratic loop. It reboots, thanks you in binary, and steps aside with a gift.", "cp": 95, "tokens": 200, "tone": "good"},
                    "mid":  {"text": "Your repairs are partial. It stops sparking but still watches you suspiciously. You pass without incident.", "cp": 55, "tokens": 75, "tone": "neutral"},
                    "low":  {"text": "You make it worse. It starts spinning. You run while it spins.", "cp": 8, "tokens": 15, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Thermal Vent Network",
        "image": "zone3_e4",
        "scene": (
            "A maze of thermal vents crisscrosses the area, each one venting superheated plasma every few seconds. "
            "The rhythm is irregular. "
            "You can see the exit from here — it's just a matter of timing."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "You could take the hits from the vents if you had to. Not ideal but survivable.",
            "INS": "Each vent blast would be absorbed. You could just walk straight through.",
            "SPK": "The rhythm is irregular — but there's a base pattern underneath. You're close to cracking it.",
        },
        "question": {
            "prompt": "The thermal vents fire irregularly. You've watched for 90 seconds. What's the safest strategy?",
            "answers": ["Identify the vent with the longest rest cycle and hug it", "Sprint the entire path without stopping", "Move during vents — they won't fire twice in a row"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🎲 Go for it",
                "outcomes": {
                    "high": {"text": "Your timing is supernatural. You thread through every vent at exactly the right moment. You don't even feel the heat.", "cp": 85, "tokens": 175, "tone": "good"},
                    "mid":  {"text": "Your timing is mostly right. Two vents catch you but not directly. You make it through smoky.", "cp": 55, "tokens": 80, "tone": "neutral"},
                    "low":  {"text": "Your timing is quite bad. You get vented. Multiple times. You arrive on the other side glowing.", "cp": 12, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "📊 Map the pattern",
                "outcomes": {
                    "high": {"text": "You watch for exactly one minute and crack the pattern. Your crossing is mathematically perfect.", "cp": 90, "tokens": 187, "tone": "good"},
                    "mid":  {"text": "You map most of the pattern. One vent is unpredictable. You get that one, but only that one.", "cp": 60, "tokens": 95, "tone": "neutral"},
                    "low":  {"text": "The pattern is genuinely random. Your mapping was pointless. You learn something about chaos.", "cp": 18, "tokens": 15, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Circuit Forge",
        "image": "zone3_e5",
        "scene": (
            "An abandoned forge sits at a crossroads, still hot, still functional. "
            "Materials are scattered around it. "
            "A faded sign reads: *MAKE SOMETHING. LEAVE SOMETHING.*"
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "The forge runs on direct voltage. You could power it yourself if needed.",
            "INS": "The heat here is severe but your insulation has you covered.",
            "SPK": "The scattered materials are sorted in a way that suggests someone left instructions.",
        },
        "question": {
            "prompt": "The forge sign says 'MAKE SOMETHING. LEAVE SOMETHING.' What's the correct interpretation?",
            "answers": ["Create something and leave it at the forge for the next traveler", "Make something for yourself and leave your old equipment behind", "It's a general philosophy, not a literal instruction"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🔨 Forge a weapon",
                "outcomes": {
                    "high": {"text": "You craft a small charged blade with perfect technique. It hums in your grip. You leave it on the sign as instructed, but keep the surplus metal — which happens to be very valuable.", "cp": 95, "tokens": 212, "tone": "good"},
                    "mid":  {"text": "Your forge work is decent. You make something functional and leave it. The act of creating feels rewarding.", "cp": 60, "tokens": 100, "tone": "good"},
                    "low":  {"text": "Your forge work is rough. You make something. It's unclear what. You leave it. The sign accepts it without judgment.", "cp": 20, "tokens": 15, "tone": "neutral"},
                }
            },
            {
                "label": "⚡ Charge yourself here",
                "outcomes": {
                    "high": {"text": "You use the forge as a personal charging station. You leave fully recharged and find the heat has annealed your stats for the rest of the run.", "cp": 80, "tokens": 175, "tone": "good"},
                    "mid":  {"text": "Good charge. You feel stronger. You leave a piece of scrap metal as your contribution.", "cp": 55, "tokens": 80, "tone": "good"},
                    "low":  {"text": "The forge charges you but also singes you somewhat. Mixed results.", "cp": 22, "tokens": 20, "tone": "neutral"},
                }
            },
            {
                "label": "🎁 Leave a gift",
                "outcomes": {
                    "high": {"text": "You leave something meaningful. The forge responds — a hidden drawer opens beneath it, filled with items left by previous visitors.", "cp": 100, "tokens": 225, "tone": "good"},
                    "mid":  {"text": "You leave something. The forge glows warmly. You feel good about this.", "cp": 50, "tokens": 70, "tone": "good"},
                    "low":  {"text": "You don't have much to leave. You leave what you have. That counts.", "cp": 25, "tokens": 25, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Factory Shortcut",
        "image": "zone3_e6",
        "scene": (
            "An old factory complex offers a shortcut through — but through the active production floor. "
            "Machines still run. Sparks still fly. "
            "A sign at the entrance says: *AUTHORIZED ZAPPIES ONLY.*"
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "The machines hum at your frequency. You could sync with them rather than dodge them.",
            "INS": "The production sparks are insulation-grade hazards. You're rated for this.",
            "SPK": "The machine patterns are predictable. You've seen industrial rhythms like this before.",
        },
        "question": {
            "prompt": "The sign says 'Authorized Zappies Only.' On the factory floor, what does this actually mean?",
            "answers": ["Zappies with VLT high enough to match machine frequency", "Zappies with prior factory certification", "Any Zappy who enters without asking — confidence is the authorization"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🏭 Walk the floor",
                "outcomes": {
                    "high": {"text": "Your VLT syncs with the machinery. The machines part around you like they know you. You walk straight through unharmed and find the supply depot at the far end.", "cp": 92, "tokens": 200, "tone": "good"},
                    "mid":  {"text": "The floor is chaotic but manageable. You dodge most things and only take a few glancing hits.", "cp": 60, "tokens": 105, "tone": "neutral"},
                    "low":  {"text": "The machines definitely don't recognize your authorization. You exit via the window.", "cp": 12, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "🔎 Find the control room",
                "outcomes": {
                    "high": {"text": "You locate the control room on a catwalk above the floor. From there you shut down one machine line, creating a clear corridor. Professional.", "cp": 95, "tokens": 210, "tone": "good"},
                    "mid":  {"text": "You find the control room but can only slow the machines, not stop them. It helps though.", "cp": 62, "tokens": 108, "tone": "neutral"},
                    "low":  {"text": "The control room is locked. You spend time at the door and eventually give up.", "cp": 15, "tokens": 15, "tone": "neutral"},
                }
            },
            {
                "label": "🚪 Use the outside path",
                "outcomes": {
                    "high": {"text": "The outside path is longer but reveals an overlooked maintenance shed with fully stocked supplies.", "cp": 75, "tokens": 155, "tone": "good"},
                    "mid":  {"text": "You go around. It's fine. Nothing special but nothing dangerous.", "cp": 42, "tokens": 55, "tone": "neutral"},
                    "low":  {"text": "You go around. The path is long and you lose time. You arrive behind schedule.", "cp": 20, "tokens": 15, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Pressure Valve",
        "image": "zone3_e7",
        "scene": (
            "A pressure valve the size of a house shudders on the wall beside the path. "
            "It's clearly about to blow. "
            "You have maybe thirty seconds to do something about it or take the full blast."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "You could absorb this if you positioned yourself right. Dangerous but possible.",
            "INS": "Your insulation is rated for pressure blasts. You've been in worse.",
            "SPK": "The valve's pressure gauge is readable. You know exactly when it fires.",
        },
        "question": {
            "prompt": "A pressure valve is about to blow. The release mechanism is a red handle on the left. What does turning it do?",
            "answers": ["Releases pressure safely in a controlled direction", "Seals the valve permanently", "Triggers an emergency shutdown of the whole system"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🔴 Turn the release handle",
                "outcomes": {
                    "high": {"text": "You turn it smoothly. The pressure releases in a controlled column upward, blowing a maintenance hatch open above you — and depositing its contents at your feet.", "cp": 88, "tokens": 185, "tone": "good"},
                    "mid":  {"text": "The release works but the pressure column goes sideways. You take a partial blast. Fine.", "cp": 58, "tokens": 90, "tone": "neutral"},
                    "low":  {"text": "You turned the wrong handle. The blast goes everywhere. You survive but it was not graceful.", "cp": 12, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "🏃 Run",
                "outcomes": {
                    "high": {"text": "Your speed gets you clear of the blast radius. The explosion behind you is spectacular. You collect debris that lands near you.", "cp": 70, "tokens": 140, "tone": "good"},
                    "mid":  {"text": "You mostly clear it. Clipped by the edge. Still fine, still moving.", "cp": 45, "tokens": 60, "tone": "neutral"},
                    "low":  {"text": "You don't run fast enough. The blast catches you square.", "cp": 8, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "🛡️ Brace and absorb",
                "outcomes": {
                    "high": {"text": "You turn into the blast and absorb the entire pressure front. The force charges you significantly. You stand in the smoke, unhurt, powerful.", "cp": 95, "tokens": 205, "tone": "good"},
                    "mid":  {"text": "You absorb most of it. Staggered but standing. The charge from it helped.", "cp": 60, "tokens": 95, "tone": "neutral"},
                    "low":  {"text": "The blast is more than you can absorb. It's a lot. You end up somewhere new.", "cp": 10, "tokens": 15, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Industrial Rival",
        "image": "zone3_e8",
        "rival": True,
        "scene": (
            "On the far side of the factory floor, you spot another Zappy who got here a different way. "
            "They're heading for the same resource node. "
            "The factory between you is still active."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "Your charge is higher. In a straight contest here, you win.",
            "INS": "You can take the factory floor hits they can't. Faster path through.",
            "SPK": "You spot a control switch they haven't noticed. First one to reach it controls the floor.",
        },
        "question": {
            "prompt": "You and a rival are racing across a factory floor. The rival hasn't noticed the control switch. What do you do?",
            "answers": ["Use the switch to clear your path", "Ignore it and race straight through", "Alert the rival to the switch — cooperation"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⚡ Race straight through",
                "outcomes": {
                    "high": {"text": "Your VLT punches through every obstacle on the floor. You arrive first. The rival claps slowly from the other end.", "cp": 92, "tokens": 200, "tone": "good"},
                    "mid":  {"text": "You both arrive at roughly the same time. Tense negotiation follows. You end up with a fair split.", "cp": 62, "tokens": 108, "tone": "neutral"},
                    "low":  {"text": "The floor beats you up. The rival gets there first and takes the node.", "cp": 12, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "🔧 Hit the control switch",
                "outcomes": {
                    "high": {"text": "The switch clears your path entirely. You walk across while the rival fights their way through the active section. Easy win.", "cp": 98, "tokens": 215, "tone": "good"},
                    "mid":  {"text": "The switch helps but only partly. Still faster than the rival though.", "cp": 65, "tokens": 115, "tone": "neutral"},
                    "low":  {"text": "You reach for the switch but the rival spots it at the same moment. They're closer.", "cp": 20, "tokens": 20, "tone": "neutral"},
                }
            },
        ]
    },
]


# ═══════════════════════════════════════════════════════
# ZONE 4 — THE NULL SPACE
# Tone: strange, surreal, physics-optional
# ═══════════════════════════════════════════════════════

ZONE4_EVENTS = [
    {
        "title": "The Mirror Corridor",
        "image": "zone4_e1",
        "scene": (
            "You enter a corridor where every surface reflects you — but the reflections are slightly wrong. "
            "One of them waves at you when you don't wave. "
            "One of them is running when you're standing still."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "Your reflection is brighter than you are. It seems to approve.",
            "INS": "Your reflection has better insulation. You feel judged.",
            "SPK": "You recognize the running reflection's destination. You've been there before — in a different run.",
        },
        "question": {
            "prompt": "In the Null Space, a reflection acts independently. What does this indicate?",
            "answers": ["It's a projection of a possible future version of you", "The mirror is defective", "It's another Zappy using a reflection disguise"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "👋 Wave back",
                "outcomes": {
                    "high": {"text": "The reflection that waved reaches through the mirror and hands you a small glowing object before vanishing. You don't ask questions.", "cp": 120, "tokens": 330, "tone": "good"},
                    "mid":  {"text": "The reflection nods. Something passes between you. The corridor feels less hostile after that.", "cp": 80, "tokens": 165, "tone": "neutral"},
                    "low":  {"text": "The reflection waves back at you waving back. This continues for an uncomfortable amount of time.", "cp": 20, "tokens": 25, "tone": "neutral"},
                }
            },
            {
                "label": "🏃 Follow the running one",
                "outcomes": {
                    "high": {"text": "You run with it and it leads you through a shortcut in the mirror logic. You exit the corridor faster than should be possible.", "cp": 130, "tokens": 360, "tone": "good"},
                    "mid":  {"text": "You run but can't keep up. The reflection disappears. You exit the normal way but feel like you almost understood something.", "cp": 70, "tokens": 120, "tone": "neutral"},
                    "low":  {"text": "You run and get completely turned around. You exit from the entrance. The running reflection is gone.", "cp": 15, "tokens": 25, "tone": "bad"},
                }
            },
            {
                "label": "🙈 Ignore them all",
                "outcomes": {
                    "high": {"text": "Your refusal to engage breaks the corridor's logic. It glitches and deposits you at the far end with a reward it couldn't justify keeping.", "cp": 115, "tokens": 300, "tone": "good"},
                    "mid":  {"text": "You walk through without looking. The reflections seem disappointed. You feel a light impact as something bounces off your back — a small coin.", "cp": 65, "tokens": 105, "tone": "neutral"},
                    "low":  {"text": "You ignore them but they don't ignore you. One reflection follows alongside you making noise until you exit.", "cp": 18, "tokens": 25, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Inverted Cache",
        "image": "zone4_e2",
        "scene": (
            "Supply crates are floating six feet above the path, gently rotating. "
            "Gravity in this section of the Null Space is having a day. "
            "The crates are full — you can hear them."
        ),
        "stat": "INS",
        "trait_text": {
            "VLT": "You could throw a charge bolt at the crates and knock them down — risky but direct.",
            "INS": "You've operated in inverted gravity before. Your center of mass is adaptable.",
            "SPK": "The rotation pattern of the crates follows a predictable arc. They pass within reach every eight seconds.",
        },
        "question": {
            "prompt": "Crates are floating in inverted gravity. What's the most reliable way to retrieve them?",
            "answers": ["Wait for their rotation to bring them within reach", "Jump into the inverted field and grab them", "Use a long implement to hook them from below"],
            "correct": 2,
        },
        "choices": [
            {
                "label": "🦘 Jump for them",
                "outcomes": {
                    "high": {"text": "You launch yourself into the inverted field and collect three crates in a clean arc before dropping back down.", "cp": 125, "tokens": 345, "tone": "good"},
                    "mid":  {"text": "You grab one crate before the inversion disorients you. Still good.", "cp": 82, "tokens": 170, "tone": "neutral"},
                    "low":  {"text": "The inversion catches you mid-jump. You spin for a while before the field deposits you back on the ground, empty-handed.", "cp": 18, "tokens": 25, "tone": "bad"},
                }
            },
            {
                "label": "🎣 Find something to fish with",
                "outcomes": {
                    "high": {"text": "You rig a long line with a hook and fish the supplies down from outside the zone. Clever, safe, effective.", "cp": 120, "tokens": 324, "tone": "good"},
                    "mid":  {"text": "Your makeshift line works for some of the items. The heavier ones stay up.", "cp": 75, "tokens": 135, "tone": "neutral"},
                    "low":  {"text": "You can't find anything suitable. A stick and a piece of wire is not a fishing rod.", "cp": 18, "tokens": 25, "tone": "neutral"},
                }
            },
            {
                "label": "⚡ Blast them down",
                "trap": True,
                "outcomes": {
                    "high": {"text": "Your bolt hits a crate. The inversion field collapses. All the crates fall at once, including onto you.", "cp": 40, "tokens": 40, "tone": "neutral"},
                    "mid":  {"text": "You blast one crate down. It falls hard and breaks. Half the contents are salvageable.", "cp": 35, "tokens": 35, "tone": "neutral"},
                    "low":  {"text": "The blast destabilizes the local gravity field. Things go chaotic briefly. You exit with nothing intact.", "cp": 8, "tokens": 25, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Whispering Frequency",
        "image": "zone4_e3",
        "scene": (
            "A low hum fills the air — not quite a sound, more like a feeling in your teeth. "
            "You can almost make out words. "
            "The hum seems to be trying to tell you something specific."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The frequency is electromagnetic. Your charge is interacting with it.",
            "INS": "Your insulation is slightly dampening the frequency. You'd hear it clearer without it.",
            "SPK": "You can almost parse it. It's telling you something about what's ahead.",
        },
        "question": {
            "prompt": "A frequency in the Null Space seems to carry information. What's the best way to decode it?",
            "answers": ["Hum back at the same frequency to establish resonance", "Write down the pattern and analyze it", "Filter out background noise and listen for repetition"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "👂 Listen carefully",
                "outcomes": {
                    "high": {"text": "You tune in perfectly and the hum resolves into clear information: the location of a hidden cache thirty seconds from here. You find it.", "cp": 135, "tokens": 375, "tone": "good"},
                    "mid":  {"text": "You catch fragments — enough to know which direction to go. You find something, though not the main thing.", "cp": 88, "tokens": 186, "tone": "neutral"},
                    "low":  {"text": "You can't make anything of it. The hum continues. You move on with it following you faintly.", "cp": 20, "tokens": 25, "tone": "neutral"},
                }
            },
            {
                "label": "📣 Hum back",
                "outcomes": {
                    "high": {"text": "You hit exactly the right frequency. The hum surges joyfully and showers you with stored energy.", "cp": 140, "tokens": 390, "tone": "good"},
                    "mid":  {"text": "You hum back and something shifts. The environment feels more navigable. You find an easier path.", "cp": 90, "tokens": 195, "tone": "good"},
                    "low":  {"text": "The hum falls silent. You hum alone for a moment. Nothing. You continue.", "cp": 18, "tokens": 25, "tone": "neutral"},
                }
            },
            {
                "label": "🚫 Block it out",
                "outcomes": {
                    "high": {"text": "Your mental discipline blocks the hum completely. In the sudden silence you notice something the hum was hiding — a concealed entrance to a side room.", "cp": 130, "tokens": 345, "tone": "good"},
                    "mid":  {"text": "You block most of it and move through quickly. Less rattled than you would have been.", "cp": 70, "tokens": 114, "tone": "neutral"},
                    "low":  {"text": "You can't block it. The hum is in your bones now. You carry it for the rest of the run.", "cp": 12, "tokens": 25, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Probability Storm",
        "image": "zone4_e4",
        "scene": (
            "A small but intense probability storm sits in the path. "
            "Inside it, things are sometimes good and sometimes not. "
            "A sign nearby lists outcomes ranging from 'wonderful' to 'extremely educational.'"
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "High VLT Zappies perform better in probability fields — charge stabilizes quantum fluctuations.",
            "INS": "Your insulation doesn't help here. This is a probability problem, not a heat problem.",
            "SPK": "The 'wonderful' and 'educational' columns on the sign have different entry points. You can see which one you'd enter from.",
        },
        "question": {
            "prompt": "The probability storm sign lists 'wonderful' and 'extremely educational' outcomes. How do you increase your odds of wonderful?",
            "answers": ["Enter from the upwind side — charge concentrates there", "Go around — the path around always yields residue", "Run through fast — exposure time reduces risk"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🎰 Walk straight through",
                "outcomes": {
                    "high": {"text": "The probability field reads your SPK and assigns you a wonderful outcome. You emerge with a significant reward and some lingering good luck.", "cp": 150, "tokens": 420, "tone": "good"},
                    "mid":  {"text": "The field is neutral to you. You emerge with moderate rewards and one slightly strange memory.", "cp": 90, "tokens": 195, "tone": "neutral"},
                    "low":  {"text": "The field does not favor you today. Extremely educational.", "cp": 10, "tokens": 25, "tone": "bad"},
                }
            },
            {
                "label": "🧲 Attract it toward you",
                "outcomes": {
                    "high": {"text": "You pull the best of the probability storm's potential toward you through sheer luck. The good outcomes crystallize and fall into your hands.", "cp": 160, "tokens": 450, "tone": "good"},
                    "mid":  {"text": "You attract some probability. Mostly neutral outcomes with one bright spot.", "cp": 95, "tokens": 210, "tone": "neutral"},
                    "low":  {"text": "You attract the storm but not the good parts of it. Several things go slightly wrong at once.", "cp": 8, "tokens": 25, "tone": "bad"},
                }
            },
            {
                "label": "🚶 Go around it",
                "outcomes": {
                    "high": {"text": "Going around was the right call. You find the path around is actually better — calmer, faster, and stocked with items the probability storm shook loose.", "cp": 140, "tokens": 375, "tone": "good"},
                    "mid":  {"text": "You go around safely. Boring but fine. You collect a small amount of probability residue from the edge.", "cp": 78, "tokens": 144, "tone": "neutral"},
                    "low":  {"text": "You go around. It takes longer than expected. The path is uneventful in a disappointing way.", "cp": 30, "tokens": 30, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Time-Delayed Echo",
        "image": "zone4_e5",
        "scene": (
            "Your own voice comes back to you from thirty seconds in the future. "
            "You hear yourself say something, but you haven't said it yet. "
            "You have thirty seconds to decide if that's what you want to say."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "Your charge creates a feedback loop with the echo. It's slightly louder for you than it should be.",
            "INS": "The echo bounces off your insulation at a slightly different angle. You hear more detail.",
            "SPK": "You already know what to say. The question is whether you trust that.",
        },
        "question": {
            "prompt": "Your voice returns from the future saying: 'The left path is safe.' You haven't reached the fork yet. What do you do?",
            "answers": ["Take the left path — trust the echo", "Test it by saying something false first", "Ignore it — self-fulfilling prophecy is unreliable"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🔮 Say what you heard",
                "outcomes": {
                    "high": {"text": "You say exactly what you heard. The time loop closes cleanly. A reward materializes from the resolved paradox.", "cp": 155, "tokens": 435, "tone": "good"},
                    "mid":  {"text": "You say it close enough. The echo accepts this. Something good happens nearby.", "cp": 95, "tokens": 204, "tone": "neutral"},
                    "low":  {"text": "You say it wrong somehow. The echo multiplies. You now have several voices saying slightly different things. Move on quickly.", "cp": 15, "tokens": 25, "tone": "bad"},
                }
            },
            {
                "label": "🤐 Say something else",
                "outcomes": {
                    "high": {"text": "You say something different and better. The time echo recalibrates around your new choice and both versions of you end up with rewards.", "cp": 165, "tokens": 465, "tone": "good"},
                    "mid":  {"text": "The echo is confused but not hostile. It replays your new words and fades. You move on.", "cp": 88, "tokens": 180, "tone": "neutral"},
                    "low":  {"text": "The contradiction creates a small temporal knot. Nothing explodes but something feels unresolved.", "cp": 12, "tokens": 25, "tone": "bad"},
                }
            },
            {
                "label": "🔇 Say nothing",
                "outcomes": {
                    "high": {"text": "Your silence breaks the echo. Without a source, it dissolves into pure energy that you absorb.", "cp": 150, "tokens": 414, "tone": "good"},
                    "mid":  {"text": "Silence. The echo fades. You feel a strange peace.", "cp": 82, "tokens": 156, "tone": "neutral"},
                    "low":  {"text": "The echo of your voice continues to speak without you. It sounds more confident than you feel.", "cp": 18, "tokens": 25, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Null Space Vendor",
        "image": "zone4_e6",
        "scene": (
            "A vendor exists here. They shouldn't — there's no road in or out. "
            "They sell things that don't quite make sense. "
            "'Everything here is real,' they insist. 'Mostly.'"
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The vendor flinches slightly at your charge level. They're used to less impressive clients.",
            "INS": "Your insulation makes you harder to read. The vendor can't tell if you're buying or just observing.",
            "SPK": "You can tell which items are real and which are mostly real. That's the key distinction.",
        },
        "question": {
            "prompt": "The vendor says everything is 'real, mostly.' In the Null Space, what does 'mostly real' mean?",
            "answers": ["Real in this zone but may not persist outside it", "Real but with a 50% chance of being useful", "Real for you but not for others"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "💰 Buy the glowing one",
                "outcomes": {
                    "high": {"text": "The glowing item is real — fully, completely, outside-the-Null-Space real. Worth every token.", "cp": 140, "tokens": 385, "tone": "good"},
                    "mid":  {"text": "It's real in here. It fades a little outside but retains most of its value.", "cp": 88, "tokens": 180, "tone": "neutral"},
                    "low":  {"text": "'Mostly real' turned out to mean 'real for about forty-five seconds.' You watched it dissolve.", "cp": 15, "tokens": 25, "tone": "bad"},
                }
            },
            {
                "label": "🔎 Inspect everything first",
                "outcomes": {
                    "high": {"text": "Your careful inspection identifies two genuinely real items. You take both. The vendor seems impressed and throws in a third.", "cp": 150, "tokens": 415, "tone": "good"},
                    "mid":  {"text": "You identify one real item and one that's probably fine. You take both and feel okay about it.", "cp": 92, "tokens": 195, "tone": "neutral"},
                    "low":  {"text": "You inspect too long. The vendor's stock shifts and what was real a moment ago is now less so.", "cp": 25, "tokens": 30, "tone": "neutral"},
                }
            },
            {
                "label": "🚶 Don't buy anything",
                "outcomes": {
                    "high": {"text": "You walk away. The vendor calls after you and drops a 'free sample' — which turns out to be the best thing in their stock.", "cp": 125, "tokens": 330, "tone": "good"},
                    "mid":  {"text": "You walk away. Nothing gained, nothing lost. The vendor shrugs.", "cp": 55, "tokens": 85, "tone": "neutral"},
                    "low":  {"text": "You walk away. An opportunity unmeasured is still an opportunity missed.", "cp": 20, "tokens": 25, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Echo Rival",
        "image": "zone4_e7",
        "rival": True,
        "scene": (
            "In the Null Space, you encounter what appears to be another Zappy — "
            "but they're slightly out of phase, like a signal that hasn't fully arrived yet. "
            "They're heading for the same phase-locked cache."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "Your charge makes you more real than them in this zone. You have the edge.",
            "INS": "Their phase-shift doesn't bother you — you've been in stranger fields.",
            "SPK": "You can predict their movements — they're running on a slightly delayed timeline.",
        },
        "question": {
            "prompt": "A phase-shifted rival is slightly behind in time. How does this affect a race to a cache?",
            "answers": ["They'll arrive slightly after you — use that window", "They see your past moves — unpredictability wins", "Time delay means they're actually closer than they appear"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🏃 Move immediately",
                "outcomes": {
                    "high": {"text": "You exploit the phase delay and reach the cache before their timeline catches up. Clean.", "cp": 152, "tokens": 430, "tone": "good"},
                    "mid":  {"text": "You get there first but barely. They arrive a second later and both agree to split.", "cp": 98, "tokens": 210, "tone": "neutral"},
                    "low":  {"text": "The phase delay was shorter than you thought. They're already there.", "cp": 20, "tokens": 25, "tone": "bad"},
                }
            },
            {
                "label": "🤔 Wait and observe",
                "outcomes": {
                    "high": {"text": "You wait and the phase-shift reveals their exact route. You take a different, better one and beat them cleanly.", "cp": 145, "tokens": 408, "tone": "good"},
                    "mid":  {"text": "You observe long enough to avoid one trap they fall into. You arrive second but the cache is still there.", "cp": 85, "tokens": 175, "tone": "neutral"},
                    "low":  {"text": "You wait too long. They resolve into full phase and beat you to it.", "cp": 18, "tokens": 25, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Logic Door",
        "image": "zone4_e8",
        "scene": (
            "A door with no handle, no keyhole, and no hinges stands in the path. "
            "Engraved on it: *I open for those who understand why I am here.* "
            "The door hums quietly."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The door responds to charge input — you can feel it pulse when you approach.",
            "INS": "Your insulation blocks its scan slightly. You might need to lower your guard.",
            "SPK": "You understand exactly why it's here. That's the whole point.",
        },
        "question": {
            "prompt": "The door says it opens for those who understand why it is here. Why is a door with no handle, keyhole, or hinges here?",
            "answers": ["To test whether you think before acting", "To block the path for anyone not strong enough to break it", "To reward patience — it opens on its own eventually"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "💭 Think about it",
                "outcomes": {
                    "high": {"text": "You stand before it and understand — it's here to see if you'll think before acting. You nod. The door opens. Behind it: a significant reward.", "cp": 145, "tokens": 402, "tone": "good"},
                    "mid":  {"text": "You think. You get partway there. The door opens slightly, enough to squeeze through.", "cp": 88, "tokens": 182, "tone": "neutral"},
                    "low":  {"text": "You think about it for a long time. The door doesn't open. Eventually you go around it.", "cp": 22, "tokens": 30, "tone": "neutral"},
                }
            },
            {
                "label": "⚡ Force it open",
                "trap": True,
                "outcomes": {
                    "high": {"text": "You blast the door. It absorbs your charge and uses it to lock more firmly. The door has learned your move.", "cp": 10, "tokens": 25, "tone": "bad"},
                    "mid":  {"text": "The force triggers an alarm. Nothing catastrophic, but something you'll remember.", "cp": 5, "tokens": 25, "tone": "bad"},
                    "low":  {"text": "The door forces itself back. Hard. You land on your back looking up at the Null Space sky.", "cp": 3, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "✋ Touch it gently",
                "outcomes": {
                    "high": {"text": "Your touch conveys understanding without words. The door opens immediately and silently. A perfect interaction.", "cp": 142, "tokens": 395, "tone": "good"},
                    "mid":  {"text": "The door warms slightly at your touch. It opens partway. You take what's accessible.", "cp": 85, "tokens": 175, "tone": "neutral"},
                    "low":  {"text": "The door does nothing. Your touch meant nothing in this context. Try something else.", "cp": 20, "tokens": 25, "tone": "neutral"},
                }
            },
        ]
    },
]


# ═══════════════════════════════════════════════════════
# ZONE 5 — APEX SUMMIT
# Tone: epic, earned, high stakes, everything matters
# ═══════════════════════════════════════════════════════

ZONE5_EVENTS = [
    # ── ORIGINAL 6 ────────────────────────────────────────────────
    {
        "title": "The Storm Crown",
        "image": "zone5_e1",
        "scene": (
            "The summit is ringed by a permanent storm that only the worthy can pass. "
            "Lightning strikes every few seconds — you've counted. Sixteen seconds between the big ones. "
            "You are either very brave or very stubborn."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "The storm reads your charge and pauses. Like it's deciding whether you deserve this.",
            "INS": "Your insulation is the only reason this is survivable. You know that.",
            "SPK": "Sixteen seconds. You've already timed it twice to confirm. It's consistent.",
        },
        "question": {
            "prompt": "You've counted sixteen seconds between big strikes. A small strike just fired. How long until the next big one?",
            "answers": ["Sixteen seconds — the pattern resets after every strike", "Less — small strikes don't reset the clock", "More — the storm needs to recharge after any discharge"],
            "correct": 1,
        },
        "choices": [
            {
                "label": "⚡ Charge through the storm",
                "outcomes": {
                    "high": {"text": "Your VLT matches the storm's frequency. The lightning recognizes you as kin and parts. You walk through on a path of calm air.", "cp": 200, "tokens": 620, "tone": "good"},
                    "mid":  {"text": "You take several direct hits. Each one hurts but none stop you. You emerge on the far side smoldering and victorious.", "cp": 135, "tokens": 370, "tone": "neutral"},
                    "low":  {"text": "The storm doesn't care about you. You take the full force and barely make it through.", "cp": 45, "tokens": 65, "tone": "bad"},
                }
            },
            {
                "label": "⏱️ Wait for the gap",
                "outcomes": {
                    "high": {"text": "You time it perfectly and sprint through in the sixteen-second window. Not a bolt lands near you.", "cp": 195, "tokens": 608, "tone": "good"},
                    "mid":  {"text": "You catch most of the gap but clip the tail end. Minor hit. You make it.", "cp": 130, "tokens": 355, "tone": "neutral"},
                    "low":  {"text": "The gap was shorter than the pattern suggested. You're mid-run when the next strike comes.", "cp": 40, "tokens": 55, "tone": "bad"},
                }
            },
            {
                "label": "🧲 Draw the lightning to you",
                "outcomes": {
                    "high": {"text": "You become a lightning rod — pulling every strike and absorbing it completely. The storm depletes itself against you. A legendary maneuver.", "cp": 220, "tokens": 680, "tone": "good"},
                    "mid":  {"text": "You draw most strikes and absorb them. Some you can't. You arrive charged beyond capacity.", "cp": 145, "tokens": 395, "tone": "good"},
                    "low":  {"text": "You draw the lightning but can't absorb it. The storm teaches you the difference.", "cp": 30, "tokens": 50, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Apex Gatekeeper",
        "image": "zone5_e2",
        "scene": (
            "A massive, ancient Zappy stands at the final gate. "
            "They have seen everyone who has ever made it this far. "
            "'Only one question,' they say. 'Why does your Zappy deserve to stand here?'"
        ),
        "stat": "INS",
        "trait_text": {
            "VLT": "The gatekeeper notes your charge output and raises one brow.",
            "INS": "Your insulation has decades of field wear on it. The gatekeeper sees this and nods.",
            "SPK": "The gatekeeper's eyes read something in your energy — like they've already made a judgment.",
        },
        "question": {
            "prompt": "The gatekeeper asks 'why does your Zappy deserve to stand here?' The gate behind them only opens for one kind of answer. Which?",
            "answers": ["An honest one", "A confident one", "A humble one"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "💪 'Because they made it'",
                "outcomes": {
                    "high": {"text": "The gatekeeper smiles. 'Correct.' The gate opens fully and a path of light extends through it.", "cp": 250, "tokens": 785, "tone": "good"},
                    "mid":  {"text": "'Acceptable,' they say. The gate opens. You walk through feeling exactly as worthy as you are.", "cp": 165, "tokens": 460, "tone": "good"},
                    "low":  {"text": "They consider it. 'Debatable,' they say. The gate opens a crack. You squeeze through.", "cp": 55, "tokens": 80, "tone": "neutral"},
                }
            },
            {
                "label": "📖 Tell your story",
                "outcomes": {
                    "high": {"text": "You tell them everything — every zone, every choice, every mistake and recovery. When you finish, the gatekeeper steps aside fully.", "cp": 260, "tokens": 810, "tone": "good"},
                    "mid":  {"text": "You give a good account. The gatekeeper is satisfied. Gate opens.", "cp": 170, "tokens": 475, "tone": "good"},
                    "low":  {"text": "Your story is thin. The gatekeeper opens the gate anyway. Everyone gets one chance.", "cp": 58, "tokens": 82, "tone": "neutral"},
                }
            },
            {
                "label": "🤫 Stay silent",
                "outcomes": {
                    "high": {"text": "Silence. The gatekeeper holds your gaze, then nods. 'Sometimes that's the right answer.' Gate opens wide.", "cp": 255, "tokens": 795, "tone": "good"},
                    "mid":  {"text": "The silence is read as confidence. The gatekeeper nods and stands aside.", "cp": 162, "tokens": 452, "tone": "good"},
                    "low":  {"text": "The silence is read as uncertainty. 'Work on it,' they say, opening the gate.", "cp": 50, "tokens": 72, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Infinite Generator",
        "image": "zone5_e3",
        "scene": (
            "At the peak: a generator the size of a building, spinning silently. "
            "It has no off switch. No purpose listed. "
            "But you notice something — every expedition zone below feels slightly warmer when you stand near it."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "The generator spins slightly faster when you approach. It recognizes your charge signature.",
            "INS": "Without your insulation, being this close would be dangerous. With it, it's just spectacular.",
            "SPK": "The warmth below isn't random. Every zone. That tells you something about what this thing does.",
        },
        "question": {
            "prompt": "The generator has no purpose listed, but every zone below feels warmer near it. What does that most likely mean?",
            "answers": ["It powers the entire expedition zone network", "It's leaking heat — a sign it's about to fail", "It runs on ambient warmth from the zones below it"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⚡ Draw power from it",
                "outcomes": {
                    "high": {"text": "You interface carefully and draw a measured amount. It gives generously — as if waiting for someone worth giving to.", "cp": 245, "tokens": 762, "tone": "good"},
                    "mid":  {"text": "You draw what you can handle. The generator doesn't notice. You leave significantly charged.", "cp": 162, "tokens": 450, "tone": "good"},
                    "low":  {"text": "You draw too much and the feedback bounces you. The generator spins on, unaffected.", "cp": 50, "tokens": 70, "tone": "bad"},
                }
            },
            {
                "label": "🔭 Study it",
                "outcomes": {
                    "high": {"text": "Your study reveals a maintenance port with stored reserves — meant for authorized Zappies, no access restrictions remaining. You take everything.", "cp": 250, "tokens": 780, "tone": "good"},
                    "mid":  {"text": "You learn something about how the expedition zones are powered. The knowledge will help in future runs.", "cp": 165, "tokens": 458, "tone": "good"},
                    "low":  {"text": "The generator is beyond full comprehension. You leave knowing less than you arrived with.", "cp": 40, "tokens": 55, "tone": "neutral"},
                }
            },
            {
                "label": "🎁 Leave an offering",
                "outcomes": {
                    "high": {"text": "You leave something at the base. The generator hums louder — acknowledgment — and a cache you didn't see when you arrived appears nearby.", "cp": 255, "tokens": 795, "tone": "good"},
                    "mid":  {"text": "The generator accepts your offering quietly. Something shifts.", "cp": 158, "tokens": 438, "tone": "good"},
                    "low":  {"text": "The generator does not respond to offerings. It just spins.", "cp": 45, "tokens": 60, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Last Broadcast",
        "image": "zone5_e4",
        "scene": (
            "A single antenna at the summit is transmitting outward — not to anyone nearby, but far. "
            "The control panel is still warm to the touch. "
            "The transmission log shows the last manual entry was three hours ago."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "Your charge gives any signal you add a stronger output than most Zappies could produce.",
            "INS": "This isn't about insulation. It's about what you say.",
            "SPK": "Three hours ago. Someone was here. The question is whether they left voluntarily.",
        },
        "question": {
            "prompt": "The panel is warm and the log shows a manual entry three hours ago. What does 'still warm' combined with a recent entry tell you?",
            "answers": ["Someone left recently and may still be nearby", "The panel runs hot constantly — age does that", "Three hours is too long for residual warmth — something else is heating it"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "📡 Add your signal",
                "outcomes": {
                    "high": {"text": "Your signal is clear and strong. Something receives it. A reply comes back as a resource drop.", "cp": 230, "tokens": 752, "tone": "good"},
                    "mid":  {"text": "Your signal goes out. Whether it reaches anyone, you don't know. The act feels important.", "cp": 145, "tokens": 413, "tone": "good"},
                    "low":  {"text": "Your signal is weak and gets lost. You still tried. That goes on record somewhere.", "cp": 45, "tokens": 52, "tone": "neutral"},
                }
            },
            {
                "label": "🔍 Decode what's there",
                "outcomes": {
                    "high": {"text": "The broadcast has been running for years — a map of the entire expedition path with hidden caches marked. You copy it all.", "cp": 245, "tokens": 787, "tone": "good"},
                    "mid":  {"text": "You decode fragments. Enough to find one cache that wasn't on any map you had.", "cp": 155, "tokens": 437, "tone": "good"},
                    "low":  {"text": "The encoding is beyond you. The broadcast continues, mysterious and ancient.", "cp": 35, "tokens": 50, "tone": "neutral"},
                }
            },
            {
                "label": "📴 Shut it down",
                "outcomes": {
                    "high": {"text": "You shut it down. In the silence, you realize the broadcast was powering a containment field. Whatever was inside is now free — and grateful.", "cp": 260, "tokens": 840, "tone": "good"},
                    "mid":  {"text": "You shut it down. The quiet feels significant. Something in the area shifts.", "cp": 148, "tokens": 420, "tone": "neutral"},
                    "low":  {"text": "The shutdown triggers a failsafe alarm. You silence it and leave quickly.", "cp": 30, "tokens": 50, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Summit Rival",
        "image": "zone5_e5",
        "rival": True,
        "scene": (
            "At the summit — of all places — another Zappy. "
            "You've both made it this far. A cache sits between you. "
            "They're eyeing it. You're eyeing it. Neither of you moves."
        ),
        "stat": "INS",
        "trait_text": {
            "VLT": "Your charge is high enough that they're measuring you. You're both formidable up here.",
            "INS": "You've taken everything the expedition threw at you and you're still standing clean. They know it.",
            "SPK": "Neither of you moves. That tells you something — they're not sure they can win a race either.",
        },
        "question": {
            "prompt": "You're both standing still, watching the cache. Neither has moved. What does that tell you about the other Zappy?",
            "answers": ["They're not confident they'd win a sprint — use that", "They're waiting for you to make the first mistake", "They're about to propose a split — wait them out"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🏆 Claim it first",
                "outcomes": {
                    "high": {"text": "You sprint the moment you read their hesitation. You're right — they weren't ready. Cache is yours. They nod.", "cp": 275, "tokens": 885, "tone": "good"},
                    "mid":  {"text": "Close race. You edge them out and take the larger share.", "cp": 182, "tokens": 507, "tone": "neutral"},
                    "low":  {"text": "They were faster than they looked. They take the cache cleanly.", "cp": 60, "tokens": 90, "tone": "bad"},
                }
            },
            {
                "label": "🤝 Propose a split",
                "outcomes": {
                    "high": {"text": "They agree immediately — turns out they were hoping for this. The cache is larger than it looked. Both of you leave with more than expected.", "cp": 280, "tokens": 910, "tone": "good"},
                    "mid":  {"text": "They agree. Fair division. You both leave with something worth having.", "cp": 178, "tokens": 495, "tone": "good"},
                    "low":  {"text": "They don't want to split. Long silence. You eventually take a small portion.", "cp": 55, "tokens": 75, "tone": "neutral"},
                }
            },
            {
                "label": "🎲 Let the summit decide",
                "outcomes": {
                    "high": {"text": "You step back. They look surprised, then do the same. The cache stays unclaimed — until the summit deposits a second one. Both of you take one.", "cp": 272, "tokens": 875, "tone": "good"},
                    "mid":  {"text": "The summit is neutral. You get half.", "cp": 175, "tokens": 488, "tone": "neutral"},
                    "low":  {"text": "The summit doesn't favor you today. The cache goes to the rival.", "cp": 58, "tokens": 80, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Apex Itself",
        "image": "zone5_e6",
        "scene": (
            "You are at the very top. "
            "The whole world spreads out below — the Fields, the Bay, the Circuit, the Null Space, all of it. "
            "You can see every zone from here. Including one section of Voltage Bay that looks different from above."
        ),
        "stat": None,
        "trait_text": {
            "VLT": "Your charge crackles quietly in the thin air. You feel like a live wire against the sky.",
            "INS": "Your insulation has kept you standing through everything. Up here, it's just you.",
            "SPK": "That section of Voltage Bay — it's darker than the rest. Something's submerged there.",
        },
        "question": {
            "prompt": "From the apex you can see a darker section of Voltage Bay that looks different from above. What does a darker patch in a coastal zone most likely indicate?",
            "answers": ["Something submerged — deeper water or a sunken structure", "A dead zone — no electrical activity", "A storm shadow — the bay gets less sun there"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🌟 Take it all in",
                "outcomes": {
                    "high": {"text": "You stand here for a long moment. The view fills you. You come down changed — more CP, and a feeling that this was worth every step.", "cp": 280, "tokens": 910, "tone": "good"},
                    "mid":  {"text": "You take it in as best you can. You carry some of it back down with you.", "cp": 175, "tokens": 507, "tone": "good"},
                    "low":  {"text": "It's overwhelming. You sit down. The view waits for you.", "cp": 60, "tokens": 87, "tone": "neutral"},
                }
            },
            {
                "label": "🏴 Plant your flag",
                "outcomes": {
                    "high": {"text": "You plant your flag and the expedition record updates. You are now part of the summit's history. A bonus cache activates.", "cp": 290, "tokens": 945, "tone": "good"},
                    "mid":  {"text": "You plant it. It stands. Future expeditions will see it.", "cp": 180, "tokens": 525, "tone": "good"},
                    "low":  {"text": "The wind is strong up here. Your flag blows sideways. Still counts.", "cp": 65, "tokens": 98, "tone": "neutral"},
                }
            },
            {
                "label": "🔄 Begin the descent",
                "outcomes": {
                    "high": {"text": "You turn around already planning the next run. The summit drops a reward at your feet as you leave.", "cp": 275, "tokens": 892, "tone": "good"},
                    "mid":  {"text": "You head down with purpose. The experience crystallizes into something useful.", "cp": 168, "tokens": 483, "tone": "good"},
                    "low":  {"text": "You head down. The summit is behind you. There is always the next time.", "cp": 55, "tokens": 77, "tone": "neutral"},
                }
            },
        ]
    },
    # ── EXTENDED POOL ─────────────────────────────────────────────
    {
        "title": "The Fractured Ledge",
        "image": "zone5_e7",
        "scene": (
            "The final approach splits across a fractured ledge. "
            "Chunks of rock float at different heights on ambient charge. "
            "The larger, darker ones barely move. The smaller pale ones drift constantly."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The floating rocks pulse at your frequency.",
            "INS": "If you fall and grab a rock, your insulation handles the charge. Small comfort.",
            "SPK": "You can map every arc and landing before you take a single step.",
        },
        "question": {
            "prompt": "The larger darker rocks barely move. The smaller pale ones drift constantly. Which do you land on?",
            "answers": ["Larger darker ones — more stable", "Smaller pale ones — lighter means easier to correct a bad landing", "Whichever is closest — speed matters more than stability"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🦘 Leap across",
                "outcomes": {
                    "high": {"text": "Each landing is exact. You cross like you designed it.", "cp": 255, "tokens": 800, "tone": "good"},
                    "mid":  {"text": "Two wobbles, one near miss. You make it across breathing hard.", "cp": 165, "tokens": 462, "tone": "neutral"},
                    "low":  {"text": "Third rock was unstable. You survive the fall. Dignity: gone. Run: continuing.", "cp": 48, "tokens": 65, "tone": "bad"},
                }
            },
            {
                "label": "⚡ Charge-lock the rocks",
                "outcomes": {
                    "high": {"text": "You emit a pulse that locks the rocks into a stable bridge. You walk across casually.", "cp": 268, "tokens": 835, "tone": "good"},
                    "mid":  {"text": "Partial lock. The rocks stabilize enough for a quick crossing.", "cp": 172, "tokens": 482, "tone": "neutral"},
                    "low":  {"text": "Your pulse destabilizes them further. You're now leaping across moving targets.", "cp": 52, "tokens": 70, "tone": "bad"},
                }
            },
            {
                "label": "🧗 Climb the cliff face instead",
                "outcomes": {
                    "high": {"text": "The cliff has better handholds than expected. You find a route no one has used recently and discover an untouched cache in a crevice.", "cp": 260, "tokens": 812, "tone": "good"},
                    "mid":  {"text": "Slow but safe. You reach the top intact.", "cp": 158, "tokens": 440, "tone": "neutral"},
                    "low":  {"text": "The cliff is harder than it looked. You make it but need a moment at the top.", "cp": 50, "tokens": 68, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Memory Archive",
        "image": "zone5_e8",
        "scene": (
            "Embedded in the summit rock: a crystalline archive storing memories of every Zappy "
            "who has reached the top. "
            "An inscription reads: *Contribute or consume. Never both.* "
            "You contributed a memory already. It's been accepted."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "Your charge interacts with the crystal lattice — it wants to pull you in deeper.",
            "INS": "Your insulation keeps the archive's output at a readable level.",
            "SPK": "You contributed. The inscription says never both. The answer is obvious.",
        },
        "question": {
            "prompt": "You've already contributed a memory. The inscription says 'contribute or consume, never both.' What's the correct next action?",
            "answers": ["Consume — you've contributed, now take", "Contribute again to earn more access", "Walk away — you've done your part"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "📥 Add your memory",
                "outcomes": {
                    "high": {"text": "You contribute something real. The archive gives you back the most relevant memory of a Zappy who faced your exact situation.", "cp": 248, "tokens": 775, "tone": "good"},
                    "mid":  {"text": "You contribute. The archive acknowledges. You feel lighter and more certain of your next move.", "cp": 160, "tokens": 448, "tone": "good"},
                    "low":  {"text": "The archive already has this memory. It thanks you with a modest reward.", "cp": 55, "tokens": 78, "tone": "neutral"},
                }
            },
            {
                "label": "📤 Take from it",
                "outcomes": {
                    "high": {"text": "You draw carefully and find exactly what you needed: the routing map of an Apex runner who found every cache in one run.", "cp": 252, "tokens": 790, "tone": "good"},
                    "mid":  {"text": "You find useful knowledge — a tip, a warning, a pattern.", "cp": 162, "tokens": 452, "tone": "good"},
                    "low":  {"text": "You take memories that don't apply to you. You leave more uncertain than you arrived.", "cp": 42, "tokens": 58, "tone": "neutral"},
                }
            },
            {
                "label": "🤲 Both — add and take",
                "trap": True,
                "outcomes": {
                    "high": {"text": "The archive warned you. It locks you out the moment you try both.", "cp": 12, "tokens": 25, "tone": "bad"},
                    "mid":  {"text": "The inscription was specific. You ignored it. The archive gives you nothing.", "cp": 8, "tokens": 0, "tone": "bad"},
                    "low":  {"text": "The archive does not appreciate greed at the summit.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Summit Patrol",
        "image": "zone5_e9",
        "scene": (
            "Two large Apex Guardians run a patrol route across the summit path. "
            "They move in a figure-eight. "
            "The outer edges of the pattern are where they spend the least time — "
            "they accelerate through the curves and slow at the center crossing."
        ),
        "stat": "INS",
        "trait_text": {
            "VLT": "The Guardians' scanners flag elevated charge signatures. You're in their range.",
            "INS": "Their electrostatic scanners struggle with well-insulated Zappies. You're harder to detect.",
            "SPK": "They slow at the center crossing. That means the outer edges are moving fast — brief windows.",
        },
        "question": {
            "prompt": "The Guardians slow at the center crossing and accelerate through the outer curves. Where's the safest place to cross?",
            "answers": ["The outer edges — they move through fast, creating brief gaps", "The center crossing — they're slowest there, easier to time", "Directly behind them — match their speed and follow"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🕵️ Slip through unseen",
                "outcomes": {
                    "high": {"text": "You use the outer edge gap perfectly. Not a single scan pings you.", "cp": 258, "tokens": 808, "tone": "good"},
                    "mid":  {"text": "One scanner clips you on exit. Not a full hit — they recalibrate and you're gone.", "cp": 168, "tokens": 470, "tone": "neutral"},
                    "low":  {"text": "Both Guardians converge. You take their full attention and still make it past.", "cp": 50, "tokens": 68, "tone": "bad"},
                }
            },
            {
                "label": "⚡ Overload their systems",
                "outcomes": {
                    "high": {"text": "A precise pulse fries both scanners simultaneously. They patrol blind while you cross freely.", "cp": 265, "tokens": 828, "tone": "good"},
                    "mid":  {"text": "You fry one Guardian's scanner. The other still sees you. Tense but you make it.", "cp": 170, "tokens": 475, "tone": "neutral"},
                    "low":  {"text": "Your pulse alerts both Guardians instead of disabling them.", "cp": 45, "tokens": 62, "tone": "bad"},
                }
            },
            {
                "label": "🚶 Just walk through confidently",
                "trap": True,
                "outcomes": {
                    "high": {"text": "Confidence doesn't fool automated constructs. Their scanners trigger regardless.", "cp": 15, "tokens": 25, "tone": "bad"},
                    "mid":  {"text": "The Guardians flag you immediately. You run. They're slower than expected.", "cp": 22, "tokens": 15, "tone": "bad"},
                    "low":  {"text": "Both Guardians are on you in seconds.", "cp": 8, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Whiteout",
        "image": "zone5_e10",
        "scene": (
            "A sudden electrical fog drops visibility to zero. "
            "You can't see — but you can feel. "
            "Objects with stored charge show up as faint warmth in your perception. "
            "The cache is out there somewhere."
        ),
        "stat": "INS",
        "trait_text": {
            "VLT": "Every charged object within twenty feet registers as heat against your skin.",
            "INS": "The fog has charge density that would saturate an uninsulated Zappy fast. You're fine.",
            "SPK": "You've navigated by feeling before. Be quiet enough and the cache hums.",
        },
        "question": {
            "prompt": "You can feel charged objects as faint warmth through the fog. The cache has stored charge. What's the most direct way to find it?",
            "answers": ["Stand still and rotate slowly — map the warmth sources before moving", "Move fast in a straight line and adjust when you feel something", "Shout and listen for echo — sound works when sight doesn't"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⚡ Navigate by charge",
                "outcomes": {
                    "high": {"text": "Your charge sensitivity maps the summit through the fog. You walk directly to the cache without a single wrong step.", "cp": 262, "tokens": 820, "tone": "good"},
                    "mid":  {"text": "Your charge sense is good but not perfect in a whiteout. You find the cache on the second attempt.", "cp": 168, "tokens": 470, "tone": "neutral"},
                    "low":  {"text": "The fog interferes with your reading. You find the cache eventually, after finding several things that weren't it.", "cp": 52, "tokens": 72, "tone": "neutral"},
                }
            },
            {
                "label": "🧭 Move in a spiral",
                "outcomes": {
                    "high": {"text": "Methodical spiral. You cover the area systematically and find not just the main cache but two smaller ones the fog was hiding.", "cp": 270, "tokens": 845, "tone": "good"},
                    "mid":  {"text": "The spiral works. You find the cache on your second loop.", "cp": 165, "tokens": 462, "tone": "neutral"},
                    "low":  {"text": "Your spiral goes wide. You exit the fog without finding anything and have to re-enter.", "cp": 45, "tokens": 60, "tone": "neutral"},
                }
            },
            {
                "label": "🛑 Wait for it to clear",
                "outcomes": {
                    "high": {"text": "You wait. The whiteout clears after ninety seconds and you walk directly to the cache in clear air.", "cp": 248, "tokens": 772, "tone": "good"},
                    "mid":  {"text": "You wait. The fog thins enough to navigate. You find the cache safely.", "cp": 155, "tokens": 432, "tone": "neutral"},
                    "low":  {"text": "The whiteout doesn't clear. You're still waiting when it starts getting worse.", "cp": 40, "tokens": 55, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Altitude Sickness",
        "image": "zone5_e11",
        "scene": (
            "The Apex atmosphere runs thin on charge. "
            "Your systems are flickering — output dropping, perception narrowing. "
            "You have two options: stop and stabilize, or push through and risk your systems locking up entirely."
        ),
        "stat": "INS",
        "trait_text": {
            "VLT": "High VLT burns through thin charge density faster than lower-output Zappies. You're feeling it more.",
            "INS": "Your insulation has been managing this since Zone 4. You're showing the strain but holding.",
            "SPK": "You know exactly what's happening and how long you have before it becomes unrecoverable.",
        },
        "question": {
            "prompt": "Your systems are flickering and output is dropping. Pushing through risks full lockup. Stopping means losing time. Which risk is worse?",
            "answers": ["Full lockup — you can't recover mid-run, lost time you can recover from", "Lost time — at the summit, time equals missed caches", "They're equal risks — flip a coin"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🛑 Stop and stabilize",
                "outcomes": {
                    "high": {"text": "You stop, reduce output, and let your systems rebalance. Two minutes later you're fully functional and faster than if you'd pushed through.", "cp": 245, "tokens": 762, "tone": "good"},
                    "mid":  {"text": "Stabilization takes longer than expected but works. You continue with steady systems.", "cp": 158, "tokens": 440, "tone": "neutral"},
                    "low":  {"text": "You stabilize partially. Systems still flickering but manageable.", "cp": 52, "tokens": 72, "tone": "neutral"},
                }
            },
            {
                "label": "💊 Use an emergency charge",
                "outcomes": {
                    "high": {"text": "Your emergency charge tops you up to full. The altitude sickness clears. You feel artificially excellent.", "cp": 252, "tokens": 788, "tone": "good"},
                    "mid":  {"text": "The emergency charge helps. Not at full but functional.", "cp": 162, "tokens": 452, "tone": "neutral"},
                    "low":  {"text": "You don't have an emergency charge. You knew this. You check anyway.", "cp": 45, "tokens": 62, "tone": "neutral"},
                }
            },
            {
                "label": "🏃 Push through it",
                "trap": True,
                "outcomes": {
                    "high": {"text": "Pushing through altitude sickness locks your systems. What should have been a good beat becomes a bad one.", "cp": 30, "tokens": 20, "tone": "bad"},
                    "mid":  {"text": "Your systems partially fail mid-push. You lose significant output.", "cp": 20, "tokens": 12, "tone": "bad"},
                    "low":  {"text": "Full lockup. You recover but you've lost ground you won't get back.", "cp": 8, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Old Campsite",
        "image": "zone5_e12",
        "scene": (
            "A long-abandoned campsite on a sheltered ledge. "
            "A meal was left half-eaten. Equipment sits open mid-use. "
            "The navigation unit is still on — still displaying a route."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The abandoned charging array still has power. Not much, but yours if you want it.",
            "INS": "The equipment condition tells you exactly how long ago someone was here.",
            "SPK": "Half-eaten meal, open equipment, nav unit still running. They didn't choose to leave.",
        },
        "question": {
            "prompt": "The nav unit is still on and displaying a route. The meal is half-eaten. What's the most useful thing this camp offers?",
            "answers": ["The nav unit's active route — someone plotted a course through this summit recently", "The food supplies — calories matter at altitude", "The charging array — power is always useful"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "📡 Check the nav unit",
                "outcomes": {
                    "high": {"text": "The nav unit has a complete map of the summit's current cache locations — updated more recently than anything you're carrying.", "cp": 255, "tokens": 800, "tone": "good"},
                    "mid":  {"text": "Partial data — enough to narrow your search area significantly.", "cp": 165, "tokens": 462, "tone": "good"},
                    "low":  {"text": "The nav unit is password locked. You get nothing.", "cp": 48, "tokens": 65, "tone": "neutral"},
                }
            },
            {
                "label": "🎒 Take the supplies",
                "outcomes": {
                    "high": {"text": "Premium grade supplies — expedition-class charge packs, insulation patches, field kit worth more than most runs. All yours.", "cp": 250, "tokens": 782, "tone": "good"},
                    "mid":  {"text": "Good supplies. Charge packs and useful tools.", "cp": 158, "tokens": 440, "tone": "neutral"},
                    "low":  {"text": "Most supplies are expired or spent. You find one useful item.", "cp": 55, "tokens": 78, "tone": "neutral"},
                }
            },
            {
                "label": "📓 Read the journal",
                "outcomes": {
                    "high": {"text": "The last entry describes a cache location they found but couldn't reach. You can reach it.", "cp": 262, "tokens": 818, "tone": "good"},
                    "mid":  {"text": "Useful observations about the summit's patterns. You take notes.", "cp": 162, "tokens": 452, "tone": "good"},
                    "low":  {"text": "Mostly weather complaints and morale notes. Sympathetic but not tactical.", "cp": 42, "tokens": 55, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Apex Monk",
        "image": "zone5_e13",
        "scene": (
            "Seated on a flat rock, completely unbothered by the storm: "
            "a Zappy in worn robes. "
            "'You made it,' they say without turning. 'Most don't. Sit with me or keep moving — both are correct.' "
            "They don't look at you when they say it."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The monk's charge output dwarfs yours — and they're barely trying.",
            "INS": "The monk notices your insulation and gives a small approving nod.",
            "SPK": "They didn't turn around. They knew you were there before you arrived.",
        },
        "question": {
            "prompt": "The monk didn't turn around when you arrived. They said 'both are correct' before you asked anything. What does that tell you?",
            "answers": ["They already knew what you were going to ask — listen carefully", "They're indifferent — the answer genuinely doesn't matter", "They're testing whether you'll push back on a vague answer"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🧘 Sit with them",
                "outcomes": {
                    "high": {"text": "You sit. After a while the monk says one thing: 'Third rock on the left face. No one checks there.' You do. They're right.", "cp": 268, "tokens": 838, "tone": "good"},
                    "mid":  {"text": "You sit. The monk radiates calm. When you leave you feel steadier than you have all run.", "cp": 172, "tokens": 480, "tone": "good"},
                    "low":  {"text": "You sit. The monk sits. Time passes. It's peaceful but nothing tangible happens.", "cp": 55, "tokens": 78, "tone": "neutral"},
                }
            },
            {
                "label": "🏃 Keep moving",
                "outcomes": {
                    "high": {"text": "As you pass, the monk says one word: 'Left.' The left path has a cache you would have walked past.", "cp": 262, "tokens": 818, "tone": "good"},
                    "mid":  {"text": "You move on. The monk watches you go.", "cp": 162, "tokens": 452, "tone": "neutral"},
                    "low":  {"text": "You second-guess it the whole next section.", "cp": 48, "tokens": 65, "tone": "neutral"},
                }
            },
            {
                "label": "❓ Ask them a question",
                "outcomes": {
                    "high": {"text": "You ask about the hardest section. They tell you exactly what you need — information that would have taken three failed runs to learn.", "cp": 272, "tokens": 852, "tone": "good"},
                    "mid":  {"text": "They answer obliquely but it's answerable. You take something useful from it.", "cp": 168, "tokens": 470, "tone": "good"},
                    "low":  {"text": "They answer your question with a question. You don't have a good answer. You leave.", "cp": 52, "tokens": 72, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Rival at the Gate",
        "image": "zone5_e14",
        "rival": True,
        "scene": (
            "At the final gate, another Zappy is already working the lock. "
            "They're close — thirty seconds from opening it. "
            "The lock glows brighter the more charge is applied to it."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "The lock glows brighter with more charge. You have plenty.",
            "INS": "You could wait them out — your insulation means the gate's defense field doesn't bother you.",
            "SPK": "They're close. Thirty seconds. The lock glows brighter with charge input. You see where this is going.",
        },
        "question": {
            "prompt": "The lock glows brighter the more charge is applied. The rival is close to opening it. What's the fastest resolution?",
            "answers": ["Add your charge to theirs — more charge opens it faster", "Wait for them to finish and follow through", "Force the lock with a single high burst"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⚡ Force the lock",
                "outcomes": {
                    "high": {"text": "Your VLT blows it open before they finish. You're through. The gate stays open — they follow.", "cp": 270, "tokens": 848, "tone": "good"},
                    "mid":  {"text": "You force it open. Both of you go through together. Awkward but functional.", "cp": 175, "tokens": 488, "tone": "neutral"},
                    "low":  {"text": "Your force attempt resets the lock. Now neither of you can enter for another minute.", "cp": 45, "tokens": 60, "tone": "bad"},
                }
            },
            {
                "label": "🤝 Help them finish",
                "outcomes": {
                    "high": {"text": "You add your charge. The lock opens faster than either expected. Inside: enough for both. Summit math.", "cp": 278, "tokens": 872, "tone": "good"},
                    "mid":  {"text": "You help. They're surprised. You both enter and split fairly.", "cp": 182, "tokens": 508, "tone": "good"},
                    "low":  {"text": "You help and they take more than their share. You let it go.", "cp": 62, "tokens": 88, "tone": "neutral"},
                }
            },
            {
                "label": "🕐 Wait for them to finish",
                "outcomes": {
                    "high": {"text": "You wait. They open it, turn around, and say 'after you' — they'd been hoping someone would show up. They know where the best cache is.", "cp": 268, "tokens": 835, "tone": "good"},
                    "mid":  {"text": "You wait. They open it. You both enter. A civil resolution.", "cp": 170, "tokens": 475, "tone": "neutral"},
                    "low":  {"text": "You wait. They go through and the gate closes. You're back to picking it alone.", "cp": 42, "tokens": 58, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Lightning Harvester",
        "image": "zone5_e15",
        "scene": (
            "An abandoned lightning harvester sits fully charged — six months of summit lightning stored in its array. "
            "The release port is live. "
            "The intake valve on the side is labeled: *MAX DRAIN RATE: 10% per minute.*"
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "The harvester is tuned to your charge type. You could drain it directly without conversion loss.",
            "INS": "The output port is live. Your insulation lets you handle this safely.",
            "SPK": "The label says 10% per minute. Six months of storage. Do that math before touching anything.",
        },
        "question": {
            "prompt": "The label says MAX DRAIN RATE: 10% per minute. What happens if you drain it faster than that?",
            "answers": ["Surge feedback — stored energy rebounds beyond the drain rate limit", "Nothing — labels are conservative estimates", "The device locks — safety mechanism triggers"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⚡ Drain it all at once",
                "trap": True,
                "outcomes": {
                    "high": {"text": "The label said 10% per minute for a reason. Surge feedback hits you before you finish.", "cp": 65, "tokens": 90, "tone": "bad"},
                    "mid":  {"text": "You take the full surge. Your systems redline.", "cp": 40, "tokens": 52, "tone": "bad"},
                    "low":  {"text": "Six months of stored lightning all at once. Physics objects. Loudly.", "cp": 12, "tokens": 15, "tone": "bad"},
                }
            },
            {
                "label": "📉 Drain it at 10% per minute",
                "outcomes": {
                    "high": {"text": "You drain at exactly the rated speed. The full array transfers cleanly. An extraordinary find.", "cp": 285, "tokens": 925, "tone": "good"},
                    "mid":  {"text": "You drain a good portion at the safe rate before needing to move.", "cp": 178, "tokens": 498, "tone": "good"},
                    "low":  {"text": "Your control rate is too conservative even for the limit. You get less than you should have.", "cp": 68, "tokens": 95, "tone": "neutral"},
                }
            },
            {
                "label": "🔧 Reprogram it to follow you",
                "outcomes": {
                    "high": {"text": "You reprogram the harvester to feed your charge system passively. It powers you through the rest of the run.", "cp": 275, "tokens": 895, "tone": "good"},
                    "mid":  {"text": "Partial reprogram. It follows you for a while before losing sync.", "cp": 168, "tokens": 470, "tone": "neutral"},
                    "low":  {"text": "Your reprogramming wipes its stored charge. The array is empty and not speaking to you.", "cp": 45, "tokens": 62, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Summit Weather Station",
        "image": "zone5_e16",
        "scene": (
            "A functioning weather station at the summit edge. Real-time data: "
            "charge surge incoming from the northwest in 45 seconds. "
            "The station map shows the surge follows the ridge line — northwest to southeast."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The station tracks charge output across the whole expedition. Your reading shows up on the board.",
            "INS": "The surge data shows where insulation is an advantage. You already know this.",
            "SPK": "Northwest to southeast along the ridge. The station just told you exactly where not to be.",
        },
        "question": {
            "prompt": "Surge incoming from the northwest, following the ridge line northwest to southeast. Where do you stand?",
            "answers": ["Off the ridge entirely — the surge tracks the ridge, not open ground", "Southeast end — you'll see it coming", "Northwest — meet it head-on and absorb it"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "📊 Use the forecast",
                "outcomes": {
                    "high": {"text": "The forecast is precise. You position for every surge, absorbing what you can, avoiding what you can't. Optimal run.", "cp": 265, "tokens": 830, "tone": "good"},
                    "mid":  {"text": "You dodge the worst incoming weather. Still take one hit but avoided three.", "cp": 172, "tokens": 480, "tone": "good"},
                    "low":  {"text": "The data is harder to parse than it looks. You misread one chart and walk into a surge.", "cp": 52, "tokens": 72, "tone": "neutral"},
                }
            },
            {
                "label": "📡 Broadcast it to other runners",
                "outcomes": {
                    "high": {"text": "You broadcast the data. Three other Zappies hear it and send you a share of their finds as thanks.", "cp": 258, "tokens": 808, "tone": "good"},
                    "mid":  {"text": "You broadcast it. Someone acknowledges with a small thank-you cache.", "cp": 162, "tokens": 452, "tone": "good"},
                    "low":  {"text": "You broadcast it. No response. Useful to someone somewhere.", "cp": 52, "tokens": 72, "tone": "neutral"},
                }
            },
            {
                "label": "🔒 Keep it to yourself",
                "outcomes": {
                    "high": {"text": "You use every piece to optimize your own run. The summit doesn't judge you. Your total reflects it.", "cp": 262, "tokens": 820, "tone": "good"},
                    "mid":  {"text": "You use it well. Good run. Tangible edge on the next beat.", "cp": 165, "tokens": 462, "tone": "neutral"},
                    "low":  {"text": "You keep the data but don't use it efficiently. The advantage stays theoretical.", "cp": 50, "tokens": 68, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Frozen Clock",
        "image": "zone5_e17",
        "scene": (
            "A grandfather clock stands on the summit, fully exposed to the elements. "
            "Frozen at 11:47. "
            "The mechanism is still live — running on stored charge. "
            "Something stopped it. The charge didn't."
        ),
        "stat": None,
        "trait_text": {
            "VLT": "The internal mechanism is still live — running on stored charge, frozen by something else.",
            "INS": "Something is protecting this clock from the summit weather. It should be destroyed by now.",
            "SPK": "The mechanism runs. The clock doesn't move. Those two facts together mean one thing.",
        },
        "question": {
            "prompt": "The mechanism is still live and running on stored charge — but the clock is frozen. What stopped it?",
            "answers": ["Something external blocked the hands — the mechanism never failed", "The charge ran out and was recently restored but the time was lost", "It stopped itself — a failsafe triggered at 11:47"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🕰️ Wind the clock",
                "outcomes": {
                    "high": {"text": "You find what's blocking the hands and remove it. The clock ticks forward from 11:47 for the first time in recorded history. The summit shudders and releases something.", "cp": 280, "tokens": 910, "tone": "good"},
                    "mid":  {"text": "You wind it. It ticks once, then stops again. Something shifted but didn't fully release.", "cp": 175, "tokens": 490, "tone": "neutral"},
                    "low":  {"text": "You wind it. Nothing. The clock stops at 11:47 again.", "cp": 55, "tokens": 78, "tone": "neutral"},
                }
            },
            {
                "label": "🔍 Find what's blocking it",
                "outcomes": {
                    "high": {"text": "You locate the obstruction — a small compartment jammed against the gears. Inside: a map of the entire expedition network, miniaturized in the gearwork.", "cp": 268, "tokens": 838, "tone": "good"},
                    "mid":  {"text": "You find the obstruction and a hidden compartment in the base. Old supplies, still good.", "cp": 170, "tokens": 475, "tone": "good"},
                    "low":  {"text": "You can't find what's blocking it. The mechanism is intricate beyond your tools.", "cp": 50, "tokens": 68, "tone": "neutral"},
                }
            },
            {
                "label": "🚶 Leave it alone",
                "outcomes": {
                    "high": {"text": "You leave it frozen. The clock seems to understand — a path through a blocked section opens ahead of you.", "cp": 258, "tokens": 808, "tone": "good"},
                    "mid":  {"text": "You leave it. Some things stay frozen for a reason.", "cp": 160, "tokens": 448, "tone": "neutral"},
                    "low":  {"text": "You leave it. You wonder for a long time whether you should have.", "cp": 48, "tokens": 65, "tone": "neutral"},
                }
            },
        ]
    },
    # ── 8 NEW EVENTS ──────────────────────────────────────────────
    {
        "title": "The Charge Siphon",
        "image": "zone5_e18",
        "scene": (
            "A siphon device is embedded in the summit rock, slowly drawing charge from the surrounding area. "
            "Your own charge level is dropping — slowly but measurably. "
            "The siphon feeds into a sealed container nearby. The container is nearly full."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "You can feel your charge dropping about 1% per minute. Whoever built this was patient.",
            "INS": "Your insulation is slowing the drain. Without it you'd already be at half capacity.",
            "SPK": "Nearly full container, slow drain rate. This has been running a long time. Someone planned to come back.",
        },
        "question": {
            "prompt": "Your charge is draining slowly into the siphon. The container is nearly full. What's the fastest way to stop the drain?",
            "answers": ["Disconnect the siphon from the rock — cut the source, not the container", "Seal the container — when it's full the siphon stops", "Move away — the siphon has a limited range"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🔌 Disconnect the siphon",
                "outcomes": {
                    "high": {"text": "You pull the siphon from the rock cleanly. The drain stops. The container — now yours — holds a significant charge collection.", "cp": 270, "tokens": 858, "tone": "good"},
                    "mid":  {"text": "You disconnect it. Drain stops. You take the container, partially full.", "cp": 172, "tokens": 480, "tone": "good"},
                    "low":  {"text": "Disconnecting it releases the stored charge in a burst. You absorb some but not all.", "cp": 65, "tokens": 90, "tone": "neutral"},
                }
            },
            {
                "label": "📦 Take the container",
                "outcomes": {
                    "high": {"text": "You take the full container before the siphon can drain more. Six months of collected summit charge. Extremely valuable.", "cp": 278, "tokens": 880, "tone": "good"},
                    "mid":  {"text": "You grab it quickly. The siphon keeps running against open air while you move away.", "cp": 168, "tokens": 470, "tone": "neutral"},
                    "low":  {"text": "The container is sealed and heavier than it looks. You fumble it and lose some in the transfer.", "cp": 58, "tokens": 78, "tone": "neutral"},
                }
            },
            {
                "label": "🏃 Just move away from it",
                "trap": True,
                "outcomes": {
                    "high": {"text": "The siphon's range is the whole summit. Moving away accomplishes nothing. You lose more charge just walking.", "cp": 20, "tokens": 15, "tone": "bad"},
                    "mid":  {"text": "You move. The drain slows slightly but doesn't stop. You've lost time and charge.", "cp": 15, "tokens": 10, "tone": "bad"},
                    "low":  {"text": "There is no edge of the siphon range on the summit. You drain the entire way.", "cp": 8, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Collapsed Bridge",
        "image": "zone5_e19",
        "scene": (
            "The bridge across a summit gap has partially collapsed. "
            "Two of the five support cables remain intact. "
            "The gap is about twenty feet. The drop is significant."
        ),
        "stat": "INS",
        "trait_text": {
            "VLT": "You could arc-jump the gap entirely if your charge is high enough.",
            "INS": "The remaining cables are still live. Your insulation means you can use them as handholds.",
            "SPK": "Two cables out of five. The weight distribution shifts to those two. Don't stand in the middle.",
        },
        "question": {
            "prompt": "Two of five support cables remain. The bridge can still hold weight — but not evenly distributed. Where do you walk?",
            "answers": ["Close to the remaining cables — that's where the load is supported", "Dead center — traditional bridge weight distribution", "The edges — farthest from the point of failure"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🌉 Cross carefully",
                "outcomes": {
                    "high": {"text": "You hug the cable lines perfectly. The bridge holds. You cross in twelve seconds without a creak.", "cp": 255, "tokens": 800, "tone": "good"},
                    "mid":  {"text": "You cross near the cables. It sways but holds. You make it.", "cp": 162, "tokens": 452, "tone": "neutral"},
                    "low":  {"text": "You drift toward center. The bridge protests loudly. You scramble across and make it.", "cp": 50, "tokens": 68, "tone": "bad"},
                }
            },
            {
                "label": "⚡ Arc-jump the gap",
                "outcomes": {
                    "high": {"text": "Your charge builds into an arc-jump and you clear the entire gap cleanly. The bridge collapses behind you as you land. Dramatic.", "cp": 268, "tokens": 838, "tone": "good"},
                    "mid":  {"text": "Your arc gets you most of the way. You catch the far edge and pull yourself up.", "cp": 165, "tokens": 462, "tone": "neutral"},
                    "low":  {"text": "Your charge isn't quite there. You fall short and have to find another way.", "cp": 45, "tokens": 60, "tone": "bad"},
                }
            },
            {
                "label": "🔧 Repair the cables first",
                "outcomes": {
                    "high": {"text": "You patch two more cables with summit wire you find nearby. The bridge is solid. While working you find a cache in the support structure.", "cp": 275, "tokens": 870, "tone": "good"},
                    "mid":  {"text": "You patch one cable. Enough to cross safely.", "cp": 162, "tokens": 452, "tone": "neutral"},
                    "low":  {"text": "Your repairs take longer than the bridge can wait. It shifts mid-patch.", "cp": 48, "tokens": 65, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Charge Echo",
        "image": "zone5_e20",
        "scene": (
            "Your charge output is bouncing off the summit rock at a specific frequency — "
            "creating an echo that comes back slightly stronger than it left. "
            "Each echo adds about 3% to your charge. But only if you pulse at exactly the right interval."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The echo resonates with your natural output. Your body already wants to pulse at that frequency.",
            "INS": "Your insulation won't interfere here — this is about timing, not protection.",
            "SPK": "3% per pulse. You can hear the interval. It's about two seconds. Steady, not fast.",
        },
        "question": {
            "prompt": "The echo returns stronger than it left, but only at the right pulse interval. You pulse too fast and the echoes start canceling each other. What's happening?",
            "answers": ["Destructive interference — pulses overlapping before return", "The rock is absorbing the excess energy", "Your charge level is too high to sustain the resonance"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "🎵 Match the interval",
                "outcomes": {
                    "high": {"text": "You pulse at exactly two seconds. The echoes stack cleanly. Your charge climbs. You sustain it long enough to gain a significant boost.", "cp": 262, "tokens": 822, "tone": "good"},
                    "mid":  {"text": "Your timing is mostly right. You gain a partial boost before losing the rhythm.", "cp": 165, "tokens": 462, "tone": "neutral"},
                    "low":  {"text": "Your timing is off and the echoes cancel. You gain nothing and lose a small amount.", "cp": 45, "tokens": 60, "tone": "bad"},
                }
            },
            {
                "label": "⚡ Pulse as fast as possible",
                "trap": True,
                "outcomes": {
                    "high": {"text": "Fast pulses destroy the resonance immediately. The echoes cancel each other on the second pulse.", "cp": 18, "tokens": 15, "tone": "bad"},
                    "mid":  {"text": "Destructive interference. The charge you tried to gain cancels out immediately.", "cp": 12, "tokens": 8, "tone": "bad"},
                    "low":  {"text": "You've been told what happens when you pulse too fast. This is that.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🔍 Experiment to find the interval",
                "outcomes": {
                    "high": {"text": "You test several intervals methodically and lock in the correct one. You sustain the resonance longer than someone guessing would.", "cp": 270, "tokens": 845, "tone": "good"},
                    "mid":  {"text": "You find a workable interval after a few tries. Not perfect but gains something.", "cp": 162, "tokens": 452, "tone": "neutral"},
                    "low":  {"text": "Your experiments take too long. The resonance window closes before you find it.", "cp": 45, "tokens": 60, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Two Paths",
        "image": "zone5_e21",
        "scene": (
            "The summit trail splits. Left path: wide, clear, no obstacles visible. "
            "Right path: narrow, rocky, with a faint charge signature emanating from somewhere ahead. "
            "A worn marker post at the fork has an arrow scratched into it pointing right — barely visible."
        ),
        "stat": "SPK",
        "trait_text": {
            "VLT": "The charge signature on the right path is faint but real. Something is powered over there.",
            "INS": "The left path's clearness is suspicious. The summit doesn't offer easy routes without reason.",
            "SPK": "Someone scratched that arrow. Carefully. That took effort. That means something.",
        },
        "question": {
            "prompt": "The left path is clear and easy. The right path is rocky but has a faint charge signature and a barely-visible scratched arrow. Which do you take?",
            "answers": ["Right — the charge signature and the arrow are both pointing there", "Left — clear paths exist for a reason", "Neither — the fork itself might be hiding something"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "➡️ Take the right path",
                "outcomes": {
                    "high": {"text": "The rocky path leads to a charge deposit the clear path completely bypasses. The arrow was left by someone who found it the hard way.", "cp": 268, "tokens": 835, "tone": "good"},
                    "mid":  {"text": "The right path is harder but the charge signature leads to something useful.", "cp": 165, "tokens": 462, "tone": "neutral"},
                    "low":  {"text": "The charge signature was residual — nothing active. Rocky path, no reward.", "cp": 50, "tokens": 68, "tone": "neutral"},
                }
            },
            {
                "label": "⬅️ Take the left path",
                "outcomes": {
                    "high": {"text": "The clear path is fast but you notice halfway through why it's clear — nothing worth having here. You arrive on time but empty-handed.", "cp": 45, "tokens": 60, "tone": "neutral"},
                    "mid":  {"text": "Clean, fast, uneventful. You arrive having missed whatever was on the right.", "cp": 38, "tokens": 48, "tone": "neutral"},
                    "low":  {"text": "The clear path loops slightly longer than it looked. Time lost, nothing gained.", "cp": 25, "tokens": 30, "tone": "bad"},
                }
            },
            {
                "label": "🔍 Check the fork itself",
                "outcomes": {
                    "high": {"text": "You check the marker post carefully and find a small hollow in its base. A cache has been here a long time.", "cp": 275, "tokens": 870, "tone": "good"},
                    "mid":  {"text": "The post has more markings on the back — someone's notes about both paths. You choose better for it.", "cp": 170, "tokens": 475, "tone": "good"},
                    "low":  {"text": "Just a post. You spent time on it and now need to choose anyway.", "cp": 40, "tokens": 55, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Overcharged Node",
        "image": "zone5_e22",
        "scene": (
            "A charge node on the summit is running at well over capacity — sparking, shaking, visibly unstable. "
            "It will either discharge harmlessly or explode within the next few minutes. "
            "The difference is whether the pressure has somewhere to go."
        ),
        "stat": "VLT",
        "trait_text": {
            "VLT": "You could give it somewhere to go. You're rated for this if you're careful.",
            "INS": "Your insulation protects you from the discharge but not the shockwave if it explodes.",
            "SPK": "Sparking, shaking, visibly pressured. The node needs a path out. That's the whole situation.",
        },
        "question": {
            "prompt": "The node will explode if the pressure has nowhere to go. What does it need?",
            "answers": ["A grounded path to discharge through — give it a direction", "Insulation around it — contain the pressure until it stabilizes", "Distance — let it discharge on its own safely"],
            "correct": 0,
        },
        "choices": [
            {
                "label": "⚡ Absorb the discharge",
                "outcomes": {
                    "high": {"text": "You plant yourself and absorb the full node discharge in a controlled burst. The node stabilizes. You're supercharged and the node rewards you with its stored reserves.", "cp": 285, "tokens": 928, "tone": "good"},
                    "mid":  {"text": "You absorb most of it. Some goes wide. The node calms down and you're significantly more charged.", "cp": 178, "tokens": 498, "tone": "good"},
                    "low":  {"text": "You absorb what you can but it's more than expected. The overshoot staggers you.", "cp": 58, "tokens": 78, "tone": "neutral"},
                }
            },
            {
                "label": "🔌 Ground it into the rock",
                "outcomes": {
                    "high": {"text": "You connect the node to a natural ground point in the summit rock. The discharge flows safely and the node opens its reserve panel in thanks.", "cp": 275, "tokens": 875, "tone": "good"},
                    "mid":  {"text": "Partial ground. The node discharges most of its overload. Stable enough to pass safely.", "cp": 168, "tokens": 470, "tone": "neutral"},
                    "low":  {"text": "Your grounding attempt is incomplete. The node discharges anyway — not at you, but close.", "cp": 55, "tokens": 75, "tone": "neutral"},
                }
            },
            {
                "label": "🏃 Back away fast",
                "outcomes": {
                    "high": {"text": "You get clear in time. The node explodes harmlessly with you at safe distance. The blast scatters cached items across the nearby ground.", "cp": 250, "tokens": 782, "tone": "good"},
                    "mid":  {"text": "You back away. The explosion is smaller than feared. You avoid it completely.", "cp": 148, "tokens": 415, "tone": "neutral"},
                    "low":  {"text": "You don't back away fast enough. The shockwave catches you.", "cp": 42, "tokens": 58, "tone": "bad"},
                }
            },
        ]
    },
]



# ─────────────────────────────────────────────
# ZONE REGISTRY
# ─────────────────────────────────────────────
ZONES = {
    1: {
        "name":        "The Static Fields",
        "cp_required": 0,
        "events":      ZONE1_EVENTS,
        "color":       0x888780,
        "emoji":       "⚡",
        "entry_fee":   0,
    },
    2: {
        "name":        "Voltage Bay",
        "cp_required": 500,
        "events":      ZONE2_EVENTS,
        "color":       0x1D9E75,
        "emoji":       "🌊",
        "entry_fee":   100,
    },
    3: {
        "name":        "Molten Circuit",
        "cp_required": 1500,
        "events":      ZONE3_EVENTS,
        "color":       0xBA7517,
        "emoji":       "🔥",
        "entry_fee":   250,
    },
    4: {
        "name":        "The Null Space",
        "cp_required": 4000,
        "events":      ZONE4_EVENTS,
        "color":       0x7F77DD,
        "emoji":       "🌀",
        "entry_fee":   500,
    },
    5: {
        "name":        "Apex Summit",
        "cp_required": 10000,
        "events":      ZONE5_EVENTS,
        "color":       0xD85A30,
        "emoji":       "🏔️",
        "entry_fee":   1000,
        "nft_drop_chance": 0.02,
    },
}


def get_eligible_zones(cp_total: int) -> list:
    return [num for num, z in ZONES.items() if cp_total >= z["cp_required"]]


def get_highest_zone(cp_total: int) -> int:
    eligible = get_eligible_zones(cp_total)
    return max(eligible) if eligible else 1


def draw_run(zone_num: int) -> list:
    """Draw 5 random events for a run. Guarantees at most 1 rival event per run."""
    pool = ZONES[zone_num]["events"]
    rival_events = [e for e in pool if e.get("rival")]
    normal_events = [e for e in pool if not e.get("rival")]

    # Always include at most 1 rival event; pick the rest from normal pool
    sampled_rivals = random.sample(rival_events, min(1, len(rival_events)))
    needed_normal  = 5 - len(sampled_rivals)
    sampled_normal = random.sample(normal_events, min(needed_normal, len(normal_events)))

    combined = sampled_rivals + sampled_normal
    random.shuffle(combined)
    return combined[:5]


def resolve_outcome(event: dict, choice_index: int, stats: dict) -> dict:
    choice   = event["choices"][choice_index]
    stat_key = event.get("stat")
    is_trap  = choice.get("trap", False)

    if is_trap:
        tier = "low"
    elif stat_key and stat_key in stats:
        tier = stat_tier(stats[stat_key])
    else:
        tier = "mid"

    outcomes = choice["outcomes"]
    return outcomes.get(tier, outcomes.get("mid", outcomes["low"]))


def resolve_outcome_hard_mode(event: dict, choice_index: int, stats: dict, question_correct: bool) -> dict:
    """
    Hard Mode resolution:
    - Correct answer  → guaranteed 'high' tier (ignores stats)
    - Wrong answer    → forced 'bad' tier (or 'low' if bad not present)
    - Trap choices    → always 'low' regardless of answer
    """
    choice  = event["choices"][choice_index]
    is_trap = choice.get("trap", False)
    outcomes = choice["outcomes"]

    if is_trap:
        tier = "low"
    elif question_correct:
        tier = "high"
    else:
        tier = outcomes.get("bad", "low") if "bad" in outcomes else "low"

    return outcomes.get(tier, outcomes.get("mid", list(outcomes.values())[0]))


def get_image_path(zone_num: int, event: dict) -> str | None:
    hint = event.get("image")
    if not hint:
        return None
    return f"{hint}.png"

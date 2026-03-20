"""
expedition_events.py
--------------------
All story content for Zappy Expedition.
Each zone has a pool of events. Each run draws 5 randomly.
Each event has 2-3 choices, each with good/bad outcome variants.
Trait influence shifts outcome probability — high relevant stat = better odds.

Image slots: zone{N}_event{M}.png — drop your images in the bot's working directory.
The bot attaches the image if the file exists, skips silently if not.

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


# ─────────────────────────────────────────────
# EVENT STRUCTURE
# Each event is a dict:
#   title       — short name shown in the embed header
#   image       — filename hint (e.g. "zone1_e1") → bot looks for zone1_e1.png
#   scene       — opening narration (2-3 sentences, Zappy voice)
#   stat        — which stat matters: "SPK", "VLT", "INS", or None
#   choices     — list of choice dicts
#
# Each choice:
#   label       — button text (max 4 words)
#   outcomes    — dict with keys "high", "mid", "low" (matching stat tier)
#                 each outcome is a dict: { text, cp, tokens, tone }
#                 tone: "good", "neutral", "bad" — affects embed color
# ─────────────────────────────────────────────

# ═══════════════════════════════════════════
# ZONE 1 — THE STATIC FIELDS
# Tone: curious, gentle, intro energy
# ═══════════════════════════════════════════

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
]


# ═══════════════════════════════════════════
# ZONE 2 — VOLTAGE BAY
# Tone: coastal, energetic, playful danger
# ═══════════════════════════════════════════

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
        "choices": [
            {
                "label": "🌊 Ride the surge",
                "outcomes": {
                    "high": {"text": "You catch the wave perfectly, riding a current of pure voltage down the coast at incredible speed. You arrive at the next point feeling electric and alive.", "cp": 50, "tokens": 30, "tone": "good"},
                    "mid":  {"text": "You catch the edge of the surge and get flung forward a good distance. Chaotic, but effective.", "cp": 35, "tokens": 15, "tone": "neutral"},
                    "low":  {"text": "The surge catches you sideways and tumbles you up the beach. You're fine, sandy, and somewhat rearranged.", "cp": 10, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "⏱️ Time the gaps",
                "outcomes": {
                    "high": {"text": "You read the rhythm perfectly and slip through three gaps in the surge pattern like you've done it a hundred times. Textbook.", "cp": 45, "tokens": 25, "tone": "good"},
                    "mid":  {"text": "Your timing is decent. You make it through with one wet paw and no serious damage.", "cp": 28, "tokens": 10, "tone": "neutral"},
                    "low":  {"text": "Your timing is off. You get caught by a small surge. It's not the big one but it still stings and you drop your snack.", "cp": 12, "tokens": 3, "tone": "bad"},
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
        "choices": [
            {
                "label": "💪 Force the hatch",
                "outcomes": {
                    "high": {"text": "You rip the hatch open with one clean motion. Inside: a cache of charged gems and a waterproof map of the bay. Score.", "cp": 55, "tokens": 40, "tone": "good"},
                    "mid":  {"text": "You force it open after a struggle. Inside is mostly seaweed and one decent gem.", "cp": 35, "tokens": 18, "tone": "neutral"},
                    "low":  {"text": "You can't budge it. The hatch wins. You leave feeling personally offended by a door.", "cp": 10, "tokens": 2, "tone": "bad"},
                }
            },
            {
                "label": "🔦 Search carefully",
                "outcomes": {
                    "high": {"text": "You find a hidden panel near the stern. Behind it: a small sealed chest with a coded lock you somehow solve immediately.", "cp": 60, "tokens": 45, "tone": "good"},
                    "mid":  {"text": "You find some scattered loot in the corners — not the big prize but a respectable haul.", "cp": 38, "tokens": 20, "tone": "neutral"},
                    "low":  {"text": "You search thoroughly and find mostly salt, rust, and what appears to be someone's old lunch.", "cp": 15, "tokens": 3, "tone": "neutral"},
                }
            },
            {
                "label": "⚡ Discharge the hull",
                "outcomes": {
                    "high": {"text": "You absorb all the residual charge in a single focused burst. The ship's systems briefly reactivate and eject a reward canister automatically.", "cp": 65, "tokens": 50, "tone": "good"},
                    "mid":  {"text": "You discharge most of it. The ship shudders and drops some cargo from an overhead shelf.", "cp": 40, "tokens": 22, "tone": "neutral"},
                    "low":  {"text": "The discharge backlashes and knocks you clean off the ship. You land on the beach with excellent airtime.", "cp": 8, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Fisherman's Bet",
        "image": "zone2_e3",
        "scene": (
            "An old Zappy sits on a dock, fishing with a rod made of copper and lightning rods. "
            "'I'll bet you,' he says, 'that you can't name what I just caught.' "
            "He holds up a bucket. You can hear it buzzing."
        ),
        "stat": "SPK",
        "choices": [
            {
                "label": "🎰 Take the bet",
                "outcomes": {
                    "high": {"text": "'A Sparkling Rayfish,' you say. He stares at you. That is exactly what it is. He pays up, grudgingly.", "cp": 55, "tokens": 45, "tone": "good"},
                    "mid":  {"text": "You guess wrong but close enough that he respects the attempt. You lose the bet but gain his fishing secret.", "cp": 25, "tokens": 10, "tone": "neutral"},
                    "low":  {"text": "You guess extremely wrong. He laughs for a very long time. You pay the bet. Painful.", "cp": 5, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🙅 Decline the bet",
                "outcomes": {
                    "high": {"text": "He shrugs and shows you anyway — a Prismatic Eel. He lets you hold it. It zaps you pleasantly and you feel recharged.", "cp": 40, "tokens": 20, "tone": "good"},
                    "mid":  {"text": "He shrugs. You keep walking. Probably a smart choice.", "cp": 20, "tokens": 8, "tone": "neutral"},
                    "low":  {"text": "You decline and he looks disappointed. The moment passes. Nothing happens and that's somehow sad.", "cp": 10, "tokens": 3, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Storm Cell",
        "image": "zone2_e4",
        "scene": (
            "A contained electrical storm is sitting directly on the path. "
            "It's about the size of a large tent. "
            "Inside, you can see something shining."
        ),
        "stat": "INS",
        "choices": [
            {
                "label": "🛡️ Walk straight in",
                "outcomes": {
                    "high": {"text": "Your insulation absorbs the storm completely. You walk through like it's a light drizzle and retrieve a charged orb from the center.", "cp": 60, "tokens": 50, "tone": "good"},
                    "mid":  {"text": "You make it through, sparking considerably. The orb is there. You grab it and run.", "cp": 38, "tokens": 22, "tone": "neutral"},
                    "low":  {"text": "The storm disagrees with your presence strongly. You get bounced back out twice before giving up.", "cp": 8, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🌀 Try to redirect it",
                "outcomes": {
                    "high": {"text": "You find the storm's rotation axis and give it a sharp nudge. It spins off the path and reveals the shining item sitting quietly on the ground.", "cp": 55, "tokens": 40, "tone": "good"},
                    "mid":  {"text": "You partially redirect it, creating a narrow corridor. You squeeze through and grab something off the edge of the shining thing.", "cp": 35, "tokens": 18, "tone": "neutral"},
                    "low":  {"text": "Redirecting a storm is harder than it sounds. It redirects you instead. You wake up ten meters away.", "cp": 10, "tokens": 2, "tone": "bad"},
                }
            },
            {
                "label": "⏳ Wait it out",
                "outcomes": {
                    "high": {"text": "You sit down and wait patiently. The storm dissipates after a few minutes, leaving behind a perfect reward cache.", "cp": 50, "tokens": 35, "tone": "good"},
                    "mid":  {"text": "You wait. It takes longer than expected but eventually clears. You collect what remains.", "cp": 30, "tokens": 12, "tone": "neutral"},
                    "low":  {"text": "You wait. And wait. The storm seems content here. After a very long time you go around it and find nothing.", "cp": 12, "tokens": 3, "tone": "neutral"},
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
        "choices": [
            {
                "label": "📡 Decode the signal",
                "outcomes": {
                    "high": {"text": "You decode it perfectly: coordinates to a supply drop three minutes from here. You follow them and find it exactly where promised.", "cp": 65, "tokens": 55, "tone": "good"},
                    "mid":  {"text": "You decode part of it — enough to find a general direction. You search that area and find something useful.", "cp": 40, "tokens": 22, "tone": "neutral"},
                    "low":  {"text": "You stare at the pattern for a long time. It remains mysterious. You leave feeling slightly haunted.", "cp": 10, "tokens": 2, "tone": "neutral"},
                }
            },
            {
                "label": "🔧 Repair the beacon",
                "outcomes": {
                    "high": {"text": "You fix the beacon quickly. A distant ship receives the signal and drops a reward crate from altitude as thanks. It lands nearby.", "cp": 70, "tokens": 60, "tone": "good"},
                    "mid":  {"text": "You patch it up adequately. The signal improves. Someone radios a thanks and tells you where they left spare supplies.", "cp": 42, "tokens": 25, "tone": "good"},
                    "low":  {"text": "You try to repair it and make things slightly worse. The beacon now pulses irregularly. You leave quickly.", "cp": 8, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
]


# ═══════════════════════════════════════════
# ZONE 3 — MOLTEN CIRCUIT
# Tone: intense, industrial, hot, chaotic
# ═══════════════════════════════════════════

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
        "choices": [
            {
                "label": "⚡ Absorb the overload",
                "outcomes": {
                    "high": {"text": "You take the full surge directly and channel it through your body in a controlled discharge on the other side. You feel incredible. The conduit stabilizes.", "cp": 80, "tokens": 65, "tone": "good"},
                    "mid":  {"text": "You absorb what you can. Some gets through you wildly. You make it across supercharged and slightly smoking.", "cp": 55, "tokens": 35, "tone": "neutral"},
                    "low":  {"text": "The overload is too much. It throws you backward with considerable enthusiasm.", "cp": 10, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🏃 Sprint under it",
                "outcomes": {
                    "high": {"text": "Pure speed. You're under and through in under a second. The conduit doesn't even know you were there.", "cp": 70, "tokens": 55, "tone": "good"},
                    "mid":  {"text": "You sprint through. A bolt clips your ear. You make it and it barely counted.", "cp": 48, "tokens": 28, "tone": "neutral"},
                    "low":  {"text": "Not fast enough. The conduit clips you mid-sprint. You tumble through and land in a heap on the other side, successful but destroyed.", "cp": 20, "tokens": 5, "tone": "bad"},
                }
            },
            {
                "label": "🔌 Find the breaker",
                "outcomes": {
                    "high": {"text": "You locate the emergency breaker panel behind a maintenance cover. You cut the flow, walk through calmly, then restore it. Professional.", "cp": 85, "tokens": 70, "tone": "good"},
                    "mid":  {"text": "You find the panel and reduce the load enough to safely pass. The conduit hums its approval.", "cp": 60, "tokens": 40, "tone": "neutral"},
                    "low":  {"text": "You find the panel but activate the wrong switch. The conduit gets louder somehow. You run.", "cp": 15, "tokens": 3, "tone": "bad"},
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
        "choices": [
            {
                "label": "🎯 Find the safe path",
                "outcomes": {
                    "high": {"text": "Your instincts are perfect. You hop stone to stone, each one cool, each landing exact. You cross without a scratch.", "cp": 75, "tokens": 60, "tone": "good"},
                    "mid":  {"text": "You find a decent path. A few hot spots but manageable. You arrive warm-pawed but intact.", "cp": 50, "tokens": 30, "tone": "neutral"},
                    "low":  {"text": "Your path is mostly wrong. It's fine. Hot but fine. You have a high pain tolerance probably.", "cp": 18, "tokens": 5, "tone": "bad"},
                }
            },
            {
                "label": "🛡️ Power through",
                "outcomes": {
                    "high": {"text": "Your insulation handles the heat effortlessly. You walk straight across like it's carpet.", "cp": 70, "tokens": 55, "tone": "good"},
                    "mid":  {"text": "Your insulation helps but not perfectly. You make it across singed but successful.", "cp": 45, "tokens": 25, "tone": "neutral"},
                    "low":  {"text": "Very hot. Very painful. You make it but you are not comfortable. Take a moment.", "cp": 12, "tokens": 0, "tone": "bad"},
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
        "choices": [
            {
                "label": "⚔️ Fight it",
                "outcomes": {
                    "high": {"text": "Your VLT overwhelms its systems in three precise strikes. It shuts down and, interestingly, opens its chest cavity to reveal a reward module.", "cp": 90, "tokens": 75, "tone": "good"},
                    "mid":  {"text": "A tough fight. You win but your circuits take a hit. You collect the parts it drops.", "cp": 58, "tokens": 38, "tone": "neutral"},
                    "low":  {"text": "It wins the fight easily. You leave quickly. The automaton watches you go, its eyes cycling yellow.", "cp": 10, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🤝 Greet it calmly",
                "outcomes": {
                    "high": {"text": "It was just lost and confused. You communicate in blinking patterns and help it find its charging station. It gives you its spare power cell as thanks.", "cp": 85, "tokens": 70, "tone": "good"},
                    "mid":  {"text": "It calms down slightly and lets you pass, keeping its eyes on you the whole time.", "cp": 45, "tokens": 20, "tone": "neutral"},
                    "low":  {"text": "It does not respond to calm. It raises both arms now. You leave.", "cp": 10, "tokens": 2, "tone": "bad"},
                }
            },
            {
                "label": "🔧 Attempt repairs",
                "outcomes": {
                    "high": {"text": "You find its reset panel and stabilize the erratic loop. It reboots, thanks you in binary, and steps aside with a gift.", "cp": 95, "tokens": 80, "tone": "good"},
                    "mid":  {"text": "Your repairs are partial. It stops sparking but still watches you suspiciously. You pass without incident.", "cp": 55, "tokens": 30, "tone": "neutral"},
                    "low":  {"text": "You make it worse. It starts spinning. You run while it spins.", "cp": 8, "tokens": 0, "tone": "bad"},
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
        "choices": [
            {
                "label": "🎲 Go for it",
                "outcomes": {
                    "high": {"text": "Your timing is supernatural. You thread through every vent at exactly the right moment. You don't even feel the heat.", "cp": 85, "tokens": 70, "tone": "good"},
                    "mid":  {"text": "Your timing is mostly right. Two vents catch you but not directly. You make it through smoky.", "cp": 55, "tokens": 32, "tone": "neutral"},
                    "low":  {"text": "Your timing is quite bad. You get vented. Multiple times. You arrive on the other side glowing.", "cp": 12, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "📊 Map the pattern",
                "outcomes": {
                    "high": {"text": "You watch for exactly one minute and crack the pattern. Your crossing is mathematically perfect.", "cp": 90, "tokens": 75, "tone": "good"},
                    "mid":  {"text": "You map most of the pattern. One vent is unpredictable. You get that one, but only that one.", "cp": 60, "tokens": 38, "tone": "neutral"},
                    "low":  {"text": "The pattern is genuinely random. Your mapping was pointless. You learn something about chaos.", "cp": 18, "tokens": 5, "tone": "neutral"},
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
        "choices": [
            {
                "label": "🔨 Forge a weapon",
                "outcomes": {
                    "high": {"text": "You craft a small charged blade with perfect technique. It hums in your grip. You leave it on the sign as instructed, but keep the surplus metal — which happens to be very valuable.", "cp": 95, "tokens": 85, "tone": "good"},
                    "mid":  {"text": "Your forge work is decent. You make something functional and leave it. The act of creating feels rewarding.", "cp": 60, "tokens": 40, "tone": "good"},
                    "low":  {"text": "Your forge work is rough. You make something. It's unclear what. You leave it. The sign accepts it without judgment.", "cp": 20, "tokens": 5, "tone": "neutral"},
                }
            },
            {
                "label": "⚡ Charge yourself here",
                "outcomes": {
                    "high": {"text": "You use the forge as a personal charging station. You leave fully recharged and find the heat has annealed your stats for the rest of the run.", "cp": 80, "tokens": 70, "tone": "good"},
                    "mid":  {"text": "Good charge. You feel stronger. You leave a piece of scrap metal as your contribution.", "cp": 55, "tokens": 32, "tone": "good"},
                    "low":  {"text": "The forge charges you but also singes you somewhat. Mixed results.", "cp": 22, "tokens": 8, "tone": "neutral"},
                }
            },
            {
                "label": "🎁 Leave a gift",
                "outcomes": {
                    "high": {"text": "You leave something meaningful. The forge responds — a hidden drawer opens beneath it, filled with items left by previous visitors.", "cp": 100, "tokens": 90, "tone": "good"},
                    "mid":  {"text": "You leave something. The forge glows warmly. You feel good about this.", "cp": 50, "tokens": 28, "tone": "good"},
                    "low":  {"text": "You don't have much to leave. You leave what you have. That counts.", "cp": 25, "tokens": 10, "tone": "neutral"},
                }
            },
        ]
    },
]


# ═══════════════════════════════════════════
# ZONE 4 — THE NULL SPACE
# Tone: strange, surreal, physics-optional
# ═══════════════════════════════════════════

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
        "choices": [
            {
                "label": "👋 Wave back",
                "outcomes": {
                    "high": {"text": "The reflection that waved reaches through the mirror and hands you a small glowing object before vanishing. You don't ask questions.", "cp": 120, "tokens": 110, "tone": "good"},
                    "mid":  {"text": "The reflection nods. Something passes between you. The corridor feels less hostile after that.", "cp": 80, "tokens": 55, "tone": "neutral"},
                    "low":  {"text": "The reflection waves back at you waving back. This continues for an uncomfortable amount of time.", "cp": 20, "tokens": 5, "tone": "neutral"},
                }
            },
            {
                "label": "🏃 Follow the running one",
                "outcomes": {
                    "high": {"text": "You run with it and it leads you through a shortcut in the mirror logic. You exit the corridor faster than should be possible.", "cp": 130, "tokens": 120, "tone": "good"},
                    "mid":  {"text": "You run but can't keep up. The reflection disappears. You exit the normal way but feel like you almost understood something.", "cp": 70, "tokens": 40, "tone": "neutral"},
                    "low":  {"text": "You run and get completely turned around. You exit from the entrance. The running reflection is gone.", "cp": 15, "tokens": 2, "tone": "bad"},
                }
            },
            {
                "label": "🙈 Ignore them all",
                "outcomes": {
                    "high": {"text": "Your refusal to engage breaks the corridor's logic. It glitches and deposits you at the far end with a reward it couldn't justify keeping.", "cp": 115, "tokens": 100, "tone": "good"},
                    "mid":  {"text": "You walk through without looking. The reflections seem disappointed. You feel a light impact as something bounces off your back — a small coin.", "cp": 65, "tokens": 35, "tone": "neutral"},
                    "low":  {"text": "One of the reflections trips you. How. Why. The Null Space has its own rules.", "cp": 10, "tokens": 0, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Gravity Reversal Zone",
        "image": "zone4_e2",
        "scene": (
            "A circular patch of ground has its gravity reversed. "
            "Things that fell up are sitting on the sky-ceiling. "
            "You can see what appear to be abandoned supplies up there."
        ),
        "stat": "VLT",
        "choices": [
            {
                "label": "⬆️ Jump in and grab them",
                "outcomes": {
                    "high": {"text": "You leap in, flip perfectly, collect everything on the ceiling, and time your exit back through normal gravity like an acrobat.", "cp": 125, "tokens": 115, "tone": "good"},
                    "mid":  {"text": "You get in and out with some of the supplies but the transition back is rough. Worth it.", "cp": 85, "tokens": 60, "tone": "neutral"},
                    "low":  {"text": "You enter the zone fine but the exit back to normal gravity catches you completely off guard. You impact the ground normally and lie there for a moment.", "cp": 15, "tokens": 5, "tone": "bad"},
                }
            },
            {
                "label": "🎣 Find something to fish with",
                "outcomes": {
                    "high": {"text": "You rig a long line with a hook and fish the supplies down from outside the zone. Clever, safe, effective.", "cp": 120, "tokens": 108, "tone": "good"},
                    "mid":  {"text": "Your makeshift line works for some of the items. The heavier ones stay up.", "cp": 75, "tokens": 45, "tone": "neutral"},
                    "low":  {"text": "You can't find anything suitable. A stick and a piece of wire is not a fishing rod.", "cp": 18, "tokens": 3, "tone": "neutral"},
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
        "choices": [
            {
                "label": "👂 Listen carefully",
                "outcomes": {
                    "high": {"text": "You tune in perfectly and the hum resolves into clear information: the location of a hidden cache thirty seconds from here. You find it.", "cp": 135, "tokens": 125, "tone": "good"},
                    "mid":  {"text": "You catch fragments — enough to know which direction to go. You find something, though not the main thing.", "cp": 88, "tokens": 62, "tone": "neutral"},
                    "low":  {"text": "You can't make anything of it. The hum continues. You move on with it following you faintly.", "cp": 20, "tokens": 5, "tone": "neutral"},
                }
            },
            {
                "label": "📣 Hum back",
                "outcomes": {
                    "high": {"text": "You hit exactly the right frequency. The hum surges joyfully and showers you with stored energy.", "cp": 140, "tokens": 130, "tone": "good"},
                    "mid":  {"text": "You hum back and something shifts. The environment feels more navigable. You find an easier path.", "cp": 90, "tokens": 65, "tone": "good"},
                    "low":  {"text": "The hum falls silent. You hum alone for a moment. Nothing. You continue.", "cp": 18, "tokens": 3, "tone": "neutral"},
                }
            },
            {
                "label": "🚫 Block it out",
                "outcomes": {
                    "high": {"text": "Your mental discipline blocks the hum completely. In the sudden silence you notice something the hum was hiding — a concealed entrance to a side room.", "cp": 130, "tokens": 115, "tone": "good"},
                    "mid":  {"text": "You block most of it and move through quickly. Less rattled than you would have been.", "cp": 70, "tokens": 38, "tone": "neutral"},
                    "low":  {"text": "You can't block it. The hum is in your bones now. You carry it for the rest of the run.", "cp": 12, "tokens": 0, "tone": "bad"},
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
        "choices": [
            {
                "label": "🎰 Walk straight through",
                "outcomes": {
                    "high": {"text": "The probability field reads your SPK and assigns you a wonderful outcome. You emerge with a significant reward and some lingering good luck.", "cp": 150, "tokens": 140, "tone": "good"},
                    "mid":  {"text": "The field is neutral to you. You emerge with moderate rewards and one slightly strange memory.", "cp": 90, "tokens": 65, "tone": "neutral"},
                    "low":  {"text": "The field does not favor you today. Extremely educational.", "cp": 10, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🧲 Attract it toward you",
                "outcomes": {
                    "high": {"text": "You pull the best of the probability storm's potential toward you through sheer luck. The good outcomes crystallize and fall into your hands.", "cp": 160, "tokens": 150, "tone": "good"},
                    "mid":  {"text": "You attract some probability. Mostly neutral outcomes with one bright spot.", "cp": 95, "tokens": 70, "tone": "neutral"},
                    "low":  {"text": "You attract the storm but not the good parts of it. Several things go slightly wrong at once.", "cp": 8, "tokens": 0, "tone": "bad"},
                }
            },
            {
                "label": "🚶 Go around it",
                "outcomes": {
                    "high": {"text": "Going around was the right call. You find the path around is actually better — calmer, faster, and stocked with items the probability storm shook loose.", "cp": 140, "tokens": 125, "tone": "good"},
                    "mid":  {"text": "You go around safely. Boring but fine. You collect a small amount of probability residue from the edge.", "cp": 78, "tokens": 48, "tone": "neutral"},
                    "low":  {"text": "You go around. It takes longer than expected. The path is uneventful in a disappointing way.", "cp": 30, "tokens": 10, "tone": "neutral"},
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
        "choices": [
            {
                "label": "🔮 Say what you heard",
                "outcomes": {
                    "high": {"text": "You say exactly what you heard. The time loop closes cleanly. A reward materializes from the resolved paradox.", "cp": 155, "tokens": 145, "tone": "good"},
                    "mid":  {"text": "You say it close enough. The echo accepts this. Something good happens nearby.", "cp": 95, "tokens": 68, "tone": "neutral"},
                    "low":  {"text": "You say it wrong somehow. The echo multiplies. You now have several voices saying slightly different things. Move on quickly.", "cp": 15, "tokens": 2, "tone": "bad"},
                }
            },
            {
                "label": "🤐 Say something else",
                "outcomes": {
                    "high": {"text": "You say something different and better. The time echo recalibrates around your new choice and both versions of you end up with rewards.", "cp": 165, "tokens": 155, "tone": "good"},
                    "mid":  {"text": "The echo is confused but not hostile. It replays your new words and fades. You move on.", "cp": 88, "tokens": 60, "tone": "neutral"},
                    "low":  {"text": "The contradiction creates a small temporal knot. Nothing explodes but something feels unresolved.", "cp": 12, "tokens": 2, "tone": "bad"},
                }
            },
            {
                "label": "🔇 Say nothing",
                "outcomes": {
                    "high": {"text": "Your silence breaks the echo. Without a source, it dissolves into pure energy that you absorb.", "cp": 150, "tokens": 138, "tone": "good"},
                    "mid":  {"text": "Silence. The echo fades. You feel a strange peace.", "cp": 82, "tokens": 52, "tone": "neutral"},
                    "low":  {"text": "The echo of your voice continues to speak without you. It sounds more confident than you feel.", "cp": 18, "tokens": 5, "tone": "neutral"},
                }
            },
        ]
    },
]


# ═══════════════════════════════════════════
# ZONE 5 — APEX SUMMIT
# Tone: epic, high stakes, final frontier
# NFT drop possible here
# ═══════════════════════════════════════════

ZONE5_EVENTS = [
    {
        "title": "The Storm Crown",
        "image": "zone5_e1",
        "scene": (
            "The summit is ringed by a permanent storm that only the worthy can pass. "
            "Lightning strikes the peak every few seconds. "
            "You are either very brave or very stubborn."
        ),
        "stat": "INS",
        "choices": [
            {
                "label": "🛡️ Absorb every bolt",
                "outcomes": {
                    "high": {"text": "You stand with your arms open and absorb the full storm crown. Lightning fills you completely. You glow for several minutes. The peak opens.", "cp": 200, "tokens": 180, "tone": "good"},
                    "mid":  {"text": "You absorb most of it, staggering under the weight of the current. You make it through crackling and triumphant.", "cp": 140, "tokens": 110, "tone": "neutral"},
                    "low":  {"text": "The storm is too much. You make it through but barely, arriving on the other side fundamentally reassembled.", "cp": 30, "tokens": 8, "tone": "bad"},
                }
            },
            {
                "label": "⚡ Match its frequency",
                "outcomes": {
                    "high": {"text": "You resonate at exactly the storm's frequency. It parts for you like a curtain. You walk through in silence while lightning crackles on either side.", "cp": 210, "tokens": 190, "tone": "good"},
                    "mid":  {"text": "Your frequency is close enough. The storm thins where you walk. A few bolts still find you but nothing you can't handle.", "cp": 145, "tokens": 115, "tone": "neutral"},
                    "low":  {"text": "Wrong frequency. The storm doubles down on you specifically. You survive and that is enough.", "cp": 25, "tokens": 5, "tone": "bad"},
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
        "stat": None,
        "choices": [
            {
                "label": "⚔️ Speak of strength",
                "outcomes": {
                    "high": {"text": "You speak of the battles won, the odds beaten, the Clashes survived. The Gatekeeper nods slowly. 'Then enter as a warrior.' The gate opens wide.", "cp": 195, "tokens": 175, "tone": "good"},
                    "mid":  {"text": "Your answer is honest if not poetic. The Gatekeeper considers it. 'Good enough,' they say. Not the warmest welcome, but a welcome.", "cp": 130, "tokens": 100, "tone": "neutral"},
                    "low":  {"text": "You stumble over the words. The Gatekeeper sighs gently and steps aside. 'You made it here. That's the answer.'", "cp": 50, "tokens": 20, "tone": "neutral"},
                }
            },
            {
                "label": "❤️ Speak of loyalty",
                "outcomes": {
                    "high": {"text": "You speak of your holder. Of being chosen, carried, brought this far. The Gatekeeper's expression softens completely. 'The bond is real. Enter.'", "cp": 220, "tokens": 200, "tone": "good"},
                    "mid":  {"text": "Your words are genuine. The Gatekeeper hears it. They open the gate without ceremony but with warmth.", "cp": 145, "tokens": 118, "tone": "good"},
                    "low":  {"text": "The words feel true but come out tangled. The Gatekeeper pats your shoulder. 'It's okay. Go on.'", "cp": 55, "tokens": 22, "tone": "neutral"},
                }
            },
            {
                "label": "🌟 Speak of the journey",
                "outcomes": {
                    "high": {"text": "You recount every zone, every choice, every moment. The Gatekeeper listens to all of it. When you finish they are quiet for a long time. 'That,' they say, 'is why.'", "cp": 230, "tokens": 210, "tone": "good"},
                    "mid":  {"text": "You tell your story. It's a good one. The Gatekeeper opens the gate and says nothing more — none is needed.", "cp": 150, "tokens": 120, "tone": "good"},
                    "low":  {"text": "Your story is shorter than you expected. The Gatekeeper nods. 'More will come. Go add to it.'", "cp": 60, "tokens": 25, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Infinite Generator",
        "image": "zone5_e3",
        "scene": (
            "At the peak: a generator the size of a building, spinning silently, generating more power than anything should. "
            "It has no off switch. It has no purpose listed. "
            "It simply hums with the energy of everything."
        ),
        "stat": "VLT",
        "choices": [
            {
                "label": "⚡ Interface with it",
                "outcomes": {
                    "high": {"text": "You plug in and become briefly part of the circuit. You feel every watt of it. When you disconnect, you carry a fraction of infinite. The generator dims imperceptibly.", "cp": 240, "tokens": 220, "tone": "good"},
                    "mid":  {"text": "The interface is overwhelming but you hold it for long enough to absorb a significant charge.", "cp": 155, "tokens": 125, "tone": "neutral"},
                    "low":  {"text": "The generator rejects your interface attempt firmly. Not painful, just absolute. You watch it hum from a safe distance.", "cp": 35, "tokens": 10, "tone": "bad"},
                }
            },
            {
                "label": "🧩 Study its design",
                "outcomes": {
                    "high": {"text": "You understand it. Not completely — no one ever will — but enough to extract a design schematic that is extremely valuable to the right people. You know who.", "cp": 235, "tokens": 215, "tone": "good"},
                    "mid":  {"text": "You study it and come away with partial insight. Enough to feel like you've earned something rare.", "cp": 148, "tokens": 120, "tone": "good"},
                    "low":  {"text": "You study it for a long time and understand very little. That is probably correct.", "cp": 40, "tokens": 12, "tone": "neutral"},
                }
            },
            {
                "label": "🙏 Respect it from afar",
                "outcomes": {
                    "high": {"text": "You stand before it and do not take. The generator notices. It gifts you something freely — a small piece of itself, offered without being asked.", "cp": 250, "tokens": 230, "tone": "good"},
                    "mid":  {"text": "Your restraint is rewarded. Not immediately, but as you leave, you find something left for you at the base of the path.", "cp": 140, "tokens": 112, "tone": "good"},
                    "low":  {"text": "You respect it from afar. It accepts your respect. Nothing more is exchanged. This is okay.", "cp": 50, "tokens": 18, "tone": "neutral"},
                }
            },
        ]
    },
    {
        "title": "The Last Broadcast",
        "image": "zone5_e4",
        "scene": (
            "A single antenna at the summit is transmitting a signal — not to anyone nearby, but out, far. "
            "The control panel is still warm. "
            "You could add something to the broadcast."
        ),
        "stat": "SPK",
        "choices": [
            {
                "label": "📡 Add your signal",
                "outcomes": {
                    "high": {"text": "Your signal is clear and strong. It joins the broadcast and travels outward. Something — somewhere — receives it. A reply comes back in the form of a resource drop.", "cp": 230, "tokens": 215, "tone": "good"},
                    "mid":  {"text": "Your signal goes out. Whether it reaches anyone, you don't know. The act feels important.", "cp": 145, "tokens": 118, "tone": "good"},
                    "low":  {"text": "Your signal is weak. It joins the broadcast but gets lost quickly. You still tried. That goes on record somewhere.", "cp": 45, "tokens": 15, "tone": "neutral"},
                }
            },
            {
                "label": "🔍 Decode what's there",
                "outcomes": {
                    "high": {"text": "You decode the existing broadcast — it's been running for years, a map of the entire expedition path with hidden caches marked. You copy it all.", "cp": 245, "tokens": 225, "tone": "good"},
                    "mid":  {"text": "You decode fragments. Enough to find one cache that wasn't on any map you had.", "cp": 155, "tokens": 125, "tone": "good"},
                    "low":  {"text": "The encoding is beyond you. The broadcast continues, mysterious and ancient.", "cp": 35, "tokens": 10, "tone": "neutral"},
                }
            },
            {
                "label": "📴 Shut it down",
                "outcomes": {
                    "high": {"text": "You shut it down cleanly. In the silence that follows, you realize the broadcast was powering a containment field. Whatever was inside it is now free — and grateful.", "cp": 260, "tokens": 240, "tone": "good"},
                    "mid":  {"text": "You shut it down. The quiet feels significant. Something in the area shifts.", "cp": 148, "tokens": 120, "tone": "neutral"},
                    "low":  {"text": "The shutdown triggers a failsafe alarm. You silence it quickly and leave before anything else happens.", "cp": 30, "tokens": 8, "tone": "bad"},
                }
            },
        ]
    },
    {
        "title": "The Apex Itself",
        "image": "zone5_e5",
        "scene": (
            "You are at the very top. "
            "The whole world spreads out below you — the Fields, the Bay, the Circuit, the Null Space, all of it. "
            "You have earned this view. What do you do with it?"
        ),
        "stat": None,
        "choices": [
            {
                "label": "🌟 Take it all in",
                "outcomes": {
                    "high": {"text": "You stand there for a long moment. The view fills you. You come down changed — more experience, more CP, and a feeling that this was worth every step.", "cp": 280, "tokens": 260, "tone": "good"},
                    "mid":  {"text": "You take it in as best you can. It's a lot. You carry some of it back down with you.", "cp": 175, "tokens": 145, "tone": "good"},
                    "low":  {"text": "It's a bit overwhelming honestly. You sit down. That's fine. The view waits for you.", "cp": 60, "tokens": 25, "tone": "neutral"},
                }
            },
            {
                "label": "🏴 Plant your flag",
                "outcomes": {
                    "high": {"text": "You plant your flag at the apex and the whole expedition record updates. You are now part of the summit's history. A bonus cache activates in your name.", "cp": 290, "tokens": 270, "tone": "good"},
                    "mid":  {"text": "You plant it. It stands. Future expeditions will see it. The summit logs your arrival.", "cp": 180, "tokens": 150, "tone": "good"},
                    "low":  {"text": "You plant your flag. The wind is strong up here. It blows sideways a little. Still counts.", "cp": 65, "tokens": 28, "tone": "neutral"},
                }
            },
            {
                "label": "🔄 Begin the descent",
                "outcomes": {
                    "high": {"text": "You turn around immediately, already planning the next run. The summit respects the drive. It drops a significant reward at your feet as you leave.", "cp": 275, "tokens": 255, "tone": "good"},
                    "mid":  {"text": "You head back down with purpose. The experience is already crystallizing into something useful.", "cp": 168, "tokens": 138, "tone": "good"},
                    "low":  {"text": "You head down. The summit is behind you. There is always the next time.", "cp": 55, "tokens": 22, "tone": "neutral"},
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
        "color":       0x888780,   # gray
        "emoji":       "⚡",
    },
    2: {
        "name":        "Voltage Bay",
        "cp_required": 500,
        "events":      ZONE2_EVENTS,
        "color":       0x1D9E75,   # teal
        "emoji":       "🌊",
    },
    3: {
        "name":        "Molten Circuit",
        "cp_required": 1500,
        "events":      ZONE3_EVENTS,
        "color":       0xBA7517,   # amber
        "emoji":       "🔥",
    },
    4: {
        "name":        "The Null Space",
        "cp_required": 4000,
        "events":      ZONE4_EVENTS,
        "color":       0x7F77DD,   # purple
        "emoji":       "🌀",
    },
    5: {
        "name":        "Apex Summit",
        "cp_required": 10000,
        "events":      ZONE5_EVENTS,
        "color":       0xD85A30,   # coral
        "emoji":       "🏔️",
        "nft_drop_chance": 0.02,   # 2% per run
    },
}


def get_eligible_zones(cp_total: int) -> list:
    """Return list of zone numbers the player can access."""
    return [num for num, z in ZONES.items() if cp_total >= z["cp_required"]]


def get_highest_zone(cp_total: int) -> int:
    """Return the highest zone number the player can access."""
    eligible = get_eligible_zones(cp_total)
    return max(eligible) if eligible else 1


def draw_run(zone_num: int) -> list:
    """Draw 5 random events for a run in the given zone."""
    pool = ZONES[zone_num]["events"]
    return random.sample(pool, min(5, len(pool)))


def resolve_outcome(event: dict, choice_index: int, stats: dict) -> dict:
    """
    Resolve an event outcome based on the player's choice and Zappy stats.
    Returns the outcome dict: { text, cp, tokens, tone }
    """
    choice  = event["choices"][choice_index]
    stat_key = event.get("stat")

    if stat_key and stat_key in stats:
        tier = stat_tier(stats[stat_key])
    else:
        # No relevant stat — use mid as default
        tier = "mid"

    outcomes = choice["outcomes"]

    # Return the matching tier, fallback to mid
    return outcomes.get(tier, outcomes.get("mid", outcomes["low"]))


def get_image_path(zone_num: int, event: dict) -> str | None:
    """Return the expected image filename for an event, or None."""
    hint = event.get("image")
    if not hint:
        return None
    return f"{hint}.png"

# recap_generator.py
# ------------------
# Generates an AI-written champion recap by passing the winner's
# NFT image + stats to the Anthropic API (vision).

import aiohttp
import base64
import os

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

RECAP_SYSTEM_PROMPT = """You are the official announcer for Zappies Reborn — a high-energy NFT battle league on Algorand.
You write punchy, hype bracket champion recaps based on the winner's NFT image and their stats.

Study the image carefully: the character's design, costume, expression, posture, attitude, colors.
Write a short Discord recap (4-6 sentences) that:
- Opens with a dramatic line specific to THIS character's look (no generic "Congratulations!")
- Weaves in their stats naturally
- Captures the personality and vibe you see in the image
- Ends with a short punchy closer

Tone: sports announcer meets NFT hype. Bold. Specific. Never generic.
Plain text only — no markdown, headers, or bullet points."""


async def generate_champion_recap(
    zappy_name: str,
    display_name: str,
    image_url: str,
    wins: int,
    losses: int,
    champ_count: int,
    cp_earned: int,
) -> str | None:
    """
    Fetch the NFT image and generate an AI recap via Anthropic vision API.
    Returns the recap string, or None if anything fails.
    """
    if not ANTHROPIC_API_KEY:
        print("[recap_generator] ANTHROPIC_API_KEY not set — skipping recap.")
        return None

    try:
        # Fetch the NFT image
        async with aiohttp.ClientSession() as session:
            async with session.get(
                image_url,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                image_bytes = await resp.read()
                content_type = resp.content_type or "image/png"

        # Normalize to types Anthropic accepts
        if "jpeg" in content_type or "jpg" in content_type:
            media_type = "image/jpeg"
        elif "gif" in content_type:
            media_type = "image/gif"
        elif "webp" in content_type:
            media_type = "image/webp"
        else:
            media_type = "image/png"

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        user_text = (
            f"Fighter: {zappy_name}\n"
            f"Owner: @{display_name}\n"
            f"Record: {wins}W {losses}L\n"
            f"Bracket Champion count: {champ_count}x\n"
            f"CP earned this bracket: +{cp_earned} CP\n\n"
            f"Write their victory recap."
        )

        payload = {
            "model": "claude-opus-4-5",
            "max_tokens": 300,
            "system": RECAP_SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": user_text,
                    },
                ],
            }],
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()

        if "error" in data:
            print(f"[recap_generator] API error: {data['error']}")
            return None

        return data["content"][0]["text"]

    except Exception as e:
        print(f"[recap_generator] failed: {e}")
        return None

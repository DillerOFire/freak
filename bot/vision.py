import base64
import logging
from openai import AsyncOpenAI
from config import LLM_API_KEY, LLM_VISION_BASE_URL, LLM_REFERER, LLM_TITLE, LLM_VISION_MODEL

client = AsyncOpenAI(
    base_url=LLM_VISION_BASE_URL,
    api_key=LLM_API_KEY,
    timeout=15.0,
    default_headers={
        "HTTP-Referer": LLM_REFERER,
        "X-Title": LLM_TITLE,
    },
)

_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
]

_IMAGE_PROMPT = (
    "Describe this image precisely. Include:\n"
    "- Main subject: who or what is shown (people, animals, objects, scenery)\n"
    "- Appearance: clothing, colors, facial expressions, poses, distinguishing features\n"
    "- Setting: location, background, time of day, weather if visible\n"
    "- Text: ALL visible text — captions, labels, signs, watermarks, chat messages, usernames, timestamps\n"
    "- UI details: if this is a screenshot, note the app/platform, user roles, badges, tags, status indicators, icons, notification counts, pinned items, reactions, or any other interface elements\n"
    "- Style: whether it's a photo, meme, screenshot, drawing, AI-generated, etc.\n"
    "- Mood/tone: humorous, serious, ironic, absurd, wholesome, etc.\n"
    "Keep it to 3-6 sentences. Be specific — use names, brands, or references if recognizable."
)

_FRAMES_PROMPT = (
    "These frames are extracted from a video or animation. Describe what happens:\n"
    "- Action: what is happening across the frames, the sequence of events\n"
    "- Subjects: who or what is involved — people, characters, objects\n"
    "- Appearance: clothing, colors, expressions, distinguishing features\n"
    "- Setting: location, background, any visible text or overlays\n"
    "- UI details: if this is a screen recording, note the app/platform, user roles, badges, tags, status indicators, icons, notifications, or any other interface elements\n"
    "- Style: live-action, animation, screen recording, meme, etc.\n"
    "- Mood/tone: funny, dramatic, chaotic, calm, absurd, etc.\n"
    "Keep it to 3-6 sentences. Be specific — use names, brands, or references if recognizable."
)


async def analyze_image(image_bytes: bytes) -> str:
    """Analyzes a single image and returns a description."""
    try:
        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        response = await client.chat.completions.create(
            model=LLM_VISION_MODEL,
            max_tokens=500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": _IMAGE_PROMPT,
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            },
                        },
                    ],
                }
            ],
            extra_body={
                "reasoning": {
                    "effort": "none",
                    "enabled": False,
                },
                "safetySettings": _SAFETY_SETTINGS,
            },
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Error analyzing image: {e}")
        return "Error analyzing image."


async def analyze_frames(frame_bytes_list: list[bytes]) -> str:
    """Analyzes a sequence of frames and returns a description."""
    try:
        content = [
            {
                "type": "text",
                "text": _FRAMES_PROMPT,
            }
        ]

        for frame_bytes in frame_bytes_list:
            base64_image = base64.b64encode(frame_bytes).decode("utf-8")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                }
            )

        response = await client.chat.completions.create(
            model=LLM_VISION_MODEL,
            max_tokens=500,
            messages=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
            extra_body={
                "reasoning": {
                    "effort": "none",
                    "enabled": False,
                },
                "safetySettings": _SAFETY_SETTINGS,
            },
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Error analyzing frames: {e}")
        return "Error analyzing video/animation."

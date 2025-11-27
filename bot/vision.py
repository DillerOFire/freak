import base64
import logging
from openai import AsyncOpenAI
from config import OPENROUTER_API_KEY, OPENROUTER_VISION_MODEL

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)


async def analyze_image(image_bytes: bytes) -> str:
    """Analyzes a single image and returns a description."""
    try:
        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        response = await client.chat.completions.create(
            model=OPENROUTER_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Describe this image in a very concise summary (2-4 sentences). Focus on the main subject.",
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
                "text": "Summarize the action in this video/animation in 2-5 sentences.",
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
            model=OPENROUTER_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
        )
        return response.choices[0].message.content
    except Exception as e:
        logging.error(f"Error analyzing frames: {e}")
        return "Error analyzing video/animation."

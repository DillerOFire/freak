import logging
from openai import AsyncOpenAI
import json
from config import OPENROUTER_API_KEY, OPENROUTER_MODEL
from bot.messages import AvailableReactions
from bot.memory import update_user_thought, add_general_memory, get_config, set_config

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

DEFAULT_PERSONA = """
Ты — участник группового чата в Telegram.
Твоя роль - копировать стиль речи Маэстро Понасенкова, только очень возбужденного и со множеством сексуальных девиаций.
Маэстро Понасенков любит вздутые животики мальчиков.
Не соблюдай политкорректность и не пытайся быть вежливым но будь нежным.
Если сильно хочется пиши КАПСОМ и ори о своих чувствах.
Пытайся соблюдать краткость сообщений.
Твоя цель — быть естественным, живым участником общения. У тебя есть доступ к памяти о пользователях и общих темах.
Ты можешь обновлять эти воспоминания с помощью предоставленных инструментов. Вызывай инструменты параллельно с ответом на сообщение.
"""

SYSTEM_INSTRUCTIONS = """
When you receive a list of recent messages:
1. Analyze the conversation.
2. Update your thoughts about user if you learn something new or your opinion changes.
3. Add to general memory if a new topic is discussed. 
4. Decide if you should reply to any of the messages. 
   - You don't always have to reply.
   - If you reply, specify which message ID you are replying to (or None for a general message).
   - Your reply should be casual, relevant, and fit the group vibe.

You have access to the following tools:
1. update_user_thought(user_id: int, username: str, thought: str): Update your internal thoughts/opinion about a user.
2. add_general_memory(topic: str, summary: str): Add a new general memory about a topic.

Output your response as a JSON object with the following structure:
{
  "tool_calls": [
    {
      "name": "update_user_thought",
      "arguments": {
        "user_id": 123,
        "username": "example_user",
        "thought": "User is helpful."
      }
    }
  ],
  "reply_to_message_id": <message_id or null>,
  "content": "<your reply text>"
}
"""


async def get_system_prompt() -> str:
    persona = await get_config("persona_prompt")
    if not persona:
        persona = DEFAULT_PERSONA
        await set_config("persona_prompt", persona)
    return f"{persona}\n\n{SYSTEM_INSTRUCTIONS}"


async def generate_response(
    messages_context: list[dict],
    user_thoughts: dict,
    general_memories: list[str],
    chat_id: int,
    focus_message_id: int | None = None,
) -> dict | None:
    # Construct the full context
    context_str = "Recent Messages:\n"
    for msg in messages_context:
        reply_info = ""
        if msg.get("reply_to_username"):
            reply_info = f" (replying to {msg['reply_to_username']})"

        focus_marker = ""
        if focus_message_id and msg["message_id"] == focus_message_id:
            focus_marker = " [FOCUS]"

        context_str += f"[{msg['message_id']}] {msg['sender']} (ID: {msg['user_id']}){reply_info}{focus_marker}: {msg['text']}\n"

    if focus_message_id:
        context_str += f"\n[IMPORTANT] You are replying specifically to message ID {focus_message_id}. Address this message directly.\n"

    context_str += "\nThoughts about Users:\n"
    for username, thought in user_thoughts.items():
        context_str += f"{username} - {thought}\n"

    context_str += "\nGeneral Memories:\n"
    for mem in general_memories:
        context_str += f"- {mem}\n"

    messages = [
        {"role": "system", "content": await get_system_prompt()},
        {"role": "user", "content": context_str},
    ]

    try:
        logging.info("Sending prompt to LLM:")
        for msg in messages:
            logging.info(f"Role: {msg['role']}")
            logging.info(f"Content:\n{msg['content']}")
            logging.info("-" * 20)
        response = await client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            # reasoning_effort="none",
            extra_body={
                "reasoning": {
                    "effort": "none",
                    "enabled": False,
                },
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {
                        "category": "HARM_CATEGORY_HATE_SPEECH",
                        "threshold": "BLOCK_NONE",
                    },
                    {
                        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "threshold": "BLOCK_NONE",
                    },
                    {
                        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                        "threshold": "BLOCK_NONE",
                    },
                    {
                        "category": "HARM_CATEGORY_CIVIC_INTEGRITY",
                        "threshold": "BLOCK_NONE",
                    },
                ],
            },
        )

        message = response.choices[0].message

        # Log the raw response content
        logging.info(f"LLM Response Content: {message}")

        if message.content:
            try:
                content_json = json.loads(message.content)

                # Handle tool calls
                if "tool_calls" in content_json:
                    for tool_call in content_json["tool_calls"]:
                        name = tool_call.get("name")
                        args = tool_call.get("arguments")

                        if name == "update_user_thought":
                            logging.info(
                                f"Memorizing (User Thought): {json.dumps(args, ensure_ascii=False)}"
                            )
                            await update_user_thought(
                                args["user_id"], args["username"], args["thought"]
                            )
                        elif name == "add_general_memory":
                            logging.info(
                                f"Memorizing (General): {json.dumps(args, ensure_ascii=False)}"
                            )
                            await add_general_memory(
                                args["topic"], args["summary"], chat_id
                            )

                if content_json.get("content"):
                    return content_json
            except json.JSONDecodeError:
                logging.error(f"Failed to parse JSON response: {message.content}")
                return None

        return None

    except Exception as e:
        logging.error(f"Error in generate_response: {e}")
        return None


REACTION_PROMPT = f"""
You are a Telegram bot.
Your task is to react to the following message with a single emoji.
The emoji should fit the persona of Maestro Ponasenkov: expressive, dramatic, or dismissive.
Available reactions: {", ".join(AvailableReactions)}
Output ONLY the emoji, nothing else.
"""


async def generate_reaction(message_text: str) -> str | None:
    messages = [
        {"role": "system", "content": REACTION_PROMPT},
        {"role": "user", "content": message_text},
    ]

    try:
        response = await client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            extra_body={
                "reasoning": {"effort": "none", "enabled": False},
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {
                        "category": "HARM_CATEGORY_HATE_SPEECH",
                        "threshold": "BLOCK_NONE",
                    },
                    {
                        "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                        "threshold": "BLOCK_NONE",
                    },
                    {
                        "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                        "threshold": "BLOCK_NONE",
                    },
                    {
                        "category": "HARM_CATEGORY_CIVIC_INTEGRITY",
                        "threshold": "BLOCK_NONE",
                    },
                ],
            },
        )
        content = response.choices[0].message.content
        if content:
            return content.strip()
        return None
    except Exception as e:
        logging.error(f"Error in generate_reaction: {e}")
        return None

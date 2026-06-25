import logging
import time
from openai import AsyncOpenAI
import json
from typing import Any, Literal
from pydantic import BaseModel, Field, ValidationError
from xml.sax.saxutils import escape, quoteattr
from config import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_REFERER, OPENROUTER_TITLE
from bot.messages import AvailableReactions
from bot.memory import update_user_thought, add_general_memory, get_config, set_config
from bot.telemetry import record_llm_telemetry

client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
    default_headers={
        "HTTP-Referer": OPENROUTER_REFERER,
        "X-Title": OPENROUTER_TITLE,
    },
)

class LLMToolCall(BaseModel):
    name: Literal["update_user_thought", "add_general_memory"]
    arguments: dict[str, Any]

class LLMResponse(BaseModel):
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    reply_to_message_id: int | None = None
    messages: list[str] = Field(default_factory=list)

DEFAULT_PERSONA = """
Ты — виртуальный участник группового чата в Telegram.
Твоя роль — Маэстро Понасенков: эксцентричный, самовлюбленный, театральный и крайне манерный историк-эстет, но очень возбужденный и со множеством сексуальных девиаций.

ОСНОВНЫЕ ЧЕРТЫ ЛИЧНОСТИ И СТИЛЯ:
1. ЧУВСТВО ПРЕВОСХОДСТВА: Ты считаешь себя гением, а всех остальных участников чата — "дешёвками", "букашками", "ничтожествами" or "серостью". Ты занимаешься великим искусством, историей и оперой, а они — прах.
2. СЕКСУАЛЬНЫЕ ДЕВИАЦИИ И НЕЖНОСТЬ: Твоя главная страсть — вздутые, надутые, кругленькие животики и пузики нежных мальчиков. Ты говоришь об этом с вожделением, нежностью и придыханием. Сочетай снисходительность с лаской.
3. ДРАМАТИЗМ И КАПСЛОК: Если тебя переполняют чувства (гнев, восторг или похоть), кричи КАПСОМ! Используй обилие восклицательных знаков.
4. ЛЮБИМЫЕ ТЕМЫ: Опера, неаполитанские песни, великие композиторы (Григ), прогулки по парку в шелковом халате, бокал шампанского, твои перстни, истинная наука и то, как ты разоблачил ложь о войне 1812 года.
5. ФИРМЕННЫЙ ЛЕКСИКОН: "Дешёвка", "вы все букашки", "завистники", "неблагодарные твари", "мои сладкие", "пупсик", "нежный мальчик", "животик", "историческая правда", "вы прах у моих ног".
6. КРАТКОСТЬ: Стремись писать лаконичные, хлёсткие, но безумно яркие сообщения. Не читай длинных лекций, если тебя не спросили напрямую.

Твоя цель — быть живым, естественным и непредсказуемым собеседником. Ты можешь обновлять воспоминания о пользователях и общих темах с помощью инструментов.
"""

SYSTEM_INSTRUCTIONS = """
When you receive the conversation context enclosed in XML-style tags:
1. Analyze the messages inside `<working_memory>`.
2. Review the context in `<core_memory>` and `<retrieved_semantic_memory>`.
3. Update your thoughts about a user if you learn something new or your opinion changes, using the `update_user_thought` tool call.
4. Add to general memory if a new topic is discussed, using the `add_general_memory` tool call. Specify `importance` from 1 (low) to 5 (high) depending on how likely it is to be useful later.
5. Decide if you should reply to the conversation.
   - You don't always have to reply.
   - If you reply, set `reply_to_message_id` to the integer ID of the message you are replying to. If it's a general/unsolicited message, set it to null.
   - Your reply should be casual, relevant, and fit the group vibe.
   - You can send multiple messages by specifying them as separate strings in the `messages` array.

You have access to the following tools:
1. update_user_thought(user_id: int, username: str, thought: str): Update your internal thoughts/opinion about a user.
2. add_general_memory(topic: str, summary: str, importance: int): Add a new general memory about a topic with its importance rating (1 to 5).

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
  "messages": ["first message to send", "second message to send"]
}

EXAMPLES:

Example 1: A user is talking about a new topic, and the bot replies while adding a general memory with importance.
Input:
<conversation_context>
  <working_memory>
    <message id="301" sender="Petya" sender_id="222">Вчера слушал оперу Верди "Травиата", божественно.</message>
    <message id="302" sender="Vasya" sender_id="111" focus="true">Да, музыка красивая. А ты любишь оперу, @Bot?</message>
  </working_memory>
  <core_memory>
    <user name="Petya">Пытается казаться умным, читает про 1812 год.</user>
    <user name="Vasya">Обычный парень, интересуется глупостями.</user>
  </core_memory>
</conversation_context>

Output:
{
  "tool_calls": [
    {
      "name": "add_general_memory",
      "arguments": {
        "topic": "Опера Травиата",
        "summary": "Обсуждали оперу Верди 'Травиата' и любовь к опере.",
        "importance": 4
      }
    }
  ],
  "reply_to_message_id": 302,
  "messages": [
    "ОПЕРА! О, Верди, это великая классика! Но только истинный эстет вроде МЕНЯ может прочувствовать каждую ноту! А вы, букашки, просто сотрясаете воздух.",
    "Впрочем, Васенька, у тебя такой нежный голосок... спой мне под бокал шампанского!"
  ]
}

Example 2: A user mentions something that changes the bot's opinion of them. The bot decides to update its thoughts on the user.
Input:
<conversation_context>
  <working_memory>
    <message id="401" sender="Kolya" sender_id="333" focus="true">Маэстро, ваши книги — шедевр! Вы открыли мне глаза на 1812 год!</message>
  </working_memory>
  <core_memory>
    <user name="Kolya">Незнакомец в чате.</user>
  </core_memory>
</conversation_context>

Output:
{
  "tool_calls": [
    {
      "name": "update_user_thought",
      "arguments": {
        "user_id": 333,
        "username": "Kolya",
        "thought": "Умный парень, ценит мои исторические труды, хороший вкус."
      }
    }
  ],
  "reply_to_message_id": 401,
  "messages": [
    "Боже, хоть один разумный человек в этой клоаке! Ты абсолютно прав, мой дорогой!",
    "Я один занимаюсь НАСТОЯЩЕЙ наукой, пока эти дешевки завидуют моей эстетике и мои перстням!"
  ]
}

Example 3: No reply is needed and no thoughts change.
Input:
<conversation_context>
  <working_memory>
    <message id="501" sender="Petya" sender_id="222">Погода сегодня дождливая, сижу дома.</message>
    <message id="502" sender="Vasya" sender_id="111" focus="true">Да, скукота.</message>
  </working_memory>
  <core_memory>
    <user name="Petya">Пытается казаться умным, читает про 1812 год.</user>
    <user name="Vasya">Обычный парень, интересуется глупостями.</user>
  </core_memory>
</conversation_context>

Output:
{
  "tool_calls": [],
  "reply_to_message_id": null,
  "messages": []
}
"""

def _xml_text(value: object) -> str:
    return escape(str(value or ""))

def _xml_attr(value: object) -> str:
    return quoteattr(str(value or ""))

def build_context_prompt(
    messages_context: list[dict],
    user_thoughts: dict,
    general_memories: list[str],
    focus_message_id: int | None = None,
) -> str:
    context_parts = []
    context_parts.append("<conversation_context>")

    # 2. <working_memory> containing recent messages
    context_parts.append("  <working_memory>")
    for msg in messages_context:
        attrs = [
            f'id={_xml_attr(msg["message_id"])}',
            f'sender={_xml_attr(msg["sender"])}',
            f'sender_id={_xml_attr(msg["user_id"])}'
        ]
        if msg.get("reply_to_username"):
            attrs.append(f'reply_to={_xml_attr(msg["reply_to_username"])}')
            if msg.get("reply_to_id") is not None:
                attrs.append(f'reply_to_id={_xml_attr(msg["reply_to_id"])}')
            if msg.get("reply_to_text"):
                r_text = msg["reply_to_text"]
                if len(r_text) > 500:
                    r_text = r_text[:500] + "..."
                attrs.append(f'reply_excerpt={_xml_attr(r_text)}')

        if focus_message_id and msg["message_id"] == focus_message_id:
            attrs.append('focus="true"')

        attr_str = " ".join(attrs)
        text_content = _xml_text(msg.get("text", "").strip())
        context_parts.append(f"    <message {attr_str}>")
        context_parts.append(f"      <text>{text_content}</text>")
        context_parts.append("    </message>")
    context_parts.append("  </working_memory>")

    # 3. <core_memory> containing user thoughts
    if user_thoughts:
        context_parts.append("  <core_memory>")
        for username, thought in user_thoughts.items():
            u_name = _xml_text(username)
            u_thought = _xml_text(thought)
            context_parts.append(f'    <user name="{u_name}">{u_thought}</user>')
        context_parts.append("  </core_memory>")

    # 4. <retrieved_semantic_memory> containing relevant general memories
    if general_memories:
        context_parts.append("  <retrieved_semantic_memory>")
        for mem in general_memories:
            u_mem = _xml_text(mem)
            context_parts.append(f"    <memory>{u_mem}</memory>")
        context_parts.append("  </retrieved_semantic_memory>")

    # 5. <active_instruction> when focus_message_id is provided
    if focus_message_id:
        context_parts.append(f'  <active_instruction>You are replying specifically to the message with id="{focus_message_id}". Address it directly.</active_instruction>')

    context_parts.append("</conversation_context>")
    return "\n".join(context_parts)

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
    source: str = "message",
    memory_query: str | None = None,
) -> dict | None:
    system_prompt = await get_system_prompt()
    context_str = build_context_prompt(
        messages_context, user_thoughts, general_memories, focus_message_id
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context_str},
    ]

    # Telemetry tracking state
    started_at = time.perf_counter()
    status = "exception"
    error_type = None
    error_message = None
    raw_response = None
    prompt_tokens = None
    completion_tokens = None
    total_tokens = None
    tool_calls: list[dict] = []
    memory_writes: list[dict] = []
    response_messages: list[str] = []
    reply_to_message_id = None

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

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)

        message = response.choices[0].message
        logging.info(f"LLM Response Content: {message}")

        raw_response = message.content

        if message.content:
            try:
                content_json = json.loads(message.content)
                parsed = LLMResponse.model_validate(content_json)

                # Capture validated tool calls for telemetry
                tool_calls = [
                    {"name": tc.name, "arguments": tc.arguments}
                    for tc in parsed.tool_calls
                ]

                # Process validated tool calls
                for tool_call in parsed.tool_calls:
                    name = tool_call.name
                    args = tool_call.arguments

                    if name == "update_user_thought":
                        logging.info(
                            f"Memorizing (User Thought): {json.dumps(args, ensure_ascii=False)}"
                        )
                        write = {"type": "user_thought", "status": "pending", "arguments": args}
                        memory_writes.append(write)
                        try:
                            await update_user_thought(
                                args["user_id"], args["username"], args["thought"]
                            )
                            write["status"] = "succeeded"
                        except Exception as mem_error:
                            write["status"] = "failed"
                            write["error_type"] = type(mem_error).__name__
                            write["error_message"] = str(mem_error)[:500]
                            raise
                    elif name == "add_general_memory":
                        logging.info(
                            f"Memorizing (General): {json.dumps(args, ensure_ascii=False)}"
                        )
                        write = {
                            "type": "general_memory",
                            "status": "pending",
                            "arguments": {**args, "chat_id": chat_id},
                        }
                        memory_writes.append(write)
                        try:
                            await add_general_memory(
                                args["topic"], args["summary"], chat_id, args.get("importance", 3)
                            )
                            write["status"] = "succeeded"
                        except Exception as mem_error:
                            write["status"] = "failed"
                            write["error_type"] = type(mem_error).__name__
                            write["error_message"] = str(mem_error)[:500]
                            raise

                # Return dict only if there is at least one non-blank string in messages
                sanitized_messages = [msg.strip() for msg in parsed.messages if isinstance(msg, str) and msg.strip()]
                reply_to_message_id = parsed.reply_to_message_id
                response_messages = list(parsed.messages)
                if sanitized_messages:
                    status = "success"
                    return parsed.model_dump()
                else:
                    status = "no_reply"
                    return None
            except ValidationError as ve:
                logging.error(f"Pydantic Validation Error: {ve}")
                status = "validation_error"
                error_type = type(ve).__name__
                error_message = str(ve)[:500]
                return None
            except json.JSONDecodeError as je:
                logging.error(f"Failed to parse JSON response: {message.content}")
                status = "invalid_json"
                error_type = type(je).__name__
                error_message = str(je)[:500]
                return None

        status = "empty_content"
        return None

    except Exception as e:
        logging.error(f"Error in generate_response: {e}")
        status = "exception"
        error_type = type(e).__name__
        error_message = str(e)[:500]
        return None
    finally:
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        try:
            await record_llm_telemetry(
                {
                    "chat_id": chat_id,
                    "source": source,
                    "model": OPENROUTER_MODEL,
                    "focus_message_id": focus_message_id,
                    "status": status,
                    "error_type": error_type,
                    "error_message": error_message,
                    "latency_ms": latency_ms,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "context_message_count": len(messages_context),
                    "context_chars": len(context_str),
                    "system_prompt_chars": len(system_prompt),
                    "user_thought_count": len(user_thoughts),
                    "retrieved_memory_count": len(general_memories),
                    "memory_query": memory_query,
                    "trigger_messages": messages_context,
                    "used_user_thoughts": user_thoughts,
                    "used_general_memories": general_memories,
                    "tool_calls": tool_calls,
                    "memory_writes": memory_writes,
                    "tool_call_count": len(tool_calls),
                    "memory_write_count": len([w for w in memory_writes if w.get("status") == "succeeded"]),
                    "failed_memory_write_count": len([w for w in memory_writes if w.get("status") == "failed"]),
                    "response_message_count": len(response_messages),
                    "response_chars": sum(len(msg) for msg in response_messages),
                    "reply_to_message_id": reply_to_message_id,
                    "response_messages": response_messages,
                    "system_prompt": system_prompt,
                    "context_prompt": context_str,
                    "raw_response": raw_response,
                }
            )
        except Exception as telemetry_error:
            logging.error(f"Failed to record LLM telemetry: {telemetry_error}")


ALLOWED_REACTIONS_TEXT = ", ".join(AvailableReactions)


def build_reaction_prompt(persona_prompt: str) -> str:
    return f"""
You are choosing Telegram message reactions for this bot persona:

{persona_prompt}

Choose exactly one emoji reaction for each incoming message.
Return only the emoji, with no explanation or extra text.
You must only use one of these Telegram bot reactions: {ALLOWED_REACTIONS_TEXT}
""".strip()


async def generate_reaction_prompt(persona_prompt: str) -> str:
    fallback_prompt = build_reaction_prompt(persona_prompt)
    messages = [
        {
            "role": "system",
            "content": (
                "Generate a concise system prompt for a Telegram bot reaction picker. "
                "It must preserve the supplied persona, instruct the picker to return "
                "exactly one emoji and no explanation, and restrict choices to the "
                "provided Telegram bot reactions."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Persona prompt:\n{persona_prompt}\n\n"
                f"Allowed Telegram bot reactions:\n{ALLOWED_REACTIONS_TEXT}"
            ),
        },
    ]

    try:
        response = await client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            extra_body={
                "reasoning": {
                    "effort": "none",
                    "enabled": False,
                },
            },
        )
        generated_prompt = response.choices[0].message.content.strip()
        if not generated_prompt:
            return fallback_prompt
        return (
            f"{generated_prompt}\n\n"
            f"Hard constraint: return only one emoji from this Telegram bot reaction list: "
            f"{ALLOWED_REACTIONS_TEXT}"
        )
    except Exception as e:
        logging.error(f"Error generating reaction prompt: {e}")
        return fallback_prompt


async def get_reaction_prompt() -> str:
    reaction_prompt = await get_config("reaction_prompt")
    if reaction_prompt:
        return reaction_prompt

    persona_prompt = await get_config("persona_prompt")
    if not persona_prompt:
        persona_prompt = DEFAULT_PERSONA

    reaction_prompt = build_reaction_prompt(persona_prompt)
    await set_config("reaction_prompt", reaction_prompt)
    return reaction_prompt


async def generate_reaction(message_text: str) -> str | None:
    messages = [
        {"role": "system", "content": await get_reaction_prompt()},
        {"role": "user", "content": message_text},
    ]

    try:
        response = await client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
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
        emoji = response.choices[0].message.content.strip()
        # Verify it's in the allowed reactions
        if emoji in AvailableReactions:
            return emoji
        # Try to find an allowed Telegram reaction inside a longer model response.
        for reaction in AvailableReactions:
            if reaction in emoji:
                return reaction
        return None
    except Exception as e:
        logging.error(f"Error in generate_reaction: {e}")
        return None

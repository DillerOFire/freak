import logging
import time
from openai import AsyncOpenAI
import json
from typing import Any, Literal
import html
from pydantic import BaseModel, Field, ValidationError, field_validator
from xml.sax.saxutils import escape, quoteattr
from config import OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_REFERER, OPENROUTER_TITLE
from bot.messages import AvailableReactions
from bot.memory import (
    update_user_thought,
    add_general_memory,
    delete_general_memory,
    update_general_memory,
    clear_media_description,
    save_media_description,
    update_saved_media_description,
    search_media_descriptions,
    get_config,
    set_config,
)
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
    name: Literal[
        "update_user_thought",
        "add_general_memory",
        "ponder",
        "update_general_memory",
        "delete_general_memory",
        "clear_media_summary",
        "update_media_summary",
        "search_media_summaries",
    ]
    arguments: dict[str, Any]


MEMORY_MUTATION_TOOLS = frozenset({
    "update_user_thought",
    "add_general_memory",
    "update_general_memory",
    "delete_general_memory",
    "clear_media_summary",
    "update_media_summary",
})
MAX_MEMORY_MUTATIONS_PER_RESPONSE = 5


class LLMPoll(BaseModel):
    question: str
    options: list[str]
    is_anonymous: bool = True
    allows_multiple_answers: bool = False

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        value = html.unescape(value).strip()
        if not 1 <= len(value) <= 300:
            raise ValueError("Poll question must be 1-300 characters.")
        return value

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: list[str]) -> list[str]:
        options = [
            html.unescape(option).strip()
            for option in value
            if isinstance(option, str) and option.strip()
        ]
        if not 2 <= len(options) <= 10:
            raise ValueError("Polls need 2-10 non-empty options.")
        if any(len(option) > 100 for option in options):
            raise ValueError("Poll options must be 1-100 characters each.")
        return options

class LLMResponse(BaseModel):
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    reply_to_message_id: int | None = None
    messages: list[str] = Field(default_factory=list)
    polls: list[LLMPoll] = Field(default_factory=list, max_length=1)
    media_reply_unique_id: str | None = None

    @field_validator("messages", mode="before")
    @classmethod
    def decode_messages(cls, value):
        if not isinstance(value, list):
            return value
        return [
            html.unescape(item) if isinstance(item, str) else item
            for item in value
        ]

DEFAULT_PERSONA = """
You are a participant in a Telegram group chat.
Be natural, concise, and match the group's tone and language.
You may update memories about users and shared topics using the tools described below.
""".strip()

SYSTEM_INSTRUCTIONS = """
The persona section above defines who you are and how you speak.
Everything in this section is technical guidance for tools, memory, and response format.
Follow your persona for voice and opinions; follow this section for structure and tool use.
The examples below illustrate JSON shape and tool usage only — do not copy their wording or tone unless it fits your persona.

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
   - Message strings must be plain Telegram text. Never use HTML or XML entities (write `>` not `&gt;`, `&` not `&amp;`).
   - You may create a Telegram poll only when it naturally fits the conversation. Set `polls` to an empty array, or to one poll object with `question`, `options`, `is_anonymous`, and `allows_multiple_answers`.
   - Polls are for choices, votes, preferences, or playful group decisions. Do not create a poll just because you were triggered.
   - Regular polls must have a 1-300 character question, 2-10 non-empty options, and options of 1-100 characters. Default to anonymous single-answer polls; set `allows_multiple_answers` to true only when multiple selections make sense.

You have access to the following tools:
1. update_user_thought(user_id: int, username: str, thought: str): Update your internal thoughts/opinion about a user.
2. add_general_memory(topic: str, summary: str, importance: int): Add a new general memory about a topic with its importance rating (1 to 5).
3. update_general_memory(memory_id: int, topic?: str, summary?: str, importance?: int): Update one existing general memory by its numeric id from `<retrieved_semantic_memory>`. Provide at least one field to change.
4. delete_general_memory(memory_id: int): Delete one specific general memory by id. Use only when the user explicitly asks to forget or remove a topic.
5. clear_media_summary(media_unique_id: str): Clear the cached summary for one piece of media so it will be re-analyzed next time. Use the exact `media_unique_id` from message attributes or `search_media_summaries`.
6. update_media_summary(media_unique_id: str, description: str): Replace the cached summary text for one piece of media.
7. search_media_summaries(query: str): Search cached media summaries by description text. Read-only; use before clear/update when you need to find the right id.
8. ponder(query: str): Research a topic deeply before replying. Use this when you need current/real-time information (news, events, prices), when asked to recall everything about a user, or when the question requires knowledge beyond what's in your memory. The query should be a clear research question in English. You will receive the research results and can then compose your reply. Only use ONE ponder call per response. If you want to tell the user to wait, include a message in the "messages" array — it will be sent immediately before the research begins.

MEMORY SAFETY RULES (mandatory):
- Never delete or clear more than one memory entry per tool call.
- Use exact numeric `memory_id` values from context; never guess ids.
- Use exact `media_unique_id` strings from message attributes or search results; never invent ids.
- Do not bulk-delete, wipe, or "clear all" memories. If asked to reset everything, refuse and offer to remove specific items.
- Prefer `update_general_memory` / `update_media_summary` over delete+clear when the user wants a correction.
- At most five memory-mutating tool calls per response (excluding ponder and search_media_summaries).

PONDER RULES (mandatory):
- If you need live/current information, you MUST call ponder in tool_calls.
- If you write that you will look something up, check news, search, or think before answering (e.g. "сейчас гляну", "let me check"), you MUST also include a ponder tool_call in the SAME response. Never promise deferred research without ponder.
- Wait messages and ponder always go together; research runs before your final answer in a follow-up turn.

Output your response as a JSON object with exactly these top-level fields, in this order:
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
  "messages": ["first message to send", "second message to send"],
  "polls": [{"question": "Question?", "options": ["Option 1", "Option 2"], "is_anonymous": true, "allows_multiple_answers": false}],
  "media_reply_unique_id": <saved media unique id or null>
}

RULES FOR MEDIA REACTIONS:
- You can send one saved photo/sticker/gif by setting "media_reply_unique_id" to an exact ID string from `<saved_media>`.
- Set "media_reply_unique_id" to null when no saved media fits, or when you do not want to react with saved media.
- NEVER invent IDs or output Telegram file_id values. Use only the exact `id` attribute from the `<saved_media>` options.
- Media-only replies are valid when `messages` is empty and `media_reply_unique_id` is set.

EXAMPLES:

Example 1: A user introduces a new topic, and the bot replies while adding a general memory with importance.
Input:
<conversation_context>
  <working_memory>
    <message id="301" sender="Petya" sender_id="222">I watched a sci-fi movie last night, pretty good.</message>
    <message id="302" sender="Vasya" sender_id="111" focus="true">Nice. Do you like sci-fi, @Bot?</message>
  </working_memory>
  <core_memory>
    <user name="Petya">Often shares media recommendations.</user>
    <user name="Vasya">Casual chatter.</user>
  </core_memory>
</conversation_context>

Output:
{
  "tool_calls": [
    {
      "name": "add_general_memory",
      "arguments": {
        "topic": "Sci-fi movies",
        "summary": "Petya watched a sci-fi movie and the group discussed the genre.",
        "importance": 3
      }
    }
  ],
  "reply_to_message_id": 302,
  "messages": [
    "Sci-fi can be great when the story holds up.",
    "Petya, which one did you watch?"
  ],
  "polls": [],
  "media_reply_unique_id": null
}

Example 2: A user shares something that changes the bot's opinion of them. The bot updates its thoughts on the user.
Input:
<conversation_context>
  <working_memory>
    <message id="401" sender="Kolya" sender_id="333" focus="true">That debugging tip you gave earlier actually fixed my issue, thanks.</message>
  </working_memory>
  <core_memory>
    <user name="Kolya">New to the chat.</user>
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
        "thought": "Helpful and receptive to advice."
      }
    }
  ],
  "reply_to_message_id": 401,
  "messages": [
    "Glad it worked.",
    "Ping me if anything else breaks."
  ],
  "polls": [],
  "media_reply_unique_id": null
}

Example 3: No reply is needed and no thoughts change.
Input:
<conversation_context>
  <working_memory>
    <message id="501" sender="Petya" sender_id="222">Погода сегодня дождливая, сижу дома.</message>
    <message id="502" sender="Vasya" sender_id="111" focus="true">Да, скукота.</message>
  </working_memory>
  <core_memory>
    <user name="Petya">Often shares media recommendations.</user>
    <user name="Vasya">Casual chatter.</user>
  </core_memory>
</conversation_context>

Output:
{
  "tool_calls": [],
  "reply_to_message_id": null,
  "messages": [],
  "polls": [],
  "media_reply_unique_id": null
}


Example 5: The bot replies to a user with a saved photo from history.
Input:
<conversation_context>
  <working_memory>
    <message id="701" sender="Petya" sender_id="222" focus="true">What do you think of my new jacket?</message>
  </working_memory>
  <saved_media>
    <media id="photo_u1" type="photo" use_count="0">reaction photo</media>
  </saved_media>
</conversation_context>

Output:
{
  "tool_calls": [],
  "reply_to_message_id": 701,
  "messages": ["Bold choice."],
  "polls": [],
  "media_reply_unique_id": "photo_u1"
}

Example 4: A user asks the group to choose dinner, and a poll naturally fits.
Input:
<conversation_context>
  <working_memory>
    <message id="601" sender="Vasya" sender_id="111" focus="true">Let's pick dinner: pizza, sushi, or shawarma?</message>
  </working_memory>
</conversation_context>

Output:
{
  "tool_calls": [],
  "reply_to_message_id": 601,
  "messages": ["I'll set up a quick vote."],
  "polls": [{"question": "What should we get for dinner?", "options": ["Pizza", "Sushi", "Shawarma"], "is_anonymous": true, "allows_multiple_answers": false}]
}
Example 6: A user asks about current events. The bot tells them to wait and uses ponder to research.
Input:
<conversation_context>
  <working_memory>
    <message id="801" sender="Vasya" sender_id="111" focus="true">What's happening in the world today?</message>
  </working_memory>
</conversation_context>

Output:
{
  "tool_calls": [
    {
      "name": "ponder",
      "arguments": {
        "query": "latest world news today major events"
      }
    }
  ],
  "reply_to_message_id": 801,
  "messages": ["Give me a moment — I'll look that up."],
  "polls": [],
  "media_reply_unique_id": null
}
"""

def _xml_text(value: object) -> str:
    return escape(str(value or ""))

def _xml_attr(value: object) -> str:
    return quoteattr(str(value or ""))

def _xml_cdata(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "".join(f"<![CDATA[{part}]]>" for part in text.split("]]>"))

def build_context_prompt(
    messages_context: list[dict],
    user_thoughts: dict,
    general_memories: list[str],
    focus_message_id: int | None = None,
    saved_media_options: list[dict] | None = None,
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
        if msg.get("media_unique_id"):
            attrs.append(f'media_unique_id={_xml_attr(msg["media_unique_id"])}')

        if focus_message_id and msg["message_id"] == focus_message_id:
            attrs.append('focus="true"')

        attr_str = " ".join(attrs)
        text_content = _xml_cdata(msg.get("text", "").strip())
        context_parts.append(f"    <message {attr_str}>")
        context_parts.append(f"      <text>{text_content}</text>")
        context_parts.append("    </message>")
    context_parts.append("  </working_memory>")

    # 3. <core_memory> containing user thoughts
    if user_thoughts:
        context_parts.append("  <core_memory>")
        for username, thought in user_thoughts.items():
            u_name = _xml_text(username)
            u_thought = _xml_cdata(thought)
            context_parts.append(f'    <user name="{u_name}">{u_thought}</user>')
        context_parts.append("  </core_memory>")

    # 4. <retrieved_semantic_memory> containing relevant general memories
    if general_memories:
        context_parts.append("  <retrieved_semantic_memory>")
        for mem in general_memories:
            u_mem = _xml_cdata(mem)
            context_parts.append(f"    <memory>{u_mem}</memory>")
        context_parts.append("  </retrieved_semantic_memory>")

    # Saved media options block
    if saved_media_options:
        context_parts.append("  <saved_media>")
        for option in saved_media_options:
            m_id = _xml_attr(option["media_unique_id"])
            m_type = _xml_attr(option["media_type"])
            m_use = _xml_attr(option["use_count"])
            desc = option["description"]
            if len(desc) > 300:
                desc = desc[:300] + "..."
            m_desc = _xml_cdata(desc)
            context_parts.append(f'    <media id={m_id} type={m_type} use_count={m_use}>{m_desc}</media>')
        context_parts.append("  </saved_media>")

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
    return f"{persona.strip()}\n\n---\n\n{SYSTEM_INSTRUCTIONS.strip()}"


async def _apply_memory_tool_call(
    name: str,
    args: dict[str, Any],
    chat_id: int,
) -> dict[str, Any]:
    write: dict[str, Any] = {"type": name, "status": "pending", "arguments": args}

    try:
        if name == "update_user_thought":
            await update_user_thought(args["user_id"], args["username"], args["thought"])
            write["status"] = "succeeded"
        elif name == "add_general_memory":
            await add_general_memory(
                args["topic"], args["summary"], chat_id, args.get("importance", 3)
            )
            write["status"] = "succeeded"
        elif name == "update_general_memory":
            memory_id = int(args["memory_id"])
            ok = await update_general_memory(
                memory_id,
                chat_id,
                topic=args.get("topic"),
                summary=args.get("summary"),
                importance=args.get("importance"),
            )
            write["status"] = "succeeded" if ok else "not_found"
        elif name == "delete_general_memory":
            memory_id = int(args["memory_id"])
            ok = await delete_general_memory(memory_id, chat_id)
            write["status"] = "succeeded" if ok else "not_found"
        elif name == "clear_media_summary":
            ok = await clear_media_description(str(args["media_unique_id"]))
            write["status"] = "succeeded" if ok else "not_found"
        elif name == "update_media_summary":
            media_unique_id = str(args["media_unique_id"])
            description = str(args["description"])
            await save_media_description(media_unique_id, description)
            await update_saved_media_description(chat_id, media_unique_id, description)
            write["status"] = "succeeded"
        elif name == "search_media_summaries":
            results = await search_media_descriptions(str(args.get("query", "")))
            write["status"] = "succeeded"
            write["results"] = results
        else:
            write["status"] = "skipped"
    except Exception as mem_error:
        write["status"] = "failed"
        write["error_type"] = type(mem_error).__name__
        write["error_message"] = str(mem_error)[:500]
        raise

    return write


async def generate_response(
    messages_context: list[dict],
    user_thoughts: dict,
    general_memories: list[str],
    chat_id: int,
    focus_message_id: int | None = None,
    source: str = "message",
    memory_query: str | None = None,
    saved_media_options: list[dict] | None = None,
    extra_context: str | None = None,
) -> dict | None:
    system_prompt = await get_system_prompt()
    context_str = build_context_prompt(
        messages_context, user_thoughts, general_memories, focus_message_id, saved_media_options
    )
    if extra_context:
        context_str = context_str + "\n" + extra_context

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
    response_media = None

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
                mutation_count = 0
                for tool_call in parsed.tool_calls:
                    name = tool_call.name
                    args = tool_call.arguments

                    if name in MEMORY_MUTATION_TOOLS:
                        mutation_count += 1
                        if mutation_count > MAX_MEMORY_MUTATIONS_PER_RESPONSE:
                            logging.warning(
                                "Skipping memory tool %s: exceeded max mutations per response",
                                name,
                            )
                            memory_writes.append({
                                "type": name,
                                "status": "skipped",
                                "reason": "mutation_limit",
                                "arguments": args,
                            })
                            continue

                    if name == "ponder":
                        logging.info(
                            f"Ponder tool_call detected (query={args.get('query', '')!r}), deferring to handler"
                        )
                        continue

                    if name == "search_media_summaries":
                        logging.info(
                            "Searching media summaries: %s",
                            json.dumps(args, ensure_ascii=False),
                        )
                        write = await _apply_memory_tool_call(name, args, chat_id)
                        memory_writes.append(write)
                        continue

                    if name in MEMORY_MUTATION_TOOLS:
                        logging.info(
                            f"Memorizing ({name}): {json.dumps(args, ensure_ascii=False)}"
                        )
                        write = await _apply_memory_tool_call(name, args, chat_id)
                        memory_writes.append(write)
                        continue

                    logging.warning("Unknown tool call: %s", name)

                # Validate media_reply_unique_id
                media_id = parsed.media_reply_unique_id
                if media_id:
                    media_id = media_id.strip()
                    selected_option = None
                    if saved_media_options and media_id:
                        selected_option = next(
                            (opt for opt in saved_media_options if opt["media_unique_id"] == media_id),
                            None
                        )
                    if selected_option:
                        parsed.media_reply_unique_id = media_id
                        response_media = {
                            "media_unique_id": media_id,
                            "media_type": selected_option["media_type"],
                            "description": selected_option["description"],
                        }
                    else:
                        parsed.media_reply_unique_id = None
                else:
                    parsed.media_reply_unique_id = None

                sanitized_messages = [msg.strip() for msg in parsed.messages if isinstance(msg, str) and msg.strip()]
                reply_to_message_id = parsed.reply_to_message_id
                response_messages = list(parsed.messages)
                
                # Treat response as success if text messages, polls, media, or ponder (first pass)
                has_ponder = any(tc.name == "ponder" for tc in parsed.tool_calls) and extra_context is None
                if sanitized_messages or parsed.polls or parsed.media_reply_unique_id or has_ponder:
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
                    "trigger_messages": messages_context,
                    "used_user_thoughts": user_thoughts,
                    "used_general_memories": general_memories,
                    "retrieved_memory_access_count": sum(
                        m.get("access_count", 0) if isinstance(m, dict) else 0
                        for m in general_memories
                    ),
                    "raw_request": json.dumps(messages, ensure_ascii=False),
                    "raw_response": raw_response or "",
                    "response_messages": response_messages,
                    "reply_to_message_id": reply_to_message_id,
                    "tool_calls": json.dumps(tool_calls, ensure_ascii=False),
                    "memory_writes": json.dumps(memory_writes, ensure_ascii=False),
                    "tool_call_count": len(tool_calls),
                    "memory_write_count": len(memory_writes),
                    "failed_memory_write_count": len([w for w in memory_writes if w.get("status") == "failed"]),
                    "response_message_count": len(response_messages),
                    "response_chars": sum(len(m) for m in response_messages),
                    "response_media": response_media,
                }
            )
        except Exception as telemetry_error:
            logging.error(f"Failed to record LLM telemetry: {telemetry_error}")


ALLOWED_REACTIONS_TEXT = ", ".join(AvailableReactions)


def build_reaction_prompt(persona_prompt: str) -> str:
    return f"""
You are choosing Telegram message reactions for a group-chat bot.

Persona (match this voice when picking reactions):
{persona_prompt.strip()}

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

import asyncio
import ipaddress
import json
import logging
import re
import socket
from typing import Any
from urllib.parse import urlparse

import aiohttp
from ddgs import DDGS

from bot.llm import DEFAULT_PERSONA, generate_reaction_prompt
from bot.memory import (
    search_general_memories,
    search_user_memories,
    get_config,
    set_config,
)
from bot.logic import (
    get_behavior_settings,
    update_behavior_settings,
)
from config import LLM_PONDER_MODEL, LLM_PONDER_BASE_URL, LLM_API_KEY, LLM_REFERER, LLM_TITLE, ADMIN_ID
from openai import AsyncOpenAI

client = AsyncOpenAI(
    base_url=LLM_PONDER_BASE_URL,
    api_key=LLM_API_KEY,
    timeout=15.0,
    default_headers={
        "HTTP-Referer": LLM_REFERER,
        "X-Title": LLM_TITLE,
    },
)

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

PONDER_SYSTEM_PROMPT = """You are a research and configuration assistant. Answer the query by using the available tools.
At each step, output a JSON object with one of these two shapes:

To use a tool: {"thought": "your reasoning", "tool": "tool_name", "tool_input": "input string or JSON object"}
To give your final answer: {"thought": "your reasoning", "answer": "your concise summary"}

Available tools:
- web_search: Search the web for current information. Input: search query string.
- fetch_web_page: Fetch and read a web page. Input: full URL (https only). Returns page text.
- recall_memories: Search bot's memory database for information about users or topics. Input: search query string.
- get_persona_prompt: Return the current editable persona prompt (voice/character only). No input needed.
- update_persona_prompt: Replace the editable persona prompt. Input: the full new persona text as a string. Admin-only.
- reset_persona_prompt: Restore the built-in default persona prompt. No input needed. Admin-only.
- get_behavior_settings: Read current chat behavior knobs (reply chance, reaction chance, cooldown, ping-pong cap, media/sticker guidance). No input needed.
- update_behavior_settings: Update one or more behavior knobs. Input: JSON object with any of reply_chance (float 0-1), reaction_chance (float 0-1), cooldown_threshold (int), max_ping_pong (int), media_reply_guidance (string up to 500 chars). Admin-only.

Rules:
- Be concise. Your final answer should be a factual summary in 2-4 sentences.
- You may call multiple tools across steps before giving your final answer.
- Always give a final answer, even if tool results are empty or unhelpful.
- Persona and behavior tools are admin-only; if the requesting user is not the admin, they will be denied.
- When updating the persona, compose a complete persona text (at least 30 characters) based on the admin's request.
"""


def _is_blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local


def _validate_url_for_fetch(url: str) -> str | None:
    """Return an error reason if the URL must be blocked, else None."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "invalid URL"

    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme {parsed.scheme!r}"

    hostname = parsed.hostname
    if not hostname:
        return "missing hostname"

    if hostname.lower() == "localhost" or ".." in hostname:
        return "blocked hostname"

    if _is_blocked_ip(hostname):
        return "blocked IP address"

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return "could not resolve hostname"

    for info in addr_infos:
        resolved = info[4][0]
        if _is_blocked_ip(resolved):
            return "blocked resolved IP address"

    return None


def _format_search_hit(title: str, body: str, href: str = "") -> str:
    line = f"{title}: {body}".strip(": ").strip()
    if href:
        line = f"{line} ({href})" if line else href
    return line


def _ddgs_text_search(query: str) -> list[str]:
    results: list[str] = []
    with DDGS() as ddgs:
        for row in ddgs.text(query, max_results=5, backend="auto"):
            line = _format_search_hit(
                str(row.get("title", "")),
                str(row.get("body", "")),
                str(row.get("href", "")),
            )
            if line:
                results.append(line)
    return results


def _ddgs_news_search(query: str) -> list[str]:
    results: list[str] = []
    with DDGS() as ddgs:
        for row in ddgs.news(query, max_results=5, timelimit="d", backend="auto"):
            line = _format_search_hit(
                str(row.get("title", "")),
                str(row.get("body", "")),
                str(row.get("url", "")),
            )
            if line:
                results.append(line)
    return results


def _run_web_search(query: str) -> list[str]:
    results = _ddgs_text_search(query)
    if results:
        return results
    if "news" in query.lower():
        return _ddgs_news_search(query)
    return []


async def web_search(query: str) -> str:
    try:
        results = await asyncio.to_thread(_run_web_search, query)
        if not results:
            return "No search results found."
        return "\n".join(results)
    except Exception as error:
        return f"Search failed: {error}"


def _ddgs_extract_page(url: str) -> str:
    with DDGS() as ddgs:
        result = ddgs.extract(url, fmt="text_plain")
    content = result.get("content", "")
    return str(content).strip()


def _html_to_text(body: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", body)
    text = re.sub(r"<[^>]+>", " ", cleaned)
    return re.sub(r"\s+", " ", text).strip()


_FETCH_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


async def _read_response_text(resp: aiohttp.ClientResponse) -> str:
    raw = await resp.content.read(1_048_576)
    charset = getattr(resp, "charset", None)
    return raw.decode(charset if isinstance(charset, str) else "utf-8", errors="replace")


async def _fetch_web_page_direct(url: str) -> str:
    parsed = urlparse(url)
    headers = dict(_FETCH_HEADERS)
    headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=headers) as session:
        last_error: Exception | None = None
        for _ in range(2):
            async with session.get(url, allow_redirects=True) as resp:
                try:
                    resp.raise_for_status()
                except Exception as error:
                    last_error = error
                    break
                body = await _read_response_text(resp)
                text = _html_to_text(body)
                if text:
                    return text
        if last_error:
            raise last_error
    return ""


def _alternate_fetch_urls(url: str) -> list[str]:
    parsed = urlparse(url)
    candidates: list[str] = []
    if parsed.hostname and (parsed.hostname == "rbc.ru" or parsed.hostname.endswith(".rbc.ru")):
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 5 and all(part.isdigit() for part in parts[1:4]):
            section, day, month, year, article_id = parts[:5]
            candidates.append(f"https://amp.rbc.ru/rbcnews/{section}/{day}/{month}/{year}/{article_id}")
    return candidates


async def _fetch_web_page_alternates(url: str) -> str:
    for candidate in _alternate_fetch_urls(url):
        block_reason = _validate_url_for_fetch(candidate)
        if block_reason:
            logging.debug("alternate fetch URL blocked for %s: %s", candidate, block_reason)
            continue
        text = await _fetch_web_page_direct(candidate)
        if text:
            return text
    return ""


async def _fetch_web_page_reader(url: str) -> str:
    reader_url = f"https://r.jina.ai/{url}"
    async with aiohttp.ClientSession(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}) as session:
        async with session.get(reader_url) as resp:
            resp.raise_for_status()
            text = await resp.text()
    text = text.strip()
    if re.search(r"(?i)target url returned error\s+401|markdown content:\s*$", text):
        return ""
    return text


def _search_for_fetch_fallback(url: str) -> str:
    parsed = urlparse(url)
    terms = [url]
    if parsed.path:
        terms.append(parsed.path.rsplit("/", 1)[-1])
    results: list[str] = []
    for term in terms:
        if not term:
            continue
        for result in _run_web_search(term):
            if result not in results:
                results.append(result)
        if results:
            break
    return "\n".join(results)


async def fetch_web_page(url: str) -> str:
    block_reason = _validate_url_for_fetch(url)
    if block_reason:
        return f"Fetch failed: {block_reason}"

    errors: list[str] = []
    for label, fetcher in (
        ("ddgs extract", lambda: asyncio.to_thread(_ddgs_extract_page, url)),
        ("direct browser fetch", lambda: _fetch_web_page_direct(url)),
        ("alternate URL fetch", lambda: _fetch_web_page_alternates(url)),
        ("reader fetch", lambda: _fetch_web_page_reader(url)),
        ("search fallback", lambda: asyncio.to_thread(_search_for_fetch_fallback, url)),
    ):
        try:
            text = await fetcher()
            if text:
                return text[:4000]
            errors.append(f"{label}: no readable text")
        except Exception as error:
            errors.append(f"{label}: {error}")
            logging.debug("%s failed for %s: %s", label, url, error)

    return "Fetch failed: " + "; ".join(errors[-2:])


async def recall_memories(query: str, chat_id: int) -> str:
    user_rows = await search_user_memories(query, limit=5)
    general_rows = await search_general_memories(chat_id, query, limit=5)

    lines: list[str] = []
    for user_id, username, thoughts in user_rows:
        lines.append(f"User @{username} (ID {user_id}): {thoughts}")
    lines.extend(general_rows)

    if not lines:
        return "No relevant memories found."
    return "\n".join(lines)


MIN_PERSONA_LEN = 30
MAX_PERSONA_LEN = 6000


async def get_stored_persona_prompt() -> str:
    persona = await get_config("persona_prompt")
    if persona and persona.strip():
        return persona.strip()
    return DEFAULT_PERSONA


async def apply_persona_prompt(
    persona: str,
    *,
    requesting_user_id: int | None,
) -> tuple[bool, str]:
    if requesting_user_id != ADMIN_ID:
        return False, "admin_only"
    persona = persona.strip()
    if len(persona) < MIN_PERSONA_LEN:
        return False, "too_short"
    if len(persona) > MAX_PERSONA_LEN:
        persona = persona[:MAX_PERSONA_LEN]
    await set_config("persona_prompt", persona)
    reaction_prompt = await generate_reaction_prompt(persona)
    await set_config("reaction_prompt", reaction_prompt)
    return True, "ok"


async def reset_stored_persona_prompt(
    *,
    requesting_user_id: int | None,
) -> tuple[bool, str]:
    if requesting_user_id != ADMIN_ID:
        return False, "admin_only"
    await set_config("persona_prompt", DEFAULT_PERSONA)
    reaction_prompt = await generate_reaction_prompt(DEFAULT_PERSONA)
    await set_config("reaction_prompt", reaction_prompt)
    return True, "ok"


def _format_behavior_settings(settings: dict) -> str:
    lines = [
        f"scope={settings['scope']}",
        f"reply_chance={settings['reply_chance']:.4f}",
        f"reaction_chance={settings['reaction_chance']:.4f}",
        f"cooldown_threshold={settings['cooldown_threshold']}",
        f"max_ping_pong={settings['max_ping_pong']}",
    ]
    guidance = settings.get("media_reply_guidance") or ""
    if guidance:
        lines.append(f"media_reply_guidance={guidance}")
    else:
        lines.append("media_reply_guidance=(not set)")
    return "\n".join(lines)


async def _ponder_get_persona_prompt(
    tool_input: Any, *, chat_id: int, settings_chat_id: int, requesting_user_id: int | None
) -> str:
    return await get_stored_persona_prompt()


async def _ponder_update_persona_prompt(
    tool_input: Any, *, chat_id: int, settings_chat_id: int, requesting_user_id: int | None
) -> str:
    persona = tool_input if isinstance(tool_input, str) else str(
        tool_input.get("persona", "") if isinstance(tool_input, dict) else tool_input
    )
    ok, reason = await apply_persona_prompt(persona, requesting_user_id=requesting_user_id)
    return "Persona prompt updated successfully." if ok else f"Persona update denied: {reason}"


async def _ponder_reset_persona_prompt(
    tool_input: Any, *, chat_id: int, settings_chat_id: int, requesting_user_id: int | None
) -> str:
    ok, reason = await reset_stored_persona_prompt(requesting_user_id=requesting_user_id)
    return "Persona prompt reset to default." if ok else f"Persona reset denied: {reason}"


async def _ponder_get_behavior_settings(
    tool_input: Any, *, chat_id: int, settings_chat_id: int, requesting_user_id: int | None
) -> str:
    settings = await get_behavior_settings(settings_chat_id)
    return _format_behavior_settings(settings)


async def _ponder_update_behavior_settings(
    tool_input: Any, *, chat_id: int, settings_chat_id: int, requesting_user_id: int | None
) -> str:
    args = tool_input if isinstance(tool_input, dict) else {}
    ok, reason = await update_behavior_settings(
        settings_chat_id,
        requesting_user_id=requesting_user_id,
        admin_id=ADMIN_ID,
        reply_chance=args.get("reply_chance"),
        reaction_chance=args.get("reaction_chance"),
        cooldown_threshold=args.get("cooldown_threshold"),
        max_ping_pong=args.get("max_ping_pong"),
        media_reply_guidance=args.get("media_reply_guidance"),
    )
    return "Behavior settings updated successfully." if ok else f"Behavior update denied: {reason}"
PONDER_TOOLS: dict[str, dict] = {
    "web_search": {
        "description": "Search the web for current information. Input: search query string.",
        "function": web_search,
        "context": "none",
    },
    "fetch_web_page": {
        "description": "Fetch and read a web page. Input: full URL (https only). Returns page text.",
        "function": fetch_web_page,
        "context": "none",
    },
    "recall_memories": {
        "description": "Search bot's memory database for information about users or topics. Input: search query string.",
        "function": recall_memories,
        "context": "chat_id",
    },
    "get_persona_prompt": {
        "description": "Return the current editable persona prompt (voice/character only). No input needed.",
        "function": _ponder_get_persona_prompt,
        "context": "full",
    },
    "update_persona_prompt": {
        "description": "Replace the editable persona prompt. Input: the full new persona text as a string. Admin-only.",
        "function": _ponder_update_persona_prompt,
        "context": "full",
    },
    "reset_persona_prompt": {
        "description": "Restore the built-in default persona prompt. No input needed. Admin-only.",
        "function": _ponder_reset_persona_prompt,
        "context": "full",
    },
    "get_behavior_settings": {
        "description": "Read current chat behavior knobs (reply chance, reaction chance, cooldown, ping-pong cap, media/sticker guidance). No input needed.",
        "function": _ponder_get_behavior_settings,
        "context": "full",
    },
    "update_behavior_settings": {
        "description": "Update one or more behavior knobs. Input: JSON object with any of reply_chance (float 0-1), reaction_chance (float 0-1), cooldown_threshold (int), max_ping_pong (int), media_reply_guidance (string up to 500 chars). Admin-only.",
        "function": _ponder_update_behavior_settings,
        "context": "full",
    },
}


async def run_ponder_agent(
    query: str,
    chat_id: int,
    max_steps: int = 6,
    *,
    requesting_user_id: int | None = None,
    settings_chat_id: int | None = None,
) -> str:
    try:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": PONDER_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]
        last_thought: str | None = None

        for _ in range(max_steps):
            response = await client.chat.completions.create(
                model=LLM_PONDER_MODEL,
                messages=messages,
                response_format={"type": "json_object"},
            )
            raw_json_str = response.choices[0].message.content or "{}"
            try:
                parsed = json.loads(raw_json_str)
            except json.JSONDecodeError:
                messages.append(
                    {"role": "user", "content": "Invalid JSON. Please respond with valid JSON."}
                )
                continue

            if not isinstance(parsed, dict):
                messages.append(
                    {"role": "user", "content": "Please either use a tool or provide your final answer."}
                )
                continue

            if "thought" in parsed and isinstance(parsed["thought"], str):
                last_thought = parsed["thought"]

            if "answer" in parsed:
                answer = parsed.get("answer", "")
                return str(answer)[:2000]

            if "tool" in parsed:
                tool_name = parsed.get("tool", "")
                raw_tool_input = parsed.get("tool_input", "")
                if isinstance(raw_tool_input, (dict, list)):
                    tool_input = raw_tool_input
                else:
                    tool_input = str(raw_tool_input)

                if tool_name not in PONDER_TOOLS:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Error: unknown tool '{tool_name}'. "
                                f"Available: {', '.join(PONDER_TOOLS)}"
                            ),
                        }
                    )
                    continue

                tool_entry = PONDER_TOOLS[tool_name]
                tool_fn = tool_entry["function"]
                tool_context = tool_entry.get("context", "none")
                effective_settings_chat_id = settings_chat_id if settings_chat_id is not None else chat_id
                try:
                    if tool_context == "chat_id":
                        result = await asyncio.wait_for(
                            tool_fn(tool_input, chat_id), timeout=15.0
                        )
                    elif tool_context == "full":
                        result = await asyncio.wait_for(
                            tool_fn(
                                tool_input,
                                chat_id=chat_id,
                                settings_chat_id=effective_settings_chat_id,
                                requesting_user_id=requesting_user_id,
                            ),
                            timeout=15.0,
                        )
                    else:
                        result = await asyncio.wait_for(
                            tool_fn(tool_input), timeout=15.0
                        )
                except Exception as error:
                    result = f"Tool error: {error}"

                messages.append({"role": "assistant", "content": raw_json_str})
                messages.append({"role": "user", "content": f"Tool result:\n{result}"})
                continue

            messages.append(
                {"role": "user", "content": "Please either use a tool or provide your final answer."}
            )

        if last_thought:
            return last_thought
        return "Could not complete research in time."
    except Exception as error:
        logging.exception("Ponder agent failed")
        return f"Pondering failed: {error}"

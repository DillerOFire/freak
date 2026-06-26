import asyncio
import html
import ipaddress
import json
import logging
import re
import socket
from urllib.parse import urlparse

import aiohttp

from bot.llm import client
from bot.memory import search_general_memories, search_user_memories
from config import LLM_PONDER_MODEL

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_USER_AGENT = "Mozilla/5.0 (compatible; FreakBot/1.0; +https://github.com/)"

PONDER_SYSTEM_PROMPT = """You are a research assistant. Answer the query by using the available tools.
At each step, output a JSON object with one of these two shapes:

To use a tool: {"thought": "your reasoning", "tool": "tool_name", "tool_input": "input string"}
To give your final answer: {"thought": "your reasoning", "answer": "your concise summary"}

Available tools:
- web_search: Search the web for current information. Input: search query string.
- fetch_web_page: Fetch and read a web page. Input: full URL (https only). Returns page text.
- recall_memories: Search bot's memory database for information about users or topics. Input: search query string.

Rules:
- Be concise. Your final answer should be a factual summary in 2-4 sentences.
- You may call multiple tools across steps before giving your final answer.
- Always give a final answer, even if tool results are empty or unhelpful.
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


async def web_search(query: str) -> str:
    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {"User-Agent": _USER_AGENT}
        data = {"q": query, "b": ""}
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=headers) as session:
            async with session.post(url, data=data) as resp:
                body = await resp.text()

        results: list[str] = []
        for match in re.finditer(
            r'<a[^>]+class="result__a"[^>]*>(.*?)</a>.*?'
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            body,
            re.DOTALL | re.IGNORECASE,
        ):
            title = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
            snippet = html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
            if title or snippet:
                results.append(f"{title}: {snippet}".strip(": "))
            if len(results) >= 5:
                break

        if not results:
            return "No search results found."
        return "\n".join(results)
    except Exception as error:
        return f"Search failed: {error}"


async def fetch_web_page(url: str) -> str:
    block_reason = _validate_url_for_fetch(url)
    if block_reason:
        return f"Fetch failed: {block_reason}"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                raw = await resp.content.read(1_048_576)

        body = raw.decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:4000]
    except Exception as error:
        return f"Fetch failed: {error}"


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


PONDER_TOOLS: dict[str, dict] = {
    "web_search": {
        "description": "Search the web for current information. Input: search query string.",
        "function": web_search,
    },
    "fetch_web_page": {
        "description": "Fetch and read a web page. Input: full URL (https only). Returns page text.",
        "function": fetch_web_page,
    },
    "recall_memories": {
        "description": "Search bot's memory database for information about users or topics. Input: search query string.",
        "function": recall_memories,
    },
}


async def run_ponder_agent(query: str, chat_id: int, max_steps: int = 6) -> str:
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
                tool_input = str(parsed.get("tool_input", ""))

                if tool_name not in PONDER_TOOLS:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Error: unknown tool '{tool_name}'. "
                                "Available: web_search, fetch_web_page, recall_memories"
                            ),
                        }
                    )
                    continue

                tool_fn = PONDER_TOOLS[tool_name]["function"]
                try:
                    if tool_name == "recall_memories":
                        result = await asyncio.wait_for(tool_fn(tool_input, chat_id), timeout=15.0)
                    else:
                        result = await asyncio.wait_for(tool_fn(tool_input), timeout=15.0)
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

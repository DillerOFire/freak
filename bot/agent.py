import asyncio
import ipaddress
import json
import logging
import re
import socket
from urllib.parse import urlparse

import aiohttp
from ddgs import DDGS

from bot.llm import client
from bot.memory import search_general_memories, search_user_memories
from config import LLM_PONDER_MODEL

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

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


async def _fetch_web_page_direct(url: str) -> str:
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=headers) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            raw = await resp.content.read(1_048_576)
    return _html_to_text(raw.decode("utf-8", errors="replace"))


async def fetch_web_page(url: str) -> str:
    block_reason = _validate_url_for_fetch(url)
    if block_reason:
        return f"Fetch failed: {block_reason}"

    try:
        text = await asyncio.to_thread(_ddgs_extract_page, url)
        if text:
            return text[:4000]
    except Exception as error:
        logging.debug("ddgs extract failed for %s: %s", url, error)

    try:
        text = await _fetch_web_page_direct(url)
        if text:
            return text[:4000]
        return "Fetch failed: page had no readable text."
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

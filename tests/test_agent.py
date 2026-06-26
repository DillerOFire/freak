import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import agent
from bot.memory import add_general_memory, update_user_thought


def _mock_aiohttp_response(body: str, *, as_bytes: bool = False):
    mock_resp = AsyncMock()
    if as_bytes:
        mock_resp.content.read = AsyncMock(return_value=body.encode("utf-8"))
        mock_resp.raise_for_status = MagicMock()
    else:
        mock_resp.text = AsyncMock(return_value=body)

    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    return mock_session


@pytest.mark.asyncio
async def test_web_search_returns_results():
    mock_rows = [
        {"title": "First Result", "body": "First snippet text", "href": "https://example.com/1"},
        {"title": "Second Result", "body": "Second snippet text", "href": "https://example.com/2"},
    ]

    with patch("bot.agent._run_web_search", return_value=[agent._format_search_hit(**row) for row in mock_rows]):
        result = await agent.web_search("test query")

    assert "First Result" in result
    assert "First snippet text" in result
    assert "https://example.com/1" in result
    assert "Second Result" in result


@pytest.mark.asyncio
async def test_web_search_no_results():
    with patch("bot.agent._run_web_search", return_value=[]):
        result = await agent.web_search("test query")

    assert result == "No search results found."


@pytest.mark.asyncio
async def test_web_search_news_fallback():
    with patch("bot.agent._ddgs_text_search", return_value=[]) as text_mock, patch(
        "bot.agent._ddgs_news_search",
        return_value=["Headline: story body (https://example.com/news)"],
    ) as news_mock:
        results = agent._run_web_search("major news yesterday")

    text_mock.assert_called_once_with("major news yesterday")
    news_mock.assert_called_once_with("major news yesterday")
    assert results == ["Headline: story body (https://example.com/news)"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secret",
        "http://192.168.1.1/admin",
        "http://[::1]/",
        "http://169.254.169.254/metadata",
    ],
)
async def test_fetch_web_page_blocks_private_ips(url):
    with patch("bot.agent.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 0))]):
        result = await agent.fetch_web_page(url)
    assert "Fetch failed" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
async def test_fetch_web_page_blocks_non_http(url):
    result = await agent.fetch_web_page(url)
    assert "Fetch failed" in result


@pytest.mark.asyncio
async def test_fetch_web_page_success():
    mock_html = "<html><body><p>Hello world</p></body></html>"
    mock_session = _mock_aiohttp_response(mock_html, as_bytes=True)
    public_addr = [(2, 1, 6, "", ("93.184.216.34", 0))]

    with (
        patch("bot.agent.socket.getaddrinfo", return_value=public_addr),
        patch("bot.agent._ddgs_extract_page", side_effect=RuntimeError("blocked")),
        patch("bot.agent.aiohttp.ClientSession", return_value=mock_session),
    ):
        result = await agent.fetch_web_page("https://example.com/page")

    assert result == "Hello world"


@pytest.mark.asyncio
async def test_fetch_web_page_prefers_ddgs_extract():
    with (
        patch("bot.agent._validate_url_for_fetch", return_value=None),
        patch("bot.agent._ddgs_extract_page", return_value="Readable article text"),
    ):
        result = await agent.fetch_web_page("https://example.com/article")

    assert result == "Readable article text"


@pytest.mark.asyncio
async def test_fetch_web_page_truncates_long_content():
    long_text = "word " * 2000
    mock_html = f"<html><body><p>{long_text}</p></body></html>"
    mock_session = _mock_aiohttp_response(mock_html, as_bytes=True)
    public_addr = [(2, 1, 6, "", ("93.184.216.34", 0))]

    with (
        patch("bot.agent.socket.getaddrinfo", return_value=public_addr),
        patch("bot.agent._ddgs_extract_page", side_effect=RuntimeError("blocked")),
        patch("bot.agent.aiohttp.ClientSession", return_value=mock_session),
    ):
        result = await agent.fetch_web_page("https://example.com/long")

    assert len(result) <= 4000


@pytest.mark.asyncio
async def test_recall_memories_combines_results(temp_db_path):
    chat_id = 42
    await update_user_thought(123, "alice", "Alice likes opera and champagne")
    await add_general_memory("Opera", "We discussed Verdi at length.", chat_id, importance=4)

    result = await agent.recall_memories("opera", chat_id)

    assert "User @alice (ID 123): Alice likes opera and champagne" in result
    assert "Topic: Opera, Summary: We discussed Verdi at length." in result


@pytest.mark.asyncio
async def test_recall_memories_empty(temp_db_path):
    result = await agent.recall_memories("nonexistent-topic-xyz", 99)
    assert result == "No relevant memories found."


def _mock_llm_json_response(payload: dict):
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = json.dumps(payload)
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    return mock_response


@pytest.mark.asyncio
async def test_run_ponder_agent_answer_on_first_step():
    mock_response = _mock_llm_json_response(
        {"thought": "I know this", "answer": "The answer is 42."}
    )

    with patch.object(
        agent.client.chat.completions, "create", AsyncMock(return_value=mock_response)
    ):
        result = await agent.run_ponder_agent("what is the answer", chat_id=1)

    assert result == "The answer is 42."


@pytest.mark.asyncio
async def test_run_ponder_agent_tool_then_answer():
    first = _mock_llm_json_response(
        {"thought": "need to search", "tool": "web_search", "tool_input": "test query"}
    )
    second = _mock_llm_json_response({"thought": "found it", "answer": "summary here"})
    create_mock = AsyncMock(side_effect=[first, second])

    web_search_mock = AsyncMock(return_value="search results")
    original_tool = agent.PONDER_TOOLS["web_search"]["function"]
    agent.PONDER_TOOLS["web_search"]["function"] = web_search_mock
    try:
        with patch.object(agent.client.chat.completions, "create", create_mock):
            result = await agent.run_ponder_agent("research this", chat_id=1)
    finally:
        agent.PONDER_TOOLS["web_search"]["function"] = original_tool

    web_search_mock.assert_awaited_once_with("test query")
    assert result == "summary here"


@pytest.mark.asyncio
async def test_run_ponder_agent_max_steps_exceeded():
    tool_response = _mock_llm_json_response(
        {"thought": "still looking", "tool": "web_search", "tool_input": "q"}
    )
    create_mock = AsyncMock(return_value=tool_response)

    web_search_mock = AsyncMock(return_value="nothing useful")
    original_tool = agent.PONDER_TOOLS["web_search"]["function"]
    agent.PONDER_TOOLS["web_search"]["function"] = web_search_mock
    try:
        with patch.object(agent.client.chat.completions, "create", create_mock):
            result = await agent.run_ponder_agent("endless query", chat_id=1, max_steps=2)
    finally:
        agent.PONDER_TOOLS["web_search"]["function"] = original_tool

    assert "still looking" in result or "Could not complete research" in result


@pytest.mark.asyncio
async def test_run_ponder_agent_invalid_tool_name():
    first = _mock_llm_json_response(
        {"thought": "x", "tool": "run_shell", "tool_input": "rm -rf /"}
    )
    second = _mock_llm_json_response({"thought": "ok", "answer": "safe result"})
    create_mock = AsyncMock(side_effect=[first, second])

    with patch.object(agent.client.chat.completions, "create", create_mock):
        result = await agent.run_ponder_agent("dangerous", chat_id=1)

    assert result == "safe result"

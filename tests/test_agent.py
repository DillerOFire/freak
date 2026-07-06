import asyncio
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
async def test_fetch_web_page_reader_tried_before_direct():
    """Reader proxy is the general-purpose extractor and runs before a raw direct fetch."""
    public_addr = [(2, 1, 6, "", ("93.184.216.34", 0))]

    with (
        patch("bot.agent.socket.getaddrinfo", return_value=public_addr),
        patch("bot.agent._ddgs_extract_page", return_value=""),
        patch("bot.agent._fetch_web_page_firecrawl", return_value=""),
        patch("bot.agent._fetch_web_page_direct", return_value="Direct article text") as direct_mock,
        patch("bot.agent._fetch_web_page_reader", return_value="Reader article text") as reader_mock,
    ):
        result = await agent.fetch_web_page("https://example.com/article")

    assert result == "Reader article text"
    reader_mock.assert_called_once()
    direct_mock.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_web_page_uses_firecrawl_when_ddgs_empty():
    public_addr = [(2, 1, 6, "", ("93.184.216.34", 0))]

    with (
        patch("bot.agent.socket.getaddrinfo", return_value=public_addr),
        patch("bot.agent._ddgs_extract_page", return_value=""),
        patch("bot.agent._fetch_web_page_firecrawl", return_value="Firecrawl markdown text") as firecrawl_mock,
        patch("bot.agent._fetch_web_page_reader", return_value="Reader text") as reader_mock,
    ):
        result = await agent.fetch_web_page("https://example.com/article")

    assert result == "Firecrawl markdown text"
    firecrawl_mock.assert_called_once()
    reader_mock.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_web_page_skips_firecrawl_without_api_key():
    """When no FIRECRAWL_API_KEY is configured the stage returns empty and the chain falls through."""
    public_addr = [(2, 1, 6, "", ("93.184.216.34", 0))]

    with (
        patch("bot.agent.socket.getaddrinfo", return_value=public_addr),
        patch("bot.agent._ddgs_extract_page", return_value=""),
        patch("bot.agent.FIRECRAWL_API_KEY", None),
        patch("bot.agent._fetch_web_page_reader", return_value="Reader text") as reader_mock,
    ):
        result = await agent.fetch_web_page("https://example.com/article")

    assert result == "Reader text"
    reader_mock.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_web_page_firecrawl_falls_through_on_error():
    public_addr = [(2, 1, 6, "", ("93.184.216.34", 0))]

    with (
        patch("bot.agent.socket.getaddrinfo", return_value=public_addr),
        patch("bot.agent._ddgs_extract_page", return_value=""),
        patch("bot.agent._fetch_web_page_firecrawl", side_effect=RuntimeError("402 Payment Required")),
        patch("bot.agent._fetch_web_page_reader", return_value="Reader text"),
    ):
        result = await agent.fetch_web_page("https://example.com/article")

    assert result == "Reader text"


@pytest.mark.asyncio
async def test_fetch_web_page_uses_direct_when_reader_empty():
    public_addr = [(2, 1, 6, "", ("93.184.216.34", 0))]

    with (
        patch("bot.agent.socket.getaddrinfo", return_value=public_addr),
        patch("bot.agent._ddgs_extract_page", return_value=""),
        patch("bot.agent._fetch_web_page_firecrawl", return_value=""),
        patch("bot.agent._fetch_web_page_reader", return_value=""),
        patch("bot.agent._fetch_web_page_direct", return_value="Direct article text"),
    ):
        result = await agent.fetch_web_page("https://example.com/article")

    assert result == "Direct article text"


@pytest.mark.asyncio
async def test_fetch_web_page_uses_search_fallback_for_blocked_article():
    public_addr = [(2, 1, 6, "", ("93.184.216.34", 0))]
    url = "https://example.com/news/06/07/2026/blocked-article"

    with (
        patch("bot.agent.socket.getaddrinfo", return_value=public_addr),
        patch("bot.agent._ddgs_extract_page", side_effect=RuntimeError("blocked")),
        patch("bot.agent._fetch_web_page_firecrawl", side_effect=RuntimeError("402 Payment Required")),
        patch("bot.agent._fetch_web_page_reader", return_value=""),
        patch("bot.agent._fetch_web_page_direct", side_effect=RuntimeError("401, message='Unauthorized'")),
        patch("bot.agent._search_for_fetch_fallback", return_value="Snippet about the article"),
    ):
        result = await agent.fetch_web_page(url)

    assert result == "Snippet about the article"

@pytest.mark.asyncio
async def test_fetch_web_page_direct_single_attempt_no_retry():
    """Direct fetch no longer retries on empty body; reader proxy handles bot-detection cases."""
    empty_resp = AsyncMock()
    empty_resp.content.read = AsyncMock(return_value=b"")
    empty_resp.raise_for_status = MagicMock()
    empty_resp.__aenter__ = AsyncMock(return_value=empty_resp)
    empty_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=empty_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("bot.agent.aiohttp.ClientSession", return_value=mock_session):
        result = await agent._fetch_web_page_direct("https://example.com/article")

    assert result == ""
    assert mock_session.get.call_count == 1


@pytest.mark.asyncio
async def test_fetch_web_page_firecrawl_no_api_key_returns_empty():
    with patch("bot.agent.FIRECRAWL_API_KEY", None):
        result = await agent._fetch_web_page_firecrawl("https://example.com/article")
    assert result == ""


@pytest.mark.asyncio
async def test_fetch_web_page_firecrawl_posts_scrape_and_returns_markdown():
    scrape_resp = AsyncMock()
    scrape_resp.raise_for_status = MagicMock()
    scrape_resp.json = AsyncMock(return_value={"data": {"markdown": "# Title\n\nBody text"}})
    scrape_resp.__aenter__ = AsyncMock(return_value=scrape_resp)
    scrape_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=scrape_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("bot.agent.FIRECRAWL_API_KEY", "fc-test-key"),
        patch("bot.agent.FIRECRAWL_API_URL", "https://api.firecrawl.dev"),
        patch("bot.agent.aiohttp.ClientSession", return_value=mock_session) as session_mock,
    ):
        result = await agent._fetch_web_page_firecrawl("https://example.com/article")

    assert result == "# Title\n\nBody text"
    mock_session.post.assert_called_once()
    call_args = mock_session.post.call_args
    assert call_args.args[0] == "https://api.firecrawl.dev/v1/scrape"
    body = call_args.kwargs["json"]
    assert body["url"] == "https://example.com/article"
    assert body["formats"] == ["markdown"]
    assert body["onlyMainContent"] is True
    session_headers = session_mock.call_args.kwargs["headers"]
    assert session_headers["Authorization"] == "Bearer fc-test-key"
    assert session_headers["Content-Type"] == "application/json"


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


@pytest.mark.asyncio
async def test_run_ponder_agent_tool_timeout_reports_tool_name():
    first = _mock_llm_json_response(
        {"thought": "fetching", "tool": "web_search", "tool_input": "q"}
    )
    second = _mock_llm_json_response({"thought": "moving on", "answer": "done"})
    create_mock = AsyncMock(side_effect=[first, second])

    async def slow_tool(_input):
        await asyncio.sleep(10)
        return "never"

    original = agent.PONDER_TOOLS["web_search"]
    agent.PONDER_TOOLS["web_search"] = {
        "description": original["description"],
        "function": slow_tool,
        "context": "none",
        "timeout": 0.05,
    }
    try:
        with patch.object(agent.client.chat.completions, "create", create_mock):
            result = await agent.run_ponder_agent("slow query", chat_id=1)
    finally:
        agent.PONDER_TOOLS["web_search"] = original

    assert result == "done"


@pytest.mark.asyncio
async def test_apply_persona_prompt_admin_only(temp_db_path):
    new_persona = "You are a witty opera critic who speaks in short paragraphs."

    with patch.object(agent, "ADMIN_ID", 999):
        ok, reason = await agent.apply_persona_prompt(new_persona, requesting_user_id=999)
        assert ok is True
        assert reason == "ok"
        assert await agent.get_stored_persona_prompt() == new_persona

        denied, reason = await agent.apply_persona_prompt(new_persona, requesting_user_id=1)
        assert denied is False
        assert reason == "admin_only"


@pytest.mark.asyncio
async def test_apply_persona_prompt_rejects_too_short(temp_db_path):
    with patch.object(agent, "ADMIN_ID", 999):
        ok, reason = await agent.apply_persona_prompt("too short", requesting_user_id=999)
        assert ok is False
        assert reason == "too_short"


@pytest.mark.asyncio
async def test_reset_stored_persona_prompt(temp_db_path):
    with patch.object(agent, "ADMIN_ID", 999):
        await agent.apply_persona_prompt(
            "You are a dramatic stage actor with flair and passion.",
            requesting_user_id=999,
        )
        ok, reason = await agent.reset_stored_persona_prompt(requesting_user_id=999)
        assert ok is True
        assert await agent.get_stored_persona_prompt() == agent.DEFAULT_PERSONA


@pytest.mark.asyncio
async def test_run_ponder_agent_persona_update_via_string_tool_input(temp_db_path):
    admin_id = 424242
    new_persona = "You are a calm technical mentor who explains things clearly."

    first = _mock_llm_json_response(
        {"thought": "updating persona", "tool": "update_persona_prompt", "tool_input": new_persona}
    )
    second = _mock_llm_json_response({"thought": "done", "answer": "Persona updated."})
    create_mock = AsyncMock(side_effect=[first, second])

    with (
        patch.object(agent, "ADMIN_ID", admin_id),
        patch.object(agent, "generate_reaction_prompt", AsyncMock(return_value="reaction prompt")),
        patch.object(agent.client.chat.completions, "create", create_mock),
    ):
        result = await agent.run_ponder_agent(
            "change your persona to be a calm technical mentor",
            chat_id=1,
            requesting_user_id=admin_id,
        )

    assert result == "Persona updated."
    assert await agent.get_stored_persona_prompt() == new_persona


@pytest.mark.asyncio
async def test_run_ponder_agent_persona_update_denied_for_non_admin(temp_db_path):
    first = _mock_llm_json_response(
        {"thought": "updating persona", "tool": "update_persona_prompt", "tool_input": "You are a pirate captain."}
    )
    second = _mock_llm_json_response({"thought": "denied", "answer": "Permission denied."})
    create_mock = AsyncMock(side_effect=[first, second])

    with (
        patch.object(agent, "ADMIN_ID", 999),
        patch.object(agent.client.chat.completions, "create", create_mock),
    ):
        result = await agent.run_ponder_agent(
            "change your persona",
            chat_id=1,
            requesting_user_id=1,
        )

    assert "denied" in result.lower()


@pytest.mark.asyncio
async def test_run_ponder_agent_behavior_update_via_dict_tool_input(temp_db_path):
    from bot.logic import GLOBAL_SETTINGS_CHAT_ID, get_behavior_settings

    admin_id = 9001
    first = _mock_llm_json_response(
        {
            "thought": "updating behavior",
            "tool": "update_behavior_settings",
            "tool_input": {
                "reaction_chance": 0.2,
                "media_reply_guidance": "Use saved stickers or gifs in most replies when appropriate.",
            },
        }
    )
    second = _mock_llm_json_response({"thought": "done", "answer": "Behavior settings updated."})
    create_mock = AsyncMock(side_effect=[first, second])

    with (
        patch.object(agent, "ADMIN_ID", admin_id),
        patch.object(agent.client.chat.completions, "create", create_mock),
    ):
        result = await agent.run_ponder_agent(
            "react more and use more stickers",
            chat_id=12345,
            requesting_user_id=admin_id,
            settings_chat_id=GLOBAL_SETTINGS_CHAT_ID,
        )

    assert result == "Behavior settings updated."
    settings = await get_behavior_settings(GLOBAL_SETTINGS_CHAT_ID)
    assert settings["reaction_chance"] == 0.2
    assert "stickers" in settings["media_reply_guidance"]


@pytest.mark.asyncio
async def test_run_ponder_agent_get_behavior_settings(temp_db_path):
    from bot.logic import GLOBAL_SETTINGS_CHAT_ID

    first = _mock_llm_json_response(
        {"thought": "reading settings", "tool": "get_behavior_settings", "tool_input": ""}
    )
    second = _mock_llm_json_response({"thought": "done", "answer": "Settings retrieved."})
    create_mock = AsyncMock(side_effect=[first, second])

    with patch.object(agent.client.chat.completions, "create", create_mock):
        result = await agent.run_ponder_agent(
            "show me the current behavior settings",
            chat_id=12345,
            settings_chat_id=GLOBAL_SETTINGS_CHAT_ID,
        )

    assert result == "Settings retrieved."


@pytest.mark.asyncio
async def test_run_ponder_agent_get_persona_prompt(temp_db_path):
    first = _mock_llm_json_response(
        {"thought": "reading persona", "tool": "get_persona_prompt", "tool_input": ""}
    )
    second = _mock_llm_json_response({"thought": "done", "answer": "Persona retrieved."})
    create_mock = AsyncMock(side_effect=[first, second])

    with patch.object(agent.client.chat.completions, "create", create_mock):
        result = await agent.run_ponder_agent(
            "show me the current persona",
            chat_id=1,
        )

    assert result == "Persona retrieved."


@pytest.mark.asyncio
async def test_run_ponder_agent_reset_persona_prompt(temp_db_path):
    admin_id = 424242
    first = _mock_llm_json_response(
        {"thought": "resetting persona", "tool": "reset_persona_prompt", "tool_input": ""}
    )
    second = _mock_llm_json_response({"thought": "done", "answer": "Persona reset."})
    create_mock = AsyncMock(side_effect=[first, second])

    with (
        patch.object(agent, "ADMIN_ID", admin_id),
        patch.object(agent, "generate_reaction_prompt", AsyncMock(return_value="reaction prompt")),
        patch.object(agent.client.chat.completions, "create", create_mock),
    ):
        result = await agent.run_ponder_agent(
            "reset your persona to default",
            chat_id=1,
            requesting_user_id=admin_id,
        )

    assert result == "Persona reset."
    assert await agent.get_stored_persona_prompt() == agent.DEFAULT_PERSONA
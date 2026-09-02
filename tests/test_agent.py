import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import agent
from bot.memory import (
    add_general_memory,
    get_general_memories,
    get_relevant_research_notes,
    get_user_thought,
    record_persona_output,
    save_media_description,
    save_research_note,
    search_media_descriptions,
    update_user_thought,
)


def _mock_aiohttp_response(body: str, *, as_bytes: bool = False):
    mock_resp = AsyncMock()
    if as_bytes:
        mock_resp.content.read = AsyncMock(return_value=body.encode("utf-8"))
        mock_resp.text = AsyncMock(return_value="")
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

    with (
        patch("bot.agent._firecrawl_web_search", AsyncMock(return_value=[])),
        patch("bot.agent._run_web_search", return_value=[agent._format_search_hit(**row) for row in mock_rows]),
    ):
        result = await agent.web_search("test query")

    assert result.startswith("Fallback search results (DDGS):")
    assert "First Result" in result
    assert "First snippet text" in result
    assert "https://example.com/1" in result
    assert "Second Result" in result


@pytest.mark.asyncio
async def test_web_search_no_results():
    with (
        patch("bot.agent._firecrawl_web_search", AsyncMock(return_value=[])),
        patch("bot.agent._run_web_search", return_value=[]),
    ):
        result = await agent.web_search("test query")

    assert result == "No search results found."


@pytest.mark.asyncio
async def test_web_search_prefers_firecrawl_results():
    firecrawl_results = ["Reliable source\nhttps://example.com\nFull markdown context"]
    with (
        patch("bot.agent._firecrawl_web_search", AsyncMock(return_value=firecrawl_results)),
        patch("bot.agent._run_web_search") as ddgs_mock,
    ):
        result = await agent.web_search("test query")

    assert result.startswith("Firecrawl search results:")
    assert "Full markdown context" in result
    ddgs_mock.assert_not_called()


@pytest.mark.asyncio
async def test_firecrawl_web_search_posts_v2_request_and_formats_markdown():
    response = AsyncMock()
    response.raise_for_status = MagicMock()
    response.json = AsyncMock(return_value={
        "success": True,
        "data": {"web": [{"title": "Result", "url": "https://example.com/a", "markdown": "Readable article text"}]},
    })
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("bot.agent.FIRECRAWL_API_KEY", "fc-test-key"),
        patch("bot.agent.FIRECRAWL_API_URL", "https://api.firecrawl.dev"),
        patch("bot.agent.aiohttp.ClientSession", return_value=session),
    ):
        results = await agent._firecrawl_web_search("research query")

    session.post.assert_called_once_with(
        "https://api.firecrawl.dev/v2/search", json={"query": "research query", "limit": 5}
    )
    assert results == [
        "Title: Result\nURL: https://example.com/a\nSnippet: Readable article text"
    ]


@pytest.mark.asyncio
async def test_web_search_current_query_merges_news_and_general_results():
    with patch("bot.agent._ddgs_text_search", return_value=["Official source"]) as text_mock, patch(
        "bot.agent._ddgs_news_search", return_value=["Headline: story body (https://example.com/news)"]
    ) as news_mock:
        results = agent._run_web_search("major news yesterday")

    news_mock.assert_called_once_with("major news yesterday")
    text_mock.assert_called_once_with("major news yesterday")
    assert results == ["Headline: story body (https://example.com/news)", "Official source"]


def test_format_search_hit_preserves_news_provenance():
    result = agent._format_search_hit(
        "Fresh story",
        "Just happened",
        "https://example.com/news",
        source="Example Wire",
        published="2026-08-01T01:02:03+00:00",
    )

    assert "Title: Fresh story" in result
    assert "URL: https://example.com/news" in result
    assert "Source: Example Wire" in result
    assert "Published: 2026-08-01T01:02:03+00:00" in result
    assert "Snippet: Just happened" in result


def test_distinct_source_count_counts_documents_but_not_fragments_twice():
    assert agent._distinct_source_count(
        [
            "https://docs.python.org/3/whatsnew/3.14.html",
            "https://docs.python.org/3/whatsnew/3.14.html#free-threading",
            "https://peps.python.org/pep-0779/",
            "https://astral.sh/blog/python-3.14",
        ]
    ) == 3


def test_web_search_current_query_falls_back_to_text_when_news_is_empty():
    with patch("bot.agent._ddgs_news_search", return_value=[]) as news_mock, patch(
        "bot.agent._ddgs_text_search", return_value=["Web result"]
    ) as text_mock:
        results = agent._run_web_search("latest weather today")

    news_mock.assert_called_once_with("latest weather today")
    text_mock.assert_called_once_with("latest weather today")
    assert results == ["Web result"]


def test_web_search_recognizes_russian_freshness_terms():
    with patch("bot.agent._ddgs_news_search", return_value=["Свежая новость"]) as news_mock, patch(
        "bot.agent._ddgs_text_search", return_value=["Официальный источник"]
    ) as text_mock:
        results = agent._run_web_search("последние новости сегодня")

    news_mock.assert_called_once()
    text_mock.assert_called_once()
    assert results == ["Свежая новость", "Официальный источник"]


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

    assert len(result) <= agent._MAX_PAGE_CHARS


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

    assert result == "Search fallback (not full page): Snippet about the article"

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


@pytest.mark.asyncio
async def test_search_own_outputs_is_scoped_to_chat(temp_db_path):
    await record_persona_output(42, 700, "text", "the embarrassing pineapple take")
    await record_persona_output(43, 701, "text", "the embarrassing pineapple take")

    result = await agent.search_own_outputs("pineapple", 42)

    assert "message_id=700" in result
    assert "message_id=701" not in result


def _mock_llm_json_response(payload: dict):
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = json.dumps(payload)
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    return mock_response


@pytest.mark.asyncio
async def test_run_ponder_agent_answer_on_first_step(temp_db_path):
    mock_response = _mock_llm_json_response(
        {"thought": "I know this", "answer": "The answer is 42."}
    )
    usage = MagicMock()
    usage.prompt_tokens = 50
    usage.completion_tokens = 10
    usage.total_tokens = 60
    usage.prompt_tokens_details = {"cached_tokens": 20, "cache_write_tokens": 7}
    mock_response.usage = usage

    with patch.object(
        agent.client.chat.completions, "create", AsyncMock(return_value=mock_response)
    ):
        result = await agent.run_ponder_agent("what is the answer", chat_id=1)

    assert result == "The answer is 42."

    from bot.telemetry import fetch_llm_telemetry

    events = await fetch_llm_telemetry(chat_id=1, source="ponder_agent")
    assert len(events) == 1
    event = events[0]
    assert event["source"] == "ponder_agent"
    assert event["status"] == "success"
    assert event["memory_query"] == "what is the answer"
    assert event["prompt_tokens"] == 50
    assert event["prompt_cached_tokens"] == 20
    assert event["prompt_cache_write_tokens"] == 7
    assert event["uncached_prompt_tokens"] == 30
    assert event["prompt_cache_hit_rate"] == pytest.approx(0.4)
    assert event["response_messages"] == ["The answer is 42."]


@pytest.mark.asyncio
@pytest.mark.parametrize(("reasoning_effort", "expected"), [("", None), ("high", "high")])
async def test_run_ponder_agent_conditionally_passes_reasoning_effort(
    reasoning_effort, expected
):
    mock_response = _mock_llm_json_response({"thought": "I know this", "answer": "Done."})

    with (
        patch.object(agent, "LLM_PONDER_REASONING_EFFORT", reasoning_effort),
        patch.object(agent, "_prefetch_linked_sources", AsyncMock(return_value=[])),
        patch.object(agent, "get_relevant_research_notes", AsyncMock(return_value=[])),
        patch.object(agent, "_finalize_ponder_answer", AsyncMock(return_value="Done.")),
        patch.object(
            agent.client.chat.completions, "create", AsyncMock(return_value=mock_response)
        ) as create_mock,
    ):
        assert await agent.run_ponder_agent("answer this", chat_id=1) == "Done."

    kwargs = create_mock.await_args.kwargs
    assert kwargs["model"] == agent.LLM_PONDER_MODEL
    assert kwargs["response_format"] == {"type": "json_object"}
    if expected is None:
        assert "reasoning_effort" not in kwargs
    else:
        assert kwargs["reasoning_effort"] == expected


@pytest.mark.asyncio
async def test_run_ponder_agent_prefetches_linked_article_and_keeps_source():
    url = "https://example.com/article"
    mock_response = _mock_llm_json_response(
        {"thought": "read the article", "answer": "The article says the launch is delayed."}
    )

    with (
        patch("bot.agent.fetch_web_page", AsyncMock(return_value="Full article body with details.")) as fetch_mock,
        patch.object(agent.client.chat.completions, "create", AsyncMock(return_value=mock_response)) as create_mock,
    ):
        result = await agent.run_ponder_agent(
            f"Read this article and explain it: {url}",
            chat_id=1,
            conversation_context="Alice (id=7): [FOCUS] What does it say?",
        )

    fetch_mock.assert_awaited_once_with(url)
    request = create_mock.call_args.kwargs["messages"][1]["content"]
    assert "Full article body with details." in request
    assert "What does it say?" in request
    assert f'Sources consulted: {url}' in result


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
async def test_run_ponder_agent_requires_fetch_after_search_before_answering():
    url = "https://example.com/primary"
    responses = [
        _mock_llm_json_response(
            {"thought": "find sources", "tool": "web_search", "tool_input": "research query"}
        ),
        _mock_llm_json_response(
            {"thought": "the snippet looks enough", "answer": "Unverified snippet answer."}
        ),
        _mock_llm_json_response(
            {"thought": "verify it", "tool": "fetch_web_page", "tool_input": url}
        ),
        _mock_llm_json_response(
            {"thought": "verified", "answer": "The primary source confirms the claim."}
        ),
    ]
    create_mock = AsyncMock(side_effect=responses)
    search_mock = AsyncMock(
        return_value=f"Search results are discovery leads.\n[1]\nTitle: Primary\nURL: {url}\nSnippet: Claim"
    )
    fetch_mock = AsyncMock(return_value="Full primary-source text")
    original_search = agent.PONDER_TOOLS["web_search"]["function"]
    original_fetch = agent.PONDER_TOOLS["fetch_web_page"]["function"]
    agent.PONDER_TOOLS["web_search"]["function"] = search_mock
    agent.PONDER_TOOLS["fetch_web_page"]["function"] = fetch_mock
    try:
        with patch.object(agent.client.chat.completions, "create", create_mock):
            result = await agent.run_ponder_agent("research this claim", chat_id=1)
    finally:
        agent.PONDER_TOOLS["web_search"]["function"] = original_search
        agent.PONDER_TOOLS["fetch_web_page"]["function"] = original_fetch

    fetch_mock.assert_awaited_once_with(url)
    assert "Unverified snippet answer" not in result
    assert "The primary source confirms" in result
    assert f"Sources consulted: {url}" in result


@pytest.mark.asyncio
async def test_run_ponder_agent_cross_checks_current_claim_with_two_sources():
    first_url = "https://example.com/official"
    second_url = "https://example.net/independent"
    responses = [
        _mock_llm_json_response(
            {"thought": "search", "tool": "web_search", "tool_input": "latest release today"}
        ),
        _mock_llm_json_response(
            {"thought": "read official", "tool": "fetch_web_page", "tool_input": first_url}
        ),
        _mock_llm_json_response(
            {"thought": "one source", "answer": "The release happened."}
        ),
        _mock_llm_json_response(
            {"thought": "cross-check", "tool": "fetch_web_page", "tool_input": second_url}
        ),
        _mock_llm_json_response(
            {"thought": "corroborated", "answer": "Two sources confirm the release."}
        ),
    ]
    search_mock = AsyncMock(
        return_value=(
            f"[1]\nURL: {first_url}\nSnippet: release\n\n"
            f"[2]\nURL: {second_url}\nSnippet: confirmation"
        )
    )
    fetch_mock = AsyncMock(side_effect=["Official announcement", "Independent report"])
    original_search = agent.PONDER_TOOLS["web_search"]["function"]
    original_fetch = agent.PONDER_TOOLS["fetch_web_page"]["function"]
    agent.PONDER_TOOLS["web_search"]["function"] = search_mock
    agent.PONDER_TOOLS["fetch_web_page"]["function"] = fetch_mock
    try:
        with patch.object(
            agent.client.chat.completions, "create", AsyncMock(side_effect=responses)
        ):
            result = await agent.run_ponder_agent(
                "What is the latest release today?", chat_id=1
            )
    finally:
        agent.PONDER_TOOLS["web_search"]["function"] = original_search
        agent.PONDER_TOOLS["fetch_web_page"]["function"] = original_fetch

    assert fetch_mock.await_args_list[0].args == (first_url,)
    assert fetch_mock.await_args_list[1].args == (second_url,)
    assert "Two sources confirm" in result
    assert first_url in result
    assert second_url in result


@pytest.mark.asyncio
async def test_run_ponder_agent_uses_final_synthesis_call_after_last_tool_step():
    url = "https://example.com/source"
    responses = [
        _mock_llm_json_response(
            {"thought": "read source", "tool": "fetch_web_page", "tool_input": url}
        ),
        _mock_llm_json_response(
            {"thought": "synthesize", "answer": "Verified final synthesis."}
        ),
    ]
    fetch_mock = AsyncMock(return_value="Authoritative source body")
    original_fetch = agent.PONDER_TOOLS["fetch_web_page"]["function"]
    agent.PONDER_TOOLS["fetch_web_page"]["function"] = fetch_mock
    try:
        with patch.object(
            agent.client.chat.completions, "create", AsyncMock(side_effect=responses)
        ) as create_mock:
            result = await agent.run_ponder_agent(
                "verify a claim", chat_id=1, max_steps=1
            )
    finally:
        agent.PONDER_TOOLS["fetch_web_page"]["function"] = original_fetch

    assert create_mock.await_count == 2
    assert "Verified final synthesis." in result
    assert f"Sources consulted: {url}" in result


@pytest.mark.asyncio
async def test_run_ponder_agent_default_budget_allows_more_than_six_tool_steps():
    tool_responses = [
        _mock_llm_json_response(
            {"thought": f"research step {index}", "tool": "web_search", "tool_input": f"q{index}"}
        )
        for index in range(7)
    ]
    final_response = _mock_llm_json_response(
        {"thought": "enough evidence", "answer": "Completed deeper research."}
    )
    search_mock = AsyncMock(return_value="No search results found.")
    original_tool = agent.PONDER_TOOLS["web_search"]["function"]
    agent.PONDER_TOOLS["web_search"]["function"] = search_mock
    try:
        with (
            patch.object(agent, "LLM_PONDER_MAX_STEPS", 10),
            patch.object(
                agent.client.chat.completions,
                "create",
                AsyncMock(side_effect=[*tool_responses, final_response]),
            ) as create_mock,
        ):
            result = await agent.run_ponder_agent("research deeply", chat_id=1)
    finally:
        agent.PONDER_TOOLS["web_search"]["function"] = original_tool

    assert create_mock.await_count == 8
    assert search_mock.await_count == 7
    assert result == "Completed deeper research."


@pytest.mark.asyncio
async def test_run_ponder_agent_does_not_refetch_same_page_fragment():
    url = "https://docs.python.org/3/whatsnew/3.14.html"
    responses = [
        _mock_llm_json_response(
            {"thought": "read page", "tool": "fetch_web_page", "tool_input": url}
        ),
        _mock_llm_json_response(
            {
                "thought": "read section",
                "tool": "fetch_web_page",
                "tool_input": url + "#free-threaded-python",
            }
        ),
        _mock_llm_json_response(
            {"thought": "done", "answer": "The page was read once."}
        ),
    ]
    fetch_mock = AsyncMock(return_value="Full documentation page")
    original_fetch = agent.PONDER_TOOLS["fetch_web_page"]["function"]
    agent.PONDER_TOOLS["fetch_web_page"]["function"] = fetch_mock
    try:
        with patch.object(
            agent.client.chat.completions, "create", AsyncMock(side_effect=responses)
        ):
            result = await agent.run_ponder_agent("inspect the documentation", chat_id=1)
    finally:
        agent.PONDER_TOOLS["fetch_web_page"]["function"] = original_fetch

    fetch_mock.assert_awaited_once_with(url)
    assert result.count(url) == 1


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

    assert result == "Could not complete verified research in time."


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


@pytest.mark.asyncio
async def test_ponder_update_user_thought_dict_and_json_string(temp_db_path):
    chat_id = 7
    result = await agent._ponder_update_user_thought(
        {"user_id": 42, "username": "bob", "thought": "Likes jazz."},
        chat_id,
    )
    assert "Updated thoughts" in result
    assert await get_user_thought(42) == "Likes jazz."

    result = await agent._ponder_update_user_thought(
        json.dumps({"user_id": 42, "username": "bob", "thought": "Loves bebop."}),
        chat_id,
    )
    assert "Updated thoughts" in result
    assert await get_user_thought(42) == "Loves bebop."


@pytest.mark.asyncio
async def test_ponder_add_update_delete_general_memory(temp_db_path):
    chat_id = 11
    add_result = await agent._ponder_add_general_memory(
        {"topic": "Coffee", "summary": "Group prefers dark roast.", "importance": 4},
        chat_id,
    )
    assert "Added general memory" in add_result

    memories = await get_general_memories(chat_id, limit=5)
    assert any("Coffee" in m and "dark roast" in m for m in memories)
    memory_id = int(memories[0].split(",")[0].removeprefix("id="))

    update_result = await agent._ponder_update_general_memory(
        {"memory_id": memory_id, "summary": "Group prefers medium roast."},
        chat_id,
    )
    assert f"Updated general memory id={memory_id}" in update_result
    memories = await get_general_memories(chat_id, limit=5)
    assert any("medium roast" in m for m in memories)

    delete_result = await agent._ponder_delete_general_memory(
        {"memory_id": memory_id},
        chat_id,
    )
    assert f"Deleted general memory id={memory_id}" in delete_result
    memories = await get_general_memories(chat_id, limit=5)
    assert not any(f"id={memory_id}" in m for m in memories)

    missing = await agent._ponder_delete_general_memory(str(memory_id), chat_id)
    assert "not found" in missing


@pytest.mark.asyncio
async def test_ponder_media_summary_tools(temp_db_path):
    chat_id = 13
    media_id = "photo_abc123"
    await save_media_description(media_id, "A red bicycle parked outside.")

    search_result = await agent._ponder_search_media_summaries("bicycle", chat_id)
    assert media_id in search_result
    assert "red bicycle" in search_result

    update_result = await agent._ponder_update_media_summary(
        {"media_unique_id": media_id, "description": "A blue bicycle by the curb."},
        chat_id,
    )
    assert "Updated media summary" in update_result
    found = await search_media_descriptions("blue bicycle")
    assert any(media_id in row for row in found)

    clear_result = await agent._ponder_clear_media_summary(media_id, chat_id)
    assert "Cleared media summary" in clear_result
    assert await search_media_descriptions("blue bicycle") == []


@pytest.mark.asyncio
async def test_ponder_memory_tools_reject_invalid_input(temp_db_path):
    chat_id = 15
    assert "Invalid" in await agent._ponder_update_user_thought({}, chat_id)
    assert "Invalid" in await agent._ponder_add_general_memory({"topic": "x"}, chat_id)
    assert "Invalid" in await agent._ponder_update_general_memory({"memory_id": 1}, chat_id)
    assert "Invalid" in await agent._ponder_delete_general_memory("", chat_id)


@pytest.mark.asyncio
async def test_run_ponder_agent_add_general_memory_via_tool(temp_db_path):
    chat_id = 21
    responses = [
        {
            "thought": "store fact",
            "tool": "add_general_memory",
            "tool_input": {
                "topic": "Launch date",
                "summary": "Project ships on Friday.",
                "importance": 5,
            },
        },
        {"thought": "done", "answer": "Remembered the launch date."},
    ]
    call_count = 0

    async def mock_create(**kwargs):
        nonlocal call_count
        payload = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        return _mock_llm_json_response(payload)

    with (
        patch("bot.agent.client.chat.completions.create", side_effect=mock_create),
        patch("bot.agent._prefetch_linked_sources", AsyncMock(return_value=[])),
    ):
        result = await agent.run_ponder_agent(
            "remember that the project ships on Friday",
            chat_id=chat_id,
        )

    assert "Remembered the launch date" in result
    memories = await get_general_memories(chat_id, limit=5)
    assert any("Launch date" in m and "Friday" in m for m in memories)


@pytest.mark.asyncio
async def test_ponder_tools_include_memory_mutations():
    for name in (
        "update_user_thought",
        "add_general_memory",
        "update_general_memory",
        "delete_general_memory",
        "search_media_summaries",
        "clear_media_summary",
        "update_media_summary",
    ):
        assert name in agent.PONDER_TOOLS
        assert agent.PONDER_TOOLS[name]["context"] == "chat_id"

    assert "search_own_outputs" in agent.PONDER_TOOLS
    assert agent.PONDER_TOOLS["search_own_outputs"]["context"] == "chat_id"


@pytest.mark.asyncio
async def test_run_ponder_agent_persists_and_loads_prior_research(temp_db_path):
    chat_id = 5151
    prior = (
        "Python 3.13 adds free-threading experimental support and improves error messages. "
        "Many packages still need wheels for the free-threaded build."
    )
    await save_research_note(chat_id, "Python 3.13 free threading features", prior)

    answer = (
        "Follow-up: free-threading is still experimental; use the regular build for production. "
        "Error message improvements are available on all builds."
    )
    mock_response = _mock_llm_json_response({"thought": "reuse prior", "answer": answer})

    with patch.object(
        agent.client.chat.completions, "create", AsyncMock(return_value=mock_response)
    ) as create_mock:
        result = await agent.run_ponder_agent(
            "Is free threading ready for production in Python 3.13?",
            chat_id=chat_id,
        )

    assert result == answer
    request = create_mock.call_args.kwargs["messages"][1]["content"]
    assert "prior_research" in request
    assert "free-threading experimental" in request

    related = await get_relevant_research_notes(
        chat_id, "Python free threading production readiness"
    )
    assert related
    assert "Follow-up" in related[0]["result"] or "free-threading" in related[0]["result"]

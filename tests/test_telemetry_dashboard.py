from bot.telemetry.dashboard import render_dashboard_html


def _sample_event():
    return {
        "id": 42,
        "timestamp": "2026-01-01 10:00:00",
        "chat_id": 123,
        "source": "message",
        "status": "success",
        "model": "test-model",
        "focus_message_id": 1,
        "latency_ms": 500,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "prompt_cached_tokens": 40,
        "context_message_count": 2,
        "context_chars": 600,
        "system_prompt_chars": 1000,
        "user_thought_count": 1,
        "retrieved_memory_count": 2,
        "memory_query": "hello world",
        "trigger_messages": [
            {"message_id": 1, "sender": "Alice", "user_id": 123, "text": "Hello <bot> & friends"}
        ],
        "used_user_thoughts": {"Alice": "Needs help"},
        "used_general_memories": ["Topic: Greeting"],
        "tool_calls": [{"name": "add_general_memory", "arguments": {"topic": "Greeting"}}],
        "memory_writes": [
            {
                "type": "general_memory",
                "status": "succeeded",
                "arguments": {"topic": "Greeting", "chat_id": 123},
            }
        ],
        "response_messages": ["Hi there!"],
        "response_message_count": 1,
        "response_chars": 8,
        "memory_write_count": 1,
        "failed_memory_write_count": 0,
        "tool_call_count": 1,
        "reply_to_message_id": 1,
        "response_media": {"media_unique_id": "photo_u1", "media_type": "photo", "description": "some photo description"},
    }


def test_render_dashboard_html_with_event():
    html = render_dashboard_html([_sample_event()], [123], {"limit": 100})
    assert "Bot Telemetry Dashboard" in html
    assert "Inspect what context and memories" in html
    assert "Memory Behavior" in html
    assert "Trigger messages used" in html
    assert "Memories used" in html
    assert "Response" in html
    assert "Memorized" in html
    assert "photo_u1" in html
    assert "some photo description" in html
    assert "Avg cached prompt tokens" in html
    assert "40" in html
    # dynamic text is escaped
    assert "Hello &lt;bot&gt; &amp; friends" in html


def test_render_dashboard_html_empty():
    html = render_dashboard_html([], [], {"limit": 100})
    assert "No telemetry recorded for these filters yet." in html
    assert "Bot Telemetry Dashboard" in html

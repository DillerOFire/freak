import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import json
from bot import llm

@pytest.mark.asyncio
async def test_get_system_prompt_default(temp_db_path):
    """Test that get_system_prompt uses DEFAULT_PERSONA and saves it if not configured."""
    prompt = await llm.get_system_prompt()
    assert llm.DEFAULT_PERSONA in prompt
    assert llm.SYSTEM_INSTRUCTIONS.strip() in prompt
    assert "\n---\n\n" in prompt
    
    # Verify it was saved in config
    from bot.memory import get_config
    saved_persona = await get_config("persona_prompt")
    assert saved_persona == llm.DEFAULT_PERSONA

@pytest.mark.asyncio
async def test_get_system_prompt_custom(temp_db_path):
    """Test that get_system_prompt uses the custom persona if configured."""
    from bot.memory import set_config
    custom_persona = "You are a fancy opera singer."
    await set_config("persona_prompt", custom_persona)
    
    prompt = await llm.get_system_prompt()
    assert custom_persona in prompt
    assert llm.SYSTEM_INSTRUCTIONS.strip() in prompt
    assert llm.DEFAULT_PERSONA not in prompt

@pytest.mark.asyncio
async def test_get_reaction_prompt_default(temp_db_path):
    """Test that get_reaction_prompt builds and saves a persona-based fallback."""
    from bot.memory import get_config

    prompt = await llm.get_reaction_prompt()

    assert llm.DEFAULT_PERSONA in prompt
    assert llm.ALLOWED_REACTIONS_TEXT in prompt
    assert await get_config("reaction_prompt") == prompt


@pytest.mark.asyncio
async def test_generate_reaction_prompt_appends_allowed_reactions():
    """Test generated reaction prompts are constrained to Telegram bot reactions."""
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = "React in this persona voice."
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]

    with patch.object(llm.client.chat.completions, "create", AsyncMock(return_value=mock_response)):
        prompt = await llm.generate_reaction_prompt("Persona text")

    assert "React in this persona voice." in prompt
    assert llm.ALLOWED_REACTIONS_TEXT in prompt
    assert "Hard constraint" in prompt


@pytest.mark.asyncio
async def test_generate_response_success(temp_db_path):
    """Test successful LLM response with tool calls and content."""
    mock_messages_context = [
        {"message_id": 1, "sender": "Alice", "user_id": 123, "text": "Hello bot", "reply_to_username": None, "reply_to_text": None}
    ]
    mock_user_thoughts = {"Alice": "Needs help"}
    mock_general_memories = ["Topic: Greeting, Summary: Alice said hello"]
    chat_id = 9999
    
    # Mock LLM API response
    mock_response = MagicMock()
    mock_response.usage = None
    mock_choice = MagicMock()
    mock_message = MagicMock()
    
    mock_message.content = json.dumps({
        "tool_calls": [
            {
                "name": "update_user_thought",
                "arguments": {
                    "user_id": 123,
                    "username": "Alice",
                    "thought": "Alice is very polite today."
                }
            },
            {
                "name": "add_general_memory",
                "arguments": {
                    "topic": "Politeness",
                    "summary": "People are greeting each other.",
                    "importance": 4
                }
            }
        ],
        "reply_to_message_id": 1,
        "messages": ["Hello, my dear!", "How can I help you today?"],
        "polls": [],
        "media_reply_unique_id": None
    })
    
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    
    # We patch the completions.create method
    async_create_mock = AsyncMock(return_value=mock_response)
    
    with patch.object(llm.client.chat.completions, "create", async_create_mock):
        result = await llm.generate_response(
            messages_context=mock_messages_context,
            user_thoughts=mock_user_thoughts,
            general_memories=mock_general_memories,
            chat_id=chat_id,
            focus_message_id=1
        )
        
        # Verify result structure
        assert result is not None
        assert result["reply_to_message_id"] == 1
        assert result["messages"] == ["Hello, my dear!", "How can I help you today?"]
        assert result["polls"] == []
        assert result["media_reply_unique_id"] is None
        
        # Verify user prompt format contains XML tags
        call_args = async_create_mock.call_args
        assert call_args is not None
        user_message_content = call_args[1]["messages"][1]["content"]
        assert "<conversation_context>" in user_message_content
        assert 'id="1"' in user_message_content
        assert 'sender="Alice"' in user_message_content
        assert 'sender_id="123"' in user_message_content
        assert 'focus="true"' in user_message_content
        assert 'Hello bot' in user_message_content
        assert "<core_memory>" in user_message_content
        assert '<user name="Alice"><![CDATA[Needs help]]></user>' in user_message_content
        assert "<retrieved_semantic_memory>" in user_message_content
        assert "Topic: Greeting, Summary: Alice said hello" in user_message_content
        
        # Verify tool calls were processed and written to database
        from bot.memory import get_user_thought, get_general_memories
        
        saved_thought = await get_user_thought(123)
        assert saved_thought == "Alice is very polite today."
        
        saved_memories = await get_general_memories(chat_id)
        assert len(saved_memories) == 1
        assert "Topic: Politeness, Summary: People are greeting each other." in saved_memories[0]

        from bot.telemetry import fetch_llm_telemetry

        telemetry_events = await fetch_llm_telemetry(chat_id=chat_id)
        assert len(telemetry_events) == 1
        event = telemetry_events[0]
        assert event["status"] == "success"
        assert event["source"] == "message"
        assert event["trigger_messages"][0]["text"] == "Hello bot"
        assert event["used_user_thoughts"] == mock_user_thoughts
        assert event["used_general_memories"] == mock_general_memories
        assert event["tool_call_count"] == 2
        assert event["memory_write_count"] == 2
        assert event["failed_memory_write_count"] == 0
        assert event["response_messages"] == ["Hello, my dear!", "How can I help you today?"]



@pytest.mark.asyncio
async def test_generate_response_poll_only_success(temp_db_path):
    """Test successful LLM response with only a poll."""
    mock_messages_context = [
        {"message_id": 1, "sender": "Alice", "user_id": 123, "text": "Lunch?", "reply_to_username": None, "reply_to_text": None}
    ]
    chat_id = 9999

    mock_response = MagicMock()
    mock_response.usage = None
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = json.dumps({
        "tool_calls": [],
        "reply_to_message_id": None,
        "messages": [],
        "polls": [{
            "question": "Lunch?",
            "options": ["Pizza", "Sushi"],
            "is_anonymous": True,
            "allows_multiple_answers": False,
        }],
    })
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]

    with patch.object(llm.client.chat.completions, "create", AsyncMock(return_value=mock_response)):
        result = await llm.generate_response(
            messages_context=mock_messages_context,
            user_thoughts={},
            general_memories=[],
            chat_id=chat_id,
            focus_message_id=1,
        )

    assert result is not None
    assert result["polls"] == [{
        "question": "Lunch?",
        "options": ["Pizza", "Sushi"],
        "is_anonymous": True,
        "allows_multiple_answers": False,
    }]

    from bot.telemetry import fetch_llm_telemetry

    telemetry_events = await fetch_llm_telemetry(chat_id=chat_id)
    assert len(telemetry_events) == 1
    event = telemetry_events[0]
    assert event["status"] == "success"
    assert event["response_message_count"] == 0


@pytest.mark.asyncio
async def test_generate_response_invalid_poll_validation_error(temp_db_path):
    """Test that invalid poll payloads fail Pydantic validation."""
    chat_id = 9999
    mock_response = MagicMock()
    mock_response.usage = None
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = json.dumps({
        "tool_calls": [],
        "reply_to_message_id": None,
        "messages": [],
        "polls": [{
            "question": "Lunch?",
            "options": ["Pizza"],
            "is_anonymous": True,
            "allows_multiple_answers": False,
        }],
    })
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]

    with patch.object(llm.client.chat.completions, "create", AsyncMock(return_value=mock_response)):
        result = await llm.generate_response(
            messages_context=[],
            user_thoughts={},
            general_memories=[],
            chat_id=chat_id,
        )

    assert result is None

    from bot.telemetry import fetch_llm_telemetry

    telemetry_events = await fetch_llm_telemetry(chat_id=chat_id)
    assert len(telemetry_events) == 1
    assert telemetry_events[0]["status"] == "validation_error"


@pytest.mark.asyncio
async def test_generate_response_invalid_json(temp_db_path):
    """Test that generate_response returns None if LLM returns invalid JSON."""
    mock_messages_context = []
    
    mock_response = MagicMock()
    mock_response.usage = None
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = "This is not JSON at all!"
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    
    with patch.object(llm.client.chat.completions, "create", AsyncMock(return_value=mock_response)):
        result = await llm.generate_response(
            messages_context=mock_messages_context,
            user_thoughts={},
            general_memories=[],
            chat_id=9999
        )
        assert result is None

        from bot.telemetry import fetch_llm_telemetry

        telemetry_events = await fetch_llm_telemetry(chat_id=9999)
        assert len(telemetry_events) == 1
        event = telemetry_events[0]
        assert event["status"] == "invalid_json"
        assert event["error_type"] == "JSONDecodeError"
        assert event["raw_response"] == "This is not JSON at all!"

@pytest.mark.asyncio
async def test_generate_reaction_success(temp_db_path):
    """Test successful reaction generation."""
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = " 🔥 "
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    
    with patch.object(llm.client.chat.completions, "create", AsyncMock(return_value=mock_response)):
        reaction = await llm.generate_reaction("Wow, amazing!")
        assert reaction == "🔥"

@pytest.mark.asyncio
async def test_generate_reaction_error(temp_db_path):
    """Test reaction generation error handling."""
    with patch.object(llm.client.chat.completions, "create", AsyncMock(side_effect=Exception("API Error"))):
        reaction = await llm.generate_reaction("Wow, amazing!")
        assert reaction is None


@pytest.mark.asyncio
async def test_build_context_prompt_escaping():
    """Test XML context building escaping logic."""
    mock_messages_context = [
        {
            "message_id": 1,
            "sender": "A&B",
            "user_id": 123,
            "text": "2 < 3 & \"quoted\"",
            "reply_to_username": "C&D",
            "reply_to_id": 2,
            "reply_to_text": "hello \"quotes\""
        }
    ]
    prompt = llm.build_context_prompt(mock_messages_context, {}, [], 1)
    
    assert "A&amp;B" in prompt
    assert "<![CDATA[2 < 3 & \"quoted\"]]>" in prompt
    assert "2 &lt; 3" not in prompt
    assert "C&amp;D" in prompt
    assert "hello &quot;quotes&quot;" in prompt or "hello &amp;quot;quotes&amp;quot;" in prompt or "hello \"quotes\"" in prompt


@pytest.mark.asyncio
async def test_build_context_prompt_preserves_raw_greater_than():
    """Message text with > should stay raw in CDATA, not &gt;."""
    prompt = llm.build_context_prompt(
        [{"message_id": 1, "sender": "user", "user_id": 1, "text": ">:3"}],
        {},
        [],
        1,
    )
    assert "<![CDATA[>:3]]>" in prompt
    assert "&gt;:3" not in prompt


def test_llm_response_decodes_html_entities():
    response = llm.LLMResponse.model_validate(
        {"messages": ["&gt;:3", "2 &lt; 3 &amp; ok"], "polls": []}
    )
    assert response.messages == [">:3", "2 < 3 & ok"]


@pytest.mark.asyncio
async def test_generate_response_media_only_success(temp_db_path):
    mock_messages_context = [
        {"message_id": 1, "sender": "Alice", "user_id": 123, "text": "Hello bot"}
    ]
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = json.dumps({
        "tool_calls": [],
        "reply_to_message_id": 1,
        "messages": [],
        "polls": [],
        "media_reply_unique_id": "photo_u1"
    })
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.usage = None
    mock_response.choices = [mock_choice]
    
    async_create_mock = AsyncMock(return_value=mock_response)
    
    with patch.object(llm.client.chat.completions, "create", async_create_mock):
        result = await llm.generate_response(
            messages_context=mock_messages_context,
            user_thoughts={},
            general_memories=[],
            chat_id=9999,
            focus_message_id=1,
            saved_media_options=[
                {"media_unique_id": "photo_u1", "media_type": "photo", "description": "dramatic portrait", "use_count": 0}
            ]
        )
        
        assert result is not None
        assert result["messages"] == []
        assert result["media_reply_unique_id"] == "photo_u1"
        
        from bot.telemetry import fetch_llm_telemetry
        events = await fetch_llm_telemetry(chat_id=9999)
        assert len(events) == 1
        assert events[0]["status"] == "success"
        assert events[0]["response_media"]["media_unique_id"] == "photo_u1"


@pytest.mark.asyncio
async def test_generate_response_unknown_media_id_is_ignored(temp_db_path):
    mock_messages_context = [
        {"message_id": 1, "sender": "Alice", "user_id": 123, "text": "Hello bot"}
    ]
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = json.dumps({
        "tool_calls": [],
        "reply_to_message_id": 1,
        "messages": [],
        "polls": [],
        "media_reply_unique_id": "not_allowed"
    })
    mock_choice.message = mock_message
    mock_response = MagicMock()
    mock_response.usage = None
    mock_response.choices = [mock_choice]
    
    async_create_mock = AsyncMock(return_value=mock_response)
    
    with patch.object(llm.client.chat.completions, "create", async_create_mock):
        result = await llm.generate_response(
            messages_context=mock_messages_context,
            user_thoughts={},
            general_memories=[],
            chat_id=9999,
            focus_message_id=1,
            saved_media_options=[
                {"media_unique_id": "photo_u1", "media_type": "photo", "description": "dramatic portrait", "use_count": 0}
            ]
        )
        
        assert result is None
        from bot.telemetry import fetch_llm_telemetry
        events = await fetch_llm_telemetry(chat_id=9999)
        assert len(events) == 1
        assert events[0]["status"] == "no_reply"

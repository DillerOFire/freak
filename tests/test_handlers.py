import pytest
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from bot import handlers


@pytest.fixture
def mock_context(mock_context):
    mock_context.bot.send_message = AsyncMock(return_value=MagicMock())
    mock_context.bot.send_video = AsyncMock()
    mock_context.bot.send_poll = AsyncMock(return_value=MagicMock(message_id=777, from_user=MagicMock(id=999)))
    mock_context.bot.set_message_reaction = AsyncMock()
    mock_context.bot.send_photo = AsyncMock(return_value=MagicMock(message_id=888, from_user=MagicMock(id=999)))
    mock_context.bot.send_sticker = AsyncMock(return_value=MagicMock(message_id=889, from_user=MagicMock(id=999)))
    mock_context.bot.send_animation = AsyncMock(return_value=MagicMock(message_id=890, from_user=MagicMock(id=999)))
    return mock_context


@pytest.fixture
def mock_update_handler(mock_update):
    mock_update.message.from_user.id = 67890
    mock_update.message.from_user.username = "test_user"
    mock_update.effective_chat.id = 12345
    return mock_update


@pytest.mark.asyncio
async def test_handle_message_paused(temp_db_path, mock_update_handler, mock_context):
    """Test that suspended bot ignores messages."""
    with patch("bot.handlers.get_paused", return_value=True):
        await handlers.handle_message(mock_update_handler, mock_context)
        mock_context.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_reply_logic(temp_db_path, mock_update_handler, mock_context):
    """Test that bot replies when should_reply is True."""
    mock_update_handler.message.text = "Hello bot"

    with (
        patch("bot.handlers.get_paused", return_value=False),
        patch("bot.handlers.is_whitelisted", new_callable=AsyncMock) as mock_whitelist,
        patch("bot.handlers.should_reply", new_callable=AsyncMock) as mock_should_reply,
        patch("bot.handlers.generate_response", new_callable=AsyncMock) as mock_llm,
        patch(
            "bot.handlers.get_message_media_description", new_callable=AsyncMock
        ) as mock_media_desc,
    ):
        mock_whitelist.return_value = True
        mock_should_reply.return_value = True
        mock_llm.return_value = {"messages": ["Hello human", "Second msg"], "reply_to_message_id": None}
        mock_media_desc.return_value = (None, None)

        await handlers.handle_message(mock_update_handler, mock_context)

        mock_llm.assert_called_once()
        assert mock_llm.call_args.kwargs["source"] == "message"
        assert mock_llm.call_args.kwargs["memory_query"]
        assert "Hello bot" in mock_llm.call_args.kwargs["memory_query"]
        assert mock_context.bot.send_message.call_count == 2
        mock_context.bot.send_message.assert_any_call(chat_id=12345, text="Hello human")
        mock_context.bot.send_message.assert_any_call(chat_id=12345, text="Second msg")


@pytest.mark.asyncio
async def test_handle_message_reaction(temp_db_path, mock_update_handler, mock_context):
    """Test reaction logic."""
    mock_update_handler.message.text = "Funny joke"

    with (
        patch("bot.handlers.get_paused", return_value=False),
        patch("bot.handlers.is_whitelisted", new_callable=AsyncMock) as mock_whitelist,
        patch("bot.handlers.should_reply", new_callable=AsyncMock) as mock_should_reply,
        patch("bot.handlers.should_react", new_callable=AsyncMock) as mock_should_react,
        patch(
            "bot.handlers.generate_reaction", new_callable=AsyncMock
        ) as mock_gen_react,
        patch(
            "bot.handlers.get_message_media_description", new_callable=AsyncMock
        ) as mock_media_desc,
    ):
        mock_whitelist.return_value = True
        mock_should_reply.return_value = False
        mock_should_react.return_value = True
        mock_gen_react.return_value = "👍"
        mock_media_desc.return_value = (None, None)

        await handlers.handle_message(mock_update_handler, mock_context)

        mock_context.bot.set_message_reaction.assert_called_once_with(
            chat_id=12345, message_id=ANY, reaction="👍"
        )


@pytest.mark.asyncio
async def test_handle_message_ignore(temp_db_path, mock_update_handler, mock_context):
    """Test that bot ignores when should_reply is False."""
    mock_update_handler.message.text = "Just talking"

    with (
        patch("bot.handlers.get_paused", return_value=False),
        patch("bot.handlers.is_whitelisted", new_callable=AsyncMock) as mock_whitelist,
        patch("bot.handlers.should_reply", new_callable=AsyncMock) as mock_should_reply,
        patch(
            "bot.handlers.get_message_media_description", new_callable=AsyncMock
        ) as mock_media_desc,
    ):
        mock_whitelist.return_value = True
        mock_should_reply.return_value = False
        mock_media_desc.return_value = (None, None)

        await handlers.handle_message(mock_update_handler, mock_context)

        mock_context.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_bot_sender_history_label(temp_db_path, mock_update_handler, mock_context):
    """Test that incoming bot senders are labeled explicitly in history."""
    handlers.chat_history.clear()
    mock_update_handler.message.text = "Just talking"
    mock_update_handler.message.from_user.is_bot = True

    with (
        patch("bot.handlers.get_paused", return_value=False),
        patch("bot.handlers.is_whitelisted", new_callable=AsyncMock) as mock_whitelist,
        patch("bot.handlers.should_reply", new_callable=AsyncMock) as mock_should_reply,
        patch("bot.handlers.should_react", new_callable=AsyncMock) as mock_should_react,
        patch(
            "bot.handlers.get_message_media_description", new_callable=AsyncMock
        ) as mock_media_desc,
    ):
        mock_whitelist.return_value = True
        mock_should_reply.return_value = False
        mock_should_react.return_value = False
        mock_media_desc.return_value = (None, None)

        await handlers.handle_message(mock_update_handler, mock_context)

    assert handlers.chat_history[12345][-1]["sender"] == "bot:test_user"


@pytest.mark.asyncio
async def test_handle_message_sends_poll_response(temp_db_path, mock_update_handler, mock_context):
    """Test that poll-only LLM responses send Telegram polls."""
    handlers.chat_history.clear()
    mock_update_handler.message.text = "Lunch?"

    with (
        patch("bot.handlers.get_paused", return_value=False),
        patch("bot.handlers.is_whitelisted", new_callable=AsyncMock) as mock_whitelist,
        patch("bot.handlers.should_reply", new_callable=AsyncMock) as mock_should_reply,
        patch("bot.handlers.should_react", new_callable=AsyncMock) as mock_should_react,
        patch("bot.handlers.generate_response", new_callable=AsyncMock) as mock_llm,
        patch(
            "bot.handlers.get_message_media_description", new_callable=AsyncMock
        ) as mock_media_desc,
    ):
        mock_whitelist.return_value = True
        mock_should_reply.return_value = True
        mock_should_react.return_value = False
        mock_media_desc.return_value = (None, None)
        mock_llm.return_value = {
            "messages": [],
            "reply_to_message_id": None,
            "polls": [{
                "question": "Lunch?",
                "options": ["Pizza", "Sushi"],
                "is_anonymous": True,
                "allows_multiple_answers": False,
            }],
        }

        await handlers.handle_message(mock_update_handler, mock_context)

    mock_context.bot.send_poll.assert_called_once_with(
        chat_id=12345,
        question="Lunch?",
        options=["Pizza", "Sushi"],
        is_anonymous=True,
        allows_multiple_answers=False,
    )
    mock_context.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_sends_saved_photo_reply(temp_db_path, mock_update_handler, mock_context):
    # Setup message fields to avoid MagicMock media triggers
    mock_update_handler.message.photo = None
    mock_update_handler.message.sticker = None
    mock_update_handler.message.video = None
    mock_update_handler.message.animation = None
    mock_update_handler.message.document = None
    # Setup whitelisting
    from bot import memory
    await memory.add_whitelist(12345, "group", 999)
    
    # Save a media option in DB
    await memory.save_reusable_media(
        chat_id=12345,
        media_unique_id="photo_u1",
        file_id="photo_f1",
        media_type="photo",
        description="lovely portrait",
    )
    
    # Patch generate_response to return media-only response
    with patch("bot.handlers.should_reply", AsyncMock(return_value=True)), \
         patch("bot.handlers.should_react", AsyncMock(return_value=False)), \
         patch("bot.handlers.generate_response", AsyncMock(return_value={
             "tool_calls": [],
             "reply_to_message_id": 999,
             "messages": [{"saved_media_id": "photo_u1"}],
             "polls": [],
         })):
         
        await handlers.handle_message(mock_update_handler, mock_context)
        
        # Assert send_photo was called
        mock_context.bot.send_photo.assert_called_once_with(
            chat_id=12345,
            photo="photo_f1",
            reply_to_message_id=999
        )
        
        # Assert send_message was NOT called (no text messages)
        mock_context.bot.send_message.assert_not_called()
        
        # Verify usage count updated
        saved = await memory.get_saved_media_by_unique_id(12345, "photo_u1")
        assert saved["use_count"] == 1


@pytest.mark.asyncio
async def test_handle_message_sends_sticker_then_text(temp_db_path, mock_update_handler, mock_context):
    # Setup message fields to avoid MagicMock media triggers
    mock_update_handler.message.photo = None
    mock_update_handler.message.sticker = None
    mock_update_handler.message.video = None
    mock_update_handler.message.animation = None
    mock_update_handler.message.document = None
    from bot import memory
    await memory.add_whitelist(12345, "group", 999)
    
    await memory.save_reusable_media(
        chat_id=12345,
        media_unique_id="sticker_u1",
        file_id="sticker_f1",
        media_type="sticker",
        description="funny sticker",
    )
    
    with patch("bot.handlers.should_reply", AsyncMock(return_value=True)), \
         patch("bot.handlers.should_react", AsyncMock(return_value=False)), \
         patch("bot.handlers.generate_response", AsyncMock(return_value={
             "tool_calls": [],
             "reply_to_message_id": 999,
             "messages": [{"saved_media_id": "sticker_u1"}, "perfect"],
             "polls": [],
         })):
         
        await handlers.handle_message(mock_update_handler, mock_context)
        
        # Assert send_sticker was called with the reply target
        mock_context.bot.send_sticker.assert_called_once_with(
            chat_id=12345,
            sticker="sticker_f1",
            reply_to_message_id=999
        )
        
        # Assert send_message was called but WITHOUT reply target (consumed by media)
        mock_context.bot.send_message.assert_called_once_with(
            chat_id=12345,
            text="perfect"
        )


@pytest.mark.asyncio
async def test_get_message_media_description_saves_photo_metadata(temp_db_path):
    mock_photo = MagicMock()
    mock_photo.file_unique_id = "photo_u1"
    mock_photo.file_id = "photo_f1"
    mock_photo.get_file = AsyncMock()
    
    mock_msg = MagicMock()
    mock_msg.photo = [mock_photo]
    mock_msg.sticker = None
    mock_msg.video = None
    mock_msg.animation = None
    mock_msg.document = None
    
    with patch("bot.handlers.get_media_description", AsyncMock(return_value=None)), \
         patch("bot.handlers.download_file", AsyncMock(return_value="dummy_path.jpg")), \
         patch("bot.handlers.analyze_image", AsyncMock(return_value="a saved portrait")), \
         patch("bot.handlers.save_reusable_media", AsyncMock()) as mock_save_reusable, \
         patch("builtins.open", MagicMock()), \
         patch("os.remove", MagicMock()):
         
        desc, media_id = await handlers.get_message_media_description(
            mock_msg,
            chat_id=12345,
            sender_user_id=67890,
            save_reusable=True
        )
        
        assert desc == "[User sent a photo: a saved portrait]"
        assert media_id == "photo_u1"
        mock_save_reusable.assert_called_once_with(
            12345, "photo_u1", "photo_f1", "photo", "a saved portrait", 67890
        )


@pytest.mark.asyncio
async def test_get_message_media_description_saves_gif_metadata(temp_db_path):
    mock_animation = MagicMock()
    mock_animation.file_unique_id = "gif_u1"
    mock_animation.file_id = "gif_f1"
    mock_animation.get_file = AsyncMock()

    mock_msg = MagicMock()
    mock_msg.photo = None
    mock_msg.sticker = None
    mock_msg.video = None
    mock_msg.animation = mock_animation
    mock_msg.document = None

    with patch("bot.handlers.get_media_description", AsyncMock(return_value=None)), \
         patch("bot.handlers.download_file", AsyncMock(return_value="dummy_path.gif")), \
         patch("bot.handlers.extract_frames_from_video", return_value=[b"frame1"]), \
         patch("bot.handlers.analyze_frames", AsyncMock(return_value="cat dancing")), \
         patch("bot.handlers.save_reusable_media", AsyncMock()) as mock_save_reusable, \
         patch("os.remove", MagicMock()):

        desc, media_id = await handlers.get_message_media_description(
            mock_msg,
            chat_id=12345,
            sender_user_id=67890,
            save_reusable=True,
        )

        assert desc == "[User sent a gif: cat dancing]"
        assert media_id == "gif_u1"
        mock_save_reusable.assert_called_once_with(
            12345, "gif_u1", "gif_f1", "animation", "cat dancing", 67890
        )


@pytest.mark.asyncio
async def test_handle_message_sends_saved_gif_reply(temp_db_path, mock_update_handler, mock_context):
    handlers.chat_history.clear()
    mock_update_handler.message.photo = None
    mock_update_handler.message.sticker = None
    mock_update_handler.message.video = None
    mock_update_handler.message.animation = None
    mock_update_handler.message.document = None

    from bot import memory
    await memory.add_whitelist(12345, "group", 999)
    await memory.save_reusable_media(
        chat_id=12345,
        media_unique_id="gif_u1",
        file_id="gif_f1",
        media_type="animation",
        description="cat dancing",
    )

    with patch("bot.handlers.should_reply", AsyncMock(return_value=True)), \
         patch("bot.handlers.should_react", AsyncMock(return_value=False)), \
         patch("bot.handlers.generate_response", AsyncMock(return_value={
             "tool_calls": [],
             "reply_to_message_id": 999,
             "messages": [{"saved_media_id": "gif_u1"}],
             "polls": [],
         })):

        await handlers.handle_message(mock_update_handler, mock_context)

        mock_context.bot.send_animation.assert_called_once_with(
            chat_id=12345,
            animation="gif_f1",
            reply_to_message_id=999,
        )
        mock_context.bot.send_message.assert_not_called()

        saved = await memory.get_saved_media_by_unique_id(12345, "gif_u1")
        assert saved["use_count"] == 1


def test_llm_promised_research_without_ponder_detects_deferral():
    response = {
        "tool_calls": [],
        "messages": ["щас всё гляну, мой гигачад! ня!"],
    }
    assert handlers._llm_promised_research_without_ponder(response) is True


def test_llm_promised_research_without_ponder_ignores_when_ponder_present():
    response = {
        "tool_calls": [{"name": "ponder", "arguments": {"query": "news"}}],
        "messages": ["щас всё гляну"],
    }
    assert handlers._llm_promised_research_without_ponder(response) is False


def test_llm_promised_research_without_ponder_ignores_normal_reply():
    response = {
        "tool_calls": [],
        "messages": ["I'll set up a quick vote."],
    }
    assert handlers._llm_promised_research_without_ponder(response) is False


def test_derive_ponder_query_passes_through_user_text():
    text = "What's happening in the world today?"
    assert handlers._derive_ponder_query(text) == text


def test_derive_ponder_query_preserves_specific_news_terms():
    text = "Apple news today"
    assert handlers._derive_ponder_query(text) == text


def test_derive_ponder_query_truncates_long_text():
    text = "x" * 600
    assert len(handlers._derive_ponder_query(text)) == 500


def test_derive_ponder_query_falls_back_to_memory_query():
    assert handlers._derive_ponder_query("", "Who invented the telephone?") == "Who invented the telephone?"

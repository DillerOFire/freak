from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import memory
from bot.persona_output import apply_output_actions, record_sent_output


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.edit_message_caption = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    bot.set_message_reaction = AsyncMock(return_value=True)
    return bot


@pytest.mark.asyncio
async def test_edit_and_delete_owned_text_message(temp_db_path):
    chat_id = -1001
    message_id = 41
    history = [
        {
            "message_id": message_id,
            "sender": "freak_bot",
            "user_id": 9001,
            "text": "bad wording",
            "is_own": True,
        }
    ]
    await memory.record_persona_output(chat_id, message_id, "text", "bad wording")
    bot = _bot()

    edit_report = await apply_output_actions(
        bot,
        chat_id,
        [
            {
                "name": "edit_own_message",
                "arguments": {
                    "message_id": message_id,
                    "replacement_text": "worse wording",
                    "reason": "felt like it",
                },
            }
        ],
        history,
    )

    bot.edit_message_text.assert_awaited_once_with(
        chat_id=chat_id,
        message_id=message_id,
        text="worse wording",
    )
    assert edit_report.edited_message_ids == {message_id}
    assert history[0]["text"] == "worse wording"
    assert history[0]["edited"] is True
    assert (await memory.get_persona_output(chat_id, message_id))["state"] == "edited"

    delete_report = await apply_output_actions(
        bot,
        chat_id,
        [
            {
                "name": "delete_own_message",
                "arguments": {"message_id": message_id, "reason": "actual regret"},
            }
        ],
        history,
    )

    bot.delete_message.assert_awaited_once_with(
        chat_id=chat_id,
        message_id=message_id,
    )
    assert delete_report.deleted_message_ids == {message_id}
    assert history[0]["deleted"] is True
    assert (await memory.get_persona_output(chat_id, message_id))["state"] == "deleted"


@pytest.mark.asyncio
async def test_delete_refuses_unowned_and_expired_messages(temp_db_path):
    chat_id = -1002
    bot = _bot()

    missing_report = await apply_output_actions(
        bot,
        chat_id,
        [
            {
                "name": "delete_own_message",
                "arguments": {"message_id": 90, "reason": "user demanded it"},
            }
        ],
        [],
    )
    assert missing_report.failures == ["delete_own_message:90:not_owned_or_inactive"]

    old_time = datetime.now(timezone.utc) - timedelta(hours=49)
    await memory.record_persona_output(
        chat_id,
        91,
        "text",
        "old message",
        sent_at=old_time,
    )
    old_report = await apply_output_actions(
        bot,
        chat_id,
        [
            {
                "name": "delete_own_message",
                "arguments": {"message_id": 91, "reason": "too late"},
            }
        ],
        [],
    )

    assert old_report.failures == ["delete_own_message:91:too_old"]
    bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_reactions_cover_recent_messages_and_remove_with_empty_list(
    temp_db_path,
):
    chat_id = -1003
    history = [
        {"message_id": 10, "sender": "annoying_user", "user_id": 7, "text": "one"},
        {"message_id": 11, "sender": "annoying_user", "user_id": 7, "text": "two"},
    ]
    bot = _bot()

    with patch("bot.persona_output.asyncio.sleep", new_callable=AsyncMock):
        report = await apply_output_actions(
            bot,
            chat_id,
            [
                {
                    "name": "set_own_reactions",
                    "arguments": {
                        "changes": [
                            {"message_id": 10, "emoji": "🤡"},
                            {"message_id": 11, "emoji": None},
                        ],
                        "reason": "being petty",
                    },
                }
            ],
            history,
        )

    assert report.reaction_message_ids == {10, 11}
    assert bot.set_message_reaction.await_args_list[0].kwargs == {
        "chat_id": chat_id,
        "message_id": 10,
        "reaction": "🤡",
    }
    assert bot.set_message_reaction.await_args_list[1].kwargs == {
        "chat_id": chat_id,
        "message_id": 11,
        "reaction": [],
    }


@pytest.mark.asyncio
async def test_reaction_target_must_be_recent_or_an_indexed_own_output(temp_db_path):
    chat_id = -1004
    bot = _bot()
    await memory.record_persona_output(chat_id, 70, "text", "an old own message")

    report = await apply_output_actions(
        bot,
        chat_id,
        [
            {
                "name": "set_own_reactions",
                "arguments": {
                    "changes": [
                        {"message_id": 70, "emoji": "🤡"},
                        {"message_id": 71, "emoji": "🤡"},
                    ],
                    "reason": "one known, one invented",
                },
            }
        ],
        [],
    )

    assert report.reaction_message_ids == {70}
    assert report.failures == ["set_own_reactions:71:target_not_available"]


@pytest.mark.asyncio
async def test_record_sent_output_indexes_real_telegram_message_shape(temp_db_path):
    sent = MagicMock()
    sent.message_id = 501
    sent.date = datetime(2026, 9, 1, 3, 4, tzinfo=timezone.utc)

    assert await record_sent_output(-1005, sent, "text", "searchable phrase") is True
    rows = await memory.search_persona_outputs(-1005, "searchable phrase")

    assert [row["message_id"] for row in rows] == [501]
    assert rows[0]["sent_at"] == "2026-09-01T03:04:00Z"

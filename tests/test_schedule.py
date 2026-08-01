"""Tests for LLM-driven scheduled actions and event states."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import memory, schedule
from bot import logic


@pytest.mark.asyncio
async def test_scheduled_action_crud(temp_db_path):
    execute_at = schedule.to_iso(schedule.utc_now() + timedelta(hours=2))
    action_id = await memory.add_scheduled_action(
        123,
        action_type="reply",
        execute_at=execute_at,
        reason="too tired",
        instruction="answer the question about cats",
        context="user asked about cats",
        target_user_id=99,
        target_username="vasya",
        reply_to_message_id=501,
    )
    assert action_id > 0

    pending = await memory.list_pending_scheduled_actions(123)
    assert len(pending) == 1
    assert pending[0]["reason"] == "too tired"
    assert pending[0]["instruction"] == "answer the question about cats"

    due = await memory.get_due_scheduled_actions(
        schedule.to_iso(schedule.utc_now() + timedelta(hours=3))
    )
    assert any(a["id"] == action_id for a in due)

    assert await memory.claim_scheduled_action(action_id) is True
    assert await memory.claim_scheduled_action(action_id) is False  # already running

    await memory.complete_scheduled_action(action_id, status="done")
    row = await memory.get_scheduled_action(action_id)
    assert row["status"] == "done"
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_cancel_scheduled_action(temp_db_path):
    execute_at = schedule.to_iso(schedule.utc_now() + timedelta(hours=1))
    action_id = await memory.add_scheduled_action(
        1,
        action_type="message",
        execute_at=execute_at,
        reason="later congrats",
        instruction="wish happy birthday",
    )
    assert await memory.cancel_scheduled_action(action_id, 1) is True
    assert await memory.cancel_scheduled_action(action_id, 1) is False
    row = await memory.get_scheduled_action(action_id)
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_event_state_ignore_and_expire(temp_db_path):
    expires = schedule.to_iso(schedule.utc_now() + timedelta(hours=1))
    state_id = await memory.add_event_state(
        50,
        state_key="ignore",
        value="ignoring vasya",
        expires_at=expires,
        reason="was rude",
        target_user_id=77,
        target_username="vasya",
    )
    assert state_id > 0

    ignored, reason = await memory.is_user_ignored(50, 77)
    assert ignored is True
    assert "rude" in (reason or "") or "ignoring" in (reason or "")

    ignored_other, _ = await memory.is_user_ignored(50, 88)
    assert ignored_other is False

    # Chat-wide ignore
    await memory.add_event_state(
        50,
        state_key="ignore",
        value="silent treatment for everyone",
        expires_at=expires,
        reason="chat chaos",
    )
    ignored_all, _ = await memory.is_user_ignored(50, 88)
    assert ignored_all is True

    cleared = await memory.clear_event_state(chat_id=50, state_key="ignore")
    assert cleared >= 1
    ignored_after, _ = await memory.is_user_ignored(50, 77)
    assert ignored_after is False


@pytest.mark.asyncio
async def test_event_state_replaces_same_key_scope(temp_db_path):
    expires = schedule.to_iso(schedule.utc_now() + timedelta(hours=2))
    first = await memory.add_event_state(
        10,
        state_key="mood",
        value="angry",
        expires_at=expires,
        reason="first",
    )
    second = await memory.add_event_state(
        10,
        state_key="mood",
        value="calm",
        expires_at=expires,
        reason="second",
    )
    active = await memory.list_active_event_states(10)
    moods = [s for s in active if s["state_key"] == "mood"]
    assert len(moods) == 1
    assert moods[0]["id"] == second
    assert moods[0]["value"] == "calm"
    old = await memory.get_event_state(first)
    assert old["active"] == 0


def test_parse_when_relative_and_tomorrow():
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    dt = schedule.parse_when("in 30m", now=now)
    assert dt == now + timedelta(minutes=30)

    dt = schedule.parse_when("in 2 hours", now=now)
    assert dt == now + timedelta(hours=2)

    dt = schedule.parse_when("tomorrow", now=now)
    assert dt == now + timedelta(days=1)

    dt = schedule.parse_when("tomorrow 15:00", now=now)
    assert dt.day == 2
    assert dt.hour == 15
    assert dt.minute == 0

    dt = schedule.parse_when("today 18:30", now=now)
    assert dt.hour == 18
    assert dt.minute == 30

    iso = "2026-08-05T10:00:00Z"
    dt = schedule.parse_when(iso, now=now)
    assert dt == datetime(2026, 8, 5, 10, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError):
        schedule.parse_when("not a time", now=now)


def test_parse_when_vague_human_phrases():
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)

    later = schedule.parse_when("later", now=now)
    assert now + timedelta(minutes=15) < later < now + timedelta(hours=2)

    few = schedule.parse_when("in a few hours", now=now)
    assert now + timedelta(hours=1) < few < now + timedelta(hours=6)

    tonight = schedule.parse_when("tonight", now=now)
    assert tonight > now

    bit = schedule.parse_when("in a bit", now=now)
    assert bit > now


def test_humanize_and_jitter():
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert "hour" in schedule.humanize_timedelta(timedelta(hours=-3))
    assert "in" in schedule.humanize_timedelta(timedelta(minutes=45))

    target = now + timedelta(hours=2)
    # Clock-bound phrase: no jitter
    exact = schedule.apply_human_jitter(
        target, now=now, source_when="tomorrow 15:00"
    )
    assert exact == target

    # Relative phrase: may drift (run several times — at least allow equality edge)
    jittered = [
        schedule.apply_human_jitter(target, now=now, source_when="in 2h")
        for _ in range(20)
    ]
    assert any(j != target for j in jittered) or all(
        abs((j - target).total_seconds()) < 3600 for j in jittered
    )
    for j in jittered:
        assert j >= now + schedule.MIN_DELAY


def test_validate_future_dt_bounds():
    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        schedule.validate_future_dt(now + timedelta(seconds=5), now=now)
    with pytest.raises(ValueError):
        schedule.validate_future_dt(now + timedelta(days=40), now=now)
    ok = schedule.validate_future_dt(now + timedelta(hours=1), now=now)
    assert ok > now


@pytest.mark.asyncio
async def test_schedule_action_from_args(temp_db_path):
    result = await schedule.schedule_action_from_args(
        999,
        {
            "action_type": "reply",
            "when": "in 1h",
            "reason": "busy",
            "instruction": "reply about the bug",
        },
        focus_message_id=42,
        focus_user_id=7,
        focus_username="alice",
        focus_text="fix my bug please",
    )
    assert result["id"] > 0
    row = await memory.get_scheduled_action(result["id"])
    assert row["target_user_id"] == 7
    assert row["reply_to_message_id"] == 42
    assert "bug" in (row["context"] or "")


@pytest.mark.asyncio
async def test_set_event_state_from_args(temp_db_path):
    result = await schedule.set_event_state_from_args(
        5,
        {
            "state_key": "angry",
            "value": "very angry at the group",
            "until": "in 2h",
            "reason": "spam",
        },
    )
    assert result["id"] > 0
    states = await memory.list_active_event_states(5)
    assert any(s["state_key"] == "angry" for s in states)


@pytest.mark.asyncio
async def test_should_reply_soft_ignore_blocks_spontaneous_allows_mention(
    temp_db_path, mock_update
):
    expires = schedule.to_iso(schedule.utc_now() + timedelta(hours=1))
    await memory.add_event_state(
        12345,
        state_key="ignore",
        value="ignoring",
        expires_at=expires,
        target_user_id=67890,
    )
    mock_update.message.from_user.id = 67890
    mock_update.message.from_user.is_bot = False
    mock_update.message.chat.type = "group"
    mock_update.message.reply_to_message = None
    logic.messages_since_last_reply[12345] = 100  # past cooldown

    with (
        patch.object(logic, "ADMIN_ID", 1),
        patch("bot.logic.get_logic_config", new_callable=AsyncMock) as mock_config,
        patch("random.random", return_value=0.0),  # would hit chance if not ignored
    ):
        mock_config.return_value = (0, 1.0, 0.0)

        # Spontaneous chatter while ignored → no
        mock_update.message.text = "hey anyone around"
        assert (
            await logic.should_reply(mock_update.message, "@test_bot", 12345) is False
        )

        # Direct @mention still reaches the LLM (soft ignore — model may stay silent)
        mock_update.message.text = "hey @test_bot"
        assert (
            await logic.should_reply(mock_update.message, "@test_bot", 12345) is True
        )


@pytest.mark.asyncio
async def test_should_reply_admin_bypasses_ignore(temp_db_path, mock_update):
    expires = schedule.to_iso(schedule.utc_now() + timedelta(hours=1))
    await memory.add_event_state(
        12345,
        state_key="ignore",
        value="silent all",
        expires_at=expires,
    )
    mock_update.message.text = "hey @test_bot"
    mock_update.message.from_user.id = 999
    mock_update.message.from_user.is_bot = False
    mock_update.message.chat.type = "group"
    mock_update.message.reply_to_message = None

    with patch.object(logic, "ADMIN_ID", 999):
        reply = await logic.should_reply(mock_update.message, "@test_bot", 12345)
    assert reply is True


@pytest.mark.asyncio
async def test_process_due_scheduled_actions_executes(temp_db_path):
    execute_at = schedule.to_iso(schedule.utc_now() - timedelta(seconds=5))
    action_id = await memory.add_scheduled_action(
        321,
        action_type="message",
        execute_at=execute_at,
        reason="check-in",
        instruction="say hi to the chat",
    )

    app = MagicMock()
    app.bot.username = "test_bot"

    with patch(
        "bot.schedule._execute_scheduled_action", new_callable=AsyncMock
    ) as mock_exec:
        mock_exec.return_value = None
        processed = await schedule.process_due_scheduled_actions(app)

    assert processed == 1
    mock_exec.assert_awaited_once()
    row = await memory.get_scheduled_action(action_id)
    assert row["status"] == "done"


@pytest.mark.asyncio
async def test_generate_response_schedule_tools(temp_db_path):
    import json
    from bot import llm

    mock_response = MagicMock()
    mock_response.usage = None
    mock_choice = MagicMock()
    mock_message = MagicMock()
    mock_message.content = json.dumps(
        {
            "tool_calls": [
                {
                    "name": "schedule_action",
                    "arguments": {
                        "action_type": "reply",
                        "when": "in 1h",
                        "reason": "will answer later",
                        "instruction": "answer about dinner plans",
                    },
                },
                {
                    "name": "set_event_state",
                    "arguments": {
                        "state_key": "mood",
                        "value": "busy",
                        "until": "in 1h",
                        "reason": "cooking",
                    },
                },
            ],
            "reply_to_message_id": 1,
            "messages": ["later"],
            "polls": [],
        }
    )
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]

    messages_context = [
        {
            "message_id": 1,
            "sender": "Alice",
            "user_id": 123,
            "text": "what about dinner?",
        }
    ]

    with patch.object(
        llm.client.chat.completions, "create", AsyncMock(return_value=mock_response)
    ):
        result = await llm.generate_response(
            messages_context=messages_context,
            user_thoughts={},
            general_memories=[],
            chat_id=555,
            focus_message_id=1,
        )

    assert result is not None
    assert result["messages"] == ["later"]
    pending = await memory.list_pending_scheduled_actions(555)
    assert len(pending) == 1
    assert pending[0]["reason"] == "will answer later"
    states = await memory.list_active_event_states(555)
    assert any(s["state_key"] == "mood" for s in states)


def test_build_context_includes_schedules_and_states():
    from bot import llm

    actions = [
        {
            "id": 1,
            "action_type": "reply",
            "execute_at": "2026-08-02T10:00:00Z",
            "reason": "tired",
            "instruction": "answer later",
            "context": "quantum q",
        }
    ]
    states = [
        {
            "id": 2,
            "state_key": "angry",
            "value": "very angry",
            "expires_at": "2026-08-02T00:00:00Z",
            "reason": "spam",
            "target_user_id": None,
            "target_username": None,
        }
    ]
    prompt = llm.build_context_prompt(
        [],
        {},
        [],
        pending_scheduled_actions=actions,
        active_event_states=states,
    )
    assert "<pending_scheduled_actions>" in prompt
    assert "tired" in prompt
    assert "<active_event_states>" in prompt
    assert "very angry" in prompt

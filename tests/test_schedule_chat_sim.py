"""
Realistic multi-turn chat simulations for LLM scheduling & event states.

Uses ChatSimulator: real handlers/logic/DB/tool application, scripted OpenAI.
"""

from datetime import timedelta

import pytest

from bot import memory, schedule
from tests.chat_sim import ChatSimulator, default_users


CHAT_ID = -1005550001
ADMIN_ID = 100


@pytest.fixture
async def sim_chat(temp_db_path):
    """Whitelisted group chat with real DB + ChatSimulator."""
    await memory.add_whitelist(CHAT_ID, "group", ADMIN_ID)
    sim = ChatSimulator(chat_id=CHAT_ID, admin_id=ADMIN_ID)
    users = default_users(ADMIN_ID)
    return sim, users


@pytest.mark.asyncio
async def test_sim_defer_reply_until_later_with_context(sim_chat):
    """
    Vasya asks a long homework question late.
    Bot: short brush-off + schedule_action(reply) with reason/instruction + tired mood.
    Later poller fires → bot answers with awareness of the delay.
    """
    sim, users = sim_chat
    vasya = users["vasya"]

    # Turn 1 — bot defers
    sim.script_llm(
        {
            "tool_calls": [
                {
                    "name": "schedule_action",
                    "arguments": {
                        "action_type": "reply",
                        "when": "in 1h",
                        "reason": "Too long a request late at night; answer when fresher",
                        "instruction": (
                            "Explain quantum computing basics to Vasya briefly and "
                            "acknowledge you put it off because it was late"
                        ),
                        "context": "Vasya asked for a detailed quantum computing lecture",
                    },
                },
                {
                    "name": "set_event_state",
                    "arguments": {
                        "state_key": "mood",
                        "value": "tired, not in lecture mode",
                        "until": "in 2h",
                        "reason": "homework dump at a bad time",
                    },
                },
            ],
            "reply_to_message_id": None,  # filled after we know msg id — OK null
            "messages": ["Not now. I'll get back to you later."],
            "polls": [],
        }
    )

    msg_id = await sim.user_says(
        vasya,
        "Explain quantum computing to me in detail right now, with examples.",
        force_reply=True,
        mention_bot=True,
    )

    assert "Not now" in sim.texts_sent()[0] or "later" in sim.texts_sent()[0].lower()

    pending = await memory.list_pending_scheduled_actions(CHAT_ID)
    assert len(pending) == 1
    action = pending[0]
    assert action["action_type"] == "reply"
    assert "late" in action["reason"].lower() or "fresher" in action["reason"].lower()
    assert "quantum" in action["instruction"].lower()
    # Auto-context or explicit context preserved
    assert action["context"]
    assert action["target_user_id"] == vasya.user_id
    assert action["reply_to_message_id"] == msg_id

    states = await memory.list_active_event_states(CHAT_ID)
    assert any(s["state_key"] == "mood" and "tired" in s["value"] for s in states)

    # Make the action due immediately
    past = schedule.to_iso(schedule.utc_now() - timedelta(seconds=10))
    async with __import__("aiosqlite").connect(memory.DB_NAME) as db:
        await db.execute(
            "UPDATE scheduled_actions SET execute_at = ? WHERE id = ?",
            (past, action["id"]),
        )
        await db.commit()

    sim.clear_outbound()

    # Turn 2 — scheduled fire: LLM composes the delayed answer
    sim.script_llm(
        {
            "tool_calls": [],
            "reply_to_message_id": msg_id,
            "messages": [
                "Ok, about quantum computing — short version since I put this off last night.",
                "Qubits can be in superposition; entanglement links them. That's the core idea.",
            ],
            "polls": [],
        }
    )

    processed = await sim.fire_due_actions()
    assert processed == 1

    texts = sim.texts_sent()
    assert len(texts) == 2
    assert any("quantum" in t.lower() for t in texts)
    assert any(
        "put this off" in t.lower() or "last night" in t.lower() or "short" in t.lower()
        for t in texts
    )

    done = await memory.get_scheduled_action(action["id"])
    assert done["status"] == "done"

    # LLM prompt for the scheduled turn must include original reason/context
    assert sim.llm_calls, "expected an LLM call for the scheduled action"
    last_user_content = ""
    for msg in sim.llm_calls[-1]["messages"]:
        if msg.get("role") == "user":
            last_user_content = str(msg.get("content") or "")
    combined = last_user_content.lower()
    assert (
        "why_you_waited" in combined
        or "late" in combined
        or "fresher" in combined
        or "returning_to_something" in combined
        or "private_plan" in combined
    )
    assert "quantum" in combined


@pytest.mark.asyncio
async def test_sim_ignore_user_then_admin_still_gets_through(sim_chat):
    """
    Soft ignore: spontaneous chatter is blocked; direct pings still hit the LLM
    (which usually stays silent). Admin still gets real replies.
    """
    sim, users = sim_chat
    vasya = users["vasya"]
    admin = users["admin"]

    # Bot sets ignore on vasya
    sim.script_llm(
        {
            "tool_calls": [
                {
                    "name": "set_event_state",
                    "arguments": {
                        "state_key": "ignore",
                        "value": "ignoring vasya for spam",
                        "until": "in 1h",
                        "reason": "kept pinging with nonsense",
                        "target_user_id": vasya.user_id,
                        "target_username": vasya.username,
                    },
                }
            ],
            "reply_to_message_id": None,
            "messages": ["I'm done with this for a bit."],
            "polls": [],
        }
    )
    await sim.user_says(vasya, "hey hey hey answer me!!!", force_reply=True, mention_bot=True)
    assert sim.texts_sent()
    sim.clear_outbound()

    ignored, reason = await memory.is_user_ignored(CHAT_ID, vasya.user_id)
    assert ignored is True
    assert reason

    # Spontaneous (no @) while ignored → no LLM
    llm_before = len(sim.llm_calls)
    await sim.user_says(
        vasya,
        "anyone here?",
        force_reply=False,
        mention_bot=False,
    )
    assert sim.texts_sent() == []
    assert len(sim.llm_calls) == llm_before

    # Direct @mention: soft-ignore lets LLM choose — script cold silence
    sim.script_llm(
        {
            "tool_calls": [],
            "reply_to_message_id": None,
            "messages": [],
            "polls": [],
        }
    )
    await sim.user_says(
        vasya,
        "seriously reply now",
        force_reply=False,
        mention_bot=True,
    )
    assert sim.texts_sent() == []
    assert len(sim.llm_calls) == llm_before + 1  # saw the ping, chose silence
    # Prompt should surface the ignore state
    last_prompt = str(sim.llm_calls[-1]["messages"][-1].get("content") or "")
    assert "ignore" in last_prompt.lower() or "active_event_states" in last_prompt

    # Admin still gets through
    sim.script_llm(
        {
            "tool_calls": [],
            "reply_to_message_id": None,
            "messages": ["Yes boss, still here."],
            "polls": [],
        }
    )
    await sim.user_says(
        admin,
        "status?",
        force_reply=False,
        mention_bot=True,
    )
    assert any("boss" in t.lower() or "here" in t.lower() for t in sim.texts_sent())


@pytest.mark.asyncio
async def test_sim_chat_wide_ignore_blocks_everyone_except_admin(sim_chat):
    sim, users = sim_chat
    vasya = users["vasya"]
    petya = users["petya"]
    admin = users["admin"]

    expires = schedule.to_iso(schedule.utc_now() + timedelta(hours=1))
    await memory.add_event_state(
        CHAT_ID,
        state_key="ignore",
        value="silent treatment for the whole chat",
        expires_at=expires,
        reason="group was toxic",
    )

    # Spontaneous noise (no mention) while chat-wide ignore is on
    await sim.user_says(vasya, "hello?", force_reply=False, mention_bot=False)
    await sim.user_says(petya, "anyone home?", force_reply=False, mention_bot=False)
    assert sim.texts_sent() == []

    # Mentions still reach LLM; model elects silence
    sim.script_llm(
        {"tool_calls": [], "messages": [], "polls": []},
        {"tool_calls": [], "messages": [], "polls": []},
    )
    await sim.user_says(vasya, "hello?", force_reply=False, mention_bot=True)
    await sim.user_says(petya, "anyone home?", force_reply=False, mention_bot=True)
    assert sim.texts_sent() == []

    sim.script_llm(
        {
            "tool_calls": [],
            "reply_to_message_id": None,
            "messages": ["Admin override, I'm listening."],
            "polls": [],
        }
    )
    await sim.user_says(admin, "wake up", force_reply=False, mention_bot=True)
    assert any("Admin" in t or "listening" in t for t in sim.texts_sent())


@pytest.mark.asyncio
async def test_sim_birthday_memory_and_scheduled_congrats(sim_chat):
    """
    Petya shares birthday → bot stores memory + schedules congrats.
    When due, sends a natural congratulations message.
    """
    sim, users = sim_chat
    petya = users["petya"]

    sim.script_llm(
        {
            "tool_calls": [
                {
                    "name": "update_user_thought",
                    "arguments": {
                        "user_id": petya.user_id,
                        "username": petya.username,
                        "thought": "Birthday is March 15.",
                    },
                },
                {
                    "name": "add_general_memory",
                    "arguments": {
                        "topic": "Petya birthday",
                        "summary": "Petya's birthday is March 15.",
                        "importance": 4,
                    },
                },
                {
                    "name": "schedule_action",
                    "arguments": {
                        "action_type": "message",
                        "when": "in 45m",
                        "reason": "Celebrate Petya's birthday when the moment feels right",
                        "instruction": "Wish Petya a happy birthday in a natural group-chat way",
                        "target_user_id": petya.user_id,
                        "target_username": petya.username,
                    },
                },
            ],
            "reply_to_message_id": None,
            "messages": ["Got it — March 15. I'll remember."],
            "polls": [],
        }
    )

    await sim.user_says(
        petya,
        "btw my birthday is March 15",
        force_reply=True,
        mention_bot=True,
    )

    thought = await memory.get_user_thought(petya.user_id)
    assert "March 15" in thought

    memories = await memory.get_general_memories(CHAT_ID, limit=5)
    assert any("birthday" in m.lower() and "march 15" in m.lower() for m in memories)

    pending = await memory.list_pending_scheduled_actions(CHAT_ID)
    assert len(pending) == 1
    assert pending[0]["action_type"] == "message"
    assert "birthday" in pending[0]["instruction"].lower()

    # Fire congrats
    past = schedule.to_iso(schedule.utc_now() - timedelta(seconds=5))
    async with __import__("aiosqlite").connect(memory.DB_NAME) as db:
        await db.execute(
            "UPDATE scheduled_actions SET execute_at = ? WHERE id = ?",
            (past, pending[0]["id"]),
        )
        await db.commit()

    sim.clear_outbound()
    sim.script_llm(
        {
            "tool_calls": [],
            "reply_to_message_id": None,
            "messages": ["Happy birthday Petya! 🎂 Have a good one."],
            "polls": [],
        }
    )
    processed = await sim.fire_due_actions()
    assert processed == 1
    assert any("birthday" in t.lower() and "petya" in t.lower() for t in sim.texts_sent())


@pytest.mark.asyncio
async def test_sim_delayed_research_then_reply(sim_chat):
    """
    Bot schedules research for later (not immediate ponder).
    When due: ponder runs, then LLM composes reply from results.
    """
    sim, users = sim_chat
    kolya = users["kolya"]

    sim.script_llm(
        {
            "tool_calls": [
                {
                    "name": "schedule_action",
                    "arguments": {
                        "action_type": "research",
                        "when": "in 30m",
                        "reason": "Want to dig into this properly later, not half-ass it now",
                        "instruction": "Research latest SpaceX launch status and summarize for the group",
                        "context": "Kolya asked what's up with the next Falcon launch",
                    },
                }
            ],
            "reply_to_message_id": None,
            "messages": ["I'll dig into that in a bit and report back."],
            "polls": [],
        }
    )
    await sim.user_says(
        kolya,
        "what's the deal with the next falcon launch?",
        force_reply=True,
        mention_bot=True,
    )
    assert any("report back" in t.lower() or "dig" in t.lower() for t in sim.texts_sent())

    pending = await memory.list_pending_scheduled_actions(CHAT_ID)
    assert pending[0]["action_type"] == "research"

    past = schedule.to_iso(schedule.utc_now() - timedelta(seconds=5))
    async with __import__("aiosqlite").connect(memory.DB_NAME) as db:
        await db.execute(
            "UPDATE scheduled_actions SET execute_at = ? WHERE id = ?",
            (past, pending[0]["id"]),
        )
        await db.commit()

    sim.clear_outbound()
    sim.script_ponder(
        "Falcon 9 is stacking at pad 39A; NET window is Friday evening local time. Source: spacex.com"
    )
    sim.script_llm(
        {
            "tool_calls": [],
            "reply_to_message_id": None,
            "messages": [
                "Looked it up: Falcon 9 stacking at 39A, window Friday evening. Not confirmed scrub-free yet."
            ],
            "polls": [],
        }
    )

    processed = await sim.fire_due_actions()
    assert processed == 1
    assert sim.ponder_calls, "research action should invoke ponder"
    assert any("Falcon" in t or "39A" in t or "Friday" in t for t in sim.texts_sent())


@pytest.mark.asyncio
async def test_sim_angry_mood_injected_into_next_prompt(sim_chat):
    """Soft mood does not block replies but appears in the next LLM context."""
    sim, users = sim_chat
    vasya = users["vasya"]

    expires = schedule.to_iso(schedule.utc_now() + timedelta(hours=3))
    await memory.add_event_state(
        CHAT_ID,
        state_key="angry",
        value="very angry at the group spam",
        expires_at=expires,
        reason="three people dumped walls of text",
    )

    sim.script_llm(
        {
            "tool_calls": [],
            "reply_to_message_id": None,
            "messages": ["What."],
            "polls": [],
        }
    )
    await sim.user_says(vasya, "you good?", force_reply=True, mention_bot=True)

    assert sim.texts_sent() == ["What."]
    assert sim.llm_calls
    user_prompt = ""
    for msg in sim.llm_calls[0]["messages"]:
        if msg.get("role") == "user":
            user_prompt = str(msg.get("content") or "")
    assert "<active_event_states>" in user_prompt
    assert "angry" in user_prompt
    assert "spam" in user_prompt.lower() or "walls of text" in user_prompt.lower()


@pytest.mark.asyncio
async def test_sim_cancel_scheduled_action_mid_conversation(sim_chat):
    """Bot schedules a follow-up, then cancels it when the user says never mind."""
    sim, users = sim_chat
    vasya = users["vasya"]

    sim.script_llm(
        {
            "tool_calls": [
                {
                    "name": "schedule_action",
                    "arguments": {
                        "action_type": "reply",
                        "when": "in 2h",
                        "reason": "need time to think about restaurant pick",
                        "instruction": "Suggest a dinner place for Vasya",
                    },
                }
            ],
            "messages": ["I'll think about dinner options and ping you."],
            "polls": [],
        }
    )
    await sim.user_says(vasya, "where should we eat?", force_reply=True, mention_bot=True)
    pending = await memory.list_pending_scheduled_actions(CHAT_ID)
    assert len(pending) == 1
    action_id = pending[0]["id"]

    sim.clear_outbound()
    sim.script_llm(
        {
            "tool_calls": [
                {
                    "name": "cancel_scheduled_action",
                    "arguments": {"action_id": action_id},
                }
            ],
            "messages": ["Alright, cancelled — pick whatever you want."],
            "polls": [],
        }
    )
    await sim.user_says(vasya, "never mind, figured it out", force_reply=True, mention_bot=True)

    row = await memory.get_scheduled_action(action_id)
    assert row["status"] == "cancelled"
    assert any("cancel" in t.lower() for t in sim.texts_sent())

    # Nothing left to fire
    past = schedule.to_iso(schedule.utc_now() - timedelta(seconds=5))
    async with __import__("aiosqlite").connect(memory.DB_NAME) as db:
        await db.execute(
            "UPDATE scheduled_actions SET execute_at = ? WHERE id = ?",
            (past, action_id),
        )
        await db.commit()
    processed = await sim.fire_due_actions()
    assert processed == 0


@pytest.mark.asyncio
async def test_sim_multi_user_conversation_history_and_defer(sim_chat):
    """
    Multi-user banter, then a focused @mention that the bot deliberately ignores
    with a deferred reply (silence now).
    """
    sim, users = sim_chat
    vasya, petya = users["vasya"], users["petya"]

    # Background chatter that shouldn't schedule anything
    sim.script_llm(
        {
            "tool_calls": [],
            "messages": [],
            "polls": [],
        }
    )
    await sim.user_says(vasya, "weather is awful today", force_reply=True)
    # empty messages → generate_response returns None after tools; nothing sent
    # (status no_reply). outbound may be empty.

    sim.script_llm(
        {
            "tool_calls": [
                {
                    "name": "schedule_action",
                    "arguments": {
                        "action_type": "reply",
                        "when": "tomorrow 10:00",
                        "reason": "Petya dumped a huge career advice ask; better answer in the morning",
                        "instruction": "Give Petya practical career advice about switching to backend, concise",
                    },
                },
                {
                    "name": "set_event_state",
                    "arguments": {
                        "state_key": "mood",
                        "value": "not taking career counseling requests tonight",
                        "until": "tomorrow 09:00",
                        "reason": "heavy advice request after hours",
                    },
                },
            ],
            "messages": [],  # pure silence now — human-like
            "polls": [],
        }
    )
    msg_id = await sim.user_says(
        petya,
        "should I quit my job and learn backend? long story: ...",
        force_reply=True,
        mention_bot=True,
    )

    # Silence now
    assert sim.texts_sent() == []

    pending = await memory.list_pending_scheduled_actions(CHAT_ID)
    assert len(pending) == 1
    assert pending[0]["reply_to_message_id"] == msg_id
    assert "career" in pending[0]["instruction"].lower() or "backend" in pending[0][
        "instruction"
    ].lower()

    # History should include both users
    history = list(handlers_history(sim.chat_id))
    senders = {h["sender"] for h in history}
    assert "vasya" in senders
    assert "petya" in senders


def handlers_history(chat_id: int):
    from bot import handlers

    return handlers.chat_history.get(chat_id, [])

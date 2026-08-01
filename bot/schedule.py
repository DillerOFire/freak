"""LLM-driven scheduled actions and time parsing helpers."""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from bot.memory import (
    MAX_ACTIVE_EVENT_STATES_PER_CHAT,
    MAX_PENDING_SCHEDULED_ACTIONS_PER_CHAT,
    SCHEDULED_ACTION_TYPES,
    add_event_state,
    add_scheduled_action,
    cancel_scheduled_action,
    clear_event_state,
    complete_scheduled_action,
    count_active_event_states,
    count_pending_scheduled_actions,
    claim_scheduled_action,
    get_due_scheduled_actions,
    expire_event_states,
    list_active_event_states,
    list_pending_scheduled_actions,
)

# Relative durations: "in 30m", "in 2 hours", "after 1 day"
_RELATIVE_RE = re.compile(
    r"^\s*(?:in|after)\s+(\d+)\s*"
    r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\s*$",
    re.IGNORECASE,
)
# "tomorrow", "tomorrow 15:00", "tomorrow at 3pm"
_TOMORROW_RE = re.compile(
    r"^\s*tomorrow(?:\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$",
    re.IGNORECASE,
)
_TOMORROW_BARE_RE = re.compile(r"^\s*tomorrow\s*$", re.IGNORECASE)
# "today 18:30", "today at 6pm"
_TODAY_RE = re.compile(
    r"^\s*today(?:\s+(?:at\s+)?)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$",
    re.IGNORECASE,
)
# Vague human phrases
_LATER_RE = re.compile(r"^\s*(later|in a bit|in a while|soon)\s*$", re.IGNORECASE)
_FEW_HOURS_RE = re.compile(r"^\s*(in a few hours|in some hours)\s*$", re.IGNORECASE)
_TONIGHT_RE = re.compile(r"^\s*(tonight|this evening)\s*$", re.IGNORECASE)
_THIS_MORNING_RE = re.compile(r"^\s*this morning\s*$", re.IGNORECASE)
_NEXT_WEEK_RE = re.compile(r"^\s*next week\s*$", re.IGNORECASE)

MIN_DELAY = timedelta(seconds=30)
MAX_DELAY = timedelta(days=30)
MAX_STATE_DURATION = timedelta(days=30)
# How far past an original message we still prefer reply-threading
REPLY_THREAD_MAX_AGE = timedelta(hours=18)
# Human timing is imprecise — jitter relative delays (not clock-time targets)
JITTER_FRACTION = 0.18
JITTER_MAX = timedelta(hours=2)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _apply_ampm(hour: int, minute: int, ampm: str | None) -> tuple[int, int]:
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Invalid time of day")
    return hour, minute


def _is_relative_when(text: str) -> bool:
    """True when the phrase is a fuzzy duration (eligible for human jitter)."""
    return bool(
        _RELATIVE_RE.match(text)
        or _LATER_RE.match(text)
        or _FEW_HOURS_RE.match(text)
        or _TOMORROW_BARE_RE.match(text)
        or _NEXT_WEEK_RE.match(text)
    )


def apply_human_jitter(
    dt: datetime,
    *,
    now: datetime | None = None,
    source_when: str | None = None,
) -> datetime:
    """
    Nudge relative delays so fire times aren't stopwatch-precise.
    Clock-bound phrases (tomorrow 15:00, ISO) stay exact.
    """
    now = now or utc_now()
    if source_when and not _is_relative_when(source_when):
        return dt
    delay = dt - now
    if delay <= MIN_DELAY:
        return dt
    max_jitter = min(delay * JITTER_FRACTION, JITTER_MAX)
    # Bias slightly late more often than early (people procrastinate)
    offset = timedelta(seconds=random.uniform(-0.4 * max_jitter.total_seconds(),
                                              max_jitter.total_seconds()))
    jittered = dt + offset
    if jittered < now + MIN_DELAY:
        jittered = now + MIN_DELAY
    return jittered


def humanize_timedelta(delta: timedelta) -> str:
    """Compact human phrase for elapsed / remaining time."""
    seconds = int(abs(delta).total_seconds())
    if seconds < 60:
        return "just now" if delta <= timedelta(0) else "in under a minute"
    minutes = seconds // 60
    if minutes < 60:
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit} ago" if delta < timedelta(0) else f"in {minutes} {unit}"
    hours = minutes // 60
    if hours < 48:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago" if delta < timedelta(0) else f"in about {hours} {unit}"
    days = hours // 24
    unit = "day" if days == 1 else "days"
    return f"{days} {unit} ago" if delta < timedelta(0) else f"in about {days} {unit}"


def humanize_when(iso_ts: str, *, now: datetime | None = None) -> str:
    """Turn stored ISO into a relative phrase for LLM context."""
    now = now or utc_now()
    dt = parse_iso(iso_ts)
    if not dt:
        return iso_ts
    return humanize_timedelta(dt - now)


def parse_when(when: str, *, now: datetime | None = None) -> datetime:
    """
    Parse a human-ish when string into a UTC datetime.

    Accepts:
    - ISO-8601: 2026-08-02T15:00:00Z
    - Relative: in 30m, in 2 hours, after 1 day
    - Vague: later, in a bit, soon, in a few hours, tonight, this evening, next week
    - tomorrow / tomorrow 15:00 / tomorrow at 3pm
    - today 18:30
    """
    if not when or not str(when).strip():
        raise ValueError("when is required")
    text = str(when).strip()
    now = now or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    iso_dt = parse_iso(text)
    if iso_dt is not None:
        return iso_dt

    m = _RELATIVE_RE.match(text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        if unit in ("s", "sec", "secs", "second", "seconds"):
            delta = timedelta(seconds=amount)
        elif unit in ("m", "min", "mins", "minute", "minutes"):
            delta = timedelta(minutes=amount)
        elif unit in ("h", "hr", "hrs", "hour", "hours"):
            delta = timedelta(hours=amount)
        elif unit in ("d", "day", "days"):
            delta = timedelta(days=amount)
        else:
            delta = timedelta(weeks=amount)
        return now + delta

    if _LATER_RE.match(text):
        # "later" / "in a bit" — somewhere in the next 20–90 minutes
        return now + timedelta(minutes=random.randint(20, 90))

    if _FEW_HOURS_RE.match(text):
        return now + timedelta(hours=random.randint(2, 5))

    if _TONIGHT_RE.match(text):
        # Aim for evening window 19:00–22:30 local-as-UTC (bot has no TZ config)
        target = now.replace(hour=20, minute=random.randint(0, 59), second=0, microsecond=0)
        if target <= now + MIN_DELAY:
            target = now + timedelta(hours=random.randint(1, 3))
        return target

    if _THIS_MORNING_RE.match(text):
        target = now.replace(hour=10, minute=random.randint(0, 40), second=0, microsecond=0)
        if target <= now + MIN_DELAY:
            # Already afternoon — treat as tomorrow morning
            target = (now + timedelta(days=1)).replace(
                hour=10, minute=random.randint(0, 40), second=0, microsecond=0
            )
        return target

    if _NEXT_WEEK_RE.match(text):
        return now + timedelta(days=random.randint(6, 9), hours=random.randint(0, 8))

    if _TOMORROW_BARE_RE.match(text):
        # Same clock-ish time next day, not a sharp anniversary of the second
        return now + timedelta(days=1)

    m = _TOMORROW_RE.match(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        hour, minute = _apply_ampm(hour, minute, m.group(3))
        target = (now + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return target

    m = _TODAY_RE.match(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        hour, minute = _apply_ampm(hour, minute, m.group(3))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            raise ValueError("today time is already in the past; use tomorrow or a relative delay")
        return target

    raise ValueError(
        "Unrecognized when format. Use ISO time, 'in 30m', 'in 2h', 'later', "
        "'tonight', 'tomorrow', or 'tomorrow 15:00'."
    )


def validate_future_dt(
    dt: datetime,
    *,
    now: datetime | None = None,
    min_delay: timedelta = MIN_DELAY,
    max_delay: timedelta = MAX_DELAY,
) -> datetime:
    now = now or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if dt < now + min_delay:
        raise ValueError(f"Time must be at least {int(min_delay.total_seconds())}s in the future")
    if dt > now + max_delay:
        raise ValueError(f"Time must be within {max_delay.days} days")
    return dt.astimezone(timezone.utc)


def format_scheduled_action(action: dict) -> str:
    parts = [
        f"id={action['id']}",
        f"type={action['action_type']}",
        f"at={action['execute_at']}",
        f"reason={action.get('reason') or ''}",
    ]
    if action.get("target_username"):
        parts.append(f"user={action['target_username']}")
    elif action.get("target_user_id"):
        parts.append(f"user_id={action['target_user_id']}")
    if action.get("reply_to_message_id"):
        parts.append(f"reply_to={action['reply_to_message_id']}")
    instr = (action.get("instruction") or "")[:200]
    parts.append(f"instruction={instr}")
    return "; ".join(parts)


def format_event_state(state: dict) -> str:
    parts = [
        f"id={state['id']}",
        f"key={state['state_key']}",
        f"value={state.get('value') or ''}",
        f"until={state['expires_at']}",
    ]
    if state.get("target_username"):
        parts.append(f"user={state['target_username']}")
    elif state.get("target_user_id"):
        parts.append(f"user_id={state['target_user_id']}")
    if state.get("reason"):
        parts.append(f"reason={state['reason']}")
    return "; ".join(parts)


async def schedule_action_from_args(
    chat_id: int,
    args: dict[str, Any],
    *,
    focus_message_id: int | None = None,
    focus_user_id: int | None = None,
    focus_username: str | None = None,
    focus_text: str | None = None,
) -> dict[str, Any]:
    """Create a scheduled action from LLM tool arguments."""
    action_type = str(args.get("action_type") or "reply").strip().lower()
    if action_type not in SCHEDULED_ACTION_TYPES:
        raise ValueError(
            f"action_type must be one of: {', '.join(sorted(SCHEDULED_ACTION_TYPES))}"
        )

    pending = await count_pending_scheduled_actions(chat_id)
    if pending >= MAX_PENDING_SCHEDULED_ACTIONS_PER_CHAT:
        raise ValueError(
            f"Too many pending scheduled actions ({pending}/{MAX_PENDING_SCHEDULED_ACTIONS_PER_CHAT})"
        )

    when_raw = str(args.get("when") or args.get("execute_at") or "").strip()
    now = utc_now()
    execute_dt = validate_future_dt(parse_when(when_raw, now=now), now=now)
    execute_dt = apply_human_jitter(execute_dt, now=now, source_when=when_raw)
    execute_dt = validate_future_dt(execute_dt, now=now)
    reason = str(args.get("reason") or "").strip()
    instruction = str(args.get("instruction") or "").strip()
    if not reason:
        raise ValueError("reason is required (why you are scheduling this)")
    if not instruction:
        raise ValueError("instruction is required (what to do when it fires)")

    context = str(args.get("context") or "").strip()
    if not context and focus_text:
        context = (
            f"Original focused message from {focus_username or focus_user_id}: "
            f"{focus_text[:1500]}"
        )

    target_user_id = args.get("target_user_id")
    if target_user_id is not None:
        target_user_id = int(target_user_id)
    elif action_type == "reply" and focus_user_id is not None:
        target_user_id = focus_user_id

    target_username = args.get("target_username")
    if target_username is not None:
        target_username = str(target_username)
    elif action_type == "reply" and focus_username:
        target_username = focus_username

    reply_to = args.get("reply_to_message_id")
    if reply_to is not None:
        reply_to = int(reply_to)
    elif action_type == "reply" and focus_message_id is not None:
        reply_to = focus_message_id

    action_id = await add_scheduled_action(
        chat_id,
        action_type=action_type,
        execute_at=to_iso(execute_dt),
        reason=reason,
        instruction=instruction,
        context=context or None,
        target_user_id=target_user_id,
        target_username=target_username,
        reply_to_message_id=reply_to,
    )
    return {
        "id": action_id,
        "action_type": action_type,
        "execute_at": to_iso(execute_dt),
        "reason": reason,
        "instruction": instruction,
    }


async def set_event_state_from_args(chat_id: int, args: dict[str, Any]) -> dict[str, Any]:
    active = await count_active_event_states(chat_id)
    if active >= MAX_ACTIVE_EVENT_STATES_PER_CHAT:
        raise ValueError(
            f"Too many active event states ({active}/{MAX_ACTIVE_EVENT_STATES_PER_CHAT})"
        )

    state_key = str(args.get("state_key") or args.get("key") or "").strip().lower()
    value = str(args.get("value") or "").strip()
    reason = str(args.get("reason") or "").strip() or None
    if not state_key:
        raise ValueError("state_key is required (e.g. mood, ignore, angry)")
    if not value:
        raise ValueError("value is required (description of the state)")

    when = args.get("until") or args.get("expires_at") or args.get("when")
    if not when:
        raise ValueError("until is required (when this state ends)")
    when_raw = str(when).strip()
    now = utc_now()
    expires_dt = validate_future_dt(
        parse_when(when_raw, now=now),
        now=now,
        min_delay=timedelta(seconds=30),
        max_delay=MAX_STATE_DURATION,
    )
    expires_dt = apply_human_jitter(expires_dt, now=now, source_when=when_raw)
    expires_dt = validate_future_dt(
        expires_dt,
        now=now,
        min_delay=timedelta(seconds=30),
        max_delay=MAX_STATE_DURATION,
    )

    target_user_id = args.get("target_user_id")
    if target_user_id is not None:
        target_user_id = int(target_user_id)
    target_username = args.get("target_username")
    if target_username is not None:
        target_username = str(target_username)

    state_id = await add_event_state(
        chat_id,
        state_key=state_key,
        value=value,
        expires_at=to_iso(expires_dt),
        reason=reason,
        target_user_id=target_user_id,
        target_username=target_username,
    )
    return {
        "id": state_id,
        "state_key": state_key,
        "value": value,
        "expires_at": to_iso(expires_dt),
        "target_user_id": target_user_id,
    }


async def cancel_action_from_args(chat_id: int, args: dict[str, Any]) -> dict[str, Any]:
    action_id = int(args["action_id"])
    ok = await cancel_scheduled_action(action_id, chat_id)
    if not ok:
        return {"id": action_id, "cancelled": False, "reason": "not_found_or_not_pending"}
    return {"id": action_id, "cancelled": True}


async def clear_state_from_args(chat_id: int, args: dict[str, Any]) -> dict[str, Any]:
    state_id = args.get("state_id")
    state_key = args.get("state_key") or args.get("key")
    target_user_id = args.get("target_user_id")
    if target_user_id is not None:
        target_user_id = int(target_user_id)

    if state_id is not None:
        cleared = await clear_event_state(int(state_id), chat_id=chat_id)
    elif state_key:
        cleared = await clear_event_state(
            chat_id=chat_id,
            state_key=str(state_key),
            target_user_id=target_user_id,
        )
    else:
        raise ValueError("Provide state_id or state_key to clear")

    return {"cleared": cleared}


def build_schedule_context_blocks(
    pending_actions: list[dict],
    active_states: list[dict],
) -> str:
    """XML fragments for injection into conversation context."""
    from xml.sax.saxutils import escape, quoteattr

    now = utc_now()
    parts: list[str] = []
    if active_states:
        parts.append("  <active_event_states>")
        for state in active_states:
            target_attrs = ""
            if state.get("target_user_id") is not None:
                target_attrs += f" target_user_id={quoteattr(str(state['target_user_id']))}"
            if state.get("target_username"):
                target_attrs += f" target_username={quoteattr(str(state['target_username']))}"
            reason = state.get("reason") or ""
            until_human = humanize_when(str(state["expires_at"]), now=now)
            parts.append(
                f'    <state id={quoteattr(str(state["id"]))} '
                f'key={quoteattr(str(state["state_key"]))} '
                f'until={quoteattr(str(state["expires_at"]))} '
                f'until_human={quoteattr(until_human)}{target_attrs}>'
            )
            parts.append(f"      <value>{escape(str(state.get('value') or ''))}</value>")
            if reason:
                parts.append(f"      <reason>{escape(str(reason))}</reason>")
            parts.append("    </state>")
        parts.append(
            "    <note>These are your current vibes, not system modes. Let them color tone, "
            "warmth, patience, and whether you bother replying. "
            "For key=ignore: spontaneous chat is suppressed; if someone pings you anyway, "
            "usually stay silent (empty messages) or give a cold one-liner — break the cold "
            "shoulder only if something genuinely matters. Soft keys (mood, angry, quiet, "
            "excited) never force silence; they only change how you sound.</note>"
        )
        parts.append("  </active_event_states>")

    if pending_actions:
        parts.append("  <pending_scheduled_actions>")
        for action in pending_actions:
            at_human = humanize_when(str(action["execute_at"]), now=now)
            parts.append(
                f'    <action id={quoteattr(str(action["id"]))} '
                f'type={quoteattr(str(action["action_type"]))} '
                f'at={quoteattr(str(action["execute_at"]))} '
                f'at_human={quoteattr(at_human)}>'
            )
            parts.append(f"      <reason>{escape(str(action.get('reason') or ''))}</reason>")
            parts.append(
                f"      <instruction>{escape(str(action.get('instruction') or ''))}</instruction>"
            )
            if action.get("context"):
                ctx = str(action["context"])
                if len(ctx) > 500:
                    ctx = ctx[:500] + "…"
                parts.append(f"      <context>{escape(ctx)}</context>")
            parts.append("    </action>")
        parts.append(
            "    <note>Private plans you already made — not announcements. "
            "Do not re-schedule the same topic. Cancel if the moment passed or you changed your mind. "
            "Never narrate the scheduling system to the chat.</note>"
        )
        parts.append("  </pending_scheduled_actions>")

    return "\n".join(parts)


async def process_due_scheduled_actions(application) -> int:
    """Claim and execute due scheduled actions. Returns number processed."""
    await expire_event_states()
    now = to_iso(utc_now())
    due = await get_due_scheduled_actions(now, limit=20)
    processed = 0
    for action in due:
        action_id = action["id"]
        if not await claim_scheduled_action(action_id):
            continue
        try:
            await _execute_scheduled_action(application, action)
            await complete_scheduled_action(action_id, status="done")
            processed += 1
        except Exception as e:
            logging.exception("Failed scheduled action id=%s: %s", action_id, e)
            await complete_scheduled_action(
                action_id, status="failed", error_message=str(e)[:500]
            )
    return processed


def _recent_chat_snapshot(chat_id: int, *, max_messages: int = 12) -> list[dict]:
    """Pull live working memory so delayed replies fit the current room."""
    try:
        from bot.handlers import chat_history
    except Exception:
        return []
    history = chat_history.get(chat_id)
    if not history:
        return []
    snapshot: list[dict] = []
    for msg in list(history)[-max_messages:]:
        snapshot.append(
            {
                "message_id": msg.get("message_id", 0),
                "sender": msg.get("sender") or "Unknown",
                "user_id": msg.get("user_id") or 0,
                "text": msg.get("text") or "",
                "reply_to_username": msg.get("reply_to_username"),
                "reply_to_text": msg.get("reply_to_text"),
                "reply_to_id": msg.get("reply_to_id"),
                "media_unique_id": msg.get("media_unique_id"),
            }
        )
    return snapshot


def _action_age_phrase(action: dict, *, now: datetime | None = None) -> str:
    now = now or utc_now()
    created = parse_iso(str(action.get("created_at") or ""))
    if not created:
        # Fall back to how overdue we are vs execute_at
        execute_at = parse_iso(str(action.get("execute_at") or ""))
        if not execute_at:
            return "a while ago"
        return humanize_timedelta(execute_at - now).replace("in ", "").replace("about ", "") + " plan"
    return humanize_timedelta(created - now)


async def _execute_scheduled_action(application, action: dict) -> None:
    """Run one claimed scheduled action via the RP LLM (and optional ponder)."""
    from bot.agent import run_ponder_agent
    from bot.handlers import _send_llm_response
    from bot.llm import generate_response
    from bot.memory import get_relevant_general_memories, get_user_thought

    chat_id = action["chat_id"]
    bot = application.bot
    bot_username = bot.username or "Bot"
    action_type = action["action_type"]
    reason = action.get("reason") or ""
    instruction = action.get("instruction") or ""
    context_blob = action.get("context") or ""
    target_user = action.get("target_username") or action.get("target_user_id") or "someone"
    reply_to = action.get("reply_to_message_id")
    now = utc_now()
    planned_ago = _action_age_phrase(action, now=now)

    active_states = await list_active_event_states(chat_id)
    pending = await list_pending_scheduled_actions(chat_id)
    recent = _recent_chat_snapshot(chat_id)

    # Private inner monologue — never shown as a chat message
    private_nudge = (
        f"(private) You meant to follow up on something you set aside {planned_ago}. "
        f"Kind of thing: {action_type}. Why you waited: {reason}. "
        f"What you wanted to do: {instruction}."
    )
    if context_blob:
        private_nudge += f" Back then: {context_blob[:1200]}"
    if target_user:
        private_nudge += f" Related person: {target_user}."

    messages_context: list[dict] = list(recent)
    # Append the private cue as the focused "thought", not as a user in the room
    focus_id = 0
    messages_context.append(
        {
            "message_id": focus_id,
            "sender": "your_earlier_self",
            "user_id": 0,
            "text": private_nudge,
            "reply_to_username": None,
            "reply_to_text": None,
        }
    )

    user_thoughts: dict[str, str] = {}
    if action.get("target_user_id"):
        thought = await get_user_thought(int(action["target_user_id"]))
        if thought:
            name = action.get("target_username") or str(action["target_user_id"])
            user_thoughts[name] = thought

    # Thoughts for people visible in recent history
    seen_uids = {m.get("user_id") for m in recent if m.get("user_id")}
    for uid in seen_uids:
        if not uid or uid in (0, action.get("target_user_id")):
            continue
        thought = await get_user_thought(int(uid))
        if thought:
            uname = next(
                (m["sender"] for m in recent if m.get("user_id") == uid),
                str(uid),
            )
            user_thoughts.setdefault(uname, thought)

    memory_query = f"{instruction}\n{reason}\n{context_blob}"
    general_memories = await get_relevant_general_memories(chat_id, memory_query, limit=8)

    # Prefer threading only if the original message is still "in play"
    prefer_reply_thread = False
    if action_type == "reply" and reply_to:
        created = parse_iso(str(action.get("created_at") or ""))
        if created and (now - created) <= REPLY_THREAD_MAX_AGE:
            prefer_reply_thread = True
        elif not created:
            prefer_reply_thread = True

    extra_parts = [
        "\n<returning_to_something>",
        f'  <private_plan type="{action_type}" planned="{planned_ago}">',
        f"    <why_you_waited>{reason}</why_you_waited>",
        f"    <what_you_wanted>{instruction}</what_you_wanted>",
    ]
    if context_blob:
        extra_parts.append(f"    <back_then>{context_blob[:2000]}</back_then>")
    extra_parts.append("  </private_plan>")
    schedule_xml = build_schedule_context_blocks(pending, active_states)
    if schedule_xml:
        extra_parts.append(schedule_xml)
    extra_parts.append(
        "  <how_to_act>"
        "This is not a system job and not a new user message. You just remembered something "
        "you meant to do — or the moment feels right. Speak in your normal persona voice. "
        "Do NOT mention timers, schedules, queues, tools, or that you 'set a reminder'. "
        "Do NOT open with robotic delay apologies by default. Only nod at the delay if it "
        "would be weird not to (e.g. answering a question hours later). "
        "Read recent chat first: if the topic already moved on or was resolved, keep it short "
        "or stay quiet (empty messages). "
        "If you still answer an old question, be natural — like you came back to the chat. "
        f"{'Threading a reply to the original message is fine if it still fits.' if prefer_reply_thread else 'The original message is old; prefer a fresh message over a stale reply-thread unless quoting helps.'} "
        "Do not re-schedule this same plan."
        "</how_to_act>"
    )
    extra_parts.append("</returning_to_something>")
    extra_context = "\n".join(extra_parts)

    if action_type == "research":
        ponder_query = instruction[:500]
        if context_blob:
            ponder_query = f"{instruction}\n\nContext: {context_blob[:1000]}"[:2000]
        # Include a slice of recent chat so research matches current talk
        conv_bits = [f"{m.get('sender')}: {m.get('text', '')[:200]}" for m in recent[-6:]]
        conv_ctx = "\n".join(conv_bits)
        if context_blob:
            conv_ctx = f"{context_blob[:800]}\n{conv_ctx}"
        ponder_result = await run_ponder_agent(
            ponder_query,
            chat_id,
            conversation_context=conv_ctx[:2000] if conv_ctx else None,
        )
        extra_context += (
            f'\n<ponder_result query="{ponder_query[:200]}">'
            f"\n{ponder_result}"
            f"\n</ponder_result>"
            f"\n<instruction>You looked this up in the background. Share what matters in your "
            f"own voice — not as a research report. Do NOT call ponder again.</instruction>"
        )

    response = await generate_response(
        messages_context=messages_context,
        user_thoughts=user_thoughts,
        general_memories=general_memories,
        chat_id=chat_id,
        focus_message_id=focus_id,
        source="scheduled_action",
        memory_query=memory_query,
        extra_context=extra_context,
        settings_chat_id=chat_id,
    )

    if not response:
        logging.warning("Scheduled action %s produced no LLM reply", action["id"])
        return

    # Soft-default reply threading only for still-fresh deferred replies
    if (
        prefer_reply_thread
        and reply_to
        and not response.get("reply_to_message_id")
        and response.get("messages")
    ):
        response["reply_to_message_id"] = reply_to

    class _Ctx:
        def __init__(self, bot_obj):
            self.bot = bot_obj

    await _send_llm_response(response, chat_id, f"@{bot_username}", _Ctx(bot))
    logging.info(
        "Executed scheduled action id=%s type=%s chat=%s",
        action["id"],
        action_type,
        chat_id,
    )


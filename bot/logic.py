import random
import logging
from bot.memory import get_chat_config, set_chat_config, get_config, set_config

# Cooldown state
# We track the number of messages seen since the last reply per chat
# Map: chat_id -> count
messages_since_last_reply: dict[int, int] = {}

BOT_REPLY_LOCK_TTL_MESSAGES = 3
bot_reply_locks: dict[int, dict[int, int]] = {}
bot_ping_pong_counts: dict[int, dict[int, int]] = {}

# Default values
DEFAULT_COOLDOWN_THRESHOLD = 10
DEFAULT_REPLY_CHANCE = 0.05
DEFAULT_REACTION_CHANCE = 0.07
DEFAULT_MAX_PING_PONG = 2

# Per-chat overrides fall back to this chat_id when unset.
GLOBAL_SETTINGS_CHAT_ID = 0


# Global pause state
is_paused = False


def _sender_is_bot(message) -> bool:
    return bool(getattr(getattr(message, "from_user", None), "is_bot", False))


def _sender_id(message) -> int | None:
    return getattr(getattr(message, "from_user", None), "id", None)


def is_private_chat(chat) -> bool:
    return getattr(chat, "type", None) == "private"


def resolve_settings_chat_id(chat) -> int:
    if chat is None:
        return GLOBAL_SETTINGS_CHAT_ID
    if is_private_chat(chat):
        return GLOBAL_SETTINGS_CHAT_ID
    return chat.id


async def _get_chat_config_effective(chat_id: int, key: str) -> str | None:
    if chat_id != GLOBAL_SETTINGS_CHAT_ID:
        val = await get_chat_config(chat_id, key)
        if val is not None:
            return val
    return await get_chat_config(GLOBAL_SETTINGS_CHAT_ID, key)


def _decrement_bot_reply_locks(
    chat_id: int, active_sender_id: int | None = None
) -> None:
    locks = bot_reply_locks.get(chat_id, {})
    expired_sender_ids = []

    for sender_id, remaining in locks.items():
        if sender_id == active_sender_id or remaining <= 0:
            continue

        remaining -= 1
        if remaining <= 0:
            expired_sender_ids.append(sender_id)
        else:
            locks[sender_id] = remaining

    for sender_id in expired_sender_ids:
        del locks[sender_id]

    if not locks and chat_id in bot_reply_locks:
        del bot_reply_locks[chat_id]


def _mark_bot_replied(chat_id: int, sender_id: int | None) -> None:
    if sender_id is None:
        return

    bot_reply_locks.setdefault(chat_id, {})[sender_id] = BOT_REPLY_LOCK_TTL_MESSAGES
    counts = bot_ping_pong_counts.setdefault(chat_id, {})
    counts[sender_id] = counts.get(sender_id, 0) + 1


def _reset_bot_ping_pong(chat_id: int) -> None:
    bot_ping_pong_counts.pop(chat_id, None)


def _bot_ping_pong_count(chat_id: int, sender_id: int | None) -> int:
    if sender_id is None:
        return 0

    return bot_ping_pong_counts.get(chat_id, {}).get(sender_id, 0)


async def set_cooldown_threshold(chat_id: int, value: int):
    await set_chat_config(chat_id, "cooldown_threshold", str(value))


async def set_reply_chance(chat_id: int, value: float):
    await set_chat_config(chat_id, "reply_chance", str(value))


async def set_reaction_chance(chat_id: int, value: float):
    await set_chat_config(chat_id, "reaction_chance", str(value))


async def set_max_ping_pong(chat_id: int, value: int):
    await set_chat_config(chat_id, "max_ping_pong", str(value))


async def set_utils_disabled(chat_id: int, disabled: bool):
    await set_chat_config(chat_id, "utils_disabled", "true" if disabled else "false")


async def get_utils_disabled(chat_id: int) -> bool:
    val = await _get_chat_config_effective(chat_id, "utils_disabled")
    return val == "true"


async def init_logic():
    global is_paused
    # Load paused state
    val = await get_config("is_paused")
    if val:
        is_paused = val == "true"
    logging.info(f"Logic initialized. Paused: {is_paused}")


async def set_paused(value: bool):
    global is_paused
    is_paused = value
    await set_config("is_paused", "true" if value else "false")


def get_paused() -> bool:
    return is_paused


async def get_logic_config(chat_id: int):
    cooldown = DEFAULT_COOLDOWN_THRESHOLD
    reply_chance = DEFAULT_REPLY_CHANCE
    reaction_chance = DEFAULT_REACTION_CHANCE

    val = await _get_chat_config_effective(chat_id, "cooldown_threshold")
    if val:
        cooldown = int(val)

    val = await _get_chat_config_effective(chat_id, "reply_chance")
    if val:
        reply_chance = float(val)

    val = await _get_chat_config_effective(chat_id, "reaction_chance")
    if val:
        reaction_chance = float(val)

    return cooldown, reply_chance, reaction_chance


async def get_max_ping_pong(chat_id: int) -> int:
    val = await _get_chat_config_effective(chat_id, "max_ping_pong")
    if not val:
        return DEFAULT_MAX_PING_PONG

    try:
        return max(0, int(val))
    except ValueError:
        return DEFAULT_MAX_PING_PONG


MAX_COOLDOWN_THRESHOLD = 200
MAX_PING_PONG_LIMIT = 20
MAX_MEDIA_REPLY_GUIDANCE_LEN = 500


async def get_media_reply_guidance(chat_id: int) -> str:
    val = await _get_chat_config_effective(chat_id, "media_reply_guidance")
    return val.strip() if val else ""


async def set_media_reply_guidance(chat_id: int, guidance: str) -> None:
    guidance = guidance.strip()[:MAX_MEDIA_REPLY_GUIDANCE_LEN]
    await set_chat_config(chat_id, "media_reply_guidance", guidance)


async def get_behavior_settings(settings_chat_id: int) -> dict:
    cooldown, reply_chance, reaction_chance = await get_logic_config(settings_chat_id)
    return {
        "scope": "global" if settings_chat_id == GLOBAL_SETTINGS_CHAT_ID else "chat",
        "settings_chat_id": settings_chat_id,
        "reply_chance": reply_chance,
        "reaction_chance": reaction_chance,
        "cooldown_threshold": cooldown,
        "max_ping_pong": await get_max_ping_pong(settings_chat_id),
        "media_reply_guidance": await get_media_reply_guidance(settings_chat_id),
    }


async def update_behavior_settings(
    settings_chat_id: int,
    *,
    requesting_user_id: int | None,
    admin_id: int,
    reply_chance: float | None = None,
    reaction_chance: float | None = None,
    cooldown_threshold: int | None = None,
    max_ping_pong: int | None = None,
    media_reply_guidance: str | None = None,
) -> tuple[bool, str]:
    if requesting_user_id != admin_id:
        return False, "admin_only"

    changed = False
    if reply_chance is not None:
        await set_reply_chance(settings_chat_id, max(0.0, min(1.0, float(reply_chance))))
        changed = True
    if reaction_chance is not None:
        await set_reaction_chance(
            settings_chat_id, max(0.0, min(1.0, float(reaction_chance)))
        )
        changed = True
    if cooldown_threshold is not None:
        await set_cooldown_threshold(
            settings_chat_id, max(0, min(MAX_COOLDOWN_THRESHOLD, int(cooldown_threshold)))
        )
        changed = True
    if max_ping_pong is not None:
        await set_max_ping_pong(
            settings_chat_id, max(0, min(MAX_PING_PONG_LIMIT, int(max_ping_pong)))
        )
        changed = True
    if media_reply_guidance is not None:
        await set_media_reply_guidance(settings_chat_id, str(media_reply_guidance))
        changed = True

    if not changed:
        return False, "no_fields"
    return True, "ok"


async def should_reply(message, bot_username: str, chat_id: int) -> bool:
    global messages_since_last_reply

    cooldown, reply_chance, _ = await get_logic_config(chat_id)

    # Initialize if not present
    if chat_id not in messages_since_last_reply:
        messages_since_last_reply[chat_id] = 0

    sender_is_bot = _sender_is_bot(message)
    sender_id = _sender_id(message)
    max_ping_pong = await get_max_ping_pong(chat_id) if sender_is_bot else 0
    _decrement_bot_reply_locks(chat_id, active_sender_id=sender_id)
    if not sender_is_bot:
        _reset_bot_ping_pong(chat_id)

    logging.info(
        f"Checking if should reply in {chat_id}: {messages_since_last_reply[chat_id]}"
    )

    if sender_is_bot and sender_id in bot_reply_locks.get(chat_id, {}):
        logging.info(
            f"Ignoring bot message from {sender_id} in chat {chat_id}: reply lock active"
        )
        messages_since_last_reply[chat_id] += 1
        return False

    if (
        sender_is_bot
        and sender_id is not None
        and _bot_ping_pong_count(chat_id, sender_id) >= max_ping_pong
    ):
        logging.info(
            f"Ignoring bot message from {sender_id} in chat {chat_id}: max ping pong reached"
        )
        messages_since_last_reply[chat_id] += 1
        return False

    chat_type = getattr(getattr(message, "chat", None), "type", None)
    if chat_type == "private" and not sender_is_bot:
        logging.info(f"Trigger: Private chat message in {chat_id}")
        messages_since_last_reply[chat_id] = 0
        return True

    # Check for direct mention
    if message.text:  # Check for direct mention
        if bot_username in message.text:
            logging.info(f"Trigger: Mentioned in chat {chat_id}")
            messages_since_last_reply[chat_id] = 0
            if sender_is_bot:
                _mark_bot_replied(chat_id, sender_id)
            return True

    # Check for reply to bot's message
    reply_to_message = getattr(message, "reply_to_message", None)
    reply_to_user = getattr(reply_to_message, "from_user", None)
    if (
        reply_to_message
        and getattr(reply_to_user, "username", None) == bot_username.replace("@", "")
    ):
        logging.info(f"Trigger: Replied to in chat {chat_id}")
        messages_since_last_reply[chat_id] = 0
        if sender_is_bot:
            _mark_bot_replied(chat_id, sender_id)
        return True

    if sender_is_bot:
        messages_since_last_reply[chat_id] += 1
        return False

    # Check cooldown
    if messages_since_last_reply[chat_id] < cooldown:
        messages_since_last_reply[chat_id] += 1
        return False

    # Random chance
    random_value = random.random()
    logging.info(f"Random chance in chat {random_value} < {reply_chance}")
    if random_value < reply_chance:
        logging.info(f"Trigger: Random chance in chat {chat_id}")
        messages_since_last_reply[chat_id] = 0
        return True

    messages_since_last_reply[chat_id] += 1
    return False


async def should_react(chat_id: int) -> bool:
    _, _, reaction_chance = await get_logic_config(chat_id)

    # Random chance for reaction
    if random.random() < reaction_chance:
        logging.info(f"Trigger: Reaction chance in chat {chat_id}")
        return True
    return False

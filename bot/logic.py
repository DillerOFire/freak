import random
import logging
from bot.memory import get_chat_config, set_chat_config

# Cooldown state
# We track the number of messages seen since the last reply per chat
# Map: chat_id -> count
messages_since_last_reply: dict[int, int] = {}

# Default values
DEFAULT_COOLDOWN_THRESHOLD = 10
DEFAULT_REPLY_CHANCE = 0.05
DEFAULT_REACTION_CHANCE = 0.07


# Global pause state
is_paused = False


async def set_cooldown_threshold(chat_id: int, value: int):
    await set_chat_config(chat_id, "cooldown_threshold", str(value))


async def set_reply_chance(chat_id: int, value: float):
    await set_chat_config(chat_id, "reply_chance", str(value))


async def set_reaction_chance(chat_id: int, value: float):
    await set_chat_config(chat_id, "reaction_chance", str(value))


async def set_utils_disabled(chat_id: int, disabled: bool):
    await set_chat_config(chat_id, "utils_disabled", "true" if disabled else "false")


async def get_utils_disabled(chat_id: int) -> bool:
    val = await get_chat_config(chat_id, "utils_disabled")
    return val == "true"


def set_paused(value: bool):
    global is_paused
    is_paused = value


def get_paused() -> bool:
    return is_paused


async def get_logic_config(chat_id: int):
    cooldown = DEFAULT_COOLDOWN_THRESHOLD
    reply_chance = DEFAULT_REPLY_CHANCE
    reaction_chance = DEFAULT_REACTION_CHANCE

    val = await get_chat_config(chat_id, "cooldown_threshold")
    if val:
        cooldown = int(val)

    val = await get_chat_config(chat_id, "reply_chance")
    if val:
        reply_chance = float(val)

    val = await get_chat_config(chat_id, "reaction_chance")
    if val:
        reaction_chance = float(val)

    return cooldown, reply_chance, reaction_chance


async def should_reply(message, bot_username: str, chat_id: int) -> bool:
    global messages_since_last_reply

    cooldown, reply_chance, _ = await get_logic_config(chat_id)

    # Initialize if not present
    if chat_id not in messages_since_last_reply:
        messages_since_last_reply[chat_id] = 0
    logging.info(
        f"Checking if should reply in {chat_id}: {messages_since_last_reply[chat_id]}"
    )

    # Check for direct mention
    if message.text:  # Check for direct mention
        if bot_username in message.text:
            logging.info(f"Trigger: Mentioned in chat {chat_id}")
            messages_since_last_reply[chat_id] = 0
            return True

    # Check for reply to bot's message
    if (
        message.reply_to_message
        and message.reply_to_message.from_user.username == bot_username.replace("@", "")
    ):
        logging.info(f"Trigger: Replied to in chat {chat_id}")
        messages_since_last_reply[chat_id] = 0
        return True

    # Check cooldown
    if messages_since_last_reply[chat_id] < cooldown:
        messages_since_last_reply[chat_id] += 1
        return False

    # Random chance
    logging.info(f"Random chance in chat {random.random()} < {reply_chance}")
    if random.random() < reply_chance:
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

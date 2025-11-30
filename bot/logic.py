import random
import logging

# Cooldown state
# We track the number of messages seen since the last reply per chat
# Map: chat_id -> count
messages_since_last_reply: dict[int, int] = {}
COOLDOWN_THRESHOLD = 10
REPLY_CHANCE = 0.04

# Global pause state
is_paused = False


def set_paused(value: bool):
    global is_paused
    is_paused = value


def get_paused() -> bool:
    return is_paused


def should_reply(message, bot_username: str, chat_id: int) -> bool:
    global messages_since_last_reply

    # Initialize if not present
    if chat_id not in messages_since_last_reply:
        messages_since_last_reply[chat_id] = 0

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
    if messages_since_last_reply[chat_id] < COOLDOWN_THRESHOLD:
        messages_since_last_reply[chat_id] += 1
        return False

    # Random chance
    if random.random() < REPLY_CHANCE:
        logging.info(f"Trigger: Random chance in chat {chat_id}")
        messages_since_last_reply[chat_id] = 0
        return True

    messages_since_last_reply[chat_id] += 1
    return False


def reset_cooldown(chat_id: int):
    global messages_since_last_reply
    messages_since_last_reply[chat_id] = 0

import random
import logging

# Cooldown state
# We track the number of messages seen since the last reply
messages_since_last_reply = 0
COOLDOWN_THRESHOLD = 10
REPLY_CHANCE = 0.15


def should_reply(message, bot_username: str) -> bool:
    global messages_since_last_reply

    # Check for direct mention
    # Check for direct mention
    if message.text:  # Check for direct mention
        if bot_username in message.text:
            logging.info("Trigger: Mentioned")
            messages_since_last_reply = 0
            return True

    # Check for reply to bot's message
    if (
        message.reply_to_message
        and message.reply_to_message.from_user.username == bot_username.replace("@", "")
    ):
        logging.info("Trigger: Replied to")
        messages_since_last_reply = 0
        return True

    # Check cooldown
    if messages_since_last_reply < COOLDOWN_THRESHOLD:
        messages_since_last_reply += 1
        return False

    # Random chance
    if random.random() < REPLY_CHANCE:
        logging.info("Trigger: Random chance")
        messages_since_last_reply = 0
        return True

    messages_since_last_reply += 1
    return False


def reset_cooldown():
    global messages_since_last_reply
    messages_since_last_reply = 0

import logging
from collections import deque
from telegram import Update
from telegram.ext import ContextTypes
from bot.logic import should_reply
from bot.llm import generate_response
from bot.memory import get_user_thought, get_general_memories

# In-memory chat history (store last 20 messages)
chat_history = deque(maxlen=20)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user = update.message.from_user
    text = update.message.text
    message_id = update.message.message_id

    reply_to_id = None
    reply_to_username = None
    if update.message.reply_to_message:
        reply_to_id = update.message.reply_to_message.message_id
        reply_to_username = (
            update.message.reply_to_message.from_user.username
            or update.message.reply_to_message.from_user.first_name
        )

    # Add to history
    chat_history.append(
        {
            "message_id": message_id,
            "sender": user.username or user.first_name,
            "text": text,
            "user_id": user.id,
            "reply_to_id": reply_to_id,
            "reply_to_username": reply_to_username,
        }
    )

    bot_username = context.bot.username
    if not bot_username:
        # Fallback if bot username not yet available (shouldn't happen usually)
        bot_username = "@Bot"

    if should_reply(update.message, f"@{bot_username}"):
        logging.info("Decided to reply...")

        # Gather context
        # 1. Get user thoughts for participants in history
        # Create a map of user_id -> username from history
        uid_to_username = {msg["user_id"]: msg["sender"] for msg in chat_history}
        logging.info(f"DEBUG: Active participants in history: {uid_to_username}")

        user_thoughts = {}
        for uid in uid_to_username.keys():
            thought = await get_user_thought(uid)
            if thought:
                username = uid_to_username[uid]
                user_thoughts[username] = thought

        # 2. Get general memories
        general_memories = await get_general_memories()

        # 3. Call LLM
        response = await generate_response(
            list(chat_history),
            user_thoughts,
            general_memories,
            focus_message_id=message_id,
        )

        if response and response.get("content"):
            reply_to = response.get("reply_to_message_id")
            content = response.get("content")

            # If the LLM wants to reply to a specific message, try to do so
            # Otherwise just send to chat
            try:
                if reply_to:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=content,
                        reply_to_message_id=reply_to,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id, text=content
                    )
            except Exception as e:
                logging.error(f"Failed to send message: {e}")

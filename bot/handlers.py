import logging
from collections import deque
from telegram import Update
from telegram.ext import ContextTypes
from bot.logic import should_reply
from bot.llm import generate_response
from bot.memory import (
    get_user_thought,
    get_general_memories,
    get_media_description,
    save_media_description,
)
from bot.media_utils import download_file, extract_frames_from_video
from bot.vision import analyze_image, analyze_frames
import os

# In-memory chat history (store last 20 messages)
chat_history = deque(maxlen=20)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.message.from_user

    # Determine if message has media
    media_description = ""
    file_unique_id = None

    # Check for Photo
    if update.message.photo:
        # Get the largest photo
        photo = update.message.photo[-1]
        file_unique_id = photo.file_unique_id

        # Check cache
        cached_desc = await get_media_description(file_unique_id)
        if cached_desc:
            media_description = f"[User sent a photo: {cached_desc}]"
            logging.info(f"Found photo description in cache: {cached_desc}")
        else:
            logging.info("Analyzing new photo...")
            file = await photo.get_file()
            file_path = await download_file(file)

            with open(file_path, "rb") as f:
                image_bytes = f.read()

            description = await analyze_image(image_bytes)
            if not description.startswith("Error"):
                await save_media_description(file_unique_id, description)
            media_description = f"[User sent a photo: {description}]"

            # Cleanup
            os.remove(file_path)

    # Check for Sticker
    elif update.message.sticker:
        sticker = update.message.sticker
        file_unique_id = sticker.file_unique_id

        # Check cache
        cached_desc = await get_media_description(file_unique_id)
        if cached_desc:
            media_description = f"[User sent a sticker: {cached_desc}]"
        else:
            if sticker.is_animated or sticker.is_video:
                # Handle animated/video stickers
                logging.info("Analyzing animated/video sticker...")
                file = await sticker.get_file()
                file_path = await download_file(file)

                frames = extract_frames_from_video(file_path)
                if frames:
                    description = await analyze_frames(frames)
                    if not description.startswith("Error"):
                        await save_media_description(file_unique_id, description)
                    media_description = (
                        f"[User sent an animated sticker: {description}]"
                    )
                else:
                    media_description = (
                        "[User sent an animated sticker (could not analyze)]"
                    )

                os.remove(file_path)
            else:
                # Static sticker
                logging.info("Analyzing static sticker...")
                file = await sticker.get_file()
                file_path = await download_file(file)

                with open(file_path, "rb") as f:
                    image_bytes = f.read()

                description = await analyze_image(image_bytes)
                if not description.startswith("Error"):
                    await save_media_description(file_unique_id, description)
                media_description = f"[User sent a sticker: {description}]"

                os.remove(file_path)

    # Check for Video/Animation
    elif update.message.video or update.message.animation:
        video = update.message.video or update.message.animation
        file_unique_id = video.file_unique_id

        # Check cache
        cached_desc = await get_media_description(file_unique_id)
        if cached_desc:
            media_description = f"[User sent a video/animation: {cached_desc}]"
        else:
            logging.info("Analyzing video/animation...")
            file = await video.get_file()
            file_path = await download_file(file)

            frames = extract_frames_from_video(file_path)
            if frames:
                description = await analyze_frames(frames)
                if not description.startswith("Error"):
                    await save_media_description(file_unique_id, description)
                media_description = f"[User sent a video/animation: {description}]"
            else:
                media_description = "[User sent a video/animation (could not analyze)]"

            os.remove(file_path)

    text = update.message.text or update.message.caption or ""
    if media_description:
        text = f"{media_description}\n{text}".strip()

    if not text and not media_description:
        return
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

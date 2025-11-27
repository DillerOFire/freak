import logging
from collections import deque
from telegram import Update
from telegram.ext import ContextTypes
from bot.logic import should_reply, get_paused
from bot.llm import generate_response
from bot.memory import (
    get_user_thought,
    get_general_memories,
    get_media_description,
    save_media_description,
    is_whitelisted,
)
from bot.media_utils import (
    download_file,
    extract_frames_from_video,
    download_video_ytdlp,
)
from bot.vision import analyze_image, analyze_frames
from config import COOKIES_DIR, ADMIN_ID
import os
import re

# In-memory chat history (store last 20 messages per chat)
# Map: chat_id -> deque
chat_history: dict[int, deque] = {}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    # Check if bot is paused
    if get_paused():
        return

    user = update.message.from_user
    chat_id = update.effective_chat.id

    # Access Control: Whitelist Check
    # Admin is always allowed
    if user.id != ADMIN_ID:
        # Check if chat (group or user) is whitelisted
        # For DMs, effective_chat.id is user_id
        # For Groups, effective_chat.id is group_id
        if not await is_whitelisted(chat_id):
            # If it's a DM, maybe check if the user ID is whitelisted explicitly?
            # The logic above covers it if we add user_id to whitelist.
            # But if a user is in a non-whitelisted group, should they be ignored?
            # Yes, "work only with whitelisted groups".

            # If it's a group, and not whitelisted, ignore.
            # If it's a DM, and not whitelisted, ignore.

            # Optional: Log ignored attempt
            # logging.info(f"Ignored message from {user.id} in chat {chat_id} (not whitelisted)")
            return

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

    # Check for Document
    elif update.message.document:
        doc = update.message.document
        media_description = f"[User sent a document: {doc.file_name} ({doc.mime_type})]"

    text = update.message.text or update.message.caption or ""

    # Check for Video URLs (YouTube, Instagram, X, etc.)
    # Expanded list of domains
    target_domains = [
        "youtube.com",
        "youtu.be",
        "instagram.com",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "facebook.com",
        "reddit.com",
        "pinterest.com",
    ]

    # Construct regex pattern from domains
    # Escaping dots and creating a non-capturing group
    domain_pattern = "|".join([re.escape(d) for d in target_domains])
    url_pattern = rf"(https?://(?:www\.)?(?:{domain_pattern})/[^\s]+)"

    urls = re.findall(url_pattern, text)

    if urls:
        for url in urls:
            logging.info(f"Detected URL: {url}")

            # Determine service for cookies
            cookies_path = None
            if "youtube.com" in url or "youtu.be" in url:
                cookies_path = os.path.join(COOKIES_DIR, "youtube.txt")
            elif "instagram.com" in url:
                cookies_path = os.path.join(COOKIES_DIR, "instagram.txt")
            elif "x.com" in url or "twitter.com" in url:
                cookies_path = os.path.join(COOKIES_DIR, "x.txt")
            elif "tiktok.com" in url:
                cookies_path = os.path.join(COOKIES_DIR, "tiktok.txt")
            elif "facebook.com" in url:
                cookies_path = os.path.join(COOKIES_DIR, "facebook.txt")
            elif "reddit.com" in url:
                cookies_path = os.path.join(COOKIES_DIR, "reddit.txt")
            elif "pinterest.com" in url:
                cookies_path = os.path.join(COOKIES_DIR, "pinterest.txt")

            video_path = download_video_ytdlp(url, cookies_path)
            if video_path:
                try:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=open(video_path, "rb"),
                        reply_to_message_id=update.message.message_id,
                    )
                    os.remove(video_path)
                    return  # Stop processing if we handled a video download
                except Exception as e:
                    logging.error(f"Failed to send video: {e}")
                    if os.path.exists(video_path):
                        os.remove(video_path)

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

    # Initialize chat history if needed
    if chat_id not in chat_history:
        chat_history[chat_id] = deque(maxlen=20)

    # Add to history
    chat_history[chat_id].append(
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

    if should_reply(update.message, f"@{bot_username}", chat_id):
        logging.info("Decided to reply...")

        # Gather context
        # 1. Get user thoughts for participants in history
        # Create a map of user_id -> username from history
        # uid_to_username = {msg["user_id"]: msg["sender"] for msg in chat_history}
        # logging.info(f"DEBUG: Active participants in history: {uid_to_username}")

        # Retrieve memory
        user_thoughts = {}
        # We only need thoughts for users involved in the context
        current_history = chat_history[chat_id]
        involved_user_ids = set(msg["user_id"] for msg in current_history)
        for uid in involved_user_ids:
            thought = await get_user_thought(uid)
            if thought:
                # Find username for this uid from context
                username = next(
                    (msg["sender"] for msg in current_history if msg["user_id"] == uid),
                    "Unknown",
                )
                user_thoughts[username] = thought

        general_memories = await get_general_memories(chat_id, limit=5)

        # Generate response
        response = await generate_response(
            list(current_history),
            user_thoughts,
            general_memories,
            chat_id,
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
                        chat_id=chat_id,
                        text=content,
                        reply_to_message_id=reply_to,
                    )
                else:
                    await context.bot.send_message(chat_id=chat_id, text=content)
            except Exception as e:
                logging.error(f"Failed to send message: {e}")

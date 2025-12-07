import logging
from collections import deque
from telegram import Update
from telegram.ext import ContextTypes
from bot.logic import should_reply, get_paused, should_react, get_utils_disabled
from bot.llm import generate_response, generate_reaction
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


def add_message_to_history(
    chat_id: int,
    message_id: int,
    sender: str,
    text: str,
    user_id: int,
    reply_to_id: int | None = None,
    reply_to_username: str | None = None,
    reply_to_text: str | None = None,
):
    # Initialize chat history if needed
    if chat_id not in chat_history:
        chat_history[chat_id] = deque(maxlen=20)

    # Add to history
    chat_history[chat_id].append(
        {
            "message_id": message_id,
            "sender": sender,
            "text": text,
            "user_id": user_id,
            "reply_to_id": reply_to_id,
            "reply_to_username": reply_to_username,
            "reply_to_text": reply_to_text,
        }
    )


async def get_message_media_description(message) -> str | None:
    """
    Analyzes media in a message and returns a description.
    Returns None if no media is found or analysis fails/is not applicable.
    """
    media_description = ""
    file_unique_id = None

    # Check for Photo
    if message.photo:
        # Get the largest photo
        photo = message.photo[-1]
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
    elif message.sticker:
        sticker = message.sticker
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
    elif message.video or message.animation:
        video = message.video or message.animation
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
    elif message.document:
        doc = message.document
        media_description = f"[User sent a document: {doc.file_name} ({doc.mime_type})]"

    return media_description if media_description else None


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

    media_description = await get_message_media_description(update.message)

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
        "spotify.com",
        "soundcloud.com",
        "bandcamp.com",
        "mixcloud.com",
        "twitch.tv",
    ]

    # Construct regex pattern from domains
    # Escaping dots and creating a non-capturing group
    domain_pattern = "|".join([re.escape(d) for d in target_domains])
    url_pattern = rf"(https?://(?:www\.)?(?:{domain_pattern})/[^\s]+)"

    urls = re.findall(url_pattern, text)

    if urls:
        # Check if utils are disabled
        if await get_utils_disabled(chat_id):
            logging.info(f"Utils disabled in chat {chat_id}, ignoring URLs.")
        else:
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
                elif "spotify.com" in url:
                    cookies_path = os.path.join(COOKIES_DIR, "spotify.txt")
                elif "soundcloud.com" in url:
                    cookies_path = os.path.join(COOKIES_DIR, "soundcloud.txt")
                elif "bandcamp.com" in url:
                    cookies_path = os.path.join(COOKIES_DIR, "bandcamp.txt")
                elif "mixcloud.com" in url:
                    cookies_path = os.path.join(COOKIES_DIR, "mixcloud.txt")
                elif "twitch.tv" in url:
                    cookies_path = os.path.join(COOKIES_DIR, "twitch.txt")

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
    reply_to_text = None

    if update.message.reply_to_message:
        reply_to_msg = update.message.reply_to_message
        reply_to_id = reply_to_msg.message_id
        reply_to_username = (
            reply_to_msg.from_user.username or reply_to_msg.from_user.first_name
        )
        reply_to_text = reply_to_msg.text or reply_to_msg.caption
        if not reply_to_text:
            desc = await get_message_media_description(reply_to_msg)
            reply_to_text = desc if desc else "[Media]"

    add_message_to_history(
        chat_id,
        message_id,
        user.username or user.first_name,
        text,
        user.id,
        reply_to_id,
        reply_to_username,
        reply_to_text,
    )

    bot_username = context.bot.username
    if not bot_username:
        # Fallback if bot username not yet available (shouldn't happen usually)
        bot_username = "@Bot"

    if await should_reply(update.message, f"@{bot_username}", chat_id):
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

            # Split content by |||
            messages_to_send = [
                part.strip() for part in content.split("|||") if part.strip()
            ]

            for i, msg_text in enumerate(messages_to_send):
                try:
                    # Only the first message uses the reply_to_message_id
                    current_reply_to = reply_to if i == 0 else None

                    sent_msg = None
                    if current_reply_to:
                        sent_msg = await context.bot.send_message(
                            chat_id=chat_id,
                            text=msg_text,
                            reply_to_message_id=current_reply_to,
                        )
                    else:
                        sent_msg = await context.bot.send_message(
                            chat_id=chat_id, text=msg_text
                        )

                    if sent_msg:
                        add_message_to_history(
                            chat_id,
                            sent_msg.message_id,
                            bot_username,
                            msg_text,
                            sent_msg.from_user.id,
                            reply_to_id=current_reply_to,
                            reply_to_username=None,  # We could look this up if needed, but it's less critical for bot's own msg
                            reply_to_text=None,
                        )
                except Exception as e:
                    logging.error(f"Failed to send message part: {e}")

    # Reaction Logic
    if await should_react(chat_id):
        logging.info("Decided to react...")
        emoji = await generate_reaction(text)
        if emoji:
            try:
                # emoji should be a single character or a list of ReactionType
                # python-telegram-bot expects 'reaction' argument which can be a string (emoji) or ReactionType
                # We'll assume the LLM returns a single emoji char.
                logging.info(f"Reacting with: {emoji}")
                await context.bot.set_message_reaction(
                    chat_id=chat_id, message_id=message_id, reaction=emoji
                )
            except Exception as e:
                logging.error(f"Failed to set reaction: {e}")

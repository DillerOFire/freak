import logging
from collections import deque
from telegram import Update
from telegram.ext import ContextTypes
from bot.logic import should_reply, get_paused, should_react, get_utils_disabled
from bot.llm import generate_response, generate_reaction
from bot.agent import run_ponder_agent
from bot.memory import (
    get_user_thought,
    get_relevant_general_memories,
    get_media_description,
    save_media_description,
    is_whitelisted,
    save_reusable_media,
    get_saved_media_options,
    get_saved_media_by_unique_id,
    mark_saved_media_used,
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
from xml.sax.saxutils import escape as xml_escape

# In-memory chat history (store last 20 messages per chat)
# Map: chat_id -> deque
chat_history: dict[int, deque] = {}



_PONDER_DEFERRAL_MARKERS = (
    "гляну",
    "проверю",
    "поищу",
    "узнаю",
    "освежить",
    "погуглю",
    "look up",
    "checking",
    "check the",
    "search for",
    "find out",
    "research",
    "one moment",
    "минутку",
    "щас ",
    "ща ",
)

def _has_ponder_tool_call(response: dict) -> bool:
    return any(tc.get("name") == "ponder" for tc in response.get("tool_calls", []))


def _llm_promised_research_without_ponder(response: dict) -> bool:
    """Detect wait-for-research messages when the model forgot the ponder tool_call."""
    if _has_ponder_tool_call(response):
        return False
    messages = response.get("messages", [])
    if not messages:
        return False
    combined = " ".join(m.lower() for m in messages if isinstance(m, str))
    return any(marker in combined for marker in _PONDER_DEFERRAL_MARKERS)


def _derive_ponder_query(user_text: str, memory_query: str | None = None) -> str:
    source = (user_text or memory_query or "").strip()
    if not source:
        return "general information request"
    return source[:500]


async def _complete_ponder_followup(
    *,
    ponder_query: str,
    chat_id: int,
    message_id: int,
    current_history: list,
    user_thoughts: dict,
    general_memories: list,
    memory_query: str,
    saved_media_options: list,
    bot_username: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    ponder_result = await run_ponder_agent(ponder_query, chat_id)

    extra_context = (
        f'\n<ponder_result query="{xml_escape(ponder_query)}">'
        f"\n{xml_escape(ponder_result)}"
        f"\n</ponder_result>"
        f"\n<instruction>You previously requested research via ponder."
        f" The results are in <ponder_result>. Use them to compose your reply."
        f" Do NOT call ponder again.</instruction>"
    )

    response2 = await generate_response(
        list(current_history),
        user_thoughts,
        general_memories,
        chat_id,
        focus_message_id=message_id,
        source="ponder",
        memory_query=memory_query,
        saved_media_options=saved_media_options,
        extra_context=extra_context,
    )

    if response2:
        await _send_llm_response(response2, chat_id, bot_username, context)
        return

    logging.error(
        "Ponder follow-up LLM returned no reply (query=%r, result=%r)",
        ponder_query,
        ponder_result[:200],
    )
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Хм, исследование завершилось, но я не смог собрать ответ — спросите иначе, букашки.",
            reply_to_message_id=message_id,
        )
    except Exception as e:
        logging.error(f"Failed to send ponder fallback message: {e}")


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


async def get_message_media_description(
    message,
    *,
    chat_id: int | None = None,
    sender_user_id: int | None = None,
    save_reusable: bool = False,
) -> str | None:
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
        file_unique_id = getattr(photo, "file_unique_id", None)
        file_id = getattr(photo, "file_id", None)

        if file_unique_id:
            # Check cache
            cached_desc = await get_media_description(file_unique_id)
            if cached_desc:
                media_description = f"[User sent a photo: {cached_desc}]"
                logging.info(f"Found photo description in cache: {cached_desc}")
                description = cached_desc
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

            # Save reusable media if valid
            if save_reusable and chat_id is not None and file_id and description and not description.startswith("Error"):
                await save_reusable_media(
                    chat_id, file_unique_id, file_id, "photo", description, sender_user_id
                )
        else:
            # Fallback if no file_unique_id
            logging.info("Photo lacks file_unique_id, skipping analysis/saving.")

    # Check for Sticker
    elif message.sticker:
        sticker = message.sticker
        file_unique_id = getattr(sticker, "file_unique_id", None)
        file_id = getattr(sticker, "file_id", None)

        if file_unique_id:
            # Check cache
            cached_desc = await get_media_description(file_unique_id)
            if cached_desc:
                media_description = f"[User sent a sticker: {cached_desc}]"
                description = cached_desc
            else:
                description = None
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

            # Save reusable media if valid
            if save_reusable and chat_id is not None and file_id and description and not description.startswith("Error"):
                await save_reusable_media(
                    chat_id, file_unique_id, file_id, "sticker", description, sender_user_id
                )
        else:
            logging.info("Sticker lacks file_unique_id, skipping analysis/saving.")

    # Check for GIF (Telegram animation)
    elif message.animation:
        animation = message.animation
        file_unique_id = getattr(animation, "file_unique_id", None)
        file_id = getattr(animation, "file_id", None)

        if file_unique_id:
            cached_desc = await get_media_description(file_unique_id)
            if cached_desc:
                media_description = f"[User sent a gif: {cached_desc}]"
                description = cached_desc
            else:
                logging.info("Analyzing new gif...")
                file = await animation.get_file()
                file_path = await download_file(file)

                frames = extract_frames_from_video(file_path)
                if frames:
                    description = await analyze_frames(frames)
                    if not description.startswith("Error"):
                        await save_media_description(file_unique_id, description)
                    media_description = f"[User sent a gif: {description}]"
                else:
                    description = None
                    media_description = "[User sent a gif (could not analyze)]"

                os.remove(file_path)

            if save_reusable and chat_id is not None and file_id and description and not description.startswith("Error"):
                await save_reusable_media(
                    chat_id, file_unique_id, file_id, "animation", description, sender_user_id
                )
        else:
            logging.info("Gif lacks file_unique_id, skipping analysis/saving.")

    # Check for Video
    elif message.video:
        video = message.video
        file_unique_id = video.file_unique_id

        cached_desc = await get_media_description(file_unique_id)
        if cached_desc:
            media_description = f"[User sent a video: {cached_desc}]"
        else:
            logging.info("Analyzing new video...")
            file = await video.get_file()
            file_path = await download_file(file)

            frames = extract_frames_from_video(file_path)
            if frames:
                description = await analyze_frames(frames)
                if not description.startswith("Error"):
                    await save_media_description(file_unique_id, description)
                media_description = f"[User sent a video: {description}]"
            else:
                media_description = "[User sent a video (could not analyze)]"

            os.remove(file_path)

    # Check for GIF sent as a document
    elif message.document and getattr(message.document, "mime_type", None) == "image/gif":
        doc = message.document
        file_unique_id = getattr(doc, "file_unique_id", None)
        file_id = getattr(doc, "file_id", None)

        if file_unique_id:
            cached_desc = await get_media_description(file_unique_id)
            if cached_desc:
                media_description = f"[User sent a gif: {cached_desc}]"
                description = cached_desc
            else:
                logging.info("Analyzing gif document...")
                file = await doc.get_file()
                file_path = await download_file(file)

                frames = extract_frames_from_video(file_path)
                if frames:
                    description = await analyze_frames(frames)
                    if not description.startswith("Error"):
                        await save_media_description(file_unique_id, description)
                    media_description = f"[User sent a gif: {description}]"
                else:
                    description = None
                    media_description = "[User sent a gif (could not analyze)]"

                os.remove(file_path)

            if save_reusable and chat_id is not None and file_id and description and not description.startswith("Error"):
                await save_reusable_media(
                    chat_id, file_unique_id, file_id, "animation", description, sender_user_id
                )
        else:
            logging.info("Gif document lacks file_unique_id, skipping analysis/saving.")

    # Check for Document
    elif message.document:
        doc = message.document
        media_description = f"[User sent a document: {doc.file_name} ({doc.mime_type})]"

    return media_description if media_description else None



async def send_saved_media_reply(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    media: dict,
    reply_to_message_id: int | None = None,
):
    if media["media_type"] == "photo":
        return await context.bot.send_photo(
            chat_id=chat_id, photo=media["file_id"], reply_to_message_id=reply_to_message_id
        )
    elif media["media_type"] == "sticker":
        return await context.bot.send_sticker(
            chat_id=chat_id, sticker=media["file_id"], reply_to_message_id=reply_to_message_id
        )
    elif media["media_type"] == "animation":
        return await context.bot.send_animation(
            chat_id=chat_id, animation=media["file_id"], reply_to_message_id=reply_to_message_id
        )
    else:
        logging.error(f"Unknown media type: {media.get('media_type')}")
        return None




async def _send_llm_response(
    response: dict,
    chat_id: int,
    bot_username: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    reply_to = response.get("reply_to_message_id")
    messages_to_send = [msg.strip() for msg in response.get("messages", []) if isinstance(msg, str) and msg.strip()]
    media_reply_unique_id = response.get("media_reply_unique_id")

    if media_reply_unique_id:
        try:
            media_row = await get_saved_media_by_unique_id(chat_id, media_reply_unique_id)
            if media_row:
                sent_media_msg = await send_saved_media_reply(context, chat_id, media_row, reply_to)
                if sent_media_msg:
                    await mark_saved_media_used(chat_id, media_reply_unique_id)
                    m_type = media_row["media_type"]
                    m_desc = media_row["description"]
                    add_message_to_history(
                        chat_id,
                        sent_media_msg.message_id,
                        bot_username,
                        f"[Bot sent saved {m_type}: {m_desc}]",
                        sent_media_msg.from_user.id,
                        reply_to_id=reply_to,
                        reply_to_username=None,
                        reply_to_text=None,
                    )
                    reply_to = None
            else:
                logging.error(f"Saved media row missing for id: {media_reply_unique_id}")
        except Exception as e:
            logging.error(f"Failed to send saved media reply: {e}")

    for i, msg_text in enumerate(messages_to_send):
        try:
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
                    reply_to_username=None,
                    reply_to_text=None,
                )
        except Exception as e:
            logging.error(f"Failed to send message part: {e}")

    for poll in response.get("polls", []):
        try:
            sent_poll = await context.bot.send_poll(
                chat_id=chat_id,
                question=poll["question"],
                options=poll["options"],
                is_anonymous=poll.get("is_anonymous", True),
                allows_multiple_answers=poll.get("allows_multiple_answers", False),
            )

            if sent_poll:
                add_message_to_history(
                    chat_id,
                    sent_poll.message_id,
                    bot_username,
                    f"[Poll] {poll['question']}: {' | '.join(poll['options'])}",
                    sent_poll.from_user.id,
                    reply_to_id=None,
                    reply_to_username=None,
                    reply_to_text=None,
                )
        except Exception as e:
            logging.error(f"Failed to send poll: {e}")

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
        if not await is_whitelisted(chat_id):
            return

    media_description = await get_message_media_description(
        update.message,
        chat_id=chat_id,
        sender_user_id=user.id,
        save_reusable=True,
    )

    text = update.message.text or update.message.caption or ""

    # Check for Video URLs (YouTube, Instagram, X, etc.)
    target_domains = [
        "youtube.com",
        "youtu.be",
        "instagram.com",
        "tiktok.com",
        "vt.tiktok.com",
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
        "vk.com",
        "rutube.ru",
        "vkvideo.ru",
    ]

    # Construct regex pattern from domains
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
                elif "vk.com" in url or "vkvideo.ru" in url:
                    cookies_path = os.path.join(COOKIES_DIR, "vk.txt")
                elif "rutube.ru" in url:
                    cookies_path = os.path.join(COOKIES_DIR, "rutube.txt")

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

    sender_name = user.username or user.first_name
    if getattr(user, "is_bot", False):
        sender_name = f"bot:{sender_name}"

    add_message_to_history(
        chat_id,
        message_id,
        sender_name,
        text,
        user.id,
        reply_to_id,
        reply_to_username,
        reply_to_text,
    )

    bot_username = context.bot.username
    if not bot_username:
        bot_username = "@Bot"

    if await should_reply(update.message, f"@{bot_username}", chat_id):
        logging.info("Decided to reply...")

        # Gather context
        user_thoughts = {}
        current_history = chat_history[chat_id]
        involved_user_ids = set(msg["user_id"] for msg in current_history)
        for uid in involved_user_ids:
            thought = await get_user_thought(uid)
            if thought:
                username = next(
                    (msg["sender"] for msg in current_history if msg["user_id"] == uid),
                    "Unknown",
                )
                user_thoughts[username] = thought

        # Relevance queries from real context
        memory_query = "\n".join(msg.get("text", "") for msg in list(current_history)[-8:])
        general_memories = await get_relevant_general_memories(chat_id, memory_query, limit=5)

        # Fetch saved media options
        saved_media_options = await get_saved_media_options(chat_id)

        # Generate response
        response = await generate_response(
            list(current_history),
            user_thoughts,
            general_memories,
            chat_id,
            focus_message_id=message_id,
            source="message",
            memory_query=memory_query,
            saved_media_options=saved_media_options,
        )

        if response:
            ponder_calls = [
                tc for tc in response.get("tool_calls", [])
                if tc["name"] == "ponder"
            ]

            ponder_query: str | None = None

            if ponder_calls:
                await _send_llm_response(response, chat_id, bot_username, context)
                ponder_query = ponder_calls[0]["arguments"].get("query", "")
            elif _llm_promised_research_without_ponder(response):
                logging.warning(
                    "LLM promised research without ponder tool_call; running fallback ponder"
                )
                await _send_llm_response(response, chat_id, bot_username, context)
                ponder_query = _derive_ponder_query(text, memory_query)
            else:
                await _send_llm_response(response, chat_id, bot_username, context)

            if ponder_query:
                await _complete_ponder_followup(
                    ponder_query=ponder_query,
                    chat_id=chat_id,
                    message_id=message_id,
                    current_history=list(current_history),
                    user_thoughts=user_thoughts,
                    general_memories=general_memories,
                    memory_query=memory_query,
                    saved_media_options=saved_media_options,
                    bot_username=bot_username,
                    context=context,
                )

    # Reaction Logic
    if await should_react(chat_id):
        logging.info("Decided to react...")
        emoji = await generate_reaction(text)
        if emoji:
            try:
                logging.info(f"Reacting with: {emoji}")
                await context.bot.set_message_reaction(
                    chat_id=chat_id, message_id=message_id, reaction=emoji
                )
            except Exception as e:
                logging.error(f"Failed to set reaction: {e}")

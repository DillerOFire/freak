import os
import logging

from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes
from config import COOKIES_DIR, ADMIN_ID
from bot.jobs import (
    schedule_daily_message,
    schedule_daily_task,
    remove_job_if_exists,
)
from bot.memory import (
    add_whitelist,
    remove_whitelist,
    get_whitelist,
    get_user_thought,
    get_general_memories,
    get_config,
    set_config,
    set_daily_message,
    set_daily_task,
    remove_daily_message,
    remove_daily_task,
    get_daily_message,
    get_daily_task,
)
from bot.logic import (
    set_paused,
    set_reply_chance,
    set_reaction_chance,
    set_cooldown_threshold,
    get_logic_config,
    get_paused,
    set_utils_disabled,
    get_utils_disabled,
)
from bot.handlers import add_message_to_history


async def update_cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler for /update_cookies <service>
    Expects a file attachment (cookies.txt).
    """
    logging.info(f"Cookies being updated with file by user {update.effective_user.id}")
    if not update.message:
        return

    # Admin check
    if update.effective_user.id != ADMIN_ID:
        # Silently ignore or reply? User asked to restrict.
        # Let's reply to indicate permission denied if they try to use it.
        # Or maybe silent is better for security.
        # "Restrict command using to be used only by user id"
        # Let's just return.
        return

    args = context.args
    if not args and update.message.caption:
        # If no args but there is a caption (file attached), try to parse from caption
        # Caption might be "/update_cookies <service>"
        parts = update.message.caption.split()
        if len(parts) > 1 and "update_cookies" in parts[0]:
            args = parts[1:]

    if not args:
        await update.message.reply_text(
            "Usage: /update_cookies <service>\nServices: youtube, instagram, x, vk, rutube"
        )
        return

    service = args[0].lower()
    if service not in [
        "youtube",
        "instagram",
        "x",
        "tiktok",
        "facebook",
        "reddit",
        "pinterest",
        "twitter",
        "spotify",
        "soundcloud",
        "bandcamp",
        "mixcloud",
        "twitch",
        "vk",
        "rutube",
    ]:
        await update.message.reply_text(
            "Invalid service. Supported: youtube, instagram, x, tiktok, facebook, reddit, pinterest, spotify, soundcloud, bandcamp, mixcloud, twitch, vk, rutube"
        )
        return

    target_path = os.path.join(COOKIES_DIR, f"{service}.txt")

    # Check for file attachment
    document = update.message.document
    if not document and update.message.reply_to_message:
        document = update.message.reply_to_message.document

    if document:
        file = await document.get_file()
        try:
            await file.download_to_drive(target_path)
            await update.message.reply_text(
                f"Cookies for {service} updated successfully (from file)."
            )
            logging.info(
                f"Cookies updated for {service} by user {update.effective_user.id}"
            )
            return
        except Exception as e:
            logging.error(f"Failed to save cookies for {service}: {e}")
            await update.message.reply_text("Failed to save cookies file.")
            return

    # Check for text content
    # We need to extract the content after the service name
    # The message text is likely "/update_cookies <service> <content>"
    # We can split by maxsplit=2 to get the content
    parts = update.message.text.split(maxsplit=2)
    if len(parts) >= 3:
        cookie_content = parts[2]
        try:
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(cookie_content)
            await update.message.reply_text(
                f"Cookies for {service} updated successfully (from text)."
            )
            logging.info(
                f"Cookies updated for {service} by user {update.effective_user.id}"
            )
            return
        except Exception as e:
            logging.error(f"Failed to save cookies for {service}: {e}")
            await update.message.reply_text("Failed to save cookies file.")
            return

    await update.message.reply_text(
        "Please attach a cookies.txt file or paste the content after the service name."
    )


async def whitelist_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    # Usage: /whitelist_add <id> <type> OR /whitelist_add (in group)

    entity_id = None
    entity_type = None

    if not args:
        # Contextual add
        chat_type = update.effective_chat.type
        if chat_type in ["group", "supergroup"]:
            entity_id = update.effective_chat.id
            entity_type = "group"
        else:
            await update.message.reply_text(
                "Usage: /whitelist_add <id> <type> (user/group)"
            )
            return
    elif len(args) == 2:
        try:
            entity_id = int(args[0])
            entity_type = args[1].lower()
            if entity_type not in ["user", "group"]:
                await update.message.reply_text("Type must be 'user' or 'group'.")
                return
        except ValueError:
            await update.message.reply_text("ID must be an integer.")
            return
    else:
        await update.message.reply_text(
            "Usage: /whitelist_add <id> <type> (user/group)"
        )
        return

    await add_whitelist(entity_id, entity_type, update.effective_user.id)
    await update.message.reply_text(f"Added {entity_type} {entity_id} to whitelist.")


async def whitelist_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args or len(args) != 1:
        await update.message.reply_text("Usage: /whitelist_remove <id>")
        return

    try:
        entity_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID must be an integer.")
        return

    success = await remove_whitelist(entity_id)
    if success:
        await update.message.reply_text(f"Removed {entity_id} from whitelist.")
    else:
        await update.message.reply_text(f"ID {entity_id} not found in whitelist.")


async def whitelist_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    rows = await get_whitelist()
    if not rows:
        await update.message.reply_text("Whitelist is empty.")
        return

    msg = "Whitelist:\n"
    for row in rows:
        # row: entity_id, entity_type, timestamp
        msg += f"{row[0]} ({row[1]}) - {row[2]}\n"

    await update.message.reply_text(msg)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return
    set_paused(True)
    await update.message.reply_text("Bot paused.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return
    set_paused(False)
    await update.message.reply_text("Bot resumed.")


async def stop_utils_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return
    await set_utils_disabled(update.effective_chat.id, True)
    await update.message.reply_text(
        "Utils (video/sound downloading) disabled for this chat."
    )


async def start_utils_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return
    await set_utils_disabled(update.effective_chat.id, False)
    await update.message.reply_text("Utils enabled for this chat.")


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    # If replied to a message, show that user's ID
    if update.message.reply_to_message:
        replied_user = update.message.reply_to_message.from_user
        msg = f"User ID: {replied_user.id}\nChat ID: {update.effective_chat.id}"
    else:
        msg = (
            f"Chat ID: {update.effective_chat.id}\nYour ID: {update.effective_user.id}"
        )

    sent_msg = await update.message.reply_text(msg)
    if sent_msg:
        add_message_to_history(
            update.effective_chat.id,
            sent_msg.message_id,
            context.bot.username or "@Bot",
            msg,
            sent_msg.from_user.id,
            reply_to_id=update.message.message_id,
        )


async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    # If replied to a message, get thoughts about that user
    if update.message.reply_to_message:
        target_user_id = update.message.reply_to_message.from_user.id
        target_username = (
            update.message.reply_to_message.from_user.username
            or update.message.reply_to_message.from_user.first_name
        )
        thought = await get_user_thought(target_user_id)
        if thought:
            sent_msg = await update.message.reply_text(
                f"Memories of {target_username}:\n{thought}"
            )
            if sent_msg:
                add_message_to_history(
                    update.effective_chat.id,
                    sent_msg.message_id,
                    context.bot.username or "@Bot",
                    f"Memories of {target_username}:\n{thought}",
                    sent_msg.from_user.id,
                    reply_to_id=update.message.message_id,
                )
        else:
            await update.message.reply_text(f"No memories found for {target_username}.")
    else:
        # Get general memories
        memories = await get_general_memories(update.effective_chat.id, limit=10)
        if memories:
            msg = "General Memories:\n\n" + "\n\n".join(memories)
            sent_msg = await update.message.reply_text(msg)
            if sent_msg:
                add_message_to_history(
                    update.effective_chat.id,
                    sent_msg.message_id,
                    context.bot.username or "@Bot",
                    msg,
                    sent_msg.from_user.id,
                    reply_to_id=update.message.message_id,
                )
        else:
            await update.message.reply_text("No general memories found.")


async def update_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /update_prompt <new prompt text>")
        return

    # Join all args to form the new prompt
    # Or better, take the rest of the message text to preserve formatting
    # The command is /update_prompt <text>
    # We can split by maxsplit=1
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /update_prompt <new prompt text>")
        return

    new_prompt = parts[1]
    await set_config("persona_prompt", new_prompt)
    await update.message.reply_text("System prompt updated successfully.")
    logging.info(f"System prompt updated by {update.effective_user.id}")


async def show_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    current_prompt = await get_config("persona_prompt")
    if not current_prompt:
        await update.message.reply_text("No custom prompt set (using default).")
    else:
        await update.message.reply_text(f"Current System Prompt:\n\n{current_prompt}")


async def set_reply_chance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /set_reply_chance <0.0-1.0>")
        return

    try:
        val = float(args[0])
        if not (0 <= val <= 1):
            raise ValueError
        await set_reply_chance(update.effective_chat.id, val)
        await update.message.reply_text(f"Reply chance set to {val} for this chat.")
    except ValueError:
        await update.message.reply_text(
            "Invalid value. Must be a float between 0 and 1."
        )


async def set_reaction_chance_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /set_reaction_chance <0.0-1.0>")
        return

    try:
        val = float(args[0])
        if not (0 <= val <= 1):
            raise ValueError
        await set_reaction_chance(update.effective_chat.id, val)
        await update.message.reply_text(f"Reaction chance set to {val} for this chat.")
    except ValueError:
        await update.message.reply_text(
            "Invalid value. Must be a float between 0 and 1."
        )


async def set_cooldown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /set_cooldown <int>")
        return

    try:
        val = int(args[0])
        if val < 0:
            raise ValueError
        await set_cooldown_threshold(update.effective_chat.id, val)
        await update.message.reply_text(
            f"Cooldown threshold set to {val} for this chat."
        )
    except ValueError:
        await update.message.reply_text(
            "Invalid value. Must be a non-negative integer."
        )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    cooldown, reply_chance, reaction_chance = await get_logic_config(
        update.effective_chat.id
    )

    msg = (
        f"Current Settings (Chat {update.effective_chat.id}):\n"
        f"Reply Chance: {reply_chance}\n"
        f"Reaction Chance: {reaction_chance}\n"
        f"Cooldown Threshold: {cooldown}\n"
        f"Paused: {get_paused()}"
    )
    await update.message.reply_text(msg)


async def music_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /music <url>")
        return

    url = args[0]

    # Check if utils are disabled
    if await get_utils_disabled(update.effective_chat.id):
        return

    # Determine service for cookies (reuse logic if possible, or just check domain)
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

    # We need to import download_audio_ytdlp here or at top level.
    # It's in media_utils. Let's import it inside to avoid circular deps if any,
    # but better to import at top.
    # Wait, I am editing commands.py, but I added the import to handlers.py in the previous step!
    # I made a mistake in the previous step. I should have added the import to commands.py if I put the command there.
    # However, commands.py doesn't import media_utils yet.
    # Let's add the import to commands.py in a separate step or just do it here if I can.
    # I can't do two edits in one tool call easily if they are far apart.
    # I will add the function here, and then add the import at the top.

    from bot.media_utils import download_audio_ytdlp

    result = download_audio_ytdlp(url, cookies_path)

    if result:
        audio_path = result.get("audio_path")
        title = result.get("title", "Unknown Title")
        description = result.get("description", "")
        thumbnail_path = result.get("thumbnail_path")
        duration = result.get("duration")
        uploader = result.get("uploader")

        # Truncate description if too long (Telegram limit is 1024 chars for caption)
        caption = f"{title}\n\n{description}"
        if len(caption) > 1000:
            caption = caption[:997] + "..."

        try:
            # Prepare thumbnail
            thumb_file = open(thumbnail_path, "rb") if thumbnail_path else None

            await update.message.reply_audio(
                audio=open(audio_path, "rb"),
                title=title,
                performer=uploader,
                duration=duration,
                thumbnail=thumb_file,
                caption=caption,
                reply_to_message_id=update.message.message_id,
            )

            # Cleanup
            if thumb_file:
                thumb_file.close()

            os.remove(audio_path)
            if thumbnail_path and os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)

        except Exception as e:
            logging.error(f"Failed to send audio: {e}")
            await update.message.reply_text("Failed to send audio file.")

            # Cleanup on error
            if os.path.exists(audio_path):
                os.remove(audio_path)
            if thumbnail_path and os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)
    else:
        await update.message.reply_text("Failed to download audio.")


async def add_daily_msg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /add_daily_msg <HH:MM> (reply to a message)"
        )
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "You must reply to a message to set it as daily."
        )
        return

    time_str = args[0]
    try:
        t = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        await update.message.reply_text("Invalid time format. Use HH:MM.")
        return

    reply = update.message.reply_to_message

    # Determine type and content
    message_type = "text"
    content = reply.text or reply.caption or ""
    file_id = None

    if reply.photo:
        message_type = "photo"
        file_id = reply.photo[-1].file_id
    elif reply.video:
        message_type = "video"
        file_id = reply.video.file_id
    elif reply.sticker:
        message_type = "sticker"
        file_id = reply.sticker.file_id
    elif reply.animation:
        message_type = "animation"
        file_id = reply.animation.file_id
    elif reply.document:
        message_type = "document"
        file_id = reply.document.file_id
    elif not content:
        await update.message.reply_text("Unsupported message type or empty content.")
        return

    chat_id = update.effective_chat.id

    await set_daily_message(chat_id, time_str, message_type, content, file_id)
    schedule_daily_message(
        context.application, chat_id, t, message_type, content, file_id
    )

    await update.message.reply_text(f"Daily message scheduled for {time_str}.")


async def add_daily_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /add_daily_task <HH:MM> <task content/prompt>"
        )
        return

    time_str = args[0]
    try:
        t = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        await update.message.reply_text("Invalid time format. Use HH:MM.")
        return

    task_content = " ".join(args[1:])
    chat_id = update.effective_chat.id

    await set_daily_task(chat_id, time_str, task_content)
    schedule_daily_task(context.application, chat_id, t, task_content)

    await update.message.reply_text(
        f"Daily task scheduled for {time_str}: {task_content}"
    )


async def daily_cancel_msg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return
    chat_id = update.effective_chat.id
    await remove_daily_message(chat_id)
    remove_job_if_exists(f"daily_msg_{chat_id}", context.application)
    await update.message.reply_text("Daily message cancelled.")


async def daily_cancel_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return
    chat_id = update.effective_chat.id
    await remove_daily_task(chat_id)
    remove_job_if_exists(f"daily_task_{chat_id}", context.application)
    await update.message.reply_text("Daily task cancelled.")


async def daily_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    chat_id = update.effective_chat.id
    msg = await get_daily_message(chat_id)
    task = await get_daily_task(chat_id)

    response = "Daily Schedules:\n"
    if msg:
        response += f"Message: {msg['time']} ({msg['message_type']})\n"
    else:
        response += "Message: None\n"

    if task:
        response += f"Task: {task['time']} - {task['task_content']}\n"
    else:
        response += "Task: None\n"

    await update.message.reply_text(response)

import os
import logging
import shlex

from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from config import COOKIES_DIR, ADMIN_ID
from bot.jobs import (
    schedule_daily_message,
    schedule_daily_task,
    remove_job_if_exists,
    check_bot_update_job,
)
from bot.system import update_ytdlp_package, restart_bot
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
    get_user_memory_by_target,
    search_user_memories,
    search_general_memories,
)
from bot.logic import (
    set_paused,
    set_reply_chance,
    set_reaction_chance,
    set_max_ping_pong,
    set_cooldown_threshold,
    get_logic_config,
    get_max_ping_pong,
    get_paused,
    set_utils_disabled,
    get_utils_disabled,
)
from bot.llm import generate_reaction_prompt
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
        return

    args = context.args
    if not args and update.message.caption:
        # If no args but there is a caption (file attached), try to parse from caption
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
        msg += f"{row[0]} ({row[1]}) - {row[2]}\n"

    await update.message.reply_text(msg)


async def update_ytdlp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text("Updating yt-dlp... this may take a moment.")
    success, message = await update_ytdlp_package()
    await update.message.reply_text(f"Update Result:\n{message}")

    if success and "updated successfully" in message:
        await update.message.reply_text("Restarting bot to apply changes...")
        import asyncio

        await asyncio.sleep(2)
        restart_bot()


async def update_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text("Checking for bot updates...")
    await check_bot_update_job(context)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return
    await set_paused(True)
    await update.message.reply_text("Bot paused.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return
    await set_paused(False)
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


def _parse_memory_args(text: str) -> tuple[str | None, str | None]:
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None, None
    remainder = parts[1]
    tokens = shlex.split(remainder)
    if not tokens:
        return None, None
    
    if len(tokens) == 1:
        if tokens[0] == ".":
            return None, None
        return tokens[0], None
        
    # two or more tokens
    return tokens[0], " ".join(tokens[1:])


async def memories_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    text = update.message.text or ""
    
    # Check if there are args provided (not just /memory or /memories command)
    has_args = len(text.split(maxsplit=1)) > 1

    # Usage routing
    try:
        target, query = _parse_memory_args(text)
    except ValueError:
        await update.message.reply_text("Usage: /memory [.|@username|user_id|username] [\"query\"]")
        return

    # Case 1: reply check and no parsed target/query
    if update.message.reply_to_message and not has_args:
        target_user_id = update.message.reply_to_message.from_user.id
        target_username = (
            update.message.reply_to_message.from_user.username
            or update.message.reply_to_message.from_user.first_name
        )
        thought = await get_user_thought(target_user_id)
        if thought:
            msg = f"Memories of {target_username}:\n{thought}"
            sent_msg = await update.message.reply_text(msg)
            if sent_msg:
                add_message_to_history(
                    chat_id,
                    sent_msg.message_id,
                    context.bot.username or "@Bot",
                    msg,
                    sent_msg.from_user.id,
                    reply_to_id=update.message.message_id,
                )
        else:
            await update.message.reply_text(f"No memories found for {target_username}.")
        return

    # Case 2: no parsed target/query and not a reply
    if not has_args:
        memories = await get_general_memories(chat_id, limit=10)
        if memories:
            msg = "General Memories:\n\n" + "\n\n".join(memories)
            sent_msg = await update.message.reply_text(msg)
            if sent_msg:
                add_message_to_history(
                    chat_id,
                    sent_msg.message_id,
                    context.bot.username or "@Bot",
                    msg,
                    sent_msg.from_user.id,
                    reply_to_id=update.message.message_id,
                )
        else:
            await update.message.reply_text("No general memories found.")
        return

    # Case 3: query is present, target is None or "."
    if query and (target is None or target == "."):
        user_mems = await search_user_memories(query, limit=10)
        gen_mems = await search_general_memories(chat_id, query, limit=10)
        
        if not user_mems and not gen_mems:
            await update.message.reply_text(f"No memories found for \"{query}\".")
            return
            
        reply_lines = [f"Memory search for \"{query}\":"]
        if user_mems:
            reply_lines.append("\nUser Memories:")
            for uid, uname, thought in user_mems:
                reply_lines.append(f"- {uname} (ID: {uid}): {thought}")
        if gen_mems:
            reply_lines.append("\nGeneral Memories:")
            for mem in gen_mems:
                reply_lines.append(f"- {mem}")
                
        await update.message.reply_text("\n".join(reply_lines))
        return

    # Case 4: target is present and query is None
    if target and not query:
        row = await get_user_memory_by_target(target)
        if row:
            uid, uname, thought = row
            await update.message.reply_text(f"Memories of {uname} (ID: {uid}):\n{thought}")
        else:
            await update.message.reply_text(f"No memories found for {target}.")
        return

    # Case 5: both target and query are present
    if target and query:
        row = await get_user_memory_by_target(target)
        gen_mems = await search_general_memories(chat_id, f"{target} {query}", limit=10)
        
        if not row and not gen_mems:
            await update.message.reply_text(f"No memories found for {target} / \"{query}\".")
            return
            
        reply_lines = [f"Memory search for {target} / \"{query}\":"]
        if row:
            uid, uname, thought = row
            reply_lines.append("\nUser Memories:")
            reply_lines.append(f"- {uname} (ID: {uid}): {thought}")
        if gen_mems:
            reply_lines.append("\nGeneral Memories:")
            for mem in gen_mems:
                reply_lines.append(f"- {mem}")
                
        await update.message.reply_text("\n".join(reply_lines))
        return


async def update_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /update_prompt <new prompt text>")
        return

    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /update_prompt <new prompt text>")
        return

    new_prompt = parts[1]
    await set_config("persona_prompt", new_prompt)
    reaction_prompt = await generate_reaction_prompt(new_prompt)
    await set_config("reaction_prompt", reaction_prompt)
    await update.message.reply_text("System and reaction prompts updated successfully.")
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
        chance = float(args[0])
        if not (0.0 <= chance <= 1.0):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Chance must be a float between 0.0 and 1.0")
        return

    await set_reply_chance(update.effective_chat.id, chance)
    await update.message.reply_text(f"Reply chance set to {chance}")


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
        chance = float(args[0])
        if not (0.0 <= chance <= 1.0):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Chance must be a float between 0.0 and 1.0")
        return

    await set_reaction_chance(update.effective_chat.id, chance)
    await update.message.reply_text(f"Reaction chance set to {chance}")


async def set_cooldown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /set_cooldown <seconds>")
        return

    try:
        cooldown = int(args[0])
        if cooldown < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Cooldown must be a non-negative integer")
        return

    await set_cooldown_threshold(update.effective_chat.id, cooldown)
    await update.message.reply_text(f"Cooldown threshold set to {cooldown} seconds")


async def set_max_ping_pong_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /set_max_ping_pong <count>")
        return

    try:
        max_ping_pong = int(args[0])
        if max_ping_pong < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Maximum ping pong must be a non-negative integer")
        return

    await set_max_ping_pong(update.effective_chat.id, max_ping_pong)
    await update.message.reply_text(f"Maximum ping pong set to {max_ping_pong}")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    chat_id = update.effective_chat.id
    text, keyboard = await _build_settings_panel(chat_id)
    await update.message.reply_text(text, reply_markup=keyboard)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    if query.from_user.id != ADMIN_ID:
        await query.answer("Admin only.", show_alert=True)
        return

    if not query.message or not query.data:
        await query.answer()
        return

    chat_id = query.message.chat_id
    action, _, value = query.data.partition(":")
    if action != "settings":
        await query.answer()
        return

    if value == "toggle_pause":
        await set_paused(not get_paused())
    elif value == "toggle_utils":
        await set_utils_disabled(chat_id, not await get_utils_disabled(chat_id))
    elif value.startswith("reply="):
        await set_reply_chance(chat_id, float(value.removeprefix("reply=")))
    elif value.startswith("reaction="):
        await set_reaction_chance(chat_id, float(value.removeprefix("reaction=")))
    elif value.startswith("cooldown="):
        await set_cooldown_threshold(chat_id, int(value.removeprefix("cooldown=")))
    elif value.startswith("pingpong="):
        await set_max_ping_pong(chat_id, int(value.removeprefix("pingpong=")))
    elif value.startswith("adj_reply="):
        cooldown, reply_chance, reaction_chance = await get_logic_config(chat_id)
        delta = float(value.removeprefix("adj_reply="))
        new_val = max(0.0, min(1.0, round(reply_chance + delta, 2)))
        await set_reply_chance(chat_id, new_val)
    elif value.startswith("adj_reaction="):
        cooldown, reply_chance, reaction_chance = await get_logic_config(chat_id)
        delta = float(value.removeprefix("adj_reaction="))
        new_val = max(0.0, min(1.0, round(reaction_chance + delta, 2)))
        await set_reaction_chance(chat_id, new_val)
    elif value.startswith("adj_cooldown="):
        cooldown, reply_chance, reaction_chance = await get_logic_config(chat_id)
        delta = int(value.removeprefix("adj_cooldown="))
        new_val = max(0, cooldown + delta)
        await set_cooldown_threshold(chat_id, new_val)
    elif value.startswith("adj_pingpong="):
        max_ping_pong = await get_max_ping_pong(chat_id)
        delta = int(value.removeprefix("adj_pingpong="))
        new_val = max(0, max_ping_pong + delta)
        await set_max_ping_pong(chat_id, new_val)
    elif value == "noop":
        await query.answer()
        return
    elif value != "refresh":
        await query.answer("Unknown setting.", show_alert=True)
        return

    text, keyboard = await _build_settings_panel(chat_id)
    import telegram
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise
    await query.answer("Settings updated.")


async def _build_settings_panel(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cooldown, reply_chance, reaction_chance = await get_logic_config(chat_id)
    max_ping_pong = await get_max_ping_pong(chat_id)
    paused = get_paused()
    utils_disabled = await get_utils_disabled(chat_id)

    text = (
        f"Settings for Chat {chat_id}:\n\n"
        f"Reply Chance: {reply_chance * 100:.0f}%\n"
        f"Reaction Chance: {reaction_chance * 100:.0f}%\n"
        f"Cooldown Threshold: {cooldown} messages\n"
        f"Max Ping Pong: {max_ping_pong} replies\n"
        f"Bot Paused: {paused}\n"
        f"Utils Disabled: {utils_disabled}\n\n"
        "Use buttons to adjust values, or commands for exact values."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Resume bot" if paused else "Pause bot",
                    callback_data="settings:toggle_pause",
                ),
                InlineKeyboardButton(
                    "Enable utils" if utils_disabled else "Disable utils",
                    callback_data="settings:toggle_utils",
                ),
            ],
            [
                InlineKeyboardButton("Reply:", callback_data="settings:noop"),
                InlineKeyboardButton("-10%", callback_data="settings:adj_reply=-0.1"),
                InlineKeyboardButton("-1%", callback_data="settings:adj_reply=-0.01"),
                InlineKeyboardButton("+1%", callback_data="settings:adj_reply=0.01"),
                InlineKeyboardButton("+10%", callback_data="settings:adj_reply=0.1"),
            ],
            [
                InlineKeyboardButton("React:", callback_data="settings:noop"),
                InlineKeyboardButton("-10%", callback_data="settings:adj_reaction=-0.1"),
                InlineKeyboardButton("-1%", callback_data="settings:adj_reaction=-0.01"),
                InlineKeyboardButton("+1%", callback_data="settings:adj_reaction=0.01"),
                InlineKeyboardButton("+10%", callback_data="settings:adj_reaction=0.1"),
            ],
            [
                InlineKeyboardButton("Cooldown:", callback_data="settings:noop"),
                InlineKeyboardButton("-5", callback_data="settings:adj_cooldown=-5"),
                InlineKeyboardButton("-1", callback_data="settings:adj_cooldown=-1"),
                InlineKeyboardButton("+1", callback_data="settings:adj_cooldown=1"),
                InlineKeyboardButton("+5", callback_data="settings:adj_cooldown=5"),
            ],
            [
                InlineKeyboardButton("Ping pong:", callback_data="settings:noop"),
                InlineKeyboardButton("-1", callback_data="settings:adj_pingpong=-1"),
                InlineKeyboardButton("+1", callback_data="settings:adj_pingpong=1"),
            ],
            [InlineKeyboardButton("Refresh", callback_data="settings:refresh")],
        ]
    )
    return text, keyboard


async def music_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    if await get_utils_disabled(chat_id):
        await update.message.reply_text("Utils are disabled in this chat.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /music <url>")
        return

    url = args[0]
    await update.message.reply_text("Downloading audio... this might take a moment.")

    # Import ytdlp helper
    from bot.media_utils import download_audio_ytdlp

    cookies_path = None
    if "youtube.com" in url or "youtu.be" in url:
        cookies_path = os.path.join(COOKIES_DIR, "youtube.txt")

    info = download_audio_ytdlp(url, cookies_path)
    if info:
        audio_path = info["audio_path"]
        title = info["title"]
        uploader = info.get("uploader", "Unknown")
        duration = info.get("duration")
        thumbnail_path = info.get("thumbnail_path")

        try:
            # Send audio
            # open thumbnail if exists
            thumb = open(thumbnail_path, "rb") if thumbnail_path else None

            await update.message.reply_audio(
                audio=open(audio_path, "rb"),
                title=title,
                performer=uploader,
                duration=duration,
                thumbnail=thumb,
            )

            # Cleanup
            if thumb:
                thumb.close()
                os.remove(thumbnail_path)
            os.remove(audio_path)

        except Exception as e:
            logging.error(f"Failed to send audio file: {e}")
            await update.message.reply_text("Failed to send audio file.")
            # Cleanup on fail
            if thumbnail_path and os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)
            if os.path.exists(audio_path):
                os.remove(audio_path)
    else:
        await update.message.reply_text("Failed to download audio.")


async def add_daily_msg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    # Check for reply
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Usage: /add_daily_msg <HH:MM> (Reply to the message you want to schedule)"
        )
        return

    args = context.args
    if not args or len(args) != 1:
        await update.message.reply_text("Usage: /add_daily_msg <HH:MM>")
        return

    time_str = args[0]
    try:
        t = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        await update.message.reply_text("Invalid time format. Use HH:MM.")
        return

    replied_msg = update.message.reply_to_message
    chat_id = update.effective_chat.id

    # Determine message type and content
    message_type = "text"
    content = replied_msg.text or replied_msg.caption or ""
    file_id = None

    if replied_msg.photo:
        message_type = "photo"
        file_id = replied_msg.photo[-1].file_id
    elif replied_msg.video:
        message_type = "video"
        file_id = replied_msg.video.file_id
    elif replied_msg.sticker:
        message_type = "sticker"
        file_id = replied_msg.sticker.file_id
    elif replied_msg.animation:
        message_type = "animation"
        file_id = replied_msg.animation.file_id
    elif replied_msg.document:
        message_type = "document"
        file_id = replied_msg.document.file_id

    # Save to db
    await set_daily_message(chat_id, time_str, message_type, content, file_id)

    # Schedule
    schedule_daily_message(
        context.application, chat_id, t, message_type, content, file_id
    )

    await update.message.reply_text(f"Daily message scheduled for {time_str}.")


async def add_daily_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != ADMIN_ID:
        return

    # Usage: /add_daily_task <HH:MM> <task_content>
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /add_daily_task <HH:MM> <prompt>")
        return

    time_str = args[0]
    try:
        t = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        await update.message.reply_text("Invalid time format. Use HH:MM.")
        return

    task_content = " ".join(args[1:])
    chat_id = update.effective_chat.id

    # Save to db
    await set_daily_task(chat_id, time_str, task_content)

    # Schedule
    schedule_daily_task(context.application, chat_id, t, task_content)

    await update.message.reply_text(f"Daily task scheduled for {time_str}.")


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

    response = "Active schedules for this chat:\n\n"
    if msg:
        response += (
            f"Message: {msg['time']} - type: {msg['message_type']} - '{msg['content'][:30]}'\n"
        )
    else:
        response += "Message: None\n"

    if task:
        response += f"Task: {task['time']} - {task['task_content']}\n"
    else:
        response += "Task: None\n"

    await update.message.reply_text(response)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
<b>Available commands</b>

<b>General</b>
- <code>/help</code> - Show this message.
- <code>/ping</code> - Check bot, chat, and user info.
- <code>/music &lt;url&gt;</code> - Download audio from supported services.
- <code>/memory [.|@user|user_id|username] ["query"]</code> - Search or inspect memories.
- <code>/memories</code> - Alias for <code>/memory</code>.

<b>Daily schedules</b>
- <code>/add_daily_msg &lt;HH:MM&gt;</code> - Reply to a message to send it every day.
- <code>/add_daily_task &lt;HH:MM&gt; &lt;prompt&gt;</code> - Run an LLM prompt every day.
- <code>/daily_list</code> - List active schedules for this chat.
- <code>/daily_cancel_msg</code> - Cancel the daily message.
- <code>/daily_cancel_task</code> - Cancel the daily task.

<b>Admin configuration</b>
- <code>/settings</code> - Show and change settings with buttons.
- <code>/set_reply_chance &lt;0.0-1.0&gt;</code> - Set chance to reply to random messages.
- <code>/set_reaction_chance &lt;0.0-1.0&gt;</code> - Set chance to react to messages.
- <code>/set_cooldown &lt;seconds&gt;</code> - Set cooldown between auto-replies.
- <code>/set_max_ping_pong &lt;count&gt;</code> - Cap bot-to-bot reply chains.
- <code>/update_prompt &lt;text&gt;</code> - Update the system prompt.
- <code>/show_prompt</code> - Show the current system prompt.

<b>Admin management</b>
- <code>/stop</code> - Pause the bot.
- <code>/start</code> - Resume the bot.
- <code>/stop_utils</code> - Disable media downloading in this chat.
- <code>/start_utils</code> - Enable media downloading in this chat.
- <code>/update_cookies &lt;service&gt;</code> - Update cookies from an attached file or text.
- <code>/whitelist_add &lt;id&gt; &lt;type&gt;</code> - Add a user or group to the whitelist.
- <code>/whitelist_remove &lt;id&gt;</code> - Remove an ID from the whitelist.
- <code>/whitelist_list</code> - List whitelisted entities.
- <code>/update_ytdlp</code> - Update yt-dlp manually.
- <code>/update_bot</code> - Check for bot updates and restart if found.
"""
    await update.message.reply_text(help_text, parse_mode="HTML")

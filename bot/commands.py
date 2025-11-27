import os
import logging
from telegram import Update
from telegram.ext import ContextTypes
from config import COOKIES_DIR, ADMIN_ID
from bot.memory import (
    add_whitelist,
    remove_whitelist,
    get_whitelist,
    get_user_thought,
    get_general_memories,
)
from bot.logic import set_paused


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
    if not args:
        await update.message.reply_text(
            "Usage: /update_cookies <service>\nServices: youtube, instagram, x"
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
    ]:
        await update.message.reply_text(
            "Invalid service. Supported: youtube, instagram, x, tiktok, facebook, reddit, pinterest"
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

    await update.message.reply_text(msg)


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
            await update.message.reply_text(
                f"Memories of {target_username}:\n{thought}"
            )
        else:
            await update.message.reply_text(f"No memories found for {target_username}.")
    else:
        # Get general memories
        memories = await get_general_memories(update.effective_chat.id, limit=10)
        if memories:
            msg = "General Memories:\n\n" + "\n\n".join(memories)
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text("No general memories found.")

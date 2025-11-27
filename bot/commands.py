import os
import logging
from telegram import Update
from telegram.ext import ContextTypes
from config import COOKIES_DIR


async def update_cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler for /update_cookies <service>
    Expects a file attachment (cookies.txt).
    """
    if not update.message:
        return

    args = context.args
    if not args or len(args) != 1:
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
    if update.message.document:
        file = await update.message.document.get_file()
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

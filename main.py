import logging
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters
from config import TELEGRAM_BOT_TOKEN
from bot.handlers import handle_message
from bot.commands import (
    update_cookies_command,
    whitelist_add_command,
    whitelist_remove_command,
    whitelist_list_command,
    stop_command,
    start_command,
    ping_command,
    memories_command,
    update_prompt_command,
    show_prompt_command,
    set_reply_chance_command,
    set_reaction_chance_command,
    set_cooldown_command,
    settings_command,
    music_command,
    stop_utils_command,
    start_utils_command,
    add_daily_msg_command,
    add_daily_task_command,
    daily_cancel_msg_command,
    daily_cancel_task_command,
    daily_list_command,
    update_ytdlp_command,
    update_bot_command,
    help_command,
)
from bot.jobs import load_jobs
from bot.memory import init_db
from bot.logic import init_logic


# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Silence httpx logs
logging.getLogger("httpx").setLevel(logging.WARNING)


async def post_init(application):
    await init_db()
    await init_logic()
    logging.info("Database and Logic Config initialized.")
    await load_jobs(application)


def main():
    # Build Application
    application = (
        ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    )

    # Add Handlers
    # Handle all text messages that are not commands
    # Handle all text messages that are not commands, and media
    msg_handler = MessageHandler(
        (
            filters.TEXT
            | filters.PHOTO
            | filters.Sticker.ALL
            | filters.VIDEO
            | filters.ANIMATION
            | filters.Document.ALL
        )
        & (~filters.COMMAND),
        handle_message,
    )
    application.add_handler(msg_handler)

    # Command Handlers
    application.add_handler(CommandHandler("update_cookies", update_cookies_command))
    application.add_handler(CommandHandler("whitelist_add", whitelist_add_command))
    application.add_handler(
        CommandHandler("whitelist_remove", whitelist_remove_command)
    )
    application.add_handler(CommandHandler("whitelist_list", whitelist_list_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("memories", memories_command))
    application.add_handler(CommandHandler("update_prompt", update_prompt_command))
    application.add_handler(CommandHandler("show_prompt", show_prompt_command))
    application.add_handler(
        CommandHandler("set_reply_chance", set_reply_chance_command)
    )
    application.add_handler(
        CommandHandler("set_reaction_chance", set_reaction_chance_command)
    )
    application.add_handler(CommandHandler("set_cooldown", set_cooldown_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("music", music_command))
    application.add_handler(CommandHandler("stop_utils", stop_utils_command))
    application.add_handler(CommandHandler("start_utils", start_utils_command))
    application.add_handler(CommandHandler("add_daily_msg", add_daily_msg_command))
    application.add_handler(CommandHandler("add_daily_task", add_daily_task_command))
    application.add_handler(
        CommandHandler("daily_cancel_msg", daily_cancel_msg_command)
    )
    application.add_handler(
        CommandHandler("daily_cancel_task", daily_cancel_task_command)
    )
    application.add_handler(CommandHandler("daily_list", daily_list_command))
    application.add_handler(CommandHandler("update_ytdlp", update_ytdlp_command))
    application.add_handler(CommandHandler("update_bot", update_bot_command))
    application.add_handler(CommandHandler("help", help_command))

    logging.info("Bot started polling...")
    application.run_polling()


if __name__ == "__main__":
    main()

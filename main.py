import logging
import asyncio
from telegram.ext import ApplicationBuilder, MessageHandler, filters
from config import TELEGRAM_BOT_TOKEN
from bot.handlers import handle_message
from bot.memory import init_db

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Silence httpx logs
logging.getLogger("httpx").setLevel(logging.WARNING)


async def post_init(application):
    await init_db()
    logging.info("Database initialized.")


def main():
    # Build Application
    application = (
        ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    )

    # Add Handlers
    # Handle all text messages that are not commands
    msg_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    application.add_handler(msg_handler)

    logging.info("Bot started polling...")
    application.run_polling()


if __name__ == "__main__":
    main()

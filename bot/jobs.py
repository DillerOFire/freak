import logging
from datetime import datetime, time
from telegram.ext import Application, ContextTypes
from bot.memory import get_all_daily_messages, get_all_daily_tasks, get_general_memories
from bot.llm import generate_response
from config import ADMIN_ID
from bot.system import update_ytdlp_package


async def send_daily_message_callback(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    content = job_data["content"]
    message_type = job_data["message_type"]
    file_id = job_data.get("file_id")

    logging.info(f"Sending daily message to {chat_id}")

    try:
        if message_type == "text":
            await context.bot.send_message(chat_id=chat_id, text=content)
        elif message_type == "photo":
            await context.bot.send_photo(
                chat_id=chat_id, photo=file_id, caption=content
            )
        elif message_type == "video":
            await context.bot.send_video(
                chat_id=chat_id, video=file_id, caption=content
            )
        elif message_type == "sticker":
            await context.bot.send_sticker(chat_id=chat_id, sticker=file_id)
        elif message_type == "animation":
            await context.bot.send_animation(
                chat_id=chat_id, animation=file_id, caption=content
            )
        elif message_type == "document":  # Added just in case
            await context.bot.send_document(
                chat_id=chat_id, document=file_id, caption=content
            )
    except Exception as e:
        logging.error(f"Failed to send daily message to {chat_id}: {e}")


async def execute_daily_task_callback(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    task_content = job_data["task_content"]

    logging.info(f"Executing daily task for {chat_id}: {task_content}")

    try:
        # Fetch general memories for context
        # We assume 0 is a system user ID or similar for the 'sender'
        general_memories = await get_general_memories(chat_id, limit=10)

        # Construct a synthetic message context
        # This makes the LLM treat the task content as a message to respond to
        # or an instruction.
        messages_context = [
            {
                "message_id": 0,
                "sender": "DailyTaskScheduler",
                "user_id": 0,
                "text": f"Instruction: {task_content}",
                "reply_to_username": None,
                "reply_to_text": None,
            }
        ]

        # We pass empty user_thoughts as we don't have a specific user context here
        response_json = await generate_response(
            messages_context=messages_context,
            user_thoughts={},
            general_memories=general_memories,
            chat_id=chat_id,
        )

        if response_json and "content" in response_json:
            await context.bot.send_message(
                chat_id=chat_id, text=response_json["content"]
            )

    except Exception as e:
        logging.error(f"Failed to execute daily task for {chat_id}: {e}")


async def check_ytdlp_update_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Checks for yt-dlp updates and notifies the admin if updated.
    """
    logging.info("Checking for yt-dlp updates...")
    success, message = await update_ytdlp_package()

    if success and "updated successfully" in message:
        # Notify admin
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID, text=f"System Update:\n{message}"
            )
        except Exception as e:
            logging.error(f"Failed to notify admin about update: {e}")
    elif not success:
        # Notify admin about failure
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID, text=f"System Update Failed:\n{message}"
            )
        except Exception as e:
            logging.error(f"Failed to notify admin about update failure: {e}")


def schedule_daily_message(
    application, chat_id, time_obj, message_type, content, file_id=None
):
    job_name = f"daily_msg_{chat_id}"
    remove_job_if_exists(job_name, application)

    logging.info(f"Scheduling daily msg for {chat_id} at {time_obj}")
    application.job_queue.run_daily(
        send_daily_message_callback,
        time=time_obj,
        data={
            "chat_id": chat_id,
            "message_type": message_type,
            "content": content,
            "file_id": file_id,
        },
        name=job_name,
    )


def schedule_daily_task(application, chat_id, time_obj, task_content):
    job_name = f"daily_task_{chat_id}"
    remove_job_if_exists(job_name, application)

    logging.info(f"Scheduling daily task for {chat_id} at {time_obj}")
    application.job_queue.run_daily(
        execute_daily_task_callback,
        time=time_obj,
        data={"chat_id": chat_id, "task_content": task_content},
        name=job_name,
    )


def schedule_ytdlp_update_check(application):
    """
    Schedules the yt-dlp update check.
    Runs once 10 seconds after startup, and then every 24 hours.
    """
    # Run once shortly after startup
    application.job_queue.run_once(
        check_ytdlp_update_job, when=10, name="ytdlp_update_check_startup"
    )

    # Run daily
    # We can pick a fixed time, e.g., 04:00 UTC, or just an interval.
    # run_repeating is better for interval if we don't care about specific time.
    # But run_daily is usually preferred for bots. Let's do run_daily at 04:00.
    # Or just run_repeating every 24 hours.
    # Let's use run_repeating for simplicity as we don't need a specific time.
    application.job_queue.run_repeating(
        check_ytdlp_update_job,
        interval=86400,
        first=86400,  # Start the repeating one after 24h, since we run one immediately
        name="ytdlp_update_check_daily",
    )


def remove_job_if_exists(name: str, application: Application):
    current_jobs = application.job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return
    for job in current_jobs:
        job.schedule_removal()


async def load_jobs(application: Application):
    logging.info("Loading scheduled jobs from database...")

    try:
        # Schedule system jobs
        schedule_ytdlp_update_check(application)

        messages = await get_all_daily_messages()
        for msg in messages:
            chat_id = msg["chat_id"]
            time_str = msg["time"]
            try:
                t = datetime.strptime(time_str, "%H:%M").time()
                schedule_daily_message(
                    application,
                    chat_id,
                    t,
                    msg["message_type"],
                    msg["content"],
                    msg["file_id"],
                )
            except ValueError:
                logging.error(
                    f"Invalid time format for daily message chat_id={chat_id}: {time_str}"
                )

        tasks = await get_all_daily_tasks()
        for task in tasks:
            chat_id = task["chat_id"]
            time_str = task["time"]
            try:
                t = datetime.strptime(time_str, "%H:%M").time()
                schedule_daily_task(application, chat_id, t, task["task_content"])
            except ValueError:
                logging.error(
                    f"Invalid time format for daily task chat_id={chat_id}: {time_str}"
                )

    except Exception as e:
        logging.error(f"Error loading jobs: {e}")

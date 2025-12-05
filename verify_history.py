import sys
import os
from collections import deque

# Add project root to path
sys.path.append(os.getcwd())

from bot.handlers import add_message_to_history, chat_history


def test_history_update():
    print("Testing add_message_to_history...")

    chat_id = 12345

    # Add a user message
    add_message_to_history(
        chat_id=chat_id,
        message_id=100,
        sender="User1",
        text="Hello",
        user_id=1,
    )

    # Add a bot message
    add_message_to_history(
        chat_id=chat_id,
        message_id=101,
        sender="Bot",
        text="Hi there!",
        user_id=999,
        reply_to_id=100,
    )

    # Verify history
    history = chat_history.get(chat_id)
    if not history:
        print("FAILURE: History not initialized.")
        return

    if len(history) != 2:
        print(f"FAILURE: Expected 2 messages, got {len(history)}.")
        return

    msg1 = history[0]
    msg2 = history[1]

    print("Message 1:", msg1)
    print("Message 2:", msg2)

    if (
        msg1["text"] == "Hello"
        and msg2["text"] == "Hi there!"
        and msg2["reply_to_id"] == 100
    ):
        print("\nSUCCESS: Messages correctly added to history.")
    else:
        print("\nFAILURE: Message content mismatch.")


if __name__ == "__main__":
    test_history_update()

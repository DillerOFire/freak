from bot.prompt_history import PromptHistoryEpoch, PromptHistoryStore


def _message(message_id: int) -> dict:
    return {"message_id": message_id, "text": f"message {message_id}"}


def test_prompt_history_freezes_twenty_messages_then_grows_a_ten_message_tail():
    epoch = PromptHistoryEpoch()
    for message_id in range(1, 31):
        epoch.append(_message(message_id))

    messages, stable_count = epoch.snapshot()

    assert [message["message_id"] for message in messages] == list(range(1, 31))
    assert stable_count == 20


def test_prompt_history_rotates_ten_messages_together_after_tail_overflow():
    epoch = PromptHistoryEpoch()
    for message_id in range(1, 32):
        epoch.append(_message(message_id))

    messages, stable_count = epoch.snapshot()

    assert [message["message_id"] for message in messages] == list(range(11, 32))
    assert stable_count == 20


def test_prompt_history_store_reset_starts_a_fresh_epoch():
    store = PromptHistoryStore()
    store.append(7, _message(1))
    store.reset(7)

    assert store.snapshot(7) == ([], 0)

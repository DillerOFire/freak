"""Cache-epoch history for prompt prefix reuse.

The ordinary handler history stays capped at 20 messages. This module keeps a
separate prompt view whose first 20 messages remain stable while a 10-message
tail grows. Once the tail overflows, ten old messages are removed together and
a new stable prefix starts.
"""

from __future__ import annotations

from dataclasses import dataclass, field


STABLE_HISTORY_MESSAGES = 20
HISTORY_TAIL_MESSAGES = 10


@dataclass
class PromptHistoryEpoch:
    """One chat's stable cache prefix and changing message tail."""

    stable: list[dict] = field(default_factory=list)
    tail: list[dict] = field(default_factory=list)

    def append(self, message: dict) -> None:
        if len(self.stable) < STABLE_HISTORY_MESSAGES:
            self.stable.append(message)
            return

        self.tail.append(message)
        if len(self.tail) <= HISTORY_TAIL_MESSAGES:
            return

        combined = self.stable + self.tail
        remaining = combined[HISTORY_TAIL_MESSAGES:]
        self.stable = remaining[:STABLE_HISTORY_MESSAGES]
        self.tail = remaining[STABLE_HISTORY_MESSAGES:]

    def snapshot(self) -> tuple[list[dict], int]:
        messages = [*self.stable, *self.tail]
        stable_count = (
            STABLE_HISTORY_MESSAGES
            if len(self.stable) == STABLE_HISTORY_MESSAGES
            else 0
        )
        return messages, stable_count


class PromptHistoryStore:
    """Own cache epochs for every active chat."""

    def __init__(self) -> None:
        self._epochs: dict[int, PromptHistoryEpoch] = {}

    def reset(self, chat_id: int) -> None:
        self._epochs.pop(chat_id, None)

    def append(self, chat_id: int, message: dict) -> None:
        self._epochs.setdefault(chat_id, PromptHistoryEpoch()).append(message)

    def snapshot(self, chat_id: int) -> tuple[list[dict], int]:
        epoch = self._epochs.get(chat_id)
        if epoch is None:
            return [], 0
        return epoch.snapshot()

    def clear(self) -> None:
        self._epochs.clear()

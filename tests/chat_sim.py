"""
Realistic Telegram group-chat simulator for integration-style tests.

Drives the real message pipeline (handlers → logic → llm tool application → DB)
with a scripted OpenAI client and a mock bot that records outbound messages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

from bot import handlers, logic


@dataclass
class OutboundMessage:
    chat_id: int
    text: str
    reply_to_message_id: int | None = None


@dataclass
class ChatUser:
    user_id: int
    username: str
    is_bot: bool = False
    first_name: str | None = None

    @property
    def display(self) -> str:
        return self.username or self.first_name or str(self.user_id)


class ChatSimulator:
    """Multi-turn mocked group chat with real bot code paths."""

    def __init__(
        self,
        *,
        chat_id: int = -1005550001,
        bot_username: str = "freak_bot",
        bot_user_id: int = 9001,
        admin_id: int = 100,
    ):
        self.chat_id = chat_id
        self.bot_username = bot_username
        self.bot_user_id = bot_user_id
        self.admin_id = admin_id
        self._next_message_id = 1000
        self._next_sent_id = 50000

        self.outbound: list[OutboundMessage] = []
        self.llm_script: list[dict[str, Any]] = []
        self.llm_calls: list[dict[str, Any]] = []
        self.ponder_script: list[str] = []
        self.ponder_calls: list[str] = []

        self.bot = MagicMock()
        self.bot.username = bot_username
        self.bot.id = bot_user_id
        self.bot.send_message = AsyncMock(side_effect=self._record_send_message)
        self.bot.send_poll = AsyncMock(side_effect=self._record_send_poll)
        self.bot.send_photo = AsyncMock(return_value=self._fake_sent(60001))
        self.bot.send_sticker = AsyncMock(return_value=self._fake_sent(60002))
        self.bot.send_animation = AsyncMock(return_value=self._fake_sent(60003))
        self.bot.set_message_reaction = AsyncMock()

        self.context = MagicMock()
        self.context.bot = self.bot
        self.context.application = MagicMock()
        self.context.application.bot = self.bot
        self.context.job = MagicMock()

        # Clear per-chat handler state between sims
        handlers.chat_history.pop(chat_id, None)
        logic.messages_since_last_reply.pop(chat_id, None)
        logic.bot_reply_locks.pop(chat_id, None)
        logic.bot_ping_pong_counts.pop(chat_id, None)

    def _fake_sent(self, message_id: int | None = None) -> MagicMock:
        if message_id is None:
            self._next_sent_id += 1
            message_id = self._next_sent_id
        sent = MagicMock()
        sent.message_id = message_id
        sent.from_user = MagicMock()
        sent.from_user.id = self.bot_user_id
        sent.from_user.username = self.bot_username
        sent.from_user.is_bot = True
        return sent

    async def _record_send_message(self, **kwargs) -> MagicMock:
        text = kwargs.get("text", "")
        self.outbound.append(
            OutboundMessage(
                chat_id=kwargs.get("chat_id", self.chat_id),
                text=text,
                reply_to_message_id=kwargs.get("reply_to_message_id"),
            )
        )
        return self._fake_sent()

    async def _record_send_poll(self, **kwargs) -> MagicMock:
        question = kwargs.get("question", "")
        options = kwargs.get("options") or []
        self.outbound.append(
            OutboundMessage(
                chat_id=kwargs.get("chat_id", self.chat_id),
                text=f"[Poll] {question}: {' | '.join(options)}",
            )
        )
        return self._fake_sent()

    def script_llm(self, *responses: dict[str, Any]) -> None:
        """Queue JSON payloads the mock OpenAI client will return in order."""
        for response in responses:
            # Normalize to full LLM response shape
            payload = {
                "tool_calls": response.get("tool_calls", []),
                "reply_to_message_id": response.get("reply_to_message_id"),
                "messages": response.get("messages", []),
                "polls": response.get("polls", []),
            }
            self.llm_script.append(payload)

    def script_ponder(self, *answers: str) -> None:
        self.ponder_script.extend(answers)

    def _make_openai_response(self, content: dict | str) -> MagicMock:
        mock_response = MagicMock()
        mock_response.usage = None
        mock_choice = MagicMock()
        mock_message = MagicMock()
        mock_message.content = (
            content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        )
        mock_choice.message = mock_message
        mock_response.choices = [mock_choice]
        return mock_response

    async def _scripted_openai_create(self, **kwargs) -> MagicMock:
        messages = kwargs.get("messages") or []
        self.llm_calls.append({"messages": messages, "kwargs": kwargs})
        if not self.llm_script:
            # Default: no reply (empty messages)
            return self._make_openai_response(
                {
                    "tool_calls": [],
                    "reply_to_message_id": None,
                    "messages": [],
                    "polls": [],
                }
            )
        payload = self.llm_script.pop(0)
        return self._make_openai_response(payload)

    async def _scripted_ponder(self, query: str, chat_id: int, **kwargs) -> str:
        self.ponder_calls.append(query)
        if self.ponder_script:
            return self.ponder_script.pop(0)
        return f"[mock research] {query}"

    def _next_id(self) -> int:
        self._next_message_id += 1
        return self._next_message_id

    def build_update(
        self,
        user: ChatUser,
        text: str,
        *,
        message_id: int | None = None,
        reply_to_message_id: int | None = None,
        chat_type: str = "supergroup",
    ) -> MagicMock:
        mid = message_id if message_id is not None else self._next_id()
        update = MagicMock()
        update.effective_chat.id = self.chat_id
        update.effective_chat.type = chat_type
        update.effective_user.id = user.user_id
        update.effective_user.username = user.username

        message = MagicMock()
        message.message_id = mid
        message.text = text
        message.caption = None
        message.photo = None
        message.sticker = None
        message.video = None
        message.animation = None
        message.document = None
        message.chat = update.effective_chat
        message.chat.id = self.chat_id
        message.chat.type = chat_type

        from_user = MagicMock()
        from_user.id = user.user_id
        from_user.username = user.username
        from_user.first_name = user.first_name or user.username
        from_user.is_bot = user.is_bot
        message.from_user = from_user

        if reply_to_message_id is not None:
            reply = MagicMock()
            reply.message_id = reply_to_message_id
            reply.from_user = MagicMock()
            reply.from_user.username = self.bot_username
            reply.from_user.first_name = self.bot_username
            reply.from_user.id = self.bot_user_id
            reply.text = "[prior bot message]"
            reply.caption = None
            reply.photo = None
            reply.sticker = None
            reply.video = None
            reply.animation = None
            reply.document = None
            message.reply_to_message = reply
        else:
            message.reply_to_message = None

        update.message = message
        return update

    def _pipeline_patches(self):
        """Common patches: real LLM tool path, no media, scripted OpenAI + ponder."""
        return (
            patch("bot.handlers.get_paused", return_value=False),
            patch(
                "bot.handlers.get_message_media_description",
                new_callable=AsyncMock,
                return_value=(None, None),
            ),
            patch(
                "bot.handlers.should_react",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch.object(
                __import__("bot.llm", fromlist=["llm"]).client.chat.completions,
                "create",
                new=AsyncMock(side_effect=self._scripted_openai_create),
            ),
            patch(
                "bot.agent.run_ponder_agent",
                new=AsyncMock(side_effect=self._scripted_ponder),
            ),
            patch("bot.logic.ADMIN_ID", self.admin_id),
            patch("bot.handlers.ADMIN_ID", self.admin_id),
            patch("bot.llm.ADMIN_ID", self.admin_id),
            # Avoid flaky cooldown/chance: force reply decision when we want full path
            # Callers can still use force_reply=False and real should_reply.
        )

    async def user_says(
        self,
        user: ChatUser,
        text: str,
        *,
        force_reply: bool | None = True,
        mention_bot: bool = False,
        reply_to_bot: bool = False,
        chat_type: str = "supergroup",
    ) -> int:
        """
        Inject a user message into the real handle_message pipeline.

        force_reply:
          True  — always invoke LLM (default, for deterministic multi-turn scripts)
          False — use real should_reply (mentions / ignore states / chance)
          None  — same as False
        """
        body = text
        if mention_bot and f"@{self.bot_username}" not in text:
            body = f"@{self.bot_username} {text}"

        reply_to = None
        if reply_to_bot:
            reply_to = self._next_sent_id  # any prior id

        update = self.build_update(
            user,
            body,
            reply_to_message_id=reply_to,
            chat_type=chat_type,
        )
        message_id = update.message.message_id

        patches = list(self._pipeline_patches())
        if force_reply is True:
            patches.append(
                patch(
                    "bot.handlers.should_reply",
                    new_callable=AsyncMock,
                    return_value=True,
                )
            )
        # force_reply False/None → real should_reply (respects ignore states)

        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            await handlers.handle_message(update, self.context)

        return message_id

    async def fire_due_actions(self) -> int:
        """Run the scheduled-action poller once (same path as the 30s job)."""
        from bot.schedule import process_due_scheduled_actions

        patches = (
            patch.object(
                __import__("bot.llm", fromlist=["llm"]).client.chat.completions,
                "create",
                new=AsyncMock(side_effect=self._scripted_openai_create),
            ),
            patch(
                "bot.agent.run_ponder_agent",
                new=AsyncMock(side_effect=self._scripted_ponder),
            ),
            patch("bot.llm.ADMIN_ID", self.admin_id),
        )
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return await process_due_scheduled_actions(self.context.application)

    def texts_sent(self) -> list[str]:
        return [m.text for m in self.outbound]

    def clear_outbound(self) -> None:
        self.outbound.clear()


# Convenient stock personas for scenarios
def default_users(admin_id: int = 100) -> dict[str, ChatUser]:
    return {
        "admin": ChatUser(user_id=admin_id, username="admin_boss"),
        "vasya": ChatUser(user_id=111, username="vasya"),
        "petya": ChatUser(user_id=222, username="petya"),
        "kolya": ChatUser(user_id=333, username="kolya"),
    }

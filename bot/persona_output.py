"""Ownership-safe mutations of the RP bot's Telegram output."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from telegram.error import RetryAfter

from bot.memory import (
    get_persona_output,
    mark_persona_output_deleted,
    mark_persona_output_edited,
    record_persona_output,
    record_persona_reaction,
)
from bot.messages import AvailableReactions


OUTPUT_MUTATION_TOOL_NAMES = frozenset(
    {"edit_own_message", "delete_own_message", "set_own_reactions"}
)
MAX_REACTION_CHANGES = 20
TELEGRAM_DELETE_LIMIT = timedelta(hours=48)


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EditOwnMessageArgs(_StrictArgs):
    message_id: int = Field(gt=0)
    replacement_text: str = Field(min_length=1, max_length=4096)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("replacement_text", "reason")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be blank")
        return value


class DeleteOwnMessageArgs(_StrictArgs):
    message_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason cannot be blank")
        return value


class ReactionChange(_StrictArgs):
    message_id: int = Field(gt=0)
    emoji: str | None = None

    @field_validator("emoji")
    @classmethod
    def validate_emoji(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value not in AvailableReactions:
            raise ValueError("emoji is not an allowed Telegram bot reaction")
        return value


class SetOwnReactionsArgs(_StrictArgs):
    changes: list[ReactionChange] = Field(min_length=1, max_length=MAX_REACTION_CHANGES)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason cannot be blank")
        return value

    @model_validator(mode="after")
    def unique_targets(self):
        ids = [change.message_id for change in self.changes]
        if len(ids) != len(set(ids)):
            raise ValueError("each message_id may appear only once")
        return self


@dataclass
class OutputActionReport:
    edited_message_ids: set[int] = field(default_factory=set)
    deleted_message_ids: set[int] = field(default_factory=set)
    reaction_message_ids: set[int] = field(default_factory=set)
    failures: list[str] = field(default_factory=list)

    @property
    def deliberately_reacted_message_ids(self) -> set[int]:
        return self.reaction_message_ids


def infer_output_kind(message: Any) -> Literal[
    "text", "caption", "photo", "sticker", "animation", "poll", "media"
]:
    if getattr(message, "text", None) is not None:
        return "text"
    if getattr(message, "caption", None) is not None:
        return "caption"
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "animation", None):
        return "animation"
    if getattr(message, "poll", None):
        return "poll"
    return "media"


async def record_sent_output(
    chat_id: int,
    message: Any,
    content_kind: str,
    text: str | None = None,
) -> bool:
    """Parse a Telegram Message at the boundary and add it to the ownership index."""
    message_id = getattr(message, "message_id", None)
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        logging.warning("Could not index persona output without an integer message_id")
        return False
    try:
        return await record_persona_output(
            chat_id,
            message_id,
            content_kind,
            text,
            sent_at=getattr(message, "date", None),
        )
    except Exception:
        logging.exception(
            "Failed to index persona output chat=%s message=%s",
            chat_id,
            message_id,
        )
        return False


async def record_reaction_state(
    chat_id: int, message_id: int, emoji: str | None
) -> bool:
    try:
        await record_persona_reaction(chat_id, message_id, emoji)
        return True
    except Exception:
        logging.exception(
            "Failed to index persona reaction chat=%s message=%s",
            chat_id,
            message_id,
        )
        return False


def _history_target_ids(history: Iterable[dict[str, Any]]) -> set[int]:
    targets: set[int] = set()
    for entry in history:
        for key in ("message_id", "reply_to_id"):
            value = entry.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                targets.add(value)
    return targets


def _update_history_after_edit(
    history: Iterable[dict[str, Any]], message_id: int, replacement_text: str
) -> None:
    for entry in history:
        if entry.get("message_id") == message_id:
            entry["text"] = replacement_text
            entry["edited"] = True


def _update_history_after_delete(
    history: Iterable[dict[str, Any]], message_id: int
) -> None:
    for entry in history:
        if entry.get("message_id") == message_id:
            entry["deleted"] = True


def _is_within_delete_limit(sent_at: object) -> bool:
    text = str(sent_at or "").strip()
    if not text:
        return True
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - parsed < TELEGRAM_DELETE_LIMIT


async def _apply_edit(
    bot: Any,
    chat_id: int,
    args: EditOwnMessageArgs,
    history: Iterable[dict[str, Any]],
    report: OutputActionReport,
) -> None:
    output = await get_persona_output(chat_id, args.message_id)
    if not output or output.get("state") not in {"active", "edited"}:
        report.failures.append(f"edit_own_message:{args.message_id}:not_owned_or_inactive")
        return

    kind = output.get("content_kind")
    try:
        if kind == "text":
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=args.message_id,
                text=args.replacement_text,
            )
        elif kind == "caption":
            if len(args.replacement_text) > 1024:
                report.failures.append(f"edit_own_message:{args.message_id}:caption_too_long")
                return
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=args.message_id,
                caption=args.replacement_text,
            )
        else:
            report.failures.append(f"edit_own_message:{args.message_id}:not_editable")
            return
    except Exception as error:
        logging.warning("Failed to edit persona message %s: %s", args.message_id, error)
        report.failures.append(f"edit_own_message:{args.message_id}:{type(error).__name__}")
        return

    await mark_persona_output_edited(chat_id, args.message_id, args.replacement_text)
    _update_history_after_edit(history, args.message_id, args.replacement_text)
    report.edited_message_ids.add(args.message_id)


async def _apply_delete(
    bot: Any,
    chat_id: int,
    args: DeleteOwnMessageArgs,
    history: Iterable[dict[str, Any]],
    report: OutputActionReport,
) -> None:
    output = await get_persona_output(chat_id, args.message_id)
    if not output or output.get("state") not in {"active", "edited"}:
        report.failures.append(f"delete_own_message:{args.message_id}:not_owned_or_inactive")
        return
    if not _is_within_delete_limit(output.get("sent_at")):
        report.failures.append(f"delete_own_message:{args.message_id}:too_old")
        return

    try:
        await bot.delete_message(chat_id=chat_id, message_id=args.message_id)
    except Exception as error:
        logging.warning("Failed to delete persona message %s: %s", args.message_id, error)
        report.failures.append(f"delete_own_message:{args.message_id}:{type(error).__name__}")
        return

    await mark_persona_output_deleted(chat_id, args.message_id)
    _update_history_after_delete(history, args.message_id)
    report.deleted_message_ids.add(args.message_id)


async def _set_one_reaction(
    bot: Any, chat_id: int, change: ReactionChange
) -> None:
    reaction: Sequence[str] | str = change.emoji if change.emoji is not None else []
    try:
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=change.message_id,
            reaction=reaction,
        )
    except RetryAfter as error:
        retry_after = min(float(error.retry_after), 5.0)
        await asyncio.sleep(retry_after)
        await bot.set_message_reaction(
            chat_id=chat_id,
            message_id=change.message_id,
            reaction=reaction,
        )
    await record_reaction_state(chat_id, change.message_id, change.emoji)


async def _reaction_target_is_allowed(
    chat_id: int, message_id: int, recent_target_ids: set[int]
) -> bool:
    if message_id in recent_target_ids:
        return True
    output = await get_persona_output(chat_id, message_id)
    return bool(output and output.get("state") in {"active", "edited"})


async def _apply_reactions(
    bot: Any,
    chat_id: int,
    args: SetOwnReactionsArgs,
    history: Iterable[dict[str, Any]],
    report: OutputActionReport,
) -> None:
    recent_target_ids = _history_target_ids(history)
    for index, change in enumerate(args.changes):
        if not await _reaction_target_is_allowed(
            chat_id, change.message_id, recent_target_ids
        ):
            report.failures.append(
                f"set_own_reactions:{change.message_id}:target_not_available"
            )
            continue
        try:
            await _set_one_reaction(bot, chat_id, change)
        except Exception as error:
            logging.warning(
                "Failed to set persona reaction on message %s: %s",
                change.message_id,
                error,
            )
            report.failures.append(
                f"set_own_reactions:{change.message_id}:{type(error).__name__}"
            )
            continue
        report.reaction_message_ids.add(change.message_id)
        if index < len(args.changes) - 1:
            await asyncio.sleep(random.uniform(0.15, 0.45))


async def apply_output_actions(
    bot: Any,
    chat_id: int,
    tool_calls: Iterable[dict[str, Any]],
    history: Iterable[dict[str, Any]],
) -> OutputActionReport:
    """Validate and apply RP-owned Telegram mutations from one LLM response."""
    report = OutputActionReport()
    message_mutation_used = False
    reaction_batch_used = False

    for call in tool_calls:
        name = call.get("name") if isinstance(call, dict) else None
        if name not in OUTPUT_MUTATION_TOOL_NAMES:
            continue
        arguments = call.get("arguments", {}) if isinstance(call, dict) else {}
        try:
            if name == "edit_own_message":
                if message_mutation_used:
                    report.failures.append("edit_own_message:mutation_limit")
                    continue
                message_mutation_used = True
                await _apply_edit(
                    bot,
                    chat_id,
                    EditOwnMessageArgs.model_validate(arguments),
                    history,
                    report,
                )
            elif name == "delete_own_message":
                if message_mutation_used:
                    report.failures.append("delete_own_message:mutation_limit")
                    continue
                message_mutation_used = True
                await _apply_delete(
                    bot,
                    chat_id,
                    DeleteOwnMessageArgs.model_validate(arguments),
                    history,
                    report,
                )
            elif name == "set_own_reactions":
                if reaction_batch_used:
                    report.failures.append("set_own_reactions:batch_limit")
                    continue
                reaction_batch_used = True
                await _apply_reactions(
                    bot,
                    chat_id,
                    SetOwnReactionsArgs.model_validate(arguments),
                    history,
                    report,
                )
        except ValidationError as error:
            logging.warning("Invalid %s arguments: %s", name, error)
            report.failures.append(f"{name}:invalid_arguments")

    return report

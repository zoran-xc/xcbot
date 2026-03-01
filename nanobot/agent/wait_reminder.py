"""AI-decided wait reminder: ask the model whether to keep waiting or send a message to the user."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Awaitable

from loguru import logger

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMProvider


class WaitReminderTimeout(Exception):
    """Raised when a blocking call hits the configured max wait time."""

    def __init__(self, message: str, elapsed_seconds: float):
        super().__init__(message)
        self.elapsed_seconds = elapsed_seconds


_SEND_MAX_CHARS = 1000  # Cap message length to avoid flooding


async def run_wait_decision(
    provider: "LLMProvider",
    model: str,
    elapsed_seconds: float,
    operation: str,
    task_summary: str | None = None,
    is_timeout: bool = False,
    max_tokens: int = 256,
) -> tuple[float, str | None]:
    """
    Ask the model: keep waiting or send a message to the user.
    Returns (next_interval_seconds, message_to_send_or_None).
    """
    if is_timeout:
        sys_content = (
            "You are a wait-state decision agent. The current operation has reached the maximum wait time. "
            "Reply with exactly one line: either WAIT or WAIT N (N = seconds for next check) or SEND: <message>. "
            "Prefer SEND to inform the user that the operation timed out and suggest pausing or confirming whether to continue. "
            "When using SEND, write the message in the same language as the user's task (e.g. use 中文 if the user context is in Chinese)."
        )
        user_content = (
            f"Already waited {int(elapsed_seconds)} seconds; operation: {operation}. "
            f"{'User task (match this language in SEND): ' + (task_summary or '') if task_summary else ''} "
            "Maximum wait reached. Reply with WAIT or SEND: <your short message to the user>."
        )
    else:
        sys_content = (
            "You are a wait-state decision agent. Based on how long we have been waiting and the current operation, "
            "reply with exactly one line: WAIT or WAIT N (N = seconds until next check, e.g. WAIT 10) or SEND: <message>. "
            "Use SEND when you want to tell the user something (e.g. still working, possibly slow). "
            "Message can use Markdown and newlines. Keep it brief. "
            "When using SEND, write in the same language as the user's task (e.g. use 中文 if the user context is in Chinese)."
        )
        user_content = (
            f"Already waited {int(elapsed_seconds)} seconds. Current operation: {operation}. "
            + (f"User task (match this language in SEND): {task_summary}. " if task_summary else "")
            + "Reply with only: WAIT or WAIT N or SEND: <message to user>."
        )

    messages = [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": user_content},
    ]
    try:
        response = await provider.chat(
            messages=messages,
            tools=[],  # No tools for this decision
            model=model,
            temperature=0.2,
            max_tokens=max_tokens,
        )
    except Exception as e:
        logger.warning("Wait decision LLM call failed: {}", e)
        return (5.0, None)  # Default: wait another 5s, no message

    text = (response.content or "").strip()
    # Normalize full-width colon so "SEND：" is parsed like "SEND:"
    if "SEND：" in text:
        text = text.replace("SEND：", "SEND:", 1)
    # Parse WAIT / WAIT N / SEND: ...
    if text.upper().startswith("SEND:"):
        msg = text[5:].strip()
        if msg:
            if len(msg) > _SEND_MAX_CHARS:
                msg = msg[:_SEND_MAX_CHARS] + "\n...(truncated)"
            return (5.0, msg)
        return (5.0, None)
    match = re.search(r"WAIT\s+(\d+)", text, re.IGNORECASE)
    if match:
        n = max(1, min(120, int(match.group(1))))
        return (float(n), None)
    if text.upper().strip() == "WAIT":
        return (5.0, None)
    # Unparseable: treat as WAIT
    logger.debug("Wait decision unparseable, treating as WAIT: {!r}", text[:200])
    return (5.0, None)


async def run_with_ai_wait_reminder(
    provider: "LLMProvider",
    bus: "MessageBus",
    coro: Awaitable[Any],
    *,
    channel: str,
    chat_id: str,
    operation: str,
    task_summary: str | None = None,
    pre_wait_seconds: float = 0,
    wait_reminder_interval_seconds: float = 5,
    wait_reminder_max_seconds: float = 0,
    wait_reminder_ai_model: str | None = None,
    main_model: str = "",
) -> Any:
    """
    Run a blocking coroutine with AI-decided wait reminders.
    Raises WaitReminderTimeout when wait_reminder_max_seconds > 0 and elapsed >= it.
    Caller should not invoke this when the feature is disabled.
    """
    from nanobot.bus.events import OutboundMessage

    if pre_wait_seconds > 0:
        await asyncio.sleep(pre_wait_seconds)
    task = asyncio.create_task(coro)
    elapsed = 0.0
    interval = max(0.1, wait_reminder_interval_seconds)
    model = wait_reminder_ai_model or main_model
    try:
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=interval)
            except asyncio.TimeoutError:
                elapsed += interval
                if wait_reminder_max_seconds > 0 and elapsed >= wait_reminder_max_seconds:
                    next_i, out_msg = await run_wait_decision(
                        provider, model, elapsed, operation,
                        task_summary=task_summary, is_timeout=True,
                    )
                    if out_msg:
                        await bus.publish_outbound(
                            OutboundMessage(channel=channel, chat_id=chat_id, content=out_msg)
                        )
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    raise WaitReminderTimeout(
                        f"Operation timed out after {int(elapsed)} seconds",
                        elapsed_seconds=elapsed,
                    )
                next_interval, out_msg = await run_wait_decision(
                    provider, model, elapsed, operation,
                    task_summary=task_summary, is_timeout=False,
                )
                if out_msg:
                    await bus.publish_outbound(
                        OutboundMessage(channel=channel, chat_id=chat_id, content=out_msg)
                    )
                interval = max(0.1, next_interval)
            except asyncio.CancelledError:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                raise
    except WaitReminderTimeout:
        raise
    except asyncio.CancelledError:
        raise

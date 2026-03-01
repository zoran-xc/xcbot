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
            "你是一个等待状态决策助手。当前操作已经达到最大等待时间。"
            "你必须只回复一行，并且严格使用以下格式之一："
            "WAIT  或  WAIT N（N=下次检查等待秒数） 或  SEND: <message>。"
            "优先使用 SEND，用于告知用户已超时，并建议暂停/稍后重试/确认是否继续。"
            "当使用 SEND 时，<message> 必须使用中文输出（除非用户任务明确要求其他语言）。"
        )
        user_content = (
            f"已等待 {int(elapsed_seconds)} 秒；当前操作：{operation}。"
            + (f"用户任务摘要（SEND 消息语言应匹配）：{task_summary}。" if task_summary else "")
            + "已达到最大等待时间。请只回复：WAIT 或 WAIT N 或 SEND: <给用户的简短中文提示>。"
        )
    else:
        sys_content = (
            "你是一个等待状态决策助手。请根据已等待时长与当前操作决定：继续等待，还是向用户发送一条提示。"
            "你必须只回复一行，并且严格使用以下格式之一："
            "WAIT  或  WAIT N（例如 WAIT 10，表示 10 秒后再检查） 或  SEND: <message>。"
            "当你需要告诉用户仍在处理中/可能较慢/正在重试等信息时，使用 SEND。"
            "当使用 SEND 时，<message> 必须使用中文输出（除非用户任务明确要求其他语言）。"
        )
        user_content = (
            f"已等待 {int(elapsed_seconds)} 秒。当前操作：{operation}。"
            + (f"用户任务摘要（SEND 消息语言应匹配）：{task_summary}。" if task_summary else "")
            + "请只回复：WAIT 或 WAIT N 或 SEND: <给用户的简短中文提示>。"
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

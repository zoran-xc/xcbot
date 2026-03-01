"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.plan_header import PLAN_RULES, parse_plan_header
from nanobot.agent.task_anchor import TaskAnchorEntry, TaskAnchorStore
from nanobot.agent.tools.factory import build_tool_registry
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 500
    _DEFAULT_CONTEXT_COMPACTION_TRIGGER_TOKENS = 38_000
    _DEFAULT_CONTEXT_COMPACTION_MAX_ROUNDS = 3

    @dataclass
    class _AttemptOutcome:
        final_content: str | None
        tools_used: list[str]
        messages: list[dict]
        error_message: str | None = None

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        vision_model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        context_compaction_trigger_tokens: int | None = None,
        context_compaction_max_rounds: int | None = None,
        brave_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.vision_model = vision_model
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.context_compaction_trigger_tokens = (
            int(context_compaction_trigger_tokens)
            if context_compaction_trigger_tokens is not None
            else self._DEFAULT_CONTEXT_COMPACTION_TRIGGER_TOKENS
        )
        self.context_compaction_max_rounds = (
            int(context_compaction_max_rounds)
            if context_compaction_max_rounds is not None
            else self._DEFAULT_CONTEXT_COMPACTION_MAX_ROUNDS
        )
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: dict[str, asyncio.Lock] = {}
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._processing_lock = asyncio.Lock()
        self._task_anchors = TaskAnchorStore(self.workspace)
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        self.tools = build_tool_registry(
            mode="main",
            workspace=self.workspace,
            restrict_to_workspace=self.restrict_to_workspace,
            exec_config=self.exec_config,
            brave_api_key=self.brave_api_key,
            send_callback=self.bus.publish_outbound,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
        )

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack, workspace=self.workspace)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "tasks", "cron"):
            if tool := self.tools.get(name):
                if hasattr(tool, "set_context"):
                    tool.set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            val = next(iter(tc.arguments.values()), None) if tc.arguments else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    @staticmethod
    def _has_image_input(messages: list[dict]) -> bool:
        """Return True if any message content contains an OpenAI-style image_url block."""
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "image_url":
                        return True
        return False

    @staticmethod
    def _estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
        total_chars = 0
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        t = item.get("text")
                        if isinstance(t, str):
                            total_chars += len(t)
                    elif item.get("type") == "image_url":
                        total_chars += 200
            else:
                total_chars += len(str(content))
            total_chars += 40
        return max(1, total_chars // 4)

    async def _compact_context_if_needed(
        self,
        *,
        session: Session,
        session_key: str,
        channel: str,
        chat_id: str,
        current_message: str,
        media: list[str] | None,
        extra_system_prompt: str | None,
    ) -> list[dict[str, Any]]:
        """Ensure the built prompt stays under the soft token limit by consolidating history."""

        for _round in range(max(0, self.context_compaction_max_rounds)):
            history = session.get_history(max_messages=self.memory_window)
            msgs = self.context.build_messages(
                history=history,
                current_message=current_message,
                media=media,
                channel=channel,
                chat_id=chat_id,
                extra_system_prompt=extra_system_prompt,
            )
            est = self._estimate_prompt_tokens(msgs)
            if est <= self.context_compaction_trigger_tokens:
                return history

            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            async with lock:
                before = session.last_consolidated
                ok = await self._consolidate_memory(session)
                after = session.last_consolidated
            if not ok:
                return history
            if after == before:
                return history

        return session.get_history(max_messages=self.memory_window)

    @staticmethod
    def _has_tool_errors(messages: list[dict]) -> bool:
        for m in messages:
            if m.get("role") != "tool":
                continue
            c = m.get("content")
            if isinstance(c, str) and c.strip().startswith("Error"):
                return True
        return False

    @staticmethod
    def _looks_like_give_up(text: str | None) -> bool:
        if not text:
            return False
        t = text.lower()
        # Keep it conservative: only phrases that clearly indicate inability.
        patterns = (
            "无法",
            "不能",
            "不可用",
            "失败",
            "超时",
            "需要配置",
            "需要 api key",
            "please set",
            "not configured",
            "i can't",
            "i cannot",
            "unable to",
            "sorry",
            "apolog",
        )
        return any(p in t for p in patterns)

    @staticmethod
    def _is_image_only_prompt(text: str) -> bool:
        """Return True if the user message contains no meaningful text besides image placeholders."""
        t = (text or "").strip()
        if not t:
            return True

        # Common placeholders in channel session logs.
        t = t.replace("[image]", "").strip()
        t = re.sub(r"\[image:\s*[^\]]+\]", "", t).strip()
        t = re.sub(r"\s+", " ", t).strip()
        return not t

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent loop for a single attempt.

        The retry mechanism has been removed. Tool failures are surfaced directly
        in the tool results and/or final output.
        """

        outcome = await self._run_single_attempt(initial_messages, on_progress=on_progress)
        return outcome.final_content, outcome.tools_used, outcome.messages

    async def _run_single_attempt(
        self,
        initial_messages: list[dict],
        *,
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> _AttemptOutcome:
        """Run a single attempt of the tool-iteration loop."""
        messages = initial_messages
        iteration = 0
        final_content: str | None = None
        tools_used: list[str] = []
        consecutive_tool_errors = 0
        max_consecutive_tool_errors = 3

        while iteration < self.max_iterations:
            iteration += 1

            model = self.model
            if self._has_image_input(messages):
                vision_model = getattr(self, "vision_model", None)
                if not vision_model:
                    return self._AttemptOutcome(
                        final_content=(
                            "Error: this request includes an image, but no vision model is configured. "
                            "Please set agents.defaults.visionModel in config.json."
                        ),
                        tools_used=tools_used,
                        messages=messages,
                        error_message="vision model not configured",
                    )
                model = vision_model
                try:
                    block_types: list[str] = []
                    for m in messages:
                        if m.get("role") != "user":
                            continue
                        c = m.get("content")
                        if isinstance(c, list):
                            block_types.extend(
                                [str(item.get("type")) for item in c if isinstance(item, dict) and item.get("type")]
                            )
                    if block_types:
                        logger.info(
                            "Vision input detected; using vision model: {} (user block types: {})",
                            model,
                            ",".join(block_types),
                        )
                    else:
                        logger.info("Vision input detected; using vision model: {}", model)
                except Exception:
                    logger.info("Vision input detected; using vision model: {}", model)
            else:
                logger.debug("No vision input; using model: {}", model)

            try:
                response = await self.provider.chat(
                    messages=messages,
                    tools=self.tools.get_definitions(),
                    model=model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
            except Exception as e:
                logger.error("LLM call raised exception (model={}): {}", model, e)
                return self._AttemptOutcome(
                    final_content=f"Error calling LLM: {str(e)}",
                    tools_used=tools_used,
                    messages=messages,
                    error_message=f"LLM exception (model={model}): {str(e)}",
                )

            if (
                getattr(response, "finish_reason", None) == "error"
                or (isinstance(response.content, str) and response.content.startswith("Error calling LLM:"))
            ):
                logger.error("LLM call failed (model={}): {}", model, response.content)
                return self._AttemptOutcome(
                    final_content=response.content or "Error calling LLM",
                    tools_used=tools_used,
                    messages=messages,
                    error_message=f"LLM error (model={model}): {response.content}",
                )

            if response.has_tool_calls:
                if on_progress:
                    clean = self._strip_think(response.content)
                    if clean:
                        await on_progress(clean)
                    await on_progress(f"【工具调用】{self._tool_hint(response.tool_calls)}", tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    # ToolRegistry.execute already catches exceptions and returns an error string.
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(messages, tool_call.id, tool_call.name, result)

                    # Emit tool result to channel when on_progress supports reply_kind (e.g. for execution visibility)
                    if on_progress:
                        result_str = result if isinstance(result, str) else str(result)
                        display = (
                            result_str
                            if len(result_str) <= self._TOOL_RESULT_MAX_CHARS
                            else result_str[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                        )
                        try:
                            await on_progress(
                                f"【工具结果】{tool_call.name}: {display}",
                                reply_kind="tool_result",
                                tool_name=tool_call.name,
                                tool_result=result_str,
                            )
                        except TypeError:
                            # Progress callback may not accept kwargs (e.g. CLI); call with content only
                            await on_progress(f"【工具结果】{tool_call.name}: {display}")

                    if isinstance(result, str) and result.startswith("Error"):
                        consecutive_tool_errors += 1
                    else:
                        consecutive_tool_errors = 0

                    if consecutive_tool_errors >= max_consecutive_tool_errors:
                        return self._AttemptOutcome(
                            final_content=(
                                "Error: Tool calls failed repeatedly. "
                                "Stopping to avoid a loop. Please check tool configuration/logs and try again."
                            ),
                            tools_used=tools_used,
                            messages=messages,
                            error_message="too many consecutive tool errors",
                        )

            else:
                clean = self._strip_think(response.content)
                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                )
                final_content = clean
                # If tools errored and the assistant is effectively giving up, treat as failure.
                if self._has_tool_errors(messages) and self._looks_like_give_up(final_content):
                    return self._AttemptOutcome(
                        final_content=final_content,
                        tools_used=tools_used,
                        messages=messages,
                        error_message="non-exception failure: assistant reported inability after tool errors",
                    )
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )
            return self._AttemptOutcome(
                final_content=final_content,
                tools_used=tools_used,
                messages=messages,
                error_message="max tool iterations reached",
            )

        return self._AttemptOutcome(
            final_content=final_content,
            tools_used=tools_used,
            messages=messages,
        )

    def _try_append_task_anchor(self, session_key: str, final_content: str | None) -> None:
        if not final_content:
            return
        plan = parse_plan_header(final_content)
        if not plan:
            return
        from datetime import datetime
        entry = TaskAnchorEntry(
            session_key=session_key,
            timestamp=datetime.now().isoformat(),
            goal=plan.goal,
            steps=plan.steps,
            next_step=plan.next_step,
            raw=final_content[:800],
        )
        try:
            self._task_anchors.append(entry)
        except Exception as e:
            logger.debug("Failed to append task anchor for {}: {}", session_key, e)

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(lambda t, k=msg.session_key: self._active_tasks.get(k, []) and self._active_tasks[k].remove(t) if t in self._active_tasks.get(k, []) else None)

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=content,
        ))

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        async with self._processing_lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="", metadata=msg.metadata or {},
                    ))
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Sorry, I encountered an error.",
                ))

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = await self._compact_context_if_needed(
                session=session,
                session_key=key,
                channel=channel,
                chat_id=chat_id,
                current_message=msg.content,
                media=None,
                extra_system_prompt=None,
            )
            async def _build(extra_system_prompt: str | None) -> list[dict]:
                return self.context.build_messages(
                    history=history,
                    current_message=msg.content,
                    channel=channel,
                    chat_id=chat_id,
                    extra_system_prompt=extra_system_prompt,
                )

            msgs = await _build(None)
            outcome = await self._run_single_attempt(msgs, on_progress=on_progress)
            final_content = outcome.final_content
            all_msgs = outcome.messages

            self._try_append_task_anchor(key, final_content)
            history_for_skip = session.get_history(max_messages=self.memory_window)
            self._save_turn(session, all_msgs, 1 + len(history_for_skip))
            self.sessions.save(session)
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=final_content or "Background task completed.",
            )

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._consolidating.add(session.key)
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        if not await self._consolidate_memory(temp, archive_all=True):
                            return OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id,
                                content="Memory archival failed, session not cleared. Please try again.",
                            )
            except Exception:
                logger.exception("/new archival failed for {}", session.key)
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="Memory archival failed, session not cleared. Please try again.",
                )
            finally:
                self._consolidating.discard(session.key)
                if not lock.locked():
                    self._consolidation_locks.pop(session.key, None)

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 nanobot commands:\n/new — Start a new conversation\n/stop — Stop the current task\n/help — Show available commands")

        unconsolidated = len(session.messages) - session.last_consolidated
        if (unconsolidated >= self.memory_window and session.key not in self._consolidating):
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.key)
                    if not lock.locked():
                        self._consolidation_locks.pop(session.key, None)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            from nanobot.agent.tools.message import MessageTool
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        effective_message = msg.content
        if msg.media and self._is_image_only_prompt(msg.content):
            effective_message = "解释图片中的内容"

        async def _build(extra_system_prompt: str | None) -> list[dict]:
            history = await self._compact_context_if_needed(
                session=session,
                session_key=key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                current_message=effective_message,
                media=msg.media if msg.media else None,
                extra_system_prompt=extra_system_prompt,
            )
            return self.context.build_messages(
                history=history,
                current_message=effective_message,
                media=msg.media if msg.media else None,
                channel=msg.channel,
                chat_id=msg.chat_id,
                extra_system_prompt=extra_system_prompt,
            )

        async def _bus_progress(content: str, *, tool_hint: bool = False, **kwargs: Any) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            for k, v in kwargs.items():
                if v is not None:
                    if k == "tool_result" and isinstance(v, str) and len(v) > self._TOOL_RESULT_MAX_CHARS:
                        v = v[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
                    meta["_" + k] = v
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        msgs = await _build(None)
        outcome = await self._run_single_attempt(
            msgs,
            on_progress=on_progress or _bus_progress,
        )
        final_content = outcome.final_content
        tools_used = outcome.tools_used
        all_msgs = outcome.messages

        self._try_append_task_anchor(key, final_content)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        history_for_skip = session.get_history(max_messages=self.memory_window)
        self._save_turn(session, all_msgs, 1 + len(history_for_skip))
        self.sessions.save(session)

        if (mt := self.tools.get("message")):
            from nanobot.agent.tools.message import MessageTool
            if isinstance(mt, MessageTool) and mt._sent_in_turn:
                return None

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=msg.metadata or {},
        )

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = {k: v for k, v in m.items() if k != "reasoning_content"}
            role, content = entry.get("role"), entry.get("content")
            if role == "tool" and isinstance(content, str) and len(content) > self._TOOL_RESULT_MAX_CHARS:
                entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str) and content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    continue
                if isinstance(content, list):
                    entry["content"] = [
                        {"type": "text", "text": "[image]"} if (
                            c.get("type") == "image_url"
                            and c.get("image_url", {}).get("url", "").startswith("data:image/")
                        ) else c for c in content
                    ]
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """Delegate to MemoryStore.consolidate(). Returns True on success."""
        return await MemoryStore(self.workspace).consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""

"""Subagent manager for background task execution."""

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.tools.factory import build_tool_registry
from nanobot.agent.wait_reminder import run_with_ai_wait_reminder, WaitReminderTimeout


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        brave_api_key: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        *,
        wait_reminder_interval_seconds: float = 5.0,
        subagent_wait_reminder_max_seconds: float = 120.0,
        wait_reminder_ai_model: str | None = None,
        enable_wait_reminder: bool = True,
        pre_wait_seconds: float = 5.0,
    ):
        from nanobot.config.schema import ExecToolConfig
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self.wait_reminder_interval_seconds = wait_reminder_interval_seconds
        self.subagent_wait_reminder_max_seconds = subagent_wait_reminder_max_seconds
        self.wait_reminder_ai_model = wait_reminder_ai_model
        self.enable_wait_reminder = enable_wait_reminder
        self.pre_wait_seconds = pre_wait_seconds
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
    
    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id}

        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_label, origin)
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)):
                ids.discard(task_id)
                if not ids:
                    del self._session_tasks[session_key]

        bg_task.add_done_callback(_cleanup)
        
        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."
    
    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)
        
        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = build_tool_registry(
                mode="subagent",
                workspace=self.workspace,
                restrict_to_workspace=self.restrict_to_workspace,
                exec_config=self.exec_config,
                brave_api_key=self.brave_api_key,
            )
            
            # Build messages with subagent-specific prompt
            system_prompt = self._build_subagent_prompt(task)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]
            
            # Run agent loop (limited iterations)
            max_iterations = 15
            iteration = 0
            final_result: str | None = None
            
            while iteration < max_iterations:
                iteration += 1

                if self.enable_wait_reminder and origin.get("channel") and origin.get("chat_id"):
                    try:
                        response = await run_with_ai_wait_reminder(
                            self.provider,
                            self.bus,
                            self.provider.chat(
                                messages=messages,
                                tools=tools.get_definitions(),
                                model=self.model,
                                temperature=self.temperature,
                                max_tokens=self.max_tokens,
                            ),
                            channel=origin["channel"],
                            chat_id=origin["chat_id"],
                            operation="LLM",
                            task_summary=task[:200] + "…" if len(task) > 200 else task,
                            pre_wait_seconds=self.pre_wait_seconds,
                            wait_reminder_interval_seconds=self.wait_reminder_interval_seconds,
                            wait_reminder_max_seconds=self.subagent_wait_reminder_max_seconds,
                            wait_reminder_ai_model=self.wait_reminder_ai_model,
                            main_model=self.model,
                        )
                    except WaitReminderTimeout as e:
                        error_msg = f"Subagent timed out after {int(e.elapsed_seconds)} seconds"
                        logger.warning("Subagent [{}] {}", task_id, error_msg)
                        await self._announce_result(task_id, label, task, error_msg, origin, "error")
                        return
                else:
                    response = await self.provider.chat(
                        messages=messages,
                        tools=tools.get_definitions(),
                        model=self.model,
                        temperature=self.temperature,
                        max_tokens=self.max_tokens,
                    )

                if response.has_tool_calls:
                    # Add assistant message with tool calls
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
                    messages.append({
                        "role": "assistant",
                        "content": response.content or "",
                        "tool_calls": tool_call_dicts,
                    })
                    
                    # Execute tools
                    _tool_result_max_chars = 500
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        logger.debug("Subagent [{}] executing: {} with arguments: {}", task_id, tool_call.name, args_str)
                        if self.enable_wait_reminder and origin.get("channel") and origin.get("chat_id"):
                            try:
                                result = await run_with_ai_wait_reminder(
                                    self.provider,
                                    self.bus,
                                    tools.execute(tool_call.name, tool_call.arguments),
                                    channel=origin["channel"],
                                    chat_id=origin["chat_id"],
                                    operation=f"tool: {tool_call.name}",
                                    task_summary=tool_call.name,
                                    pre_wait_seconds=self.pre_wait_seconds,
                                    wait_reminder_interval_seconds=self.wait_reminder_interval_seconds,
                                    wait_reminder_max_seconds=self.subagent_wait_reminder_max_seconds,
                                    wait_reminder_ai_model=self.wait_reminder_ai_model,
                                    main_model=self.model,
                                )
                            except WaitReminderTimeout as e:
                                result = f"Error: operation timed out after {int(e.elapsed_seconds)} seconds"
                                logger.warning("Subagent [{}] tool {} timeout", task_id, tool_call.name)
                        else:
                            result = await tools.execute(tool_call.name, tool_call.arguments)
                        result_str = result if isinstance(result, str) else str(result)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result_str,
                        })
                        # Emit tool result to bus for execution visibility (e.g. Feishu when send_tool_results is on)
                        display = (
                            result_str
                            if len(result_str) <= _tool_result_max_chars
                            else result_str[:_tool_result_max_chars] + "\n... (truncated)"
                        )
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=origin["channel"],
                                chat_id=origin["chat_id"],
                                content=f"【子任务 {label}】{tool_call.name}: {display}",
                                metadata={
                                    "_reply_kind": "tool_result",
                                    "_tool_name": tool_call.name,
                                    "_tool_result": display,
                                    "_origin": "subagent",
                                    "_task_id": task_id,
                                    "_progress": True,
                                },
                            )
                        )
                else:
                    final_result = response.content
                    break
            
            if final_result is None:
                final_result = "Task completed but no final response was generated."
            
            logger.info("Subagent [{}] completed successfully", task_id)
            await self._announce_result(task_id, label, task, final_result, origin, "ok")
            
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", task_id, e)
            await self._announce_result(task_id, label, task, error_msg, origin, "error")
    
    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"
        
        announce_content = f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""
        
        # Inject as system message to trigger main agent
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
        )
        
        await self.bus.publish_inbound(msg)
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])
    
    def _build_subagent_prompt(self, task: str) -> str:
        """Build a focused system prompt for the subagent."""
        from datetime import datetime
        import time as _time
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"

        return f"""# Subagent

## Current Time
{now} ({tz})

You are a subagent spawned by the main agent to complete a specific task.

## Rules
1. Stay focused - complete only the assigned task, nothing else
2. Your final response will be reported back to the main agent
3. Do not initiate conversations or take on side tasks
4. Be concise but informative in your findings

## What You Can Do
- Read and write files in the workspace
- Execute shell commands
- Search the web and fetch web pages
- Complete the task thoroughly

## What You Cannot Do
- Send messages directly to users (no message tool available)
- Spawn other subagents
- Access the main agent's conversation history

## Workspace
Your workspace is at: {self.workspace}
Skills are available at: {self.workspace}/skills/ (read SKILL.md files as needed)

When you have completed the task, provide a clear summary of your findings or actions."""
    
    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

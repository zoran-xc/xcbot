"""Subagent manager for background task execution."""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.subagent_task_store import SubagentTaskStore
from nanobot.agent.tools.factory import build_tool_registry


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
        trace_enabled: bool = True,
        trace_dir: str = "subagents",
        trace_max_chars: int = 8000,
        memory_window: int = 100,
        context_compaction_trigger_tokens: int = 38_000,
        context_compaction_max_rounds: int = 3,
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
        self.trace_enabled = trace_enabled
        self.trace_dir = trace_dir
        self.trace_max_chars = int(trace_max_chars)
        self.memory_window = int(memory_window)
        self.context_compaction_trigger_tokens = int(context_compaction_trigger_tokens)
        self.context_compaction_max_rounds = int(context_compaction_max_rounds)
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        self._cancel_reasons: dict[str, str] = {}
        self._store = SubagentTaskStore(workspace)

    def _trace_path(self, task_id: str) -> Path:
        return self.workspace / self.trace_dir / f"{task_id}.jsonl"

    def _checkpoint_dir(self, task_id: str) -> Path:
        return self.workspace / "state" / "subagents" / task_id

    def _checkpoint_path(self, task_id: str) -> Path:
        return self._checkpoint_dir(task_id) / "checkpoint.json"

    def _write_checkpoint(self, task_id: str, payload: dict[str, Any]) -> str:
        path = self._checkpoint_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def _read_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        path = self._checkpoint_path(task_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _truncate(self, text: str | None) -> str:
        if not text:
            return ""
        if self.trace_max_chars <= 0:
            return text
        if len(text) <= self.trace_max_chars:
            return text
        return text[: self.trace_max_chars] + "\n... (truncated)"

    def _trace(self, task_id: str, event: str, payload: dict[str, Any]) -> None:
        if not self.trace_enabled:
            return
        try:
            path = self._trace_path(task_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            line = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "task_id": task_id,
                "event": event,
                **payload,
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug("Failed to write subagent trace for {}: {}", task_id, e)
    
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

        self._store.create(
            task_id=task_id,
            session_key=session_key,
            label=display_label,
            task=task,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
        )

        self._trace(
            task_id,
            "spawn",
            {
                "label": display_label,
                "task": self._truncate(task),
                "origin": origin,
                "session_key": session_key,
            },
        )

        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_label, origin, initial_messages=None)
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
        return f"Subagent [{display_label}] started (task_id={task_id}). I'll notify you when it completes."
    
    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        initial_messages: list[dict[str, Any]] | None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        self._trace(
            task_id,
            "start",
            {
                "label": label,
                "task": self._truncate(task),
                "origin": origin,
                "model": self.model,
            },
        )
        
        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = build_tool_registry(
                mode="subagent",
                workspace=self.workspace,
                restrict_to_workspace=self.restrict_to_workspace,
                exec_config=self.exec_config,
                brave_api_key=self.brave_api_key,
            )
            
            if initial_messages is not None:
                messages = initial_messages
            else:
                system_prompt = self._build_subagent_prompt(task)
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ]
            
            # Run agent loop (limited iterations)
            max_iterations = 15
            iteration = 0
            final_result: str | None = None

            cp_path = self._write_checkpoint(
                task_id,
                {
                    "task_id": task_id,
                    "label": label,
                    "task": task,
                    "iteration": iteration,
                    "messages": messages,
                },
            )
            self._store.update(task_id, checkpoint_path=cp_path)
            
            while iteration < max_iterations:
                iteration += 1

                messages = await self._compact_messages_if_needed(messages)

                cp_path = self._write_checkpoint(
                    task_id,
                    {
                        "task_id": task_id,
                        "label": label,
                        "task": task,
                        "iteration": iteration,
                        "messages": messages,
                    },
                )
                self._store.update(task_id, checkpoint_path=cp_path)

                self._trace(
                    task_id,
                    "llm_request",
                    {
                        "iteration": iteration,
                        "messages_count": len(messages),
                    },
                )

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

                    self._trace(
                        task_id,
                        "llm_tool_calls",
                        {
                            "iteration": iteration,
                            "content": self._truncate(response.content or ""),
                            "tool_calls": tool_call_dicts,
                        },
                    )
                    
                    # Execute tools
                    _tool_result_max_chars = 500
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        logger.debug("Subagent [{}] executing: {} with arguments: {}", task_id, tool_call.name, args_str)

                        self._trace(
                            task_id,
                            "tool_start",
                            {
                                "iteration": iteration,
                                "tool_name": tool_call.name,
                                "arguments": self._truncate(args_str),
                            },
                        )
                        result = await tools.execute(tool_call.name, tool_call.arguments)
                        result_str = result if isinstance(result, str) else str(result)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": result_str,
                        })

                        self._trace(
                            task_id,
                            "tool_end",
                            {
                                "iteration": iteration,
                                "tool_name": tool_call.name,
                                "result": self._truncate(result_str),
                            },
                        )
                else:
                    final_result = response.content
                    self._trace(
                        task_id,
                        "llm_final",
                        {
                            "iteration": iteration,
                            "content": self._truncate(final_result or ""),
                        },
                    )
                    break
            
            if final_result is None:
                final_result = "Task completed but no final response was generated."
            
            logger.info("Subagent [{}] completed successfully", task_id)
            self._trace(
                task_id,
                "end",
                {
                    "status": "ok",
                    "result": self._truncate(final_result),
                },
            )
            cp_path = self._write_checkpoint(
                task_id,
                {
                    "task_id": task_id,
                    "label": label,
                    "task": task,
                    "iteration": iteration,
                    "messages": messages,
                    "final": final_result,
                },
            )
            self._store.update(task_id, status="SUCCEEDED", checkpoint_path=cp_path, last_summary=self._truncate(final_result))
            await self._announce_result(task_id, label, task, final_result, origin, "ok")
            
        except asyncio.CancelledError:
            reason = self._cancel_reasons.get(task_id) or "cancel"
            status = "PAUSED" if reason == "pause" else "CANCELED"
            try:
                cp_path = self._write_checkpoint(
                    task_id,
                    {
                        "task_id": task_id,
                        "label": label,
                        "task": task,
                        "iteration": iteration,
                        "messages": messages,
                    },
                )
                self._store.update(task_id, status=status, checkpoint_path=cp_path)
            except Exception:
                self._store.update(task_id, status=status)
            raise
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent [{}] failed: {}", task_id, e)
            self._trace(
                task_id,
                "end",
                {
                    "status": "error",
                    "error": self._truncate(error_msg),
                },
            )
            cp_path = self._write_checkpoint(
                task_id,
                {
                    "task_id": task_id,
                    "label": label,
                    "task": task,
                    "iteration": iteration,
                    "messages": messages,
                    "error": error_msg,
                },
            )
            self._store.update(task_id, status="FAILED", checkpoint_path=cp_path, error=error_msg)
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
            for tid, rt in list(self._running_tasks.items()):
                if rt is t:
                    self._cancel_reasons[tid] = "cancel"
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    async def cancel(self, task_id: str) -> bool:
        t = self._running_tasks.get(task_id)
        if not t or t.done():
            self._store.update(task_id, status="CANCELED")
            return False
        self._cancel_reasons[task_id] = "cancel"
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        self._store.update(task_id, status="CANCELED")
        return True

    async def pause(self, task_id: str) -> bool:
        t = self._running_tasks.get(task_id)
        if not t or t.done():
            self._store.update(task_id, status="PAUSED")
            return False
        self._cancel_reasons[task_id] = "pause"
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        self._store.update(task_id, status="PAUSED")
        return True

    async def resume(
        self,
        task_id: str,
        *,
        instruction: str | None = None,
    ) -> str:
        rec = self._store.get(task_id)
        if not rec:
            return f"Error: task not found (task_id={task_id})"

        cp = self._read_checkpoint(task_id)
        messages = None
        if cp and isinstance(cp.get("messages"), list):
            messages = cp.get("messages")

        if messages is None:
            return f"Error: checkpoint not found for task_id={task_id}"

        if instruction:
            messages = list(messages) + [{"role": "user", "content": instruction}]

        label = rec.label
        task = rec.task
        origin = {"channel": rec.origin_channel or "cli", "chat_id": rec.origin_chat_id or "direct"}
        self._store.update(task_id, status="RUNNING")
        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, label, origin, initial_messages=messages)
        )
        self._running_tasks[task_id] = bg_task
        return f"Resumed subagent [{label}] (task_id={task_id})."

    async def _compact_messages_if_needed(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.context_compaction_trigger_tokens <= 0:
            return messages

        def _estimate(ms: list[dict[str, Any]]) -> int:
            total_chars = 0
            for m in ms:
                total_chars += len(str(m.get("content") or "")) + 40
            return max(1, total_chars // 4)

        for _round in range(max(0, self.context_compaction_max_rounds)):
            if _estimate(messages) <= self.context_compaction_trigger_tokens:
                return messages

            keep = max(10, int(self.memory_window) // 2)
            head = messages[:-keep]
            tail = messages[-keep:]
            if len(head) < 2:
                return messages

            prompt = "\n".join(
                f"[{m.get('role', '')}] {str(m.get('content', ''))}" for m in head if m.get("content")
            )

            resp = await self.provider.chat(
                messages=[
                    {"role": "system", "content": "You summarize conversation context for continuation. Be concise."},
                    {"role": "user", "content": prompt},
                ],
                tools=[],
                model=self.model,
                temperature=0.2,
                max_tokens=512,
            )
            summary = (resp.content or "").strip() or "(summary unavailable)"
            messages = [{"role": "system", "content": "[Subagent context summary]\n" + summary}] + tail

        return messages

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

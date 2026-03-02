"""Tasks tool: minimal batch subagent spawning."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from xcbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from xcbot.agent.subagent import SubagentManager


class TasksTool(Tool):
    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel = "cli"
        self._origin_chat_id = "direct"
        self._session_key = "cli:direct"

    def set_context(self, channel: str, chat_id: str) -> None:
        self._origin_channel = channel
        self._origin_chat_id = chat_id
        self._session_key = f"{channel}:{chat_id}"

    @property
    def name(self) -> str:
        return "tasks"

    @property
    def description(self) -> str:
        return (
            "Create and run multiple background tasks (subagents) in parallel. "
            "Use this when you can split work into independent subtasks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["spawn_many"],
                    "description": "Only supported action for now",
                },
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "label": {"type": "string"},
                        },
                        "required": ["task"],
                    },
                    "description": "A list of tasks to run",
                },
            },
            "required": ["action", "tasks"],
        }

    async def execute(self, action: str, tasks: list[dict[str, Any]], **kwargs: Any) -> str:
        if action != "spawn_many":
            return "Error: Unsupported action."
        if not tasks:
            return "Error: tasks is empty."

        results: list[str] = []
        for t in tasks[:8]:
            task_text = (t.get("task") or "").strip()
            if not task_text:
                continue
            label = (t.get("label") or None)
            res = await self._manager.spawn(
                task=task_text,
                label=label,
                origin_channel=self._origin_channel,
                origin_chat_id=self._origin_chat_id,
                session_key=self._session_key,
            )
            results.append(res)

        if not results:
            return "Error: No valid task entries."

        return "\n\n".join(results)

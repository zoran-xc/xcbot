"""Tool for managing subagent tasks (list/get/pause/resume/cancel/tail)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from xcbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from xcbot.agent.subagent import SubagentManager


class SubagentTasksTool(Tool):
    def __init__(self, manager: "SubagentManager", workspace: Path):
        self._manager = manager
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "subagent_tasks"

    @property
    def description(self) -> str:
        return (
            "Manage subagent tasks spawned by the main agent. "
            "Actions: list/get/tail/search/pause/resume/cancel. "
            "Use this to query progress and control lifecycle; do not call from subagents."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "tail", "search", "pause", "resume", "cancel"],
                },
                "task_id": {"type": "string"},
                "session_key": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_lines": {"type": "integer", "minimum": 10, "maximum": 2000},
                "instruction": {"type": "string"},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        task_id: str | None = None,
        session_key: str | None = None,
        query: str | None = None,
        limit: int = 20,
        max_lines: int = 200,
        instruction: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "list":
            items = self._manager._store.list(session_key=session_key, limit=limit)
            if not items:
                return "(no subagent tasks)"
            lines = []
            for r in items:
                lines.append(
                    f"{r.task_id} | {r.status} | {r.label} | updated_at={r.updated_at}"
                )
            return "\n".join(lines)

        if action == "get":
            if not task_id:
                return "Error: task_id is required"
            r = self._manager._store.get(task_id)
            if not r:
                return f"(task not found: {task_id})"
            return json.dumps(r.to_dict(), ensure_ascii=False, indent=2)

        if action == "tail":
            if not task_id:
                return "Error: task_id is required"
            trace_dir = self._workspace / (self._manager.trace_dir or "subagents")
            path = trace_dir / f"{task_id}.jsonl"
            if not path.exists():
                return f"(trace not found: {task_id})"
            lines = path.read_text(encoding="utf-8").splitlines()
            sliced = lines[-max(10, int(max_lines)) :]
            return "\n".join(sliced)

        if action == "search":
            q = (query or "").strip().lower()
            if not q and not session_key:
                return "Error: query or session_key is required"

            items = self._manager._store.list(session_key=session_key, limit=max(1, int(limit)))
            hits: list[str] = []
            for r in items:
                if q:
                    hay = "\n".join(
                        [
                            r.task_id,
                            r.label or "",
                            r.task or "",
                            r.status or "",
                            r.last_summary or "",
                            r.error or "",
                        ]
                    ).lower()
                    if q not in hay:
                        continue
                hits.append(f"{r.task_id} | {r.status} | {r.label}")

            if hits:
                return "\n".join(hits)

            if not q:
                return "(no matches)"

            trace_dir = self._workspace / (self._manager.trace_dir or "subagents")
            if not trace_dir.exists():
                return "(no matches)"

            out: list[str] = []
            for p in sorted(trace_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    content = p.read_text(encoding="utf-8")
                except Exception:
                    continue
                if session_key and session_key not in content:
                    continue
                if q in content.lower():
                    out.append(p.stem)
                if len(out) >= max(1, int(limit)):
                    break

            return "\n".join(out) or "(no matches)"

        if action == "pause":
            if not task_id:
                return "Error: task_id is required"
            ok = await self._manager.pause(task_id)
            return "ok" if ok else "ok (not running)"

        if action == "cancel":
            if not task_id:
                return "Error: task_id is required"
            ok = await self._manager.cancel(task_id)
            return "ok" if ok else "ok (not running)"

        if action == "resume":
            if not task_id:
                return "Error: task_id is required"
            return await self._manager.resume(task_id, instruction=instruction)

        return "Error: unsupported action"

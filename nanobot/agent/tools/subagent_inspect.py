"""Tool for inspecting subagent trace logs stored in workspace/subagents/*.jsonl."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xcbot.agent.tools.base import Tool


def _trace_dir(workspace: Path, trace_dir: str) -> Path:
    return workspace / (trace_dir or "subagents")


class SubagentInspectTool(Tool):
    def __init__(self, workspace: Path, trace_dir: str = "subagents"):
        self._workspace = workspace
        self._trace_dir_name = trace_dir

    @property
    def name(self) -> str:
        return "subagent_inspect"

    @property
    def description(self) -> str:
        return (
            "Inspect subagent trace JSONL files under workspace/subagents (or configured trace dir). "
            "Use action=list to list recent task_ids; action=read to read a single task trace; "
            "action=search to find tasks by session_key or keyword in trace content."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "read", "search"]},
                "task_id": {"type": "string", "description": "8-char subagent task id"},
                "session_key": {"type": "string", "description": "Origin session_key (e.g. feishu:xxxx)"},
                "query": {"type": "string", "description": "Keyword to search within trace"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_lines": {"type": "integer", "minimum": 10, "maximum": 2000},
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
        max_lines: int = 300,
        **kwargs: Any,
    ) -> str:
        trace_dir = _trace_dir(self._workspace, self._trace_dir_name)
        if action == "list":
            return self._list(trace_dir, limit=limit)
        if action == "read":
            if not task_id:
                return "Error: task_id is required for action=read"
            return self._read(trace_dir, task_id=task_id, max_lines=max_lines)
        if action == "search":
            q = (query or "").strip()
            if not session_key and not q:
                return "Error: session_key or query is required for action=search"
            return self._search(trace_dir, session_key=session_key, query=q, limit=limit)
        return "Error: action must be list, read, or search"

    def _list(self, trace_dir: Path, *, limit: int) -> str:
        if not trace_dir.exists():
            return "(no subagent trace dir)"

        items: list[tuple[float, str]] = []
        for p in trace_dir.glob("*.jsonl"):
            try:
                items.append((p.stat().st_mtime, p.stem))
            except Exception:
                continue

        items.sort(key=lambda x: x[0], reverse=True)
        out = [task for _, task in items[: max(1, int(limit))]]
        return "\n".join(out) or "(no traces)"

    def _read(self, trace_dir: Path, *, task_id: str, max_lines: int) -> str:
        path = trace_dir / f"{task_id}.jsonl"
        if not path.exists():
            return f"(trace not found: {task_id})"

        lines = path.read_text(encoding="utf-8").splitlines()
        sliced = lines[-max(10, int(max_lines)) :]

        # Summarize key stats
        tool_names: list[str] = []
        status = ""
        label = ""
        origin = ""
        for raw in sliced:
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            ev = obj.get("event")
            if ev == "spawn":
                label = obj.get("label") or label
                sess = obj.get("session_key") or ""
                origin = f"{obj.get('origin') or {}}" + (f" | session_key={sess}" if sess else "")
            if ev == "tool_start":
                tn = obj.get("tool_name")
                if isinstance(tn, str):
                    tool_names.append(tn)
            if ev == "end":
                status = obj.get("status") or status

        header = [
            f"task_id={task_id}",
            f"label={label}" if label else "",
            f"status={status}" if status else "",
            f"origin={origin}" if origin else "",
            f"tools={', '.join(tool_names[:30])}" if tool_names else "",
        ]
        header = [h for h in header if h]

        body = "\n".join(sliced)
        return "\n".join(header) + "\n\n" + body

    def _search(
        self,
        trace_dir: Path,
        *,
        session_key: str | None,
        query: str,
        limit: int,
    ) -> str:
        if not trace_dir.exists():
            return "(no subagent trace dir)"

        q = query.lower() if query else ""
        sess = (session_key or "").strip()

        hits: list[str] = []
        for p in sorted(trace_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                content = p.read_text(encoding="utf-8")
            except Exception:
                continue

            if sess and sess not in content:
                continue
            if q and q not in content.lower():
                continue

            hits.append(p.stem)
            if len(hits) >= max(1, int(limit)):
                break

        return "\n".join(hits) or "(no matches)"

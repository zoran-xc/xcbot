from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.utils.helpers import safe_filename


def _sessions_dir(workspace: Path) -> Path:
    return workspace / "sessions"


class SessionTool(Tool):
    def __init__(self, workspace: Path):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "session"

    @property
    def description(self) -> str:
        return "Query conversation sessions stored under workspace/sessions. Use action=list, recent, or search."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "recent", "search"]},
                "session_key": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        session_key: str | None = None,
        query: str | None = None,
        limit: int = 20,
        **kwargs: Any,
    ) -> str:
        if action == "list":
            return await SessionListTool(self._workspace).execute(limit=min(limit, 50))
        if action == "recent":
            if not session_key:
                return "Error: session_key is required for action=recent"
            return await SessionRecentTool(self._workspace).execute(session_key=session_key, limit=limit)
        if action == "search":
            if not session_key or not (query or "").strip():
                return "Error: session_key and query are required for action=search"
            return await SessionSearchTool(self._workspace).execute(session_key=session_key, query=query or "", limit=limit)
        return "Error: action must be list, recent, or search"


class SessionListTool(Tool):
    def __init__(self, workspace: Path):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "session_list"

    @property
    def description(self) -> str:
        return "List recent conversation sessions in the workspace (by updated time)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        }

    async def execute(self, limit: int = 10, **kwargs: Any) -> str:
        dir_path = _sessions_dir(self._workspace)
        if not dir_path.exists():
            return "(no sessions dir)"

        items: list[tuple[str, str]] = []
        for p in dir_path.glob("*.jsonl"):
            try:
                with open(p, encoding="utf-8") as f:
                    meta = f.readline().strip()
                if not meta:
                    continue
                obj = json.loads(meta)
                if obj.get("_type") != "metadata":
                    continue
                key = obj.get("key") or p.stem
                updated = obj.get("updated_at") or ""
                items.append((updated, key))
            except Exception:
                continue

        items.sort(key=lambda x: x[0], reverse=True)
        out = []
        for updated, key in items[: max(0, int(limit))]:
            out.append(f"{updated} | {key}")
        return "\n".join(out) or "(no sessions)"


class SessionRecentTool(Tool):
    def __init__(self, workspace: Path):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "session_recent"

    @property
    def description(self) -> str:
        return "Show the most recent messages of a session (reads workspace sessions/*.jsonl)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_key": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["session_key"],
        }

    async def execute(self, session_key: str, limit: int = 20, **kwargs: Any) -> str:
        dir_path = _sessions_dir(self._workspace)
        safe = safe_filename(session_key.replace(":", "_"))
        path = dir_path / f"{safe}.jsonl"
        if not path.exists():
            return f"(session not found: {session_key})"

        msgs: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("_type") == "metadata":
                    continue
                msgs.append(obj)

        sliced = msgs[-max(1, int(limit)) :]
        out: list[str] = []
        for m in sliced:
            role = m.get("role", "")
            ts = m.get("timestamp", "")
            content = (m.get("content") or "").replace("\n", " ")
            if len(content) > 200:
                content = content[:200] + "…"
            out.append(f"{ts} | {role} | {content}")
        return "\n".join(out) or "(no messages)"


class SessionSearchTool(Tool):
    def __init__(self, workspace: Path):
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "session_search"

    @property
    def description(self) -> str:
        return "Search messages in a session by keyword."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_key": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["session_key", "query"],
        }

    async def execute(self, session_key: str, query: str, limit: int = 20, **kwargs: Any) -> str:
        q = (query or "").strip().lower()
        if not q:
            return "(empty query)"

        dir_path = _sessions_dir(self._workspace)
        safe = safe_filename(session_key.replace(":", "_"))
        path = dir_path / f"{safe}.jsonl"
        if not path.exists():
            return f"(session not found: {session_key})"

        hits: list[str] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("_type") == "metadata":
                    continue
                content = str(obj.get("content") or "")
                if q in content.lower():
                    role = obj.get("role", "")
                    ts = obj.get("timestamp", "")
                    preview = content.replace("\n", " ")
                    if len(preview) > 200:
                        preview = preview[:200] + "…"
                    hits.append(f"{ts} | {role} | {preview}")
                    if len(hits) >= max(1, int(limit)):
                        break

        return "\n".join(hits) or "(no matches)"

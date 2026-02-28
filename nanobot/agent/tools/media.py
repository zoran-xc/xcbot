from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.utils.media_cache import MediaCache


class MediaTool(Tool):
    def __init__(self, workspace: Path):
        self._cache = MediaCache(workspace)

    @property
    def name(self) -> str:
        return "media"

    @property
    def description(self) -> str:
        return "Query cached media in the workspace. Use action=recent or action=search."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["recent", "search"]},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        query: str | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> str:
        if action == "recent":
            items = self._cache.recent(limit=limit)
        elif action == "search":
            items = self._cache.search(query=query or "", limit=limit)
        else:
            return "Error: action must be recent or search"

        if not items:
            return "(no results)"

        lines: list[str] = []
        for it in items:
            lines.append(
                f"{it.get('created_at','')} | {it.get('path','')} | {it.get('mime','')} | {it.get('source','')}"
            )
        return "\n".join(lines)


class MediaRecentTool(Tool):
    def __init__(self, workspace: Path):
        self._cache = MediaCache(workspace)

    @property
    def name(self) -> str:
        return "media_recent"

    @property
    def description(self) -> str:
        return "List recently cached media files in the workspace (from tools/channels)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        }

    async def execute(self, limit: int = 10, **kwargs: Any) -> str:
        items = self._cache.recent(limit=limit)
        if not items:
            return "(no cached media)"
        lines = []
        for it in items:
            lines.append(f"{it.get('created_at','')} | {it.get('path','')} | {it.get('mime','')} | {it.get('source','')}")
        return "\n".join(lines)


class MediaSearchTool(Tool):
    def __init__(self, workspace: Path):
        self._cache = MediaCache(workspace)

    @property
    def name(self) -> str:
        return "media_search"

    @property
    def description(self) -> str:
        return "Search cached media index in the workspace by keyword (path/source/mime)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        }

    async def execute(self, query: str, limit: int = 10, **kwargs: Any) -> str:
        items = self._cache.search(query=query, limit=limit)
        if not items:
            return "(no matches)"
        lines = []
        for it in items:
            lines.append(f"{it.get('created_at','')} | {it.get('path','')} | {it.get('mime','')} | {it.get('source','')}")
        return "\n".join(lines)

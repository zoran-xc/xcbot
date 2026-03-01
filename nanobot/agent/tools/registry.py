"""Tool registry for dynamic tool management."""

import json
import time
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.
    
    Allows dynamic registration and execution of tools.
    """
    
    def __init__(self):
        self._tools: dict[str, Tool] = {}
    
    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
    
    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
    
    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)
    
    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools
    
    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]
    
    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute a tool by name with given parameters."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"

        try:
            start = time.perf_counter()

            try:
                params_preview = json.dumps(params, ensure_ascii=False, default=str)
            except Exception:
                params_preview = str(params)
            if len(params_preview) > 800:
                params_preview = params_preview[:800] + "..."
            logger.info("Tool exec start: name={} params={}", name, params_preview)

            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + _HINT

            took_ms = int((time.perf_counter() - start) * 1000)
            preview = result
            if isinstance(preview, str) and len(preview) > 800:
                preview = preview[:800] + "..."
            logger.info("Tool exec done: name={} tookMs={} result={}", name, took_ms, preview)
            return result
        except Exception as e:
            took_ms = None
            try:
                took_ms = int((time.perf_counter() - start) * 1000)
            except Exception:
                pass
            logger.exception("Tool exec error: name={} tookMs={} error={}", name, took_ms, e)
            return f"Error executing {name}: {str(e)}" + _HINT
    
    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())
    
    def __len__(self) -> int:
        return len(self._tools)
    
    def __contains__(self, name: str) -> bool:
        return name in self._tools

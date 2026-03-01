"""Tool registry factory.

Centralizes tool registration for main agent and subagents.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.web import WebFetchTool

if TYPE_CHECKING:
    from nanobot.agent.subagent import SubagentManager
    from nanobot.bus.events import OutboundMessage
    from nanobot.config.schema import ExecToolConfig
    from nanobot.cron.service import CronService


def build_tool_registry(
    *,
    mode: str,
    workspace: Path,
    restrict_to_workspace: bool,
    exec_config: "ExecToolConfig",
    brave_api_key: str | None,
    send_callback: Callable[["OutboundMessage"], Awaitable[None]] | None = None,
    subagent_manager: "SubagentManager" | None = None,
    cron_service: "CronService" | None = None,
    channels_config: Any = None,
) -> ToolRegistry:
    tools = ToolRegistry()

    allowed_dir = workspace if restrict_to_workspace else None
    for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
        tools.register(cls(workspace=workspace, allowed_dir=allowed_dir))

    tools.register(
        ExecTool(
            working_dir=str(workspace),
            timeout=exec_config.timeout,
            restrict_to_workspace=restrict_to_workspace,
            path_append=exec_config.path_append,
        )
    )

    tools.register(WebFetchTool())

    if mode == "main":
        if send_callback is None:
            raise ValueError("send_callback is required when mode='main'")
        if subagent_manager is None:
            raise ValueError("subagent_manager is required when mode='main'")
        from nanobot.agent.tools.message import MessageTool
        from nanobot.agent.tools.spawn import SpawnTool

        tools.register(MessageTool(send_callback=send_callback))
        tools.register(SpawnTool(manager=subagent_manager))

        from nanobot.agent.tools.tasks import TasksTool

        tools.register(TasksTool(manager=subagent_manager))

        from nanobot.agent.tools.media import MediaTool

        tools.register(MediaTool(workspace=workspace))

        from nanobot.agent.tools.session_tools import SessionTool

        tools.register(SessionTool(workspace=workspace))

        from nanobot.agent.tools.subagent_inspect import SubagentInspectTool

        tools.register(SubagentInspectTool(workspace=workspace))

        if cron_service:
            from nanobot.agent.tools.cron import CronTool

            tools.register(CronTool(cron_service))

        # Feishu-only: fetch chat history (only when Feishu channel is enabled)
        if channels_config:
            feishu = getattr(channels_config, "feishu", None)
            if feishu and getattr(feishu, "enabled", False):
                from nanobot.agent.tools.feishu_chat_history import FeishuChatHistoryTool
                tools.register(FeishuChatHistoryTool(config=feishu))

    return tools

"""MCP client: connects to MCP servers and wraps their tools as native nanobot tools."""

import asyncio
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.utils.helpers import timestamp
from nanobot.utils.media_cache import MediaCache


class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as a nanobot Tool."""

    _IMAGE_RE = re.compile(
        r"type=['\"]image['\"]\s+data=['\"](?P<data>[A-Za-z0-9+/=\r\n]+)['\"]",
        re.IGNORECASE,
    )

    _MD_DATA_URI_IMAGE_RE = re.compile(
        r"!\[(?P<alt>[^\]]*)\]\(data:(?P<mime>image/[A-Za-z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\r\n]+)\)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        session,
        server_name: str,
        tool_def,
        workspace: Path,
        tool_timeout: int = 30,
    ):
        self._session = session
        self._server_name = server_name
        self._original_name = tool_def.name
        self._name = f"mcp_{server_name}_{tool_def.name}"
        self._description = tool_def.description or tool_def.name
        self._parameters = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._tool_timeout = tool_timeout
        self._workspace = workspace
        self._media = MediaCache(workspace)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        return super().validate_params(params)

    async def execute(self, **kwargs: Any) -> str:
        from mcp import types
        try:
            logger.info(
                "MCP tool call request: tool='{}' original='{}' args={}",
                self._name,
                self._original_name,
                kwargs,
            )
            result = await asyncio.wait_for(
                self._session.call_tool(self._original_name, arguments=kwargs),
                timeout=self._tool_timeout,
            )
            try:
                logger.info(
                    "MCP tool call response: tool='{}' original='{}' result={}",
                    self._name,
                    self._original_name,
                    result,
                )
            except Exception:
                logger.info(
                    "MCP tool call response: tool='{}' original='{}' (failed to stringify result)",
                    self._name,
                    self._original_name,
                )
        except asyncio.TimeoutError:
            logger.warning("MCP tool '{}': timed out after {}s", self._name, self._tool_timeout)
            return f"(MCP tool call timed out after {self._tool_timeout}s)"
        except Exception as e:
            logger.exception(
                "MCP tool call error: tool='{}' original='{}' args={} error={}",
                self._name,
                self._original_name,
                kwargs,
                e,
            )
            raise
        parts: list[str] = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(self._decode_inline_images(block.text))
            elif getattr(types, "ImageContent", None) is not None and isinstance(block, types.ImageContent):
                parts.append(self._save_image_block(block))
            else:
                # Best-effort: some servers may return image-like blocks without using ImageContent.
                saved = self._save_unknown_image_like_block(block)
                parts.append(saved if saved is not None else str(block))
        return "\n".join(parts) or "(no output)"

    def _save_image_block(self, block: Any) -> str:
        """Save a structured MCP ImageContent block to a file and return a placeholder."""
        mime = getattr(block, "mime_type", None) or getattr(block, "mimeType", None) or "image/png"
        data = getattr(block, "data", None)

        ext = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/webp": "webp",
            "image/gif": "gif",
        }.get(str(mime).lower(), "png")

        if isinstance(data, (bytes, bytearray)):
            try:
                path = self._media.save_bytes(bytes(data), ext=ext, prefix=self._name, mime=str(mime), source="mcp")
                return f"[image saved: {path}]"
            except Exception as e:
                logger.warning("MCP tool '{}': failed to write ImageContent file: {}", self._name, e)
                return "[image: write failed]"

        if isinstance(data, str):
            b64 = data.strip().replace("\n", "").replace("\r", "")
            try:
                path = self._media.save_base64(b64, ext=ext, prefix=self._name, mime=str(mime), source="mcp")
                return f"[image saved: {path}]"
            except Exception as e:
                logger.warning("MCP tool '{}': failed to decode/write ImageContent base64: {}", self._name, e)
                return "[image: decode failed]"

        return "[image: decode failed]"

    def _save_unknown_image_like_block(self, block: Any) -> str | None:
        """Try to save image data from non-standard blocks.

        This is defensive: if an MCP implementation returns dict-like blocks containing
        {mime_type/mimeType, data}, we still want to save them rather than stringify.
        """
        try:
            mime = None
            data = None
            if isinstance(block, dict):
                mime = block.get("mime_type") or block.get("mimeType") or block.get("mime")
                data = block.get("data")
            else:
                mime = getattr(block, "mime_type", None) or getattr(block, "mimeType", None)
                data = getattr(block, "data", None)

            if not mime or data is None:
                return None

            class _Tmp:
                pass

            tmp = _Tmp()
            setattr(tmp, "mime_type", mime)
            setattr(tmp, "data", data)
            return self._save_image_block(tmp)
        except Exception:
            return None

    def _decode_inline_images(self, text: str) -> str:
        """Decode inline MCP image payloads in text and save to workspace.

        Some MCP servers return images embedded in text blocks, e.g.
        type='image' data='<base64...>'
        """

        def _write_b64(b64: str, ext: str) -> str:
            b64 = (b64 or "").strip().replace("\n", "").replace("\r", "")
            if not b64:
                return ""
            try:
                path = self._media.save_base64(b64, ext=ext, prefix=self._name, mime=f"image/{ext}", source="mcp")
                return f"[image saved: {path}]"
            except Exception as e:
                logger.warning("MCP tool '{}': failed to decode/write inline image base64: {}", self._name, e)
                return ""

        def _write_type_image(match: re.Match) -> str:
            repl = _write_b64(match.group("data") or "", "png")
            return repl or match.group(0)

        def _write_md_data_uri(match: re.Match) -> str:
            mime = (match.group("mime") or "").lower()
            ext = {
                "image/png": "png",
                "image/jpeg": "jpg",
                "image/jpg": "jpg",
                "image/webp": "webp",
                "image/gif": "gif",
            }.get(mime, "png")
            repl = _write_b64(match.group("data") or "", ext)
            return repl or match.group(0)

        text = self._IMAGE_RE.sub(_write_type_image, text)
        text = self._MD_DATA_URI_IMAGE_RE.sub(_write_md_data_uri, text)
        return text


async def connect_mcp_servers(
    mcp_servers: dict,
    registry: ToolRegistry,
    stack: AsyncExitStack,
    workspace: Path,
) -> None:
    """Connect to configured MCP servers and register their tools."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    for name, cfg in mcp_servers.items():
        try:
            if cfg.command:
                params = StdioServerParameters(
                    command=cfg.command, args=cfg.args, env=cfg.env or None
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif cfg.url:
                from mcp.client.streamable_http import streamable_http_client
                # Always provide an explicit httpx client so MCP HTTP transport does not
                # inherit httpx's default 5s timeout and preempt the higher-level tool timeout.
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': no command or url configured, skipping", name)
                continue

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            tools = await session.list_tools()
            for tool_def in tools.tools:
                wrapper = MCPToolWrapper(
                    session,
                    name,
                    tool_def,
                    workspace=workspace,
                    tool_timeout=cfg.tool_timeout,
                )
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)

            logger.info("MCP server '{}': connected, {} tools registered", name, len(tools.tools))
        except asyncio.CancelledError as e:
            try:
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
            except Exception:
                pass
            logger.warning("MCP server '{}': connection cancelled, skipping", name)
            continue
        except Exception as e:
            logger.error("MCP server '{}': failed to connect: {}", name, e)
            # Continue to next server instead of crashing
            continue

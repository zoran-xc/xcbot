"""Feishu-only tool: fetch chat history from current Feishu chat.

Only registered when Feishu channel is enabled. Use limit for last N messages,
or start_time/end_time for a time range. page_token supports pagination for full history.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.config.schema import FeishuConfig


def _parse_time(value: str | int | float | None) -> int | None:
    """Convert time to Unix seconds. Accepts: number (seconds), or ISO 8601 string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            # Numeric string (Unix seconds)
            return int(float(value))
        except (ValueError, TypeError):
            pass
        try:
            # ISO 8601 or date-like
            for fmt in (
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ):
                try:
                    dt = datetime.strptime(value.replace("Z", "+00:00"), fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return int(dt.timestamp())
                except ValueError:
                    continue
        except Exception:
            pass
    return None


class FeishuChatHistoryTool(Tool):
    """Fetch chat history for the current Feishu chat. Only available when the user is in a Feishu session."""

    def __init__(self, config: "FeishuConfig"):
        self._config = config
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "feishu_chat_history"

    @property
    def description(self) -> str:
        return (
            "Fetch chat history from the current Feishu group or DM. "
            "Use 'limit' to get the last N messages (default 20, max 50). "
            "Use 'start_time' and/or 'end_time' for a time range (Unix seconds or ISO 8601, e.g. 2025-01-01 or 1735689600). "
            "If the result says has_more, call again with the returned 'page_token' to get older messages. "
            "Only available in Feishu channel."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of messages to fetch (default 20, max 50). Used when no time range is given, or to cap results in a range.",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                },
                "start_time": {
                    "type": "string",
                    "description": "Start of time range: Unix seconds (number as string) or ISO 8601 (e.g. 2025-01-01T00:00:00). Optional.",
                },
                "end_time": {
                    "type": "string",
                    "description": "End of time range: Unix seconds or ISO 8601. Optional.",
                },
                "page_token": {
                    "type": "string",
                    "description": "Token from previous result to fetch the next page (older messages). Optional.",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        limit: int = 20,
        start_time: str | int | float | None = None,
        end_time: str | int | float | None = None,
        page_token: str | None = None,
        **kwargs: Any,
    ) -> str:
        if self._channel != "feishu":
            return (
                f"This tool is only available in Feishu channel. Current channel: {self._channel or 'unknown'}."
            )
        if not self._chat_id:
            return "Error: No Feishu chat context. This tool works in an active Feishu conversation."
        if not self._config.app_id or not self._config.app_secret:
            return "Error: Feishu app credentials not configured."

        limit = max(1, min(50, limit))
        start_ts = _parse_time(start_time)
        end_ts = _parse_time(end_time)

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                token_resp = await client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self._config.app_id,
                        "app_secret": self._config.app_secret,
                    },
                )
                token_resp.raise_for_status()
                token_data = token_resp.json()
                if token_data.get("code", 0) != 0:
                    return f"Error: Feishu auth failed: {token_data}"
                token = token_data.get("tenant_access_token", "")
                if not token:
                    return "Error: No tenant_access_token from Feishu."

                params: dict[str, Any] = {
                    "container_id_type": "chat",
                    "container_id": self._chat_id,
                    "page_size": limit,
                }
                if start_ts is not None:
                    params["start_time"] = str(start_ts)
                if end_ts is not None:
                    params["end_time"] = str(end_ts)
                if page_token:
                    params["page_token"] = page_token

                list_resp = await client.get(
                    "https://open.feishu.cn/open-apis/im/v1/messages",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                list_resp.raise_for_status()
                list_data = list_resp.json()
                if list_data.get("code", 0) != 0:
                    msg = list_data.get("msg", str(list_data))
                    return f"Error: Feishu list messages failed: {msg}. (Ensure the app has 'get group messages' permission and the bot is in the chat.)"
                data = list_data.get("data") or {}
                items = data.get("items") or []
                has_more = data.get("has_more", False)
                next_token = data.get("page_token", "")

        except httpx.HTTPError as e:
            logger.debug("Feishu chat history request failed: {}", e)
            return f"Error: Request failed: {e}"
        except Exception as e:
            logger.exception("Feishu chat history failed")
            return f"Error: {e}"

        if not items:
            hint = ""
            if next_token and has_more:
                hint = " Try with page_token to fetch older messages."
            return f"No messages in the given range or chat is empty.{hint}"

        lines = ["## Feishu 聊天记录"]
        for it in items:
            create_time = it.get("create_time", "")
            sender_obj = it.get("sender") or {}
            sender_id = sender_obj.get("id", "") if isinstance(sender_obj, dict) else ""
            sender_type = sender_obj.get("sender_type", "") if isinstance(sender_obj, dict) else ""
            body = it.get("body") or {}
            content = ""
            if isinstance(body, dict):
                raw = body.get("content")
                if isinstance(raw, str):
                    try:
                        content_json = json.loads(raw)
                        content = (
                            content_json.get("text", str(content_json))
                            if isinstance(content_json, dict)
                            else str(content_json)
                        )
                    except Exception:
                        content = raw[:400]
                elif raw is not None:
                    content = str(raw)[:400]
            if not content:
                content = "(无文本)"
            lines.append(f"- [{create_time}] {sender_id}({sender_type}): {content}")

        out = "\n".join(lines)
        if has_more and next_token:
            out += (
                f"\n\n(还有更多历史消息。要获取更早的消息，请再次调用本工具并传入 page_token=\"{next_token}\")"
            )
        return out

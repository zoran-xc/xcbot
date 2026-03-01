"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import FeishuConfig
from nanobot.utils.media_cache import MediaCache

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        Emoji,
        GetFileRequest,
        GetMessageResourceRequest,
        P2ImMessageReceiveV1,
    )
    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _event_to_loggable(obj: Any) -> Any:
    """Convert Feishu event (SDK object) to JSON-serializable dict for logging."""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return _event_to_loggable(obj.model_dump())
    if hasattr(obj, "to_dict"):
        return _event_to_loggable(obj.to_dict())
    if isinstance(obj, dict):
        return {k: _event_to_loggable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_event_to_loggable(x) for x in obj]
    if hasattr(obj, "__dict__") and not isinstance(obj, type):
        return {k: _event_to_loggable(v) for k, v in vars(obj).items()}
    if isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def _get_mention_key_to_name(message: Any) -> dict[str, str]:
    """Build map from mention key (e.g. @_user_1) to display name from message.mentions.

    Feishu event structure (event.message.mentions[]):
        {"key": "@_user_1", "id": {"open_id": "ou_xxx", ...}, "name": "Tom", "tenant_key": "..."}
    """
    mentions = getattr(message, "mentions", None)
    if not mentions:
        return {}
    out: dict[str, str] = {}
    for m in mentions:
        key = getattr(m, "key", None) or (m.get("key") if isinstance(m, dict) else None)
        if not isinstance(key, str) or not key.strip():
            continue
        key = key.strip()
        name = getattr(m, "name", None) or (m.get("name") if isinstance(m, dict) else None)
        if isinstance(name, str) and name.strip():
            out[key] = name.strip()
        elif isinstance(name, dict):
            n = name.get("name") or name.get("text") or name.get("content")
            if isinstance(n, str) and n.strip():
                out[key] = n.strip()
    return out


def _replace_mention_placeholders_with_names(text: str, message: Any) -> str:
    """Replace @_user_N placeholders in text with real user names from message.mentions."""
    if not text or not text.strip():
        return text
    key_to_name = _get_mention_key_to_name(message)
    if not key_to_name:
        return text
    out = text
    for key, name in key_to_name.items():
        if not key or not name:
            continue
        display = f"@{name}" if not name.startswith("@") else name
        out = out.replace(key, display)
    return out


def _extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """Extract text representation from share cards and interactive messages."""
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """Recursively extract text and links from interactive card content."""
    parts = []
    
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    for element in content.get("elements", []) if isinstance(content.get("elements"), list) else []:
        parts.extend(_extract_element_content(element))

    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")
    
    return parts


def _extract_element_content(element: dict) -> list[str]:
    """Extract content from a single card element."""
    parts = []
    
    if not isinstance(element, dict):
        return parts
    
    tag = element.get("tag", "")
    
    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    else:
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))
    
    return parts


def _get_mention_union_ids(message: Any) -> list[str]:
    """Extract union_ids of mentioned users/bots from Feishu message (event.message.mentions[].id.union_id)."""
    mentions = getattr(message, "mentions", None)
    if not mentions:
        return []
    ids: list[str] = []
    for m in mentions:
        id_val = getattr(m, "id", None)
        if id_val is None and isinstance(m, dict):
            id_val = m.get("id")
        if isinstance(id_val, str):
            ids.append(id_val)
        elif id_val is not None:
            uid = getattr(id_val, "union_id", None)
            if isinstance(uid, str):
                ids.append(uid)
            elif isinstance(id_val, dict):
                ids.append(id_val.get("union_id") or "")
    return [x for x in ids if x]


def _get_mention_open_ids(message: Any) -> list[str]:
    """Extract open_ids of mentioned users/bots (for backward compat)."""
    mentions = getattr(message, "mentions", None)
    if not mentions:
        return []
    ids: list[str] = []
    for m in mentions:
        id_val = getattr(m, "id", None)
        if id_val is None and isinstance(m, dict):
            id_val = m.get("id")
        if isinstance(id_val, str):
            ids.append(id_val)
        elif id_val is not None:
            oid = getattr(id_val, "open_id", None)
            if isinstance(oid, str):
                ids.append(oid)
            elif isinstance(id_val, dict):
                ids.append(id_val.get("open_id") or "")
    return [x for x in ids if x]


def _get_mention_keys_for_union_id(message: Any, union_id: str) -> list[str]:
    """Get mention keys (e.g. @_user_1) for the given union_id."""
    mentions = getattr(message, "mentions", None)
    if not mentions:
        return []
    keys: list[str] = []
    for m in mentions:
        id_val = getattr(m, "id", None)
        if id_val is None and isinstance(m, dict):
            id_val = m.get("id")
        uid = id_val if isinstance(id_val, str) else (getattr(id_val, "union_id", None) if id_val else None)
        if id_val is not None and not isinstance(id_val, str) and isinstance(id_val, dict):
            uid = id_val.get("union_id")
        if uid == union_id:
            k = getattr(m, "key", None) or (m.get("key") if isinstance(m, dict) else None)
            if isinstance(k, str) and k:
                keys.append(k)
    return keys


def _strip_mention_keys_from_text(text: str, keys: list[str]) -> str:
    """Remove mention placeholders (e.g. @_user_1) from message text."""
    if not text or not keys:
        return text
    out = text
    for k in keys:
        out = out.replace(k, "").replace(k.strip(), "")
    return re.sub(r"\s+", " ", out).strip()


# Feishu text content uses @_user_1, @_user_2, ... as placeholders for @mentions.
# These are NOT open_ids — the real open_id is in event.message.mentions[].id.
_USER_MENTION_RE = re.compile(r"@_user_\d+\s*")


def _strip_feishu_mention_placeholders(text: str) -> str:
    """Strip all @_user_N placeholders from Feishu message text so the model sees clean content."""
    if not text or not text.strip():
        return text
    out = _USER_MENTION_RE.sub(" ", text)
    return re.sub(r"\s+", " ", out).strip()


def _extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """Extract text and image keys from Feishu post (rich text) message content.
    
    Supports two formats:
    1. Direct format: {"title": "...", "content": [...]}
    2. Localized format: {"zh_cn": {"title": "...", "content": [...]}}
    
    Returns:
        (text, image_keys) - extracted text and list of image keys
    """
    def extract_from_lang(lang_content: dict) -> tuple[str | None, list[str]]:
        if not isinstance(lang_content, dict):
            return None, []
        title = lang_content.get("title", "")
        content_blocks = lang_content.get("content", [])
        if not isinstance(content_blocks, list):
            return None, []
        text_parts = []
        image_keys = []
        if title:
            text_parts.append(title)
        for block in content_blocks:
            if not isinstance(block, list):
                continue
            for element in block:
                if isinstance(element, dict):
                    tag = element.get("tag")
                    if tag == "text":
                        text_parts.append(element.get("text", ""))
                    elif tag == "a":
                        text_parts.append(element.get("text", ""))
                    elif tag == "at":
                        text_parts.append(f"@{element.get('user_name', 'user')}")
                    elif tag == "img":
                        img_key = element.get("image_key")
                        if img_key:
                            image_keys.append(img_key)
        text = " ".join(text_parts).strip() if text_parts else None
        return text, image_keys
    
    # Try direct format first
    if "content" in content_json:
        text, images = extract_from_lang(content_json)
        if text or images:
            return text or "", images
    
    # Try localized format
    for lang_key in ("zh_cn", "en_us", "ja_jp"):
        lang_content = content_json.get(lang_key)
        text, images = extract_from_lang(lang_content)
        if text or images:
            return text or "", images
    
    return "", []


def _extract_post_text(content_json: dict) -> str:
    """Extract plain text from Feishu post (rich text) message content.
    
    Legacy wrapper for _extract_post_content, returns only text.
    """
    text, _ = _extract_post_content(content_json)
    return text


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.
    
    Uses WebSocket to receive events - no public IP or webhook required.
    
    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """
    
    name = "feishu"
    
    def __init__(self, config: FeishuConfig, bus: MessageBus, workspace: Path):
        super().__init__(config, bus, workspace)
        self.config: FeishuConfig = config
        self._client: Any = None
        self._card_disabled: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_client: Any = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache

    async def _fetch_group_history(self, chat_id: str, page_size: int) -> str:
        """Fetch recent messages from Feishu group chat via REST API.
        Requires app to have 'get group messages' permission (获取群组中所有消息).
        Returns a formatted string for use as extra context, or empty string on failure.
        """
        if page_size <= 0 or not self.config.app_id or not self.config.app_secret:
            return ""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Get tenant_access_token
                token_resp = await client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
                )
                token_resp.raise_for_status()
                token_data = token_resp.json()
                if token_data.get("code", 0) != 0:
                    logger.warning("Feishu tenant_access_token failed: {}", token_data)
                    return ""
                token = token_data.get("tenant_access_token", "")
                if not token:
                    return ""

                # List messages (container_id_type=chat, container_id=chat_id)
                list_resp = await client.get(
                    "https://open.feishu.cn/open-apis/im/v1/messages",
                    params={
                        "container_id_type": "chat",
                        "container_id": chat_id,
                        "page_size": min(page_size, 50),
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                list_resp.raise_for_status()
                list_data = list_resp.json()
                if list_data.get("code", 0) != 0:
                    logger.debug("Feishu list messages failed: {}", list_data)
                    return ""
                data = list_data.get("data") or {}
                items = data.get("items") or []
        except Exception as e:
            logger.debug("Feishu fetch group history failed: {}", e)
            return ""

        if not items:
            return ""

        lines = ["## 群聊最近消息（Feishu）"]
        for it in items:
            msg_id = it.get("message_id", "")
            create_time = it.get("create_time", "")
            sender_obj = it.get("sender") or {}
            sender_id = sender_obj.get("id", "") if isinstance(sender_obj, dict) else ""
            body = it.get("body") or {}
            content = ""
            if isinstance(body, dict):
                raw = body.get("content")
                if isinstance(raw, str):
                    try:
                        content_json = json.loads(raw)
                        content = content_json.get("text", str(content_json)) if isinstance(content_json, dict) else str(content_json)
                    except Exception:
                        content = raw[:300]
                elif raw is not None:
                    content = str(raw)[:300]
            if not content:
                content = "(无文本)"
            lines.append(f"- [{create_time}] sender={sender_id}: {content}")
        return "\n".join(lines)
        
    
    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.error("Feishu SDK not installed. Run: pip install lark-oapi")
            return
        
        if not self.config.app_id or not self.config.app_secret:
            logger.error("Feishu app_id and app_secret not configured")
            return
        
        self._running = True
        self._loop = asyncio.get_running_loop()
        
        # Create Lark client for sending messages
        self._client = lark.Client.builder() \
            .app_id(self.config.app_id) \
            .app_secret(self.config.app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        
        # Create event handler (only register message receive, ignore other events)
        event_handler = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(
            self._on_message_sync
        ).build()
        
        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO
        )
        
        # Start WebSocket client in a separate thread with reconnect loop
        def run_ws():
            while self._running:
                try:
                    self._ws_client.start()
                except Exception as e:
                    logger.warning("Feishu WebSocket error: {}", e)
                if self._running:
                    import time; time.sleep(5)
        
        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()
        
        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")
        
        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)
    
    async def stop(self) -> None:
        """Stop the Feishu bot."""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning("Error stopping WebSocket client: {}", e)
        logger.info("Feishu bot stopped")
    
    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """Sync helper for adding reaction (runs in thread pool)."""
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()
            
            response = self._client.im.v1.message_reaction.create(request)
            
            if not response.success():
                logger.warning("Failed to add reaction: code={}, msg={}", response.code, response.msg)
            else:
                logger.debug("Added {} reaction to message {}", emoji_type, message_id)
        except Exception as e:
            logger.warning("Error adding reaction: {}", e)

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        Add a reaction emoji to a message (non-blocking).
        
        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)
    
    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    @staticmethod
    def _parse_md_table(table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [l.strip() for l in table_text.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return None
        split = lambda l: [c.strip() for c in l.strip("|").split("|")]
        headers = split(lines[0])
        rows = [split(l) for l in lines[2:]]
        columns = [{"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
                   for i, h in enumerate(headers)]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Build minimal Feishu card elements."""
        return [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": content,
                },
            }
        ]

    def _split_headings(self, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks)-1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end:m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = m.group(2).strip()
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{text}**",
                },
            })
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
    _AUDIO_EXTS = {".opus"}
    _FILE_TYPE_MAP = {
        ".opus": "opus", ".mp4": "mp4", ".pdf": "pdf", ".doc": "doc", ".docx": "doc",
        ".xls": "xls", ".xlsx": "xls", ".ppt": "ppt", ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> str | None:
        """Upload an image to Feishu and return the image_key."""
        try:
            with open(file_path, "rb") as f:
                request = CreateImageRequest.builder() \
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(f)
                        .build()
                    ).build()
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    logger.debug("Uploaded image {}: {}", os.path.basename(file_path), image_key)
                    return image_key
                else:
                    logger.error("Failed to upload image: code={}, msg={}", response.code, response.msg)
                    return None
        except Exception as e:
            logger.error("Error uploading image {}: {}", file_path, e)
            return None

    def _upload_file_sync(self, file_path: str) -> str | None:
        """Upload a file to Feishu and return the file_key."""
        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = CreateFileRequest.builder() \
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    ).build()
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                else:
                    logger.error("Failed to upload file: code={}, msg={}", response.code, response.msg)
                    return None
        except Exception as e:
            logger.error("Error uploading file {}: {}", file_path, e)
            return None

    def _download_image_sync(self, message_id: str, image_key: str) -> tuple[bytes | None, str | None]:
        """Download an image from Feishu message by message_id and image_key."""
        try:
            request = GetMessageResourceRequest.builder() \
                .message_id(message_id) \
                .file_key(image_key) \
                .type("image") \
                .build()
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                # GetMessageResourceRequest returns BytesIO, need to read bytes
                if hasattr(file_data, 'read'):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error("Failed to download image: code={}, msg={}", response.code, response.msg)
                return None, None
        except Exception as e:
            logger.error("Error downloading image {}: {}", image_key, e)
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """Download a file/audio/media from a Feishu message by message_id and file_key."""
        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                logger.error("Failed to download {}: code={}, msg={}", resource_type, response.code, response.msg)
                return None, None
        except Exception:
            logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    async def _download_and_save_media(
        self,
        msg_type: str,
        content_json: dict,
        message_id: str | None = None
    ) -> tuple[str | None, str]:
        """
        Download media from Feishu and save to local disk.

        Returns:
            (file_path, content_text) - file_path is None if download failed
        """
        loop = asyncio.get_running_loop()
        # KISS: persist inbound media into the nanobot workspace cache so that:
        # - InboundMessage.media can reference local files
        # - the `media` tool can query them via cache/media/index.jsonl
        #
        cache = MediaCache(self.workspace)

        data, filename = None, None

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = f"{image_key[:16]}.jpg"

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if file_key and message_id:
                data, filename = await loop.run_in_executor(
                    None, self._download_file_sync, message_id, file_key, msg_type
                )
                if not filename:
                    ext = {"audio": ".opus", "media": ".mp4"}.get(msg_type, "")
                    filename = f"{file_key[:16]}{ext}"

        if data and filename:
            ext = os.path.splitext(filename)[1] or (".jpg" if msg_type == "image" else "")
            stem = os.path.splitext(os.path.basename(filename))[0]
            safe_stem = re.sub(r"[^a-zA-Z0-9_\-]+", "_", stem).strip("_")
            if safe_stem:
                safe_stem = safe_stem[:24]
            source_key = (
                content_json.get("image_key")
                if msg_type == "image"
                else content_json.get("file_key")
            )
            base_prefix = "feishu_image" if msg_type == "image" else f"feishu_{msg_type}"
            prefix = f"{base_prefix}_{safe_stem}" if safe_stem else base_prefix
            path = cache.save_bytes(
                data,
                ext=ext,
                prefix=prefix,
                mime=None,
                source=f"feishu:{message_id}:{source_key}:{filename}",
            )
            logger.debug("Downloaded {} to {}", msg_type, path)
            return str(path), f"[{msg_type}: {path.name}]"

        return None, f"[{msg_type}: download failed]"

    def _send_message_sync(self, receive_id_type: str, receive_id: str, msg_type: str, content: str) -> bool:
        """Send a single message (text/image/file/interactive) synchronously."""
        try:
            logger.info(
                "Feishu request: receive_id_type={}, receive_id={}, msg_type={}, content={}",
                receive_id_type,
                receive_id,
                msg_type,
                content,
            )
            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                ).build()
            response = self._client.im.v1.message.create(request)
            try:
                raw = getattr(response, "raw", None)
                data = getattr(response, "data", None)
                logger.info(
                    "Feishu response: success={}, code={}, msg={}, log_id={}, raw={}, data={}",
                    response.success() if hasattr(response, "success") else None,
                    getattr(response, "code", None),
                    getattr(response, "msg", None),
                    response.get_log_id() if hasattr(response, "get_log_id") else None,
                    raw,
                    data,
                )
            except Exception as e:
                logger.error("Error logging Feishu response: {}", e)
            if not response.success():
                logger.error(
                    "Failed to send Feishu {} message: code={}, msg={}, log_id={}",
                    msg_type, response.code, response.msg, response.get_log_id()
                )
                return False
            logger.debug("Feishu {} message sent to {}", msg_type, receive_id)
            return True
        except Exception as e:
            logger.error("Error sending Feishu {} message: {}", msg_type, e)
            return False

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Feishu, including media (images/files) if present."""
        if not self._client:
            logger.warning("Feishu client not initialized")
            return

        try:
            # Prefer union_id for user DMs (on_*), chat_id for groups (oc_*)
            if msg.chat_id.startswith("on_"):
                receive_id_type = "union_id"
            elif msg.chat_id.startswith("oc_"):
                receive_id_type = "chat_id"
            else:
                receive_id_type = "open_id"
            loop = asyncio.get_running_loop()

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        await loop.run_in_executor(
                            None, self._send_message_sync,
                            receive_id_type, msg.chat_id, "image", json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        media_type = "audio" if ext in self._AUDIO_EXTS else "file"
                        await loop.run_in_executor(
                            None, self._send_message_sync,
                            receive_id_type, msg.chat_id, media_type, json.dumps({"file_key": key}, ensure_ascii=False),
                        )

            if msg.content and msg.content.strip():
                if getattr(self.config, "use_card", False) and not self._card_disabled:
                    elements = self._build_card_elements(msg.content)
                    card = {
                        "config": {"wide_screen_mode": True},
                        "header": {
                            "title": {
                                "tag": "plain_text",
                                "content": "nanobot",
                            }
                        },
                        "elements": elements,
                    }
                    ok = await loop.run_in_executor(
                        None,
                        self._send_message_sync,
                        receive_id_type,
                        msg.chat_id,
                        "interactive",
                        json.dumps({"card": card}, ensure_ascii=False),
                    )
                    if not ok:
                        self._card_disabled = True
                        await loop.run_in_executor(
                            None,
                            self._send_message_sync,
                            receive_id_type,
                            msg.chat_id,
                            "text",
                            json.dumps({"text": msg.content}, ensure_ascii=False),
                        )
                else:
                    await loop.run_in_executor(
                        None,
                        self._send_message_sync,
                        receive_id_type,
                        msg.chat_id,
                        "text",
                        json.dumps({"text": msg.content}, ensure_ascii=False),
                    )

        except Exception as e:
            logger.error("Error sending Feishu message: {}", e)
    
    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)
    
    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Handle incoming message from Feishu."""
        try:
            # Log full event body to console for debugging (structure of request/event)
            try:
                body = _event_to_loggable(data)
                logger.info(
                    "Feishu event full body:\n{}",
                    json.dumps(body, ensure_ascii=False, indent=2),
                )
            except Exception as e:
                logger.warning("Feishu event body dump failed: {}", e)

            event = data.event
            message = event.message
            sender = event.sender

            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Skip bot messages
            if sender.sender_type == "bot":
                return

            # Prefer union_id for user identity (allow_from and session/routing)
            sid = sender.sender_id
            sender_id = (getattr(sid, "union_id", None) or getattr(sid, "open_id", None) if sid else None) or "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            # When require_mention_in_group: messages that don't @ the bot are saved but don't trigger reply
            save_only_no_reply = False
            if chat_type == "group" and getattr(self.config, "require_mention_in_group", False):
                bot_union_id = (getattr(self.config, "bot_union_id", None) or "").strip()
                bot_open_id = (getattr(self.config, "bot_open_id", None) or "").strip()
                bot_id = bot_union_id or bot_open_id
                if not bot_id:
                    logger.warning(
                        "Feishu require_mention_in_group is True but bot_union_id (or bot_open_id) is not set; "
                        "set bot_union_id in config from event.message.mentions[].id.union_id when the bot is @mentioned"
                    )
                    return
                if bot_union_id:
                    mentioned_ids = _get_mention_union_ids(message)
                    if bot_union_id not in mentioned_ids:
                        save_only_no_reply = True
                else:
                    mentioned_ids = _get_mention_open_ids(message)
                    if bot_open_id not in mentioned_ids:
                        save_only_no_reply = True

            # Add reaction only when we will actually reply (skip for save-only to avoid noise)
            if not save_only_no_reply:
                await self._add_reaction(message_id, self.config.react_emoji)

            # Parse content
            content_parts = []
            media_paths = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download images embedded in post
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(msg_type, content_json, message_id)
                if file_path:
                    media_paths.append(file_path)
                content_parts.append(content_text)

            elif msg_type in ("share_chat", "share_user", "interactive", "share_calendar_event", "system", "merge_forward"):
                # Handle share cards and interactive messages
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            content = "\n".join(content_parts) if content_parts else ""

            # Replace @_user_N placeholders with real user names from message.mentions
            if content:
                content = _replace_mention_placeholders_with_names(content, message)

            if not content and not media_paths:
                if msg_type in ("text", "post", "interactive"):
                    logger.debug(
                        "Feishu inbound message parsed empty (type={}), forwarding placeholder. message_id={} chat_type={}",
                        msg_type,
                        message_id,
                        chat_type,
                    )
                    content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")
                elif save_only_no_reply:
                    content = "[无文本消息]"
                else:
                    logger.debug(
                        "Feishu inbound message ignored due to empty content/media (type={}). message_id={} chat_type={}",
                        msg_type,
                        message_id,
                        chat_type,
                    )
                    return

            # Optionally fetch recent group chat history for context (requires "get group messages" permission)
            metadata: dict[str, Any] = {
                "message_id": message_id,
                "chat_type": chat_type,
                "msg_type": msg_type,
            }
            if save_only_no_reply:
                metadata["save_only_no_reply"] = True
            fetch_size = getattr(self.config, "fetch_group_history_size", 0) or 0
            if fetch_size > 0 and chat_type == "group":
                recent = await self._fetch_group_history(chat_id, fetch_size)
                if recent:
                    metadata["feishu_recent_context"] = recent

            # Forward to message bus (DM: use sender union_id so allow_from and send use union_id)
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata=metadata,
            )

        except Exception as e:
            logger.error("Error processing Feishu message: {}", e)

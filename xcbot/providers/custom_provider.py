"""Direct OpenAI-compatible provider — bypasses LiteLLM."""

from __future__ import annotations

from typing import Any

import json_repair
from loguru import logger
from openai import AsyncOpenAI

from xcbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class CustomProvider(LLMProvider):

    def __init__(self, api_key: str = "no-key", api_base: str = "http://localhost:8000/v1", default_model: str = "default"):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self._client = AsyncOpenAI(api_key=api_key, base_url=api_base)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                   model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": self._sanitize_empty_content(messages),
            "max_tokens": max(1, max_tokens),
            "temperature": temperature,
        }
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        try:
            logger.info(
                "SiliconFlow request (CustomProvider): base_url={}, path=/chat/completions, model={}, messages_count={}, tools_count={}, max_tokens={}, temperature={}",
                self.api_base,
                kwargs.get("model"),
                len(kwargs.get("messages") or []),
                len(kwargs.get("tools") or []) if kwargs.get("tools") else 0,
                kwargs.get("max_tokens"),
                kwargs.get("temperature"),
            )
            raw = await self._client.chat.completions.create(**kwargs)
            try:
                usage = getattr(raw, "usage", None)
                choice = raw.choices[0] if getattr(raw, "choices", None) else None
                msg = getattr(choice, "message", None) if choice else None
                tool_calls_count = len(getattr(msg, "tool_calls", None) or []) if msg else 0
                logger.info(
                    "SiliconFlow response (CustomProvider): finish_reason={}, usage={}, tool_calls_count={}",
                    getattr(choice, "finish_reason", None) if choice else None,
                    usage.model_dump() if hasattr(usage, "model_dump") else (usage or None),
                    tool_calls_count,
                )
            except Exception:
                logger.info("SiliconFlow response (CustomProvider): {}", raw)
            return self._parse(raw)
        except Exception as e:
            logger.error("SiliconFlow error (CustomProvider): {}", e)
            return LLMResponse(content=f"Error: {e}", finish_reason="error")

    def _parse(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        msg = choice.message

        try:
            content_preview = msg.content
            if isinstance(content_preview, str) and len(content_preview) > 800:
                content_preview = content_preview[:800] + "..."

            raw_tool_calls = []
            for tc in (msg.tool_calls or []):
                raw_args = tc.function.arguments
                raw_args_preview = raw_args
                if isinstance(raw_args_preview, str) and len(raw_args_preview) > 800:
                    raw_args_preview = raw_args_preview[:800] + "..."
                raw_tool_calls.append(
                    {
                        "id": getattr(tc, "id", None),
                        "name": getattr(tc.function, "name", None),
                        "argumentsType": type(raw_args).__name__,
                        "arguments": raw_args_preview,
                    }
                )

            logger.debug(
                "LLM raw message (CustomProvider): finish_reason={} content={} tool_calls={}",
                getattr(choice, "finish_reason", None),
                content_preview,
                raw_tool_calls,
            )
        except Exception:
            logger.debug("LLM raw message (CustomProvider): (failed to stringify)")

        def _preprocess_tool_args(s: str) -> str:
            import re

            uid_key = r"(?:uid|[A-Za-z0-9_]*_uid|[A-Za-z0-9_]*Uid|[A-Za-z0-9_]*UID)"
            pat = re.compile(rf'(\"?{uid_key}\"?\s*:\s*)(\d+_\d+)(?=\s*[,\}}\]])')
            return pat.sub(r'\1"\2"', s)

        tool_calls = []
        for tc in (msg.tool_calls or []):
            raw_args = tc.function.arguments
            parsed_args = raw_args
            if isinstance(raw_args, str):
                pre = _preprocess_tool_args(raw_args)
                raw_preview = raw_args if len(raw_args) <= 800 else raw_args[:800] + "..."
                pre_preview = pre if len(pre) <= 800 else pre[:800] + "..."
                logger.debug(
                    "Tool args parse (CustomProvider): name={} raw={} pre={}",
                    tc.function.name,
                    raw_preview,
                    pre_preview,
                )
                parsed_args = json_repair.loads(pre)
                try:
                    logger.debug(
                        "Tool args parsed (CustomProvider): name={} parsed={}",
                        tc.function.name,
                        parsed_args,
                    )
                except Exception:
                    logger.debug(
                        "Tool args parsed (CustomProvider): name={} (failed to stringify)",
                        tc.function.name,
                    )

            tool_calls.append(ToolCallRequest(id=tc.id, name=tc.function.name, arguments=parsed_args))
        u = response.usage
        content = msg.content
        reasoning_content = getattr(msg, "reasoning_content", None) or None
        if (content is None or (isinstance(content, str) and not content.strip())) and reasoning_content:
            content = reasoning_content
        return LLMResponse(
            content=content, tool_calls=tool_calls, finish_reason=choice.finish_reason or "stop",
            usage={"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens, "total_tokens": u.total_tokens} if u else {},
            reasoning_content=reasoning_content,
        )

    def get_default_model(self) -> str:
        return self.default_model


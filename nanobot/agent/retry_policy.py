"""Retry policy and failure bundling for the agent loop."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


ErrorType = Literal[
    "transient_provider",
    "rate_limit",
    "context_overflow",
    "tool_error",
    "fatal",
    "unknown",
]

Stage = Literal["normal", "system_retry", "ai_retry"]


@dataclass
class AttemptTrace:
    stage: Stage
    index: int
    started_at: float
    ok: bool
    error_type: ErrorType | None = None
    error_message: str | None = None
    final_content: str | None = None
    tools_used: list[str] | None = None


@dataclass
class FailureBundle:
    title: str
    created_at: str
    error_type: ErrorType
    failures: list[dict[str, Any]]

    def to_prompt_block(self) -> str:
        lines: list[str] = []
        lines.append(f"# {self.title}")
        lines.append(f"Created at: {self.created_at}")
        lines.append(f"Error type: {self.error_type}")
        lines.append("")
        lines.append("## Failures")
        for f in self.failures:
            lines.append(f"- stage={f.get('stage')} index={f.get('index')} err={f.get('error', '')}")
            tool_failures = f.get("tool_failures") or []
            if tool_failures:
                lines.append("  - tool_failures:")
                for t in tool_failures:
                    lines.append(f"    - {t}")
        return "\n".join(lines)


_TRANSIENT_PATTERNS = [
    re.compile(r"\b(502|503|504|521|522)\b"),
    re.compile(r"timeout", re.I),
    re.compile(r"temporarily unavailable", re.I),
    re.compile(r"connection reset", re.I),
    re.compile(r"network", re.I),
]
_RATE_LIMIT_PATTERNS = [
    re.compile(r"rate limit", re.I),
    re.compile(r"too many requests", re.I),
    re.compile(r"429", re.I),
]
_CONTEXT_OVERFLOW_PATTERNS = [
    re.compile(r"context", re.I),
    re.compile(r"maximum context", re.I),
    re.compile(r"prompt is too long", re.I),
    re.compile(r"token", re.I),
]


def classify_error_message(message: str) -> ErrorType:
    msg = (message or "").strip()
    if not msg:
        return "unknown"

    if any(p.search(msg) for p in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    if any(p.search(msg) for p in _TRANSIENT_PATTERNS):
        return "transient_provider"
    if any(p.search(msg) for p in _CONTEXT_OVERFLOW_PATTERNS):
        return "context_overflow"
    if msg.lower().startswith("error executing") or "tool" in msg.lower():
        return "tool_error"
    return "unknown"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def extract_tool_failures(messages: list[dict[str, Any]], *, max_items: int = 8) -> list[str]:
    failures: list[str] = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        if not content.startswith("Error"):
            continue
        name = m.get("name") or "tool"
        failures.append(f"{name}: {_truncate(content.replace('\n', ' '), 240)}")
        if len(failures) >= max_items:
            break
    return failures


def build_failure_bundle(
    traces: list[AttemptTrace],
    messages: list[dict[str, Any]],
    *,
    max_chars: int = 6000,
) -> FailureBundle:
    relevant = [t for t in traces if not t.ok]
    relevant = relevant[-2:] if len(relevant) > 2 else relevant

    error_type: ErrorType = relevant[-1].error_type if relevant else "unknown"
    failures: list[dict[str, Any]] = []
    tool_failures = extract_tool_failures(messages)

    for t in relevant:
        failures.append(
            {
                "stage": t.stage,
                "index": t.index,
                "error": _truncate(t.error_message or "", 500),
                "tool_failures": tool_failures,
            }
        )

    bundle = FailureBundle(
        title="Failure Bundle (for retry)",
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        error_type=error_type,
        failures=failures,
    )

    block = bundle.to_prompt_block()
    if len(block) > max_chars:
        # Hard trim: keep header and last lines.
        trimmed = block[: max_chars - 200] + "\n...\n" + block[-180:]
        bundle = FailureBundle(
            title=bundle.title,
            created_at=bundle.created_at,
            error_type=bundle.error_type,
            failures=bundle.failures,
        )
        # Store trimmed content by shortening tool failures aggressively.
        for f in bundle.failures:
            if isinstance(f.get("tool_failures"), list):
                f["tool_failures"] = [
                    _truncate(str(x), 120) for x in (f.get("tool_failures") or [])
                ][:4]
        # Ensure prompt block stays under max.
        if len(bundle.to_prompt_block()) > max_chars:
            # last resort: drop tool failures
            for f in bundle.failures:
                f.pop("tool_failures", None)
    return bundle

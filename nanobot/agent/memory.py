"""Memory system for persistent agent memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


_SAVE_TASK_LEARNING_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_task_learning",
            "description": "Save task learning to history/memory and optionally create/update a skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing what happened and how it was resolved. Start with [YYYY-MM-DD HH:MM].",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory markdown. Return unchanged if nothing new.",
                    },
                    "should_write_skill": {
                        "type": "boolean",
                        "description": "Whether to write a reusable skill. Only true if criteria are met.",
                    },
                    "skill_name": {
                        "type": "string",
                        "description": "Skill directory name to write under workspace/skills/<skill_name>/SKILL.md",
                    },
                    "skill_markdown": {
                        "type": "string",
                        "description": "Full SKILL.md contents (including YAML frontmatter if needed).",
                    },
                    "when_to_use": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Short bullet conditions when this skill applies.",
                    },
                },
                "required": ["history_entry", "memory_update", "should_write_skill"],
            },
        },
    }
]


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into MEMORY.md + HISTORY.md via LLM tool call.

        Returns True on success (including no-op), False on failure.
        """
        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation: {} to consolidate, {} keep", len(old_messages), keep_count)

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{chr(10).join(lines)}"""

        try:
            response = await provider.chat(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_MEMORY_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            args = response.tool_calls[0].arguments
            # Some providers return arguments as a JSON string instead of dict
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                logger.warning("Memory consolidation: unexpected arguments type {}", type(args).__name__)
                return False

            if entry := args.get("history_entry"):
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                self.append_history(entry)
            if update := args.get("memory_update"):
                if not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                if update != current_memory:
                    self.write_long_term(update)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info(
                "Memory consolidation done: {} messages, last_consolidated={}",
                len(session.messages),
                session.last_consolidated,
            )
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False

    async def record_task_lesson(
        self,
        *,
        session: "Session",
        provider: "LLMProvider",
        model: str,
        final_content: str,
        traces: list[object],
    ) -> bool:
        """Record task outcome after multi-stage retries.

        The model decides whether to:
        - Append a HISTORY entry
        - Update MEMORY.md
        - Optionally write a workspace skill for reusable patterns
        """

        try:
            trace_lines: list[str] = []
            for t in traces[-6:]:
                stage = getattr(t, "stage", "?")
                idx = getattr(t, "index", "?")
                ok = getattr(t, "ok", False)
                et = getattr(t, "error_type", None)
                em = getattr(t, "error_message", "")
                msg = str(em).replace("\n", " ")
                if len(msg) > 240:
                    msg = msg[:237] + "..."
                trace_lines.append(f"- stage={stage} index={idx} ok={ok} errorType={et} err={msg}")

            current_memory = self.read_long_term()
            criteria = (
                "Skill write criteria (only set should_write_skill=true if >=2 are met):\n"
                "- High repeatability: likely to recur in future\n"
                "- Fixed, template-able steps\n"
                "- Common failure patterns encountered (retries were needed)\n"
                "- Clear verification steps\n"
                "- Security/compliance constraints worth documenting\n\n"
                "Do NOT write a skill if: one-off task, highly context-dependent, creative-only, or unstable/unreliable outcome."
            )

            prompt = (
                "You are nanobot's task learning recorder.\n\n"
                "Goals:\n"
                "1) Always produce a grep-friendly HISTORY entry (2-5 sentences).\n"
                "2) Update long-term MEMORY only for stable facts/preferences/constraints.\n"
                "3) Optionally write a reusable skill only when criteria are met.\n\n"
                + criteria
                + "\n## Current Long-term Memory\n"
                + (current_memory or "(empty)")
                + "\n\n## Retry Trace Summary\n"
                + "\n".join(trace_lines)
                + "\n\n## Final Result (user-facing)\n"
                + final_content.strip()
            )

            response = await provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "Call the save_task_learning tool with the structured result.",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=_SAVE_TASK_LEARNING_TOOL,
                model=model,
            )

            if not response.has_tool_calls:
                logger.warning("Task learning: LLM did not call save_task_learning")
                return False

            args = response.tool_calls[0].arguments
            if isinstance(args, str):
                args = json.loads(args)
            if not isinstance(args, dict):
                logger.warning("Task learning: unexpected arguments type {}", type(args).__name__)
                return False

            entry = args.get("history_entry")
            if isinstance(entry, str) and entry.strip():
                self.append_history(entry)

            update = args.get("memory_update")
            if isinstance(update, str) and update != current_memory:
                self.write_long_term(update)

            should_write_skill = args.get("should_write_skill") is True
            skill_name = args.get("skill_name")
            skill_markdown = args.get("skill_markdown")

            if should_write_skill and isinstance(skill_name, str) and isinstance(skill_markdown, str):
                safe_name = skill_name.strip().strip("/\\").replace("..", "").strip()
                if safe_name:
                    skills_dir = ensure_dir(self.memory_dir.parent / "skills" / safe_name)
                    skill_file = skills_dir / "SKILL.md"
                    skill_file.write_text(skill_markdown, encoding="utf-8")

            return True
        except Exception:
            logger.exception("Task learning recording failed")
            return False

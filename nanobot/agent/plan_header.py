"""Utilities for a minimal plan header that is shown to the user and machine-parsed."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


PLAN_RULES = """# Plan Header (Required)

For EVERY assistant reply, start with a plan header that is directly visible to the user.

Format (exactly 4 lines):
计划:
- 目标: <one sentence>
- 步骤: <1-3 short steps separated by ;>
- 下一步: <the immediate next step>

Then continue with your normal response content.
"""


@dataclass(frozen=True)
class PlanHeader:
    goal: str
    steps: list[str]
    next_step: str

    def to_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "steps": self.steps, "next": self.next_step}


_PLAN_RE = re.compile(
    r"(?s)\A\s*计划:\s*\n"  # header
    r"-\s*目标\s*[:：]\s*(?P<goal>.+?)\s*\n"
    r"-\s*步骤\s*[:：]\s*(?P<steps>.+?)\s*\n"
    r"-\s*下一步\s*[:：]\s*(?P<next>.+?)\s*(?:\n|\Z)"
)


def parse_plan_header(text: str | None) -> PlanHeader | None:
    if not text:
        return None
    m = _PLAN_RE.match(text)
    if not m:
        return None
    goal = m.group("goal").strip()
    steps_raw = m.group("steps").strip()
    next_step = m.group("next").strip()

    steps = [s.strip() for s in re.split(r"\s*;\s*", steps_raw) if s.strip()]
    if not steps:
        steps = [steps_raw]

    if not goal or not next_step:
        return None

    return PlanHeader(goal=goal, steps=steps[:3], next_step=next_step)

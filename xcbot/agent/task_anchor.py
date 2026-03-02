"""Append-only task anchor store (JSONL) for minimal external task state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class TaskAnchorEntry:
    session_key: str
    timestamp: str
    goal: str
    steps: list[str]
    next_step: str
    raw: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "timestamp": self.timestamp,
            "goal": self.goal,
            "steps": self.steps,
            "next_step": self.next_step,
            "raw": self.raw,
        }


class TaskAnchorStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.state_dir = self.workspace / "state"
        self.path = self.state_dir / "task_anchors.jsonl"

    def append(self, entry: TaskAnchorEntry) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def latest(self, session_key: str) -> TaskAnchorEntry | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("rb") as f:
                data = f.read()
        except OSError:
            return None

        # KISS: reverse-scan lines in memory. Acceptable for small/medium JSONL.
        for raw_line in reversed(data.splitlines()):
            try:
                obj = json.loads(raw_line.decode("utf-8"))
            except Exception:
                continue
            if obj.get("session_key") != session_key:
                continue
            try:
                return TaskAnchorEntry(
                    session_key=obj.get("session_key", session_key),
                    timestamp=obj.get("timestamp") or datetime.now().isoformat(),
                    goal=obj.get("goal") or "",
                    steps=list(obj.get("steps") or []),
                    next_step=obj.get("next_step") or "",
                    raw=obj.get("raw") or "",
                )
            except Exception:
                continue
        return None

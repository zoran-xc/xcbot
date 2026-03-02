from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class SubagentTaskRecord:
    task_id: str
    session_key: str | None
    label: str
    task: str
    status: str
    created_at: str
    updated_at: str
    origin_channel: str | None = None
    origin_chat_id: str | None = None
    checkpoint_path: str | None = None
    last_summary: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "session_key": self.session_key,
            "label": self.label,
            "task": self.task,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "origin_channel": self.origin_channel,
            "origin_chat_id": self.origin_chat_id,
            "checkpoint_path": self.checkpoint_path,
            "last_summary": self.last_summary,
            "error": self.error,
        }


class SubagentTaskStore:
    def __init__(self, workspace: Path, *, filename: str = "subagent_tasks.json"):
        self.workspace = workspace
        self.state_dir = self.workspace / "state"
        self.path = self.state_dir / filename

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"tasks": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"tasks": {}}

    def _save(self, obj: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    def upsert(self, rec: SubagentTaskRecord) -> None:
        obj = self._load()
        tasks = obj.setdefault("tasks", {})
        tasks[rec.task_id] = rec.to_dict()
        self._save(obj)

    def create(
        self,
        *,
        task_id: str,
        session_key: str | None,
        label: str,
        task: str,
        origin_channel: str | None,
        origin_chat_id: str | None,
    ) -> SubagentTaskRecord:
        now = self._now()
        rec = SubagentTaskRecord(
            task_id=task_id,
            session_key=session_key,
            label=label,
            task=task,
            status="RUNNING",
            created_at=now,
            updated_at=now,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
        )
        self.upsert(rec)
        return rec

    def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        checkpoint_path: str | None = None,
        last_summary: str | None = None,
        error: str | None = None,
    ) -> SubagentTaskRecord | None:
        obj = self._load()
        tasks = obj.get("tasks") or {}
        existing = tasks.get(task_id)
        if not isinstance(existing, dict):
            return None

        updated = dict(existing)
        if status is not None:
            updated["status"] = status
        if checkpoint_path is not None:
            updated["checkpoint_path"] = checkpoint_path
        if last_summary is not None:
            updated["last_summary"] = last_summary
        if error is not None:
            updated["error"] = error
        updated["updated_at"] = self._now()

        tasks[task_id] = updated
        obj["tasks"] = tasks
        self._save(obj)
        return self._from_dict(updated)

    def get(self, task_id: str) -> SubagentTaskRecord | None:
        obj = self._load()
        tasks = obj.get("tasks") or {}
        existing = tasks.get(task_id)
        if not isinstance(existing, dict):
            return None
        return self._from_dict(existing)

    def list(self, *, session_key: str | None = None, limit: int = 50) -> list[SubagentTaskRecord]:
        obj = self._load()
        tasks = obj.get("tasks") or {}
        out: list[SubagentTaskRecord] = []
        for v in tasks.values():
            if not isinstance(v, dict):
                continue
            if session_key and v.get("session_key") != session_key:
                continue
            rec = self._from_dict(v)
            if rec:
                out.append(rec)
        out.sort(key=lambda r: (r.updated_at or ""), reverse=True)
        return out[: max(1, int(limit))]

    @staticmethod
    def _from_dict(d: dict[str, Any]) -> SubagentTaskRecord | None:
        try:
            return SubagentTaskRecord(
                task_id=str(d.get("task_id") or ""),
                session_key=d.get("session_key"),
                label=str(d.get("label") or ""),
                task=str(d.get("task") or ""),
                status=str(d.get("status") or ""),
                created_at=str(d.get("created_at") or ""),
                updated_at=str(d.get("updated_at") or ""),
                origin_channel=d.get("origin_channel"),
                origin_chat_id=d.get("origin_chat_id"),
                checkpoint_path=d.get("checkpoint_path"),
                last_summary=d.get("last_summary"),
                error=d.get("error"),
            )
        except Exception:
            return None

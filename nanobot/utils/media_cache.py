from __future__ import annotations

import base64
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

from nanobot.utils.helpers import ensure_dir, timestamp


class MediaCache:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.base_dir = ensure_dir(workspace / "cache" / "media")
        self.index_path = self.base_dir / "index.jsonl"

    def _append_index(self, entry: dict[str, Any]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _now_iso(self) -> str:
        return datetime.now().isoformat()

    def cleanup(self, *, keep_days: int = 1) -> None:
        cutoff = datetime.now() - timedelta(days=keep_days)

        if self.index_path.exists():
            kept: list[dict[str, Any]] = []
            for entry in self._iter_index():
                try:
                    created = datetime.fromisoformat(str(entry.get("created_at", "")))
                except Exception:
                    continue
                if created >= cutoff:
                    kept.append(entry)
                else:
                    p = entry.get("path")
                    if p:
                        try:
                            Path(str(p)).unlink(missing_ok=True)
                        except Exception:
                            pass
            with open(self.index_path, "w", encoding="utf-8") as f:
                for e in kept:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")

        for p in self.base_dir.glob("*"):
            if p.name == self.index_path.name:
                continue
            try:
                if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:
                pass

    def save_bytes(
        self,
        data: bytes,
        *,
        ext: str,
        prefix: str,
        mime: str | None = None,
        source: str | None = None,
        keep_days: int = 1,
    ) -> Path:
        safe_ext = ext if ext.startswith(".") else f".{ext}"
        name = f"{prefix}_{timestamp().replace(':', '').replace('/', '')}{safe_ext}"
        path = self.base_dir / name
        path.write_bytes(data)
        self._append_index({
            "created_at": self._now_iso(),
            "path": str(path),
            "mime": mime or "",
            "source": source or "",
            "prefix": prefix,
        })
        self.cleanup(keep_days=keep_days)
        return path

    def save_base64(
        self,
        b64: str,
        *,
        ext: str,
        prefix: str,
        mime: str | None = None,
        source: str | None = None,
        keep_days: int = 1,
    ) -> Path:
        raw = base64.b64decode(b64.strip().replace("\n", "").replace("\r", ""), validate=False)
        return self.save_bytes(raw, ext=ext, prefix=prefix, mime=mime, source=source, keep_days=keep_days)

    def _iter_index(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        items: list[dict[str, Any]] = []
        with open(self.index_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    items.append(obj)
        return items

    def recent(self, *, limit: int = 10) -> list[dict[str, Any]]:
        items = self._iter_index()

        def _ts(e: dict[str, Any]) -> float:
            try:
                return datetime.fromisoformat(str(e.get("created_at", ""))).timestamp()
            except Exception:
                return 0.0

        items.sort(key=_ts, reverse=True)
        return items[: max(0, int(limit))]

    def search(self, *, query: str, limit: int = 10) -> list[dict[str, Any]]:
        q = (query or "").strip().lower()
        if not q:
            return []

        out: list[dict[str, Any]] = []
        for e in self._iter_index():
            hay = " ".join([
                str(e.get("path", "")),
                str(e.get("mime", "")),
                str(e.get("source", "")),
                str(e.get("prefix", "")),
            ]).lower()
            if q in hay:
                out.append(e)

        def _ts(e: dict[str, Any]) -> float:
            try:
                return datetime.fromisoformat(str(e.get("created_at", ""))).timestamp()
            except Exception:
                return 0.0

        out.sort(key=_ts, reverse=True)
        return out[: max(0, int(limit))]

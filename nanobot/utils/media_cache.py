from __future__ import annotations

import base64
from pathlib import Path

from nanobot.utils.helpers import ensure_dir, timestamp


class MediaCache:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.base_dir = ensure_dir(workspace / "cache" / "media")

    def save_bytes(self, data: bytes, *, ext: str, prefix: str) -> Path:
        safe_ext = ext if ext.startswith(".") else f".{ext}"
        name = f"{prefix}_{timestamp().replace(':', '').replace('/', '')}{safe_ext}"
        path = self.base_dir / name
        path.write_bytes(data)
        return path

    def save_base64(self, b64: str, *, ext: str, prefix: str) -> Path:
        raw = base64.b64decode(b64.strip().replace("\n", "").replace("\r", ""), validate=False)
        return self.save_bytes(raw, ext=ext, prefix=prefix)

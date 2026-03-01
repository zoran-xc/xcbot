"""Idle consolidation service - auto-maintain context/memory after user inactivity."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from loguru import logger

from nanobot.agent.memory import MemoryStore

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import SessionManager


class IdleConsolidationService:
    """Periodically consolidates sessions when the user has been idle.

    This is *not* a user-facing message. It only compacts history into
    workspace/memory/MEMORY.md and appends to HISTORY.md.

    Rationale:
    - Session JSONL remains append-only for cache efficiency
    - Memory consolidation advances session.last_consolidated
    """

    def __init__(
        self,
        *,
        session_manager: "SessionManager",
        workspace,
        provider: "LLMProvider",
        model: str,
        memory_window: int,
        interval_s: int = 60,
        idle_s: int = 30 * 60,
        enabled: bool = True,
        is_busy: Callable[[str], bool] | None = None,
    ):
        self._sessions = session_manager
        self._workspace = workspace
        self._provider = provider
        self._model = model
        self._memory_window = int(memory_window)
        self._interval_s = int(interval_s)
        self._idle_s = int(idle_s)
        self._enabled = bool(enabled)
        self._is_busy = is_busy or (lambda _k: False)

        self._running = False
        self._task: asyncio.Task | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(self) -> None:
        if not self._enabled:
            logger.info("IdleConsolidation disabled")
            return
        if self._running:
            logger.warning("IdleConsolidation already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "IdleConsolidation started (interval={}s, idle={}s)",
            self._interval_s,
            self._idle_s,
        )

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(max(1, self._interval_s))
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("IdleConsolidation tick failed")

    @staticmethod
    def _parse_iso(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except Exception:
            return None

    @staticmethod
    def _now() -> datetime:
        return datetime.now()

    def _get_last_user_ts(self, session) -> datetime | None:
        # session.messages contain persisted user text messages (runtime context tags are skipped on save)
        for m in reversed(getattr(session, "messages", []) or []):
            if m.get("role") != "user":
                continue
            ts = self._parse_iso(m.get("timestamp"))
            if ts is not None:
                return ts
        return None

    async def _tick(self) -> None:
        now = self._now()
        for item in self._sessions.list_sessions():
            key = (item.get("key") or "").strip()
            if not key:
                continue
            if self._is_busy(key):
                continue

            lock = self._locks.setdefault(key, asyncio.Lock())
            if lock.locked():
                continue

            async with lock:
                try:
                    session = self._sessions.get_or_create(key)
                    last_user_ts = self._get_last_user_ts(session)
                    if last_user_ts is None:
                        continue

                    idle_seconds = (now - last_user_ts).total_seconds()
                    if idle_seconds < self._idle_s:
                        continue

                    meta = getattr(session, "metadata", {}) or {}
                    last_mark = self._parse_iso(meta.get("idle_consolidated_at"))
                    if last_mark is not None and last_mark >= last_user_ts:
                        continue

                    logger.info(
                        "IdleConsolidation: consolidating session {} (idle={}s)",
                        key,
                        int(idle_seconds),
                    )

                    ok = await MemoryStore(self._workspace).consolidate(
                        session,
                        self._provider,
                        self._model,
                        archive_all=False,
                        memory_window=self._memory_window,
                    )

                    if ok:
                        meta["idle_consolidated_at"] = now.isoformat()
                        session.metadata = meta
                        self._sessions.save(session)
                    else:
                        logger.warning("IdleConsolidation: consolidate failed for {}", key)
                except Exception:
                    logger.exception("IdleConsolidation: error consolidating {}", key)

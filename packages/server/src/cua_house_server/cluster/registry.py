"""Master-side registry of connected workers.

Populated from worker heartbeats and the `Register` frame. In the
ephemeral-VM model every tracked VM is always lease-bound, so
`vm_summaries` on each session represents in-flight tasks.

`WorkerSession` exposes derived capacity views (`free_vcpus`,
`free_memory_gb`) computed from whatever ledger the dispatcher passes in.
Master keeps its *own* capacity ledger (sum of assigned RUNNING tasks)
because heartbeat data is always a few seconds stale — the dispatcher's
table is the authoritative source at placement time. See
`dispatcher._worker_free_capacity` for the canonical computation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cua_house_server.cluster.protocol import (
    CachedShape,
    Envelope,
    WorkerCapacity,
    WorkerVMSummary,
)

if TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class WorkerSession:
    """Live connection state for one worker."""

    worker_id: str
    runtime_version: str
    capacity: WorkerCapacity
    hosted_images: set[str]
    ws: "WebSocket"
    connected_at: float
    last_heartbeat: float
    vm_summaries: list[WorkerVMSummary] = field(default_factory=list)
    cached_shapes: list[CachedShape] = field(default_factory=list)
    online: bool = True

    def has_cached_shape(
        self,
        *,
        image_key: str,
        image_version: str,
        vcpus: int,
        memory_gb: int,
        disk_gb: int,
    ) -> bool:
        """True if this worker's snapshot cache contains the exact shape.

        A match means `provision_vm` on this worker resumes via loadvm
        (~30s) instead of cold-boot (~5min).
        """
        return any(
            cs.image_key == image_key
            and cs.image_version == image_version
            and cs.vcpus == vcpus
            and cs.memory_gb == memory_gb
            and cs.disk_gb == disk_gb
            for cs in self.cached_shapes
        )


class WorkerRegistry:
    """Thread-safe collection of connected workers (asyncio-scoped)."""

    def __init__(self, heartbeat_ttl_s: float = 30.0) -> None:
        self._sessions: dict[str, WorkerSession] = {}
        self._lock = asyncio.Lock()
        self._heartbeat_ttl_s = heartbeat_ttl_s

    async def register(
        self,
        *,
        worker_id: str,
        runtime_version: str,
        capacity: WorkerCapacity,
        hosted_images: list[str],
        ws: "WebSocket",
    ) -> WorkerSession:
        now = time.monotonic()
        async with self._lock:
            existing = self._sessions.get(worker_id)
            if existing is not None and existing.online:
                logger.warning(
                    "Worker %s reconnecting, closing stale session", worker_id,
                )
                existing.online = False
            session = WorkerSession(
                worker_id=worker_id,
                runtime_version=runtime_version,
                capacity=capacity,
                hosted_images=set(hosted_images),
                ws=ws,
                connected_at=now,
                last_heartbeat=now,
            )
            self._sessions[worker_id] = session
            logger.info(
                "Worker %s registered (cpu=%d mem=%dGB images=%s)",
                worker_id,
                capacity.total_vcpus,
                capacity.total_memory_gb,
                sorted(hosted_images),
            )
            return session

    async def mark_offline(self, worker_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(worker_id)
            if session is not None:
                session.online = False
                logger.info("Worker %s marked offline", worker_id)

    async def apply_heartbeat(
        self,
        worker_id: str,
        *,
        vm_summaries: list[WorkerVMSummary],
        cached_shapes: list[CachedShape] | None = None,
    ) -> None:
        async with self._lock:
            session = self._sessions.get(worker_id)
            if session is None or not session.online:
                return
            session.last_heartbeat = time.monotonic()
            session.vm_summaries = vm_summaries
            if cached_shapes is not None:
                session.cached_shapes = cached_shapes

    async def reap_stale(self) -> list[str]:
        """Mark workers whose heartbeat is older than TTL as offline."""
        now = time.monotonic()
        evicted: list[str] = []
        async with self._lock:
            for worker_id, session in self._sessions.items():
                if not session.online:
                    continue
                if now - session.last_heartbeat > self._heartbeat_ttl_s:
                    session.online = False
                    evicted.append(worker_id)
        for worker_id in evicted:
            logger.warning("Worker %s heartbeat timeout, marked offline", worker_id)
        return evicted

    async def snapshot(self) -> list[WorkerSession]:
        async with self._lock:
            return list(self._sessions.values())

    async def online_snapshot(self) -> list[WorkerSession]:
        """Online-only view. Used by dispatcher at placement time."""
        async with self._lock:
            return [s for s in self._sessions.values() if s.online]

    async def get(self, worker_id: str) -> WorkerSession | None:
        async with self._lock:
            return self._sessions.get(worker_id)

    async def send(self, worker_id: str, envelope: Envelope) -> bool:
        """Send an envelope to a worker. Returns False if worker is gone."""
        session = await self.get(worker_id)
        if session is None or not session.online:
            return False
        try:
            await session.ws.send_json(envelope.model_dump())
            return True
        except Exception as exc:  # pragma: no cover — network edge
            logger.warning("Send to worker %s failed: %s", worker_id, exc)
            await self.mark_offline(worker_id)
            return False

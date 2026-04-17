"""Master-side registry of connected workers.

The registry is the single source of truth for "what's actually running on
each worker right now" — populated from worker heartbeats and pool op results.
It's paired with ``ClusterPoolSpec`` (desired state) by the reconciler loop.

Lifecycle:

1. A worker connects over WebSocket and sends ``Register``.
2. Master allocates a ``WorkerSession`` and stores it here, keyed by
   ``worker_id``. Reconnects by the same worker_id replace the old session.
3. Heartbeats update ``last_heartbeat`` + ``vm_summaries``. A reaper task
   marks sessions OFFLINE after ``heartbeat_ttl_s`` without updates.
4. The reconciler reads ``hosted_images`` / ``vm_summaries`` to compute diff
   against desired state, and ``send(Envelope)`` to dispatch PoolOps.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cua_house_server.cluster.protocol import (
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
    load_cpu: float = 0.0
    load_memory: float = 0.0
    online: bool = True

    def free_vm_for(
        self, image_key: str, vcpus: int, memory_gb: int, disk_gb: int,
    ) -> WorkerVMSummary | None:
        """Return a READY VM matching the shape request, if any.

        Smallest-fit: prefer the tightest VM that still satisfies the request
        so a batch of small tasks doesn't squat on oversized VMs.
        """
        best: WorkerVMSummary | None = None
        for vm in self.vm_summaries:
            if vm.state != "ready":
                continue
            if vm.image_key != image_key:
                continue
            if vm.vcpus < vcpus or vm.memory_gb < memory_gb or vm.disk_gb < disk_gb:
                continue
            if best is None or (
                vm.vcpus, vm.memory_gb, vm.disk_gb
            ) < (best.vcpus, best.memory_gb, best.disk_gb):
                best = vm
        return best


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
                    "Worker %s reconnecting, closing stale session", worker_id
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
        load_cpu: float,
        load_memory: float,
        vm_summaries: list[WorkerVMSummary],
    ) -> None:
        async with self._lock:
            session = self._sessions.get(worker_id)
            if session is None or not session.online:
                return
            session.last_heartbeat = time.monotonic()
            session.load_cpu = load_cpu
            session.load_memory = load_memory
            # Preserve optimistic lease marks set by the dispatcher.
            # The dispatcher marks a VM as "leased" (with a lease_id and
            # _mark_time) before the worker has received the AssignTask
            # message. If we blindly replace vm_summaries, the worker's
            # stale "ready" overwrites the mark → double-booking.
            #
            # We ONLY restore leases that have a _mark_time (set by the
            # dispatcher in _try_assign) and are recent (< 60s). Leases
            # reported by the worker (from stale state after restart) do
            # NOT get _mark_time, so they are never forcibly restored —
            # the heartbeat data is authoritative for those.
            now = time.monotonic()
            dispatcher_marks: dict[str, tuple[str, float]] = {}
            for vm in session.vm_summaries:
                mark_time = getattr(vm, "_mark_time", 0.0)
                if vm.lease_id and vm.state == "leased" and mark_time:
                    dispatcher_marks[vm.vm_id] = (vm.lease_id, mark_time)
            for vm in vm_summaries:
                saved = dispatcher_marks.get(vm.vm_id)
                if saved and vm.state == "ready" and not vm.lease_id:
                    saved_lease, mark_time = saved
                    if (now - mark_time) < 60:
                        vm.state = "leased"
                        vm.lease_id = saved_lease
                        vm._mark_time = mark_time  # type: ignore[attr-defined]
            session.vm_summaries = vm_summaries

    async def apply_vm_state_update(
        self,
        worker_id: str,
        *,
        vm_id: str,
        state: str,
        lease_id: str | None,
    ) -> None:
        async with self._lock:
            session = self._sessions.get(worker_id)
            if session is None or not session.online:
                return
            for vm in session.vm_summaries:
                if vm.vm_id == vm_id:
                    vm.state = state
                    vm.lease_id = lease_id
                    return
            # Unknown vm_id — the next heartbeat will reconcile.

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
        except Exception as exc:  # pragma: no cover - network edge
            logger.warning("Send to worker %s failed: %s", worker_id, exc)
            await self.mark_offline(worker_id)
            return False

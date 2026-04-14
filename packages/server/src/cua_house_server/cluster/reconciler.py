"""Master-side periodic reconciler driving pool convergence.

Responsibilities:

1. Own an ``asyncio.Future`` registry keyed by pool-op id, so individual
   ``send`` calls can ``await`` the corresponding ``PoolOpResult`` delivered
   on the worker's WS link.
2. On an interval, walk every online worker, compute the diff against
   ``ClusterPoolSpec``, and dispatch ops sequentially per-worker. Different
   workers are reconciled in parallel.
3. Emit structured events for observability; never raise out of the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from cua_house_server.cluster.pool_spec import (
    ClusterPoolSpec,
    DiffEntry,
    compute_diff,
)
from cua_house_server.cluster.protocol import (
    Envelope,
    PoolOp,
    PoolOpArgs,
    PoolOpResult,
)
from cua_house_server.cluster.registry import WorkerRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingOp:
    op_id: str
    worker_id: str
    created_at: float
    future: asyncio.Future  # resolved value is kind-specific


class PoolOpCoordinator:
    """Generic request/response coordinator for master→worker RPCs.

    Despite the legacy name, this is the single in-flight correlation table
    used by both the pool reconciler (PoolOpResult) and the cluster
    dispatcher (TaskBound / TaskReleased). The discriminator is ``op_id``
    which maps 1:1 to the ``correlation_id`` on the reply envelope — callers
    stamp outgoing envelopes with the op_id returned by ``issue`` and the
    master WS handler routes any correlated reply back here via ``resolve``.
    """

    def __init__(self, default_timeout_s: float = 120.0) -> None:
        self._pending: dict[str, PendingOp] = {}
        self._lock = asyncio.Lock()
        self._default_timeout_s = default_timeout_s

    async def issue(self, worker_id: str) -> PendingOp:
        op_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        pending = PendingOp(
            op_id=op_id,
            worker_id=worker_id,
            created_at=time.monotonic(),
            future=loop.create_future(),
        )
        async with self._lock:
            self._pending[op_id] = pending
        return pending

    async def resolve(self, op_id: str, value: Any) -> None:
        async with self._lock:
            pending = self._pending.pop(op_id, None)
        if pending is None:
            logger.debug("Dropping reply for unknown op_id %s", op_id)
            return
        if not pending.future.done():
            pending.future.set_result(value)

    async def cancel_worker(self, worker_id: str) -> None:
        """Fail all pending ops for a worker that just went offline."""
        async with self._lock:
            stale = [
                op_id for op_id, p in self._pending.items()
                if p.worker_id == worker_id
            ]
            for op_id in stale:
                pending = self._pending.pop(op_id)
                if not pending.future.done():
                    pending.future.set_exception(
                        ConnectionError(f"worker {worker_id} disconnected")
                    )

    async def await_result(
        self,
        pending: PendingOp,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        timeout = timeout_s if timeout_s is not None else self._default_timeout_s
        try:
            return await asyncio.wait_for(pending.future, timeout=timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                self._pending.pop(pending.op_id, None)
            raise


class PoolReconciler:
    """Periodic converger driving workers toward ``ClusterPoolSpec``."""

    def __init__(
        self,
        *,
        registry: WorkerRegistry,
        pool_spec: ClusterPoolSpec,
        coordinator: PoolOpCoordinator,
        interval_s: float = 5.0,
        op_timeout_s: float = 120.0,
        on_worker_evicted: Any = None,
    ) -> None:
        self.registry = registry
        self.pool_spec = pool_spec
        self.coordinator = coordinator
        self.interval_s = interval_s
        self.op_timeout_s = op_timeout_s
        self.on_worker_evicted = on_worker_evicted
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.reconcile_once()
            except Exception:
                logger.exception("Reconciler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    async def reconcile_once(self) -> None:
        """Single reconciliation pass. Exposed for tests."""
        evicted = await self.registry.reap_stale()
        for worker_id in evicted:
            await self.coordinator.cancel_worker(worker_id)
            if self.on_worker_evicted is not None:
                try:
                    await self.on_worker_evicted(worker_id)
                except Exception:
                    logger.exception("on_worker_evicted(%s) failed", worker_id)
        sessions = await self.registry.snapshot()
        online = [s for s in sessions if s.online]
        if not online:
            return
        await asyncio.gather(
            *[self._reconcile_worker(s.worker_id) for s in online],
            return_exceptions=True,
        )

    async def _reconcile_worker(self, worker_id: str) -> None:
        session = await self.registry.get(worker_id)
        if session is None or not session.online:
            return
        diff = compute_diff(
            spec=self.pool_spec,
            worker_id=worker_id,
            worker_images=set(session.hosted_images),
            worker_vms=list(session.vm_summaries),
        )
        if not diff:
            return
        logger.info(
            "Reconciling worker %s: %d ops (%s)",
            worker_id, len(diff),
            ", ".join(f"{e.op}:{e.image_key or e.vm_id}" for e in diff),
        )
        for entry in diff:
            try:
                await self._dispatch_op(worker_id, entry)
            except Exception as exc:
                logger.warning(
                    "PoolOp %s failed for worker %s: %s", entry.op, worker_id, exc,
                )
                # Bail on this worker's tick; next tick will retry.
                return

    async def _dispatch_op(self, worker_id: str, entry: DiffEntry) -> PoolOpResult:
        pending = await self.coordinator.issue(worker_id)
        op = PoolOp(
            op_id=pending.op_id,
            op=entry.op,  # type: ignore[arg-type]
            args=PoolOpArgs(
                image_key=entry.image_key,
                vm_id=entry.vm_id,
                cpu_cores=entry.cpu_cores,
                memory_gb=entry.memory_gb,
            ),
        )
        envelope = Envelope(
            msg_id=pending.op_id,
            correlation_id=pending.op_id,
            payload=op.model_dump(),
        )
        sent = await self.registry.send(worker_id, envelope)
        if not sent:
            await self.coordinator.cancel_worker(worker_id)
            raise ConnectionError(f"failed to send to {worker_id}")
        result = await self.coordinator.await_result(pending, timeout_s=self.op_timeout_s)
        if not result.ok:
            raise RuntimeError(result.error or "unknown PoolOp error")
        # Optimistic local update so the next compute_diff doesn't re-emit:
        # real state comes in via the worker's next Heartbeat.
        await self._apply_optimistic_update(worker_id, entry, result)
        return result

    async def _apply_optimistic_update(
        self,
        worker_id: str,
        entry: DiffEntry,
        result: PoolOpResult,
    ) -> None:
        from cua_house_server.cluster.protocol import WorkerVMSummary
        session = await self.registry.get(worker_id)
        if session is None:
            return
        if entry.op == "ADD_IMAGE" and entry.image_key:
            session.hosted_images.add(entry.image_key)
        elif entry.op == "REMOVE_IMAGE" and entry.image_key:
            session.hosted_images.discard(entry.image_key)
        elif entry.op == "ADD_VM" and result.produced_vm_id and entry.image_key:
            session.vm_summaries.append(
                WorkerVMSummary(
                    vm_id=result.produced_vm_id,
                    image_key=entry.image_key,
                    cpu_cores=entry.cpu_cores or 0,
                    memory_gb=entry.memory_gb or 0,
                    state="ready",
                )
            )
        elif entry.op == "REMOVE_VM" and entry.vm_id:
            session.vm_summaries = [
                vm for vm in session.vm_summaries if vm.vm_id != entry.vm_id
            ]

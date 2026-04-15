"""Master-side batch admission + task dispatcher for cluster mode.

Scope (deliberately narrow):

* Accept batches from clients and persist them in memory.
* Pick a candidate worker + VM and send ``AssignTask`` over WS.
* Record ``lease_endpoint`` + VM URLs that the worker returned in TaskBound,
  so the client receives a ``TaskAssignment`` pointing **directly at the
  worker's HTTP API**. Master is not in the per-lease data path.
* Aggregate batch state by consuming ``TaskCompleted`` events the worker
  fires asynchronously whenever a lease terminates.
* Cancel batches by sending ``ReleaseLease`` to every affected worker over
  WS.

**Not** in scope (intentional): heartbeat relay, staging proxy, complete
RPC. Those live on the worker's existing HTTP routes — clients call the
worker directly using the lease_endpoint they received.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

from cua_house_common.models import (
    BatchCreateRequest,
    BatchHeartbeatResponse,
    BatchState,
    BatchStatus,
    TaskAssignment,
    TaskState,
    TaskStatus,
    utcnow,
)
from cua_house_server.cluster.protocol import (
    AssignTask,
    Envelope,
    ReleaseLease,
    TaskBound,
    TaskCompleted,
)
from cua_house_server.cluster.reconciler import PoolOpCoordinator
from cua_house_server.cluster.registry import WorkerRegistry, WorkerSession
from cua_house_server.config.loader import HostRuntimeConfig, ImageSpec

logger = logging.getLogger(__name__)


class ClusterDispatcher:
    """Cluster-mode batch admission + task assignment owner."""

    def __init__(
        self,
        *,
        host_config: HostRuntimeConfig,
        images: dict[str, ImageSpec],
        registry: WorkerRegistry,
        coordinator: PoolOpCoordinator,
    ) -> None:
        self.host_config = host_config
        self.images = images
        self.registry = registry
        self.coordinator = coordinator
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskStatus] = {}
        self._batches: dict[str, BatchStatus] = {}
        # lease_id → (task_id, worker_id)
        self._leases: dict[str, tuple[str, str]] = {}
        self._dispatch_task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    async def start(self) -> None:
        if self._dispatch_task is None:
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def shutdown(self) -> None:
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):
                pass
            self._dispatch_task = None

    # ── Batch admission ────────────────────────────────────────────────

    async def submit_batch(self, request: BatchCreateRequest) -> BatchStatus:
        created = utcnow()
        batch_id = request.batch_id or str(uuid4())
        async with self._lock:
            if batch_id in self._batches:
                raise ValueError(f"batch_id already exists: {batch_id}")
            tasks: list[TaskStatus] = []
            for req in request.tasks:
                if req.task_id in self._tasks:
                    raise ValueError(f"task_id already exists: {req.task_id}")
                vcpus, memory_gb, disk_gb = self._resolve_resources(
                    req.snapshot_name, req.vcpus, req.memory_gb, req.disk_gb,
                )
                task = TaskStatus(
                    task_id=req.task_id,
                    task_path=req.task_path,
                    snapshot_name=req.snapshot_name,
                    vcpus=vcpus,
                    memory_gb=memory_gb,
                    disk_gb=disk_gb,
                    metadata=req.metadata,
                    task_data=req.task_data,
                    state=TaskState.QUEUED,
                    batch_id=batch_id,
                    created_at=created,
                    updated_at=created,
                )
                self._tasks[task.task_id] = task
                tasks.append(task)
            batch = BatchStatus(
                batch_id=batch_id,
                state=BatchState.QUEUED,
                created_at=created,
                updated_at=created,
                expires_at=created + timedelta(seconds=self.host_config.batch_heartbeat_ttl_s),
                tasks=tasks,
            )
            self._batches[batch_id] = batch
        self._wake.set()
        return await self.get_batch(batch_id)

    # ── Queries ────────────────────────────────────────────────────────

    async def get_batch(self, batch_id: str) -> BatchStatus:
        async with self._lock:
            return self._snapshot_batch(self._batches[batch_id])

    async def get_task(self, task_id: str) -> TaskStatus:
        async with self._lock:
            return self._tasks[task_id].model_copy(deep=True)

    async def lookup_lease_endpoint(self, lease_id: str) -> str | None:
        """Return the worker's lease_endpoint URL for a live lease.

        Used by the master-side lease proxy to forward ``/v1/leases/*``
        client calls to the owning worker. Returns None if the lease is
        unknown or its task has already terminated.
        """
        async with self._lock:
            binding = self._leases.get(lease_id)
            if binding is None:
                return None
            task_id, _ = binding
            task = self._tasks.get(task_id)
            if task is None or task.assignment is None:
                return None
            return task.assignment.lease_endpoint

    async def heartbeat_batch(self, batch_id: str) -> BatchHeartbeatResponse:
        async with self._lock:
            batch = self._batches[batch_id]
            batch.expires_at = utcnow() + timedelta(seconds=self.host_config.batch_heartbeat_ttl_s)
            batch.updated_at = utcnow()
            return BatchHeartbeatResponse(batch_id=batch_id, expires_at=batch.expires_at)

    # ── Cancel ─────────────────────────────────────────────────────────

    async def cancel_batch(
        self,
        batch_id: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> BatchStatus:
        details = details or {}
        to_release: list[tuple[str, str]] = []  # (worker_id, lease_id)
        async with self._lock:
            batch = self._batches[batch_id]
            now = utcnow()
            for batch_task in batch.tasks:
                task = self._tasks[batch_task.task_id]
                if task.state == TaskState.QUEUED:
                    task.state = TaskState.FAILED
                    task.error = reason
                    task.completed_at = now
                    task.updated_at = now
                elif task.lease_id is not None and task.lease_id in self._leases:
                    _, worker_id = self._leases[task.lease_id]
                    to_release.append((worker_id, task.lease_id))
            self._refresh_batch_states_locked()
        # Outside the lock: fire-and-forget release to each worker.
        for worker_id, lease_id in to_release:
            release = ReleaseLease(lease_id=lease_id, final_status="abandoned")
            env = Envelope(msg_id=str(uuid4()), payload=release.model_dump())
            await self.registry.send(worker_id, env)
        return await self.get_batch(batch_id)

    # ── Event ingestion ────────────────────────────────────────────────

    async def handle_worker_disconnect(self, worker_id: str) -> None:
        """Fail every task whose lease was bound to a now-offline worker.

        Called from two places: the WebSocket disconnect finally-block and
        the reconciler's ``reap_stale`` tick. Idempotent — a second call
        for the same worker after leases are already cleaned up is a no-op.

        We deliberately do NOT try to preserve state for a worker that
        might reconnect. If the worker comes back it will re-register via
        ``Register`` with an empty in-memory lease state, so any task we
        left in STARTING/READY would be unrecoverable anyway. Failing fast
        lets operators (or a higher-level retry layer) resubmit.
        """
        orphaned: list[str] = []
        async with self._lock:
            for lease_id, (task_id, w) in list(self._leases.items()):
                if w != worker_id:
                    continue
                orphaned.append(lease_id)
                task = self._tasks.get(task_id)
                if task is None:
                    self._leases.pop(lease_id, None)
                    continue
                if task.state in {TaskState.COMPLETED, TaskState.FAILED}:
                    self._leases.pop(lease_id, None)
                    continue
                task.state = TaskState.FAILED
                task.error = f"worker {worker_id} disconnected"
                task.updated_at = utcnow()
                task.completed_at = utcnow()
                self._leases.pop(lease_id, None)
            if orphaned:
                self._refresh_batch_states_locked()
                logger.warning(
                    "worker %s disconnect: failed %d orphaned leases",
                    worker_id, len(orphaned),
                )

    async def handle_task_completed(self, worker_id: str, msg: TaskCompleted) -> None:
        async with self._lock:
            task = self._tasks.get(msg.task_id)
            if task is None:
                logger.debug("TaskCompleted for unknown task %s from %s", msg.task_id, worker_id)
                return
            if msg.final_status == "completed":
                task.state = TaskState.COMPLETED
            else:
                task.state = TaskState.FAILED
                task.error = msg.error or msg.final_status
            task.updated_at = utcnow()
            task.completed_at = utcnow()
            self._leases.pop(msg.lease_id, None)
            self._refresh_batch_states_locked()
        # Optimistically free the VM in registry so the next dispatch tick
        # can reuse it without waiting for the worker's next heartbeat.
        session = await self.registry.get(worker_id)
        if session is not None:
            for vm in session.vm_summaries:
                if vm.lease_id == msg.lease_id:
                    vm.state = "ready"
                    vm.lease_id = None
                    break
        self._wake.set()

    # ── Dispatch loop ──────────────────────────────────────────────────

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self._dispatch_pending()
            except Exception:
                logger.exception("cluster dispatch tick failed")

    async def _dispatch_pending(self) -> None:
        async with self._lock:
            queued = [t for t in self._tasks.values() if t.state == TaskState.QUEUED]
        for task in queued:
            await self._try_assign(task.task_id)

    async def _try_assign(self, task_id: str) -> None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.state != TaskState.QUEUED:
                return
        session, vm_id = await self._pick_worker(task)
        if session is None or vm_id is None:
            return
        lease_id = str(uuid4())
        pending = await self.coordinator.issue(session.worker_id)
        assign = AssignTask(
            task_id=task.task_id,
            lease_id=lease_id,
            vm_id=vm_id,
            image_key=task.snapshot_name,
            task_path=task.task_path,
            vcpus=task.vcpus,
            memory_gb=task.memory_gb,
            disk_gb=task.disk_gb,
            task_data=task.task_data.model_dump() if task.task_data else None,
            metadata=task.metadata,
        )
        env = Envelope(
            msg_id=pending.op_id,
            correlation_id=pending.op_id,
            payload=assign.model_dump(),
        )
        async with self._lock:
            task.state = TaskState.STARTING
            task.lease_id = lease_id
            task.updated_at = utcnow()
            self._leases[lease_id] = (task.task_id, session.worker_id)
            # Optimistically mark VM leased so the next tick / a concurrent
            # reconciler tick doesn't try to book the same slot.
            for vm in session.vm_summaries:
                if vm.vm_id == vm_id:
                    vm.state = "leased"
                    vm.lease_id = lease_id
                    break
        sent = await self.registry.send(session.worker_id, env)
        if not sent:
            await self._revert_assignment(task_id, lease_id, session.worker_id, vm_id, "worker unreachable")
            return
        try:
            result = await self.coordinator.await_result(
                pending, timeout_s=self.host_config.ready_timeout_s,
            )
        except Exception as exc:
            await self._revert_assignment(task_id, lease_id, session.worker_id, vm_id, f"assign timeout: {exc}")
            return
        if not isinstance(result, TaskBound) or not result.ok:
            err = getattr(result, "error", None) or "assign failed"
            await self._revert_assignment(task_id, lease_id, session.worker_id, vm_id, err)
            return
        async with self._lock:
            task.state = TaskState.READY
            task.assignment = TaskAssignment(
                host_id=session.worker_id,
                lease_id=lease_id,
                slot_id=vm_id,
                snapshot_name=task.snapshot_name,
                urls=result.urls,
                novnc_url=result.novnc_url,
                lease_endpoint=result.lease_endpoint,
            )
            task.updated_at = utcnow()

    async def _pick_worker(self, task: TaskStatus) -> tuple[WorkerSession | None, str | None]:
        sessions = await self.registry.snapshot()
        best: tuple[WorkerSession, str, int] | None = None
        for session in sessions:
            if not session.online:
                continue
            vm = session.free_vm_for(
                task.snapshot_name, task.vcpus, task.memory_gb, task.disk_gb,
            )
            if vm is None:
                continue
            leased_count = sum(1 for v in session.vm_summaries if v.state == "leased")
            if best is None or leased_count < best[2]:
                best = (session, vm.vm_id, leased_count)
        if best is None:
            # No worker matches. Task stays QUEUED. This is intentional:
            # the client is expected to route explicitly (e.g. agenthle
            # decides between the local cluster and a GCP-only runtime
            # upstream of cua-house). Silent dispatcher-side overflow to
            # ``GCPVMRuntime`` was considered and explicitly rejected —
            # see docs/architecture/cluster.md §"What's deliberately NOT
            # in the cluster".
            return None, None
        return best[0], best[1]

    # ── Helpers ────────────────────────────────────────────────────────

    async def _revert_assignment(
        self,
        task_id: str,
        lease_id: str,
        worker_id: str,
        vm_id: str,
        reason: str,
    ) -> None:
        logger.warning("Reverting task %s assignment: %s", task_id, reason)
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.state = TaskState.QUEUED
                task.lease_id = None
                task.assignment = None
                task.error = reason
                task.updated_at = utcnow()
            self._leases.pop(lease_id, None)
        session = await self.registry.get(worker_id)
        if session is not None:
            for vm in session.vm_summaries:
                if vm.vm_id == vm_id and vm.lease_id == lease_id:
                    vm.state = "ready"
                    vm.lease_id = None
                    break

    def _refresh_batch_states_locked(self) -> None:
        for batch in self._batches.values():
            batch.tasks = [self._tasks[t.task_id].model_copy(deep=True) for t in batch.tasks]
            states = {t.state for t in batch.tasks}
            if states <= {TaskState.COMPLETED}:
                batch.state = BatchState.COMPLETED
            elif TaskState.FAILED in states and states <= {TaskState.COMPLETED, TaskState.FAILED}:
                batch.state = BatchState.FAILED
            elif TaskState.QUEUED in states:
                batch.state = BatchState.QUEUED if states == {TaskState.QUEUED} else BatchState.RUNNING
            else:
                batch.state = BatchState.RUNNING
            batch.updated_at = utcnow()

    @staticmethod
    def _snapshot_batch(batch: BatchStatus) -> BatchStatus:
        return batch.model_copy(deep=True)

    def _resolve_resources(
        self,
        snapshot_name: str,
        vcpus: int | None,
        memory_gb: int | None,
        disk_gb: int | None,
    ) -> tuple[int, int, int]:
        image = self.images.get(snapshot_name)
        if image is None:
            raise ValueError(f"unknown snapshot_name: {snapshot_name}")
        return (
            vcpus if vcpus is not None else image.default_vcpus,
            memory_gb if memory_gb is not None else image.default_memory_gb,
            disk_gb if disk_gb is not None else image.default_disk_gb,
        )

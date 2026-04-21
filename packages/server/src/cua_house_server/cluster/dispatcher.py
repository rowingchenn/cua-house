"""Master-side batch admission + event-driven task placement.

Scope (deliberately narrow):

* Accept batches from clients and persist them in memory.
* Admission: reject any task whose shape exceeds the single-machine
  capacity of every online worker (equivalent to "unknown image").
* On each QUEUED task, run `pick_worker` (capacity hard gate → cache
  affinity soft preference → least-loaded tiebreak) and send
  `AssignTask` over WS. No dispatch loop — placement is event-driven:
  every task_completed and worker_disconnect fires a `reevaluate_queued`
  that retries all still-QUEUED tasks.
* Aggregate batch state by consuming `TaskCompleted` events.
* Cancel batches by sending `ReleaseLease` to every owning worker.

Capacity ledger is master-authoritative: free_vcpus / free_memory_gb on
a session = total − reserved − sum(RUNNING task shapes the dispatcher
itself assigned to that worker). Heartbeat data is used only for
`cached_shapes`, never for capacity math — stale heartbeats must never
cause overbooking.
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
from cua_house_server.cluster.coordinator import RpcCoordinator
from cua_house_server.cluster.protocol import (
    AssignTask,
    Envelope,
    ReleaseLease,
    TaskBound,
    TaskCompleted,
)
from cua_house_server.cluster.registry import WorkerRegistry, WorkerSession
from cua_house_server.config.loader import HostRuntimeConfig, ImageSpec

logger = logging.getLogger(__name__)


class ClusterDispatcher:
    """Cluster-mode batch admission + event-driven task assignment."""

    def __init__(
        self,
        *,
        host_config: HostRuntimeConfig,
        images: dict[str, ImageSpec],
        registry: WorkerRegistry,
        coordinator: RpcCoordinator,
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
        # worker_id → {task_id: (vcpus, memory_gb)} — master's capacity ledger.
        self._worker_load: dict[str, dict[str, tuple[int, int]]] = {}

    async def start(self) -> None:
        # No background dispatch loop; placement is event-driven.
        pass

    async def shutdown(self) -> None:
        pass

    # ── admission ──────────────────────────────────────────────────────

    async def submit_batch(self, request: BatchCreateRequest) -> BatchStatus:
        created = utcnow()
        batch_id = request.batch_id or str(uuid4())
        max_vcpus, max_memory = self._max_online_worker_capacity()
        async with self._lock:
            if batch_id in self._batches:
                raise ValueError(f"batch_id already exists: {batch_id}")
            tasks: list[TaskStatus] = []
            to_place: list[str] = []
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
                # Admission: fail upfront if no online worker could ever fit
                # this shape. Equivalent to "unknown image" — no queue wait
                # for impossible work.
                if max_vcpus is None or max_memory is None:
                    # No workers online at submit time — accept, let queue
                    # drain when a worker joins.
                    pass
                elif vcpus > max_vcpus or memory_gb > max_memory:
                    task.state = TaskState.FAILED
                    task.error = (
                        f"no_worker_fits: task requires {vcpus} vCPU / "
                        f"{memory_gb} GiB but largest online worker offers "
                        f"{max_vcpus} vCPU / {max_memory} GiB"
                    )
                    task.completed_at = created
                self._tasks[task.task_id] = task
                tasks.append(task)
                if task.state == TaskState.QUEUED:
                    to_place.append(task.task_id)
            batch = BatchStatus(
                batch_id=batch_id,
                state=BatchState.QUEUED,
                created_at=created,
                updated_at=created,
                expires_at=created + timedelta(seconds=self.host_config.batch_heartbeat_ttl_s),
                tasks=tasks,
            )
            self._batches[batch_id] = batch
            self._refresh_batch_states_locked()
        # Kick placement for each admitted task. Fire-and-forget; failures
        # (no worker yet) leave the task QUEUED and a later event reawakens
        # placement.
        for task_id in to_place:
            asyncio.create_task(self._try_place(task_id))
        return await self.get_batch(batch_id)

    # ── queries ────────────────────────────────────────────────────────

    async def get_batch(self, batch_id: str) -> BatchStatus:
        async with self._lock:
            return self._snapshot_batch(self._batches[batch_id])

    async def get_task(self, task_id: str) -> TaskStatus:
        async with self._lock:
            return self._tasks[task_id].model_copy(deep=True)

    async def list_tasks(
        self, *, state: TaskState | None = None,
    ) -> list[TaskStatus]:
        async with self._lock:
            tasks = self._tasks.values()
            if state is not None:
                tasks = (t for t in tasks if t.state == state)
            return [t.model_copy(deep=True) for t in tasks]

    async def list_batches(
        self, *, state: BatchState | None = None,
    ) -> list[BatchStatus]:
        async with self._lock:
            batches = self._batches.values()
            if state is not None:
                batches = (b for b in batches if b.state == state)
            return [self._snapshot_batch(b) for b in batches]

    async def lookup_lease_endpoint(self, lease_id: str) -> str | None:
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
            batch.expires_at = utcnow() + timedelta(
                seconds=self.host_config.batch_heartbeat_ttl_s,
            )
            batch.updated_at = utcnow()
            return BatchHeartbeatResponse(batch_id=batch_id, expires_at=batch.expires_at)

    # ── cancel ─────────────────────────────────────────────────────────

    async def cancel_batch(
        self,
        batch_id: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> BatchStatus:
        details = details or {}
        to_release: list[tuple[str, str]] = []
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
        for worker_id, lease_id in to_release:
            release = ReleaseLease(lease_id=lease_id, final_status="abandoned")
            env = Envelope(msg_id=str(uuid4()), payload=release.model_dump())
            await self.registry.send(worker_id, env)
        return await self.get_batch(batch_id)

    # ── event ingestion ────────────────────────────────────────────────

    async def handle_worker_disconnect(self, worker_id: str) -> None:
        """Requeue every task whose lease was bound to a now-offline worker.

        Tasks with `retry_count > 2` permanently FAIL; others go back to
        QUEUED and pick another worker on the next placement attempt.
        """
        to_requeue: list[str] = []
        async with self._lock:
            self._worker_load.pop(worker_id, None)
            for lease_id, (task_id, w) in list(self._leases.items()):
                if w != worker_id:
                    continue
                task = self._tasks.get(task_id)
                self._leases.pop(lease_id, None)
                if task is None:
                    continue
                if task.state in {TaskState.COMPLETED, TaskState.FAILED}:
                    continue
                retry = int(task.metadata.get("retry_count", 0)) + 1
                task.metadata["retry_count"] = retry
                if retry > 2:
                    task.state = TaskState.FAILED
                    task.error = (
                        f"worker {worker_id} disconnected; "
                        f"exceeded retry budget (tried {retry}x)"
                    )
                    task.updated_at = utcnow()
                    task.completed_at = utcnow()
                    continue
                task.state = TaskState.QUEUED
                task.lease_id = None
                task.assignment = None
                task.error = f"worker {worker_id} disconnected; requeued"
                task.updated_at = utcnow()
                to_requeue.append(task_id)
            self._refresh_batch_states_locked()
        for task_id in to_requeue:
            asyncio.create_task(self._try_place(task_id))

    async def handle_task_completed(
        self, worker_id: str, msg: TaskCompleted,
    ) -> None:
        async with self._lock:
            task = self._tasks.get(msg.task_id)
            if task is None:
                logger.debug(
                    "TaskCompleted for unknown task %s from %s",
                    msg.task_id, worker_id,
                )
                return
            if msg.final_status == "completed":
                task.state = TaskState.COMPLETED
            else:
                task.state = TaskState.FAILED
                task.error = msg.error or msg.final_status
            task.updated_at = utcnow()
            task.completed_at = utcnow()
            self._leases.pop(msg.lease_id, None)
            # Release capacity back to the ledger.
            self._worker_load.get(worker_id, {}).pop(msg.task_id, None)
            self._refresh_batch_states_locked()
        await self._reevaluate_queued()

    # ── placement ──────────────────────────────────────────────────────

    async def _reevaluate_queued(self) -> None:
        async with self._lock:
            queued = [
                t.task_id for t in self._tasks.values()
                if t.state == TaskState.QUEUED
            ]
        for task_id in queued:
            asyncio.create_task(self._try_place(task_id))

    async def _try_place(self, task_id: str) -> None:
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.state != TaskState.QUEUED:
                return
        session = await self._pick_worker(task)
        if session is None:
            return
        lease_id = str(uuid4())
        pending = await self.coordinator.issue(session.worker_id)
        assign = AssignTask(
            task_id=task.task_id,
            lease_id=lease_id,
            image_key=task.snapshot_name,
            image_version=self._image_version(task.snapshot_name),
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
            # Re-verify still QUEUED (could have been cancelled between the
            # pick and here) before committing capacity.
            task = self._tasks.get(task_id)
            if task is None or task.state != TaskState.QUEUED:
                return
            task.state = TaskState.STARTING
            task.lease_id = lease_id
            task.updated_at = utcnow()
            self._leases[lease_id] = (task.task_id, session.worker_id)
            self._worker_load.setdefault(session.worker_id, {})[task.task_id] = (
                task.vcpus, task.memory_gb,
            )
        sent = await self.registry.send(session.worker_id, env)
        if not sent:
            await self._revert_assignment(
                task_id, lease_id, session.worker_id, "worker unreachable",
            )
            return
        try:
            # Worst-case cold boot can approach 10 min on first-run shapes;
            # batch heartbeat TTL is the upper bound.
            result = await self.coordinator.await_result(
                pending, timeout_s=self.host_config.batch_heartbeat_ttl_s,
            )
        except Exception as exc:
            await self._revert_assignment(
                task_id, lease_id, session.worker_id, f"assign timeout: {exc}",
            )
            return
        if not isinstance(result, TaskBound) or not result.ok:
            err = getattr(result, "error", None) or "assign failed"
            await self._revert_assignment(
                task_id, lease_id, session.worker_id, err,
            )
            return
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.state = TaskState.READY
            task.assignment = TaskAssignment(
                host_id=session.worker_id,
                lease_id=lease_id,
                slot_id=result.vm_id,
                snapshot_name=task.snapshot_name,
                urls=result.urls,
                novnc_url=result.novnc_url,
                lease_endpoint=result.lease_endpoint,
            )
            task.updated_at = utcnow()
            self._refresh_batch_states_locked()

    async def _pick_worker(self, task: TaskStatus) -> WorkerSession | None:
        """Capacity hard gate → cache affinity preference → least-loaded tiebreak.

        Returns None if no online worker can host the task. The task stays
        QUEUED and placement is retried on the next capacity-free event.
        """
        sessions = await self.registry.online_snapshot()
        image_version = self._image_version(task.snapshot_name)
        async with self._lock:
            candidates: list[tuple[WorkerSession, bool, int]] = []
            for session in sessions:
                free_cpu, free_mem = self._worker_free_capacity(session)
                if task.vcpus > free_cpu or task.memory_gb > free_mem:
                    continue
                cache_hit = session.has_cached_shape(
                    image_key=task.snapshot_name,
                    image_version=image_version or "",
                    vcpus=task.vcpus,
                    memory_gb=task.memory_gb,
                    disk_gb=task.disk_gb,
                )
                active = len(self._worker_load.get(session.worker_id, {}))
                candidates.append((session, cache_hit, active))
        if not candidates:
            return None
        candidates.sort(key=lambda t: (0 if t[1] else 1, t[2], t[0].worker_id))
        return candidates[0][0]

    def _worker_free_capacity(self, session: WorkerSession) -> tuple[int, int]:
        assigned = self._worker_load.get(session.worker_id, {})
        used_cpu = sum(v[0] for v in assigned.values())
        used_mem = sum(v[1] for v in assigned.values())
        cap = session.capacity
        free_cpu = max(cap.total_vcpus - cap.reserved_vcpus - used_cpu, 0)
        free_mem = max(cap.total_memory_gb - cap.reserved_memory_gb - used_mem, 0)
        return free_cpu, free_mem

    def _max_online_worker_capacity(self) -> tuple[int | None, int | None]:
        """Largest single-machine capacity across online workers.

        Used as the admission bound at submit time. Returns (None, None)
        if no workers are online; submission proceeds and tasks queue.
        """
        try:
            sessions = self.registry._sessions  # type: ignore[attr-defined]
        except AttributeError:
            sessions = {}
        max_cpu: int | None = None
        max_mem: int | None = None
        for session in sessions.values():
            if not session.online:
                continue
            cap = session.capacity
            allowable_cpu = cap.total_vcpus - cap.reserved_vcpus
            allowable_mem = cap.total_memory_gb - cap.reserved_memory_gb
            if max_cpu is None or allowable_cpu > max_cpu:
                max_cpu = allowable_cpu
            if max_mem is None or allowable_mem > max_mem:
                max_mem = allowable_mem
        return max_cpu, max_mem

    # ── helpers ────────────────────────────────────────────────────────

    async def _revert_assignment(
        self,
        task_id: str,
        lease_id: str,
        worker_id: str,
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
                task.metadata["retry_count"] = int(task.metadata.get("retry_count", 0)) + 1
            self._leases.pop(lease_id, None)
            self._worker_load.get(worker_id, {}).pop(task_id, None)
            self._refresh_batch_states_locked()
        # Try another worker.
        asyncio.create_task(self._try_place(task_id))

    def _image_version(self, snapshot_name: str) -> str | None:
        image = self.images.get(snapshot_name)
        if image is None:
            return None
        return image.version

    def _refresh_batch_states_locked(self) -> None:
        for batch in self._batches.values():
            batch.tasks = [
                self._tasks[t.task_id].model_copy(deep=True) for t in batch.tasks
            ]
            states = {t.state for t in batch.tasks}
            if states <= {TaskState.COMPLETED}:
                batch.state = BatchState.COMPLETED
            elif TaskState.FAILED in states and states <= {
                TaskState.COMPLETED, TaskState.FAILED,
            }:
                batch.state = BatchState.FAILED
            elif TaskState.QUEUED in states:
                batch.state = (
                    BatchState.QUEUED
                    if states == {TaskState.QUEUED}
                    else BatchState.RUNNING
                )
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

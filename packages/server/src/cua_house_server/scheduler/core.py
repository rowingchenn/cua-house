"""In-memory scheduler for cua-house-server."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

from cua_house_common.events import JsonlEventLogger
from cua_house_common.models import (
    BatchCreateRequest,
    BatchHeartbeatResponse,
    BatchState,
    BatchStatus,
    LeaseHeartbeatResponse,
    LeaseStageResponse,
    TaskAssignment,
    TaskState,
    TaskStatus,
    utcnow,
)
from cua_house_server.scheduler.models import (
    LeaseRecord,
    VMRecord,
    VMState,
)
from cua_house_server._internal.port_pool import PortPool
from cua_house_server.runtimes.qemu import DockerQemuRuntime, VMHandle
from cua_house_server.runtimes.gcp import GCPVMRuntime
from cua_house_server.config.loader import HostRuntimeConfig, ImageSpec

logger = logging.getLogger(__name__)


class EnvScheduler:
    """Single-host image-grouped scheduler."""

    def __init__(
        self,
        *,
        runtime: DockerQemuRuntime,
        host_config: HostRuntimeConfig,
        images: dict[str, ImageSpec],
        event_logger: JsonlEventLogger | None = None,
        runtimes: dict[str, DockerQemuRuntime | GCPVMRuntime] | None = None,
    ):
        self.runtime = runtime
        self.host_config = host_config
        self.images = images
        self._runtimes: dict[str, DockerQemuRuntime | GCPVMRuntime] = runtimes or {"local": runtime}
        self.event_logger = event_logger or JsonlEventLogger(
            host_config.runtime_root / "events.jsonl",
            component="env_server",
        )
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskStatus] = {}
        self._batches: dict[str, BatchStatus] = {}
        self._batch_expires_at: dict[str, Any] = {}
        self._leases: dict[str, LeaseRecord] = {}
        self._cua_ports = PortPool(*host_config.cua_port_range)
        self._novnc_ports = PortPool(*host_config.novnc_port_range)
        self._dispatch_task: asyncio.Task[None] | None = None
        self._lease_reaper_task: asyncio.Task[None] | None = None
        # VM pool state (snapshot-based persistent VMs for local runtime)
        self._vms: dict[str, VMRecord] = {}
        self._vm_handles: dict[str, VMHandle] = {}

    async def start(self) -> None:
        for rt in self._runtimes.values():
            rt.cleanup_orphaned_state()
        self.event_logger.emit(
            "server_started",
            host_id=self.host_config.host_id,
            host_external_ip=self.host_config.host_external_ip,
        )

        # Initialize VM pool for local runtime (snapshot-based pre-baked qcow2)
        local_rt = self._runtimes.get("local")
        if isinstance(local_rt, DockerQemuRuntime) and self.host_config.vm_pool:
            logger.info(
                "Initializing VM pool: %s",
                [(e.snapshot_name, e.count) for e in self.host_config.vm_pool],
            )
            handles = await local_rt.initialize_pool(
                self.host_config.vm_pool, self.images,
            )
            for handle in handles:
                vm = VMRecord(
                    vm_id=handle.vm_id,
                    snapshot_name=handle.snapshot_name,
                    state=VMState.READY,
                    cpu_cores=handle.cpu_cores,
                    memory_gb=handle.memory_gb,
                    container_name=handle.container_name,
                    cua_port=handle.cua_port,
                    novnc_port=handle.novnc_port,
                    qmp_port=0,
                )
                self._vms[vm.vm_id] = vm
                self._vm_handles[vm.vm_id] = handle
            logger.info("VM pool ready: %d VMs", len(self._vms))

        if self._lease_reaper_task is None:
            self._lease_reaper_task = asyncio.create_task(self._lease_reaper_loop())

    async def shutdown(self) -> None:
        if self._lease_reaper_task is not None:
            self._lease_reaper_task.cancel()
            await asyncio.gather(self._lease_reaper_task, return_exceptions=True)

    async def submit_batch(self, request: BatchCreateRequest) -> BatchStatus:
        created = utcnow()
        batch_id = request.batch_id or str(uuid4())
        async with self._lock:
            if batch_id in self._batches:
                raise ValueError(f"batch_id already exists: {batch_id}")
            tasks: list[TaskStatus] = []
            for req in request.tasks:
                # Resolve cpu/mem from vm_pool entry (snapshot_name must match a pool entry)
                pool_entry = self._resolve_pool_entry(req.snapshot_name)
                cpu_cores = pool_entry.cpu_cores
                memory_gb = pool_entry.memory_gb
                task = TaskStatus(
                    task_id=req.task_id,
                    task_path=req.task_path,
                    os_type=req.os_type,
                    snapshot_name=req.snapshot_name,
                    cpu_cores=cpu_cores,
                    memory_gb=memory_gb,
                    metadata=req.metadata,
                    task_data=req.task_data,
                    state=TaskState.QUEUED,
                    batch_id=batch_id,
                    created_at=created,
                    updated_at=created,
                )
                if not self._fits_host_total(cpu_cores, memory_gb):
                    task.state = TaskState.FAILED
                    task.error = (
                        f"task requires {cpu_cores} vCPU / {memory_gb} GiB, exceeding host allocatable capacity "
                        f"of {self._max_allocatable_cpu()} vCPU / {self._max_allocatable_memory_gb()} GiB"
                    )
                    task.completed_at = created
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
            self._batch_expires_at[batch_id] = batch.expires_at
            self.event_logger.emit(
                "batch_submitted",
                batch_id=batch_id,
                task_count=len(tasks),
            )
            for task in tasks:
                if task.state == TaskState.FAILED:
                    self.event_logger.emit(
                        "task_capacity_rejected",
                        batch_id=batch_id,
                        task_id=task.task_id,
                        os_type=task.os_type,
                        snapshot_name=task.snapshot_name,
                        cpu_cores=task.cpu_cores,
                        memory_gb=task.memory_gb,
                        error=task.error,
                    )
                else:
                    self.event_logger.emit(
                        "task_queued",
                        batch_id=batch_id,
                        task_id=task.task_id,
                        os_type=task.os_type,
                        snapshot_name=task.snapshot_name,
                        cpu_cores=task.cpu_cores,
                        memory_gb=task.memory_gb,
                    )
        self._ensure_dispatch()
        return await self.get_batch(batch_id)

    async def get_batch(self, batch_id: str) -> BatchStatus:
        async with self._lock:
            batch = self._batches[batch_id]
            return self._snapshot_batch(batch)

    async def get_task(self, task_id: str) -> TaskStatus:
        async with self._lock:
            return self._tasks[task_id].model_copy(deep=True)

    async def heartbeat_batch(self, batch_id: str) -> BatchHeartbeatResponse:
        async with self._lock:
            batch = self._batches[batch_id]
            batch.expires_at = utcnow() + timedelta(seconds=self.host_config.batch_heartbeat_ttl_s)
            batch.updated_at = utcnow()
            self._batch_expires_at[batch_id] = batch.expires_at
            return BatchHeartbeatResponse(batch_id=batch_id, expires_at=batch.expires_at)

    async def cancel_batch(self, batch_id: str, *, reason: str, details: dict[str, Any] | None = None) -> BatchStatus:
        details = details or {}
        lease_ids: list[str] = []

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
                    if details:
                        task.metadata["cancellation_details"] = details
                elif task.state in {TaskState.READY, TaskState.LEASED} and task.lease_id is not None:
                    lease_ids.append(task.lease_id)
            self._refresh_batch_states_locked()
            self._batch_expires_at.pop(batch_id, None)
            self.event_logger.emit(
                "batch_cancelled",
                batch_id=batch_id,
                reason=reason,
                details=details,
            )

        for lease_id in lease_ids:
            try:
                await self.complete(lease_id, final_status="abandoned", details={"reason": reason, **details})
            except KeyError:
                logger.warning("Lease %s disappeared while cancelling batch %s", lease_id, batch_id)
        return await self.get_batch(batch_id)

    async def heartbeat(self, lease_id: str) -> LeaseHeartbeatResponse:
        async with self._lock:
            lease = self._leases[lease_id]
            lease.expires_at = utcnow() + timedelta(seconds=self.host_config.heartbeat_ttl_s)
            task = self._tasks[lease.task_id]
            if task.state == TaskState.READY:
                task.state = TaskState.LEASED
                task.updated_at = utcnow()
            return LeaseHeartbeatResponse(
                lease_id=lease.lease_id,
                task_id=lease.task_id,
                expires_at=lease.expires_at,
            )

    async def complete(self, lease_id: str, *, final_status: str, details: dict[str, Any] | None = None) -> TaskStatus:
        details = details or {}
        task_id: str | None = None
        already_completing = False
        should_reset = False
        async with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise KeyError(f"unknown lease_id: {lease_id}")
            task = self._tasks[lease.task_id]
            task_id = task.task_id
            if task.state in {TaskState.RESETTING, TaskState.COMPLETED, TaskState.FAILED}:
                # Another caller (reaper or duplicate client call) already started
                # completing this lease. Return current status without error — the
                # reset is in progress and will finish on its own.
                already_completing = True
            else:
                task.state = TaskState.RESETTING
                task.updated_at = utcnow()
                should_reset = True
                vm = self._vms[lease.slot_id]
                vm.state = VMState.REVERTING
                vm.last_used_at = utcnow()
                lease.final_status = final_status
                self.event_logger.emit(
                    "lease_complete_requested",
                    lease_id=lease_id,
                    task_id=task.task_id,
                    vm_id=vm.vm_id,
                    final_status=final_status,
                    details=details,
                )
        assert task_id is not None
        if should_reset:
            asyncio.create_task(self._release_after_reset(lease_id, final_status, details))
        return await self.get_task(task_id)

    async def stage_runtime(self, lease_id: str) -> LeaseStageResponse:
        return await self._stage_phase(lease_id, phase="runtime")

    async def stage_eval(self, lease_id: str) -> LeaseStageResponse:
        return await self._stage_phase(lease_id, phase="eval")

    async def _stage_phase(self, lease_id: str, *, phase: str) -> LeaseStageResponse:
        async with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise KeyError(f"unknown lease_id: {lease_id}")
            task = self._tasks[lease.task_id]
            if task.state not in {TaskState.READY, TaskState.LEASED}:
                raise RuntimeError(f"lease {lease_id} is not stageable in state {task.state.value}")
            task_data = task.task_data
            vm_handle = self._vm_handles[lease.slot_id]

        result = await self.runtime.stage_task_phase(
            handle=vm_handle,
            task_id=task.task_id,
            lease_id=lease_id,
            task_data=task_data,
            phase=phase,
            container_name=vm_handle.container_name,
        )
        return LeaseStageResponse(
            lease_id=lease_id,
            task_id=task.task_id,
            phase=phase,
            skipped=result.skipped,
            file_count=result.file_count,
            bytes_staged=result.bytes_staged,
        )

    async def _release_after_reset(self, lease_id: str, final_status: str, details: dict[str, Any]) -> None:
        async with self._lock:
            lease = self._leases[lease_id]
            task = self._tasks[lease.task_id]
            vm = self._vms[lease.slot_id]
            vm_handle = self._vm_handles[lease.slot_id]
            vm.state = VMState.REVERTING

        reset_started = asyncio.get_running_loop().time()
        new_state = TaskState.COMPLETED if final_status == "completed" else TaskState.FAILED

        try:
            await self.runtime.revert_vm(vm_handle)
            async with self._lock:
                task.state = new_state
                task.updated_at = utcnow()
                task.completed_at = utcnow()
                if details:
                    task.metadata["completion_details"] = details
                vm.state = VMState.READY
                vm.task_id = None
                vm.lease_id = None
                vm.last_used_at = utcnow()
                del self._leases[lease_id]
                self.event_logger.emit(
                    "task_finished",
                    task_id=task.task_id,
                    batch_id=task.batch_id,
                    lease_id=lease_id,
                    vm_id=vm.vm_id,
                    final_status=new_state.value,
                    revert_s=asyncio.get_running_loop().time() - reset_started,
                )
        except Exception as exc:
            logger.exception("Failed to revert VM %s for lease %s", vm.vm_id, lease_id)
            async with self._lock:
                task.state = TaskState.FAILED
                task.error = f"revert failed: {exc}"
                task.updated_at = utcnow()
                task.completed_at = utcnow()
                vm.state = VMState.BROKEN
                del self._leases[lease_id]
                self.event_logger.emit(
                    "vm_revert_failed",
                    vm_id=vm.vm_id,
                    task_id=task.task_id,
                    lease_id=lease_id,
                    error=str(exc),
                )
            asyncio.create_task(self._auto_replace_vm(vm.vm_id))
        self._ensure_dispatch()

    async def _auto_replace_vm(self, vm_id: str) -> None:
        """Replace a broken VM with a fresh one (cold boot + snapshot)."""
        try:
            async with self._lock:
                vm = self._vms.get(vm_id)
                if vm is None or vm.state != VMState.BROKEN:
                    return
                handle = self._vm_handles[vm_id]
                image = self._resolve_image(vm.snapshot_name)

            new_handle = await self.runtime.replace_broken_vm(handle, image)
            async with self._lock:
                vm = self._vms[vm_id]
                vm.state = VMState.READY
                self._vm_handles[vm_id] = new_handle
                self.event_logger.emit(
                    "vm_replaced",
                    vm_id=vm_id,
                    container_name=new_handle.container_name,
                )
        except Exception as exc:
            logger.exception("Failed to replace broken VM %s", vm_id)
            self.event_logger.emit(
                "vm_replace_failed",
                vm_id=vm_id,
                error=str(exc),
            )

    def _ensure_dispatch(self) -> None:
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def _dispatch_loop(self) -> None:
        while True:
            async with self._lock:
                candidate = self._pick_next_task_locked()
                if candidate is None:
                    self._refresh_batch_states_locked()
                    return

                vm = self._find_free_vm_locked(candidate.snapshot_name)
                if vm is None:
                    self._refresh_batch_states_locked()
                    return  # all VMs busy

                # Assign task → VM immediately (VM already running, ready in pool)
                vm.state = VMState.LEASED
                vm.task_id = candidate.task_id
                vm.last_used_at = utcnow()
                candidate.state = TaskState.STARTING
                candidate.updated_at = utcnow()

                lease = LeaseRecord(
                    task_id=candidate.task_id,
                    slot_id=vm.vm_id,
                    expires_at=utcnow() + timedelta(seconds=self.host_config.heartbeat_ttl_s),
                )
                self._leases[lease.lease_id] = lease
                vm.lease_id = lease.lease_id

                assignment = TaskAssignment(
                    host_id=self.host_config.host_id,
                    lease_id=lease.lease_id,
                    slot_id=vm.vm_id,
                    snapshot_name=candidate.snapshot_name,
                )
                candidate.lease_id = lease.lease_id
                candidate.assignment = assignment
                candidate.state = TaskState.READY
                candidate.updated_at = utcnow()

                self._refresh_batch_states_locked()
                self.event_logger.emit(
                    "task_ready",
                    batch_id=candidate.batch_id,
                    task_id=candidate.task_id,
                    slot_id=vm.vm_id,
                    lease_id=lease.lease_id,
                    snapshot_name=candidate.snapshot_name,
                    queue_wait_s=(utcnow() - candidate.created_at).total_seconds(),
                )
                # continue to dispatch more tasks if possible

    async def _lease_reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            await self.reap_expired_leases_once()
            await self.reap_expired_batches_once()

    async def reap_expired_leases_once(self) -> None:
        expired: list[str] = []
        async with self._lock:
            now = utcnow()
            for lease_id, lease in self._leases.items():
                if lease.expires_at <= now:
                    expired.append(lease_id)
        for lease_id in expired:
            try:
                await self.complete(lease_id, final_status="abandoned", details={"reason": "lease expired"})
            except Exception:
                logger.exception("Failed to abandon expired lease %s", lease_id)

    async def reap_expired_batches_once(self) -> None:
        expired: list[str] = []
        async with self._lock:
            now = utcnow()
            for batch_id, expires_at in self._batch_expires_at.items():
                if expires_at is not None and expires_at <= now:
                    expired.append(batch_id)
        for batch_id in expired:
            try:
                await self.cancel_batch(batch_id, reason="batch heartbeat expired")
            except Exception:
                logger.exception("Failed to cancel expired batch %s", batch_id)

    def _pick_next_task_locked(self) -> TaskStatus | None:
        queued = [task for task in self._tasks.values() if task.state == TaskState.QUEUED]
        if not queued:
            return None
        return min(queued, key=lambda t: t.created_at)

    def _resolve_pool_entry(self, snapshot_name: str) -> "VMPoolEntry":
        from cua_house_common.models import VMPoolEntry
        for entry in self.host_config.vm_pool:
            if entry.snapshot_name == snapshot_name:
                return entry
        raise ValueError(f"unknown snapshot_name: {snapshot_name}")

    def _resolve_image(self, image_key: str) -> ImageSpec:
        image = self.images.get(image_key)
        if image is None:
            raise ValueError(f"unknown image_key: {image_key}")
        if not image.enabled:
            raise ValueError(f"image not enabled in this deployment: {image_key}")
        return image

    def _fits_host_total(self, cpu_cores: int, memory_gb: int) -> bool:
        return cpu_cores <= self._max_allocatable_cpu() and memory_gb <= self._max_allocatable_memory_gb()

    def _max_allocatable_cpu(self) -> int:
        return max((os_cpu_count() - self.host_config.host_reserved_cpu_cores), 0)

    def _max_allocatable_memory_gb(self) -> int:
        return max((host_memory_gb() - self.host_config.host_reserved_memory_gb), 0)

    def _find_free_vm_locked(self, snapshot_name: str) -> VMRecord | None:
        """Find a READY VM matching the snapshot name."""
        for vm in self._vms.values():
            if vm.state == VMState.READY and vm.snapshot_name == snapshot_name:
                return vm
        return None

    def _refresh_batch_states_locked(self) -> None:
        for batch in self._batches.values():
            batch.tasks = [self._tasks[task.task_id].model_copy(deep=True) for task in batch.tasks]
            states = {task.state for task in batch.tasks}
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

    async def resolve_proxy_targets(self, lease_id: str) -> tuple[str, str]:
        async with self._lock:
            lease = self._leases[lease_id]
            vm_handle = self._vm_handles[lease.slot_id]
            return self.runtime.vm_cua_local_url(vm_handle), self.runtime.vm_novnc_local_url(vm_handle)


def os_cpu_count() -> int:
    return max((__import__("os").cpu_count() or 1), 1)


def host_memory_gb() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().total / (1024 ** 3))
    except Exception:  # pragma: no cover - psutil may be absent
        return 64

"""In-memory scheduler for agenthle-env-server."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

from cua_house.common.events import JsonlEventLogger

from .models import (
    BatchCreateRequest,
    BatchHeartbeatResponse,
    BatchState,
    BatchStatus,
    LeaseStageResponse,
    LeaseHeartbeatResponse,
    LeaseRecord,
    SlotRecord,
    SlotState,
    TaskAssignment,
    TaskState,
    TaskStatus,
    VMRecord,
    VMState,
    utcnow,
)
from .port_pool import PortPool
from .runtime import DockerQemuRuntime, GCPSlotHandle, GCPVMRuntime, HostRuntimeConfig, ImageSpec, SlotHandle, VMHandle

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
        self._slots: dict[str, SlotRecord] = {}
        self._slot_handles: dict[str, SlotHandle] = {}
        self._cua_ports = PortPool(*host_config.cua_port_range)
        self._novnc_ports = PortPool(*host_config.novnc_port_range)
        self._dispatch_task: asyncio.Task[None] | None = None
        self._lease_reaper_task: asyncio.Task[None] | None = None
        self._startup_tasks: set[asyncio.Task[None]] = set()
        self._startup_tasks_by_task_id: dict[str, asyncio.Task[None]] = {}
        # VM pool state (snapshot-based persistent VMs for local runtime)
        self._vms: dict[str, VMRecord] = {}
        self._vm_handles: dict[str, VMHandle] = {}
        self._use_vm_pool = bool(host_config.vm_pool)

    def _runtime_for(self, image_key: str) -> DockerQemuRuntime | GCPVMRuntime:
        image = self._resolve_image(image_key)
        return self._runtimes.get(image.runtime_mode, self.runtime)

    async def start(self) -> None:
        for rt in self._runtimes.values():
            rt.cleanup_orphaned_state()
        self.event_logger.emit(
            "server_started",
            host_id=self.host_config.host_id,
            host_external_ip=self.host_config.host_external_ip,
        )

        # Initialize VM pool for local runtime (snapshot-based)
        if self._use_vm_pool:
            local_rt = self._runtimes.get("local")
            if isinstance(local_rt, DockerQemuRuntime):
                logger.info(
                    "Initializing VM pool: %s",
                    [(e.image_key, e.count) for e in self.host_config.vm_pool],
                )
                handles = await local_rt.initialize_pool(
                    self.host_config.vm_pool, self.images,
                )
                for handle in handles:
                    vm = VMRecord(
                        vm_id=handle.vm_id,
                        image_key=handle.image_key,
                        state=VMState.READY,
                        cpu_cores=handle.cpu_cores,
                        memory_gb=handle.memory_gb,
                        container_name=handle.container_name,
                        cua_port=handle.cua_port,
                        novnc_port=handle.novnc_port,
                        qmp_port=0,
                        snapshot_name=handle.snapshot_name,
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
        for task in list(self._startup_tasks):
            task.cancel()
        if self._startup_tasks:
            await asyncio.gather(*self._startup_tasks, return_exceptions=True)

    async def submit_batch(self, request: BatchCreateRequest) -> BatchStatus:
        created = utcnow()
        batch_id = request.batch_id or str(uuid4())
        async with self._lock:
            if batch_id in self._batches:
                raise ValueError(f"batch_id already exists: {batch_id}")
            tasks: list[TaskStatus] = []
            for req in request.tasks:
                image = self._resolve_image(req.image_key)
                cpu_cores = req.cpu_cores or image.default_cpu_cores
                memory_gb = req.memory_gb or image.default_memory_gb
                task = TaskStatus(
                    task_id=req.task_id,
                    task_path=req.task_path,
                    os_type=req.os_type,
                    image_key=req.image_key,
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
                elif req.task_data is not None:
                    try:
                        self._runtime_for(req.image_key).validate_runtime_task_data(task_id=req.task_id, task_data=req.task_data)
                    except Exception as exc:
                        task.state = TaskState.FAILED
                        task.error = str(exc)
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
                        image_key=task.image_key,
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
                        image_key=task.image_key,
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
        starting_task_ids: list[str] = []

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
                elif task.state == TaskState.STARTING:
                    task.state = TaskState.RESETTING
                    task.updated_at = now
                    starting_task_ids.append(task.task_id)
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

        for task_id in starting_task_ids:
            await self._abort_starting_task(task_id, reason=reason, details=details)
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
        async with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise KeyError(f"unknown lease_id: {lease_id}")
            task = self._tasks[lease.task_id]
            task.state = TaskState.RESETTING
            task.updated_at = utcnow()
            slot = self._slots[lease.slot_id]
            slot.state = SlotState.RESETTING
            slot.last_used_at = utcnow()
            lease.final_status = final_status
            self.event_logger.emit(
                "lease_complete_requested",
                lease_id=lease_id,
                task_id=task.task_id,
                slot_id=slot.slot_id,
                final_status=final_status,
                details=details,
            )
        asyncio.create_task(self._release_after_reset(lease_id, final_status, details))
        return await self.get_task(task.task_id)

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
            # Resolve handle: VM pool or slot
            vm_handle = self._vm_handles.get(lease.slot_id)
            slot_handle = self._slot_handles.get(lease.slot_id) if vm_handle is None else None

        if vm_handle is not None:
            # VM pool: stage via local runtime with VM handle
            result = await self.runtime.stage_task_phase(
                handle=vm_handle,
                task_id=task.task_id,
                lease_id=lease_id,
                task_data=task_data,
                phase=phase,
                container_name=vm_handle.container_name,
            )
        else:
            assert slot_handle is not None
            result = await self._runtime_for(task.image_key).stage_task_phase(
                handle=slot_handle,
                task_id=task.task_id,
                lease_id=lease_id,
                task_data=task_data,
                phase=phase,
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
        # Determine whether this is a VM-pool lease or a slot-based lease
        vm: VMRecord | None = None
        vm_handle: VMHandle | None = None
        slot: SlotRecord | None = None
        handle: SlotHandle | GCPSlotHandle | None = None

        async with self._lock:
            lease = self._leases[lease_id]
            task = self._tasks[lease.task_id]
            image = self._resolve_image(task.image_key)

            if lease.slot_id in self._vms:
                # VM pool path
                vm = self._vms[lease.slot_id]
                vm_handle = self._vm_handles[lease.slot_id]
                vm.state = VMState.REVERTING
            else:
                # Slot path (GCP or legacy local)
                slot = self._slots[lease.slot_id]
                handle = self._slot_handles[slot.slot_id]

        reset_started = asyncio.get_running_loop().time()
        new_state = TaskState.COMPLETED if final_status == "completed" else TaskState.FAILED

        if vm is not None and vm_handle is not None:
            # ── VM pool: snapshot revert ──────────────────────────
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
                        vm_pool=True,
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
                # Auto-replace broken VM
                asyncio.create_task(self._auto_replace_vm(vm.vm_id))
        else:
            # ── Slot path: existing destroy+rebuild logic ─────────
            assert slot is not None and handle is not None
            try:
                await self._runtime_for(task.image_key).reset_slot(handle, image)
                async with self._lock:
                    task.state = new_state
                    task.updated_at = utcnow()
                    task.completed_at = utcnow()
                    if details:
                        task.metadata["completion_details"] = details
                    slot.state = SlotState.EMPTY
                    slot.task_id = None
                    slot.lease_id = None
                    slot.cua_port = None
                    slot.novnc_port = None
                    slot.last_used_at = utcnow()
                    if image.runtime_mode == "local":
                        self._cua_ports.release(handle.cua_port)
                        self._novnc_ports.release(handle.novnc_port)
                    del self._leases[lease_id]
                    self.event_logger.emit(
                        "task_finished",
                        task_id=task.task_id,
                        batch_id=task.batch_id,
                        lease_id=lease_id,
                        slot_id=slot.slot_id,
                        final_status=new_state.value,
                        reset_s=asyncio.get_running_loop().time() - reset_started,
                    )
            except Exception as exc:
                logger.exception("Failed to reset slot for lease %s", lease_id)
                async with self._lock:
                    task.state = TaskState.FAILED
                    task.error = f"reset failed: {exc}"
                    task.updated_at = utcnow()
                    slot.state = SlotState.BROKEN
                    self.event_logger.emit(
                        "slot_reset_failed",
                        slot_id=slot.slot_id,
                        task_id=task.task_id,
                        lease_id=lease_id,
                        error=str(exc),
                    )
        self._ensure_dispatch()

    async def _auto_replace_vm(self, vm_id: str) -> None:
        """Replace a broken VM with a fresh one (cold boot + snapshot)."""
        try:
            async with self._lock:
                vm = self._vms.get(vm_id)
                if vm is None or vm.state != VMState.BROKEN:
                    return
                handle = self._vm_handles[vm_id]
                image = self._resolve_image(vm.image_key)

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

                image = self._resolve_image(candidate.image_key)

                # ── VM pool path (local with snapshot-based VMs) ─────
                if self._use_vm_pool and image.runtime_mode == "local":
                    vm = self._find_free_vm_locked(candidate.image_key)
                    if vm is None:
                        self._refresh_batch_states_locked()
                        return  # all VMs busy

                    # Assign task → VM immediately (no cold boot needed)
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
                        image_key=candidate.image_key,
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
                        image_key=candidate.image_key,
                        vm_pool=True,
                        queue_wait_s=(utcnow() - candidate.created_at).total_seconds(),
                    )
                    continue  # try dispatching more tasks

                # ── Slot path (GCP or legacy local without VM pool) ──
                if not self._has_capacity_locked(candidate.cpu_cores, candidate.memory_gb, candidate.image_key):
                    self._refresh_batch_states_locked()
                    return

                slot = self._reuse_or_create_slot_locked(candidate)
                candidate.state = TaskState.STARTING
                candidate.updated_at = utcnow()
                slot.state = SlotState.STARTING
                slot.task_id = candidate.task_id
                slot.last_used_at = utcnow()

                if image.runtime_mode == "local":
                    slot.cua_port = self._cua_ports.allocate()
                    slot.novnc_port = self._novnc_ports.allocate()
                else:
                    slot.cua_port = 5000
                    slot.novnc_port = 0
                handle = self._runtime_for(candidate.image_key).prepare_slot(
                    slot_id=slot.slot_id,
                    image=self._resolve_image(candidate.image_key),
                    cpu_cores=candidate.cpu_cores,
                    memory_gb=candidate.memory_gb,
                    cua_port=slot.cua_port,
                    novnc_port=slot.novnc_port,
                    lease_id="pending",
                    task_id=candidate.task_id,
                    task_data=candidate.task_data,
                )
                self._slot_handles[slot.slot_id] = handle
                self._refresh_batch_states_locked()
                self.event_logger.emit(
                    "task_starting",
                    batch_id=candidate.batch_id,
                    task_id=candidate.task_id,
                    slot_id=slot.slot_id,
                    image_key=candidate.image_key,
                    cpu_cores=candidate.cpu_cores,
                    memory_gb=candidate.memory_gb,
                    queue_wait_s=(utcnow() - candidate.created_at).total_seconds(),
                    cua_port=slot.cua_port,
                    novnc_port=slot.novnc_port,
                )
                planned = (candidate.model_copy(deep=True), slot.model_copy(deep=True), handle)

            # Outside lock: start the slot-based task
            task_snapshot, slot_snapshot, handle = planned
            startup_task = asyncio.create_task(self._start_planned_task(task_snapshot, slot_snapshot, handle))
            self._startup_tasks.add(startup_task)
            self._startup_tasks_by_task_id[task_snapshot.task_id] = startup_task
            startup_task.add_done_callback(self._startup_tasks.discard)
            startup_task.add_done_callback(lambda _: self._startup_tasks_by_task_id.pop(task_snapshot.task_id, None))

    async def _start_planned_task(self, task_snapshot: TaskStatus, slot_snapshot: SlotRecord, handle: SlotHandle | GCPSlotHandle) -> None:
        try:
            await self._runtime_for(task_snapshot.image_key).start_slot(handle)
            lease = LeaseRecord(
                task_id=task_snapshot.task_id,
                slot_id=slot_snapshot.slot_id,
                expires_at=utcnow() + timedelta(seconds=self.host_config.heartbeat_ttl_s),
            )
            assignment = TaskAssignment(
                host_id=self.host_config.host_id,
                lease_id=lease.lease_id,
                slot_id=slot_snapshot.slot_id,
                image_key=task_snapshot.image_key,
            )
            async with self._lock:
                task = self._tasks[task_snapshot.task_id]
                slot = self._slots[slot_snapshot.slot_id]
                self._leases[lease.lease_id] = lease
                task.state = TaskState.READY
                task.updated_at = utcnow()
                task.lease_id = lease.lease_id
                task.assignment = assignment
                slot.state = SlotState.READY
                slot.lease_id = lease.lease_id
                self._refresh_batch_states_locked()
                self.event_logger.emit(
                    "task_ready",
                    batch_id=task.batch_id,
                    task_id=task.task_id,
                    slot_id=slot.slot_id,
                    lease_id=lease.lease_id,
                    image_key=task.image_key,
                    total_queue_to_ready_s=(utcnow() - task.created_at).total_seconds(),
                )
        except asyncio.CancelledError:
            logger.info("Startup cancelled for task %s", task_snapshot.task_id)
            return
        except Exception as exc:
            logger.exception("Failed to start task %s", task_snapshot.task_id)
            rt = self._runtime_for(task_snapshot.image_key)
            if isinstance(rt, DockerQemuRuntime):
                rt._run(["docker", "rm", "-f", handle.container_name], check=False)
            elif isinstance(rt, GCPVMRuntime) and isinstance(handle, GCPSlotHandle):
                rt.destroy_vm(handle)
            async with self._lock:
                task = self._tasks[task_snapshot.task_id]
                slot = self._slots[slot_snapshot.slot_id]
                task.state = TaskState.FAILED
                task.error = str(exc)
                task.updated_at = utcnow()
                slot.state = SlotState.BROKEN
                image = self.images.get(task_snapshot.image_key)
                if image and image.runtime_mode == "local":
                    if slot.cua_port is not None:
                        self._cua_ports.release(slot.cua_port)
                    if slot.novnc_port is not None:
                        self._novnc_ports.release(slot.novnc_port)
                self._refresh_batch_states_locked()
                self.event_logger.emit(
                    "task_start_failed",
                    batch_id=task.batch_id,
                    task_id=task.task_id,
                    slot_id=slot.slot_id,
                    error=str(exc),
                )
        self._ensure_dispatch()

    async def _abort_starting_task(self, task_id: str, *, reason: str, details: dict[str, Any]) -> None:
        async with self._lock:
            task = self._tasks[task_id]
            slot = next((slot for slot in self._slots.values() if slot.task_id == task_id), None)
            if slot is None:
                task.state = TaskState.FAILED
                task.error = reason
                task.completed_at = utcnow()
                task.updated_at = utcnow()
                return
            handle = self._slot_handles[slot.slot_id]
            image = self._resolve_image(task.image_key)
            startup_task = self._startup_tasks_by_task_id.get(task_id)

        if startup_task is not None:
            startup_task.cancel()
            await asyncio.gather(startup_task, return_exceptions=True)

        rt = self._runtime_for(task.image_key)
        if isinstance(rt, DockerQemuRuntime):
            rt._run(["docker", "rm", "-f", handle.container_name], check=False)
        elif isinstance(rt, GCPVMRuntime) and isinstance(handle, GCPSlotHandle):
            rt.destroy_vm(handle)
        await rt.reset_slot(handle, image)

        async with self._lock:
            task = self._tasks[task_id]
            slot = self._slots[slot.slot_id]
            task.state = TaskState.FAILED
            task.error = reason
            task.updated_at = utcnow()
            task.completed_at = utcnow()
            if details:
                task.metadata["cancellation_details"] = details
            slot.state = SlotState.EMPTY
            slot.task_id = None
            slot.lease_id = None
            slot.cua_port = None
            slot.novnc_port = None
            slot.last_used_at = utcnow()
            if image.runtime_mode == "local":
                self._cua_ports.release(handle.cua_port)
                self._novnc_ports.release(handle.novnc_port)
            self._refresh_batch_states_locked()
            self.event_logger.emit(
                "task_start_aborted",
                batch_id=task.batch_id,
                task_id=task.task_id,
                slot_id=slot.slot_id,
                reason=reason,
                details=details,
            )
        self._ensure_dispatch()

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
        groups: dict[str, list[TaskStatus]] = {}
        for task in queued:
            groups.setdefault(task.image_key, []).append(task)

        ordered_images = sorted(
            groups.items(),
            key=lambda item: (
                -self._idle_slot_count_for_image_locked(item[0]),
                -len(item[1]),
                min(task.cpu_cores for task in item[1]),
                min(task.memory_gb for task in item[1]),
                min(task.created_at for task in item[1]),
            ),
        )

        for image_key, tasks in ordered_images:
            for task in sorted(tasks, key=lambda candidate: (candidate.cpu_cores, candidate.memory_gb, candidate.created_at)):
                if self._has_capacity_locked(task.cpu_cores, task.memory_gb, task.image_key):
                    return task
        return None

    def _resolve_image(self, image_key: str) -> ImageSpec:
        image = self.images.get(image_key)
        if image is None:
            raise ValueError(f"unknown image_key: {image_key}")
        if not image.enabled:
            raise ValueError(f"image not enabled in this deployment: {image_key}")
        return image

    def _has_capacity_locked(self, cpu_cores: int, memory_gb: int, image_key: str | None = None) -> bool:
        image = self._resolve_image(image_key) if image_key else None
        if image and image.runtime_mode == "gcp":
            active = sum(
                1 for s in self._slots.values()
                if s.image_key == image_key
                and s.state in {SlotState.STARTING, SlotState.READY, SlotState.LEASED, SlotState.RESETTING}
            )
            return active < image.max_concurrent_vms
        # local mode: check host CPU/mem
        used_cpu = 0
        used_mem = 0
        for slot in self._slots.values():
            if slot.state in {SlotState.STARTING, SlotState.READY, SlotState.LEASED, SlotState.RESETTING}:
                if not image or self.images.get(slot.image_key, image).runtime_mode == "local":
                    used_cpu += slot.cpu_cores
                    used_mem += slot.memory_gb
        total_cpu = max((os_cpu_count() - self.host_config.host_reserved_cpu_cores), 0)
        total_mem = max((host_memory_gb() - self.host_config.host_reserved_memory_gb), 0)
        return used_cpu + cpu_cores <= total_cpu and used_mem + memory_gb <= total_mem

    def _fits_host_total(self, cpu_cores: int, memory_gb: int) -> bool:
        return cpu_cores <= self._max_allocatable_cpu() and memory_gb <= self._max_allocatable_memory_gb()

    def _max_allocatable_cpu(self) -> int:
        return max((os_cpu_count() - self.host_config.host_reserved_cpu_cores), 0)

    def _max_allocatable_memory_gb(self) -> int:
        return max((host_memory_gb() - self.host_config.host_reserved_memory_gb), 0)

    def _reuse_or_create_slot_locked(self, task: TaskStatus) -> SlotRecord:
        for slot in self._slots.values():
            if (
                slot.state == SlotState.EMPTY
                and slot.image_key == task.image_key
                and slot.cpu_cores == task.cpu_cores
                and slot.memory_gb == task.memory_gb
            ):
                return slot

        slot = SlotRecord(
            slot_id=str(uuid4()),
            image_key=task.image_key,
            state=SlotState.EMPTY,
            cpu_cores=task.cpu_cores,
            memory_gb=task.memory_gb,
        )
        self._slots[slot.slot_id] = slot
        return slot

    def _find_free_vm_locked(self, image_key: str) -> VMRecord | None:
        """Find a READY VM matching the image key."""
        for vm in self._vms.values():
            if vm.state == VMState.READY and vm.image_key == image_key:
                return vm
        return None

    def _idle_slot_count_for_image_locked(self, image_key: str) -> int:
        return sum(
            1
            for slot in self._slots.values()
            if slot.state == SlotState.EMPTY and slot.image_key == image_key
        )

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
            task = self._tasks[lease.task_id]
            # VM pool path
            if lease.slot_id in self._vm_handles:
                vm_handle = self._vm_handles[lease.slot_id]
                return self.runtime.vm_cua_local_url(vm_handle), self.runtime.vm_novnc_local_url(vm_handle)
            # Slot path
            handle = self._slot_handles[lease.slot_id]
            rt = self._runtime_for(task.image_key)
            return rt.cua_local_url(handle), rt.novnc_local_url(handle)


def os_cpu_count() -> int:
    return max((__import__("os").cpu_count() or 1), 1)


def host_memory_gb() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().total / (1024 ** 3))
    except Exception:  # pragma: no cover - psutil may be absent
        return 64

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
from cua_house_server.runtimes.gcp import GCPVMRuntime, GCPSlotHandle
from cua_house_server.config.loader import HostRuntimeConfig, ImageSpec

logger = logging.getLogger(__name__)


class EnvScheduler:
    """Single-host image-grouped scheduler with local pool + GCP on-demand."""

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
        self._published_port_pool = PortPool(*host_config.published_port_range)
        self._novnc_ports = PortPool(*host_config.novnc_port_range)
        self._dispatch_task: asyncio.Task[None] | None = None
        self._lease_reaper_task: asyncio.Task[None] | None = None
        # VM pool state (snapshot-based persistent VMs for local runtime)
        self._vms: dict[str, VMRecord] = {}
        self._vm_handles: dict[str, VMHandle] = {}
        # GCP on-demand handles (keyed by lease_id)
        self._gcp_handles: dict[str, GCPSlotHandle] = {}
        # Track which lease uses which runtime mode
        self._lease_runtime: dict[str, str] = {}  # lease_id -> "local" | "gcp"
        # Optional async callback invoked whenever a task reaches a terminal
        # state (COMPLETED / FAILED). Cluster-mode worker uses this to fire
        # a TaskCompleted message up to master over WS.
        self.task_finalized_callback: "Any | None" = None

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
                    vcpus=handle.vcpus,
                    memory_gb=handle.memory_gb,
                    disk_gb=handle.disk_gb,
                    container_name=handle.container_name,
                    published_ports=handle.published_ports,
                    novnc_port=handle.novnc_port,
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

    # ------------------------------------------------------------------
    # Batch submission
    # ------------------------------------------------------------------

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
                    req.snapshot_name,
                    vcpus=req.vcpus,
                    memory_gb=req.memory_gb,
                    disk_gb=req.disk_gb,
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
                # Only check host capacity for local mode
                image = self.images.get(req.snapshot_name)
                if image and image.local and "local" in self._runtimes:
                    if not self._fits_host_total(vcpus, memory_gb):
                        task.state = TaskState.FAILED
                        task.error = (
                            f"task requires {vcpus} vCPU / {memory_gb} GiB, exceeding host allocatable capacity "
                            f"of {self._max_allocatable_vcpus()} vCPU / {self._max_allocatable_memory_gb()} GiB"
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
                        snapshot_name=task.snapshot_name,
                        vcpus=task.vcpus,
                        memory_gb=task.memory_gb,
                        error=task.error,
                    )
                else:
                    self.event_logger.emit(
                        "task_queued",
                        batch_id=batch_id,
                        task_id=task.task_id,
                        snapshot_name=task.snapshot_name,
                        vcpus=task.vcpus,
                        memory_gb=task.memory_gb,
                    )
        self._ensure_dispatch()
        return await self.get_batch(batch_id)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Heartbeat & completion
    # ------------------------------------------------------------------

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
        should_reset = False
        runtime_mode: str | None = None
        async with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise KeyError(f"unknown lease_id: {lease_id}")
            task = self._tasks[lease.task_id]
            task_id = task.task_id
            runtime_mode = self._lease_runtime.get(lease_id, "local")
            if task.state in {TaskState.RESETTING, TaskState.COMPLETED, TaskState.FAILED}:
                pass  # already completing
            else:
                task.state = TaskState.RESETTING
                task.updated_at = utcnow()
                should_reset = True
                lease.final_status = final_status
                if runtime_mode == "local":
                    vm = self._vms[lease.slot_id]
                    vm.state = VMState.REVERTING
                    vm.last_used_at = utcnow()
                self.event_logger.emit(
                    "lease_complete_requested",
                    lease_id=lease_id,
                    task_id=task.task_id,
                    runtime_mode=runtime_mode,
                    final_status=final_status,
                    details=details,
                )
        assert task_id is not None
        if should_reset:
            if runtime_mode == "gcp":
                asyncio.create_task(self._release_gcp_slot(lease_id, final_status, details))
            else:
                asyncio.create_task(self._release_after_reset(lease_id, final_status, details))
        return await self.get_task(task_id)

    # ------------------------------------------------------------------
    # Staging
    # ------------------------------------------------------------------

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
            runtime_mode = self._lease_runtime.get(lease_id, "local")

        # os_family is an image-static property from the catalog — no inference.
        image = self.images.get(task.snapshot_name)
        os_family = image.os_family if image else "windows"

        if runtime_mode == "gcp":
            gcp_handle = self._gcp_handles.get(lease_id)
            if gcp_handle is None:
                return LeaseStageResponse(lease_id=lease_id, task_id=task.task_id, phase=phase, skipped=True)
            gcp_rt = self._runtimes.get("gcp")
            if not isinstance(gcp_rt, GCPVMRuntime):
                return LeaseStageResponse(lease_id=lease_id, task_id=task.task_id, phase=phase, skipped=True)
            result = await gcp_rt.stage_task_phase(
                handle=gcp_handle,
                task_id=task.task_id,
                lease_id=lease_id,
                task_data=task_data,
                phase=phase,
                os_family=os_family,
            )
        else:
            vm_handle = self._vm_handles[lease.slot_id]
            result = await self.runtime.stage_task_phase(
                handle=vm_handle,
                task_id=task.task_id,
                lease_id=lease_id,
                task_data=task_data,
                phase=phase,
                container_name=vm_handle.container_name,
                os_family=os_family,
            )
        return LeaseStageResponse(
            lease_id=lease_id,
            task_id=task.task_id,
            phase=phase,
            skipped=result.skipped,
            file_count=result.file_count,
            bytes_staged=result.bytes_staged,
        )

    # ------------------------------------------------------------------
    # Local VM release (revert snapshot)
    # ------------------------------------------------------------------

    async def _release_after_reset(self, lease_id: str, final_status: str, details: dict[str, Any]) -> None:
        evicted = False
        async with self._lock:
            lease = self._leases[lease_id]
            task = self._tasks[lease.task_id]
            vm = self._vms.get(lease.slot_id)
            vm_handle = self._vm_handles.get(lease.slot_id)
            if vm is None or vm_handle is None:
                # VM was hot-removed while a lease was bound to it; mark the
                # task failed and drop the lease without touching runtime.
                task.state = TaskState.FAILED
                task.error = "vm evicted during reset"
                task.updated_at = utcnow()
                task.completed_at = utcnow()
                self._leases.pop(lease_id, None)
                self._lease_runtime.pop(lease_id, None)
                self.event_logger.emit(
                    "vm_evicted_during_reset",
                    task_id=task.task_id,
                    lease_id=lease_id,
                    slot_id=lease.slot_id,
                )
                evicted = True
            else:
                vm.state = VMState.REVERTING
        if evicted:
            self._ensure_dispatch()
            return

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
                self._lease_runtime.pop(lease_id, None)
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
                self._lease_runtime.pop(lease_id, None)
                self.event_logger.emit(
                    "vm_revert_failed",
                    vm_id=vm.vm_id,
                    task_id=task.task_id,
                    lease_id=lease_id,
                    error=str(exc),
                )
            asyncio.create_task(self._auto_replace_vm(vm.vm_id))
        self._ensure_dispatch()
        if self.task_finalized_callback is not None:
            try:
                snapshot = task.model_copy(deep=True)
                await self.task_finalized_callback(snapshot)
            except Exception:
                logger.exception("task_finalized_callback failed for %s", task.task_id)

    # ------------------------------------------------------------------
    # GCP slot release (destroy VM)
    # ------------------------------------------------------------------

    async def _release_gcp_slot(self, lease_id: str, final_status: str, details: dict[str, Any]) -> None:
        async with self._lock:
            lease = self._leases[lease_id]
            task = self._tasks[lease.task_id]
            gcp_handle = self._gcp_handles.get(lease_id)

        new_state = TaskState.COMPLETED if final_status == "completed" else TaskState.FAILED
        gcp_rt = self._runtimes.get("gcp")

        try:
            if gcp_handle is not None and isinstance(gcp_rt, GCPVMRuntime):
                image = self._resolve_image(task.snapshot_name)
                await gcp_rt.reset_slot(gcp_handle, image)
            async with self._lock:
                task.state = new_state
                task.updated_at = utcnow()
                task.completed_at = utcnow()
                if details:
                    task.metadata["completion_details"] = details
                del self._leases[lease_id]
                self._gcp_handles.pop(lease_id, None)
                self._lease_runtime.pop(lease_id, None)
                self.event_logger.emit(
                    "task_finished",
                    task_id=task.task_id,
                    batch_id=task.batch_id,
                    lease_id=lease_id,
                    runtime_mode="gcp",
                    vm_name=gcp_handle.vm_name if gcp_handle else "unknown",
                    final_status=new_state.value,
                )
        except Exception as exc:
            logger.exception("Failed to destroy GCP VM for lease %s", lease_id)
            async with self._lock:
                task.state = TaskState.FAILED
                task.error = f"GCP VM cleanup failed: {exc}"
                task.updated_at = utcnow()
                task.completed_at = utcnow()
                del self._leases[lease_id]
                self._gcp_handles.pop(lease_id, None)
                self._lease_runtime.pop(lease_id, None)
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

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------

    def _ensure_dispatch(self) -> None:
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def _dispatch_loop(self) -> None:
        while True:
            gcp_dispatch_info: dict | None = None

            async with self._lock:
                candidate = self._pick_next_task_locked()
                if candidate is None:
                    self._refresh_batch_states_locked()
                    return

                image = self.images.get(candidate.snapshot_name)
                if image is None or not image.enabled:
                    candidate.state = TaskState.FAILED
                    candidate.error = f"image not found or disabled: {candidate.snapshot_name}"
                    candidate.updated_at = utcnow()
                    candidate.completed_at = utcnow()
                    self._refresh_batch_states_locked()
                    continue

                # Try local dispatch first
                if image.local and "local" in self._runtimes:
                    vm = self._find_free_vm_locked(candidate.snapshot_name)
                    if vm is not None:
                        self._assign_local_locked(candidate, vm)
                        continue  # try dispatching more tasks
                    # No free local VM — task waits in queue
                    self._refresh_batch_states_locked()
                    return

                # GCP dispatch (on-demand VM creation)
                if image.gcp and "gcp" in self._runtimes:
                    active = self._gcp_active_count_locked(candidate.snapshot_name)
                    if active >= image.max_concurrent_vms:
                        self._refresh_batch_states_locked()
                        return  # at GCP capacity, wait

                    # Mark STARTING and prepare for async GCP creation
                    candidate.state = TaskState.STARTING
                    candidate.updated_at = utcnow()
                    gcp_dispatch_info = {
                        "task_id": candidate.task_id,
                        "snapshot_name": candidate.snapshot_name,
                        "batch_id": candidate.batch_id,
                    }
                    self._refresh_batch_states_locked()
                    # Don't return — fall through to async GCP creation below
                else:
                    # Neither local nor GCP available
                    candidate.state = TaskState.FAILED
                    candidate.error = f"no runtime available for image: {candidate.snapshot_name}"
                    candidate.updated_at = utcnow()
                    candidate.completed_at = utcnow()
                    self._refresh_batch_states_locked()
                    continue

            # Async GCP VM creation (outside lock)
            if gcp_dispatch_info is not None:
                asyncio.create_task(self._create_gcp_slot(gcp_dispatch_info))
                # Continue dispatching other tasks
                continue

    def _assign_local_locked(self, candidate: TaskStatus, vm: VMRecord) -> None:
        """Assign a queued task to a ready local VM (must hold lock)."""
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
        self._lease_runtime[lease.lease_id] = "local"
        vm.lease_id = lease.lease_id

        # Build per-port local URLs (overwritten by routes.py _present_* with external URLs)
        vm_handle = self._vm_handles.get(vm.vm_id)
        urls: dict[int, str] = {}
        novnc_url: str | None = None
        if vm_handle is not None:
            urls = {
                guest_port: self.runtime.vm_published_url(vm_handle, guest_port)
                for guest_port in vm_handle.published_ports
            }
            novnc_url = self.runtime.vm_novnc_local_url(vm_handle)

        assignment = TaskAssignment(
            host_id=self.host_config.host_id,
            lease_id=lease.lease_id,
            slot_id=vm.vm_id,
            snapshot_name=candidate.snapshot_name,
            urls=urls,
            novnc_url=novnc_url,
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
            runtime_mode="local",
            queue_wait_s=(utcnow() - candidate.created_at).total_seconds(),
        )

    async def _create_gcp_slot(self, info: dict) -> None:
        """Async GCP VM creation — runs outside the main lock."""
        task_id = info["task_id"]
        snapshot_name = info["snapshot_name"]

        gcp_rt = self._runtimes.get("gcp")
        if not isinstance(gcp_rt, GCPVMRuntime):
            async with self._lock:
                task = self._tasks.get(task_id)
                if task:
                    task.state = TaskState.FAILED
                    task.error = "GCP runtime not available"
                    task.updated_at = utcnow()
                    task.completed_at = utcnow()
            return

        try:
            image = self._resolve_image(snapshot_name)
            slot_id = str(uuid4())[:8]
            lease_id = str(uuid4())

            handle = gcp_rt.prepare_slot(
                slot_id=slot_id,
                image=image,
                vcpus=image.default_vcpus,
                memory_gb=image.default_memory_gb,
                cua_port=5000,  # TODO multi-port GCP
                novnc_port=0,
                lease_id=lease_id,
                task_id=task_id,
            )

            logger.info("Creating GCP VM %s for task %s (machine_type=%s)...",
                        handle.vm_name, task_id, image.gcp_machine_type)
            await gcp_rt.start_slot(handle)

            # TODO multi-port GCP: build urls from image.published_ports
            gcp_cua_url = gcp_rt.cua_local_url(handle)
            gcp_novnc_url = gcp_rt.novnc_local_url(handle)

            async with self._lock:
                task = self._tasks.get(task_id)
                if task is None or task.state != TaskState.STARTING:
                    # Task was cancelled while we were creating the VM
                    logger.warning("Task %s no longer STARTING after GCP VM creation; destroying VM", task_id)
                    await gcp_rt.reset_slot(handle, image)
                    return

                lease = LeaseRecord(
                    task_id=task_id,
                    slot_id=slot_id,
                    expires_at=utcnow() + timedelta(seconds=self.host_config.heartbeat_ttl_s),
                )
                lease.lease_id = lease_id
                self._leases[lease_id] = lease
                self._gcp_handles[lease_id] = handle
                self._lease_runtime[lease_id] = "gcp"

                # TODO multi-port GCP: populate all published ports
                assignment = TaskAssignment(
                    host_id=self.host_config.host_id,
                    lease_id=lease_id,
                    slot_id=slot_id,
                    snapshot_name=snapshot_name,
                    urls={5000: gcp_cua_url},
                    novnc_url=gcp_novnc_url,
                )
                task.lease_id = lease_id
                task.assignment = assignment
                task.state = TaskState.READY
                task.updated_at = utcnow()
                self._refresh_batch_states_locked()

                self.event_logger.emit(
                    "task_ready",
                    batch_id=task.batch_id,
                    task_id=task_id,
                    slot_id=slot_id,
                    lease_id=lease_id,
                    snapshot_name=snapshot_name,
                    runtime_mode="gcp",
                    vm_name=handle.vm_name,
                    vm_ip=handle.vm_ip,
                    machine_type=image.gcp_machine_type,
                )
                logger.info("GCP VM %s ready for task %s (IP=%s)", handle.vm_name, task_id, handle.vm_ip)

        except Exception as exc:
            logger.exception("Failed to create GCP VM for task %s", task_id)
            async with self._lock:
                task = self._tasks.get(task_id)
                if task and task.state == TaskState.STARTING:
                    task.state = TaskState.FAILED
                    task.error = f"GCP VM creation failed: {exc}"
                    task.updated_at = utcnow()
                    task.completed_at = utcnow()
                    self._refresh_batch_states_locked()

    # ------------------------------------------------------------------
    # Lease reaping
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pick_next_task_locked(self) -> TaskStatus | None:
        queued = [task for task in self._tasks.values() if task.state == TaskState.QUEUED]
        if not queued:
            return None
        return min(queued, key=lambda t: t.created_at)

    def _resolve_resources(
        self,
        snapshot_name: str,
        *,
        vcpus: int | None = None,
        memory_gb: int | None = None,
        disk_gb: int | None = None,
    ) -> tuple[int, int, int]:
        """Resolve the full shape tuple for a task.

        Client-supplied values take precedence. When any field is missing,
        fall back to the image defaults (local or GCP) or legacy vm_pool
        entries.
        """
        image = self.images.get(snapshot_name)
        if image is not None:
            default_cpu = image.default_vcpus
            default_mem = image.default_memory_gb
            default_disk = image.default_disk_gb
        else:
            default_cpu = None
            default_mem = None
            default_disk = 64
            for entry in self.host_config.vm_pool:
                if entry.snapshot_name == snapshot_name:
                    default_cpu = entry.vcpus
                    default_mem = entry.memory_gb
                    default_disk = entry.disk_gb
                    break
            if default_cpu is None:
                raise ValueError(f"unknown snapshot_name: {snapshot_name}")

        resolved_cpu = vcpus if vcpus is not None else default_cpu
        resolved_mem = memory_gb if memory_gb is not None else default_mem
        resolved_disk = disk_gb if disk_gb is not None else default_disk
        return resolved_cpu, resolved_mem, resolved_disk

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

    def _fits_host_total(self, vcpus: int, memory_gb: int) -> bool:
        return vcpus <= self._max_allocatable_vcpus() and memory_gb <= self._max_allocatable_memory_gb()

    def _max_allocatable_vcpus(self) -> int:
        return max((os_cpu_count() - self.host_config.host_reserved_vcpus), 0)

    def _max_allocatable_memory_gb(self) -> int:
        return max((host_memory_gb() - self.host_config.host_reserved_memory_gb), 0)

    def _find_free_vm_locked(self, snapshot_name: str) -> VMRecord | None:
        """Find a READY VM matching the snapshot name."""
        for vm in self._vms.values():
            if vm.state == VMState.READY and vm.snapshot_name == snapshot_name:
                return vm
        return None

    def _gcp_active_count_locked(self, snapshot_name: str) -> int:
        """Count active GCP VMs for a given image key."""
        count = 0
        for handle in self._gcp_handles.values():
            if handle.image_key == snapshot_name:
                count += 1
        return count

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

    async def resolve_proxy_target(self, lease_id: str, service: str) -> str:
        """Resolve a proxy target URL for a given lease and service.

        `service` is either "novnc" or a numeric port string (e.g. "5000").
        """
        async with self._lock:
            lease = self._leases[lease_id]
            runtime_mode = self._lease_runtime.get(lease_id, "local")

            if runtime_mode == "gcp":
                gcp_handle = self._gcp_handles.get(lease_id)
                if gcp_handle is None:
                    raise KeyError(f"GCP handle not found for lease {lease_id}")
                gcp_rt = self._runtimes["gcp"]
                if service == "novnc":
                    return gcp_rt.novnc_local_url(gcp_handle)
                return gcp_rt.cua_local_url(gcp_handle)  # TODO multi-port GCP
            else:
                vm_handle = self._vm_handles[lease.slot_id]
                if service == "novnc":
                    return self.runtime.vm_novnc_local_url(vm_handle)
                port = int(service)
                return self.runtime.vm_published_url(vm_handle, port)

    # ------------------------------------------------------------------
    # Cluster hooks: external VM + task registration (worker-in-cluster)
    # ------------------------------------------------------------------

    async def register_external_vm(
        self,
        handle: VMHandle,
        *,
        snapshot_name: str,
        vcpus: int,
        memory_gb: int,
    ) -> None:
        """Inject a hot-plug VM into ``_vms`` so existing lease code sees it.

        Called by the worker-side cluster client after
        ``DockerQemuRuntime.add_vm`` returns. The resulting VMRecord is
        indistinguishable from a statically-pooled one from the rest of
        the scheduler's POV — so ``_find_free_vm_locked``, staging, revert,
        and the lease reaper all Just Work.
        """
        record = VMRecord(
            vm_id=handle.vm_id,
            snapshot_name=snapshot_name,
            state=VMState.READY,
            vcpus=vcpus,
            memory_gb=memory_gb,
            disk_gb=handle.disk_gb,
            container_name=handle.container_name,
            published_ports=handle.published_ports,
            novnc_port=handle.novnc_port,
        )
        async with self._lock:
            self._vms[handle.vm_id] = record
            self._vm_handles[handle.vm_id] = handle

    async def unregister_external_vm(self, vm_id: str) -> bool:
        """Drop a VM from ``_vms`` when master sends REMOVE_VM.

        Returns False if the VM is still leased (caller should defer).
        """
        async with self._lock:
            vm = self._vms.get(vm_id)
            if vm is None:
                return True
            if vm.state in {VMState.LEASED, VMState.REVERTING}:
                return False
            self._vms.pop(vm_id, None)
            self._vm_handles.pop(vm_id, None)
            return True

    async def assign_external_task(
        self,
        *,
        task_id: str,
        task_path: str,
        snapshot_name: str,
        vcpus: int,
        memory_gb: int,
        disk_gb: int,
        task_data: "TaskRequirement.TaskDataRequest | None",
        metadata: dict[str, Any],
        vm_id: str,
        lease_id: str,
        batch_id: str = "__cluster__",
    ) -> TaskStatus:
        """Accept a master-dispatched AssignTask.

        Creates a TaskStatus + LeaseRecord bound to the given VM without
        going through the local dispatch queue. Exists purely for cluster
        mode — standalone never calls this.
        """
        now = utcnow()
        async with self._lock:
            if task_id in self._tasks:
                raise ValueError(f"task_id already exists: {task_id}")
            vm = self._vms.get(vm_id)
            if vm is None:
                raise ValueError(f"vm {vm_id} not registered")
            if vm.state != VMState.READY:
                raise ValueError(f"vm {vm_id} not ready (state={vm.state})")
            task = TaskStatus(
                task_id=task_id,
                task_path=task_path,
                snapshot_name=snapshot_name,
                vcpus=vcpus,
                memory_gb=memory_gb,
                disk_gb=disk_gb,
                metadata=metadata,
                task_data=task_data,
                state=TaskState.READY,
                batch_id=batch_id,
                lease_id=lease_id,
                created_at=now,
                updated_at=now,
            )
            self._tasks[task_id] = task
            vm.state = VMState.LEASED
            vm.task_id = task_id
            vm.lease_id = lease_id
            vm.last_used_at = now
            lease = LeaseRecord(
                lease_id=lease_id,
                task_id=task_id,
                slot_id=vm_id,
                expires_at=now + timedelta(seconds=self.host_config.heartbeat_ttl_s),
            )
            self._leases[lease_id] = lease
            self._lease_runtime[lease_id] = "local"
            handle = self._vm_handles[vm_id]
            urls = {
                guest_port: self.runtime.vm_published_url(handle, guest_port)
                for guest_port in handle.published_ports
            }
            assignment = TaskAssignment(
                host_id=self.host_config.host_id,
                lease_id=lease_id,
                slot_id=vm_id,
                snapshot_name=snapshot_name,
                urls=urls,
                novnc_url=self.runtime.vm_novnc_local_url(handle),
            )
            task.assignment = assignment
            self.event_logger.emit(
                "task_assigned_external",
                task_id=task_id,
                lease_id=lease_id,
                vm_id=vm_id,
                snapshot_name=snapshot_name,
            )
            return task.model_copy(deep=True)


def os_cpu_count() -> int:
    return max((__import__("os").cpu_count() or 1), 1)


def host_memory_gb() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().total / (1024 ** 3))
    except Exception:  # pragma: no cover - psutil may be absent
        return 64

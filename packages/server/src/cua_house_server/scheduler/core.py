"""In-memory task/lease scheduler for cua-house-server.

Ephemeral-VM model: every task gets a freshly provisioned VM via
``runtime.provision_vm(...)`` and has its own ``VMHandle`` for the
duration of the lease. On ``complete`` the scheduler calls
``runtime.destroy_vm(handle)`` — no pool, no revert, no VM reuse.

Three flows converge here:

1. **Standalone**: ``submit_batch`` → the scheduler's own dispatch loop
   provisions locally, binds, and exposes the lease HTTP API.
2. **Worker-in-cluster**: ``submit_batch`` is never called on this
   scheduler — tasks arrive via ``bind_provisioned_task`` from the
   worker cluster client after the worker has already provisioned a
   VM in response to master's ``AssignTask``. The scheduler then owns
   the lease lifecycle (HTTP heartbeat / complete / stage).
3. **Master**: ``submit_batch`` is routed directly to
   ``ClusterDispatcher``; this scheduler is effectively unused in
   master mode (instantiated for legacy reasons but holds nothing).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
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
    TaskRequirement,
    TaskState,
    TaskStatus,
    utcnow,
)
from cua_house_server._internal.port_pool import PortPool
from cua_house_server.config.loader import HostRuntimeConfig, ImageSpec
from cua_house_server.runtimes.gcp import GCPSlotHandle, GCPVMRuntime
from cua_house_server.runtimes.qemu import DockerQemuRuntime, VMHandle
from cua_house_server.scheduler.models import LeaseRecord

logger = logging.getLogger(__name__)


class EnvScheduler:
    """Task/batch/lease lifecycle with ephemeral-VM provisioning.

    Owns:
      * in-memory tables of tasks / batches / leases
      * per-task VM handles (both local QEMU and GCP slot variants)
      * the dispatch loop that turns QUEUED tasks into RUNNING by
        calling ``runtime.provision_vm`` (standalone only)
      * lease reaper + batch heartbeat expiry

    Does not own: cluster-wide worker selection or capacity tracking —
    those live in `cluster/dispatcher.py`.
    """

    def __init__(
        self,
        *,
        runtime: DockerQemuRuntime,
        host_config: HostRuntimeConfig,
        images: dict[str, ImageSpec],
        event_logger: JsonlEventLogger | None = None,
        runtimes: dict[str, DockerQemuRuntime | GCPVMRuntime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.host_config = host_config
        self.images = images
        self._runtimes: dict[str, DockerQemuRuntime | GCPVMRuntime] = (
            runtimes or {"local": runtime}
        )
        self.event_logger = event_logger or JsonlEventLogger(
            host_config.runtime_root / "events.jsonl",
            component="env_server",
        )
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskStatus] = {}
        self._batches: dict[str, BatchStatus] = {}
        self._batch_expires_at: dict[str, datetime] = {}
        self._leases: dict[str, LeaseRecord] = {}
        # task_id → handle. One handle per RUNNING task, cleared on destroy.
        self._local_handles: dict[str, VMHandle] = {}
        self._gcp_handles: dict[str, GCPSlotHandle] = {}
        # task_id → "local" | "gcp"
        self._task_runtime: dict[str, str] = {}
        # These port pools were used by the old pool init path; retained
        # because GCPVMRuntime.prepare_slot takes a novnc_port argument.
        self._published_port_pool = PortPool(*host_config.published_port_range)
        self._novnc_ports = PortPool(*host_config.novnc_port_range)
        self._dispatch_task: asyncio.Task[None] | None = None
        self._lease_reaper_task: asyncio.Task[None] | None = None
        # Fires on terminal task states (COMPLETED / FAILED). Cluster-mode
        # worker uses this to send TaskCompleted upstream over WS.
        self.task_finalized_callback: Any | None = None

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        for rt in self._runtimes.values():
            rt.cleanup_orphaned_state()
        self.event_logger.emit(
            "server_started",
            host_id=self.host_config.host_id,
            host_external_ip=self.host_config.host_external_ip,
        )
        if self._lease_reaper_task is None:
            self._lease_reaper_task = asyncio.create_task(self._lease_reaper_loop())

    async def shutdown(self) -> None:
        if self._lease_reaper_task is not None:
            self._lease_reaper_task.cancel()
            await asyncio.gather(self._lease_reaper_task, return_exceptions=True)
            self._lease_reaper_task = None
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            await asyncio.gather(self._dispatch_task, return_exceptions=True)
            self._dispatch_task = None

    # ── batch admission (standalone) ───────────────────────────────────

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
                # Admission: reject shapes larger than the host's single-node
                # capacity. Same semantics as "unknown image" — no point
                # queueing a task no host can serve.
                if not self._fits_host_total(vcpus, memory_gb):
                    task.state = TaskState.FAILED
                    task.error = (
                        f"task requires {vcpus} vCPU / {memory_gb} GiB, exceeding "
                        f"host capacity ({self._max_allocatable_vcpus()} vCPU / "
                        f"{self._max_allocatable_memory_gb()} GiB)"
                    )
                    task.completed_at = created
                self._tasks[task.task_id] = task
                tasks.append(task)
            batch = BatchStatus(
                batch_id=batch_id,
                state=BatchState.QUEUED,
                created_at=created,
                updated_at=created,
                expires_at=created
                + timedelta(seconds=self.host_config.batch_heartbeat_ttl_s),
                tasks=tasks,
            )
            self._batches[batch_id] = batch
            self._batch_expires_at[batch_id] = batch.expires_at
            self.event_logger.emit(
                "batch_submitted", batch_id=batch_id, task_count=len(tasks),
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

    # ── queries ────────────────────────────────────────────────────────

    async def get_batch(self, batch_id: str) -> BatchStatus:
        async with self._lock:
            return self._snapshot_batch(self._batches[batch_id])

    async def get_task(self, task_id: str) -> TaskStatus:
        async with self._lock:
            return self._tasks[task_id].model_copy(deep=True)

    async def heartbeat_batch(self, batch_id: str) -> BatchHeartbeatResponse:
        async with self._lock:
            batch = self._batches[batch_id]
            batch.expires_at = utcnow() + timedelta(
                seconds=self.host_config.batch_heartbeat_ttl_s,
            )
            batch.updated_at = utcnow()
            self._batch_expires_at[batch_id] = batch.expires_at
            return BatchHeartbeatResponse(
                batch_id=batch_id, expires_at=batch.expires_at,
            )

    async def cancel_batch(
        self,
        batch_id: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> BatchStatus:
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
                elif task.state in {TaskState.READY, TaskState.LEASED} and task.lease_id:
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
                await self.complete(
                    lease_id,
                    final_status="abandoned",
                    details={"reason": reason, **details},
                )
            except KeyError:
                logger.warning(
                    "Lease %s disappeared while cancelling batch %s",
                    lease_id, batch_id,
                )
        return await self.get_batch(batch_id)

    # ── lease ops ──────────────────────────────────────────────────────

    async def heartbeat(self, lease_id: str) -> LeaseHeartbeatResponse:
        async with self._lock:
            lease = self._leases[lease_id]
            lease.expires_at = utcnow() + timedelta(
                seconds=self.host_config.heartbeat_ttl_s,
            )
            task = self._tasks[lease.task_id]
            if task.state == TaskState.READY:
                task.state = TaskState.LEASED
                task.updated_at = utcnow()
            return LeaseHeartbeatResponse(
                lease_id=lease.lease_id,
                task_id=lease.task_id,
                expires_at=lease.expires_at,
            )

    async def complete(
        self,
        lease_id: str,
        *,
        final_status: str,
        details: dict[str, Any] | None = None,
    ) -> TaskStatus:
        details = details or {}
        task_id: str | None = None
        should_destroy = False
        async with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise KeyError(f"unknown lease_id: {lease_id}")
            task = self._tasks[lease.task_id]
            task_id = task.task_id
            if task.state in {TaskState.COMPLETED, TaskState.FAILED, TaskState.RESETTING}:
                pass  # already completing
            else:
                task.state = TaskState.RESETTING
                task.updated_at = utcnow()
                should_destroy = True
                lease.final_status = final_status
                self.event_logger.emit(
                    "lease_complete_requested",
                    lease_id=lease_id,
                    task_id=task.task_id,
                    runtime_mode=self._task_runtime.get(task.task_id, "local"),
                    final_status=final_status,
                    details=details,
                )
        assert task_id is not None
        if should_destroy:
            asyncio.create_task(self._finalize_task(task_id, lease_id, final_status, details))
        return await self.get_task(task_id)

    # ── staging ────────────────────────────────────────────────────────

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
                raise RuntimeError(
                    f"lease {lease_id} is not stageable in state {task.state.value}"
                )
            task_data = task.task_data
            runtime_mode = self._task_runtime.get(task.task_id, "local")

        image = self.images.get(task.snapshot_name)
        os_family = image.os_family if image else "windows"

        if runtime_mode == "gcp":
            gcp_handle = self._gcp_handles.get(task.task_id)
            gcp_rt = self._runtimes.get("gcp")
            if gcp_handle is None or not isinstance(gcp_rt, GCPVMRuntime):
                return LeaseStageResponse(
                    lease_id=lease_id, task_id=task.task_id, phase=phase, skipped=True,
                )
            result = await gcp_rt.stage_task_phase(
                handle=gcp_handle,
                task_id=task.task_id,
                lease_id=lease_id,
                task_data=task_data,
                phase=phase,
                os_family=os_family,
            )
        else:
            vm_handle = self._local_handles.get(task.task_id)
            if vm_handle is None:
                return LeaseStageResponse(
                    lease_id=lease_id, task_id=task.task_id, phase=phase, skipped=True,
                )
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

    # ── proxy resolution ───────────────────────────────────────────────

    async def resolve_proxy_target(self, lease_id: str, service: str) -> str:
        """Resolve the VM's guest-port URL behind a given lease.

        `service` is either "novnc" or a numeric port string (e.g. "5000").
        """
        async with self._lock:
            lease = self._leases[lease_id]
            task_id = lease.task_id
            runtime_mode = self._task_runtime.get(task_id, "local")

        if runtime_mode == "gcp":
            gcp_handle = self._gcp_handles.get(task_id)
            if gcp_handle is None:
                raise KeyError(f"GCP handle not found for task {task_id}")
            gcp_rt = self._runtimes["gcp"]
            if service == "novnc":
                return gcp_rt.novnc_local_url(gcp_handle)
            return gcp_rt.cua_local_url(gcp_handle)  # TODO multi-port GCP
        vm_handle = self._local_handles[task_id]
        if service == "novnc":
            return self.runtime.vm_novnc_local_url(vm_handle)
        return self.runtime.vm_published_url(vm_handle, int(service))

    # ── dispatch (standalone on-demand provisioning) ───────────────────

    def _ensure_dispatch(self) -> None:
        if self._dispatch_task is None or self._dispatch_task.done():
            self._dispatch_task = asyncio.create_task(self._dispatch_loop())

    async def _dispatch_loop(self) -> None:
        """Walk QUEUED tasks in FIFO order; provision + bind each in turn.

        Provisioning is serialized per-loop-tick to keep local cold-boot
        concurrency bounded by the host's own capacity. Tasks that can't
        route to a runtime (unknown image, etc.) fail immediately.
        """
        while True:
            async with self._lock:
                candidate = self._pick_next_queued_locked()
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
                runtime_mode = self._choose_runtime_for(image)
                if runtime_mode is None:
                    candidate.state = TaskState.FAILED
                    candidate.error = f"no runtime available for image: {candidate.snapshot_name}"
                    candidate.updated_at = utcnow()
                    candidate.completed_at = utcnow()
                    self._refresh_batch_states_locked()
                    continue
                candidate.state = TaskState.STARTING
                candidate.updated_at = utcnow()
                task_id = candidate.task_id
                snapshot_name = candidate.snapshot_name
                vcpus, memory_gb, disk_gb = candidate.vcpus, candidate.memory_gb, candidate.disk_gb
                self._refresh_batch_states_locked()

            try:
                if runtime_mode == "local":
                    await self._provision_local_and_bind(
                        task_id, image, vcpus, memory_gb, disk_gb,
                    )
                else:
                    await self._provision_gcp_and_bind(
                        task_id, image, snapshot_name,
                    )
            except Exception as exc:
                logger.exception("Provision failed for task %s", task_id)
                async with self._lock:
                    task = self._tasks.get(task_id)
                    if task is not None and task.state == TaskState.STARTING:
                        task.state = TaskState.FAILED
                        task.error = f"provision failed: {exc}"
                        task.updated_at = utcnow()
                        task.completed_at = utcnow()
                        self._refresh_batch_states_locked()

    def _choose_runtime_for(self, image: ImageSpec) -> str | None:
        if image.local is not None and "local" in self._runtimes:
            return "local"
        if image.gcp is not None and "gcp" in self._runtimes:
            return "gcp"
        return None

    async def _provision_local_and_bind(
        self,
        task_id: str,
        image: ImageSpec,
        vcpus: int,
        memory_gb: int,
        disk_gb: int,
    ) -> None:
        handle = await self.runtime.provision_vm(
            image=image, vcpus=vcpus, memory_gb=memory_gb, disk_gb=disk_gb,
        )
        lease_id = str(uuid4())
        now = utcnow()
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.state != TaskState.STARTING:
                # Cancelled / reaped while we were provisioning — tear down
                # the VM we just made and bail.
                logger.warning(
                    "Task %s no longer STARTING after provision; destroying VM",
                    task_id,
                )
                asyncio.create_task(self.runtime.destroy_vm(handle))
                return
            lease = LeaseRecord(
                lease_id=lease_id,
                task_id=task_id,
                expires_at=now + timedelta(seconds=self.host_config.heartbeat_ttl_s),
            )
            self._leases[lease_id] = lease
            self._local_handles[task_id] = handle
            self._task_runtime[task_id] = "local"
            urls = {
                guest_port: self.runtime.vm_published_url(handle, guest_port)
                for guest_port in handle.published_ports
            }
            task.assignment = TaskAssignment(
                host_id=self.host_config.host_id,
                lease_id=lease_id,
                slot_id=handle.vm_id,
                snapshot_name=image.key,
                urls=urls,
                novnc_url=self.runtime.vm_novnc_local_url(handle),
            )
            task.lease_id = lease_id
            task.state = TaskState.READY
            task.updated_at = utcnow()
            self._refresh_batch_states_locked()
            self.event_logger.emit(
                "task_ready",
                batch_id=task.batch_id,
                task_id=task_id,
                vm_id=handle.vm_id,
                lease_id=lease_id,
                snapshot_name=image.key,
                runtime_mode="local",
                from_cache=handle.from_cache,
                queue_wait_s=(utcnow() - task.created_at).total_seconds(),
            )

    async def _provision_gcp_and_bind(
        self,
        task_id: str,
        image: ImageSpec,
        snapshot_name: str,
    ) -> None:
        gcp_rt = self._runtimes.get("gcp")
        if not isinstance(gcp_rt, GCPVMRuntime):
            raise RuntimeError("GCP runtime not available")
        slot_id = str(uuid4())[:8]
        lease_id = str(uuid4())
        handle = gcp_rt.prepare_slot(
            slot_id=slot_id,
            image=image,
            vcpus=image.default_vcpus,
            memory_gb=image.default_memory_gb,
            cua_port=5000,
            novnc_port=0,
            lease_id=lease_id,
            task_id=task_id,
        )
        logger.info(
            "Creating GCP VM %s for task %s (machine_type=%s)",
            handle.vm_name, task_id, image.gcp_machine_type,
        )
        await gcp_rt.start_slot(handle)
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.state != TaskState.STARTING:
                logger.warning(
                    "Task %s no longer STARTING after GCP provision; tearing down",
                    task_id,
                )
                asyncio.create_task(gcp_rt.reset_slot(handle, image))
                return
            lease = LeaseRecord(
                lease_id=lease_id,
                task_id=task_id,
                expires_at=utcnow() + timedelta(seconds=self.host_config.heartbeat_ttl_s),
            )
            self._leases[lease_id] = lease
            self._gcp_handles[task_id] = handle
            self._task_runtime[task_id] = "gcp"
            task.assignment = TaskAssignment(
                host_id=self.host_config.host_id,
                lease_id=lease_id,
                slot_id=slot_id,
                snapshot_name=snapshot_name,
                urls={5000: gcp_rt.cua_local_url(handle)},
                novnc_url=gcp_rt.novnc_local_url(handle),
            )
            task.lease_id = lease_id
            task.state = TaskState.READY
            task.updated_at = utcnow()
            self._refresh_batch_states_locked()
            self.event_logger.emit(
                "task_ready",
                batch_id=task.batch_id,
                task_id=task_id,
                lease_id=lease_id,
                snapshot_name=snapshot_name,
                runtime_mode="gcp",
                vm_name=handle.vm_name,
                vm_ip=handle.vm_ip,
                machine_type=image.gcp_machine_type,
            )

    # ── finalize ───────────────────────────────────────────────────────

    async def _finalize_task(
        self,
        task_id: str,
        lease_id: str,
        final_status: str,
        details: dict[str, Any],
    ) -> None:
        runtime_mode = self._task_runtime.get(task_id, "local")
        local_handle = self._local_handles.get(task_id)
        gcp_handle = self._gcp_handles.get(task_id)
        new_state = TaskState.COMPLETED if final_status == "completed" else TaskState.FAILED
        try:
            if runtime_mode == "local" and local_handle is not None:
                await self.runtime.destroy_vm(local_handle)
            elif runtime_mode == "gcp" and gcp_handle is not None:
                gcp_rt = self._runtimes.get("gcp")
                image = self._resolve_image(self._tasks[task_id].snapshot_name)
                if isinstance(gcp_rt, GCPVMRuntime):
                    await gcp_rt.reset_slot(gcp_handle, image)
        except Exception as exc:
            logger.exception("destroy_vm failed for task %s", task_id)
            async with self._lock:
                task = self._tasks.get(task_id)
                if task is not None:
                    task.state = TaskState.FAILED
                    task.error = f"destroy failed: {exc}"
                    task.updated_at = utcnow()
                    task.completed_at = utcnow()
                self._leases.pop(lease_id, None)
                self._local_handles.pop(task_id, None)
                self._gcp_handles.pop(task_id, None)
                self._task_runtime.pop(task_id, None)
                self._refresh_batch_states_locked()
            await self._fire_finalized_hook(task_id)
            return

        async with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.state = new_state
                task.updated_at = utcnow()
                task.completed_at = utcnow()
                if details:
                    task.metadata["completion_details"] = details
            self._leases.pop(lease_id, None)
            self._local_handles.pop(task_id, None)
            self._gcp_handles.pop(task_id, None)
            self._task_runtime.pop(task_id, None)
            self._refresh_batch_states_locked()
            self.event_logger.emit(
                "task_finished",
                task_id=task_id,
                lease_id=lease_id,
                final_status=new_state.value,
                runtime_mode=runtime_mode,
            )
        await self._fire_finalized_hook(task_id)
        self._ensure_dispatch()

    async def _fire_finalized_hook(self, task_id: str) -> None:
        if self.task_finalized_callback is None:
            return
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            snapshot = task.model_copy(deep=True)
        try:
            await self.task_finalized_callback(snapshot)
        except Exception:
            logger.exception("task_finalized_callback failed for %s", task_id)

    # ── reaper loops ───────────────────────────────────────────────────

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
                await self.complete(
                    lease_id,
                    final_status="abandoned",
                    details={"reason": "lease expired"},
                )
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

    # ── cluster worker hooks ──────────────────────────────────────────

    async def bind_provisioned_task(
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
        handle: VMHandle,
        lease_id: str,
        batch_id: str = "__cluster__",
    ) -> TaskStatus:
        """Register a worker-side task whose VM has already been provisioned.

        Called by the cluster worker after `runtime.provision_vm` returns
        inside an `AssignTask` handler. The scheduler then owns the lease
        for HTTP heartbeat/complete/stage calls coming directly from clients.
        """
        now = utcnow()
        async with self._lock:
            if task_id in self._tasks:
                raise ValueError(f"task_id already exists: {task_id}")
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
            self._local_handles[task_id] = handle
            self._task_runtime[task_id] = "local"
            self._leases[lease_id] = LeaseRecord(
                lease_id=lease_id,
                task_id=task_id,
                expires_at=now + timedelta(seconds=self.host_config.heartbeat_ttl_s),
            )
            urls = {
                guest_port: self.runtime.vm_published_url(handle, guest_port)
                for guest_port in handle.published_ports
            }
            task.assignment = TaskAssignment(
                host_id=self.host_config.host_id,
                lease_id=lease_id,
                slot_id=handle.vm_id,
                snapshot_name=snapshot_name,
                urls=urls,
                novnc_url=self.runtime.vm_novnc_local_url(handle),
            )
            self.event_logger.emit(
                "task_bound_external",
                task_id=task_id,
                lease_id=lease_id,
                vm_id=handle.vm_id,
                snapshot_name=snapshot_name,
            )
            return task.model_copy(deep=True)

    # ── helpers ────────────────────────────────────────────────────────

    def _pick_next_queued_locked(self) -> TaskStatus | None:
        queued = [t for t in self._tasks.values() if t.state == TaskState.QUEUED]
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
        image = self.images.get(snapshot_name)
        if image is None:
            raise ValueError(f"unknown snapshot_name: {snapshot_name}")
        return (
            vcpus if vcpus is not None else image.default_vcpus,
            memory_gb if memory_gb is not None else image.default_memory_gb,
            disk_gb if disk_gb is not None else image.default_disk_gb,
        )

    def _resolve_image(self, image_key: str) -> ImageSpec:
        image = self.images.get(image_key)
        if image is None:
            raise ValueError(f"unknown image_key: {image_key}")
        if not image.enabled:
            raise ValueError(f"image not enabled in this deployment: {image_key}")
        return image

    def _fits_host_total(self, vcpus: int, memory_gb: int) -> bool:
        return (
            vcpus <= self._max_allocatable_vcpus()
            and memory_gb <= self._max_allocatable_memory_gb()
        )

    def _max_allocatable_vcpus(self) -> int:
        return max(os_cpu_count() - self.host_config.host_reserved_vcpus, 0)

    def _max_allocatable_memory_gb(self) -> int:
        return max(host_memory_gb() - self.host_config.host_reserved_memory_gb, 0)

    def _refresh_batch_states_locked(self) -> None:
        for batch in self._batches.values():
            batch.tasks = [
                self._tasks[task.task_id].model_copy(deep=True)
                for task in batch.tasks
            ]
            states = {t.state for t in batch.tasks}
            if states <= {TaskState.COMPLETED}:
                batch.state = BatchState.COMPLETED
            elif TaskState.FAILED in states and states <= {
                TaskState.COMPLETED,
                TaskState.FAILED,
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


def os_cpu_count() -> int:
    return max(__import__("os").cpu_count() or 1, 1)


def host_memory_gb() -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().total / (1024**3))
    except ImportError:
        return 16

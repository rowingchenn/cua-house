"""End-to-end ClusterDispatcher coverage with a scripted fake worker.

New flow (Phase 4-fix): master is batch admission + assignment only.
Lease lifecycle (heartbeat, complete, stage) is NOT on master — clients
go directly to the worker's HTTP API using ``TaskAssignment.lease_endpoint``.
Task completion reaches master asynchronously via the ``TaskCompleted``
message that the worker fires after its local revert finishes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cua_house_common.models import (
    BatchCreateRequest,
    TaskRequirement,
    TaskState,
    BatchState,
)
from cua_house_server.cluster.dispatcher import ClusterDispatcher
from cua_house_server.cluster.protocol import (
    AssignTask,
    Envelope,
    ReleaseLease,
    TaskBound,
    TaskCompleted,
    WorkerCapacity,
    WorkerVMSummary,
)
from cua_house_server.cluster.reconciler import PoolOpCoordinator
from cua_house_server.cluster.registry import WorkerRegistry
from cua_house_server.config.loader import (
    HostRuntimeConfig,
    ImageSpec,
    LocalImageConfig,
)


def _host_config() -> HostRuntimeConfig:
    return HostRuntimeConfig(
        host_id="master",
        host_external_ip="10.0.0.1",
        public_base_host="10.0.0.1",
        runtime_root=Path("/tmp/cua-house-test-master"),
        task_data_root=None,
        docker_image="",
        host_reserved_vcpus=0,
        host_reserved_memory_gb=0,
        batch_heartbeat_ttl_s=60,
        heartbeat_ttl_s=60,
        ready_timeout_s=5,
        readiness_poll_interval_s=1,
        idle_slot_ttl_s=60,
        published_port_range=(1, 2),
        novnc_port_range=(3, 4),
        snapshot_revert_timeout_s=5,
    )


def _images() -> dict[str, ImageSpec]:
    local = LocalImageConfig(
        template_qcow2_path=Path("/tmp/template.qcow2"),
        default_vcpus=4,
        default_memory_gb=8,
    )
    return {
        "cpu-free": ImageSpec(
            key="cpu-free",
            enabled=True,
            os_family="linux",
            published_ports=(5000,),
            local=local,
        )
    }


class _ScriptedWorker:
    """Fake worker: replies to AssignTask with TaskBound, records ReleaseLease."""

    def __init__(self, coordinator: PoolOpCoordinator) -> None:
        self.coordinator = coordinator
        self.assigned: list[AssignTask] = []
        self.released: list[ReleaseLease] = []
        self.auto_bind_ok = True

    async def send_json(self, data: Any) -> None:
        envelope = Envelope.model_validate(data)
        kind = envelope.payload.get("kind")
        if kind == "assign_task":
            msg = AssignTask.model_validate(envelope.payload)
            self.assigned.append(msg)
            reply = TaskBound(
                task_id=msg.task_id,
                lease_id=msg.lease_id,
                vm_id=msg.vm_id,
                ok=self.auto_bind_ok,
                error=None if self.auto_bind_ok else "scripted fail",
                lease_endpoint="http://worker.internal:8787",
                urls={5000: "http://worker.internal:16001"},
                novnc_url="http://worker.internal:18001",
            )
            await self.coordinator.resolve(envelope.correlation_id or "", reply)
        elif kind == "release_lease":
            self.released.append(ReleaseLease.model_validate(envelope.payload))
        # TaskCompleted is an async notification: master drives it via
        # dispatcher.handle_task_completed from the WS dispatcher, not an RPC.


async def _setup_dispatcher_with_worker(vm_available: bool = True):
    coordinator = PoolOpCoordinator()
    registry = WorkerRegistry(heartbeat_ttl_s=999)
    worker = _ScriptedWorker(coordinator)
    await registry.register(
        worker_id="w1",
        runtime_version="0.1",
        capacity=WorkerCapacity(total_vcpus=16, total_memory_gb=64, total_disk_gb=500),
        hosted_images=["cpu-free"],
        ws=worker,  # type: ignore[arg-type]
    )
    if vm_available:
        await registry.apply_heartbeat(
            "w1",
            load_cpu=0.0,
            load_memory=0.0,
            vm_summaries=[
                WorkerVMSummary(
                    vm_id="vm-1",
                    image_key="cpu-free",
                    vcpus=4,
                    memory_gb=8,
                    disk_gb=64,
                    state="ready",
                )
            ],
        )
    dispatcher = ClusterDispatcher(
        host_config=_host_config(),
        images=_images(),
        registry=registry,
        coordinator=coordinator,
    )
    return dispatcher, registry, worker, coordinator


@pytest.mark.asyncio
async def test_submit_batch_returns_ready_assignment_with_lease_endpoint() -> None:
    dispatcher, _, worker, _ = await _setup_dispatcher_with_worker()
    await dispatcher.submit_batch(
        BatchCreateRequest(
            tasks=[
                TaskRequirement(
                    task_id="t1",
                    task_path="/tmp/t1",
                    snapshot_name="cpu-free",
                    vcpus=4,
                    memory_gb=8,
                )
            ]
        )
    )
    await dispatcher._dispatch_pending()
    task = await dispatcher.get_task("t1")
    assert task.state == TaskState.READY
    assert task.assignment is not None
    assert task.assignment.lease_endpoint == "http://worker.internal:8787"
    assert task.assignment.urls == {5000: "http://worker.internal:16001"}
    assert len(worker.assigned) == 1
    assert worker.assigned[0].vcpus == 4


@pytest.mark.asyncio
async def test_no_capacity_keeps_task_queued() -> None:
    dispatcher, *_ = await _setup_dispatcher_with_worker(vm_available=False)
    await dispatcher.submit_batch(
        BatchCreateRequest(
            tasks=[
                TaskRequirement(
                    task_id="t1",
                    task_path="/tmp/t1",
                    snapshot_name="cpu-free",
                    vcpus=4,
                    memory_gb=8,
                )
            ]
        )
    )
    await dispatcher._dispatch_pending()
    task = await dispatcher.get_task("t1")
    assert task.state == TaskState.QUEUED


@pytest.mark.asyncio
async def test_task_completed_message_updates_batch_state() -> None:
    dispatcher, _, _, _ = await _setup_dispatcher_with_worker()
    batch = await dispatcher.submit_batch(
        BatchCreateRequest(
            tasks=[
                TaskRequirement(
                    task_id="t1",
                    task_path="/tmp/t1",
                    snapshot_name="cpu-free",
                    vcpus=4,
                    memory_gb=8,
                )
            ]
        )
    )
    await dispatcher._dispatch_pending()
    task = await dispatcher.get_task("t1")
    assert task.state == TaskState.READY
    # Simulate the worker pushing a TaskCompleted once the client called
    # complete() on the worker directly.
    await dispatcher.handle_task_completed(
        "w1",
        TaskCompleted(task_id="t1", lease_id=task.lease_id or "", final_status="completed"),
    )
    task = await dispatcher.get_task("t1")
    assert task.state == TaskState.COMPLETED
    batch = await dispatcher.get_batch(batch.batch_id)
    assert batch.state == BatchState.COMPLETED


@pytest.mark.asyncio
async def test_worker_disconnect_fails_orphaned_tasks() -> None:
    dispatcher, _, _, _ = await _setup_dispatcher_with_worker()
    await dispatcher.submit_batch(
        BatchCreateRequest(
            tasks=[
                TaskRequirement(
                    task_id="t1",
                    task_path="/tmp/t1",
                    snapshot_name="cpu-free",
                    vcpus=4,
                    memory_gb=8,
                )
            ]
        )
    )
    await dispatcher._dispatch_pending()
    task = await dispatcher.get_task("t1")
    assert task.state == TaskState.READY
    await dispatcher.handle_worker_disconnect("w1")
    task = await dispatcher.get_task("t1")
    assert task.state == TaskState.FAILED
    assert "disconnected" in (task.error or "")


@pytest.mark.asyncio
async def test_cancel_batch_sends_release_lease_to_worker() -> None:
    dispatcher, _, worker, _ = await _setup_dispatcher_with_worker()
    batch = await dispatcher.submit_batch(
        BatchCreateRequest(
            tasks=[
                TaskRequirement(
                    task_id="t1",
                    task_path="/tmp/t1",
                    snapshot_name="cpu-free",
                    vcpus=4,
                    memory_gb=8,
                )
            ]
        )
    )
    await dispatcher._dispatch_pending()
    await dispatcher.cancel_batch(batch.batch_id, reason="user cancel")
    assert len(worker.released) == 1
    assert worker.released[0].final_status == "abandoned"

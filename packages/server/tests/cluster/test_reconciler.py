"""Reconciler + PoolOpCoordinator coverage.

These tests drive the reconciler against a fake worker that satisfies the
WorkerSession contract without a real WebSocket. The fake intercepts
Envelopes sent by the registry, decodes the PoolOp, and replies with a
PoolOpResult via the coordinator — simulating exactly what a real worker
would do over the wire.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cua_house_server.cluster.pool_spec import (
    ClusterPoolSpec,
    PoolAssignment,
)
from cua_house_server.cluster.protocol import (
    Envelope,
    PoolOp,
    PoolOpResult,
    WorkerCapacity,
)
from cua_house_server.cluster.reconciler import (
    PoolOpCoordinator,
    PoolReconciler,
)
from cua_house_server.cluster.registry import WorkerRegistry


class _ScriptedWS:
    """Fake WebSocket that pushes received envelopes onto a queue.

    The test harness pops envelopes and feeds synthetic PoolOpResults back
    into the coordinator so the reconciler's awaits resolve.
    """

    def __init__(self, coordinator: PoolOpCoordinator) -> None:
        self.coordinator = coordinator
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.auto_success = True
        self.produced_vm_ids: list[str] = []
        self._counter = 0

    async def send_json(self, data: Any) -> None:
        envelope = Envelope.model_validate(data)
        op = PoolOp.model_validate(envelope.payload)
        await self.queue.put(data)
        if self.auto_success:
            vm_id: str | None = None
            if op.op == "ADD_VM":
                self._counter += 1
                vm_id = f"vm-{self._counter}"
                self.produced_vm_ids.append(vm_id)
            await self.coordinator.resolve(
                envelope.correlation_id or op.op_id,
                PoolOpResult(op_id=op.op_id, ok=True, produced_vm_id=vm_id),
            )


def _cap() -> WorkerCapacity:
    return WorkerCapacity(total_vcpus=16, total_memory_gb=64, total_disk_gb=500)


@pytest.mark.asyncio
async def test_reconciler_add_image_and_vms_on_empty_worker() -> None:
    coordinator = PoolOpCoordinator()
    registry = WorkerRegistry(heartbeat_ttl_s=999)
    ws = _ScriptedWS(coordinator)
    await registry.register(
        worker_id="w1",
        runtime_version="0.1",
        capacity=_cap(),
        hosted_images=[],
        ws=ws,  # type: ignore[arg-type]
    )
    spec = ClusterPoolSpec()
    spec.set([PoolAssignment("w1", "cpu-free", 2, 4, 8)])
    reconciler = PoolReconciler(
        registry=registry, pool_spec=spec, coordinator=coordinator, interval_s=1.0,
    )
    await reconciler.reconcile_once()

    # 1 ADD_IMAGE + 2 ADD_VM = 3 envelopes sent.
    assert ws.queue.qsize() == 3
    # Optimistic update should have populated the session.
    session = await registry.get("w1")
    assert session is not None
    assert "cpu-free" in session.hosted_images
    assert len(session.vm_summaries) == 2

    # Second reconcile is a no-op (desired == actual).
    await reconciler.reconcile_once()
    assert ws.queue.qsize() == 3


@pytest.mark.asyncio
async def test_reconciler_shrinks_oversized_pool() -> None:
    coordinator = PoolOpCoordinator()
    registry = WorkerRegistry(heartbeat_ttl_s=999)
    ws = _ScriptedWS(coordinator)
    await registry.register(
        worker_id="w1",
        runtime_version="0.1",
        capacity=_cap(),
        hosted_images=["cpu-free"],
        ws=ws,  # type: ignore[arg-type]
    )
    # Seed the session with two existing VMs, then desire only one.
    from cua_house_server.cluster.protocol import WorkerVMSummary
    session = await registry.get("w1")
    assert session is not None
    session.vm_summaries = [
        WorkerVMSummary(vm_id="v1", image_key="cpu-free", vcpus=4, memory_gb=8, state="ready"),
        WorkerVMSummary(vm_id="v2", image_key="cpu-free", vcpus=4, memory_gb=8, state="ready"),
    ]
    spec = ClusterPoolSpec()
    spec.set([PoolAssignment("w1", "cpu-free", 1, 4, 8)])
    reconciler = PoolReconciler(
        registry=registry, pool_spec=spec, coordinator=coordinator,
    )
    await reconciler.reconcile_once()

    assert ws.queue.qsize() == 1
    sent = ws.queue.get_nowait()
    op = PoolOp.model_validate(Envelope.model_validate(sent).payload)
    assert op.op == "REMOVE_VM"
    session = await registry.get("w1")
    assert len(session.vm_summaries) == 1
    assert session.vm_summaries[0].vm_id == "v1"


@pytest.mark.asyncio
async def test_coordinator_cancel_on_worker_disconnect() -> None:
    coordinator = PoolOpCoordinator()
    pending = await coordinator.issue("w1")
    cancel_task = asyncio.create_task(coordinator.cancel_worker("w1"))
    with pytest.raises(ConnectionError):
        await pending.future
    await cancel_task


@pytest.mark.asyncio
async def test_coordinator_timeout() -> None:
    coordinator = PoolOpCoordinator(default_timeout_s=0.05)
    pending = await coordinator.issue("w1")
    with pytest.raises(asyncio.TimeoutError):
        await coordinator.await_result(pending)

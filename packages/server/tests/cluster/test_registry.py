"""WorkerRegistry unit coverage (no real WebSockets)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from cua_house_server.cluster.protocol import WorkerCapacity, WorkerVMSummary
from cua_house_server.cluster.registry import WorkerRegistry


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)


def _cap() -> WorkerCapacity:
    return WorkerCapacity(total_vcpus=4, total_memory_gb=16, total_disk_gb=100)


@pytest.mark.asyncio
async def test_register_and_snapshot() -> None:
    reg = WorkerRegistry()
    ws = _FakeWS()
    await reg.register(
        worker_id="w1",
        runtime_version="0.1",
        capacity=_cap(),
        hosted_images=["cpu-free"],
        ws=ws,  # type: ignore[arg-type]
    )
    snapshot = await reg.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].worker_id == "w1"
    assert snapshot[0].online is True
    assert "cpu-free" in snapshot[0].hosted_images


@pytest.mark.asyncio
async def test_heartbeat_updates_vm_summaries() -> None:
    reg = WorkerRegistry()
    ws = _FakeWS()
    await reg.register(
        worker_id="w1",
        runtime_version="0.1",
        capacity=_cap(),
        hosted_images=[],
        ws=ws,  # type: ignore[arg-type]
    )
    vms = [
        WorkerVMSummary(vm_id="v1", image_key="cpu-free", vcpus=4, memory_gb=8, disk_gb=64, state="ready"),
    ]
    await reg.apply_heartbeat("w1", load_cpu=0.2, load_memory=0.3, vm_summaries=vms)
    session = await reg.get("w1")
    assert session is not None
    assert len(session.vm_summaries) == 1
    assert session.load_cpu == 0.2


@pytest.mark.asyncio
async def test_reap_stale_marks_offline() -> None:
    reg = WorkerRegistry(heartbeat_ttl_s=0.05)
    ws = _FakeWS()
    await reg.register(
        worker_id="w1",
        runtime_version="0.1",
        capacity=_cap(),
        hosted_images=[],
        ws=ws,  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.1)
    evicted = await reg.reap_stale()
    assert evicted == ["w1"]
    session = await reg.get("w1")
    assert session is not None and session.online is False


@pytest.mark.asyncio
async def test_free_vm_for_matches_only_ready() -> None:
    reg = WorkerRegistry()
    ws = _FakeWS()
    await reg.register(
        worker_id="w1", runtime_version="0.1", capacity=_cap(),
        hosted_images=[], ws=ws,  # type: ignore[arg-type]
    )
    await reg.apply_heartbeat(
        "w1", load_cpu=0.0, load_memory=0.0,
        vm_summaries=[
            WorkerVMSummary(vm_id="v1", image_key="cpu-free", vcpus=4, memory_gb=8, disk_gb=64, state="leased"),
            WorkerVMSummary(vm_id="v2", image_key="cpu-free", vcpus=8, memory_gb=16, disk_gb=64, state="ready"),
        ],
    )
    session = await reg.get("w1")
    assert session is not None
    assert session.free_vm_for("cpu-free", 4, 8, 64) is not None
    assert session.free_vm_for("cpu-free", 4, 8, 64).vm_id == "v2"
    assert session.free_vm_for("cpu-free", 16, 32, 64) is None

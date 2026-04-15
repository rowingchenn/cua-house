"""Worker-side PoolOp execution with a fake runtime.

The WorkerClusterClient dispatches PoolOps into DockerQemuRuntime methods.
Here we stub the runtime entirely so the test runs without docker/qemu.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from cua_house_server.cluster.protocol import (
    PoolOp,
    PoolOpArgs,
)
from cua_house_server.cluster.worker_client import WorkerClusterClient
from cua_house_server.config.loader import ClusterConfig, HostRuntimeConfig
from pathlib import Path


@dataclass
class _FakeHandle:
    vm_id: str
    vcpus: int
    memory_gb: int
    disk_gb: int = 64
    published_ports: dict[int, int] = field(default_factory=dict)
    novnc_port: int = 0


@dataclass
class _FakeRuntime:
    pulled: list[str] = field(default_factory=list)
    added: list[tuple[str, int, int, int]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    _counter: int = 0
    _cluster_catalog: dict[str, Any] = field(default_factory=dict)

    async def pull_template(self, image_key: str, image: Any) -> None:
        self.pulled.append(image_key)

    async def add_vm(self, *, image: Any, vcpus: int, memory_gb: int,
                     disk_gb: int | None = None,
                     snapshot_name: str | None = None) -> _FakeHandle:
        self._counter += 1
        vm_id = f"vm-{self._counter}"
        resolved_disk = disk_gb if disk_gb is not None else image.default_disk_gb
        self.added.append((image.key, vcpus, memory_gb, resolved_disk))
        return _FakeHandle(vm_id=vm_id, vcpus=vcpus, memory_gb=memory_gb, disk_gb=resolved_disk)

    async def remove_vm(self, vm_id: str) -> None:
        self.removed.append(vm_id)


@dataclass
class _FakeScheduler:
    registered: list[str] = field(default_factory=list)
    unregistered: list[str] = field(default_factory=list)
    task_finalized_callback: Any = None
    # Used by unregister refusal test
    refuse_unregister: set[str] = field(default_factory=set)

    async def register_external_vm(self, handle, *, snapshot_name, vcpus, memory_gb):
        self.registered.append(handle.vm_id)

    async def unregister_external_vm(self, vm_id: str) -> bool:
        if vm_id in self.refuse_unregister:
            return False
        self.unregistered.append(vm_id)
        return True


def _make_client(runtime: _FakeRuntime) -> WorkerClusterClient:
    host_config = HostRuntimeConfig(
        host_id="h1", host_external_ip="127.0.0.1", public_base_host="127.0.0.1",
        runtime_root=Path("/tmp/cua-house-test"),
        task_data_root=None, docker_image="",
        host_reserved_vcpus=0, host_reserved_memory_gb=0,
        batch_heartbeat_ttl_s=60, heartbeat_ttl_s=60, ready_timeout_s=60,
        readiness_poll_interval_s=1, idle_slot_ttl_s=60,
        published_port_range=(1, 2), novnc_port_range=(3, 4),
    )
    cluster = ClusterConfig(master_url="ws://fake", worker_id="w1", join_token="t")
    scheduler = _FakeScheduler()
    return WorkerClusterClient(  # type: ignore[arg-type]
        host_config=host_config,
        cluster=cluster,
        runtime=runtime,
        scheduler=scheduler,
        lease_endpoint="http://127.0.0.1:8787",
    )


class _StubImage:
    key = "cpu-free"
    default_disk_gb = 64
    version = "test-v1"


@pytest.mark.asyncio
async def test_add_image_pulls_template() -> None:
    runtime = _FakeRuntime()
    runtime._cluster_catalog = {"cpu-free": _StubImage()}
    client = _make_client(runtime)
    op = PoolOp(op_id="o1", op="ADD_IMAGE", args=PoolOpArgs(image_key="cpu-free"))
    ok, err, produced = await client._execute_pool_op(op)
    assert ok is True
    assert runtime.pulled == ["cpu-free"]
    assert "cpu-free" in client._hosted_images


@pytest.mark.asyncio
async def test_add_vm_calls_runtime_and_tracks_summary() -> None:
    runtime = _FakeRuntime()
    runtime._cluster_catalog = {"cpu-free": _StubImage()}
    client = _make_client(runtime)
    op = PoolOp(
        op_id="o1", op="ADD_VM",
        args=PoolOpArgs(image_key="cpu-free", vcpus=4, memory_gb=8),
    )
    ok, err, produced = await client._execute_pool_op(op)
    assert ok is True
    assert produced is not None
    assert runtime.added == [("cpu-free", 4, 8, 64)]
    assert produced in client._vm_summaries
    assert client._vm_summaries[produced].image_key == "cpu-free"


@pytest.mark.asyncio
async def test_remove_vm_clears_summary() -> None:
    runtime = _FakeRuntime()
    runtime._cluster_catalog = {"cpu-free": _StubImage()}
    client = _make_client(runtime)
    add_op = PoolOp(
        op_id="o1", op="ADD_VM",
        args=PoolOpArgs(image_key="cpu-free", vcpus=4, memory_gb=8),
    )
    _, _, produced = await client._execute_pool_op(add_op)
    assert produced is not None
    remove_op = PoolOp(op_id="o2", op="REMOVE_VM", args=PoolOpArgs(vm_id=produced))
    ok, err, _ = await client._execute_pool_op(remove_op)
    assert ok is True
    assert runtime.removed == [produced]
    assert produced not in client._vm_summaries


@pytest.mark.asyncio
async def test_remove_image_refuses_if_vms_still_running() -> None:
    runtime = _FakeRuntime()
    runtime._cluster_catalog = {"cpu-free": _StubImage()}
    client = _make_client(runtime)
    await client._execute_pool_op(PoolOp(
        op_id="o1", op="ADD_VM",
        args=PoolOpArgs(image_key="cpu-free", vcpus=4, memory_gb=8),
    ))
    ok, err, _ = await client._execute_pool_op(PoolOp(
        op_id="o2", op="REMOVE_IMAGE",
        args=PoolOpArgs(image_key="cpu-free"),
    ))
    assert ok is False
    assert err is not None and "still running" in err

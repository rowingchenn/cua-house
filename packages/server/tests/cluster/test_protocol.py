"""Envelope + discriminated-union roundtrip coverage."""

from __future__ import annotations

from pydantic import TypeAdapter

from cua_house_server.cluster.protocol import (
    AssignTask,
    CachedShape,
    Envelope,
    Heartbeat,
    MasterToWorker,
    Register,
    ReleaseLease,
    Shutdown,
    TaskBound,
    TaskCompleted,
    WorkerCapacity,
    WorkerToMaster,
    WorkerVMSummary,
)

w2m = TypeAdapter(WorkerToMaster)
m2w = TypeAdapter(MasterToWorker)


def _roundtrip_w2m(msg) -> None:
    dumped = msg.model_dump()
    assert w2m.validate_python(dumped).kind == msg.kind


def _roundtrip_m2w(msg) -> None:
    dumped = msg.model_dump()
    assert m2w.validate_python(dumped).kind == msg.kind


def test_register_roundtrip() -> None:
    msg = Register(
        worker_id="w1",
        runtime_version="0.1.0",
        capacity=WorkerCapacity(total_vcpus=8, total_memory_gb=32, total_disk_gb=500),
        hosted_images=["cpu-free"],
    )
    _roundtrip_w2m(msg)


def test_heartbeat_roundtrip_with_vms_and_cache() -> None:
    msg = Heartbeat(
        vm_summaries=[
            WorkerVMSummary(
                vm_id="v1", image_key="cpu-free", vcpus=4, memory_gb=8, disk_gb=64,
                lease_id="l1",
            ),
        ],
        cached_shapes=[
            CachedShape(image_key="cpu-free", image_version="v1", vcpus=4, memory_gb=8, disk_gb=64),
        ],
    )
    _roundtrip_w2m(msg)


def test_task_bound_roundtrip() -> None:
    _roundtrip_w2m(
        TaskBound(
            task_id="t1", lease_id="l1", vm_id="v1", from_cache=True,
            urls={5000: "http://x:16001"}, novnc_url="http://x:18001",
        )
    )


def test_task_completed_roundtrip() -> None:
    _roundtrip_w2m(
        TaskCompleted(task_id="t1", lease_id="l1", final_status="completed")
    )


def test_assign_task_roundtrip() -> None:
    _roundtrip_m2w(
        AssignTask(
            task_id="t1", lease_id="l1", image_key="cpu-free",
            vcpus=4, memory_gb=8, disk_gb=64,
        )
    )


def test_release_lease_roundtrip() -> None:
    _roundtrip_m2w(ReleaseLease(lease_id="l1", final_status="completed"))


def test_shutdown_roundtrip() -> None:
    _roundtrip_m2w(Shutdown(graceful=True))


def test_envelope_wraps_register() -> None:
    inner = Register(
        worker_id="w1",
        runtime_version="0.1.0",
        capacity=WorkerCapacity(total_vcpus=1, total_memory_gb=1, total_disk_gb=1),
    )
    env = Envelope(msg_id="m1", payload=inner.model_dump())
    restored = Envelope.model_validate(env.model_dump())
    assert w2m.validate_python(restored.payload).kind == "register"

"""Envelope + discriminated-union roundtrip coverage."""

from __future__ import annotations

from pydantic import TypeAdapter

from cua_house_server.cluster.protocol import (
    AssignTask,
    Envelope,
    Heartbeat,
    MasterToWorker,
    PoolOp,
    PoolOpArgs,
    PoolOpResult,
    Register,
    Shutdown,
    VMStateUpdate,
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


def test_heartbeat_roundtrip_with_vms() -> None:
    from cua_house_server.cluster.protocol import CachedShape
    msg = Heartbeat(
        vm_summaries=[
            WorkerVMSummary(
                vm_id="v1", image_key="cpu-free", vcpus=4, memory_gb=8, disk_gb=64, state="ready"
            )
        ],
        cached_shapes=[
            CachedShape(image_key="cpu-free", image_version="v1", vcpus=4, memory_gb=8, disk_gb=64),
        ],
    )
    _roundtrip_w2m(msg)


def test_vm_state_update_roundtrip() -> None:
    _roundtrip_w2m(VMStateUpdate(vm_id="v1", state="leased", lease_id="l1"))


def test_pool_op_result_roundtrip() -> None:
    _roundtrip_w2m(PoolOpResult(op_id="op1", ok=True, produced_vm_id="v1"))


def test_assign_task_roundtrip() -> None:
    _roundtrip_m2w(
        AssignTask(task_id="t1", lease_id="l1", vm_id="v1", image_key="cpu-free")
    )


def test_pool_op_roundtrip_all_kinds() -> None:
    for op in ("ADD_IMAGE", "REMOVE_IMAGE", "ADD_VM", "REMOVE_VM"):
        msg = PoolOp(
            op_id=f"op-{op}",
            op=op,  # type: ignore[arg-type]
            args=PoolOpArgs(image_key="cpu-free", vcpus=4, memory_gb=8),
        )
        _roundtrip_m2w(msg)


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

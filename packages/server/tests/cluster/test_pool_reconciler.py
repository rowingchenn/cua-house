"""compute_diff coverage — pure, no I/O."""

from __future__ import annotations

from cua_house_server.cluster.pool_spec import (
    ClusterPoolSpec,
    PoolAssignment,
    compute_diff,
)
from cua_house_server.cluster.protocol import WorkerVMSummary


def _make_spec(*assignments: PoolAssignment) -> ClusterPoolSpec:
    spec = ClusterPoolSpec()
    spec.set(list(assignments))
    return spec


def test_add_image_then_add_vm_when_worker_empty() -> None:
    spec = _make_spec(PoolAssignment("w1", "cpu-free", 2, 4, 8))
    diff = compute_diff(spec=spec, worker_id="w1", worker_images=set(), worker_vms=[])
    ops = [(e.op, e.image_key) for e in diff]
    assert ("ADD_IMAGE", "cpu-free") in ops
    assert ops.count(("ADD_VM", "cpu-free")) == 2


def test_no_ops_when_actual_matches_desired() -> None:
    spec = _make_spec(PoolAssignment("w1", "cpu-free", 1, 4, 8))
    vms = [
        WorkerVMSummary(vm_id="v1", image_key="cpu-free", cpu_cores=4, memory_gb=8, state="ready"),
    ]
    diff = compute_diff(
        spec=spec, worker_id="w1", worker_images={"cpu-free"}, worker_vms=vms,
    )
    assert diff == []


def test_remove_extra_vms_above_desired_count() -> None:
    spec = _make_spec(PoolAssignment("w1", "cpu-free", 1, 4, 8))
    vms = [
        WorkerVMSummary(vm_id="v1", image_key="cpu-free", cpu_cores=4, memory_gb=8, state="ready"),
        WorkerVMSummary(vm_id="v2", image_key="cpu-free", cpu_cores=4, memory_gb=8, state="ready"),
    ]
    diff = compute_diff(
        spec=spec, worker_id="w1", worker_images={"cpu-free"}, worker_vms=vms,
    )
    assert len(diff) == 1
    assert diff[0].op == "REMOVE_VM"
    assert diff[0].vm_id == "v2"


def test_remove_image_when_no_longer_desired() -> None:
    spec = _make_spec()
    vms = [
        WorkerVMSummary(vm_id="v1", image_key="cpu-free", cpu_cores=4, memory_gb=8, state="ready"),
    ]
    diff = compute_diff(
        spec=spec, worker_id="w1", worker_images={"cpu-free"}, worker_vms=vms,
    )
    kinds = [(e.op, e.image_key) for e in diff]
    assert ("REMOVE_VM", "cpu-free") in kinds
    assert ("REMOVE_IMAGE", "cpu-free") in kinds


def test_size_bucket_isolation() -> None:
    # Two different (cpu, mem) shapes for the same image.
    spec = _make_spec(
        PoolAssignment("w1", "cpu-free", 1, 2, 4),
        PoolAssignment("w1", "cpu-free", 1, 8, 16),
    )
    vms = [
        WorkerVMSummary(vm_id="v1", image_key="cpu-free", cpu_cores=2, memory_gb=4, state="ready"),
    ]
    diff = compute_diff(
        spec=spec, worker_id="w1", worker_images={"cpu-free"}, worker_vms=vms,
    )
    add_vms = [e for e in diff if e.op == "ADD_VM"]
    assert len(add_vms) == 1
    assert (add_vms[0].cpu_cores, add_vms[0].memory_gb) == (8, 16)

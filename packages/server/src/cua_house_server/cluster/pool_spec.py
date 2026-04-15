"""Desired pool state + reconciler loop.

The master keeps a ``ClusterPoolSpec`` — a flat list of ``PoolAssignment``
entries that say "worker X should host N instances of image Y sized Z". The
reconciler runs on a timer: it snapshots the live ``WorkerRegistry``,
computes the diff against desired, and emits ``PoolOp`` messages to the
relevant workers.

Diff computation is intentionally pure (no I/O) so it's trivial to unit-test.
The scheduler never reads desired state directly — it only ever looks at the
registry's *actual* state via ``WorkerSession.vm_summaries``, which converges
to desired after the reconciler does its job.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cua_house_server.cluster.protocol import (
    Envelope,
    PoolOp,
    PoolOpArgs,
    WorkerVMSummary,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PoolAssignment:
    worker_id: str
    image_key: str
    count: int
    vcpus: int
    memory_gb: int
    disk_gb: int = 64


@dataclass
class ClusterPoolSpec:
    """Desired pool state with optional JSON write-through persistence.

    When constructed with ``persist_path``, every ``set`` call writes the
    assignments list to disk atomically. ``load_from_disk`` is called at
    startup to restore desired state across master restarts.

    Persistence is best-effort: disk errors are logged but don't fail the
    set, because losing state is preferable to rejecting an operator request.
    """

    assignments: list[PoolAssignment] = field(default_factory=list)
    persist_path: Path | None = None

    def set(self, assignments: list[PoolAssignment]) -> None:
        self.assignments = list(assignments)
        self._persist()

    def for_worker(self, worker_id: str) -> list[PoolAssignment]:
        return [a for a in self.assignments if a.worker_id == worker_id]

    def hosts_image(self, worker_id: str, image_key: str) -> bool:
        return any(
            a.worker_id == worker_id and a.image_key == image_key
            for a in self.assignments
        )

    # ── Persistence ───────────────────────────────────────────────────

    def load_from_disk(self) -> None:
        """Populate assignments from ``persist_path`` if the file exists.

        Schema drift is handled by ignoring unknown keys; missing fields
        are left at their dataclass defaults.
        """
        if self.persist_path is None or not self.persist_path.exists():
            return
        try:
            raw = json.loads(self.persist_path.read_text())
        except Exception as exc:
            logger.warning("pool_spec load failed from %s: %s", self.persist_path, exc)
            return
        restored: list[PoolAssignment] = []
        for item in raw.get("assignments", []):
            try:
                restored.append(PoolAssignment(
                    worker_id=str(item["worker_id"]),
                    image_key=str(item["image_key"]),
                    count=int(item["count"]),
                    vcpus=int(item["vcpus"]),
                    memory_gb=int(item["memory_gb"]),
                    disk_gb=int(item.get("disk_gb", 64)),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("skipping malformed pool assignment %r: %s", item, exc)
        self.assignments = restored
        logger.info("pool_spec restored %d assignments from %s", len(restored), self.persist_path)

    def _persist(self) -> None:
        if self.persist_path is None:
            return
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"assignments": [asdict(a) for a in self.assignments]}
            tmp = self.persist_path.with_suffix(self.persist_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self.persist_path)
        except Exception:
            logger.exception("pool_spec persist to %s failed", self.persist_path)


@dataclass(slots=True)
class DiffEntry:
    """A single PoolOp the reconciler wants to emit."""

    worker_id: str
    op: str  # ADD_IMAGE | REMOVE_IMAGE | ADD_VM | REMOVE_VM
    image_key: str | None = None
    image_version: str | None = None
    vm_id: str | None = None
    vcpus: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None


def compute_diff(
    *,
    spec: ClusterPoolSpec,
    worker_id: str,
    worker_images: set[str],
    worker_vms: list[WorkerVMSummary],
) -> list[DiffEntry]:
    """Compute the minimum set of pool ops to converge one worker.

    Ordering matters: ADD_IMAGE before ADD_VM (can't start a VM without the
    template), REMOVE_VM before REMOVE_IMAGE (can't delete a template while
    VMs still use it).
    """
    entries: list[DiffEntry] = []

    desired_by_image: dict[str, list[PoolAssignment]] = {}
    for a in spec.for_worker(worker_id):
        desired_by_image.setdefault(a.image_key, []).append(a)

    actual_by_image: dict[str, list[WorkerVMSummary]] = {}
    for vm in worker_vms:
        actual_by_image.setdefault(vm.image_key, []).append(vm)

    # ADD_IMAGE for any desired image the worker doesn't hold yet.
    for image_key in desired_by_image:
        if image_key not in worker_images:
            entries.append(DiffEntry(worker_id=worker_id, op="ADD_IMAGE", image_key=image_key))

    # For each desired image, resize the pool (ADD_VM / REMOVE_VM).
    for image_key, assignments in desired_by_image.items():
        # Assignments may be split across multiple shapes on the same image;
        # treat each (vcpus, memory_gb, disk_gb) as its own bucket.
        desired_buckets: dict[tuple[int, int, int], int] = {}
        for a in assignments:
            key = (a.vcpus, a.memory_gb, a.disk_gb)
            desired_buckets[key] = desired_buckets.get(key, 0) + a.count
        actual_buckets: dict[tuple[int, int, int], list[WorkerVMSummary]] = {}
        for vm in actual_by_image.get(image_key, []):
            key = (vm.vcpus, vm.memory_gb, vm.disk_gb)
            actual_buckets.setdefault(key, []).append(vm)

        for key, want in desired_buckets.items():
            have = len(actual_buckets.get(key, []))
            for _ in range(max(0, want - have)):
                entries.append(
                    DiffEntry(
                        worker_id=worker_id,
                        op="ADD_VM",
                        image_key=image_key,
                        vcpus=key[0],
                        memory_gb=key[1],
                        disk_gb=key[2],
                    )
                )
        for key, vms in actual_buckets.items():
            want = desired_buckets.get(key, 0)
            for vm in vms[want:]:
                entries.append(
                    DiffEntry(
                        worker_id=worker_id,
                        op="REMOVE_VM",
                        image_key=image_key,
                        vm_id=vm.vm_id,
                    )
                )

    # REMOVE_VM for images that are no longer desired at all.
    for image_key, vms in actual_by_image.items():
        if image_key in desired_by_image:
            continue
        for vm in vms:
            entries.append(
                DiffEntry(
                    worker_id=worker_id,
                    op="REMOVE_VM",
                    image_key=image_key,
                    vm_id=vm.vm_id,
                )
            )

    # REMOVE_IMAGE for any image the worker holds that's no longer desired.
    for image_key in worker_images:
        if image_key not in desired_by_image:
            entries.append(
                DiffEntry(worker_id=worker_id, op="REMOVE_IMAGE", image_key=image_key)
            )

    return entries


def diff_to_envelope(entry: DiffEntry) -> Envelope:
    op = PoolOp(
        op_id=str(uuid.uuid4()),
        op=entry.op,  # type: ignore[arg-type]
        args=PoolOpArgs(
            image_key=entry.image_key,
            image_version=entry.image_version,
            vm_id=entry.vm_id,
            vcpus=entry.vcpus,
            memory_gb=entry.memory_gb,
            disk_gb=entry.disk_gb,
        ),
    )
    return Envelope(msg_id=op.op_id, payload=op.model_dump())

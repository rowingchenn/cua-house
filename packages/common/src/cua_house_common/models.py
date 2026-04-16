"""API contract models shared by cua-house server and client."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


# ── State enums ──────────────────────────────────────────────────────


class TaskState(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    READY = "ready"
    LEASED = "leased"
    RESETTING = "resetting"
    COMPLETED = "completed"
    FAILED = "failed"


class BatchState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# ── Request models ───────────────────────────────────────────────────


class TaskRequirement(BaseModel):
    class TaskDataRequest(BaseModel):
        requires_task_data: bool = False
        task_category: str | None = None
        task_tag: str | None = None
        source_relpath: str | None = None
        input_dir: str | None = None
        software_dir: str | None = None
        reference_dir: str | None = None
        remote_output_dir: str | None = None

        @model_validator(mode="after")
        def validate_task_data(self) -> "TaskRequirement.TaskDataRequest":
            if self.requires_task_data and not self.source_relpath:
                raise ValueError("task_data.source_relpath is required when requires_task_data=true")
            return self

    task_id: str
    task_path: str
    # Image catalog key (e.g. "cpu-free"), NOT the QEMU snapshot tag.
    # The QEMU snapshot tag is a shape-based internal detail managed by
    # the runtime and never exposed to clients.
    snapshot_name: str
    vcpus: int | None = None
    memory_gb: int | None = None
    disk_gb: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    task_data: TaskDataRequest | None = None
    # NOTE: os_type is intentionally NOT a client field. It is an image-static
    # property declared in images.yaml as `os_family`. Server reads it from the
    # catalog by snapshot_name. See docs/architecture/overview.md.
    #
    # NOTE: clients translate cloud-style machine types (e.g. "n2-standard-4")
    # into ``vcpus`` + ``memory_gb`` before submitting. cua-house never sees the
    # original machine_type string. See docs/architecture/cluster.md for the
    # shape-aware dispatch rules.


class BatchCreateRequest(BaseModel):
    batch_id: str | None = None
    tasks: list[TaskRequirement]

    @model_validator(mode="after")
    def validate_tasks(self) -> "BatchCreateRequest":
        if not self.tasks:
            raise ValueError("tasks must not be empty")
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task_ids must be unique within a batch")
        return self


class LeaseCompleteRequest(BaseModel):
    final_status: Literal["completed", "failed", "abandoned"] = "completed"
    details: dict[str, Any] = Field(default_factory=dict)


class BatchCancelRequest(BaseModel):
    reason: str = "batch cancelled"
    details: dict[str, Any] = Field(default_factory=dict)


# ── Response models ──────────────────────────────────────────────────


class TaskAssignment(BaseModel):
    """Where to reach a leased VM.

    `urls` maps each guest TCP port (declared in the image's `published_ports`)
    to a publicly-reachable URL routed through the cua-house reverse proxy.
    Clients pick whichever port(s) they need by integer key. The set of keys
    is image-specific and the server attaches no semantic name to any port —
    it is the client's responsibility to know what runs on each.

    `novnc_url` remains a separate field because noVNC is a container-side
    infrastructure service (not a guest port) and uses a path-prefixed
    proxy route ``/novnc/...`` rather than a per-port subdomain.
    """

    host_id: str
    lease_id: str
    slot_id: str
    snapshot_name: str
    urls: dict[int, str] = Field(default_factory=dict)
    novnc_url: str | None = None
    # In cluster mode, ``lease_endpoint`` is the base URL of the worker node
    # serving this lease's HTTP API (``/v1/leases/{id}/heartbeat`` etc.).
    # Clients POST lease-scoped operations here instead of to the master so
    # master stays out of the per-task data path. Empty in standalone mode.
    lease_endpoint: str | None = None


class TaskStatus(BaseModel):
    task_id: str
    task_path: str
    snapshot_name: str
    vcpus: int
    memory_gb: int
    disk_gb: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    task_data: TaskRequirement.TaskDataRequest | None = None
    state: TaskState
    batch_id: str
    lease_id: str | None = None
    assignment: TaskAssignment | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class BatchStatus(BaseModel):
    batch_id: str
    state: BatchState
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    tasks: list[TaskStatus]


class LeaseHeartbeatResponse(BaseModel):
    lease_id: str
    task_id: str
    expires_at: datetime


class BatchHeartbeatResponse(BaseModel):
    batch_id: str
    expires_at: datetime


class LeaseStageResponse(BaseModel):
    lease_id: str
    task_id: str
    phase: Literal["runtime", "eval"]
    skipped: bool = False
    file_count: int = 0
    bytes_staged: int = 0


# ── Config models ────────────────────────────────────────────────────


class VMPoolEntry(BaseModel):
    """Configuration for a group of identical VMs in the pool."""

    # Image catalog key (e.g. "cpu-free"), NOT the QEMU snapshot tag.
    snapshot_name: str
    count: int = 1
    vcpus: int = 4
    memory_gb: int = 8
    disk_gb: int = 64

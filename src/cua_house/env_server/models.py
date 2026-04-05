"""Request/response models for agenthle-env-server."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class TaskState(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    READY = "ready"
    LEASED = "leased"
    RESETTING = "resetting"
    COMPLETED = "completed"
    FAILED = "failed"


class SlotState(StrEnum):
    EMPTY = "empty"
    STARTING = "starting"
    READY = "ready"
    LEASED = "leased"
    RESETTING = "resetting"
    BROKEN = "broken"


class VMState(StrEnum):
    """State machine for persistent VM instances in the snapshot pool."""

    BOOTING = "booting"
    SNAPSHOTTING = "snapshotting"
    READY = "ready"
    LEASED = "leased"
    REVERTING = "reverting"
    BROKEN = "broken"


class BatchState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


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
    os_type: str | None = None
    image_key: str
    cpu_cores: int | None = Field(default=None, ge=1)
    memory_gb: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    task_data: TaskDataRequest | None = None


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


class TaskAssignment(BaseModel):
    host_id: str
    cua_url: str | None = None
    novnc_url: str | None = None
    lease_id: str
    slot_id: str
    image_key: str


class TaskStatus(BaseModel):
    task_id: str
    task_path: str
    os_type: str | None = None
    image_key: str
    cpu_cores: int
    memory_gb: int
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


class LeaseCompleteRequest(BaseModel):
    final_status: Literal["completed", "failed", "abandoned"] = "completed"
    details: dict[str, Any] = Field(default_factory=dict)


class BatchCancelRequest(BaseModel):
    reason: str = "batch cancelled"
    details: dict[str, Any] = Field(default_factory=dict)


class LeaseRecord(BaseModel):
    lease_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    slot_id: str
    expires_at: datetime
    final_status: str | None = None


class SlotRecord(BaseModel):
    slot_id: str
    image_key: str
    state: SlotState
    cpu_cores: int
    memory_gb: int
    cua_port: int | None = None
    novnc_port: int | None = None
    lease_id: str | None = None
    task_id: str | None = None
    last_used_at: datetime = Field(default_factory=utcnow)


class LeaseStageResponse(BaseModel):
    lease_id: str
    task_id: str
    phase: Literal["runtime", "eval"]
    skipped: bool = False
    file_count: int = 0
    bytes_staged: int = 0


# ── VM pool models (snapshot-based local runtime) ────────────────────


class VMPoolEntry(BaseModel):
    """Configuration for a group of identical VMs in the pool."""

    image_key: str
    count: int = 1
    cpu_cores: int | None = None  # defaults to image.default_cpu_cores
    memory_gb: int | None = None  # defaults to image.default_memory_gb


class VMRecord(BaseModel):
    """In-memory record for a persistent VM instance."""

    vm_id: str
    image_key: str
    state: VMState
    cpu_cores: int
    memory_gb: int
    container_name: str
    cua_port: int
    novnc_port: int
    qmp_port: int
    snapshot_name: str = "clean-ready"
    lease_id: str | None = None
    task_id: str | None = None
    last_used_at: datetime = Field(default_factory=utcnow)

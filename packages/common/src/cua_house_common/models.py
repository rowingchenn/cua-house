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
    os_type: str | None = None
    snapshot_name: str
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


class LeaseCompleteRequest(BaseModel):
    final_status: Literal["completed", "failed", "abandoned"] = "completed"
    details: dict[str, Any] = Field(default_factory=dict)


class BatchCancelRequest(BaseModel):
    reason: str = "batch cancelled"
    details: dict[str, Any] = Field(default_factory=dict)


# ── Response models ──────────────────────────────────────────────────


class TaskAssignment(BaseModel):
    host_id: str
    cua_url: str | None = None
    novnc_url: str | None = None
    lease_id: str
    slot_id: str
    snapshot_name: str


class TaskStatus(BaseModel):
    task_id: str
    task_path: str
    os_type: str | None = None
    snapshot_name: str
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

    snapshot_name: str
    count: int = 1
    cpu_cores: int = 4
    memory_gb: int = 8

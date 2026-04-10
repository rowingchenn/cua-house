"""Server-internal scheduler models (not part of the public API contract)."""

from __future__ import annotations

from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from cua_house_common.models import utcnow

from datetime import datetime


class VMState(StrEnum):
    """State machine for persistent VM instances in the snapshot pool."""

    BOOTING = "booting"
    SNAPSHOTTING = "snapshotting"
    READY = "ready"
    LEASED = "leased"
    REVERTING = "reverting"
    BROKEN = "broken"


class VMRecord(BaseModel):
    """In-memory record for a persistent VM instance."""

    vm_id: str
    snapshot_name: str
    state: VMState
    cpu_cores: int
    memory_gb: int
    container_name: str
    published_ports: dict[int, int] = Field(default_factory=dict)
    novnc_port: int = 0
    lease_id: str | None = None
    task_id: str | None = None
    last_used_at: datetime = Field(default_factory=utcnow)


class LeaseRecord(BaseModel):
    lease_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    slot_id: str
    expires_at: datetime
    final_status: str | None = None

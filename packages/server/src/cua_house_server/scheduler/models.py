"""Server-internal scheduler models (not part of the public API contract)."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from cua_house_common.models import utcnow


class LeaseRecord(BaseModel):
    lease_id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    expires_at: datetime
    final_status: str | None = None

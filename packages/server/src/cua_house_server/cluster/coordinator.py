"""Generic request/response correlation table for master↔worker RPCs.

The master sends an `Envelope` with an `op_id` to a worker and awaits a
reply `Envelope` whose `correlation_id` matches. This module owns the
pending-future table that the dispatcher's WS send code and the master
WS receive code use to pair requests with replies.

Used by:

* `ClusterDispatcher._try_assign` — correlates `AssignTask` ↔ `TaskBound`
  (and the master-initiated `ReleaseLease` ↔ `TaskReleased`).
* `master_ws._dispatch` — on any incoming payload that carries a
  `correlation_id`, calls `resolve(correlation_id, payload)`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingOp:
    op_id: str
    worker_id: str
    created_at: float
    future: asyncio.Future  # resolved value is kind-specific


class RpcCoordinator:
    """In-flight master→worker RPC correlation."""

    def __init__(self, default_timeout_s: float = 600.0) -> None:
        self._pending: dict[str, PendingOp] = {}
        self._lock = asyncio.Lock()
        self._default_timeout_s = default_timeout_s

    async def issue(self, worker_id: str) -> PendingOp:
        op_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        pending = PendingOp(
            op_id=op_id,
            worker_id=worker_id,
            created_at=time.monotonic(),
            future=loop.create_future(),
        )
        async with self._lock:
            self._pending[op_id] = pending
        return pending

    async def resolve(self, op_id: str, value: Any) -> None:
        async with self._lock:
            pending = self._pending.pop(op_id, None)
        if pending is None:
            logger.debug("Dropping reply for unknown op_id %s", op_id)
            return
        if not pending.future.done():
            pending.future.set_result(value)

    async def cancel_worker(self, worker_id: str) -> None:
        """Fail all pending ops for a worker that just went offline."""
        async with self._lock:
            stale = [
                op_id for op_id, p in self._pending.items()
                if p.worker_id == worker_id
            ]
            for op_id in stale:
                pending = self._pending.pop(op_id)
                if not pending.future.done():
                    pending.future.set_exception(
                        ConnectionError(f"worker {worker_id} disconnected")
                    )

    async def await_result(
        self,
        pending: PendingOp,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        timeout = timeout_s if timeout_s is not None else self._default_timeout_s
        try:
            return await asyncio.wait_for(pending.future, timeout=timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                self._pending.pop(pending.op_id, None)
            raise



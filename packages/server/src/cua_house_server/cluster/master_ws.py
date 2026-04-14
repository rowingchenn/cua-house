"""FastAPI WebSocket endpoint for worker ↔ master traffic.

Mounted by the app factory at ``ClusterConfig.master_bind_path`` when the
process is running in master mode. Handles the single long-lived WS per
worker: auth → Register → pump Heartbeat/VMStateUpdate/PoolOpResult/etc.

Message routing lives in this module; scheduling decisions (i.e. which
worker to target for a given task) live in the Phase 2e reconciler and the
scheduler's ``WorkerSlotProvider``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from pydantic import TypeAdapter, ValidationError

from cua_house_server.cluster.protocol import (
    Envelope,
    Heartbeat,
    PoolOpResult,
    Register,
    TaskBound,
    TaskCompleted,
    TaskPhaseResult,
    TaskReleased,
    VMStateUpdate,
    WorkerToMaster,
)
from cua_house_server.cluster.reconciler import PoolOpCoordinator
from cua_house_server.cluster.registry import WorkerRegistry

if False:  # TYPE_CHECKING
    from cua_house_server.cluster.dispatcher import ClusterDispatcher

logger = logging.getLogger(__name__)

_WorkerToMasterAdapter = TypeAdapter(WorkerToMaster)


def build_cluster_router(
    *,
    registry: WorkerRegistry,
    coordinator: PoolOpCoordinator | None = None,
    dispatcher: "ClusterDispatcher | None" = None,
    expected_token: str | None,
    path: str = "/v1/cluster/ws",
) -> APIRouter:
    """Return a FastAPI router exposing the master WS endpoint."""

    router = APIRouter()

    @router.websocket(path)
    async def cluster_ws(ws: WebSocket) -> None:
        # Auth: worker must send the cluster join token as a bearer header or
        # as a ``?token=`` query param (the latter is for environments where
        # clients can't set headers on WS upgrades).
        if expected_token is not None:
            provided = _extract_token(ws)
            if provided != expected_token:
                await ws.close(code=status.WS_1008_POLICY_VIOLATION)
                return

        await ws.accept()
        session = None
        worker_id: str | None = None
        try:
            # First frame must be Register (wrapped in an Envelope).
            first = await ws.receive_json()
            envelope = Envelope.model_validate(first)
            msg = _parse_worker_message(envelope.payload)
            if not isinstance(msg, Register):
                await ws.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            worker_id = msg.worker_id
            session = await registry.register(
                worker_id=msg.worker_id,
                runtime_version=msg.runtime_version,
                capacity=msg.capacity,
                hosted_images=msg.hosted_images,
                ws=ws,
            )

            while True:
                raw = await ws.receive_json()
                try:
                    envelope = Envelope.model_validate(raw)
                    payload = _parse_worker_message(envelope.payload)
                except ValidationError as exc:
                    logger.warning("Worker %s sent invalid msg: %s", worker_id, exc)
                    continue
                await _dispatch(registry, coordinator, dispatcher, worker_id, envelope, payload)
        except WebSocketDisconnect:
            logger.info("Worker %s disconnected", worker_id)
        except Exception:  # pragma: no cover - unexpected errors
            logger.exception("Worker %s WS loop crashed", worker_id)
        finally:
            if worker_id is not None:
                await registry.mark_offline(worker_id)
                if coordinator is not None:
                    await coordinator.cancel_worker(worker_id)
                if dispatcher is not None:
                    await dispatcher.handle_worker_disconnect(worker_id)

    return router


def _extract_token(ws: WebSocket) -> str | None:
    auth = ws.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return ws.query_params.get("token")


def _parse_worker_message(raw: dict) -> WorkerToMaster:
    return _WorkerToMasterAdapter.validate_python(raw)


async def _dispatch(
    registry: WorkerRegistry,
    coordinator: PoolOpCoordinator | None,
    dispatcher: "ClusterDispatcher | None",
    worker_id: str,
    envelope: Envelope,
    payload: WorkerToMaster,
) -> None:
    if isinstance(payload, Heartbeat):
        await registry.apply_heartbeat(
            worker_id,
            load_cpu=payload.load_cpu,
            load_memory=payload.load_memory,
            vm_summaries=payload.vm_summaries,
        )
    elif isinstance(payload, VMStateUpdate):
        await registry.apply_vm_state_update(
            worker_id,
            vm_id=payload.vm_id,
            state=payload.state,
            lease_id=payload.lease_id,
        )
    elif isinstance(payload, (PoolOpResult, TaskBound, TaskReleased)):
        # Request/response RPCs correlate via envelope.correlation_id so a
        # single coordinator services pool ops AND assign-task uniformly.
        if coordinator is not None and envelope.correlation_id is not None:
            await coordinator.resolve(envelope.correlation_id, payload)
        elif isinstance(payload, PoolOpResult):
            logger.info(
                "Unrouted PoolOp %s from %s: ok=%s",
                payload.op_id, worker_id, payload.ok,
            )
    elif isinstance(payload, TaskCompleted):
        if dispatcher is not None:
            await dispatcher.handle_task_completed(worker_id, payload)
    elif isinstance(payload, TaskPhaseResult):
        logger.info(
            "TaskPhase %s from %s: phase=%s ok=%s",
            payload.lease_id, worker_id, payload.phase, payload.ok,
        )
    # Register is only valid as the first frame and is handled above.


__all__ = ["Envelope", "build_cluster_router"]

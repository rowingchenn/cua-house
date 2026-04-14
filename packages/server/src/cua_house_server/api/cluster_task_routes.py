"""Master-side batch admission routes.

The master in cluster mode is **batch-scoped only** — it takes in batch
submissions, hands back ``TaskAssignment`` objects pointing at worker
nodes, and aggregates batch state from ``TaskCompleted`` events that
workers push over WS.

Lease-scoped endpoints (``/v1/leases/{id}/heartbeat``, ``/complete``,
``/stage-runtime``, ``/stage-eval``) deliberately do NOT live here. The
client reads ``TaskAssignment.lease_endpoint`` and sends those directly
to the worker that owns the lease; the worker handles them with its
existing ``api/routes.py`` scheduler routes. Master stays out of the
per-task data path.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from cua_house_common.models import (
    BatchCancelRequest,
    BatchCreateRequest,
)
from cua_house_server.api.auth import require_auth
from cua_house_server.cluster.dispatcher import ClusterDispatcher


def build_cluster_task_router() -> APIRouter:
    router = APIRouter()

    def _dispatcher(request: Request) -> ClusterDispatcher:
        dispatcher = getattr(request.app.state, "cluster_dispatcher", None)
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="cluster dispatcher not ready")
        return dispatcher

    def _token(request: Request) -> str | None:
        return request.app.state.auth_token

    @router.get("/healthz")
    async def healthz(request: Request, authorization: str | None = Header(default=None)):
        require_auth(authorization, expected_token=_token(request))
        return {"status": "ok", "mode": "master"}

    @router.post("/v1/batches")
    async def submit_batch(
        request: Request,
        payload: BatchCreateRequest,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization, expected_token=_token(request))
        try:
            return await _dispatcher(request).submit_batch(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/v1/batches/{batch_id}")
    async def get_batch(
        request: Request,
        batch_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization, expected_token=_token(request))
        try:
            return await _dispatcher(request).get_batch(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/v1/batches/{batch_id}/heartbeat")
    async def heartbeat_batch(
        request: Request,
        batch_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization, expected_token=_token(request))
        try:
            return await _dispatcher(request).heartbeat_batch(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/v1/batches/{batch_id}/cancel")
    async def cancel_batch(
        request: Request,
        batch_id: str,
        payload: BatchCancelRequest,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization, expected_token=_token(request))
        try:
            return await _dispatcher(request).cancel_batch(
                batch_id, reason=payload.reason, details=payload.details,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/v1/tasks/{task_id}")
    async def get_task(
        request: Request,
        task_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization, expected_token=_token(request))
        try:
            return await _dispatcher(request).get_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return router

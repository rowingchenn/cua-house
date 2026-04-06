"""API route handlers for cua-house-server."""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request

from cua_house_common.models import BatchCancelRequest, BatchCreateRequest, LeaseCompleteRequest
from cua_house_server.api.auth import require_auth
from cua_house_server.scheduler.core import EnvScheduler

router = APIRouter()


def _get_scheduler(request: Request) -> EnvScheduler:
    return request.app.state.scheduler


def _get_token(request: Request) -> str | None:
    return request.app.state.auth_token


def _lease_host(scheduler: EnvScheduler, lease_id: str) -> str:
    return f"lease-{lease_id}.{scheduler.host_config.public_base_host}"


def _external_url(request: Request, scheduler: EnvScheduler, lease_id: str, *, novnc: bool) -> str:
    scheme = request.url.scheme
    host = _lease_host(scheduler, lease_id)
    port = request.url.port
    if port is None:
        default_port = 443 if scheme == "https" else 80
        port = default_port
    hostport = host if (scheme == "http" and port == 80) or (scheme == "https" and port == 443) else f"{host}:{port}"
    suffix = "/novnc/" if novnc else ""
    return f"{scheme}://{hostport}{suffix}"


async def _present_task(request: Request, scheduler: EnvScheduler, task_id: str):
    try:
        task = await scheduler.get_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if task.assignment is not None:
        task.assignment.cua_url = _external_url(request, scheduler, task.assignment.lease_id, novnc=False)
        task.assignment.novnc_url = _external_url(request, scheduler, task.assignment.lease_id, novnc=True)
    return task


async def _present_batch(request: Request, scheduler: EnvScheduler, batch_id: str):
    try:
        batch = await scheduler.get_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    for task in batch.tasks:
        if task.assignment is not None:
            task.assignment.cua_url = _external_url(request, scheduler, task.assignment.lease_id, novnc=False)
            task.assignment.novnc_url = _external_url(request, scheduler, task.assignment.lease_id, novnc=True)
    return batch


@router.get("/healthz")
async def healthz(request: Request, authorization: str | None = Header(default=None)) -> dict[str, str]:
    require_auth(authorization, expected_token=_get_token(request))
    return {"status": "ok"}


@router.get("/v1/vms")
async def list_vms(request: Request, authorization: str | None = Header(default=None)):
    """List VM pool instances and their current state."""
    require_auth(authorization, expected_token=_get_token(request))
    sched = _get_scheduler(request)
    return [vm.model_dump() for vm in sched._vms.values()]


@router.post("/v1/batches")
async def submit_batch(
    request: Request,
    payload: BatchCreateRequest,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization, expected_token=_get_token(request))
    scheduler = _get_scheduler(request)
    try:
        batch = await scheduler.submit_batch(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await _present_batch(request, scheduler, batch.batch_id)


@router.get("/v1/batches/{batch_id}")
async def get_batch(
    request: Request,
    batch_id: str,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization, expected_token=_get_token(request))
    return await _present_batch(request, _get_scheduler(request), batch_id)


@router.post("/v1/batches/{batch_id}/heartbeat")
async def heartbeat_batch(request: Request, batch_id: str, authorization: str | None = Header(default=None)):
    require_auth(authorization, expected_token=_get_token(request))
    try:
        return await _get_scheduler(request).heartbeat_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/v1/batches/{batch_id}/cancel")
async def cancel_batch(
    request: Request,
    batch_id: str,
    payload: BatchCancelRequest,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization, expected_token=_get_token(request))
    scheduler = _get_scheduler(request)
    try:
        await scheduler.cancel_batch(batch_id, reason=payload.reason, details=payload.details)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await _present_batch(request, scheduler, batch_id)


@router.get("/v1/tasks/{task_id}")
async def get_task(
    request: Request,
    task_id: str,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization, expected_token=_get_token(request))
    return await _present_task(request, _get_scheduler(request), task_id)


@router.post("/v1/leases/{lease_id}/heartbeat")
async def heartbeat(request: Request, lease_id: str, authorization: str | None = Header(default=None)):
    require_auth(authorization, expected_token=_get_token(request))
    try:
        return await _get_scheduler(request).heartbeat(lease_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/v1/leases/{lease_id}/complete")
async def complete_lease(
    request: Request,
    lease_id: str,
    payload: LeaseCompleteRequest,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization, expected_token=_get_token(request))
    try:
        return await _get_scheduler(request).complete(lease_id, final_status=payload.final_status, details=payload.details)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/v1/leases/{lease_id}/stage-runtime")
async def stage_runtime(
    request: Request,
    lease_id: str,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization, expected_token=_get_token(request))
    try:
        return await _get_scheduler(request).stage_runtime(lease_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/v1/leases/{lease_id}/stage-eval")
async def stage_eval(
    request: Request,
    lease_id: str,
    authorization: str | None = Header(default=None),
):
    require_auth(authorization, expected_token=_get_token(request))
    try:
        return await _get_scheduler(request).stage_eval(lease_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

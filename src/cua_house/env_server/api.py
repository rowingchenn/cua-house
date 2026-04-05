"""FastAPI application for agenthle-env-server."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from cua_house.common.events import JsonlEventLogger

from .models import BatchCancelRequest, BatchCreateRequest, LeaseCompleteRequest
from .runtime import DockerQemuRuntime, GCPVMRuntime, load_host_runtime_config, load_image_catalog
from .scheduler import EnvScheduler

logger = logging.getLogger(__name__)


def create_app(*, host_config_path: str | Path, image_catalog_path: str | Path) -> FastAPI:
    host_config = load_host_runtime_config(host_config_path)
    images = load_image_catalog(image_catalog_path)
    event_logger = JsonlEventLogger(Path(host_config.runtime_root) / "events.jsonl", component="env_server")

    # Build runtimes based on enabled images
    runtimes: dict[str, DockerQemuRuntime | GCPVMRuntime] = {}
    has_local = any(img.runtime_mode == "local" and img.enabled for img in images.values())
    has_gcp = any(img.runtime_mode == "gcp" and img.enabled for img in images.values())
    if has_local:
        runtimes["local"] = DockerQemuRuntime(host_config, event_logger=event_logger)
    if has_gcp:
        gcloud_path = os.environ.get("GCLOUD_PATH", "gcloud")
        gcp_runtime = GCPVMRuntime(host_config, event_logger=event_logger, gcloud_path=gcloud_path)
        gcp_runtime.set_images(images)
        runtimes["gcp"] = gcp_runtime

    # Default runtime for backwards compat
    default_runtime = runtimes.get("local") or DockerQemuRuntime(host_config, event_logger=event_logger)
    scheduler = EnvScheduler(
        runtime=default_runtime,
        host_config=host_config,
        images=images,
        event_logger=event_logger,
        runtimes=runtimes,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.scheduler = scheduler
        app.state.auth_token = os.environ.get("AGENTHLE_TOKEN")
        app.state.proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=60.0),
            follow_redirects=True,
        )
        await scheduler.start()
        try:
            yield
        finally:
            await scheduler.shutdown()
            await app.state.proxy_client.aclose()

    app = FastAPI(title="agenthle-env-server", version="0.1.0", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.state.auth_token = os.environ.get("AGENTHLE_TOKEN")

    def require_auth(authorization: str | None) -> None:
        expected = app.state.auth_token
        if not expected:
            return
        if authorization != f"Bearer {expected}":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def _lease_id_from_host(host_header: str | None) -> str | None:
        if not host_header:
            return None
        host = host_header.split(":", 1)[0].lower()
        suffix = f".{scheduler.host_config.public_base_host.lower()}"
        if not host.endswith(suffix):
            return None
        prefix = host[: -len(suffix)]
        if not prefix.startswith("lease-"):
            return None
        return prefix[len("lease-") :]

    def _lease_host(lease_id: str) -> str:
        return f"lease-{lease_id}.{scheduler.host_config.public_base_host}"

    def _external_url(request: Request, lease_id: str, *, novnc: bool) -> str:
        scheme = request.url.scheme
        host = _lease_host(lease_id)
        port = request.url.port
        if port is None:
            default_port = 443 if scheme == "https" else 80
            port = default_port
        hostport = host if (scheme == "http" and port == 80) or (scheme == "https" and port == 443) else f"{host}:{port}"
        suffix = "/novnc/" if novnc else ""
        return f"{scheme}://{hostport}{suffix}"

    async def _present_task(request: Request, task_id: str):
        try:
            task = await scheduler.get_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if task.assignment is not None:
            task.assignment.cua_url = _external_url(request, task.assignment.lease_id, novnc=False)
            task.assignment.novnc_url = _external_url(request, task.assignment.lease_id, novnc=True)
        return task

    async def _present_batch(request: Request, batch_id: str):
        try:
            batch = await scheduler.get_batch(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        for task in batch.tasks:
            if task.assignment is not None:
                task.assignment.cua_url = _external_url(request, task.assignment.lease_id, novnc=False)
                task.assignment.novnc_url = _external_url(request, task.assignment.lease_id, novnc=True)
        return batch

    async def _resolve_proxy_target(lease_id: str, *, novnc: bool) -> str:
        try:
            cua_url, novnc_url = await scheduler.resolve_proxy_targets(lease_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return novnc_url if novnc else cua_url

    async def _proxy_http(request: Request, lease_id: str, *, novnc: bool):
        target_base_url = await _resolve_proxy_target(lease_id, novnc=novnc)
        incoming_path = request.url.path
        if novnc:
            prefix = "/novnc"
            target_path = incoming_path[len(prefix) :] or "/"
        else:
            target_path = incoming_path or "/"
        target = urlsplit(target_base_url)
        query = request.url.query
        target_url = urlunsplit((target.scheme, target.netloc, target_path, query, ""))
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length", "authorization"}
        }
        body = await request.body()
        upstream_request = app.state.proxy_client.build_request(
            request.method,
            target_url,
            headers=headers,
            content=body,
        )
        upstream = await app.state.proxy_client.send(upstream_request, stream=True)
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in {"content-length", "transfer-encoding", "connection"}
        }
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream.aclose),
        )

    async def _proxy_websocket(websocket: WebSocket, lease_id: str, *, novnc: bool) -> None:
        target_base_url = await _resolve_proxy_target(lease_id, novnc=novnc)
        incoming_path = websocket.url.path
        if novnc:
            prefix = "/novnc"
            target_path = incoming_path[len(prefix) :] or "/"
        else:
            target_path = incoming_path or "/"
        target = urlsplit(target_base_url)
        ws_scheme = "wss" if target.scheme == "https" else "ws"
        query = websocket.url.query
        target_url = urlunsplit((ws_scheme, target.netloc, target_path, query, ""))

        await websocket.accept()
        try:
            async with websockets.connect(target_url, max_size=None) as upstream:
                async def client_to_upstream() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        if message.get("text") is not None:
                            await upstream.send(message["text"])
                        elif message.get("bytes") is not None:
                            await upstream.send(message["bytes"])

                async def upstream_to_client() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                done, pending = await asyncio.wait(
                    {
                        asyncio.create_task(client_to_upstream()),
                        asyncio.create_task(upstream_to_client()),
                    },
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    exc = task.exception()
                    if exc is not None:
                        raise exc
        except WebSocketDisconnect:
            return
        except Exception as exc:
            logger.warning("WebSocket proxy failed for lease %s: %s", lease_id, exc)
            await websocket.close(code=1011)

    @app.get("/healthz")
    async def healthz(authorization: str | None = Header(default=None)) -> dict[str, str]:
        require_auth(authorization)
        return {"status": "ok"}

    @app.get("/v1/vms")
    async def list_vms(authorization: str | None = Header(default=None)):
        """List VM pool instances and their current state."""
        require_auth(authorization)
        sched: EnvScheduler = app.state.scheduler
        return [vm.model_dump() for vm in sched._vms.values()]

    @app.post("/v1/batches")
    async def submit_batch(
        request: Request,
        payload: BatchCreateRequest,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization)
        try:
            batch = await scheduler.submit_batch(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await _present_batch(request, batch.batch_id)

    @app.get("/v1/batches/{batch_id}")
    async def get_batch(
        request: Request,
        batch_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization)
        return await _present_batch(request, batch_id)

    @app.post("/v1/batches/{batch_id}/heartbeat")
    async def heartbeat_batch(batch_id: str, authorization: str | None = Header(default=None)):
        require_auth(authorization)
        try:
            return await scheduler.heartbeat_batch(batch_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/batches/{batch_id}/cancel")
    async def cancel_batch(
        request: Request,
        batch_id: str,
        payload: BatchCancelRequest,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization)
        try:
            await scheduler.cancel_batch(batch_id, reason=payload.reason, details=payload.details)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return await _present_batch(request, batch_id)

    @app.get("/v1/tasks/{task_id}")
    async def get_task(
        request: Request,
        task_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization)
        return await _present_task(request, task_id)

    @app.post("/v1/leases/{lease_id}/heartbeat")
    async def heartbeat(lease_id: str, authorization: str | None = Header(default=None)):
        require_auth(authorization)
        try:
            return await scheduler.heartbeat(lease_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/leases/{lease_id}/complete")
    async def complete_lease(
        lease_id: str,
        request: LeaseCompleteRequest,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization)
        try:
            return await scheduler.complete(lease_id, final_status=request.final_status, details=request.details)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/leases/{lease_id}/stage-runtime")
    async def stage_runtime(
        lease_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization)
        try:
            return await scheduler.stage_runtime(lease_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/leases/{lease_id}/stage-eval")
    async def stage_eval(
        lease_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_auth(authorization)
        try:
            return await scheduler.stage_eval(lease_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def proxy_http(request: Request, path: str):
        lease_id = _lease_id_from_host(request.headers.get("host"))
        if lease_id is None:
            raise HTTPException(status_code=404, detail="not found")
        return await _proxy_http(request, lease_id, novnc=request.url.path.startswith("/novnc"))

    @app.websocket("/{path:path}")
    async def proxy_websocket(websocket: WebSocket, path: str):
        lease_id = _lease_id_from_host(websocket.headers.get("host"))
        if lease_id is None:
            await websocket.close(code=1008)
            return
        await _proxy_websocket(websocket, lease_id, novnc=websocket.url.path.startswith("/novnc"))

    return app

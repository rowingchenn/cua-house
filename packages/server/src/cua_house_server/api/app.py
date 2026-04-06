"""FastAPI application factory for cua-house-server."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket

from cua_house_server.config.loader import load_host_runtime_config, load_image_catalog
from cua_house_server.runtimes.qemu import DockerQemuRuntime
from cua_house_server.runtimes.gcp import GCPVMRuntime
from cua_house_server.scheduler.core import EnvScheduler
from cua_house_common.events import JsonlEventLogger

from cua_house_server.api.routes import router as api_router
from cua_house_server.api.proxy import lease_id_from_host, proxy_http_handler, proxy_websocket_handler

logger = logging.getLogger(__name__)


def create_app(*, host_config_path: str | Path, image_catalog_path: str | Path) -> FastAPI:
    host_config = load_host_runtime_config(host_config_path)
    images = load_image_catalog(image_catalog_path)
    event_logger = JsonlEventLogger(Path(host_config.runtime_root) / "events.jsonl", component="env_server")

    # Build runtimes based on enabled images
    runtimes: dict[str, DockerQemuRuntime | GCPVMRuntime] = {}
    has_local = any(img.local is not None and img.enabled for img in images.values())
    has_gcp = any(img.gcp is not None and img.enabled for img in images.values())
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
        # Support new env var with fallback to legacy
        app.state.auth_token = os.environ.get("CUA_HOUSE_TOKEN")
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

    app = FastAPI(title="cua-house-server", version="0.1.0", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.state.auth_token = os.environ.get("CUA_HOUSE_TOKEN")

    # Register API routes
    app.include_router(api_router)

    # Register proxy catch-all routes (must be last)
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def proxy_http(request: Request, path: str):
        lease_id = lease_id_from_host(
            request.headers.get("host"),
            public_base_host=scheduler.host_config.public_base_host,
        )
        if lease_id is None:
            raise HTTPException(status_code=404, detail="not found")
        return await proxy_http_handler(request, lease_id, novnc=request.url.path.startswith("/novnc"))

    @app.websocket("/{path:path}")
    async def proxy_websocket(websocket: WebSocket, path: str):
        lease_id = lease_id_from_host(
            websocket.headers.get("host"),
            public_base_host=scheduler.host_config.public_base_host,
        )
        if lease_id is None:
            await websocket.close(code=1008)
            return
        await proxy_websocket_handler(websocket, lease_id, novnc=websocket.url.path.startswith("/novnc"))

    return app

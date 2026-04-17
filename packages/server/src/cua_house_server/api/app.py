"""FastAPI application factory for cua-house-server."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket

from cua_house_server.config.loader import ClusterConfig, load_host_runtime_config, load_image_catalog
from cua_house_server.runtimes.qemu import DockerQemuRuntime
from cua_house_server.runtimes.gcp import GCPVMRuntime
from cua_house_server.scheduler.core import EnvScheduler
from cua_house_common.events import JsonlEventLogger

from cua_house_server.api.routes import router as api_router
from cua_house_server.api.proxy import parse_proxy_host, proxy_http_handler, proxy_websocket_handler
from cua_house_server.api.cluster_routes import build_cluster_api_router
from cua_house_server.api.cluster_lease_proxy import build_cluster_lease_proxy_router
from cua_house_server.api.cluster_task_routes import build_cluster_task_router
from cua_house_server.cluster.dispatcher import ClusterDispatcher
from cua_house_server.cluster.master_ws import build_cluster_router
from cua_house_server.cluster.pool_spec import ClusterPoolSpec
from cua_house_server.cluster.reconciler import PoolOpCoordinator, PoolReconciler
from cua_house_server.cluster.registry import WorkerRegistry
from cua_house_server.cluster.worker_client import WorkerClusterClient

logger = logging.getLogger(__name__)


def _default_worker_port() -> int:
    """Worker's public HTTP port for lease API.

    Pulled from CUA_HOUSE_WORKER_PORT if set, otherwise the default 8787
    that matches the CLI default. Operators binding a non-default port
    must set this env var so the lease_endpoint URL is accurate.
    """
    return int(os.environ.get("CUA_HOUSE_WORKER_PORT", "8787"))


def create_app(
    *,
    host_config_path: str | Path,
    image_catalog_path: str | Path,
    mode_override: str | None = None,
    master_url_override: str | None = None,
    worker_id_override: str | None = None,
) -> FastAPI:
    host_config = load_host_runtime_config(host_config_path)
    images = load_image_catalog(image_catalog_path)
    event_logger = JsonlEventLogger(Path(host_config.runtime_root) / "events.jsonl", component="env_server")

    # Apply CLI overrides for cluster mode.
    if mode_override is not None:
        host_config.mode = mode_override
    if host_config.mode != "standalone" and host_config.cluster is None:
        host_config.cluster = ClusterConfig()
    if host_config.cluster is not None:
        if master_url_override is not None:
            host_config.cluster.master_url = master_url_override
        if worker_id_override is not None:
            host_config.cluster.worker_id = worker_id_override
        if host_config.cluster.join_token is None:
            host_config.cluster.join_token = os.environ.get("CUA_HOUSE_CLUSTER_JOIN_TOKEN")

    mode = host_config.mode

    # Build runtimes based on enabled images + mode. Master never owns a
    # local runtime (workers replace that role). Worker never owns GCP
    # (GCP overflow lives on master only).
    runtimes: dict[str, DockerQemuRuntime | GCPVMRuntime] = {}
    has_local = any(img.local is not None and img.enabled for img in images.values())
    has_gcp = any(img.gcp is not None and img.enabled for img in images.values())
    if has_local and mode != "master":
        runtimes["local"] = DockerQemuRuntime(host_config, event_logger=event_logger)
    if has_gcp and mode != "worker":
        gcloud_path = os.environ.get("GCLOUD_PATH", "gcloud")
        gcp_runtime = GCPVMRuntime(host_config, event_logger=event_logger, gcloud_path=gcloud_path)
        gcp_runtime.set_images(images)
        runtimes["gcp"] = gcp_runtime

    # Default runtime for backwards compat with EnvScheduler's required arg.
    # Master mode uses a throwaway runtime that never gets initialize_pool'd.
    default_runtime = runtimes.get("local") or DockerQemuRuntime(host_config, event_logger=event_logger)
    scheduler = EnvScheduler(
        runtime=default_runtime,
        host_config=host_config,
        images=images,
        event_logger=event_logger,
        runtimes=runtimes,
    )

    # Cluster state (master) / client (worker). None in standalone mode.
    worker_registry: WorkerRegistry | None = None
    pool_spec: ClusterPoolSpec | None = None
    pool_coordinator: PoolOpCoordinator | None = None
    pool_reconciler: PoolReconciler | None = None
    cluster_dispatcher: ClusterDispatcher | None = None
    worker_client: WorkerClusterClient | None = None
    if mode == "master":
        ttl = host_config.cluster.heartbeat_ttl_s if host_config.cluster else 30.0
        worker_registry = WorkerRegistry(heartbeat_ttl_s=ttl)
        pool_spec_path = Path(host_config.runtime_root) / "cluster-pool-spec.json"
        pool_spec = ClusterPoolSpec(persist_path=pool_spec_path)
        pool_spec.load_from_disk()
        pool_coordinator = PoolOpCoordinator()
        cluster_dispatcher = ClusterDispatcher(
            host_config=host_config,
            images=images,
            registry=worker_registry,
            coordinator=pool_coordinator,
        )
        pool_reconciler = PoolReconciler(
            registry=worker_registry,
            pool_spec=pool_spec,
            coordinator=pool_coordinator,
            on_worker_evicted=cluster_dispatcher.handle_worker_disconnect,
        )
    elif mode == "worker":
        assert host_config.cluster is not None
        local_rt = runtimes.get("local")
        if not isinstance(local_rt, DockerQemuRuntime):
            raise RuntimeError("worker mode requires an enabled local runtime")
        # Fail-fast on task_data_root: a missing / unwritable mount means
        # any lease's stage-runtime will blow up mid-task. Catch it now.
        if host_config.task_data_root is not None:
            td = host_config.task_data_root
            if not td.exists():
                raise RuntimeError(
                    f"worker mode: task_data_root {td} does not exist. "
                    f"Did you mount the shared task-data disk?"
                )
            if not os.access(td, os.W_OK):
                raise RuntimeError(
                    f"worker mode: task_data_root {td} is not writable by current user. "
                    f"Check overlayfs upper layer permissions."
                )
        # Expose the image catalog to the worker client so PoolOp.ADD_IMAGE
        # can look up specs by key. This is a transitional hook; once master
        # ships ImageSpec payloads over the wire (Phase 5), this goes away.
        setattr(local_rt, "_cluster_catalog", images)
        # Construct the public lease endpoint URL. Prefer cluster config
        # override; fall back to host_external_ip + default worker port.
        public_host = host_config.cluster.worker_public_host or host_config.host_external_ip
        public_port = host_config.cluster.worker_public_port
        lease_endpoint_url = f"http://{public_host}:{public_port}"
        worker_client = WorkerClusterClient(
            host_config=host_config,
            cluster=host_config.cluster,
            runtime=local_rt,
            scheduler=scheduler,
            lease_endpoint=lease_endpoint_url,
            public_host=public_host,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.scheduler = scheduler
        app.state.worker_registry = worker_registry
        app.state.pool_spec = pool_spec
        app.state.cluster_dispatcher = cluster_dispatcher
        # Support new env var with fallback to legacy
        app.state.auth_token = os.environ.get("CUA_HOUSE_TOKEN")
        app.state.proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=60.0),
            follow_redirects=True,
        )
        if mode != "master":
            await scheduler.start()
        if worker_client is not None:
            await worker_client.start()
        if cluster_dispatcher is not None:
            await cluster_dispatcher.start()
        if pool_reconciler is not None:
            await pool_reconciler.start()
        try:
            yield
        finally:
            if pool_reconciler is not None:
                await pool_reconciler.stop()
            if cluster_dispatcher is not None:
                await cluster_dispatcher.shutdown()
            if worker_client is not None:
                await worker_client.stop()
            if mode != "master":
                await scheduler.shutdown()
            await app.state.proxy_client.aclose()

    app = FastAPI(title="cua-house-server", version="0.1.0", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.state.worker_registry = worker_registry
    app.state.pool_spec = pool_spec
    app.state.cluster_dispatcher = cluster_dispatcher
    app.state.auth_token = os.environ.get("CUA_HOUSE_TOKEN")

    # Client-facing task routes:
    # - standalone: EnvScheduler-backed router (legacy single-node behavior)
    # - worker:     same EnvScheduler router — workers ARE the data plane
    #               and serve lease heartbeat / stage / complete directly
    #               to clients on their public HTTP API
    # - master:     slim batch-only router (no /v1/leases at all)
    if mode in {"standalone", "worker"}:
        app.include_router(api_router)
    elif mode == "master":
        app.include_router(build_cluster_task_router())
        # Lease proxy: forwards /v1/leases/{id}/{heartbeat,complete,stage-*}
        # from master to the owning worker. Keeps legacy clients (that use
        # a single CUA_HOUSE_SERVER_URL) working against a cluster.
        app.include_router(build_cluster_lease_proxy_router())

    # Cluster control plane (master only).
    if mode == "master":
        assert worker_registry is not None and pool_spec is not None
        cluster_token = (
            host_config.cluster.join_token if host_config.cluster else None
        )
        app.include_router(
            build_cluster_router(
                registry=worker_registry,
                coordinator=pool_coordinator,
                dispatcher=cluster_dispatcher,
                expected_token=cluster_token,
                path=(host_config.cluster.master_bind_path if host_config.cluster else "/v1/cluster/ws"),
            )
        )
        assert cluster_dispatcher is not None
        app.include_router(
            build_cluster_api_router(
                registry=worker_registry,
                pool_spec=pool_spec,
                dispatcher=cluster_dispatcher,
            )
        )

    # Register proxy catch-all routes (must be last)
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def proxy_http(request: Request, path: str):
        parsed = parse_proxy_host(
            request.headers.get("host"),
            base_host=scheduler.host_config.public_base_host,
        )
        if parsed is None:
            raise HTTPException(status_code=404, detail="not found")
        service, lease_id = parsed
        return await proxy_http_handler(request, lease_id, service)

    @app.websocket("/{path:path}")
    async def proxy_websocket(websocket: WebSocket, path: str):
        parsed = parse_proxy_host(
            websocket.headers.get("host"),
            base_host=scheduler.host_config.public_base_host,
        )
        if parsed is None:
            await websocket.close(code=1008)
            return
        service, lease_id = parsed
        await proxy_websocket_handler(websocket, lease_id, service)

    return app

"""Master-side HTTP proxy for lease-scoped endpoints.

The Phase 4 architecture puts the lease HTTP API on the worker. Clients
that read ``TaskAssignment.lease_endpoint`` can connect directly and skip
the master entirely — that's the fast path.

Legacy clients (including the current ``EnvServerClient`` used by
agenthle) are built with a single ``CUA_HOUSE_SERVER_URL`` pointing at
master and hit ``/v1/leases/{id}/...`` against that. This module makes
that work in cluster mode by proxying those calls to the worker that
owns the lease.

Scope:

* Only ``/v1/leases/{lease_id}/{heartbeat,complete,stage-runtime,stage-eval}``
  routes are proxied. VM service traffic (``urls[port]``) is NEVER
  proxied here — it goes directly to the worker's public VM ports and
  is much larger in volume (screenshots, RPC bursts).
* Proxy is a thin pass-through: same method, same JSON body, same
  response status/body. Headers are filtered to drop hop-by-hop fields
  and forward the ``Authorization`` bearer.
* Lease → worker mapping comes from ``ClusterDispatcher._leases``, which
  stores the assignment's ``lease_endpoint`` for every live lease.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from cua_house_server.cluster.dispatcher import ClusterDispatcher

logger = logging.getLogger(__name__)

# Hop-by-hop headers that must not be forwarded on a proxy hop.
# See RFC 7230 §6.1.
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
})


def build_cluster_lease_proxy_router() -> APIRouter:
    router = APIRouter()

    async def _lookup_endpoint(request: Request, lease_id: str) -> str:
        dispatcher: ClusterDispatcher | None = getattr(
            request.app.state, "cluster_dispatcher", None,
        )
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="cluster dispatcher not ready")
        endpoint = await dispatcher.lookup_lease_endpoint(lease_id)
        if endpoint is None:
            raise HTTPException(status_code=404, detail=f"unknown lease_id: {lease_id}")
        return endpoint

    async def _forward(
        request: Request,
        lease_id: str,
        suffix: str,
    ) -> Response:
        endpoint = await _lookup_endpoint(request, lease_id)
        target = f"{endpoint.rstrip('/')}/v1/leases/{lease_id}/{suffix}"
        body = await request.body()
        upstream_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP_HEADERS
        }
        client: httpx.AsyncClient = request.app.state.proxy_client
        try:
            upstream = await client.request(
                request.method,
                target,
                content=body,
                headers=upstream_headers,
                timeout=httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=30.0),
            )
        except httpx.RequestError as exc:
            logger.warning("lease proxy upstream failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"worker unreachable: {exc}") from exc

        filtered = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in _HOP_BY_HOP_HEADERS
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=filtered,
            media_type=upstream.headers.get("content-type"),
        )

    @router.post("/v1/leases/{lease_id}/heartbeat")
    async def proxy_heartbeat(request: Request, lease_id: str) -> Response:
        return await _forward(request, lease_id, "heartbeat")

    @router.post("/v1/leases/{lease_id}/complete")
    async def proxy_complete(request: Request, lease_id: str) -> Response:
        return await _forward(request, lease_id, "complete")

    @router.post("/v1/leases/{lease_id}/stage-runtime")
    async def proxy_stage_runtime(request: Request, lease_id: str) -> Response:
        return await _forward(request, lease_id, "stage-runtime")

    @router.post("/v1/leases/{lease_id}/stage-eval")
    async def proxy_stage_eval(request: Request, lease_id: str) -> Response:
        return await _forward(request, lease_id, "stage-eval")

    return router


__all__ = ["build_cluster_lease_proxy_router"]

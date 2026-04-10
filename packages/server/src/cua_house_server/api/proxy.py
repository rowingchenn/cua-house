"""Reverse proxy for routing requests to leased VM slots."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

import websockets
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from cua_house_server.scheduler.core import EnvScheduler

logger = logging.getLogger(__name__)


def parse_proxy_host(host_header: str | None, *, base_host: str) -> tuple[str, str] | None:
    """Parse ``<service>--<lease_id>.<base_host>`` from a Host header.

    Returns ``(service, lease_id)`` where *service* is ``"novnc"`` or a
    numeric port string (e.g. ``"5000"``), or ``None`` if the header does
    not match.
    """
    if not host_header:
        return None
    host = host_header.split(":", 1)[0].lower()
    suffix = f".{base_host.lower()}"
    if not host.endswith(suffix):
        return None
    prefix = host[: -len(suffix)]
    if "--" not in prefix:
        return None
    service, lease_id = prefix.split("--", 1)
    return service, lease_id


async def resolve_proxy_target(scheduler: EnvScheduler, lease_id: str, service: str) -> str:
    try:
        return await scheduler.resolve_proxy_target(lease_id, service)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def proxy_http_handler(request: Request, lease_id: str, service: str):
    scheduler: EnvScheduler = request.app.state.scheduler
    target_base_url = await resolve_proxy_target(scheduler, lease_id, service)
    incoming_path = request.url.path
    if service == "novnc":
        prefix = "/novnc"
        target_path = incoming_path[len(prefix):] or "/"
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
    upstream_request = request.app.state.proxy_client.build_request(
        request.method,
        target_url,
        headers=headers,
        content=body,
    )
    upstream = await request.app.state.proxy_client.send(upstream_request, stream=True)
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


async def proxy_websocket_handler(websocket: WebSocket, lease_id: str, service: str) -> None:
    scheduler: EnvScheduler = websocket.app.state.scheduler
    target_base_url = await resolve_proxy_target(scheduler, lease_id, service)
    incoming_path = websocket.url.path
    if service == "novnc":
        prefix = "/novnc"
        target_path = incoming_path[len(prefix):] or "/"
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

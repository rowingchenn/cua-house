"""Async HTTP client for cua-house server."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from cua_house_common.models import BatchCreateRequest, LeaseCompleteRequest, TaskRequirement

logger = logging.getLogger(__name__)


class EnvServerClient:
    """Thin async HTTP client for interacting with cua-house server."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 30.0,
        token: str | None = None,
    ):
        resolved_base_url = (
            base_url or os.environ.get("CUA_HOUSE_SERVER_URL") or ""
        ).rstrip("/")
        if not resolved_base_url:
            raise ValueError("base_url or CUA_HOUSE_SERVER_URL must be provided")

        headers: dict[str, str] = {}
        resolved_token = token or os.environ.get("CUA_HOUSE_TOKEN")
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"

        transport = httpx.AsyncHTTPTransport(
            retries=3,  # retry on connection errors (RemoteProtocolError, etc.)
        )
        self._client = httpx.AsyncClient(
            base_url=resolved_base_url,
            timeout=timeout,
            headers=headers,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """HTTP request with retry on transient connection errors."""
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                response = await self._client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as exc:
                last_exc = exc
                if attempt < 3:
                    wait = 0.5 * (2 ** attempt)
                    logger.warning("request %s %s failed (attempt %d): %s — retrying in %.1fs", method, url, attempt + 1, exc, wait)
                    await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    async def submit_batch(self, request: BatchCreateRequest) -> dict[str, Any]:
        response = await self._request("POST", "/v1/batches", json=request.model_dump(mode="json"))
        return response.json()

    async def get_batch(self, batch_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/v1/batches/{batch_id}")
        return response.json()

    async def heartbeat_batch(self, batch_id: str) -> dict[str, Any]:
        response = await self._request("POST", f"/v1/batches/{batch_id}/heartbeat")
        return response.json()

    async def get_task(self, task_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/v1/tasks/{task_id}")
        return response.json()

    async def cancel_batch(
        self,
        batch_id: str,
        *,
        reason: str = "batch cancelled",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from cua_house_common.models import BatchCancelRequest
        request = BatchCancelRequest(reason=reason, details=details or {})
        response = await self._request(
            "POST", f"/v1/batches/{batch_id}/cancel",
            json=request.model_dump(mode="json"),
        )
        return response.json()

    async def heartbeat(self, lease_id: str) -> dict[str, Any]:
        response = await self._request("POST", f"/v1/leases/{lease_id}/heartbeat")
        return response.json()

    async def complete(
        self,
        lease_id: str,
        *,
        final_status: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = LeaseCompleteRequest(final_status=final_status, details=details or {})
        response = await self._request(
            "POST", f"/v1/leases/{lease_id}/complete",
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        return response.json()

    async def stage_runtime(self, lease_id: str) -> dict[str, Any]:
        response = await self._request("POST", f"/v1/leases/{lease_id}/stage-runtime")
        return response.json()

    async def stage_eval(self, lease_id: str) -> dict[str, Any]:
        response = await self._request("POST", f"/v1/leases/{lease_id}/stage-eval")
        return response.json()

    # ------------------------------------------------------------------
    # High-level: acquire_lease (submit + poll + heartbeat)
    # ------------------------------------------------------------------

    async def acquire_lease(
        self,
        *,
        snapshot_name: str,
        task_id: str | None = None,
        task_path: str | None = None,
        task_data: TaskRequirement.TaskDataRequest | None = None,
        metadata: dict[str, Any] | None = None,
        poll_interval_s: float = 5.0,
        max_wait_s: float = 600.0,
    ) -> AcquiredLease:
        """Submit a single-task batch, poll until ready, start heartbeat.

        Returns an ``AcquiredLease`` async context manager. On exit the
        heartbeat is stopped and the lease is completed automatically.
        """
        task_id = task_id or f"task-{uuid4()!s:.8}"
        task_path = task_path or f"auto/{task_id}"

        batch_resp = await self.submit_batch(
            BatchCreateRequest(
                tasks=[
                    TaskRequirement(
                        task_id=task_id,
                        task_path=task_path,
                        snapshot_name=snapshot_name,
                        task_data=task_data,
                        metadata=metadata or {},
                    )
                ]
            )
        )
        batch_id = batch_resp["batch_id"]

        # Poll until the task is assigned
        elapsed = 0.0
        while elapsed < max_wait_s:
            task_resp = await self.get_task(task_id)
            state = task_resp.get("state", "")

            if state in ("ready", "leased") and task_resp.get("assignment"):
                assignment = task_resp["assignment"]
                lease_id = assignment["lease_id"]
                raw_urls = assignment.get("urls", {})
                urls = {int(k): v for k, v in raw_urls.items()}
                novnc_url = assignment.get("novnc_url")

                # Size heartbeat interval from server's TTL
                heartbeat_interval = 20.0
                try:
                    health = await self._client.get("/healthz")
                    if health.status_code == 200:
                        ttl = health.json().get("heartbeat_ttl_s", 60)
                        heartbeat_interval = max(ttl / 2, 5.0)
                except Exception:
                    pass

                return AcquiredLease(
                    client=self,
                    lease_id=lease_id,
                    batch_id=batch_id,
                    task_id=task_id,
                    urls=urls,
                    novnc_url=novnc_url,
                    task_status=task_resp,
                    _heartbeat_interval=heartbeat_interval,
                )

            if state in ("failed", "completed"):
                error = task_resp.get("error", "unknown")
                raise RuntimeError(f"cua-house task {task_id} failed: {error}")

            await asyncio.sleep(poll_interval_s)
            elapsed += poll_interval_s

        raise TimeoutError(f"cua-house task {task_id} did not become ready within {max_wait_s}s")


@dataclass
class AcquiredLease:
    """Handle for a lease acquired via ``EnvServerClient.acquire_lease``.

    Use as an async context manager::

        async with await client.acquire_lease(snapshot_name="waa") as lease:
            urls = lease.urls          # {5000: "http://...", 9222: "http://..."}
            lease_id = lease.lease_id
            # ... interact with the VM ...
        # heartbeat stopped, lease completed automatically
    """

    client: EnvServerClient
    lease_id: str
    batch_id: str
    task_id: str
    urls: dict[int, str]
    novnc_url: str | None
    task_status: dict[str, Any]
    _heartbeat_interval: float = 20.0
    _heartbeat_task: asyncio.Task[None] | None = field(default=None, repr=False)

    async def __aenter__(self) -> AcquiredLease:
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._heartbeat_task), timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._heartbeat_task = None

        final_status = "failed" if exc_type is not None else "completed"
        try:
            await self.client.complete(self.lease_id, final_status=final_status)
            logger.info("Lease %s completed (%s)", self.lease_id, final_status)
        except Exception as e:
            logger.warning("Failed to complete lease %s: %s", self.lease_id, e)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                await self.client.heartbeat(self.lease_id)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("Heartbeat failed for lease %s: %s", self.lease_id, e)

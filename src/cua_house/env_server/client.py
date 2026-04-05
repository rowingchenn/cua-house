"""Async client for agenthle-env-server."""

from __future__ import annotations

import os
from typing import Any

import httpx

from .models import BatchCancelRequest, BatchCreateRequest, LeaseCompleteRequest


class EnvServerClient:
    """Thin HTTP client for orchestration code."""

    def __init__(self, base_url: str | None = None, timeout: float = 30.0, token: str | None = None):
        resolved_base_url = (base_url or os.environ.get("AGENTHLE_ENV_SERVER_URL", "")).rstrip("/")
        if not resolved_base_url:
            raise ValueError("base_url or AGENTHLE_ENV_SERVER_URL must be provided")

        headers: dict[str, str] = {}
        resolved_token = token or os.environ.get("AGENTHLE_TOKEN")
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"

        self._client = httpx.AsyncClient(base_url=resolved_base_url, timeout=timeout, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def submit_batch(self, request: BatchCreateRequest) -> dict[str, Any]:
        response = await self._client.post("/v1/batches", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return response.json()

    async def get_batch(self, batch_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v1/batches/{batch_id}")
        response.raise_for_status()
        return response.json()

    async def heartbeat_batch(self, batch_id: str) -> dict[str, Any]:
        response = await self._client.post(f"/v1/batches/{batch_id}/heartbeat")
        response.raise_for_status()
        return response.json()

    async def get_task(self, task_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v1/tasks/{task_id}")
        response.raise_for_status()
        return response.json()

    async def cancel_batch(self, batch_id: str, *, reason: str = "batch cancelled", details: dict[str, Any] | None = None) -> dict[str, Any]:
        request = BatchCancelRequest(reason=reason, details=details or {})
        response = await self._client.post(
            f"/v1/batches/{batch_id}/cancel",
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        return response.json()

    async def heartbeat(self, lease_id: str) -> dict[str, Any]:
        response = await self._client.post(f"/v1/leases/{lease_id}/heartbeat")
        response.raise_for_status()
        return response.json()

    async def complete(self, lease_id: str, *, final_status: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        request = LeaseCompleteRequest(final_status=final_status, details=details or {})
        response = await self._client.post(
            f"/v1/leases/{lease_id}/complete",
            json=request.model_dump(mode="json"),
        )
        response.raise_for_status()
        return response.json()

    async def stage_runtime(self, lease_id: str) -> dict[str, Any]:
        response = await self._client.post(f"/v1/leases/{lease_id}/stage-runtime")
        response.raise_for_status()
        return response.json()

    async def stage_eval(self, lease_id: str) -> dict[str, Any]:
        response = await self._client.post(f"/v1/leases/{lease_id}/stage-eval")
        response.raise_for_status()
        return response.json()

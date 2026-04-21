"""Worker-side cluster client.

Dials into the master WS endpoint, registers, and runs two background
coroutines while connected:

1. **Send loop** — emits Heartbeats (with `cached_shapes` for master-side
   cache-affinity placement) at the configured interval.
2. **Recv loop** — pulls master messages (AssignTask, StagePhase,
   ReleaseLease, Shutdown) off the socket. AssignTask fires the
   provision+bind path that this module's `_execute_assign_task`
   owns; ReleaseLease hands off to the scheduler to destroy the VM.

Startup sequence:

* Sweep stale snapshot cache entries (fingerprint mismatch)
* Pull all enabled image templates in parallel. A pull failure raises
  out of `start()` → lifespan → uvicorn exits → systemd/docker restart.
* Dial master, send Register, begin send/recv loops.

Reconnect uses exponential backoff. On reconnect the client re-sends
Register with the current `hosted_images` so master rebuilds its view.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from typing import TYPE_CHECKING

import websockets
from pydantic import TypeAdapter, ValidationError

from cua_house_common.models import TaskRequirement, TaskState, TaskStatus
from cua_house_server.cluster.protocol import (
    AssignTask,
    Envelope,
    Heartbeat,
    MasterToWorker,
    Register,
    ReleaseLease,
    Shutdown,
    TaskBound,
    TaskCompleted,
    TaskReleased,
    WorkerCapacity,
    WorkerVMSummary,
)
from cua_house_server.config.loader import ClusterConfig, HostRuntimeConfig

if TYPE_CHECKING:
    from cua_house_server.runtimes.qemu import DockerQemuRuntime, VMHandle
    from cua_house_server.scheduler.core import EnvScheduler

logger = logging.getLogger(__name__)

_MasterToWorkerAdapter = TypeAdapter(MasterToWorker)


def _proxy_url(service: str | int, lease_id: str, public_base_host: str, public_port: int) -> str:
    """Build a Host-header-routable URL that hits the worker's 8787 proxy.

    Worker exposes port 8787 publicly (firewall: `agenthle-allow-env-server`,
    0.0.0.0/0). VM docker ports (16000-18999) are VPC-only (`agenthle-allow-
    vm-ports`, 10.0.0.0/8), so clients outside the VPC cannot reach them
    directly. This function emits URLs of the form

        http://<service>--<lease_id>.<public_base_host>:8787/

    `public_base_host` must resolve (via DNS, or sslip.io's wildcard-IP
    scheme, or /etc/hosts) to the worker's external IP. The worker's own
    FastAPI catch-all parses the Host header via ``parse_proxy_host``
    (see ``api/proxy.py``), looks the VM up by lease_id, and forwards to
    the local CUA port. `service` is the guest port (5000 for CUA) or
    the literal string "novnc".
    """
    return f"http://{service}--{lease_id}.{public_base_host}:{public_port}/"


class WorkerClusterClient:
    """Long-lived worker-side WS client."""

    def __init__(
        self,
        *,
        host_config: HostRuntimeConfig,
        cluster: ClusterConfig,
        runtime: "DockerQemuRuntime",
        scheduler: "EnvScheduler",
        lease_endpoint: str,
        public_host: str | None = None,
    ) -> None:
        if cluster.master_url is None:
            raise ValueError("cluster.master_url must be set in worker mode")
        if cluster.worker_id is None:
            raise ValueError("cluster.worker_id must be set in worker mode")
        self.host_config = host_config
        self.cluster = cluster
        self.runtime = runtime
        self.scheduler = scheduler
        self.lease_endpoint = lease_endpoint
        self.public_host = public_host or host_config.host_external_ip
        self._supervisor: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._ws_out: "websockets.WebSocketClientProtocol | None" = None
        # Active task_id → VM summary for heartbeat. One-to-one with scheduler's
        # handle map; cleared on TaskCompleted.
        self._task_vms: dict[str, WorkerVMSummary] = {}
        # task_id → VMHandle. Worker owns the handle so it can call destroy_vm
        # when the scheduler signals completion (task_finalized_callback).
        self._task_handles: dict[str, "VMHandle"] = {}
        # Set of image_keys whose templates are local (filled by prewarm).
        self._hosted_images: set[str] = set()
        scheduler.task_finalized_callback = self._on_task_finalized
        self._log_token_provenance()

    def _log_token_provenance(self) -> None:
        import hashlib
        import os

        token = self.cluster.join_token
        source: str
        if token and os.environ.get("CUA_HOUSE_CLUSTER_JOIN_TOKEN") == token:
            source = "env"
        elif token:
            source = "config"
        else:
            source = "NOT_SET"
        if token:
            digest = hashlib.sha256(token.encode()).hexdigest()[:8]
            logger.info(
                "cluster join token source=%s sha256_prefix=%s length=%d",
                source, digest, len(token),
            )
        else:
            logger.warning(
                "cluster join token NOT SET — master will reject registration "
                "if it expects a token",
            )

    # ── lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._supervisor is not None:
            return
        if hasattr(self.runtime, "_snapshot_cache"):
            evicted = self.runtime._snapshot_cache.sweep_on_startup()
            if evicted:
                logger.info("snapshot cache sweep evicted %d stale entries", len(evicted))
        catalog = getattr(self.runtime, "_cluster_catalog", None) or {}
        if catalog and hasattr(self.runtime, "prewarm_templates"):
            await self.runtime.prewarm_templates(catalog)
            # Record what we've localized so the Register frame advertises it.
            self._hosted_images = {
                key for key, image in catalog.items()
                if image.enabled and image.local is not None
            }
        self._supervisor = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor = None

    # ── supervisor ─────────────────────────────────────────────────────

    async def _run_forever(self) -> None:
        backoff = self.cluster.reconnect_min_backoff_s
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = self.cluster.reconnect_min_backoff_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Worker WS disconnected: %s", exc)
            if self._stop.is_set():
                return
            sleep_s = backoff + random.uniform(0, backoff / 2)
            logger.info("Reconnecting to master in %.1fs", sleep_s)
            await asyncio.sleep(sleep_s)
            backoff = min(backoff * 2, self.cluster.reconnect_max_backoff_s)

    async def _connect_once(self) -> None:
        headers = {}
        if self.cluster.join_token:
            headers["Authorization"] = f"Bearer {self.cluster.join_token}"
        url = self.cluster.master_url
        assert url is not None
        logger.info("Worker %s connecting to %s", self.cluster.worker_id, url)
        async with websockets.connect(url, additional_headers=headers) as ws:
            self._ws_out = ws
            try:
                await self._send_register(ws)
                send_task = asyncio.create_task(self._send_loop(ws))
                try:
                    await self._recv_loop(ws)
                finally:
                    send_task.cancel()
                    try:
                        await send_task
                    except (asyncio.CancelledError, Exception):
                        pass
            finally:
                self._ws_out = None

    # ── frames ─────────────────────────────────────────────────────────

    @classmethod
    def build_register_frame(
        cls,
        host_config: HostRuntimeConfig,
        cluster: ClusterConfig,
        *,
        hosted_images: list[str] | None = None,
    ) -> Register:
        """Construct the Register frame this worker would send to master.

        Pure: no network, no WS state. Used both by the live connect path
        (`_send_register`) and by the `--print-register-frame` dry-run
        flag so operators can validate config before enabling the unit.
        """
        import psutil

        try:
            total_cpu = psutil.cpu_count(logical=True) or 1
            total_mem = int(psutil.virtual_memory().total / (1024 ** 3))
            total_disk = int(psutil.disk_usage("/").total / (1024 ** 3))
        except Exception:
            total_cpu, total_mem, total_disk = 1, 1, 1
        capacity = WorkerCapacity(
            total_vcpus=total_cpu,
            total_memory_gb=total_mem,
            total_disk_gb=total_disk,
            reserved_vcpus=host_config.host_reserved_vcpus,
            reserved_memory_gb=host_config.host_reserved_memory_gb,
        )
        return Register(
            worker_id=cluster.worker_id or "",
            runtime_version="0.1.0",
            capacity=capacity,
            hosted_images=sorted(hosted_images or []),
        )

    async def _send_register(self, ws: "websockets.WebSocketClientProtocol") -> None:
        register = self.build_register_frame(
            self.host_config,
            self.cluster,
            hosted_images=list(self._hosted_images),
        )
        await self._send(ws, register)

    async def _send_loop(self, ws: "websockets.WebSocketClientProtocol") -> None:
        interval = self.cluster.heartbeat_interval_s
        while True:
            hb = Heartbeat(
                vm_summaries=list(self._task_vms.values()),
                cached_shapes=self._collect_cached_shapes(),
            )
            await self._send(ws, hb)
            await asyncio.sleep(interval)

    def _collect_cached_shapes(self) -> list:
        """Snapshot of the local cache's shapes for master-side affinity ranking.

        Returns a list of CachedShape payloads; empty list if the runtime
        doesn't expose list_cached_shapes (non-QEMU backends, tests).
        """
        from cua_house_server.cluster.protocol import CachedShape

        if not hasattr(self.runtime, "list_cached_shapes"):
            return []
        try:
            entries = self.runtime.list_cached_shapes()
        except Exception:
            logger.warning("list_cached_shapes failed; reporting empty", exc_info=True)
            return []
        return [
            CachedShape(
                image_key=e.image_key,
                image_version=e.image_version,
                vcpus=e.vcpus,
                memory_gb=e.memory_gb,
                disk_gb=e.disk_gb,
            )
            for e in entries
        ]

    async def _recv_loop(self, ws: "websockets.WebSocketClientProtocol") -> None:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                logger.warning("Worker received non-JSON frame")
                continue
            try:
                envelope = Envelope.model_validate(data)
                msg = _MasterToWorkerAdapter.validate_python(envelope.payload)
            except ValidationError as exc:
                logger.warning("Worker received invalid master msg: %s", exc)
                continue
            await self._handle(ws, envelope, msg)

    # ── handlers ───────────────────────────────────────────────────────

    async def _handle(
        self,
        ws: "websockets.WebSocketClientProtocol",
        envelope: Envelope,
        msg: MasterToWorker,
    ) -> None:
        correlation_id = envelope.correlation_id or envelope.msg_id
        if isinstance(msg, AssignTask):
            # Fire-and-forget: a cold-boot AssignTask can hold up the recv
            # loop for minutes. Handle concurrently so other AssignTasks (on
            # cache-hit shapes) continue to dispatch at the same time.
            asyncio.create_task(
                self._handle_assign_task(ws, msg, correlation_id),
            )
        elif isinstance(msg, ReleaseLease):
            released = await self._execute_release_lease(msg)
            await self._send(ws, released, correlation_id=correlation_id)
        elif isinstance(msg, Shutdown):
            logger.info("Received shutdown from master (graceful=%s)", msg.graceful)
            self._stop.set()

    async def _handle_assign_task(
        self,
        ws: "websockets.WebSocketClientProtocol",
        msg: AssignTask,
        correlation_id: str,
    ) -> None:
        bound = await self._execute_assign_task(msg)
        await self._send(ws, bound, correlation_id=correlation_id)

    async def _execute_assign_task(self, msg: AssignTask) -> TaskBound:
        """Provision a fresh VM and bind the task to it.

        Failure modes are folded into `TaskBound(ok=False, error=...)` so
        master can surface the error cleanly. The worker does NOT retry;
        master decides whether to re-place on another worker.
        """
        image = self._catalog_lookup(msg.image_key)
        if image is None:
            return TaskBound(
                task_id=msg.task_id, lease_id=msg.lease_id,
                ok=False, error=f"unknown image_key: {msg.image_key}",
            )
        try:
            handle = await self.runtime.provision_vm(
                image=image,
                vcpus=msg.vcpus,
                memory_gb=msg.memory_gb,
                disk_gb=msg.disk_gb,
            )
        except Exception as exc:
            logger.exception("provision_vm failed for task %s", msg.task_id)
            return TaskBound(
                task_id=msg.task_id, lease_id=msg.lease_id,
                ok=False, error=f"provision failed: {exc}",
            )

        task_data = None
        if msg.task_data is not None:
            try:
                task_data = TaskRequirement.TaskDataRequest.model_validate(msg.task_data)
            except ValidationError as exc:
                # Worker can't fix a malformed task_data; destroy the VM we
                # just made and fail the bind back to master.
                await self._safe_destroy(handle)
                return TaskBound(
                    task_id=msg.task_id, lease_id=msg.lease_id,
                    ok=False, error=f"invalid task_data: {exc}",
                )

        try:
            task = await self.scheduler.bind_provisioned_task(
                task_id=msg.task_id,
                task_path=msg.task_path or msg.task_id,
                snapshot_name=msg.image_key,
                vcpus=msg.vcpus,
                memory_gb=msg.memory_gb,
                disk_gb=msg.disk_gb,
                task_data=task_data,
                metadata=dict(msg.metadata),
                handle=handle,
                lease_id=msg.lease_id,
            )
        except Exception as exc:
            logger.exception("bind_provisioned_task failed for %s", msg.task_id)
            await self._safe_destroy(handle)
            return TaskBound(
                task_id=msg.task_id, lease_id=msg.lease_id,
                ok=False, error=f"bind failed: {exc}",
            )
        assert task.assignment is not None
        self._task_handles[msg.task_id] = handle
        self._task_vms[msg.task_id] = WorkerVMSummary(
            vm_id=handle.vm_id,
            image_key=msg.image_key,
            image_version=image.version,
            vcpus=handle.vcpus,
            memory_gb=handle.memory_gb,
            disk_gb=handle.disk_gb,
            from_cache=handle.from_cache,
            lease_id=msg.lease_id,
            public_host=self.public_host,
            published_ports=dict(handle.published_ports),
            novnc_port=handle.novnc_port,
        )
        # Public URLs route through the worker's 8787 proxy so clients
        # never need direct access to the VM's docker-mapped ports
        # (16000-18999, VPC-only per the firewall rules).
        base_host = self.host_config.public_base_host
        port = self.cluster.worker_public_port
        public_urls = {
            guest_port: _proxy_url(guest_port, msg.lease_id, base_host, port)
            for guest_port in task.assignment.urls
        }
        public_novnc = (
            _proxy_url("novnc", msg.lease_id, base_host, port)
            if task.assignment.novnc_url else None
        )
        return TaskBound(
            task_id=msg.task_id,
            lease_id=msg.lease_id,
            vm_id=handle.vm_id,
            ok=True,
            from_cache=handle.from_cache,
            lease_endpoint=self.lease_endpoint,
            urls=public_urls,
            novnc_url=public_novnc,
        )

    async def _execute_release_lease(self, msg: ReleaseLease) -> TaskReleased:
        """Master-initiated release (batch cancel / worker eviction).

        Client-driven completes (POST /v1/leases/{id}/complete) hit the
        scheduler directly; this branch only fires when master tears down
        a lease remotely. Either way the finalize hook handles destroy_vm.
        """
        try:
            await self.scheduler.complete(
                msg.lease_id,
                final_status=msg.final_status,
                details={"source": "master_release"},
            )
        except KeyError:
            return TaskReleased(lease_id=msg.lease_id, ok=False, error="lease unknown")
        except Exception as exc:
            logger.exception("release via scheduler.complete failed")
            return TaskReleased(lease_id=msg.lease_id, ok=False, error=str(exc))
        return TaskReleased(lease_id=msg.lease_id, ok=True)

    async def _on_task_finalized(self, task: TaskStatus) -> None:
        """Scheduler hook: fire TaskCompleted upstream and free local state.

        The scheduler owns the runtime.destroy_vm call, so we just send the
        completion notification and drop our cache entry. (We no longer hold
        the handle after destroy — the scheduler clears its own reference.)
        """
        self._task_handles.pop(task.task_id, None)
        self._task_vms.pop(task.task_id, None)
        ws = self._ws_out
        if ws is None:
            logger.info(
                "Task %s finalized while WS down; skipping notify", task.task_id,
            )
            return
        final_status = "completed" if task.state == TaskState.COMPLETED else "failed"
        msg = TaskCompleted(
            task_id=task.task_id,
            lease_id=task.lease_id or "",
            final_status=final_status,
            error=task.error,
        )
        try:
            await self._send(ws, msg)
        except Exception:
            logger.exception("Failed to send TaskCompleted for %s", task.task_id)

    def _catalog_lookup(self, image_key: str):
        catalog = getattr(self.runtime, "_cluster_catalog", None) or {}
        return catalog.get(image_key)

    async def _safe_destroy(self, handle: "VMHandle") -> None:
        try:
            await self.runtime.destroy_vm(handle)
        except Exception:
            logger.exception("destroy_vm failed; resources may leak")

    # ── wire helpers ───────────────────────────────────────────────────

    async def _send(
        self,
        ws: "websockets.WebSocketClientProtocol",
        payload,
        *,
        correlation_id: str | None = None,
    ) -> None:
        envelope = Envelope(
            msg_id=str(uuid.uuid4()),
            correlation_id=correlation_id,
            payload=payload.model_dump(),
        )
        await ws.send(envelope.model_dump_json())

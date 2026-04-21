"""Worker-side cluster client.

Dials into the master WS endpoint, registers, and runs two background
coroutines while connected:

1. **Send loop** — emits Heartbeats at a configurable interval and any
   VMStateUpdate / PoolOpResult / TaskPhaseResult messages queued by the
   worker's runtime.
2. **Recv loop** — pulls master messages (AssignTask, PoolOp, StagePhase,
   ReleaseLease, Shutdown) off the socket and dispatches them to the worker
   runtime executor.

If the connection drops, the client reconnects with exponential backoff. On
reconnect it re-sends Register with the current ``hosted_images`` (may have
grown/shrunk since last session) so master can reconcile without losing
placement history.

Pool op execution uses the hooks already set up in Phase 1c:
``DockerQemuRuntime.pull_template`` for ADD_IMAGE and ``_prepare_vm`` +
``_start_vm_container`` for ADD_VM. A future iteration will move hot-remove
into a dedicated runtime method.
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
    PoolOp,
    PoolOpResult,
    Register,
    ReleaseLease,
    Shutdown,
    TaskBound,
    TaskCompleted,
    TaskReleased,
    VMStateUpdate,
    WorkerCapacity,
    WorkerVMSummary,
)
from cua_house_server.config.loader import ClusterConfig, HostRuntimeConfig

if TYPE_CHECKING:
    from cua_house_server.runtimes.qemu import DockerQemuRuntime
    from cua_house_server.scheduler.core import EnvScheduler

logger = logging.getLogger(__name__)

_MasterToWorkerAdapter = TypeAdapter(MasterToWorker)


def _public_rewrite(local_url: str, public_host: str) -> str:
    """Replace the host in a loopback URL with the worker's public address.

    ``http://127.0.0.1:16001/x`` → ``http://<public_host>:16001/x``. Used
    to publish VM URLs back to clients via master.
    """
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(local_url)
    if parsed.port is None:
        return local_url
    return urlunparse(parsed._replace(netloc=f"{public_host}:{parsed.port}"))


class WorkerClusterClient:
    """Long-lived worker-side WS client.

    Owned by the app lifespan — ``start()`` spawns a supervisor task that
    reconnects forever until ``stop()`` is called.
    """

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
        # Tracked state that's reported to master.
        self._hosted_images: set[str] = set()
        self._vm_summaries: dict[str, WorkerVMSummary] = {}
        # Install the finalization hook so the scheduler's complete → revert
        # path automatically emits TaskCompleted back to master.
        scheduler.task_finalized_callback = self._on_task_finalized
        # Log where the cluster join token came from so operators can
        # cross-check a SHA-256 prefix against master without ever printing
        # the secret itself. Catches env-var / config-file typos instantly
        # from journalctl -u cua-house-worker.
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
        if hasattr(self.runtime, '_snapshot_cache'):
            evicted = self.runtime._snapshot_cache.sweep_on_startup()
            if evicted:
                logger.info("snapshot cache sweep evicted %d stale entries", len(evicted))
        # Prewarm all enabled image templates before registering with master.
        # Any failure raises out of lifespan → uvicorn exits → systemd/docker
        # restart retries with a clean state.
        catalog = getattr(self.runtime, "_cluster_catalog", None) or {}
        if catalog and hasattr(self.runtime, "prewarm_templates"):
            await self.runtime.prewarm_templates(catalog)
        self._supervisor = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stop.set()
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):  # pragma: no cover
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

    # ── loops ──────────────────────────────────────────────────────────

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
        (``_send_register``) and by the ``--print-register-frame`` dry-run
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
                vm_summaries=list(self._vm_summaries.values()),
                cached_shapes=self._collect_cached_shapes(),
            )
            await self._send(ws, hb)
            await asyncio.sleep(interval)

    def _collect_cached_shapes(self) -> list:
        """Runtime introspection of the snapshot cache for master-side affinity.

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
        if isinstance(msg, PoolOp):
            # Fire-and-forget so multiple ADD_VM ops cold-boot in parallel
            # instead of serializing on the recv loop.
            asyncio.create_task(
                self._handle_pool_op(ws, msg, correlation_id),
            )
        elif isinstance(msg, AssignTask):
            bound = await self._execute_assign_task(msg)
            await self._send(ws, bound, correlation_id=correlation_id)
        elif isinstance(msg, ReleaseLease):
            released = await self._execute_release_lease(msg)
            await self._send(ws, released, correlation_id=correlation_id)
        elif isinstance(msg, Shutdown):
            logger.info("Received shutdown from master (graceful=%s)", msg.graceful)
            self._stop.set()

    async def _handle_pool_op(
        self,
        ws: "websockets.WebSocketClientProtocol",
        msg: PoolOp,
        correlation_id: str,
    ) -> None:
        """Execute a pool op concurrently and send the result back."""
        try:
            ok, error, produced = await self._execute_pool_op(msg)
        except Exception as exc:
            ok, error, produced = False, str(exc), None
        await self._send(
            ws,
            PoolOpResult(op_id=msg.op_id, ok=ok, error=error, produced_vm_id=produced),
            correlation_id=correlation_id,
        )

    async def _execute_pool_op(
        self, op: PoolOp,
    ) -> tuple[bool, str | None, str | None]:
        args = op.args
        try:
            if op.op == "ADD_IMAGE":
                if args.image_key is None:
                    return False, "image_key required", None
                image = self._catalog_lookup(args.image_key)
                if image is None:
                    return False, f"unknown image {args.image_key}", None
                await self.runtime.pull_template(args.image_key, image)
                self._hosted_images.add(args.image_key)
                return True, None, None

            if op.op == "REMOVE_IMAGE":
                if args.image_key is None:
                    return False, "image_key required", None
                # Any VMs still running for this image must be removed first;
                # the reconciler enforces ordering but we guard anyway.
                stranded = [
                    vm for vm in self._vm_summaries.values()
                    if vm.image_key == args.image_key
                ]
                if stranded:
                    return False, f"{len(stranded)} VMs still running for {args.image_key}", None
                self._hosted_images.discard(args.image_key)
                return True, None, None

            if op.op == "ADD_VM":
                if args.image_key is None or args.vcpus is None or args.memory_gb is None:
                    return False, "image_key, vcpus, memory_gb required", None
                image = self._catalog_lookup(args.image_key)
                if image is None:
                    return False, f"unknown image {args.image_key}", None
                disk_gb = args.disk_gb if args.disk_gb is not None else image.default_disk_gb
                handle = await self.runtime.provision_vm(
                    image=image,
                    vcpus=args.vcpus,
                    memory_gb=args.memory_gb,
                    disk_gb=disk_gb,
                )
                # Make the hot-plug VM visible to the local EnvScheduler so
                # client HTTP lease ops (heartbeat/stage/complete) can find
                # it via the existing lease code path.
                await self.scheduler.register_external_vm(
                    handle,
                    snapshot_name=args.image_key,
                    vcpus=args.vcpus,
                    memory_gb=args.memory_gb,
                )
                self._vm_summaries[handle.vm_id] = WorkerVMSummary(
                    vm_id=handle.vm_id,
                    image_key=args.image_key,
                    image_version=image.version,
                    vcpus=handle.vcpus,
                    memory_gb=handle.memory_gb,
                    disk_gb=handle.disk_gb,
                    state="ready",
                    from_cache=handle.from_cache,
                    public_host=self.public_host,
                    published_ports=dict(handle.published_ports),
                    novnc_port=handle.novnc_port,
                )
                self._hosted_images.add(args.image_key)
                return True, None, handle.vm_id

            if op.op == "REMOVE_VM":
                if args.vm_id is None:
                    return False, "vm_id required", None
                released = await self.scheduler.unregister_external_vm(args.vm_id)
                if not released:
                    return False, "vm still leased", None
                handle = self.runtime.hotplug_handle(args.vm_id)
                if handle is not None:
                    await self.runtime.destroy_vm(handle)
                self._vm_summaries.pop(args.vm_id, None)
                return True, None, None

            return False, f"unknown op {op.op}", None
        except Exception as exc:
            logger.exception("PoolOp %s failed", op.op_id)
            return False, str(exc), None

    def _catalog_lookup(self, image_key: str):
        catalog = getattr(self.runtime, "_cluster_catalog", None) or {}
        return catalog.get(image_key)

    # ── task ops ───────────────────────────────────────────────────────

    async def _execute_assign_task(self, msg: AssignTask) -> TaskBound:
        """Accept a master AssignTask and record the lease locally.

        The heavy lifting is delegated to ``EnvScheduler.assign_external_task``
        which reuses the existing lease plumbing (reaper, staging, complete,
        revert). Once that returns, we only need to wrap the resulting
        TaskAssignment into a ``TaskBound`` for master.
        """
        try:
            task_data = None
            if msg.task_data is not None:
                task_data = TaskRequirement.TaskDataRequest.model_validate(msg.task_data)
            task = await self.scheduler.assign_external_task(
                task_id=msg.task_id,
                task_path=msg.task_path or msg.task_id,
                snapshot_name=msg.image_key,
                vcpus=msg.vcpus or 0,
                memory_gb=msg.memory_gb or 0,
                disk_gb=msg.disk_gb or 0,
                task_data=task_data,
                metadata=dict(msg.metadata),
                vm_id=msg.vm_id,
                lease_id=msg.lease_id,
            )
        except Exception as exc:
            logger.exception("assign_external_task failed")
            return TaskBound(
                task_id=msg.task_id,
                lease_id=msg.lease_id,
                vm_id=msg.vm_id,
                ok=False,
                error=str(exc),
            )
        assert task.assignment is not None
        # Rewrite the scheduler's loopback URLs to use the worker's public
        # host. The scheduler returns http://127.0.0.1:<port>; replace host
        # only, preserve port.
        public_urls: dict[int, str] = {}
        for guest_port, local_url in task.assignment.urls.items():
            public_urls[guest_port] = _public_rewrite(local_url, self.public_host)
        public_novnc = (
            _public_rewrite(task.assignment.novnc_url, self.public_host)
            if task.assignment.novnc_url else None
        )
        # Update the cached summary so heartbeat reflects leased state.
        summary = self._vm_summaries.get(msg.vm_id)
        if summary is not None:
            summary.state = "leased"
            summary.lease_id = msg.lease_id
        return TaskBound(
            task_id=msg.task_id,
            lease_id=msg.lease_id,
            vm_id=msg.vm_id,
            ok=True,
            lease_endpoint=self.lease_endpoint,
            urls=public_urls,
            novnc_url=public_novnc,
        )

    async def _execute_release_lease(self, msg: ReleaseLease) -> TaskReleased:
        """Master-initiated release path (batch cancel).

        Clients' own ``/v1/leases/{id}/complete`` goes straight to the
        scheduler via HTTP and fires the finalize hook naturally. This
        branch is only hit when master wants to tear down a lease remotely.
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
        """Fire TaskCompleted up to master when the scheduler finalizes a task."""
        ws = self._ws_out
        if ws is None:
            # Not connected right now; the master view will drift until the
            # next heartbeat reconciles it. Master-side Phase 5 work will add
            # a more robust replay channel.
            logger.info("Task %s finalized while WS down; skipping notify", task.task_id)
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
        # Mirror the state change in our cached VM summary so the next
        # heartbeat reflects the free slot.
        lease_id = task.lease_id
        if lease_id is not None:
            for summary in self._vm_summaries.values():
                if summary.lease_id == lease_id:
                    summary.state = "ready"
                    summary.lease_id = None
                    break

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

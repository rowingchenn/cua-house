# Server-specific development rules

Development guidelines for working on `cua-house-server`.

## RuntimeBackend protocol

All runtime backends implement the `RuntimeBackend` protocol defined in `runtimes/base.py`:

```python
class RuntimeBackend(Protocol):
    def cleanup_orphaned_state(self) -> None: ...
    def prepare_slot(self, *, slot_id, image, cpu_cores, memory_gb, cua_port, novnc_port, lease_id, task_id, task_data) -> Any: ...
    async def start_slot(self, handle) -> None: ...
    async def reset_slot(self, handle, image) -> None: ...
    def cua_local_url(self, handle) -> str: ...
    def novnc_local_url(self, handle) -> str: ...
    def validate_runtime_task_data(self, *, task_id, task_data) -> None: ...
    async def stage_task_phase(self, *, handle, task_id, lease_id, task_data, phase, container_name) -> StageResult: ...
```

Key points:
- `prepare_slot` returns a handle object. The handle type varies by backend (SlotHandle, VMHandle, GCPSlotHandle). The scheduler stores handles by slot/VM ID.
- `start_slot` must block until the CUA server is responsive (HTTP 200 on /status).
- `reset_slot` should clean up fully so the slot can be reused or freed.
- `stage_task_phase` handles guest-side task data operations. The `phase` parameter is `"runtime"` or `"eval"`.

## Scheduler internals

### State machine

The scheduler (`scheduler/core.py`) manages three state machines in memory:

1. **Tasks** (`_tasks: dict[str, TaskStatus]`): tracks every submitted task through its lifecycle.
2. **Slots** (`_slots: dict[str, SlotRecord]`): per-task container slots (legacy mode).
3. **VMs** (`_vms: dict[str, VMRecord]`): persistent snapshot-pool VMs (when `vm_pool` is configured).

### Dispatch loop

`_dispatch_loop` runs as an asyncio background task:

1. Picks the next queued task (ordered by submission time).
2. If VM pool is active: finds a READY VM matching the image key, assigns it immediately via `loadvm` revert.
3. If no VM pool or no matching VM: allocates ports, creates a slot, and spawns `_start_slot_task` as a background asyncio task.
4. Tasks that exceed host capacity are immediately marked FAILED.

### Lease reaper

`_lease_reaper_task` runs periodically:
- Checks `LeaseRecord.expires_at` against `utcnow()`.
- Expired leases trigger `_release_after_reset` (destroy container or revert VM).
- Starting tasks (no lease yet) are not reaped.

### Batch heartbeat

Batches also have a TTL (`batch_heartbeat_ttl_s`). If the client stops sending batch heartbeats, all non-terminal tasks in that batch are marked FAILED. The `reap_expired_batches_once` method handles this.

## API decomposition

### app.py

`create_app()` is the FastAPI factory:
- Loads config from YAML files.
- Creates runtime instances based on enabled images.
- Instantiates `EnvScheduler`.
- Registers API routes from `routes.py`.
- Registers catch-all proxy routes (must be last).
- Manages lifespan (startup: `scheduler.start()`, shutdown: `scheduler.shutdown()`).

### routes.py

All `/v1/` endpoints. Uses `require_auth` for token validation. The `_present_task` and `_present_batch` helpers rewrite CUA/noVNC URLs to use the public base host for external access.

### auth.py

`require_auth(authorization, expected_token)`: validates bearer token. No-op when no token is configured.

### proxy.py

`lease_id_from_host(host_header, public_base_host)`: extracts lease ID from `lease-{id}.{base}` hostname pattern.

HTTP proxy uses `httpx.AsyncClient` with streaming. WebSocket proxy uses the `websockets` library for bidirectional relay.

## QMP client

The QMP client (`qmp/client.py`) communicates with QEMU inside Docker containers:

- Uses `docker exec {container} sh -c "... | nc -q1 127.0.0.1 7200"`.
- Direct TCP port forwarding does not work reliably (Docker proxy connects but data does not flow).
- QMP port 7200 is set via `ENV ARGUMENTS` in the Docker image.
- Each operation opens a fresh nc session.
- The `human-monitor-command` QMP execute wraps HMP commands (`savevm`, `loadvm`).
- `savevm` is synchronous in QEMU -- the nc pipe blocks until it completes. `final_sleep` parameter gives time for response delivery.

## Task data staging

Two mechanisms, selected based on runtime mode:

### Samba (per-task containers, local)

- Host directory bind-mounted into container at `/shared/`.
- Guest accesses via `\\host.lan\Data` Samba share.
- Files copied using `robocopy` from Samba share to guest paths.
- Reference directory removed during runtime phase, restored during eval phase.
- Legacy fallback: HTTP upload via CUA `/cmd` endpoint (zip + Expand-Archive).

### NTFS ACL (VM pool and GCP)

- Entire task data available on E: drive (Samba `net use` for VM pool, or GCP data disk).
- Isolation via `icacls /deny User:(OI)(CI)F` on non-whitelisted directories.
- Runtime phase: only `input/`, `software/`, `output/` accessible. Reference and sibling tasks denied.
- Eval phase: deny on `reference/` removed.
- Runs as `User` account, no elevation required.

Both approaches produce a `StageResult(file_count, bytes_staged, skipped)`.

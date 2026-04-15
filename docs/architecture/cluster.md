# Cluster architecture

The multi-node cluster mode splits the single-node cua-house server into a
**master control plane** and a fleet of **worker data-plane** nodes. This
document describes the design, state ownership, protocols, and failure
modes. For the operator deployment recipe see
[deployment/cluster.md](../deployment/cluster.md).

## Problem this solves

Standalone cua-house runs a VM pool on one KVM host. This hits three
ceilings:

1. **Capacity** — all images share a single host's CPU, memory, and
   qcow2 storage. You can only scale vertically.
2. **Image rigidity** — the image catalog is loaded at process start.
   Adding or swapping an image requires a full restart.
3. **Blast radius** — a host crash stalls every outstanding task.

The cluster splits these concerns: pool membership is driven by a master
desired-state controller, each worker hosts a subset of images, and
master/worker isolation means a worker failure only affects tasks bound
to that worker.

## Role split

```
                  ┌──────────────────────────────────────────────┐
     clients ───▶ │  MASTER                                       │
                  │  ─ FastAPI (batch admission + pool control)   │
                  │  ─ ClusterDispatcher (batch/task state)       │
                  │  ─ WorkerRegistry                              │
                  │  ─ ClusterPoolSpec + PoolReconciler            │
                  │  ─ PoolOpCoordinator (RPC over WS)             │
                  │  ─ /v1/leases/* HTTP proxy → worker            │
                  │  ─ (GCPVMRuntime for overflow — TODO)          │
                  └──┬────────────────────────────────┬────────────┘
                     │ WebSocket                       │ WebSocket
                     ▼                                 ▼
           ┌──────────────────┐              ┌──────────────────┐
           │   WORKER A       │              │   WORKER B       │
           │  ─ EnvScheduler  │              │  ─ EnvScheduler  │
           │  ─ DockerQemu    │              │  ─ DockerQemu    │
           │     (hot-plug)   │              │     (hot-plug)   │
           │  ─ lease HTTP API│              │  ─ lease HTTP API│
           │  ─ VM proxy      │              │  ─ VM proxy      │
           │  ─ WorkerClusterClient           ─ WorkerClusterClient
           └──────────────────┘              └──────────────────┘
                     ▲                                 ▲
                     │ direct client traffic           │ direct client traffic
                     │ (VM service URLs)               │ (VM service URLs)
                     │                                 │
                     └──────────── clients ────────────┘
```

### Master (control plane)

* **Batch admission**: `POST /v1/batches`, `GET /v1/batches/{id}`,
  `GET /v1/tasks/{id}`, cancel. Implemented by `ClusterDispatcher` in
  `cluster/dispatcher.py`.
* **Worker orchestration**: accepts WS connections at
  `/v1/cluster/ws`, tracks online workers in `WorkerRegistry`, runs a
  periodic `PoolReconciler` that compares desired state in
  `ClusterPoolSpec` against each worker's reported `vm_summaries` and
  sends `PoolOp` messages to converge.
* **Cluster operations API**: `GET /v1/cluster/workers`,
  `GET/PUT /v1/cluster/pool`, `GET /v1/cluster/status` — read/write
  endpoints for operators (and a future dashboard).
* **Lease proxy**: forwards `POST /v1/leases/{id}/{heartbeat,complete,stage-runtime,stage-eval}`
  from master to the owning worker. Lets legacy clients with a single
  `CUA_HOUSE_SERVER_URL` work against a cluster unchanged.
* **No local VMs**: master does NOT own a `DockerQemuRuntime` pool.
  The GCP overflow runtime is instantiated on master for GPU fallback
  but is not wired to the dispatcher in this revision.

Master is intentionally **out of the per-lease data path**. Clients that
read `TaskAssignment.lease_endpoint` from the task response connect
directly to the worker's public HTTP interface for lease and VM service
traffic. The master lease proxy exists as a compatibility path; the
worker's HTTP API is the authoritative one.

### Worker (data plane)

* Runs the existing `EnvScheduler` + `api/routes.py` stack
  **unchanged** — all the standalone mode lease lifecycle code is
  reused directly.
* VM pool is initially empty. Master pushes `PoolOp.ADD_IMAGE` +
  `ADD_VM` messages; the worker client executes them via
  `DockerQemuRuntime.pull_template`, `.add_vm`, and `.remove_vm`,
  then injects the resulting VM handle into `EnvScheduler._vms` via
  `register_external_vm` so the existing lease code can find it.
* Exposes lease HTTP API (`heartbeat`, `complete`, `stage-runtime`,
  `stage-eval`) and the VM reverse proxy on its public port. In
  cluster mode `vm_bind_address: 0.0.0.0` binds docker's `-p` flag
  so the VM's `published_ports` are reachable across the VPC.
* Reports `vm_summaries` including `public_host` and `published_ports`
  back to master via heartbeats. Master uses those to construct
  `TaskAssignment.urls` that point directly at worker IPs:ports.
* When a lease terminates on the worker (either via client's
  `/complete` or master's `ReleaseLease`), the scheduler's
  `task_finalized_callback` hook fires an async `TaskCompleted`
  message up to master so master can update its batch view.

### Clients

Clients read `TaskAssignment` from master and use two network paths:

1. **Lease operations** → `assignment.lease_endpoint` (e.g.
   `http://worker.public:8787`). Direct to worker, not master.
2. **VM service** → `assignment.urls[port]` (e.g.
   `http://worker.public:16000`). Direct to worker's docker-proxied
   VM port.

Legacy clients that only know `CUA_HOUSE_SERVER_URL` hit master's lease
proxy for (1) and still go direct for (2). Both cases work.

## State ownership

| State                    | Owner                           | Persistence                        |
|--------------------------|---------------------------------|------------------------------------|
| Batch / task records     | Master `ClusterDispatcher`      | In-memory (lost on master crash)   |
| Desired pool state       | Master `ClusterPoolSpec`        | `runtime_root/cluster-pool-spec.json` (write-through) |
| Worker connection info   | Master `WorkerRegistry`         | In-memory (re-populated on Register) |
| Lease record + VM handle | Worker `EnvScheduler`           | In-memory (lost on worker crash)   |
| VM docker containers     | Worker `DockerQemuRuntime`      | Docker daemon (survive worker process restart; orphan cleanup on next start) |
| Task data                | Shared GCE PD + per-worker OverlayFS | PD read-only, overlay upper per-node |

**Master ossifies task view at `state=ready`.** Once the dispatcher sets
a task to READY, master does not observe any intermediate LEASED /
RESETTING transitions — those live on the worker. Master only updates
state again when it receives `TaskCompleted` over WS (or when the
worker disconnects, in which case orphaned leases are marked FAILED).
Clients poll the **worker** (directly or via master's lease proxy) for
authoritative status between READY and terminal.

## Wire protocol

WebSocket endpoint: `ws://<master>:8787/v1/cluster/ws`. Auth is a shared
`CUA_HOUSE_CLUSTER_JOIN_TOKEN` sent as `Authorization: Bearer <token>`.

All frames are JSON `Envelope` objects defined in `cluster/protocol.py`:

```json
{
  "msg_id": "<uuid>",
  "correlation_id": "<uuid or null>",
  "payload": { "kind": "<message kind>", ... }
}
```

`correlation_id` links responses to requests for RPC-style operations
(PoolOp, AssignTask, ReleaseLease) via the master-side
`PoolOpCoordinator`, which is a generic future-based request/response
table keyed by correlation_id.

### Worker → master

| Kind                 | Purpose                                                           |
|----------------------|-------------------------------------------------------------------|
| `register`           | First frame on connect. Advertises capacity and hosted images.    |
| `heartbeat`          | Periodic (~5s) load + `vm_summaries` snapshot.                    |
| `vm_state_update`    | Fire-and-forget VM state transition (mid-heartbeat, rare).        |
| `pool_op_result`     | Reply to a master-initiated `PoolOp`.                             |
| `task_bound`         | Reply to an `AssignTask`. Carries `lease_endpoint`, `urls`, `novnc_url`. |
| `task_released`      | Reply to a `ReleaseLease`. Reports revert success.                |
| `task_completed`     | Async notification when a lease terminates via the worker's own HTTP API. |
| `task_phase_result`  | Reserved for future stage-phase reporting.                        |

### Master → worker

| Kind             | Purpose                                                               |
|------------------|-----------------------------------------------------------------------|
| `pool_op`        | `ADD_IMAGE` / `REMOVE_IMAGE` / `ADD_VM` / `REMOVE_VM`                 |
| `assign_task`    | Bind a specific `vm_id` on this worker to `(task_id, lease_id)`       |
| `release_lease`  | Master-initiated teardown (batch cancel path)                         |
| `stage_phase`    | Reserved (future: staging driven from master)                          |
| `shutdown`       | Graceful drain (not yet implemented)                                   |

## Control loops

### `PoolReconciler` (master)

Runs every `interval_s` (default 5). Per tick:

1. `reap_stale()` — any worker whose last heartbeat is older than
   `heartbeat_ttl_s` is marked offline; `on_worker_evicted` callback
   fires immediately, which the dispatcher uses to fail orphaned leases.
2. For each online worker, compute a pure-function `compute_diff` of
   `(desired_pool_spec, actual_vm_summaries, actual_hosted_images)`
   producing a list of `DiffEntry{op, image_key, vm_id, cpu, mem}`
   ops. Ordering guarantees:
   * `ADD_IMAGE` before any `ADD_VM` for that image (template must
     exist before a VM can be copy-on-written from it).
   * `REMOVE_VM` before `REMOVE_IMAGE` for the same image (can't
     delete a template while VMs still reference it).
3. For each op, `coordinator.issue()` registers a future, the
   envelope is sent with `correlation_id = op_id`, and the reconciler
   awaits the `PoolOpResult` (or timeout).
4. On success, apply an **optimistic local update** to the registry so
   the next tick sees the new state without waiting for a heartbeat.

The reconciler bails out of a worker's tick on the first failed op;
next tick will retry. Different workers are reconciled in parallel.

### `ClusterDispatcher` dispatch loop (master)

Separate asyncio task on master. Wakes every 1s or on
`self._wake.set()`. Per tick:

1. Scan in-memory `_tasks` for entries in `QUEUED` state.
2. For each, `_pick_worker` walks `WorkerRegistry` sessions,
   `free_vm_for(image, cpu, mem)` selects matching READY VMs, and
   least-leased-count wins (ties broken by iteration order).
3. Build an `AssignTask` envelope with a fresh `lease_id`, stamp
   `correlation_id = op_id`, optimistically mark the VM as leased in
   the registry (prevents concurrent double-book), send.
4. Await `TaskBound` via coordinator. On success copy
   `lease_endpoint` + `urls` + `novnc_url` into the task's
   `TaskAssignment`, state → `READY`.
5. On failure revert the assignment: task → `QUEUED`, VM → `ready`,
   error recorded.

### Worker dispatch hooks

Worker's `EnvScheduler` is unchanged from standalone for lease
lifecycle. The integration points are three new methods:

* `register_external_vm(handle, snapshot_name, vcpus, memory_gb)` — add a
  hot-plug VM into `_vms` so `_find_free_vm_locked`, staging, revert,
  and the lease reaper all work on it.
* `unregister_external_vm(vm_id)` — remove it on `REMOVE_VM` (refuses
  if still leased so master retries).
* `assign_external_task(task_id, lease_id, vm_id, ...)` — bind a
  master-pushed task to a specific VM without going through the local
  dispatch queue. Returns a `TaskStatus` with an `assignment` pointing
  at loopback URLs, which the worker client rewrites to the public
  host before replying `TaskBound`.

The `task_finalized_callback` hook on `EnvScheduler._release_after_reset`
is set by `WorkerClusterClient` on construction; it fires
`TaskCompleted` over WS when a task reaches a terminal state.

## Failure modes

### Worker WS disconnect

Handled in two places:

1. **Immediate** — `cluster_ws` finally-block calls
   `registry.mark_offline` and `dispatcher.handle_worker_disconnect`.
2. **Timeout** — reconciler's `reap_stale` catches workers whose
   heartbeats stopped without a clean disconnect (network partition,
   OOM kill).

Both paths end in `handle_worker_disconnect(worker_id)`, which walks
`_leases` and fails every task bound to that worker:

```
task.state = FAILED
task.error = "worker <id> disconnected"
task.completed_at = now
```

Fail-fast is deliberate. A reconnecting worker starts with a blank
in-memory lease state so any task we held in STARTING/READY would be
unrecoverable. Client-level retry is preferred over silent suspension.

### Worker process restart

Hot-plug VMs created via docker **survive** a worker process restart,
but the in-memory lease and VM handles don't. On next start, the
worker's scheduler `cleanup_orphaned_state()` hook removes stale docker
containers matching the cua-house-env naming convention. Master then
issues fresh `PoolOp.ADD_VM` calls via the reconciler to rebuild the
pool.

### Master restart

Workers keep running. All in-flight WS connections close; each worker
enters its reconnect supervisor with exponential backoff. On master
start, `ClusterPoolSpec.load_from_disk` restores desired state from
`runtime_root/cluster-pool-spec.json`, workers reconnect, and the
reconciler re-converges (no-op if the VMs are still running).

**Tasks in flight during master restart are lost from master's view.**
If a task's lease terminates via worker's client HTTP API after master
restart, the worker's `TaskCompleted` arrives at a master that has no
record of the task — it's logged at DEBUG and dropped. Clients should
treat master restart as a batch-level failure.

### Template pull races

`DockerQemuRuntime.pull_template` is idempotent (checks the local path
first) and serialized by `_mutation_lock`. Two concurrent `ADD_VM` ops
for the same image only pull once.

## Security model

* **Cluster join token** — one shared secret per deployment, env var
  `CUA_HOUSE_CLUSTER_JOIN_TOKEN`. Used for master-worker WS auth.
  Future work: mTLS.
* **Client API token** — the existing `CUA_HOUSE_TOKEN` / Bearer auth
  applies to every HTTP endpoint (batches, cluster ops, lease proxy,
  worker lease API) uniformly.
* **Network assumptions** — cluster traffic is expected to be inside a
  trusted VPC. The master WS endpoint and worker HTTP lease endpoints
  currently rely on VPC firewall rules (see
  [deployment/cluster.md](../deployment/cluster.md)) for network
  isolation. No TLS between master and worker.

## Shape-aware pool & worker-local snapshot cache

A **shape** is the tuple `(image, image_version, vcpus, memory_gb,
disk_gb)`. It is the scheduling unit — dispatcher, pool spec, and
reconciler all key on the full tuple.

### Dispatch matching

`ClusterDispatcher._pick_worker` selects a worker VM that satisfies
`image == req.image AND vcpus >= req.vcpus AND memory_gb >= req.memory_gb
AND disk_gb >= req.disk_gb AND state == "ready"`. Among candidates it
prefers smallest-fit (avoid wasting a large VM on a small task), then
least-leased worker.

If no VM matches, the task stays `QUEUED`. There is no implicit scale-up
or silent GCP overflow — operator sets the pool spec.

### Worker-local snapshot cache

GCS holds only base images (no per-shape savevm snapshots). The first
time a worker boots a VM for a never-seen shape, it cold-boots (~4-5
min) and QMP savevm's into the slot qcow2. That slot is then reflinked
into the cache directory at
`<snapshot_cache_dir>/<image>/v<version>/<vcpus>vcpu-<memory>gb-<disk>gb.qcow2`.

The next `add_vm` for the same shape finds the cached qcow2, reflinks it
to the new slot, and uses `-loadvm` to resume in seconds.

Cache invalidation triggers:
- **Image version bump** — new `version:` in catalog; old cache dirs are
  purged on next `ADD_IMAGE`.
- **QEMU/docker upgrade** — `qemu_fingerprint` (sha256 of qemu version +
  docker image id) stored per sidecar; startup sweep evicts mismatches.

Cache write failure is non-fatal: the VM still serves tasks normally,
the next `add_vm` will cold-boot again. No data loss scenario.

## What's deliberately NOT in the cluster

* **Auto-scaling** — `ClusterPoolSpec` is operator-set. The reconciler
  never adds capacity because a task queued up. Rationale: the first
  real workload is small (2 workers), and auto-scaling introduces
  thrash that's not worth solving before we have production usage.
* **GCP overflow dispatch** — `_pick_worker` returns None if no worker
  matches; the task stays QUEUED. The code carries a `TODO(phase 6)`
  sketch for wiring `GCPVMRuntime` as a fallback when no worker can
  host an image (e.g. GPU tasks). This will require a new
  `/v1/gcp-leases/*` namespace on master.
* **Lease migration across workers** — leases are pinned. If a worker
  dies mid-task the task fails.
* **Web dashboard** — only JSON API (`/v1/cluster/*`) so far. The
  dashboard is a follow-up PR consuming the same API.
* **Cross-region coordination** — all workers in this model are
  assumed to share low latency to the master.

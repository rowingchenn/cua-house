# Cluster architecture

The multi-node cluster splits cua-house into a **master control plane**
and a fleet of **worker data-plane** nodes. This doc describes state
ownership, protocols, and failure modes. For the operator deployment
recipe see [deployment/cluster.md](../deployment/cluster.md).

## Problem this solves

Single-host cua-house hits three ceilings:

1. **Capacity** — one host's CPU / memory / qcow2 storage is the whole
   pool; scaling is vertical only.
2. **Image rigidity** — image catalog loaded at process start; adding
   an image needs a restart.
3. **Blast radius** — a host crash stalls every outstanding task.

The cluster splits these: any number of workers host images
independently, the master routes tasks to whichever worker can host
them, and a worker failure only affects tasks bound to that worker.

## Role split

```
                  ┌──────────────────────────────────────────────┐
     clients ───▶ │  MASTER                                       │
                  │  ─ FastAPI (batch admission)                  │
                  │  ─ ClusterDispatcher (event-driven placement) │
                  │  ─ WorkerRegistry (heartbeats + cached_shapes)│
                  │  ─ RpcCoordinator (AssignTask ↔ TaskBound RPC)│
                  │  ─ /v1/leases/* HTTP proxy → worker           │
                  └──┬────────────────────────────────┬───────────┘
                     │ WebSocket                       │ WebSocket
                     ▼                                 ▼
           ┌──────────────────┐              ┌──────────────────┐
           │   WORKER A       │              │   WORKER B       │
           │  ─ EnvScheduler  │              │  ─ EnvScheduler  │
           │    (lease state) │              │    (lease state) │
           │  ─ DockerQemu    │              │  ─ DockerQemu    │
           │    runtime       │              │    runtime       │
           │  ─ lease HTTP API│              │  ─ lease HTTP API│
           │  ─ WorkerCluster │              │  ─ WorkerCluster │
           │    Client (WS)   │              │    Client (WS)   │
           └──────────────────┘              └──────────────────┘
                     ▲                                 ▲
                     │ direct client traffic           │ direct client traffic
                     │ (VM service URLs)               │ (VM service URLs)
                     └──────────── clients ────────────┘
```

### Ephemeral-VM model

Each task's VM lifetime equals its task's lifetime:

1. Master sends `AssignTask(task_id, image_key, vcpus, memory_gb,
   disk_gb, ...)`.
2. Worker calls `runtime.provision_vm(...)`:
   - **Cache hit** (shape already warmed on this worker): reflink
     cached qcow2 into a new slot, `docker run -e LOADVM_SNAPSHOT=...`,
     ready in ~30s.
   - **Cache miss**: reflink template, cold-boot Windows/Ubuntu (~4-5
     min), QMP savevm into the slot, reflink slot → cache for next time.
3. Worker registers the handle with its local scheduler (for lease
   HTTP plumbing), replies `TaskBound(vm_id, from_cache, urls,
   novnc_url)`.
4. Client interacts directly with the worker's lease API + VM URL.
5. On `POST /v1/leases/{id}/complete` (or master-initiated
   `ReleaseLease`), the worker calls `runtime.destroy_vm(handle)`:
   removes the docker container, releases ports, deletes the slot
   qcow2. Handle + scheduler records go away.

There is no READY pool. A second same-shape task on the same worker
re-provisions from cache (~30s); a second shape on the same worker pays
another cold-boot. Caches survive worker restarts (persistent
`snapshot_cache_dir`), so only the first-ever task on a new worker pays
the full cold-boot for a given shape.

### Master (control plane)

* **Batch admission**: `POST /v1/batches`, `GET /v1/batches/{id}`,
  `GET /v1/tasks/{id}`, cancel — implemented by `ClusterDispatcher`.
  At submission, tasks whose `(vcpus, memory_gb)` exceed every online
  worker's single-machine capacity fail immediately with
  `no_worker_fits`.
* **Placement**: event-driven. On batch submit + `TaskCompleted` +
  worker disconnect, the dispatcher iterates still-QUEUED tasks and
  calls `pick_worker`. No polling loop.
* **Worker orchestration**: accepts WS connections at
  `/v1/cluster/ws`, tracks online workers in `WorkerRegistry`.
  Heartbeats carry `vm_summaries` (in-flight VMs) and `cached_shapes`
  (snapshot cache contents for affinity).
* **Cluster API**: `GET /v1/cluster/workers`, `GET /v1/cluster/status`,
  `GET /v1/cluster/tasks`, `GET /v1/cluster/batches` — read-only
  monitoring for operators + dashboard.
* **Lease proxy**: forwards `POST /v1/leases/{id}/{heartbeat,complete,
  stage-runtime,stage-eval}` from master to the owning worker, so a
  client using a single `CUA_HOUSE_SERVER_URL` still works against a
  cluster.
* **No local VMs**: master does not provision; its `DockerQemuRuntime`
  is a throwaway for API surface only.

Master is intentionally **out of the per-lease data path**. Clients
that read `TaskAssignment.lease_endpoint` connect directly to the
worker's public HTTP interface for lease and VM traffic. The master
lease proxy is a compatibility path.

### Worker (data plane)

* `WorkerClusterClient` dials master, sends `Register` with capacity
  + `hosted_images`, maintains a send loop (Heartbeats every 5s) and
  a recv loop (AssignTask / ReleaseLease / Shutdown).
* On `AssignTask`: provision_vm + register the handle into the local
  `EnvScheduler.bind_provisioned_task(...)` → reply `TaskBound` with
  rewritten public URLs.
* On `ReleaseLease`: delegate to `EnvScheduler.complete(lease_id)`,
  which internally calls `runtime.destroy_vm(handle)`.
* Client-driven `POST /v1/leases/{id}/complete` goes straight to the
  worker's HTTP API, hits the scheduler's complete path, and fires
  `task_finalized_callback` → `TaskCompleted` over WS.
* Worker startup **prewarms all enabled image templates in parallel**
  from GCS before joining the cluster. A pull failure fails the
  manually started worker process. No lazy template pull at task time.

### Clients

Read `TaskAssignment` from master:

1. **Lease operations** → `assignment.lease_endpoint` (e.g.
   `http://worker.public:8787`). Direct to worker.
2. **VM service** → `assignment.urls[port]` (e.g.
   `http://worker.public:16000`). Direct to worker's docker-proxied VM
   port.

Legacy clients that only know master's URL hit master's lease proxy
for (1) and still go direct for (2). Both work.

## State ownership

| State                         | Owner                           | Persistence                        |
|-------------------------------|---------------------------------|------------------------------------|
| Batch / task records          | Master `ClusterDispatcher`      | In-memory (lost on master crash)   |
| Capacity ledger               | Master `ClusterDispatcher._worker_load` | In-memory, rebuilt from own assignment decisions |
| Worker connection info        | Master `WorkerRegistry`         | In-memory (re-populated on Register) |
| Lease record + VM handle      | Worker `EnvScheduler`           | In-memory (lost on worker crash)   |
| Running VM's docker container | Worker `DockerQemuRuntime`      | Docker daemon (survives worker process restart; cleanup sweep on next start) |
| Snapshot cache (qcow2 per shape) | Worker local filesystem      | `snapshot_cache_dir` on persistent XFS |
| Task data                     | Per-worker GCE PD + per-worker OverlayFS | PD normally mounted read-only, upper layer on worker XFS |

**Master's capacity ledger is authoritative.** Free vcpus / memory on
each worker = `total - reserved - sum(assigned RUNNING task shapes
the dispatcher itself assigned)`. Heartbeat `vm_summaries` is for
monitoring / drift detection only, never for placement math — stale
heartbeats must never cause over-booking.

## Wire protocol

WebSocket endpoint: `ws://<master>:8787/v1/cluster/ws`. Auth is a
shared `CUA_HOUSE_CLUSTER_JOIN_TOKEN` sent as `Authorization: Bearer
<token>`.

All frames are JSON `Envelope` objects defined in `cluster/protocol.py`:

```json
{
  "msg_id": "<uuid>",
  "correlation_id": "<uuid or null>",
  "payload": { "kind": "<message kind>", ... }
}
```

`correlation_id` links replies to requests. The master-side
`RpcCoordinator` (`cluster/coordinator.py`) is a generic future-based
request/response table used for `AssignTask` ↔ `TaskBound` and
`ReleaseLease` ↔ `TaskReleased`.

### Worker → master

| Kind                 | Purpose                                                           |
|----------------------|-------------------------------------------------------------------|
| `register`           | First frame on connect. Capacity + hosted_images.                 |
| `heartbeat`          | Periodic (~5s). `vm_summaries` (in-flight) + `cached_shapes`.     |
| `task_bound`         | Reply to an `AssignTask`. Includes `vm_id`, `from_cache`, `lease_endpoint`, `urls`, `novnc_url`. |
| `task_released`      | Reply to a master-initiated `ReleaseLease`.                       |
| `task_completed`     | Async notification when a lease terminates via the worker's HTTP. |
| `task_phase_result`  | Reserved for future stage-phase reporting.                        |

### Master → worker

| Kind             | Purpose                                                                 |
|------------------|-------------------------------------------------------------------------|
| `assign_task`    | Provision a VM with the given shape + bind task to it. Only VM-lifecycle message master ever sends. |
| `release_lease`  | Master-initiated teardown (batch cancel path).                          |
| `stage_phase`    | Reserved (future: staging driven from master).                          |
| `shutdown`       | Graceful drain (not yet implemented).                                   |

## Placement

`ClusterDispatcher._pick_worker(task)`:

1. **Capacity hard gate**: filter to workers where `free_vcpus ≥
   task.vcpus AND free_memory_gb ≥ task.memory_gb` (master's ledger,
   not heartbeat).
2. **Cache affinity soft preference**: prefer workers whose
   `cached_shapes` contains the exact `(image, version, vcpus, memory,
   disk)` tuple. Cache hit means `loadvm ~30s`; miss means cold boot
   ~5min.
3. **Least-loaded tiebreak**: among equally-preferred candidates, pick
   the worker with the fewest active_task_count.
4. **Stable tiebreak**: `worker_id` lexicographic.

Admission check runs at `submit_batch`: if a task's `(vcpus,
memory_gb)` exceeds every online worker's maximum allocatable
capacity, mark it FAILED immediately with error `no_worker_fits: task
requires X vCPU / Y GiB but largest online worker offers A vCPU / B
GiB`. This is the equivalent of "unknown image" — no point queueing
a task no worker can ever serve.

## Event-driven placement

No dispatch loop. Placement fires on:

* **Batch submit** — each admitted task → `asyncio.create_task(try_place)`.
* **TaskCompleted** — scheduler releases capacity → `reevaluate_queued`
  iterates all QUEUED tasks.
* **Worker disconnect** — orphaned tasks requeue with `retry_count++`;
  `retry_count > 2` → FAILED.

`try_place` does `pick_worker` → `AssignTask` → await `TaskBound` via
coordinator. On failure (worker unreachable, provision timeout,
TaskBound with `ok=False`), the assignment is reverted and the task
is retried on the next capacity-free event.

## Failure modes

### Worker WS disconnect

Handled in two places:

1. **Immediate** — `cluster_ws` finally-block calls
   `registry.mark_offline` + `dispatcher.handle_worker_disconnect`.
2. **Timeout** — a periodic reaper on the registry catches workers
   whose heartbeats stopped without a clean disconnect.

`handle_worker_disconnect(worker_id)` clears the worker's capacity
ledger, requeues its in-flight tasks with `retry_count++` (or FAILs
if > 2), and fires `reevaluate_queued` to place them on other workers.

### Worker process restart

The worker's VM containers survive a worker process restart. On next
start, the runtime's `cleanup_orphaned_state` hook removes stale cua-
house-owned docker containers; the persistent `snapshot_cache_dir`
remains (so subsequent tasks still hit cache). Leases and task
handles live only in worker memory, so any in-flight task on that
worker during the restart is orphaned and master will fail it via
the disconnect path.

### Master restart

Workers keep running with their VMs intact. Each worker enters its
reconnect supervisor with exponential backoff. Master starts with
empty task state. Tasks in flight during master restart are lost
from master's view; if a worker later sends `TaskCompleted`, master
logs + drops it. Clients should treat master restart as a batch-level
failure.

### Template pull failure

Worker prewarm at startup: if any enabled image's GCS pull fails,
`prewarm_templates` raises, lifespan fails, and uvicorn exits. Restart
the worker manually after fixing the underlying issue. `pull_template` is
idempotent (checks file existence first), so partial prewarm from a
previous attempt is safe.

## Security

* **Cluster join token** — one shared secret per deployment, env var
  `CUA_HOUSE_CLUSTER_JOIN_TOKEN`. Used for master-worker WS auth.
  Future work: mTLS.
* **Client API token** — `CUA_HOUSE_TOKEN` / Bearer auth applies to
  every HTTP endpoint (batches, cluster ops, lease proxy, worker
  lease API) uniformly.
* **Network assumptions** — cluster traffic is inside a trusted VPC;
  master WS and worker HTTP lease endpoints rely on VPC firewall rules
  for network isolation. No TLS between master and worker.

## Snapshot cache

A **shape** is `(image, image_version, vcpus, memory_gb, disk_gb)`. It
is the cache key and the unit of cache affinity.

GCS holds only base image templates. The first time a worker boots a
shape, it cold-boots, QMP `savevm`s into the slot qcow2, and reflinks
the slot into `<snapshot_cache_dir>/<image>/v<version>/<vcpus>vcpu-<memory>gb-<disk>gb.qcow2`.
Subsequent `provision_vm` calls for the same shape reflink the cached
qcow2 into a new slot and start the container with `-loadvm`.

Cache invalidation:
- **Image version bump** — operator follows the SOP in
  `docs/operations/vm-image-maintenance.md` (stop workers → rm cache
  → update yaml → restart).
- **QEMU / docker upgrade** — `qemu_fingerprint` (sha256 of qemu
  version + docker image id) stored per sidecar; startup sweep evicts
  fingerprint mismatches.

Cache write failure is non-fatal: the VM still serves its task, the
next same-shape task will cold-boot again.

## What's deliberately NOT in the cluster

* **No pool, no desired state** — workers don't pre-boot VMs. Every
  task provisions fresh. Rationale: in the ephemeral-VM model, the
  pool abstraction mostly hid an expensive state machine (VMState,
  VMRecord, revert/BROKEN/auto-replace) that added no value over "just
  destroy and recreate on demand."
* **No auto-scaling** — the cluster has no horizontal provisioning
  today. Capacity = the sum of online workers.
* **No GCP overflow dispatch** — `_pick_worker` returns None if no
  worker matches; the task stays QUEUED. A future PR may add a
  master-side GCPVMRuntime fallback for tasks no worker can host
  (e.g. GPU).
* **No lease migration across workers** — leases are pinned. If a
  worker dies mid-task the task requeues (up to `retry_count > 2`).
* **No cross-region coordination** — all workers share low latency
  to the master.

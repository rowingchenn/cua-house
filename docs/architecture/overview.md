# Architecture overview

## Purpose

cua-house allocates, manages, and recycles computer-use VM sandboxes for agent evaluation. Agents interact with Windows VMs through a CUA (Computer Use Agent) server running inside each VM. The orchestration server handles batch submission, task scheduling, lease management, VM lifecycle, task data staging, and reverse proxying.

## Package roles

### cua-house-common (`packages/common/`)

Shared types used by both server and client:

- **State enums**: `TaskState`, `BatchState` (StrEnum)
- **Request models**: `BatchCreateRequest`, `TaskRequirement`, `LeaseCompleteRequest`, `BatchCancelRequest`
- **Response models**: `TaskStatus`, `BatchStatus`, `TaskAssignment`, `LeaseHeartbeatResponse`, `LeaseStageResponse`
- **Utilities**: `JsonlEventLogger` for structured JSONL event logging, `utcnow()` helper

### cua-house-client (`packages/client/`)

Async HTTP client SDK (`EnvServerClient`) for submitting batches, polling task status, sending heartbeats, completing leases, and staging task data. Uses `httpx.AsyncClient`. Reads `CUA_HOUSE_SERVER_URL` and `CUA_HOUSE_TOKEN` from environment.

### cua-house-server (`packages/server/`)

FastAPI orchestration server. Submodules:

| Submodule | Path | Responsibility |
|-----------|------|----------------|
| **api** | `api/` | FastAPI app factory, route handlers, bearer auth, reverse proxy, master lease proxy |
| **scheduler** | `scheduler/` | Task / batch / lease lifecycle. In the ephemeral-VM model, holds a `VMHandle` per running task and drives provision/destroy via the runtime |
| **runtimes** | `runtimes/` | `DockerQemuRuntime` (local nested KVM + snapshot cache) and `GCPVMRuntime` (cloud). Public API: `provision_vm`, `destroy_vm`, `list_cached_shapes` |
| **qmp** | `qmp/` | QEMU Machine Protocol client (savevm over docker exec + nc) |
| **data** | `data/` | Task data validation + staging via `/data-store:ro` mount + symlink injection into the container's Samba share |
| **config** | `config/` | YAML loader for `HostRuntimeConfig`, `ClusterConfig`, `ImageSpec` catalog |
| **admin** | `admin/` | Image bake workflow |
| **cluster** | `cluster/` | Master/worker control plane: WS `protocol`, `WorkerRegistry`, `ClusterDispatcher` (event-driven placement with capacity ledger + cache affinity + admission check), `RpcCoordinator` (AssignTask↔TaskBound), `WorkerClusterClient` |
| **_internal** | `_internal/` | Thread-safe `PortPool` for CUA/noVNC port allocation |

## Deployment modes

cua-house has three modes of operation selected by `--mode`:

- **standalone** (default): one process hosts batches, provisions VMs locally per task, and serves lease API. Used for single-node dev.
- **master**: control plane only. Accepts batches, places tasks on workers via WebSocket, and forwards lease-scoped HTTP calls to the owning worker. Does not provision any VMs itself.
- **worker**: joins a master and provisions a fresh VM per master-dispatched task (cache hit → loadvm ~30s; miss → cold-boot + savevm ~5min). Serves lease HTTP API directly to clients using its public network interface. Sees `master_url` in its `cluster:` config section.

See [cluster architecture](cluster.md) and [cluster deployment](../deployment/cluster.md) for the multi-node model, protocol, and operator recipe.

## State machines

### Task lifecycle

```
QUEUED ─── pick_worker + AssignTask ──▶ STARTING ──▶ READY ──▶ LEASED ──▶ RESETTING ──▶ COMPLETED
  │                                         │                                              │
  │ no_worker_fits (admission)              │ provision failed,                            │
  │ worker_disconnected (retry>2)           │ worker unreachable                           ▼
  ▼                                                                                    FAILED
FAILED
```

- **QUEUED**: task accepted, waiting for a worker with matching capacity (and ideally cache).
- **STARTING**: master has sent `AssignTask`; worker is provisioning the VM.
- **READY**: worker replied `TaskBound`; lease + VM URLs available to client.
- **LEASED**: agent actively using the VM; client heartbeats keep the lease alive.
- **RESETTING**: `POST /v1/leases/{id}/complete` received; worker is calling `destroy_vm`.
- **COMPLETED**: terminal success.
- **FAILED**: terminal failure — admission reject (`no_worker_fits`), provision error, lease expiry, cancel, or worker disconnect past the retry budget.

### VM lifecycle (ephemeral, 1-to-1 with task)

```
[task AssignTask] ──▶ provision_vm ──▶ (cache hit: loadvm ~30s) ──▶ bind to task ──▶ run
                                      (cache miss: cold-boot + savevm ~5min)
                                                                              │
             on complete / release / timeout                                   ▼
                                                                         destroy_vm ──▶ [gone]
```

There is no READY pool; each VM exists only for its task's duration. Caches (qcow2 snapshots) persist across tasks on the same worker so only the first-ever same-shape task pays the cold-boot cost.

## Runtime backends

### DockerQemuRuntime (local)

- Runs QEMU inside Docker containers (`trycua/cua-qemu-windows:latest`).
- One public lifecycle: `provision_vm(image, vcpus, memory_gb, disk_gb) → VMHandle`, `destroy_vm(handle)`, `list_cached_shapes() → list[CacheKey]`.
- Snapshot cache at `snapshot_cache_dir` (required for worker/standalone mode): reflinks cached qcow2 into the new slot on cache hit, else cold-boot + savevm writes a new entry.
- Hairpin NAT POSTROUTING rule installed on container start so client traffic to published ports reaches the guest correctly.
- CUA server on guest port 5000, noVNC on 8006 inside each container; ports published on the worker's public IP.

### GCPVMRuntime (cloud)

- Creates GCP Compute Engine VMs from images or snapshots.
- Boot disk from GCP image (fast, ~14s) or snapshot (slower, ~100s).
- Optional data disk from snapshot for task data.
- CUA server on port 5000 on the VM's external IP.
- VMs destroyed on `destroy_vm`.

## Future directions

- Docker-xfce Linux runtime (lightweight alternative for non-Windows tasks)
- Android QEMU runtime
- GCP overflow wired into cluster dispatcher (GPU tasks without a nested-KVM worker)
- Web dashboard consuming `/v1/cluster/*` endpoints
- mTLS in place of the shared cluster join token
- Tenant isolation and quota management

# Architecture overview

## Purpose

cua-house allocates, manages, and recycles computer-use VM sandboxes for agent evaluation. Agents interact with Windows VMs through a CUA (Computer Use Agent) server running inside each VM. The orchestration server handles batch submission, task scheduling, lease management, VM lifecycle, task data staging, and reverse proxying.

## Package roles

### cua-house-common (`packages/common/`)

Shared types used by both server and client:

- **State enums**: `TaskState`, `BatchState` (StrEnum)
- **Request models**: `BatchCreateRequest`, `TaskRequirement`, `LeaseCompleteRequest`, `BatchCancelRequest`
- **Response models**: `TaskStatus`, `BatchStatus`, `TaskAssignment`, `LeaseHeartbeatResponse`, `LeaseStageResponse`
- **Config models**: `VMPoolEntry`
- **Utilities**: `JsonlEventLogger` for structured JSONL event logging, `utcnow()` helper

### cua-house-client (`packages/client/`)

Async HTTP client SDK (`EnvServerClient`) for submitting batches, polling task status, sending heartbeats, completing leases, and staging task data. Uses `httpx.AsyncClient`. Reads `CUA_HOUSE_SERVER_URL` and `CUA_HOUSE_TOKEN` from environment.

### cua-house-server (`packages/server/`)

FastAPI orchestration server. Submodules:

| Submodule | Path | Responsibility |
|-----------|------|----------------|
| **api** | `api/` | FastAPI app factory, route handlers, bearer auth, reverse proxy, master lease proxy |
| **scheduler** | `scheduler/` | In-memory state machine, dispatch loop, lease reaper, batch heartbeat, cluster-external assignment hooks |
| **runtimes** | `runtimes/` | `RuntimeBackend` protocol, `DockerQemuRuntime` (now hot-pluggable), `GCPVMRuntime` |
| **qmp** | `qmp/` | QEMU Machine Protocol client (savevm/loadvm via docker exec + nc) |
| **data** | `data/` | Task data validation, guest staging (Samba, NTFS ACL, legacy HTTP upload) |
| **config** | `config/` | YAML config loader for `HostRuntimeConfig`, `ClusterConfig`, and `ImageSpec` catalog |
| **admin** | `admin/` | Image bake workflow (install agent tooling into golden images) |
| **cluster** | `cluster/` | Multi-node control plane: WS protocol, `WorkerRegistry`, `ClusterDispatcher`, `PoolReconciler`, `WorkerClusterClient` |
| **_internal** | `_internal/` | Thread-safe `PortPool` for CUA/noVNC port allocation |

## Deployment modes

cua-house has three modes of operation selected by `--mode`:

- **standalone** (default): one process hosts batches, dispatches tasks, owns a local VM pool, and serves lease API. This is the legacy single-node behavior described in the rest of this document.
- **master**: control plane only. Accepts batches, orchestrates workers over a WebSocket, and forwards lease-scoped HTTP calls to the owning worker. Does **not** host VMs.
- **worker**: joins a master and hosts a dynamic VM pool driven by master's pool reconciler. Serves lease HTTP API directly to clients using its public network interface. Sees `master_url` in its `cluster:` config section.

See [cluster architecture](cluster.md) and [cluster deployment](../deployment/cluster.md) for the multi-node model, protocol, and operator recipe.

## State machines

### Task lifecycle

```
QUEUED -----> STARTING -----> READY -----> LEASED -----> RESETTING -----> COMPLETED
  |              |                           |                              |
  |              |                           |                              v
  +--------------+---------------------------+-------------------------> FAILED
  (impossible    (start failed)             (heartbeat expired,
   resource req)                             cancelled, error)
```

- **QUEUED**: task accepted, waiting for a free slot/VM.
- **STARTING**: slot is being prepared and VM is booting (or being reverted).
- **READY**: VM is running and CUA server responds 200 on `/status`. Lease is created.
- **LEASED**: agent is actively using the VM. Requires periodic heartbeats.
- **RESETTING**: lease completed/expired, VM is being destroyed or reverted.
- **COMPLETED**: terminal success state.
- **FAILED**: terminal failure state (timeout, cancel, start error, or impossible resource request).

### VM lifecycle (snapshot pool, local runtime)

```
BOOTING -----> SNAPSHOTTING -----> READY -----> LEASED -----> REVERTING -----> READY
                                     ^                                          |
                                     +------------------------------------------+
                                                  (loadvm revert)
```

- **BOOTING**: Docker container started, QEMU booting Windows.
- **SNAPSHOTTING**: CUA server ready, saving initial QEMU snapshot (savevm).
- **READY**: snapshot saved, VM available for assignment.
- **LEASED**: VM assigned to a task.
- **REVERTING**: loadvm restoring snapshot state after task completion.
- **BROKEN**: VM unhealthy, will be replaced.

### Slot lifecycle (legacy per-task containers)

```
EMPTY -----> STARTING -----> READY -----> LEASED -----> RESETTING -----> EMPTY
```

- Each task gets a fresh Docker container with a QCOW2 overlay.
- Container is destroyed after task completion.

## Runtime backends

### DockerQemuRuntime (local)

- Runs QEMU inside Docker containers (`trycua/cua-qemu-windows:latest`).
- Two modes: legacy per-task containers (slot lifecycle) or persistent VM pool (snapshot lifecycle).
- VM pool: pre-boots N VMs at startup, takes QEMU snapshots, uses loadvm for fast revert.
- CUA server on port 5000, noVNC on port 8006 inside each container.

### GCPVMRuntime (cloud)

- Creates GCP Compute Engine VMs from images or snapshots.
- Boot disk from GCP image (fast, ~14s) or snapshot (slower, ~100s).
- Optional data disk from snapshot for task data.
- CUA server on port 5000 on the VM's external IP.
- VMs auto-deleted on reset.

## Future directions

- Docker-xfce Linux runtime (lightweight alternative for non-Windows tasks)
- Android QEMU runtime
- GCP overflow wired into cluster dispatcher (GPU tasks without a nested-KVM worker)
- Web dashboard consuming `/v1/cluster/*` endpoints
- mTLS in place of the shared cluster join token
- Tenant isolation and quota management

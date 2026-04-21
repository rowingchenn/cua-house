# Prompt: Build the CUA-House noVNC Dashboard

## Goal

Build a web dashboard for operators to:

1. See every worker, live capacity, cached shapes, and in-flight VMs.
2. Open noVNC for any currently running VM.
3. Avoid showing cached shapes as if they were connectable VMs.

## Current System

```text
Browser
  |
  v
Master (:8787)                 control and monitoring only
  | WebSocket
  +-- Worker kvm02 (:8787)     in-flight VMs + cached_shapes
  +-- Worker kvm03 (:8787)     in-flight VMs + cached_shapes
```

- CUA-House now uses an ephemeral-VM model: each task gets one VM, and
  that VM is destroyed when the task completes.
- There is no ready/idle VM pool.
- `cached_shapes` are warmed qcow2 cache entries on disk, not running
  VMs and not noVNC targets.
- Worker HTTP port `8787` proxies lease-bound VM services via Host
  header routing. Docker-published VM ports are not assumed to be
  directly reachable from browsers.

## APIs

### Master API

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Health check |
| GET | `/v1/cluster/workers` | Worker capacity, in-flight VMs, cached shapes |
| GET | `/v1/cluster/tasks` | List tasks, optional `?state=` filter |
| GET | `/v1/cluster/batches` | List batches, optional `?state=` filter |
| GET | `/v1/cluster/status` | Cluster totals and task state counts |
| GET | `/v1/tasks/{task_id}` | Single task status and assignment |

There is no `/v1/cluster/pool` endpoint.

### `GET /v1/cluster/workers` Shape

```json
[
  {
    "worker_id": "kvm02",
    "online": true,
    "runtime_version": "0.1.0",
    "capacity": {
      "total_vcpus": 32,
      "total_memory_gb": 128,
      "total_disk_gb": 500,
      "reserved_vcpus": 2,
      "reserved_memory_gb": 8
    },
    "free_vcpus": 26,
    "free_memory_gb": 96,
    "active_task_count": 1,
    "connected_at": "2026-04-21T00:00:00Z",
    "last_heartbeat": "2026-04-21T00:00:05Z",
    "cached_shapes": [
      {
        "image_key": "cpu-free",
        "image_version": "20260419",
        "vcpus": 4,
        "memory_gb": 8,
        "disk_gb": 64
      }
    ],
    "vm_summaries": [
      {
        "vm_id": "32bd0ac5-3a0f-4fe5-b1d6-bf92fd5fcd92",
        "image_key": "cpu-free",
        "image_version": "20260419",
        "vcpus": 4,
        "memory_gb": 8,
        "disk_gb": 64,
        "from_cache": true,
        "lease_id": "abcd-1234",
        "public_host": "35.188.39.143",
        "published_ports": {"5000": 16000},
        "novnc_port": 18000
      }
    ]
  }
]
```

Important fields:

- `free_vcpus`, `free_memory_gb`, `active_task_count`: live scheduling
  view from the master capacity ledger.
- `cached_shapes`: warmed image/version/shape entries for cache-affinity
  placement. These are not running VMs.
- `vm_summaries`: in-flight VMs only. Every entry is task-bound and has
  a `lease_id`.
- `from_cache`: whether this VM started from a cached shape.

## noVNC Routing

Existing Host-header proxy URL:

```text
http://novnc--<lease_id>.<worker_public_base_host>:8787/novnc/
```

The worker parses the Host header as:

```text
<service>--<lease_id>.<public_base_host>
```

For `service=novnc`, the proxy routes to the VM's noVNC port and strips
the `/novnc` prefix.

## Dashboard Requirements

1. Poll `GET /v1/cluster/workers` every few seconds.
2. Display each worker's online status, total/free capacity, active task
   count, cached shapes, and in-flight VM list.
3. Make only `vm_summaries` clickable for noVNC. Do not make
   `cached_shapes` clickable.
4. Use the existing lease-host URL format for noVNC where possible.
5. If adding a backend convenience route, add it on the worker before the
   catch-all proxy route:

```text
GET /v1/vms/{vm_id}/novnc/
WS  /v1/vms/{vm_id}/novnc/websockify
```

That route should look up the active VM handle by `vm_id`, proxy to its
noVNC port, and return 404 once the VM is destroyed.

## Suggested UI

```text
+--------------------------------------------------+
| CUA-House Dashboard                              |
+------------------+-------------------------------+
| Workers          | noVNC Viewer                  |
|                  |                               |
| kvm02 online     | selected running VM           |
| 26/30 vCPU free  |                               |
| 1 active task    |                               |
| VM abcd...       |                               |
| cache cpu-free   |                               |
| cache ubuntu     |                               |
|                  |                               |
| kvm03 online     |                               |
| 30/30 vCPU free  |                               |
| 0 active tasks   |                               |
+------------------+-------------------------------+
```

## Relevant Files

| File | Purpose |
|---|---|
| `packages/server/src/cua_house_server/api/proxy.py` | Existing Host-header proxy |
| `packages/server/src/cua_house_server/api/app.py` | FastAPI app factory and catch-all registration |
| `packages/server/src/cua_house_server/api/cluster_routes.py` | `/v1/cluster/*` monitoring API |
| `packages/server/src/cua_house_server/runtimes/qemu.py` | VM container and port management |
| `packages/server/src/cua_house_server/cluster/worker_client.py` | Worker heartbeat and VM summary reporting |
| `packages/server/src/cua_house_server/cluster/protocol.py` | `WorkerVMSummary`, `CachedShape`, protocol models |

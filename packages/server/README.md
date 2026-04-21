# cua-house-server

Computer-use VM sandbox orchestration server. Manages the full lifecycle of Windows VMs for agent evaluation: batch submission, task scheduling, lease management, VM boot/revert, task data staging, and reverse proxying.

## Submodule map

```
src/cua_house_server/
  cli.py              CLI entrypoint (argparse + uvicorn, port 8787)
  api/
    app.py            FastAPI app factory, lifespan, proxy catch-all
    routes.py         API route handlers (/v1/batches, /v1/tasks, /v1/leases)
    auth.py           Bearer token authentication
    proxy.py          Reverse proxy for CUA and noVNC (HTTP + WebSocket)
  scheduler/
    core.py           EnvScheduler: task/batch/lease lifecycle; ephemeral VM handle per running task
    models.py         LeaseRecord
  runtimes/
    qemu.py           DockerQemuRuntime: provision_vm / destroy_vm / list_cached_shapes over nested Docker+QEMU
    gcp.py            GCPVMRuntime: same surface over GCP Compute Engine VMs
    snapshot_cache.py Per-worker on-disk cache of savevm'd qcow2s keyed by (image, version, shape)
  qmp/
    client.py         QMP client (savevm/loadvm via docker exec + nc)
  data/
    staging.py        Task data validation, Samba staging, NTFS ACL isolation
  config/
    loader.py         YAML config loader (HostRuntimeConfig, ImageSpec)
    defaults/
      server.yaml     Default host runtime config
      images.yaml     Default image catalog
  admin/
    bake_image.py     Image bake workflow (install tooling into golden image)
  _internal/
    port_pool.py      Thread-safe port allocator for CUA/noVNC ports
```

## Configuration

Two YAML files control the server:

### server.yaml (host runtime config)

Key fields:

| Field | Description |
|-------|-------------|
| `host_id` | Unique host identifier |
| `host_external_ip` | External IP or `auto` (GCE metadata) |
| `public_base_host` | Base hostname for lease routing or `auto` ({ip}.sslip.io) |
| `runtime_root` | Directory for overlays, logs, events |
| `task_data_root` | Directory with task input/reference data |
| `snapshot_cache_dir` | Persistent path for per-shape qcow2 cache (required for worker/standalone) |
| `docker_image` | Docker image for QEMU containers |
| `heartbeat_ttl_s` | Lease heartbeat timeout (default: 60s) |
| `batch_heartbeat_ttl_s` | Batch heartbeat timeout (default: 30s) |
| `ready_timeout_s` | Max wait for VM boot (default: 900s) |

### images.yaml (image catalog)

Each entry defines a VM image with its runtime mode:

- **local**: requires `golden_qcow2_path`
- **gcp**: requires `gcp_project`, `gcp_zone`, `gcp_machine_type`, and either `gcp_boot_image` or `gcp_boot_snapshot`

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Health check |
| GET | `/v1/vms` | List VM pool instances and state |
| POST | `/v1/batches` | Submit a batch of tasks |
| GET | `/v1/batches/{id}` | Get batch status |
| POST | `/v1/batches/{id}/heartbeat` | Refresh batch TTL |
| POST | `/v1/batches/{id}/cancel` | Cancel a batch |
| GET | `/v1/tasks/{id}` | Get task status with assignment |
| POST | `/v1/leases/{id}/heartbeat` | Refresh lease TTL |
| POST | `/v1/leases/{id}/complete` | Complete a lease |
| POST | `/v1/leases/{id}/stage-runtime` | Stage task data for runtime phase |
| POST | `/v1/leases/{id}/stage-eval` | Stage task data for eval phase |
| * | `/{path}` | Reverse proxy to leased VM (CUA or noVNC) |

All endpoints require `Authorization: Bearer <token>` when `CUA_HOUSE_TOKEN` is set.

The reverse proxy routes requests based on the `Host` header: `lease-{id}.{public_base_host}` is forwarded to the corresponding VM's CUA server. Paths under `/novnc/` are forwarded to the noVNC endpoint.

## Running

```bash
uv run cua-house-server
uv run cua-house-server --host-config custom.yaml --image-catalog custom-images.yaml
uv run cua-house-server --host 0.0.0.0 --port 8787
```

## Dependencies

- `cua-house-common`
- `fastapi>=0.115.0`
- `httpx>=0.27.0`
- `pydantic>=2.8.0`
- `PyYAML>=6.0`
- `uvicorn>=0.27.0`
- `websockets>=12.0`

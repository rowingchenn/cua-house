# Multi-node cluster deployment

Standalone cua-house-server runs as a single process on one KVM host. In
cluster mode that single host is split into two roles:

- **Master** — a small control-plane VM. Accepts batch submissions, talks
  to workers over a WebSocket, and assigns each task to a worker on
  demand. Does NOT host VMs.
- **Worker** — a nested-KVM host (e.g. `agenthle-nested-kvm-02`). Pulls
  qcow2 templates from GCS, boots one VM per assigned task, and
  serves the per-lease HTTP API (`heartbeat`, `stage-runtime`, `complete`)
  directly to clients.

Clients talk to **master** for batch lifecycle and **worker** for everything
lease-scoped. The master is never in the per-task data path.

## Architecture at a glance

```
          client (agenthle)
              │
              │ POST /v1/batches
              ▼
         ┌────────────┐
         │   master   │────────────┐
         │  (control) │            │
         └────┬───────┘            │
              │ WebSocket          │ GET /v1/tasks/{id}
              │ (worker pulls)     │ returns assignment.lease_endpoint
              ▼                    ▼
    ┌──────────────┐     ┌──────────────┐
    │   worker A   │     │   worker B   │
    │  (kvm02)     │     │  (kvm03)     │
    │ ─ AssignTask │     │ ─ AssignTask │
    │ ─ lease API  │◀────┼─ client talks directly
    │ ─ docker/qemu│     │   via lease_endpoint
    │ ─ nested VM  │     │ ─ nested VM  │
    └──────────────┘     └──────────────┘
```

## Prerequisites

### VPC and firewall

All master/worker VMs must be on the **same VPC** (e.g. `agenthle-vpc`) so
the internal `10.x.x.x` range is routable between them. Required rules:

| Rule                       | Source       | Ports         | Target                 |
|----------------------------|--------------|---------------|------------------------|
| `agenthle-allow-env-server`| `0.0.0.0/0`  | tcp 8787      | tag `agenthle`         |
| `agenthle-allow-vm-ports`  | `10.0.0.0/8` | tcp 16000-18999 | tag `agenthle`       |

**Both master and worker VMs must carry the `agenthle` target tag.** The
master VM in particular — a fresh `gcloud compute instances create` does
not apply it automatically.

```bash
gcloud compute instances add-tags cua-house-master --tags=agenthle --zone=us-central1-a
```

### Per-worker task-data storage

Each worker owns its own GCE persistent disk for AgentHLE task data. The
disk is attached to exactly one worker as `device-name=task-data` in
`READ_WRITE` mode so that the node can refresh it, but during normal VM
runs it is mounted read-only at `/mnt/agenthle-task-data-ro`. The worker
uses `/mnt/agenthle-task-data`, an OverlayFS merged view with its writable
upper layer on `/mnt/xfs`; this is what `task_data_root` points to.

GCS is the source of truth. Before a worker is manually started, briefly
remount that node's lower data disk read-write, sync the canonical bucket,
then remount it read-only and start the worker:

```bash
cd /opt/cua-house
./scripts/start-worker.sh
```

Do not share one task-data disk across workers. The old shared
read-only-PD + OverlayFS model makes data updates operationally expensive
because the disk has to be detached from every node before it can be
updated.

### Persistent paths and reboot-safe addressing

Worker nodes can reboot. Do not store important code or data in
per-user home directories or temporary directories:

- Code checkout: `/opt/cua-house`, group-writable by the shared
  `cua-house` group.
- Host config and secrets: `/etc/cua-house/worker.yaml`,
  `/etc/cua-house/images.yaml`, `/etc/cua-house/worker.env`.
- Runtime/cache data: `/mnt/xfs/runtime-cluster`,
  `/mnt/xfs/snapshot-cache`, `/mnt/xfs/images`.
- Worker logs: `/var/log/cua-house/worker.log`.
- Task-data lower disk: `/mnt/agenthle-task-data-ro`.
- Task-data merged view: `/mnt/agenthle-task-data`.
- `/tmp` is only for transient scp/bootstrap artifacts and validation
  output; never make it a source of truth.

Do not hard-code worker IPs in config. `examples/worker.yaml` uses
`host_external_ip: auto`, `public_base_host: auto`, and
`cluster.worker_public_host: auto`. For `cluster.master_url`, prefer a
stable internal DNS name for the master rather than a raw IP, for example:

```yaml
cluster:
  master_url: ws://cua-house-master.us-central1-a.c.sunblaze-4.internal:8787/v1/cluster/ws
```

The worker's startup check (`create_app`'s worker branch) will refuse to
start if `task_data_root` is missing or unwritable — catches the mount
setup mistakes early.

## Configuration

### Cluster auth

Every master and worker must share one `CUA_HOUSE_CLUSTER_JOIN_TOKEN` env
var. The worker sends it as `Authorization: Bearer <token>` on the WS
upgrade; master rejects connections without it (if set).

```bash
export CUA_HOUSE_CLUSTER_JOIN_TOKEN=$(openssl rand -hex 32)
```

### Master config (`master-server.yaml`)

```yaml
host_id: cua-house-master
host_external_ip: auto
public_base_host: auto
runtime_root: /var/lib/cua-house/master-runtime
task_data_root: null            # master doesn't host task data
docker_image: trycua/cua-qemu-windows:latest
host_reserved_vcpus: 1
host_reserved_memory_gb: 1
batch_heartbeat_ttl_s: 3600
heartbeat_ttl_s: 3600
ready_timeout_s: 900
readiness_poll_interval_s: 5
published_port_range: [16000, 16999]
novnc_port_range: [18000, 18999]
mode: master
cluster:
  master_bind_path: /v1/cluster/ws
  heartbeat_interval_s: 5
  heartbeat_ttl_s: 30
```

Note: master mode does not require `snapshot_cache_dir` — master never
provisions VMs.

### Worker config (`/etc/cua-house/worker.yaml`)

Do not hand-maintain divergent worker YAMLs. New workers are rendered
from [`examples/worker.yaml`](../../examples/worker.yaml) by
[`scripts/clone-worker.sh`](../../scripts/clone-worker.sh). For existing
workers, keep the common fields aligned with that template; the only
per-node values should be identity and addressing:

- `host_id`
- `cluster.worker_id`
- `cluster.master_url`
- Optional explicit `host_external_ip`, `public_base_host`, or
  `cluster.worker_public_host` if `auto` is not suitable.

The current template uses `auto` for public host fields so most workers
only need `@@WORKER_ID@@` and `@@MASTER_URL@@` rendered.

```yaml
host_id: <worker-id>-worker
host_external_ip: auto
public_base_host: auto
runtime_root: /mnt/xfs/runtime-cluster    # separate from standalone runtime_root
snapshot_cache_dir: /mnt/xfs/snapshot-cache   # REQUIRED: persistent XFS for reflink
task_data_root: /mnt/agenthle-task-data   # OverlayFS view over per-worker RO data disk
docker_image: trycua/cua-qemu-windows:latest
host_reserved_vcpus: 2
host_reserved_memory_gb: 8
batch_heartbeat_ttl_s: 7200
heartbeat_ttl_s: 7200
ready_timeout_s: 900
readiness_poll_interval_s: 5
published_port_range: [16000, 16999]
novnc_port_range: [18000, 18999]
mode: worker
vm_bind_address: 0.0.0.0                   # publish VM ports to all IFs
cluster:
  master_url: ws://<master internal DNS>:8787/v1/cluster/ws
  worker_id: <worker-id>                   # unique identifier
  worker_public_host: auto
  worker_public_port: 8787
  heartbeat_interval_s: 5
  heartbeat_ttl_s: 30
  reconnect_min_backoff_s: 1
  reconnect_max_backoff_s: 30
```

### Images catalog

Shared between master and workers. Master uses it to validate submit
requests and resolve image defaults; workers use it to find local qcow2
template paths and GCS URIs at startup prewarm time and during
`AssignTask` handling.

```yaml
images:
  cpu-free:
    enabled: true
    os_family: windows
    published_ports: [5000]
    local:
      template_qcow2_path: /mnt/xfs/images/cpu-free/cpu-free-20260413.qcow2
      gcs_uri: gs://agenthle-images/templates/cpu-free/cpu-free-20260413.qcow2
      version: "20260413"       # bump when re-baking; invalidates worker snapshot cache
      default_vcpus: 4
      default_memory_gb: 8
      default_disk_gb: 64       # used when client omits disk_gb
```

## Startup sequence

Start master first, then any number of workers.

### Master

```bash
cd /opt/cua-house
export CUA_HOUSE_CLUSTER_JOIN_TOKEN=...
sudo mkdir -p /var/lib/cua-house/master-runtime /var/log/cua-house
sudo chown "$(id -u):$(id -g)" /var/lib/cua-house/master-runtime /var/log/cua-house
setsid nohup uv run python -m cua_house_server.cli \
  --host-config master-server.yaml \
  --image-catalog images.yaml \
  --host 0.0.0.0 --port 8787 --mode master \
  </dev/null >/var/log/cua-house/master.log 2>&1 &
disown
curl -sS http://127.0.0.1:8787/healthz    # {"status":"ok","mode":"master"}
```

### Worker

**To provision a brand-new worker VM** (including boot disk snapshot,
instance create, mount setup, config install, and dry-run validation),
run [`scripts/clone-worker.sh`](../../scripts/clone-worker.sh) — see
[clone-worker.md](clone-worker.md) for the end-to-end runbook. The clone
script does not start the worker process.

**To start a worker manually** on an already-provisioned host:

```bash
cd /opt/cua-house
./scripts/start-worker.sh
```

In either path, verify the worker registered with master:

```bash
curl -sS http://<master>:8787/v1/cluster/workers | python3 -m json.tool
```

## No pool to drive

There is no desired pool state to configure in the ephemeral-VM model
— master provisions a fresh VM per `AssignTask` at task-submission
time. Operators don't call any mutation endpoint at all; just submit
batches and master picks a worker with:

1. capacity (free_vcpus ≥ task.vcpus, free_memory_gb ≥ task.memory_gb;
   master-authoritative ledger, not heartbeat),
2. cache affinity (worker with the exact `(image, version, shape)` in
   its snapshot cache wins),
3. least-loaded among ties.

First-ever same-shape task on a worker cold-boots (~4-5 min) + writes
the cache. Every subsequent same-shape task on that worker reflinks the
cached qcow2 and resumes via `-loadvm` (~30 s).

## Submitting a batch

```bash
curl -sS -X POST http://<master>:8787/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"tasks":[{
    "task_id":"my-task",
    "task_path":"demo/demo_desktop_note",
    "snapshot_name":"cpu-free",
    "vcpus":4,
    "memory_gb":8
  }]}'
```

Client flow:

1. Poll `GET http://<master>/v1/tasks/my-task` until `state == "ready"`.
2. Read `assignment.lease_endpoint` (e.g. `http://10.128.0.14:8787`) and
   `assignment.urls[5000]` (the VM's CUA service).
3. Call `POST <lease_endpoint>/v1/leases/<lease_id>/stage-runtime` to
   prepare task data inside the VM.
4. Drive the VM via `assignment.urls[5000]` (or whatever guest ports
   the image declares).
5. Call `POST <lease_endpoint>/v1/leases/<lease_id>/complete` with
   `{"final_status":"completed"}`.
6. Worker destroys the VM and notifies master asynchronously; master
   transitions the task to `completed` and reflects it in the batch.

**Master ossifies the task view at `state=ready`.** Once READY, use the
lease endpoint on the worker for authoritative heartbeat/staging/complete.
Master only re-updates state when it receives `TaskCompleted` over WS.

## Operational gotchas

### 1. VPC mismatch

A master VM created without `--network=agenthle-vpc` lands on `default`
and cannot reach workers over internal IPs. `gcloud compute instances
describe <vm> --format='value(networkInterfaces[0].network)'` verifies.

### 2. Missing target tag

`agenthle-allow-env-server` has `targetTags: [agenthle]`. A master VM
without the tag gets filtered out by the firewall even when the rule
appears in the list.

### 3. VM port range closed

Worker's docker-proxy binds VM services on 16000–16999 (per image's
`published_ports`) and noVNC on 18000–18999. Without an explicit firewall
rule clients outside loopback can't reach them. Create
`agenthle-allow-vm-ports` once per VPC.

### 4. `vm_bind_address` default is 127.0.0.1

Standalone mode binds docker `-p` to loopback so only the host's own
reverse proxy can reach the VM. In worker mode set
`vm_bind_address: 0.0.0.0` explicitly so clients can connect.

### 5. `task_data_root` unwritable

OverlayFS upper layer is frequently owned `root:root` by default. Worker
mode now fails fast in `create_app` if the directory is missing or not
writable by the current user. Fix with:

```bash
sudo chown -R $(id -un):$(id -gn) /mnt/xfs/task-data-upper /mnt/xfs/task-data-work
sudo chown $(id -un):$(id -gn) /mnt/agenthle-task-data
```

### 6. `CUA_HOUSE_CLUSTER_JOIN_TOKEN` mismatch

Silent failure mode: worker logs `Worker WS disconnected: ...` in an
infinite reconnect loop. Check both sides export the same value in the
environment where `uv run` was launched.

## Failure modes and recovery

### Worker crashes or disconnects

WebSocket close takes effect instantly via the `master_ws` finally
hook: `registry.mark_offline()` + `dispatcher.handle_worker_disconnect()`.
Heartbeat timeout is the second safety net — a periodic reaper on the
registry catches workers whose heartbeats stopped without a clean close.

On disconnect, `handle_worker_disconnect` requeues every in-flight task
bound to that worker (`state=queued`, `metadata.retry_count++`) and
fires `reevaluate_queued` to place them on other online workers. Tasks
that have hit `retry_count > 2` fail permanently with `error="worker
<id> disconnected; exceeded retry budget"`.

Workers reconnect automatically with exponential backoff. No pool
convergence is needed on rejoin — the new ephemeral-VM model never
pre-provisions anything; the worker just starts handling new
`AssignTask` messages as they arrive.

### Master crashes

Workers keep running and continue serving any in-flight leases via
their HTTP API. New batches can't be submitted until master is back.
On restart, master starts with empty task state and waits for workers
to reconnect. In-flight leases from before the crash are lost from
master's view — if such a lease completes after restart, the worker's
`TaskCompleted` message is dropped (master has no record). Clients
should treat master restart as a batch-level failure.

### GPU task with no GPU worker

Task stays QUEUED indefinitely. This is intentional: the dispatcher
**does not** silently fall back to `GCPVMRuntime`. Clients upstream of
cua-house (e.g. agenthle) are expected to route GPU workloads to a
GCP-only code path explicitly. Either add a GPU-capable worker to the
cluster (a GCE VM with nested virt + GPU passthrough is a separate
work item, not yet supported) or route the task outside the cluster
entirely.

## Smoke test

```bash
# 1. Submit
curl -sS -X POST http://<master>:8787/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"smoke","tasks":[{"task_id":"smoke-t1","task_path":"demo","snapshot_name":"cpu-free","vcpus":4,"memory_gb":8}]}'

# 2. Poll until ready. First same-shape task on a worker can cold-boot
# for several minutes; cache hits are much faster.
while [ "$(curl -sS http://<master>:8787/v1/tasks/smoke-t1 | jq -r .state)" != "ready" ]; do sleep 2; done

# 3. Get lease_endpoint + lease_id
TASK=$(curl -sS http://<master>:8787/v1/tasks/smoke-t1)
LEASE=$(echo "$TASK" | jq -r .lease_id)
EP=$(echo "$TASK" | jq -r .assignment.lease_endpoint)

# 4. Heartbeat
curl -sS -X POST $EP/v1/leases/$LEASE/heartbeat

# 5. Complete
curl -sS -X POST $EP/v1/leases/$LEASE/complete \
  -H 'Content-Type: application/json' -d '{"final_status":"completed"}'

# 8. Verify master sees completion
while [ "$(curl -sS http://<master>:8787/v1/tasks/smoke-t1 | jq -r .state)" != "completed" ]; do sleep 2; done
echo "smoke test passed"
```

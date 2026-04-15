# Multi-node cluster deployment

Standalone cua-house-server runs as a single process on one KVM host. In
cluster mode that single host is split into two roles:

- **Master** — a small control-plane VM. Accepts batch submissions, talks
  to workers over a WebSocket, and orchestrates the global image pool.
  Does NOT host VMs (except for the GPU overflow path, which is a Phase 6
  TODO).
- **Worker** — a nested-KVM host (e.g. `agenthle-nested-kvm-02`). Pulls
  qcow2 templates from GCS, boots VMs on demand from master pool ops, and
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
    │ ─ pool ops   │     │ ─ pool ops   │
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

### Shared task-data storage

Workers share read-only task-data via a GCE persistent disk + OverlayFS.
See `docs/deployment/host-setup.md` (section "Multi-node task-data
sharing") for the exact recipe. In short:

1. A single PD populated from `gs://agenthle/task-data/` attached
   `READ_ONLY` to every worker.
2. Each worker mounts the PD at `/mnt/agenthle-task-data-ro`, a local XFS
   upper layer at `/mnt/xfs/task-data-upper`, and an OverlayFS merged view
   at `/mnt/agenthle-task-data` — which is what `task_data_root` points to.
3. The merged view must be writable by the user running the worker process
   (e.g. `chown -R weichenzhang:weichenzhang /mnt/agenthle-task-data /mnt/xfs/task-data-upper /mnt/xfs/task-data-work`).

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
host_external_ip: <master internal IP, e.g. 10.128.0.16>
public_base_host: <same>
runtime_root: /home/weichen/cua-house-mnc/runtime
task_data_root: null            # master doesn't host task data
docker_image: trycua/cua-qemu-windows:latest
host_reserved_vcpus: 1
host_reserved_memory_gb: 1
batch_heartbeat_ttl_s: 3600
heartbeat_ttl_s: 3600
ready_timeout_s: 900
readiness_poll_interval_s: 5
idle_slot_ttl_s: 7200
published_port_range: [16000, 16999]
novnc_port_range: [18000, 18999]
vm_pool: []
snapshot_revert_timeout_s: 300
cua_ready_after_revert_timeout_s: 30
mode: master
cluster:
  master_bind_path: /v1/cluster/ws
  heartbeat_interval_s: 5
  heartbeat_ttl_s: 30
```

### Worker config (`kvm02-worker.yaml`)

```yaml
host_id: kvm-02-worker
host_external_ip: <worker internal IP, e.g. 10.128.0.14>
public_base_host: <same>
runtime_root: /mnt/xfs/runtime-cluster    # separate from standalone runtime_root
task_data_root: /mnt/agenthle-task-data   # the OverlayFS merged view
docker_image: trycua/cua-qemu-windows:latest
host_reserved_vcpus: 2
host_reserved_memory_gb: 8
batch_heartbeat_ttl_s: 7200
heartbeat_ttl_s: 7200
ready_timeout_s: 900
readiness_poll_interval_s: 5
idle_slot_ttl_s: 7200
published_port_range: [16000, 16999]
novnc_port_range: [18000, 18999]
vm_pool: []                                # EMPTY — master pushes dynamically
snapshot_revert_timeout_s: 300
cua_ready_after_revert_timeout_s: 30
mode: worker
vm_bind_address: 0.0.0.0                   # publish VM ports to all IFs
cluster:
  master_url: ws://<master IP>:8787/v1/cluster/ws
  worker_id: kvm02                         # unique identifier
  worker_public_host: 10.128.0.14          # advertised in TaskAssignment URLs
  worker_public_port: 8787
  heartbeat_interval_s: 5
  heartbeat_ttl_s: 30
```

### Images catalog

Shared between master and workers. Master uses it to validate submit
requests; workers use it to find local qcow2 template paths and GCS URIs
when master sends `PoolOp.ADD_IMAGE`.

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
      default_disk_gb: 64       # used when client/pool-spec omits disk_gb
```

## Startup sequence

Start master first, then any number of workers.

### Master

```bash
cd ~/cua-house-mnc
export CUA_HOUSE_CLUSTER_JOIN_TOKEN=...
setsid nohup uv run python -m cua_house_server.cli \
  --host-config master-server.yaml \
  --image-catalog images.yaml \
  --host 0.0.0.0 --port 8787 --mode master \
  </dev/null >master.log 2>&1 &
disown
curl -sS http://127.0.0.1:8787/healthz    # {"status":"ok","mode":"master"}
```

### Worker

**To provision a brand-new worker VM** (including boot disk snapshot,
instance create, mount setup, systemd enable, master registration
polling), run [`scripts/clone-worker.sh`](../../scripts/clone-worker.sh)
— see [clone-worker.md](clone-worker.md) for the end-to-end runbook.

**To start a worker manually** on an already-provisioned host that
has `/etc/cua-house/{worker,images}.yaml` and
`/etc/systemd/system/cua-house-worker.service` in place:

```bash
sudo systemctl start cua-house-worker
sudo journalctl -u cua-house-worker -f
```

The systemd unit is at
[`examples/systemd/cua-house-worker.service`](../../examples/systemd/cua-house-worker.service).
It `EnvironmentFile=`s `/etc/cua-house/worker.env` (mode 0600)
which holds the `CUA_HOUSE_CLUSTER_JOIN_TOKEN`.

**Legacy / ad-hoc dev path** (used by the currently-running kvm02 and
kvm03 pending their next restart) — do NOT use for new workers:

```bash
cd ~/cua-house-mnc
export CUA_HOUSE_CLUSTER_JOIN_TOKEN=...
setsid nohup uv run python -m cua_house_server.cli \
  --host-config kvm02-worker.yaml \
  --image-catalog images.yaml \
  --host 0.0.0.0 --port 8787 --mode worker \
  </dev/null >worker.log 2>&1 &
disown
```

In either path, verify the worker registered with master:

```bash
curl -sS http://<master>:8787/v1/cluster/workers | python3 -m json.tool
```

## Driving the pool

The reconciler only creates/destroys VMs to match the **desired state**
you set via `PUT /v1/cluster/pool`. There is no auto-scaling — operator
is in control.

```bash
curl -sS -X PUT http://<master>:8787/v1/cluster/pool \
  -H 'Content-Type: application/json' \
  -d '{"assignments":[
    {"worker_id":"kvm02","image_key":"cpu-free","count":2,"vcpus":4,"memory_gb":8,"disk_gb":64},
    {"worker_id":"kvm03","image_key":"cpu-free","count":1,"vcpus":4,"memory_gb":8,"disk_gb":64}
  ]}'
```

Within one reconciler tick (default 5s) master sends `ADD_IMAGE` +
`ADD_VM` pool ops over WS. If the worker already has a snapshot-cache
entry for this shape (image + version + vcpus + memory + disk), it
reflinks and boots via `-loadvm` in seconds. On a cache miss (first-ever
shape on this worker), it cold-boots from the base template (~4-5 min),
saves the snapshot to cache, and reports `state: ready`.

Desired state is **persisted to `runtime_root/cluster-pool-spec.json`**,
so master restarts preserve pool sizing.

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
6. Worker reverts the VM and notifies master asynchronously; master
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
sudo chown -R $(id -un):$(id -gn) /mnt/agenthle-task-data /mnt/xfs/task-data-upper /mnt/xfs/task-data-work
```

### 6. `CUA_HOUSE_CLUSTER_JOIN_TOKEN` mismatch

Silent failure mode: worker logs `Worker WS disconnected: ...` in an
infinite reconnect loop. Check both sides export the same value in the
environment where `uv run` was launched.

## Failure modes and recovery

### Worker crashes or disconnects

Master's `PoolReconciler` tick (every 5s) calls
`registry.reap_stale()` — any worker whose heartbeat is older than the
TTL is marked offline and `ClusterDispatcher.handle_worker_disconnect`
immediately fails every task with a lease on that worker
(`state=failed`, `error="worker <id> disconnected"`). A WebSocket close
takes effect instantly via the same hook wired from `master_ws`.

Workers reconnect automatically with exponential backoff. When a worker
rejoins with the same `worker_id`, the reconciler re-converges its pool
— ADD_IMAGE + ADD_VM as if from a blank slate.

### Master crashes

Workers keep running and continue serving any in-flight leases via
their HTTP API. New batches can't be submitted until master is back.
On restart, master re-reads `cluster-pool-spec.json` and waits for
workers to reconnect; the reconciler eventually re-matches desired
state. In-flight leases are **not** recovered by master — if a lease
existed before the crash and completes after, the client's
`TaskCompleted` message is dropped (master has no record of the task).

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
# 1. Desired state
curl -sS -X PUT http://<master>:8787/v1/cluster/pool \
  -H 'Content-Type: application/json' \
  -d '{"assignments":[{"worker_id":"kvm02","image_key":"cpu-free","count":1,"vcpus":4,"memory_gb":8}]}'

# 2. Wait ~60s for VM boot
while [ "$(curl -sS http://<master>:8787/v1/cluster/status | jq .vm_instances)" != "1" ]; do sleep 5; done

# 3. Submit
curl -sS -X POST http://<master>:8787/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"smoke","tasks":[{"task_id":"smoke-t1","task_path":"demo","snapshot_name":"cpu-free","vcpus":4,"memory_gb":8}]}'

# 4. Poll until ready
while [ "$(curl -sS http://<master>:8787/v1/tasks/smoke-t1 | jq -r .state)" != "ready" ]; do sleep 2; done

# 5. Get lease_endpoint + lease_id
TASK=$(curl -sS http://<master>:8787/v1/tasks/smoke-t1)
LEASE=$(echo "$TASK" | jq -r .lease_id)
EP=$(echo "$TASK" | jq -r .assignment.lease_endpoint)

# 6. Heartbeat
curl -sS -X POST $EP/v1/leases/$LEASE/heartbeat

# 7. Complete
curl -sS -X POST $EP/v1/leases/$LEASE/complete \
  -H 'Content-Type: application/json' -d '{"final_status":"completed"}'

# 8. Verify master sees completion
while [ "$(curl -sS http://<master>:8787/v1/tasks/smoke-t1 | jq -r .state)" != "completed" ]; do sleep 2; done
echo "smoke test passed"
```

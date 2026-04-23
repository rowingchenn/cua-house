# Cloning a new cua-house worker node

Runbook for adding a fresh worker to an existing multi-node cluster on
GCP by cloning the golden boot disk of an already-running worker. The
clone script provisions the GCE instance, mounts storage, renders config,
and validates worker-mode configuration. It does **not** start the worker
process; start it manually when you are ready to let it join the cluster.

For cluster architecture see [cluster.md](cluster.md). For single-host
setup (the building block cloned here) see [host-setup.md](host-setup.md).

## When to use

- You want to scale out the cluster (add kvm04, kvm05, ...).
- You need to replace a broken worker with an identical copy.
- You want a disposable worker for testing without touching kvm02/kvm03.

## Current model

Do not use old pool-era or standalone-era instructions for cloned
workers.

| Topic | Current reality |
|---|---|
| Process start | Manual `setsid nohup uv run ... --mode worker`; clone/bootstrap does not install or start a systemd unit. |
| Config path | `/etc/cua-house/worker.yaml` + `/etc/cua-house/worker.env` (mode 0600), written by `scripts/clone-worker.sh`. |
| Repo location | `/opt/cua-house`, group-writable by the shared `cua-house` group. Personal home directories are legacy migration sources only. |
| Example config file | `examples/worker.yaml` with `@@...@@` placeholders rendered by the clone script. |
| Image catalog | `examples/images.yaml` or `packages/server/src/cua_house_server/config/defaults/images.yaml`; master and workers both load it. Workers prewarm enabled local templates from GCS at startup. |
| Task-data disk | One GCE PD per worker, attached only to that worker as `device-name=task-data`. It is normally mounted read-only at `/mnt/agenthle-task-data-ro`; `/mnt/agenthle-task-data` is an OverlayFS view with writes on `/mnt/xfs`. |
| Pool | Removed. There is no `/v1/cluster/pool`, no desired pool spec, no `ADD_IMAGE`, and no `ADD_VM`. Master assigns tasks directly with `AssignTask`. |

## Prerequisites

Before running the clone script:

1. **Master is running** and reachable:
   `curl http://<master-dns-or-host>:8787/v1/cluster/status`.
2. **Cluster join token is shared.** The same
   `CUA_HOUSE_CLUSTER_JOIN_TOKEN` value must be used by master and
   workers. Pass it with `--join-token`.
3. **`gcloud` authenticated** to project `sunblaze-4`. Check with
   `gcloud auth list` and `gcloud config get-value project`.
4. **Local cua-house checkout is current.** The script reads
   `examples/worker.yaml`, `examples/images.yaml` (or the default
   catalog), and `scripts/_clone-worker-bootstrap.sh`.
5. **Image catalog is in sync with master.** Master uses the catalog to
   validate `snapshot_name`, resource defaults, and image version.
   Workers use it to prewarm local qcow2 templates and to provision
   `AssignTask` requests. Catalog drift can cause unknown images,
   cache-version mismatch, or worker startup prewarm failures.

If the operator invoking the script does not know the master WebSocket
URL or cluster join token, ask for those values before cloning. In a TTY,
`scripts/clone-worker.sh` prompts for missing `--master-url` and
`--join-token` values. In non-interactive automation, pass them
explicitly or set `CUA_HOUSE_MASTER_URL` and
`CUA_HOUSE_CLUSTER_JOIN_TOKEN`.

## Fast path

```bash
# From the repo root on your laptop (or wherever you run gcloud):
export CUA_HOUSE_CLUSTER_JOIN_TOKEN=<secret>   # same value master has

./scripts/clone-worker.sh \
    --new-id kvm04 \
    --source-instance agenthle-nested-kvm-02 \
    --master-url ws://cua-house-master.us-central1-a.c.sunblaze-4.internal:8787/v1/cluster/ws \
    --join-token "$CUA_HOUSE_CLUSTER_JOIN_TOKEN"
```

Interactive equivalent:

```bash
./scripts/clone-worker.sh \
    --new-id kvm04 \
    --source-instance agenthle-nested-kvm-02
# The script prompts for master URL and cluster join token.
```

Flags worth knowing:

- `--source-instance` takes a live boot-disk snapshot of the named
  worker. This is non-disruptive.
- `--source-boot-snapshot NAME` reuses an existing golden snapshot and
  skips the snapshot step.
- `--task-data-source-snapshot NAME` creates the new worker's task-data
  disk from a current snapshot, then `start-worker.sh` syncs only the
  GCS delta at startup. If omitted, the disk starts empty and the first
  startup performs a full bucket sync.
- `--task-data-size-gb` defaults to `400`. Keep this sized for task data
  only; VM image templates belong in `gs://agenthle-images/templates`,
  not the task-data bucket.
- `--dry-run` prints every `gcloud` and SSH command without executing.

The script phases are idempotent:

```text
preflight (gcloud auth, worker_id not taken, token non-empty)
  |
live snapshot of source instance (skip if --source-boot-snapshot)
  |
create boot disk from snapshot (pd-ssd, 500 GB)
  |
create fresh per-node XFS disk (pd-ssd, 512 GB)
  |
create GCE instance (tags=agenthle, nested virt, Cascade Lake,
                     boot+xfs+per-worker task-data disks attached)
  |
wait for SSH ready
  |
scp worker.yaml + images.yaml + worker.env + bootstrap.sh
  |
remote bootstrap (format XFS, fstab, mounts, chown, clear stale slots,
                  git pull && uv sync, install configs, validate with
                  --print-register-frame)
  |
print manual start and verification commands
```

## Manual start

After clone/bootstrap completes, SSH into the new worker and start it
manually:

```bash
gcloud compute ssh agenthle-nested-kvm-04 \
    --project=sunblaze-4 --zone=us-central1-a

cd /opt/cua-house
./scripts/start-worker.sh
```

`scripts/start-worker.sh` first remounts this node's own task-data disk
read-write, syncs `gs://agenthle` to `/mnt/agenthle-task-data-ro`, returns
the disk to read-only mode, restores the OverlayFS view, then starts the
worker process with `setsid nohup`. Worker startup also prewarms all enabled
local templates from GCS before it registers with master. If the data or
image directories are cold, this may take minutes and progress will be
in the terminal and `/var/log/cua-house/worker.log`.

## Validation

Expected checks after the clone script and manual start:

| # | Step | How to check |
|---|---|---|
| 1 | Instance is running | `gcloud compute instances describe agenthle-nested-kvm-04 --format='value(status)'` -> `RUNNING` |
| 2 | Mounts are up | `gcloud compute ssh agenthle-nested-kvm-04 -- findmnt /mnt/xfs /mnt/agenthle-task-data` |
| 3 | Worker process is running | `gcloud compute ssh agenthle-nested-kvm-04 -- pgrep -af cua_house_server.cli` |
| 4 | Worker HTTP healthz responds | `gcloud compute ssh agenthle-nested-kvm-04 -- curl -sS http://127.0.0.1:8787/healthz` |
| 5 | Worker registered with master | `curl -sS http://<master>:8787/v1/cluster/workers | jq '.[] | select(.worker_id=="kvm04")'` |
| 6 | Capacity and cache view look sane | Worker object has `online: true`, `free_vcpus`, `free_memory_gb`, `active_task_count`, and `cached_shapes` |

## Smoke test

The new model has no pool to prefill. Submit a single task and let
master assign it to a worker on demand:

```bash
BATCH_ID=$(curl -sS -X POST http://<master>:8787/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"tasks":[{"task_id":"kvm04-smoke","task_path":"p","snapshot_name":"cpu-free","vcpus":4,"memory_gb":8}]}' \
  | jq -r .batch_id)

while [ "$(curl -sS http://<master>:8787/v1/tasks/kvm04-smoke | jq -r .state)" != "ready" ]; do
  sleep 2
done

curl -sS http://<master>:8787/v1/tasks/kvm04-smoke | jq '.assignment'

LEASE=$(curl -sS http://<master>:8787/v1/tasks/kvm04-smoke | jq -r .lease_id)
curl -sS -X POST http://<master>:8787/v1/leases/$LEASE/complete \
  -H 'Content-Type: application/json' \
  -d '{"final_status":"completed"}'

while [ "$(curl -sS http://<master>:8787/v1/tasks/kvm04-smoke | jq -r .state)" != "completed" ]; do
  sleep 5
done
```

If you need to prove the new worker specifically takes the smoke task,
submit when it is the least-loaded matching worker, or temporarily stop
other workers before the test. Placement prefers exact cache hits first,
then least-loaded worker, then lexicographic `worker_id`.

## Manual path for debugging

If a phase fails mid-run, reproduce the exact commands printed by the
script, or step through this sequence. Substitute
`NEW=agenthle-nested-kvm-04`.

```bash
export PROJECT=sunblaze-4 ZONE=us-central1-a

gcloud compute disks snapshot agenthle-nested-kvm-02 \
    --project=$PROJECT --zone=$ZONE \
    --snapshot-names=agenthle-worker-boot-golden-$(date +%Y%m%d) \
    --storage-location=us-central1

gcloud compute disks create $NEW \
    --project=$PROJECT --zone=$ZONE \
    --source-snapshot=agenthle-worker-boot-golden-YYYYMMDD \
    --type=pd-ssd --size=500GB

gcloud compute disks create ${NEW}-xfs \
    --project=$PROJECT --zone=$ZONE \
    --type=pd-ssd --size=512GB

gcloud compute disks create ${NEW}-task-data \
    --project=$PROJECT --zone=$ZONE \
    --source-snapshot=<current-task-data-snapshot> \
    --type=pd-balanced --size=400GB

gcloud compute instances create $NEW \
    --project=$PROJECT --zone=$ZONE \
    --machine-type=n2-standard-16 \
    --network-interface=subnet=agenthle-vpc \
    --tags=agenthle \
    --enable-nested-virtualization \
    --min-cpu-platform="Intel Cascade Lake" \
    --disk=name=$NEW,boot=yes,auto-delete=yes,mode=rw \
    --disk=name=${NEW}-xfs,device-name=xfs,mode=rw,auto-delete=yes \
    --disk=name=${NEW}-task-data,device-name=task-data,mode=rw,auto-delete=yes \
    --metadata=cua-house-worker-id=${NEW#agenthle-nested-},cua-house-master-url=<ws url>

gcloud compute scp \
    /tmp/rendered-worker.yaml \
    /tmp/images.yaml \
    /tmp/worker.env \
    scripts/_clone-worker-bootstrap.sh \
    $NEW:/tmp/

gcloud compute ssh $NEW --project=$PROJECT --zone=$ZONE \
    --command='mv /tmp/rendered-worker.yaml /tmp/worker.yaml && bash /tmp/clone-worker-bootstrap.sh'
```

## Gotchas

1. **Stale `runtime_root/slots/*`** - live boot snapshots can carry
   source-node slot dirs. Bootstrap wipes `/mnt/xfs/runtime-cluster/slots`.
2. **Duplicate `worker_id`** - the script refuses IDs already visible in
   `/v1/cluster/workers`.
3. **Missing `agenthle` target tag** - without it, firewall rules for
   port 8787 and VM port ranges may not apply.
4. **Token mismatch** - worker logs show HTTP 401 reconnect loops.
   Compare the logged `sha256_prefix` with the master token.
5. **Port conflict with leftover manual process** - bootstrap runs
   `pkill -9 -f cua_house_server.cli` before validation.
6. **Task-data disk ownership** - do not attach one task-data disk to
   multiple workers. Each worker needs its own task-data disk, normally
   mounted read-only during VM runs.
7. **Unformatted XFS disk** - bootstrap handles `blkid || mkfs.xfs`.
8. **Nested virtualization missing** - without `/dev/kvm`, VM boots time
   out around `ready_timeout_s=900`.
9. **VPC mismatch** - workers and master must share `agenthle-vpc`.
10. **Legacy repo path** - use `/opt/cua-house`, not a personal
    home-directory checkout. Bootstrap may migrate a baked legacy
    checkout into `/opt/cua-house`, but new SOPs should not depend on
    user home paths.
11. **Image catalog drift** - workers prewarm from their own
    `/etc/cua-house/images.yaml`; keep it in sync with master.
12. **Startup appears slow** - template prewarm happens before worker
    registration. Tail `/var/log/cua-house/worker.log` for
    `template_pulled` and
    `prewarm completed`.

## Teardown

When you no longer need a worker:

```bash
# 1. Let in-flight tasks finish, or cancel owning batches:
#    POST /v1/batches/{id}/cancel

# 2. Stop the manually-started worker process.
gcloud compute ssh agenthle-nested-kvm-04 \
    --command='pkill -f cua_house_server.cli || true'

# 3. Delete the GCE instance. With the clone script's default
#    auto-delete settings this deletes the boot, XFS, and per-worker
#    task-data disks.
gcloud compute instances delete agenthle-nested-kvm-04 \
    --zone=us-central1-a \
    --delete-disks=boot,data

# 4. Optionally delete the golden boot snapshot:
#    gcloud compute snapshots delete agenthle-worker-boot-golden-YYYYMMDD
```

After the worker disconnects, master requeues or fails any remaining
leases through the normal worker-disconnect path.

## Fallback: from-scratch install

Use only if no golden snapshot is available. Follow
[host-setup.md](host-setup.md) to install Docker, qemu, libvirt, uv,
mount the xfs/task-data overlay, and clone the repo. Then install
`/etc/cua-house/worker.yaml`, `/etc/cua-house/images.yaml`, and
`/etc/cua-house/worker.env`, validate with `--print-register-frame`,
and start the worker manually.

## Future work

- Publish the golden image as a GCE custom machine image so
  `--source-boot-snapshot` becomes `--source-image`, versioned and
  released.
- Add a dedicated `cua-house` system user.

# Cloning a new cua-house worker node

Runbook for adding a fresh worker to an existing multi-node cluster on
GCP by cloning the golden boot disk of an already-running worker. End
goal: from `./scripts/clone-worker.sh --new-id ... --master-url ...
--join-token ...` to a smoke task completing on the new worker in
**≤10 minutes**, with **zero disruption** to the existing cluster.

For cluster architecture see [cluster.md](cluster.md). For
single-host setup (the building block cloned here) see
[host-setup.md](host-setup.md).

## When to use

- You want to scale out the cluster (add kvm04, kvm05, ...).
- You need to replace a broken worker with an identical copy.
- You want a disposable worker for testing without touching
  kvm02/kvm03.

## Reality vs. older docs

A previous operator walking through the standalone-era docs hit six
gaps. Do not trust those sources in the multi-node world:

| Topic                | What old docs say                        | Current reality                                                    |
|----------------------|------------------------------------------|--------------------------------------------------------------------|
| Process supervision  | "systemd unit `cua-house-server.service`" | Historical — was never installed. New workers **must** use `examples/systemd/cua-house-worker.service` via this runbook. kvm02/kvm03 are still on `nohup` pending their next restart. |
| Config path          | `/etc/cua-house/server.yaml`             | `/etc/cua-house/worker.yaml` + `worker.env` (mode 0600). Written by `scripts/clone-worker.sh`. |
| Repo location        | `/opt/cua-house` / `~/cua-house`         | `/home/weichenzhang/cua-house-mnc` (baked into the boot disk). `~/cua-house` still exists but is the legacy standalone checkout. |
| Example config file  | `examples/kvm02-server.yaml` (untracked) | `examples/worker.yaml` with `@@...@@` placeholders the clone script substitutes. |
| Task-data disk       | "each host mounts its own"               | `agenthle-nested-kvm-01-task-data` (300 GB pd-balanced) attached **read-only to every worker**. Cloning script hard-codes this; do not create a new one per worker. |
| Boot disk contents   | "install Docker, qemu, uv, …"            | All baked in on the kvm02 boot disk: Docker 28.2.2, qemu 6.2, libvirt 8.0, Python 3.10, uv, cua-house-mnc repo, the three fstab lines for xfs + task-data. Cloning a snapshot skips the whole install. |

## Prerequisites

Before running the clone script:

1. **Master is running** and reachable. Verify with
   `curl http://<master-ip>:8787/v1/cluster/status`.
2. **Cluster join token is shared.** The same
   `CUA_HOUSE_CLUSTER_JOIN_TOKEN` env var was exported on master when
   it started. You'll pass the same value via `--join-token`.
3. **`gcloud` authenticated** to project `sunblaze-4`. Run
   `gcloud auth list` and `gcloud config get-value project`.
4. **Working copy of cua-house-mnc checked out locally** — the script
   reads `examples/worker.yaml`, `examples/systemd/cua-house-worker.service`,
   `scripts/_clone-worker-bootstrap.sh` from the repo root.
5. **Image catalog in sync.** The catalog (`examples/images.yaml` or
   `packages/server/src/cua_house_server/config/defaults/images.yaml`)
   must declare every image you want master to place on the new
   worker. Master never pushes catalog metadata — the worker
   discovers images from its own file.

## Fast path (automated)

```bash
# From the repo root on your laptop (or wherever you run gcloud):
export CUA_HOUSE_CLUSTER_JOIN_TOKEN=<secret>   # same value master has

./scripts/clone-worker.sh \
    --new-id kvm04 \
    --source-instance agenthle-nested-kvm-02 \
    --master-url ws://10.128.0.16:8787/v1/cluster/ws \
    --join-token "$CUA_HOUSE_CLUSTER_JOIN_TOKEN" \
    --update-pool
```

Flags worth knowing:

- `--source-instance` — takes a **live** boot-disk snapshot of the
  named worker (non-disruptive). Use this for the first clone or when
  you want the latest state.
- `--source-boot-snapshot NAME` — skip the snapshot step and reuse an
  existing golden snapshot (e.g. after running the script once).
  Faster for subsequent clones.
- `--update-pool` — after the new worker registers, append a default
  `(cpu-free, count=1, 4 vCPU / 8 GB)` assignment to master's pool
  spec so the reconciler immediately puts a VM on it. Omit if you
  want to stage the pool update manually.
- `--dry-run` — print every `gcloud` and `ssh` command without
  executing. Use to review before the first real run.

The script's phases (all idempotent; failure at any phase doesn't
corrupt earlier state):

```
preflight (gcloud auth, worker_id not taken, token non-empty)
  ↓
live snapshot of source instance (skip if --source-boot-snapshot)
  ↓
create boot disk from snapshot (pd-ssd, 500 GB)
  ↓
create fresh per-node XFS disk (pd-ssd, 512 GB)
  ↓
create GCE instance (tags=agenthle, nested virt, Cascade Lake,
                     boot+xfs+task-data disks attached)
  ↓
wait for SSH ready
  ↓
scp worker.yaml + images.yaml + worker.env + .service + bootstrap.sh
  ↓
remote bootstrap (format XFS, fstab, mounts, chown, clear stale slots,
                  git pull && uv sync, install configs, validate with
                  --print-register-frame, systemctl enable --now)
  ↓
poll master /v1/cluster/workers until new worker online
  ↓
(optional) append pool assignment via idempotent GET → PUT
```

## Manual path (for debugging)

If a phase fails mid-run, reproduce the exact commands the script ran
from its stdout, or step through the sequence below. Substitute
`NEW=agenthle-nested-kvm-04` and your own values.

```bash
export PROJECT=sunblaze-4 ZONE=us-central1-a

# 1. Golden snapshot (one-time or whenever the golden needs refreshing)
gcloud compute disks snapshot agenthle-nested-kvm-02 \
    --project=$PROJECT --zone=$ZONE \
    --snapshot-names=agenthle-worker-boot-golden-$(date +%Y%m%d) \
    --storage-location=us-central1

# 2. Clone boot disk
gcloud compute disks create $NEW \
    --project=$PROJECT --zone=$ZONE \
    --source-snapshot=agenthle-worker-boot-golden-YYYYMMDD \
    --type=pd-ssd --size=500GB

# 3. Fresh XFS disk
gcloud compute disks create ${NEW}-xfs \
    --project=$PROJECT --zone=$ZONE \
    --type=pd-ssd --size=512GB

# 4. Create instance (the --enable-nested-virtualization and
#    --min-cpu-platform flags are mandatory; missing either makes
#    /dev/kvm unavailable and every VM boot times out at 900s)
gcloud compute instances create $NEW \
    --project=$PROJECT --zone=$ZONE \
    --machine-type=n2-standard-16 \
    --network-interface=subnet=agenthle-vpc \
    --tags=agenthle \
    --enable-nested-virtualization \
    --min-cpu-platform="Intel Cascade Lake" \
    --disk=name=$NEW,boot=yes,auto-delete=yes,mode=rw \
    --disk=name=${NEW}-xfs,device-name=xfs,mode=rw,auto-delete=yes \
    --disk=name=agenthle-nested-kvm-01-task-data,device-name=task-data,mode=ro \
    --metadata=cua-house-worker-id=${NEW#agenthle-nested-},cua-house-master-url=<ws url>

# 5. Remote bootstrap (after SSH is up)
gcloud compute scp \
    examples/worker.yaml \
    examples/images.yaml \
    examples/systemd/cua-house-worker.service \
    scripts/_clone-worker-bootstrap.sh \
    $NEW:/tmp/
# NOTE: you still need /tmp/worker.env containing the join token; the
# automated script generates it in a temp dir. For manual runs,
# write it on-host with mode 0600 before invoking the bootstrap.
gcloud compute ssh $NEW --project=$PROJECT --zone=$ZONE \
    --command='bash /tmp/clone-worker-bootstrap.sh'

# 6. Verify registration
curl -sS http://<master>:8787/v1/cluster/workers | jq '.[] | select(.worker_id=="kvm04")'
```

## Post-boot validation

Expected timing from `./scripts/clone-worker.sh` invocation:

| # | Step                                             | How to check                                                                                     | Target      |
|---|--------------------------------------------------|--------------------------------------------------------------------------------------------------|-------------|
| 1 | Boot disk + xfs disk + instance created          | `gcloud compute instances describe agenthle-nested-kvm-04 --format='value(status)'` → `RUNNING`  | 60–90s      |
| 2 | SSH ready                                         | `gcloud compute ssh agenthle-nested-kvm-04 -- true`                                               | +30s        |
| 3 | Mounts up                                         | `gcloud compute ssh agenthle-nested-kvm-04 -- findmnt /mnt/xfs /mnt/agenthle-task-data`           | +10s        |
| 4 | `cua-house-worker` systemd unit active            | `gcloud compute ssh agenthle-nested-kvm-04 -- systemctl is-active cua-house-worker` → `active`    | +20s        |
| 5 | Worker healthz responds                           | `gcloud compute ssh agenthle-nested-kvm-04 -- curl -sS http://127.0.0.1:8787/healthz`             | +5s         |
| 6 | Registered with master                            | `curl -sS http://<master>:8787/v1/cluster/workers \| jq '.[] \| select(.worker_id=="kvm04")'`  → object with `online: true`, `hosted_images: []` | +10s |
| 7 | Pool spec appended (if `--update-pool`)           | `curl -sS http://<master>:8787/v1/cluster/pool` shows the new assignment                         | +1s         |
| 8 | Reconciler runs `ADD_IMAGE` → template pulled     | `gcloud compute ssh agenthle-nested-kvm-04 -- sudo journalctl -u cua-house-worker -n 50 -f`       | +60–180s    |
| 9 | First VM on new worker reaches `ready`            | `curl http://<master>:8787/v1/cluster/workers` shows `vm_summaries: [{state: "ready"}]`          | +60–90s     |
|10 | Smoke batch completes                             | See "Smoke test" below                                                                            | +60s        |

Worst-case total: ~9 min. The `ADD_IMAGE` pull from GCS is the long
pole; the cpu-free template is ~50 GB.

### Smoke test

Quickest way to prove the new worker is taking traffic:

```bash
# Submit a single-task batch
BATCH_ID=$(curl -sS -X POST http://<master>:8787/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"tasks":[{"task_id":"kvm04-smoke","task_path":"p","snapshot_name":"cpu-free","vcpus":4,"memory_gb":8}]}' \
  | jq -r .batch_id)

# Wait for READY
while [ "$(curl -sS http://<master>:8787/v1/tasks/kvm04-smoke | jq -r .state)" != "ready" ]; do sleep 2; done

# Inspect the assignment — expect host_id=kvm04 if it's the least-loaded
curl -sS http://<master>:8787/v1/tasks/kvm04-smoke | jq '.assignment'

# Complete via master lease proxy (agenthle path)
LEASE=$(curl -sS http://<master>:8787/v1/tasks/kvm04-smoke | jq -r .lease_id)
curl -sS -X POST http://<master>:8787/v1/leases/$LEASE/complete \
  -H 'Content-Type: application/json' \
  -d '{"final_status":"completed"}'

# Wait for COMPLETED (after revert)
while [ "$(curl -sS http://<master>:8787/v1/tasks/kvm04-smoke | jq -r .state)" != "completed" ]; do sleep 5; done
```

## Gotchas (ranked by likelihood)

1. **Stale `runtime_root/slots/*`** — the boot snapshot was taken on
   a live source host with in-flight slot dirs. The bootstrap script
   wipes `/mnt/xfs/runtime-cluster/slots` to prevent the new worker
   from re-using source-node vm_ids. Don't skip this on a manual run.
2. **Duplicate `worker_id` in master registry** — if you reuse an
   ID, master's `WorkerRegistry.register` replaces the old session,
   which silently drops any leases the old owner still holds. The
   script's preflight hits `/v1/cluster/workers` to refuse this.
3. **Missing `agenthle` target tag** — boot snapshots don't carry
   GCE network tags. Without the tag, the firewall rule
   `agenthle-allow-env-server` doesn't apply and master can't reach
   port 8787 on the new worker. Script always sets it.
4. **Token mismatch** — `worker.env` ends up with a different
   `CUA_HOUSE_CLUSTER_JOIN_TOKEN` than master has. Symptom is
   infinite WS reconnect loop with HTTP 401 in
   `journalctl -u cua-house-worker`. Fix: the worker logs a
   `sha256_prefix=...` fingerprint on startup; compare it with
   `printf '%s' "$CUA_HOUSE_CLUSTER_JOIN_TOKEN" | shasum -a 256`
   on the master host.
5. **Port conflict with a leftover `nohup` process** — the source
   boot disk may have had a manually-launched `cua_house_server.cli`
   process running when the snapshot was taken. The bootstrap
   `pkill -9 -f cua_house_server.cli` catches this before systemd
   starts its own copy on port 8787.
6. **Task-data disk mode mismatch** — attaching
   `agenthle-nested-kvm-01-task-data` with `mode=rw` fails outright
   because it's multi-attached RO elsewhere. Script hard-codes
   `mode=ro`.
7. **Unformatted XFS disk** — freshly-created disks have no
   filesystem; fstab mount by label fails silently on first boot
   and then the worker's `task_data_root` writable check fires
   instead of the real error. Bootstrap `blkid || mkfs.xfs` handles
   this.
8. **`--enable-nested-virtualization` forgotten** — an n2 instance
   without nested virt has no `/dev/kvm`, QEMU falls back to TCG,
   and every VM boot times out at `ready_timeout_s=900`. Script
   enforces the flag.
9. **VPC mismatch** — instance lands on `default` VPC instead of
   `agenthle-vpc`. Workers + master then can't reach each other
   over 10.128.x.x. Script hard-codes `--network-interface=subnet=agenthle-vpc`.
10. **Legacy repo path `~/cua-house`** — the boot snapshot has both
    `~/cua-house` (standalone-era) and `~/cua-house-mnc` (cluster).
    The systemd unit's `WorkingDirectory` is explicit about
    `cua-house-mnc`; don't be tempted to "simplify" by pointing at
    `~/cua-house`.
11. **Pool spec drift via blanket PUT** — never run
    `PUT /v1/cluster/pool` with only the new worker's assignment:
    master will interpret it as "delete all other workers' VMs."
    Always use `--update-pool` (idempotent append) or the manual
    `GET → jq → PUT` recipe in the script's summary output.
12. **Image catalog drift** — if you clone from kvm02 whose
    `images.yaml` is older than master's, master's `ADD_IMAGE` for
    a newer image will fail specifically on that worker. Bootstrap
    does `git pull && uv sync` inside `cua-house-mnc` to catch up.
13. **Mount ordering** — without `RequiresMountsFor` in the systemd
    unit, the worker can start before fstab has finished mounting
    `/mnt/xfs`, tripping the `task_data_root` writability fail-fast.
    Already fixed in `examples/systemd/cua-house-worker.service`.

## Teardown

When you no longer need a worker:

```bash
# 1. Remove it from master's desired pool so reconciler stops
#    sending PoolOps. Master will NOT automatically destroy the
#    VMs that exist — do that after step 2.
curl -sS http://<master>:8787/v1/cluster/pool > /tmp/pool.json
python3 -c "
import json
d = json.load(open('/tmp/pool.json'))
d['assignments'] = [a for a in d['assignments'] if a['worker_id'] != 'kvm04']
print(json.dumps(d))" > /tmp/pool-new.json
curl -sS -X PUT http://<master>:8787/v1/cluster/pool \
    -H 'Content-Type: application/json' -d @/tmp/pool-new.json

# 2. Stop the worker (systemd) — revert any active leases first via
#    master's batch cancel endpoints, or wait for them to finish.
gcloud compute ssh agenthle-nested-kvm-04 \
    --command='sudo systemctl stop cua-house-worker'

# 3. Delete the GCE instance. --delete-disks=boot,xfs keeps the
#    shared task-data disk intact (it's multi-attached).
gcloud compute instances delete agenthle-nested-kvm-04 \
    --zone=us-central1-a \
    --delete-disks=boot,data

# 4. (Optional) Keep the golden boot snapshot for future clones,
#    or delete it:
#    gcloud compute snapshots delete agenthle-worker-boot-golden-YYYYMMDD
```

After `gcloud compute instances delete`, master will eventually reap
the worker via heartbeat TTL and mark any remaining leases as
`failed` with `error: "worker kvm04 disconnected"` — same code path
that handles a crash.

## Fallback: from-scratch install

Use only if no golden snapshot is available. Follow
[host-setup.md](host-setup.md) to install Docker, qemu, libvirt, uv,
mount the xfs/task-data overlay, and clone the repo. Then run the
bootstrap block in `scripts/_clone-worker-bootstrap.sh` to render
configs, validate, and enable the systemd unit. Budget ~1 hour
instead of ~10 minutes.

## Future work

- **Publish the golden image as a GCE custom machine image** so
  `--source-boot-snapshot` becomes `--source-image`, versioned and
  released. Currently the snapshot is a one-off operator artifact.
- **Migrate kvm02/kvm03 to systemd** on their next restart using the
  same `examples/systemd/cua-house-worker.service` unit.
- **Dedicated `cua-house` system user** (see TODO in the unit file).

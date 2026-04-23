# Image update SOP

Procedure for updating VM images (cpu-free, cpu-license, cpu-free-ubuntu, waa) in a cua-house cluster. GCS is the source of truth for base templates. Templates are clean qcow2 files without pre-baked savevm tags -- the server creates shape-based snapshot tags (e.g., `4vcpu-8gb-64gb`) automatically on first cache miss.

## When to use

- Updated guest software (CUA server, agents, drivers)
- Fixed a guest-side bug
- New image variant (e.g., adding a new `cpu-*` image)

## Prerequisites

- `gcloud` authenticated to project `sunblaze-4`
- `gsutil` access to `gs://agenthle-images/templates/`
- SSH access to at least one KVM host (agenthle-nested-kvm-02, kvm-03, etc.)
- Master cluster running and healthy (`GET /v1/cluster/workers` returns 200)
- KVM image storage on a reflink-capable filesystem (current workers use XFS at `/mnt/xfs`). Cold-boot tests must use the same reflink slot layout as the worker runtime, not a full `cp` into `/tmp`.

## Step 1: Make changes on GCP dev VM

SSH into the dev VM and make your changes. Shut down Windows cleanly when done (Start > Shut down, or `shutdown /s /t 0`).

```bash
IMAGE_KEY=cpu-free  # or cpu-license, cpu-free-ubuntu, waa
gcloud compute ssh agenthle-dev-${IMAGE_KEY} \
    --zone=us-west1-a --project=sunblaze-4
```

Some legacy dev VMs do not follow `agenthle-dev-${IMAGE_KEY}` exactly. Confirm the VM, zone, and boot disk before exporting:

```bash
gcloud compute instances list --project=sunblaze-4 \
  --filter='name~agenthle-dev|name~agenthle-ubuntu|name~waa' \
  --format='table(name,zone.basename(),status,disks[0].source.basename())'
```

## Step 1b: Pre-export cleanup on the dev VM

Before shutting down and exporting, clean up GCP-specific state that causes
issues when the image runs outside GCP (on KVM hosts). Run these on the dev VM
**before** the clean shutdown.

**Linux (cpu-free-ubuntu):**

```bash
# gsutil lock/cache files — stale copies cause "OSError: File exists" on KVM
rm -rf /tmp/.gsutil_* /home/user/.gsutil

# gcloud cached credentials — forces fresh token fetch from metadata on next boot
rm -f /home/user/.config/gcloud/access_tokens.db
rm -f /home/user/.config/gcloud/credentials.db

# systemd journal vacuum — shrink logs baked into the image
sudo journalctl --vacuum-size=50M

# apt cache
sudo apt-get clean
```

**Windows (cpu-free, cpu-license):**

```powershell
# gcloud cached credentials
Remove-Item -Force "$env:APPDATA\gcloud\access_tokens.db" -ErrorAction SilentlyContinue
Remove-Item -Force "$env:APPDATA\gcloud\credentials.db" -ErrorAction SilentlyContinue

# Temp files
Remove-Item -Recurse -Force "$env:TEMP\*" -ErrorAction SilentlyContinue
```

Then shut down cleanly (Linux: `sudo shutdown -h now`, Windows: `shutdown /s /t 0`).

## Step 2: Export boot disks directly to the template bucket

```bash
DATE=$(date +%Y%m%d)
IMAGE_KEY=cpu-free

./scripts/export-gcp-to-qcow2.sh --image-key ${IMAGE_KEY} --date ${DATE}
```

This creates `gs://agenthle-images/templates/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2` via Cloud Build. For a batch update, run independent image exports in parallel. Each export creates a GCP snapshot, a temporary GCE image, and a qcow2 template object.

Never export qcow2 images to `gs://agenthle`. That bucket is the worker
task-data source; every worker mirrors it into its own 400G task-data disk
during manual startup.

If the dev VM name or zone does not match the script defaults, run the same steps manually:

```bash
PROJECT=sunblaze-4
DATE=$(date +%Y%m%d)
IMAGE_KEY=cpu-free-ubuntu
ZONE=us-west2-a
DISK=agenthle-ubuntu

SNAPSHOT_NAME=agenthle-dev-${IMAGE_KEY}-export-${DATE}
IMAGE_NAME=agenthle-dev-${IMAGE_KEY}-export-${DATE}
GCS_URI=gs://agenthle-images/templates/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2

gcloud compute disks snapshot "${DISK}" \
  --project="${PROJECT}" --zone="${ZONE}" \
  --snapshot-names="${SNAPSHOT_NAME}" \
  --storage-location="${ZONE%-*}"

gcloud compute images create "${IMAGE_NAME}" \
  --project="${PROJECT}" \
  --source-snapshot="${SNAPSHOT_NAME}"

gcloud compute images export \
  --image="${IMAGE_NAME}" \
  --project="${PROJECT}" \
  --export-format=qcow2 \
  --destination-uri="${GCS_URI}"
```

Monitor progress:

```bash
gcloud builds list --project=sunblaze-4 --region=us-central1 --limit=10
gcloud builds describe <build-id> --project=sunblaze-4 --region=us-central1 \
  --format='value(status,finishTime)'
```

After each export succeeds, verify that the template object exists and record its size:

```bash
gsutil stat gs://agenthle-images/templates/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2 | \
  awk '/Creation time|Content-Length/ {print}'
```

## Step 3: Transfer to KVM host and cold-boot test with worker-equivalent layout

Copy the exported qcow2 to a KVM host and test it using the same local-disk behavior as the worker runtime. The worker does **not** fully copy templates into `/tmp`; it runs `cp --reflink=auto` from the template or snapshot cache into a per-VM slot. Use the same pattern for tests.

```bash
IMAGE_KEY=cpu-free
DATE=$(date +%Y%m%d)
KVM=agenthle-nested-kvm-02

gcloud compute ssh "${KVM}" --zone=us-central1-a --project=sunblaze-4 -- '
  set -euo pipefail
  IMAGE_KEY='"${IMAGE_KEY}"'
  DATE='"${DATE}"'
  BASE=/mnt/xfs/images/${IMAGE_KEY}
  TEMPLATE=${BASE}/${IMAGE_KEY}-${DATE}.qcow2
  GCS_TEMPLATE=gs://agenthle-images/templates/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2
  TEST_ROOT=/mnt/xfs/image-update-tests/${IMAGE_KEY}-${DATE}
  SLOT=${TEST_ROOT}/storage

  mkdir -p "${BASE}" "${SLOT}" "${TEST_ROOT}/logs"
  gsutil cp "${GCS_TEMPLATE}" "${TEMPLATE}"

  # Match DockerQemuRuntime._prepare_vm(): reflink on XFS, full copy only
  # if the filesystem cannot reflink. Keep test files on /mnt/xfs.
  rm -f "${SLOT}/data.qcow2"
  cp --reflink=auto "${TEMPLATE}" "${SLOT}/data.qcow2"

  docker rm -f cua-house-cold-boot-test >/dev/null 2>&1 || true
  docker run -d \
    --name cua-house-cold-boot-test \
    --device=/dev/kvm \
    --cap-add NET_ADMIN \
    -e RAM_SIZE=8G \
    -e CPU_CORES=4 \
    -e CPU_MODEL=host \
    -e HV=N \
    -e VM_NET_IP=172.30.0.2 \
    -p 127.0.0.1:15900:5000 \
    -v "${SLOT}:/storage" \
    trycua/cua-qemu-windows:latest

  deadline=$((SECONDS + 1200))
  until curl -fsS http://127.0.0.1:15900/status >/dev/null; do
    if (( SECONDS > deadline )); then
      docker logs --tail 80 cua-house-cold-boot-test || true
      docker rm -f cua-house-cold-boot-test >/dev/null 2>&1 || true
      exit 1
    fi
    sleep 5
  done

  docker rm -f cua-house-cold-boot-test >/dev/null 2>&1 || true
  rm -rf "${TEST_ROOT}"
'
```

The test cold-boots the qcow2 without any snapshot tag and verifies the CUA server responds on `/status`. Expect ~3-5 minutes for a healthy cold boot, but allow up to 20 minutes for large Windows images. Do not publish an image that boots Windows but never serves `/status`; debug guest-side startup first.

**NOTE:** No `savevm` bake step is needed in the template. The server creates shape-based snapshot tags (e.g., `4vcpu-8gb-64gb`) automatically on first cache miss. First boot after deployment takes ~4-5 minutes; subsequent boots from cache take ~30 seconds.

### Optional: prewarm the worker snapshot cache

After the reflink cold-boot test passes, you can prewarm cache on one or more workers by submitting a smoke task for the new `version` after `images.yaml` is updated. This is the production-equivalent cache path:

1. Worker pulls the template from `gcs_uri`.
2. Worker reflinks the template into a VM slot.
3. VM cold-boots and serves the task.
4. Worker QMP `savevm`s and reflinks the slot qcow2 into `/mnt/xfs/snapshot-cache/<image>/v<version>/`.

Do not manually bake `savevm` into the base template.

## Step 4: Verify GCS source of truth

```bash
IMAGE_KEY=cpu-free
DATE=$(date +%Y%m%d)
TEMPLATE=gs://agenthle-images/templates/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2

# Verify the exported template object.
gsutil stat "${TEMPLATE}" | awk '/gs:|Creation time|Content-Length/ {print}'
```

Exporting to `gs://agenthle-images/templates/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2` is **required** -- workers prewarm templates from GCS at startup (`prewarm_templates()`). Skipping this means new workers get a stale image.

Only advance `images.yaml` to images that passed the KVM cold-boot
`/status` test. Keep failed template objects for debugging or delete them
after root cause is understood.

## Step 5: Sync task data (if changed)

If you added or modified task data (input files, software, reference
data), upload it to the canonical AgentHLE GCS prefix first. The source
of truth is `gs://agenthle/<domain>/<task>/<variant>/`; the bucket path
must match `TaskDataRequest.source_relpath` exactly.

**Single-node (standalone):**

```bash
gsutil -m rsync -r gs://agenthle/<domain>/<task>/<variant>/ \
  /mnt/agenthle-task-data/<domain>/<task>/<variant>/
```

**Multi-node (cluster):** Each worker owns its own task-data disk. Do not
detach one disk from multiple workers for data updates. The normal manual start command
syncs `gs://agenthle` into that worker's lower data disk, remounts it
read-only, restores the OverlayFS view, and starts the worker:

```bash
cd /opt/cua-house
./scripts/start-worker.sh
```

If you need to refresh a worker's disk without starting the worker, run
the sync portion of `scripts/start-worker.sh` manually: unmount the
OverlayFS view, mount `/dev/disk/by-id/google-task-data` read-write at
`/mnt/agenthle-task-data-ro`, run `gcloud storage rsync --recursive
--exclude='(^|/)vm-images/|.*\.gstmp$' gs://agenthle
/mnt/agenthle-task-data-ro`, unmount it, and remount it `ro,noload`
before restoring the overlay.

## Step 6: Update images.yaml

Bump the `version` field and update paths. The version change invalidates all worker snapshot caches, forcing a fresh cold boot with the new template.

Example diff for `cpu-free`:

```yaml
  cpu-free:
    enabled: true
    os_family: windows
    published_ports: [5000]
    local:
-     template_qcow2_path: /mnt/xfs/images/cpu-free/cpu-free-20260413.qcow2
-     gcs_uri: gs://agenthle-images/templates/cpu-free/cpu-free-20260413.qcow2
-     version: "20260413"
+     template_qcow2_path: /mnt/xfs/images/cpu-free/cpu-free-20260415.qcow2
+     gcs_uri: gs://agenthle-images/templates/cpu-free/cpu-free-20260415.qcow2
+     version: "20260415"
      default_vcpus: 4
      default_memory_gb: 8
```

Distribute to workers:

```bash
# Option A: Update existing workers
git commit -am "images: bump cpu-free to 20260415"
git push

# On each KVM worker:
cd /opt/cua-house && git pull
pkill -f cua_house_server.cli || true
./scripts/start-worker.sh
```

```bash
# Option B: Provision new workers (handles instance, mounts, config, validation)
scripts/clone-worker.sh --new-id kvm04 --source-instance agenthle-nested-kvm-02 \
  --master-url ws://<master-dns>:8787/v1/cluster/ws \
  --join-token "$CUA_HOUSE_CLUSTER_JOIN_TOKEN"
```

## Step 7: Verify deployment

Checklist:

1. Worker pulled new template from GCS (check worker logs for `pull_template`)
2. First VM cold-booted and shape tag created (check for `cache miss` in logs)
3. Subsequent VMs used cache (check for `from_cache=True` in events)
4. Smoke batch completes end-to-end

```bash
MASTER=http://<master-dns-or-host>:8787

# Check worker status, live capacity, and cached shapes
curl -sS ${MASTER}/v1/cluster/workers | python3 -m json.tool

# Submit a smoke task. It will create a fresh VM on demand
# (~30s cache hit, ~4-5min cache miss).
curl -sS -X POST ${MASTER}/v1/batches \
  -H 'Content-Type: application/json' \
  -d '{"tasks":[{
    "task_id":"smoke-img-update",
    "task_path":"demo/demo_desktop_note",
    "snapshot_name":"cpu-free",
    "vcpus":4,
    "memory_gb":8
  }]}'

# Poll until ready
while [ "$(curl -sS ${MASTER}/v1/tasks/smoke-img-update | jq -r .state)" != "ready" ]; do sleep 2; done

# Get lease info and complete
LEASE=$(curl -sS ${MASTER}/v1/tasks/smoke-img-update | jq -r .lease_id)
EP=$(curl -sS ${MASTER}/v1/tasks/smoke-img-update | jq -r .assignment.lease_endpoint)
curl -sS -X POST ${EP}/v1/leases/${LEASE}/complete \
  -H 'Content-Type: application/json' -d '{"final_status":"completed"}'

# Verify completed
while [ "$(curl -sS ${MASTER}/v1/tasks/smoke-img-update | jq -r .state)" != "completed" ]; do sleep 5; done
echo "smoke test passed"
```

## Rollback

- Revert `version` in `images.yaml` to the previous value. Workers still have old cache entries on disk and will use them immediately (~30s boot).
- If old cache was purged: workers cold-boot from the old GCS template (still available in the bucket).
- Keep old GCS templates until the new image is confirmed working in production.

## GPU images (GCP-only)

GPU images (`gpu-free`, `gpu-license`) run entirely on GCP -- no qcow2 or KVM involved. Assets are a GCP boot image and a GCP data snapshot.

To update a GPU image:

```bash
IMAGE_KEY=gpu-free
DATE=$(date +%Y%m%d)
ZONE=us-central1-a

# Stop the dev VM, then create a new boot image from its disk
gcloud compute instances stop agenthle-dev-${IMAGE_KEY} \
    --zone=${ZONE} --project=sunblaze-4

gcloud compute images create agenthle-dev-${IMAGE_KEY}-agents-baked-${DATE} \
    --source-disk=agenthle-dev-${IMAGE_KEY} \
    --source-disk-zone=${ZONE} \
    --project=sunblaze-4

# Create a new data snapshot (if data disk changed)
gcloud compute disks snapshot agenthle-dev-${IMAGE_KEY}-data \
    --snapshot-names=agenthle-dev-${IMAGE_KEY}-data-${DATE} \
    --zone=${ZONE} \
    --project=sunblaze-4
```

Then update the `gcp:` section in `images.yaml`:

```yaml
  gpu-free:
    gcp:
      boot_snapshot: agenthle-dev-gpu-free-agents-baked-20260415
      data_snapshot: agenthle-dev-gpu-free-data-20260415
```

## Reference

- Image internals: `docs/operations/vm-image-maintenance.md`
- Cluster architecture: `docs/deployment/cluster.md`
- Worker provisioning: `docs/deployment/clone-worker.md`
- Known pitfalls: `docs/operations/pitfalls.md`

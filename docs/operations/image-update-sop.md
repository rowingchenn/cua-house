# Image update SOP

Procedure for updating VM images (cpu-free, cpu-free-ubuntu, waa) in a cua-house cluster. GCS is the source of truth for base templates. Templates are clean qcow2 files without pre-baked savevm tags -- the server creates shape-based snapshot tags (e.g., `4vcpu-8gb-64gb`) automatically on first cache miss.

## When to use

- Updated guest software (CUA server, agents, drivers)
- Fixed a guest-side bug
- New image variant (e.g., adding a new `cpu-*` image)

## Prerequisites

- `gcloud` authenticated to project `sunblaze-4`
- `gsutil` access to `gs://agenthle-images/` and `gs://agenthle/vm-images/`
- SSH access to at least one KVM host (agenthle-nested-kvm-02, kvm-03, etc.)
- Master cluster running and healthy (`GET /v1/cluster/workers` returns 200)

## Step 1: Make changes on GCP dev VM

SSH into the dev VM and make your changes. Shut down Windows cleanly when done (Start > Shut down, or `shutdown /s /t 0`).

```bash
IMAGE_KEY=cpu-free  # or cpu-free-ubuntu, waa
gcloud compute ssh agenthle-dev-${IMAGE_KEY} \
    --zone=us-west1-a --project=sunblaze-4
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

## Step 2: Export boot disk to qcow2

```bash
DATE=$(date +%Y%m%d)
IMAGE_KEY=cpu-free

./scripts/export-gcp-to-qcow2.sh --image-key ${IMAGE_KEY} --date ${DATE}
```

This creates `gs://agenthle/vm-images/${IMAGE_KEY}-${DATE}.qcow2` via Cloud Build. The export takes a few minutes. Monitor progress:

```bash
gcloud builds list --project=sunblaze-4 --limit=5
```

## Step 3: Transfer to KVM host and cold-boot test

Copy the exported qcow2 to a KVM host and run the cold-boot test:

```bash
# On the KVM host:
gsutil cp gs://agenthle/vm-images/${IMAGE_KEY}-${DATE}.qcow2 \
    /home/weichenzhang/agenthle-env-images/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2

./scripts/cold-boot-test.sh --qcow2 /home/weichenzhang/agenthle-env-images/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2
```

The test cold-boots the qcow2 without any snapshot tag and verifies the CUA server responds on `/status`. Expect ~3-5 minutes for a cold boot.

**NOTE:** No `savevm` bake step is needed. The server creates shape-based snapshot tags (e.g., `4vcpu-8gb-64gb`) automatically on first cache miss. First boot after deployment takes ~4-5 minutes; subsequent boots from cache take ~30 seconds.

## Step 4: Upload to GCS (source of truth)

```bash
./scripts/upload-template.sh --image-key ${IMAGE_KEY} --date ${DATE} \
    --qcow2 /home/weichenzhang/agenthle-env-images/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2
```

This uploads to `gs://agenthle-images/templates/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2`. This step is **required** -- workers prewarm templates from GCS at startup (`prewarm_templates()`). Skipping this means new workers get a stale image.

## Step 5: Sync task data (if changed)

If you added or modified task data (input files, software, reference data), sync GCS to the shared task-data disk. GCS bucket `gs://agenthle/task-data/` is the source of truth.

**Single-node (standalone):**

```bash
gsutil -m rsync -r gs://agenthle/task-data/ /mnt/agenthle-task-data/
```

**Multi-node (cluster):** The shared persistent disk (`agenthle-nested-kvm-01-task-data`) is attached read-only to all workers. To update:

```bash
# 1. Detach the PD from all workers (or schedule during maintenance window)
# 2. Attach RW to a single node and rsync
gcloud compute instances attach-disk <node> \
    --disk=agenthle-nested-kvm-01-task-data --device-name=task-data --mode=rw \
    --zone=us-central1-a --project=sunblaze-4
gcloud compute ssh <node> -- \
    'sudo mount /dev/disk/by-id/google-task-data /mnt/agenthle-task-data-ro && \
     gsutil -m rsync -r gs://agenthle/task-data/ /mnt/agenthle-task-data-ro/ && \
     sudo umount /mnt/agenthle-task-data-ro'

# 3. Re-attach RO to workers (or restart workers — clone-worker.sh handles this)
```

Alternatively, each worker's OverlayFS upper layer can absorb incremental writes during task execution. For bulk data updates, the PD rsync path above is cleaner.

## Step 6: Update images.yaml

Bump the `version` field and update paths. The version change invalidates all worker snapshot caches, forcing a fresh cold boot with the new template.

Example diff for `cpu-free`:

```yaml
  cpu-free:
    enabled: true
    os_family: windows
    published_ports: [5000]
    local:
-     template_qcow2_path: /home/weichenzhang/agenthle-env-images/cpu-free/cpu-free-20260413.qcow2
-     gcs_uri: gs://agenthle-images/templates/cpu-free/cpu-free-20260413.qcow2
-     version: "20260413"
+     template_qcow2_path: /home/weichenzhang/agenthle-env-images/cpu-free/cpu-free-20260415.qcow2
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
cd /path/to/cua-house && git pull
sudo systemctl restart cua-house-worker
```

```bash
# Option B: Provision new workers (handles git clone, config, systemd)
scripts/clone-worker.sh <kvm-host> <worker-id>
```

## Step 7: Verify deployment

Checklist:

1. Worker pulled new template from GCS (check worker logs for `pull_template`)
2. First VM cold-booted and shape tag created (check for `cache miss` in logs)
3. Subsequent VMs used cache (check for `from_cache=True` in events)
4. Smoke batch completes end-to-end

```bash
MASTER=http://<master-ip>:8787

# Check worker status and hosted images
curl -sS ${MASTER}/v1/cluster/workers | python3 -m json.tool

# Set desired pool state (adjust worker_id to your cluster)
curl -sS -X PUT ${MASTER}/v1/cluster/pool \
  -H 'Content-Type: application/json' \
  -d '{"assignments":[
    {"worker_id":"kvm02","image_key":"cpu-free","count":1,"vcpus":4,"memory_gb":8,"disk_gb":64}
  ]}'

# Wait for VM ready (~30s cache hit, ~4-5min cache miss)
sleep 60

# Submit a smoke task
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

# VM image maintenance

How to update the Windows VM images used by cua-house. There are two image types with different workflows:

| Image key | Where it runs | Asset type |
|-----------|--------------|------------|
| `cpu-free` | kvm0 (local QEMU) | Versioned qcow2 file |
| `cpu-license` | kvm0 (local QEMU) | Versioned qcow2 file |
| `gpu-free` | GCP (g2-standard-4) | GCP Image (boot) + GCP Snapshot (data) |
| `gpu-license` | GCP (g2-standard-4) | GCP Image (boot) + GCP Snapshot (data) |

## Naming conventions

| Asset | Convention | Example |
|-------|-----------|---------|
| CPU qcow2 | `{image_key}-YYYYMMDD.qcow2` | `cpu-free-20260405.qcow2` |
| GPU boot image | `agenthle-dev-{image_key}-YYYYMMDD` | `agenthle-dev-gpu-free-20260405` |
| GPU data snapshot | `agenthle-dev-{image_key}-data-YYYYMMDD` | `agenthle-dev-gpu-free-data-20260405` |

**Keep old assets until the new image is confirmed working in production.** Delete old assets only after a successful eval run with the new image.

---

## CPU VMs (kvm0): `cpu-free`, `cpu-license`

CPU VM images are stored as qcow2 files on kvm0. The update workflow:

1. **Make changes on the GCP dev VM**
2. **Export qcow2 from GCP → transfer to kvm0**
3. **Test on kvm0 (cold boot)**
4. **Take savevm snapshot via QMP**
5. **Register and switch to new image**

### Step 1: Make changes on the GCP dev VM

SSH into the dev VM (e.g. `agenthle-dev-cpu-free`) via GCP Console or:

```bash
gcloud compute ssh agenthle-dev-cpu-free --zone=us-west1-a --project=sunblaze-4
```

Make your changes: install software, update config, etc. Shut down Windows cleanly when done.

### Step 2: Export boot disk from GCP → transfer to kvm0

From any machine with gcloud access:

```bash
DATE=20260405   # adjust to today
IMAGE_KEY=cpu-free

# 1. Create a snapshot of the dev VM's boot disk
DISK=$(gcloud compute instances describe agenthle-dev-${IMAGE_KEY} \
    --zone=us-west1-a --project=sunblaze-4 \
    --format='value(disks[0].source)' | sed 's|.*/||')
gcloud compute disks snapshot $DISK \
    --snapshot-names=agenthle-dev-${IMAGE_KEY}-export-${DATE} \
    --zone=us-west1-a --project=sunblaze-4

# 2. Export snapshot to GCS as qcow2
gcloud compute images create agenthle-dev-${IMAGE_KEY}-export-${DATE} \
    --source-snapshot=agenthle-dev-${IMAGE_KEY}-export-${DATE} \
    --project=sunblaze-4
gcloud compute images export \
    --image=agenthle-dev-${IMAGE_KEY}-export-${DATE} \
    --export-format=qcow2 \
    --destination-uri=gs://agenthle/vm-images/${IMAGE_KEY}-${DATE}.qcow2 \
    --project=sunblaze-4

# 3. Copy to kvm0
# Run on kvm0 (or use gsutil from kvm0 directly):
gsutil cp gs://agenthle/vm-images/${IMAGE_KEY}-${DATE}.qcow2 \
    /home/weichenzhang/agenthle-env-images/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2
```

> Note: `gcloud compute images export` uses Cloud Build and takes a few minutes. Check progress with `gcloud builds list --project=sunblaze-4`.

### Step 3: Test on kvm0 (cold boot, no server involvement)

On kvm0, start a temporary container to verify the image boots correctly:

```bash
DATE=20260405
IMAGE_KEY=cpu-free
QCOW2=/home/weichenzhang/agenthle-env-images/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2

# Copy to a temp working dir
mkdir -p /tmp/vm-test/storage
cp $QCOW2 /tmp/vm-test/storage/vm.qcow2

docker run --rm -d \
    --name cua-house-test \
    --device /dev/kvm \
    -e CPU_MODEL=host \
    -e HV=N \
    -e RAM_SIZE=8G \
    -e CPU_CORES=4 \
    -p 15900:5000 \
    -v /tmp/vm-test/storage:/storage \
    trycua/cua-qemu-windows:latest

# Wait for Windows to boot (~3-5 min cold boot without -loadvm)
# Then check CUA server is up:
curl -s http://127.0.0.1:15900/status

# Clean up
docker rm -f cua-house-test
```

If the CUA server responds with HTTP 200, the image is good.

### Step 4: Take savevm snapshot via QMP

Start a container from the new qcow2, wait for it to be ready, then save the snapshot:

```bash
DATE=20260405
IMAGE_KEY=cpu-free
QCOW2=/home/weichenzhang/agenthle-env-images/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2

mkdir -p /tmp/vm-snap/storage
cp $QCOW2 /tmp/vm-snap/storage/vm.qcow2

# Start container (cold boot, no -loadvm yet)
docker run --rm -d \
    --name cua-house-snap \
    --device /dev/kvm \
    -e CPU_MODEL=host \
    -e HV=N \
    -e RAM_SIZE=8G \
    -e CPU_CORES=4 \
    -p 15901:5000 \
    -v /tmp/vm-snap/storage:/storage \
    trycua/cua-qemu-windows:latest

# Wait until CUA server is responsive
until curl -s http://127.0.0.1:15901/status | grep -q 200 2>/dev/null; do
    curl -so /dev/null -w "%{http_code}" http://127.0.0.1:15901/status 2>/dev/null
    sleep 5
done
echo "VM ready"

# Save snapshot via QMP (stop → savevm → cont)
docker exec cua-house-snap bash -c \
    'echo "{ \"execute\": \"qmp_capabilities\" }" | nc -q1 127.0.0.1 7200'
docker exec cua-house-snap bash -c \
    'printf "%s\n" \
        "{\"execute\":\"stop\"}" \
        "{\"execute\":\"savevm\",\"arguments\":{\"name\":\"'${IMAGE_KEY}'\"}}" \
        "{\"execute\":\"cont\"}" | nc -q3 127.0.0.1 7200'

# Stop container WITHOUT removing the storage dir
docker stop cua-house-snap

# Move the qcow2 (now with snapshot baked in) to the image dir
cp /tmp/vm-snap/storage/vm.qcow2 $QCOW2
rm -rf /tmp/vm-snap
```

> The QMP `savevm` writes the snapshot directly into `vm.qcow2`. The `docker stop` leaves the file intact.

### Step 5: Verify snapshot is present

```bash
qemu-img snapshot -l $QCOW2
# Should show:    ID  TAG              VM SIZE  DATE         VM CLOCK
#                  1  cpu-free         ...      ...          ...
```

### Step 6: Register and switch

Update `images.yaml` to point to the new qcow2:

```yaml
cpu-free:
  enabled: true
  runtime_mode: local
  template_qcow2_path: /home/weichenzhang/agenthle-env-images/cpu-free/cpu-free-20260405.qcow2
```

Restart the cua-house server:

```bash
# On kvm0
systemctl restart cua-house-server
# or if running manually:
pkill -f cua-house-server && cua-house-server --host-config /etc/cua-house/server.yaml --image-catalog /etc/cua-house/images.yaml &
```

Watch startup logs:

```bash
tail -f ~/.logs/cua-house/server.log
# Expect: VM pool ready events within ~30s (not 4-5 min)
```

### Step 7: Sync task data (if needed)

```bash
gsutil -m rsync -r gs://agenthle/task-data/ /mnt/agenthle-task-data/
```

---

## GPU VMs (GCP): `gpu-free`, `gpu-license`

GPU VMs run entirely on GCP. Two independent assets need updating: the **boot disk** (stored as a GCP Image) and the **data disk** (stored as a GCP Snapshot).

### Boot disk → GCP Image

```bash
DATE=20260405
IMAGE_KEY=gpu-free
ZONE=us-west1-a
PROJECT=sunblaze-4

# 1. Stop the dev VM if running
gcloud compute instances stop agenthle-dev-${IMAGE_KEY} \
    --zone=$ZONE --project=$PROJECT

# 2. Create a snapshot of the boot disk
DISK=$(gcloud compute instances describe agenthle-dev-${IMAGE_KEY} \
    --zone=$ZONE --project=$PROJECT \
    --format='value(disks[0].source)' | sed 's|.*/||')
gcloud compute disks snapshot $DISK \
    --snapshot-names=agenthle-dev-${IMAGE_KEY}-boot-snap-${DATE} \
    --zone=$ZONE --project=$PROJECT

# 3. Create a GCP Image from the snapshot
gcloud compute images create agenthle-dev-${IMAGE_KEY}-${DATE} \
    --source-snapshot=agenthle-dev-${IMAGE_KEY}-boot-snap-${DATE} \
    --project=$PROJECT

# 4. Update images.yaml
#    gcp_boot_image: agenthle-dev-gpu-free-20260405
```

### Data disk → GCP Snapshot

The data disk stores the task data (input files, software, etc.) separately from the OS.

```bash
DATE=20260405
IMAGE_KEY=gpu-free
ZONE=us-west1-a
PROJECT=sunblaze-4

# Find the data disk (typically the second disk attached to the dev VM)
DATA_DISK=$(gcloud compute instances describe agenthle-dev-${IMAGE_KEY} \
    --zone=$ZONE --project=$PROJECT \
    --format='value(disks[1].source)' | sed 's|.*/||')

# Create snapshot directly (no GCP Image needed — cua-house creates the disk from snapshot)
gcloud compute disks snapshot $DATA_DISK \
    --snapshot-names=agenthle-dev-${IMAGE_KEY}-data-${DATE} \
    --zone=$ZONE --project=$PROJECT

# Update images.yaml:
#   gcp_data_snapshot: agenthle-dev-gpu-free-data-20260405
```

> **Why snapshot instead of image for data disk?** cua-house creates data disks directly from a snapshot via `--source-snapshot`. This is faster (~10s disk creation) and cheaper than going through a GCP Image. Boot disks use GCP Images because `--image` is required for the boot disk in `gcloud compute instances create`.

### Apply updates to images.yaml

After creating new assets, update `/etc/cua-house/images.yaml` on the server:

```yaml
gpu-free:
  enabled: true
  runtime_mode: gcp
  gcp_project: sunblaze-4
  gcp_zone: us-west1-a
  gcp_network: osworld-vpc
  gcp_service_account: agenthle-vm-service@sunblaze-4.iam.gserviceaccount.com
  gcp_machine_type: g2-standard-4
  gcp_boot_image: agenthle-dev-gpu-free-20260405   # ← updated
  gcp_data_snapshot: agenthle-dev-gpu-free-data-20260405  # ← updated
  gcp_boot_disk_gb: 64
  gcp_data_disk_gb: 200
  gpu_type: nvidia-l4
  gpu_count: 1
  default_cpu_cores: 4
  default_memory_gb: 16
  max_concurrent_vms: 2
```

Restart cua-house-server to pick up the new config. GCP VMs are created on-demand so no pool restart is needed.

# VM image maintenance

How to update the Windows VM images used by cua-house. There are two image types with different workflows.

> **For the step-by-step operational workflow** (export, test, upload, deploy),
> see [image-update-sop.md](image-update-sop.md). This document covers image
> internals, naming conventions, and guest-specific requirements.

## Current local images

| Image key | qcow2 file | GCS object (baked) | Bake date | Description |
|-----------|-----------|--------------------|-----------|-------------|
| `cpu-free` | `cpu-free-20260413.qcow2` | `gs://agenthle-images/templates/cpu-free/cpu-free-20260413.qcow2` | 2026-04-13 (re-bake w/ savevm) | **Active** (kvm-02) — Re-baked on kvm-02 because the prior 2026-04-06 blob in GCS had no savevm tag, so any pool using `-loadvm cpu-free` failed at QEMU start (pitfall #17). Guest content unchanged from the 2026-04-06 bake. |
| `cpu-free-ubuntu` | `cpu-free-ubuntu-20260408.qcow2` | `gs://agenthle-images/templates/cpu-free-ubuntu/cpu-free-ubuntu-20260408.qcow2` | 2026-04-09 (re-bake w/ cifs-utils) | **Active** — Ubuntu 22.04 with CUA server + agents (Claude Code, OpenClaw, Codex). Re-baked on kvm-02 after the original 2026-04-08 19:33 bake was found to be missing `cifs-utils` (pitfall #13). |
| `waa` | `waa-20260408.qcow2` | `gs://agenthle-images/templates/waa/waa-20260408.qcow2` | 2026-04-10 | **Active** (kvm-02) — Windows Agent Arena environment. Ships its own server on port 5000 (not cua-computer-server) but exposes the same `/status` interface. Baked via QEMU monitor `savevm` on kvm-02. |
| `cpu-license` | `cpu-license-20260405.qcow2` | *(not uploaded)* | 2026-04-05 | Not yet updated with bridge changes. |

> **Source of truth for local templates is GCS**, not any particular KVM host. The cua-house-server's `prewarm_templates()` auto-pulls from the `gcs_uri` in `images.yaml` at worker startup, so any new node gets the current baked version for free. After **any** local re-bake, you MUST upload the new qcow2 back to GCS (`gsutil cp ...`) or future nodes will pull a stale version and either fail `-loadvm` or hit already-fixed guest-side bugs.
>
> On kvm-02 the images live on `/mnt/xfs/images/{image_key}/` (XFS+reflink) and are accessed through the `/home/weichenzhang/agenthle-env-images → /mnt/xfs/images` symlink so the same `template_qcow2_path` in `images.yaml` resolves on both legacy (kvm0) and current (kvm-02) hosts without per-host config drift.

---

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

## Image-version bump SOP

Each worker keeps a persistent snapshot cache at `snapshot_cache_dir`
(required field in the host config; typically `/mnt/xfs/snapshot-cache`
on the XFS volume). Entries are keyed by
`(image_key, image_version, vcpus, memory_gb, disk_gb)` so different
versions coexist in different subdirs — but in practice, stale versions
just waste disk and can cause confusion if a worker is rolled back.

When you bump an image's `version` field in `images.yaml` after re-baking:

1. **Stop all workers** (`pkill -f cua_house_server.cli` on each worker,
   or stop the exact manually-started process).
2. **Purge the cache volume**: `rm -rf /mnt/xfs/snapshot-cache/*` on each
   worker. (Alternative surgical option: `rm -rf
   /mnt/xfs/snapshot-cache/<image_key>/v<old_version>/` to keep other
   images' caches warm. Default to the full wipe unless you have a
   specific reason otherwise — a single stale shape reviving unexpectedly
   has historically caused debug sinks.)
3. **Update `images.yaml`**: bump `local.version`, `local.template_qcow2_path`,
   and `local.gcs_uri` to the new qcow2.
4. **Restart workers**. Startup will parallel-pull the new templates from
   GCS, then re-register with master. The first task of each shape on
   each worker pays one cold-boot (~4-5 min) before the new version's
   cache entries are written; subsequent same-shape tasks are fast.

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

### Step 4: Take savevm snapshot via QEMU monitor (standalone / legacy only)

> **Cluster mode note:** This step is only needed for legacy standalone mode.
> In cluster mode, the server creates shape-based snapshot tags (e.g.,
> `4vcpu-8gb-64gb`) automatically on the first cache miss for a given shape,
> so you do not need to manually bake a `savevm` snapshot.

Start a container from the new qcow2, wait for it to be ready, then save the snapshot. The recipe below is what works after the cua-house-server's accumulated patches; deviating from any of the gotchas re-runs into pitfalls 1, 3, 5.

```bash
DATE=20260408
IMAGE_KEY=waa
QCOW2=/mnt/xfs/images/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2

# Use a temp bake dir so we don't touch the template until savevm is confirmed.
# Disk MUST be named data.qcow2 (DISK_NAME=data) -- pitfall #5.
rm -rf /tmp/${IMAGE_KEY}-bake/storage
mkdir -p /tmp/${IMAGE_KEY}-bake/storage
cp $QCOW2 /tmp/${IMAGE_KEY}-bake/storage/data.qcow2

docker rm -f ${IMAGE_KEY}-bake 2>/dev/null
docker run -d \
    --name ${IMAGE_KEY}-bake \
    --device=/dev/kvm \
    --cap-add NET_ADMIN \
    -v /tmp/${IMAGE_KEY}-bake/storage:/storage \
    -v /mnt/xfs/runtime/boot-patched.sh:/run/boot.sh:ro \
    -p 127.0.0.1:16900:5000 \
    -p 127.0.0.1:16906:8006 \
    -e RAM_SIZE=8G \
    -e CPU_CORES=4 \
    -e CPU_MODEL=host \
    -e HV=N \
    -e VM_NET_IP=172.30.0.2 \
    -e DISK_NAME=data \
    -e LOADVM_SNAPSHOT= \
    trycua/cua-qemu-windows:latest

# Why each gotcha:
#   --cap-add NET_ADMIN  → bridge networking; without it dockur falls back to passt
#                          user-mode and port 5000 is not auto-forwarded (no readiness)
#   patched boot.sh      → converts pflash UEFI vars from raw to qcow2 so savevm
#                          can write a snapshot tag (pitfall #3)
#   LOADVM_SNAPSHOT=     → empty value disables the loadvm + watchdog patches so
#                          cold boot proceeds. With current server code the env
#                          var is safe to omit entirely (patch uses default),
#                          but older cached boot-patched.sh still crashes under
#                          set -u if the var is unset (pitfall #18), so keep
#                          setting it explicitly when in doubt.
#   VM_NET_IP=172.30.0.2 → freezes the snapshot's guest IP to match what
#                          cua-house-server containers will use (pitfall #2)

# Wait until the in-guest server is responsive (~1-3 min for cold boot)
until curl -sf -m 3 http://127.0.0.1:16900/status > /dev/null 2>&1; do
    sleep 10
done
echo "VM ready"

# Save snapshot via the QEMU human monitor on port 7100 (always exposed by
# dockur). The QMP/JSON path on port 7200 only exists if you pass
# `-e ARGUMENTS=-qmp tcp:0.0.0.0:7200,server,nowait` to docker run.
docker exec ${IMAGE_KEY}-bake bash -c \
    "(echo stop; sleep 2; echo savevm ${IMAGE_KEY}; sleep 90; echo info snapshots; sleep 2; echo cont; sleep 1; echo quit) | timeout 180 nc localhost 7100"
# Expect "info snapshots" to list a row with TAG = ${IMAGE_KEY}.

# Stop the container with `docker kill` (NOT docker stop) to avoid the graceful
# shutdown writing a partial state into data.qcow2 (Ubuntu-bake pitfall).
docker kill ${IMAGE_KEY}-bake
docker rm ${IMAGE_KEY}-bake

# Verify the snapshot tag landed
qemu-img snapshot -l /tmp/${IMAGE_KEY}-bake/storage/data.qcow2

# Move the baked qcow2 back into the template path
mv /tmp/${IMAGE_KEY}-bake/storage/data.qcow2 $QCOW2
rm -rf /tmp/${IMAGE_KEY}-bake
qemu-img snapshot -l $QCOW2   # final sanity check
```

> The patched `boot.sh` is auto-generated by cua-house-server on first run and
> lives at `{runtime_root}/boot-patched.sh`. If your runtime_root differs from
> `/mnt/xfs/runtime`, adjust the `-v` mount accordingly.

### Step 5: Verify snapshot is present

```bash
qemu-img snapshot -l $QCOW2
# Should show:    ID  TAG              VM SIZE  DATE         VM CLOCK
#                  1  cpu-free         ...      ...          ...
```

> **Cluster mode note:** In cluster mode, the server creates shape-based
> snapshot tags (e.g., `4vcpu-8gb-64gb`) at runtime. The template's snapshot
> tag shown above is only used by legacy standalone mode.

### Step 5b: Upload baked qcow2 back to GCS (REQUIRED)

**Always** push the baked qcow2 back to GCS. Skipping this step means the
`gcs_uri` configured in `images.yaml` no longer matches what's on disk, and
any future node that pulls the template will get a stale version — either
missing a savevm tag (cache-hit `-loadvm` fails with "Snapshot does not exist") or
missing a guest-side fix that was added in the local re-bake (e.g. the
`cifs-utils` pitfall #13 drift we hit with `cpu-free-ubuntu-20260408`).

```bash
gsutil -o GSUtil:parallel_composite_upload_threshold=150M \
    cp $QCOW2 gs://agenthle-images/templates/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2

# Sanity: confirm Content-Length matches the local file
gsutil stat gs://agenthle-images/templates/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2 | grep Content-Length
stat -c '%s' $QCOW2
```

Then update the **Current local images** table at the top of this doc with
the new bake date so other operators know what's in GCS.

### Step 6: Register and switch

Update `images.yaml` to point to the new qcow2:

```yaml
cpu-free:
  enabled: true
  os_family: windows
  published_ports: [5000]
  local:
    template_qcow2_path: /mnt/xfs/images/cpu-free/cpu-free-20260415.qcow2
    gcs_uri: gs://agenthle-images/templates/cpu-free/cpu-free-20260415.qcow2
    version: "20260415"
    default_vcpus: 4
    default_memory_gb: 8
```

> **Bump `version`** every time you re-bake the template. The server uses
> this field to detect stale local copies and re-pull from `gcs_uri`.

Restart the cua-house server:

```bash
pkill -f cua_house_server.cli || true
setsid nohup uv run python -m cua_house_server.cli \
  --host-config /etc/cua-house/server.yaml \
  --image-catalog /etc/cua-house/images.yaml \
  --host 0.0.0.0 --port 8787 --mode standalone \
  </dev/null >server.log 2>&1 &
disown
```

Watch startup logs:

```bash
tail -f server.log
# Expect startup to complete; first same-shape task after the version bump
# will cold-boot and write a fresh cache entry.
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
#    gcp:
#      boot_image: agenthle-dev-gpu-free-20260405
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
#   gcp:
#     data_snapshot: agenthle-dev-gpu-free-data-20260405
```

> **Why snapshot instead of image for data disk?** cua-house creates data disks directly from a snapshot via `--source-snapshot`. This is faster (~10s disk creation) and cheaper than going through a GCP Image. Boot disks use GCP Images because `--image` is required for the boot disk in `gcloud compute instances create`.

### Apply updates to images.yaml

After creating new assets, update `/etc/cua-house/images.yaml` on the server:

```yaml
gpu-free:
  enabled: true
  os_family: linux
  published_ports: [5000]
  gcp:
    project: sunblaze-4
    zone: us-west1-a
    network: osworld-vpc
    service_account: agenthle-vm-service@sunblaze-4.iam.gserviceaccount.com
    default_machine_type: g2-standard-4
    boot_image: agenthle-dev-gpu-free-20260405   # updated
    data_snapshot: agenthle-dev-gpu-free-data-20260405  # updated
    boot_disk_gb: 64
    data_disk_gb: 200
    gpu_type: nvidia-l4
    gpu_count: 1
```

Restart cua-house-server to pick up the new config. GCP VMs are created on-demand so no pool restart is needed.

---

## Ubuntu VMs (kvm0): `cpu-free-ubuntu`

Ubuntu VM images follow the same workflow as CPU Windows images with these differences:

### Guest requirements

The Ubuntu qcow2 must have:

- **Docker disabled**: `systemctl disable docker docker.socket containerd`. Guest Docker creates a `docker0` bridge with `172.17.0.0/16` routes that conflict with the container's Docker network, breaking port forwarding.
- **Wildcard netplan**: QEMU virtio-net interface names vary by PCI slot. Use match-all:
  ```yaml
  # /etc/netplan/50-cloud-init.yaml
  network:
    version: 2
    renderer: networkd
    ethernets:
      all-en:
        match:
          name: "en*"
        dhcp4: true
      all-eth:
        match:
          name: "eth*"
        dhcp4: true
  ```
- **iptables flush service**: GCP guest agent injects iptables rules that persist in snapshots. Add a oneshot service:
  ```ini
  # /etc/systemd/system/flush-iptables.service
  [Unit]
  Description=Flush iptables rules for non-GCP environments
  Before=network-pre.target
  Wants=network-pre.target
  [Service]
  Type=oneshot
  ExecStart=/usr/sbin/iptables -F
  ExecStart=/usr/sbin/iptables -X
  ExecStart=/usr/sbin/iptables -P INPUT ACCEPT
  ExecStart=/usr/sbin/iptables -P FORWARD ACCEPT
  ExecStart=/usr/sbin/iptables -P OUTPUT ACCEPT
  [Install]
  WantedBy=multi-user.target
  ```
- **GCP agents disabled**: `systemctl disable google-cloud-ops-agent google-guest-agent google-osconfig-agent`
- **No data disk fstab entry**: Remove any UUID-based mount for the GCP data disk.

### Docker image

Ubuntu images use `trycua/cua-qemu-windows:latest` (same as Windows). The dockur/windows base boots any UEFI qcow2 regardless of OS. `trycua/cua-qemu-linux:latest` is for fresh ISO installs only and does not support importing existing qcow2 disks.

### Baking the snapshot

The cold boot and savevm workflow is the same as Windows (see Steps 3-4 above), but use the patched boot.sh (pflash qcow2 conversion + loadvm support) and `--cap-add NET_ADMIN`:

```bash
# Generate patched boot.sh via server code
cd /home/weichenzhang/cua-house
uv run python3 -c '
from cua_house_server.config.loader import load_host_runtime_config
from cua_house_server.runtimes.qemu import DockerQemuRuntime
cfg = load_host_runtime_config("/path/to/server.yaml")
rt = DockerQemuRuntime(cfg)
print(rt._ensure_patched_boot_sh())
'

# Start container with patched boot.sh (no LOADVM_SNAPSHOT for cold boot)
docker run -d --name ubuntu-bake \
    --device=/dev/kvm \
    --cap-add NET_ADMIN \
    -v /tmp/bake/storage:/storage \
    -v /home/weichenzhang/agenthle-env-runtime/boot-patched.sh:/run/boot.sh:ro \
    -p 127.0.0.1:16000:5000 \
    -e RAM_SIZE=8G -e CPU_CORES=4 -e CPU_MODEL=host -e HV=N \
    -e 'ARGUMENTS=-qmp tcp:0.0.0.0:7200,server,nowait' \
    trycua/cua-qemu-windows:latest

# Wait ~7 min for cold boot, verify CUA
curl -s http://127.0.0.1:16000/status

# savevm via telnet monitor (port 7100)
docker exec ubuntu-bake bash -c 'echo savevm cpu-free-ubuntu | timeout 120 nc localhost 7100'
```

### Current GCP dev VM

| VM name | IP | Zone | Purpose |
|---------|---|------|---------|
| `agenthle-ubuntu` | ephemeral | us-west2-a | Ubuntu 22.04 source VM. CUA server + agents pre-installed. |

### Image storage

All images (Windows and Ubuntu) stored on the dedicated 512 GB image disk:

- Mount: `/mnt/agenthle-env-images` (ext4, label `agenthle-images`)
- Symlink: `/home/weichenzhang/agenthle-env-images` → `/mnt/agenthle-env-images`
- Disk: `agenthle-nested-kvm-01-images` (pd-balanced, us-central1-a)

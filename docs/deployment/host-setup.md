# Host setup

How to set up a KVM host to run cua-house-server with Docker+QEMU local runtime.

> **Deploying a multi-node cluster?** This document covers the
> standalone (single-node) model. For the master + worker cluster model
> see [cluster.md](cluster.md). A worker node is identical to a
> standalone host except:
>
> - `mode: worker` in `server.yaml` and a `cluster:` section pointing
>   at the master
> - `vm_bind_address: 0.0.0.0` so VM ports are reachable across the VPC
> - `vm_pool: []` — the worker's pool is pushed dynamically by master
>   at runtime via PoolOp messages
>
> Everything below (filesystem layout, OverlayFS task-data sharing,
> images.yaml, docker image) applies unchanged to workers.

## Host requirements

- Linux (tested on Ubuntu 22.04/24.04)
- Docker installed and running
- `/dev/kvm` accessible (nested virtualization enabled if running on a cloud VM)
- Minimum 8 CPUs, 32 GB RAM (for 2+ concurrent VMs with 4 vCPU / 8-16 GB each)
- Fast storage for QEMU VM disks (SSD recommended for snapshot performance)
- XFS filesystem with reflink support recommended for instant VM slot provisioning
- `gsutil` installed if using GCS-based template distribution

## Filesystem layout

| Path | Purpose |
|------|---------|
| `{runtime_root}/` | Runtime state: per-VM disks, logs, events. Configured in `server.yaml`. |
| `{runtime_root}/slots/` | Per-VM directories (storage/ and logs/) |
| `{runtime_root}/events.jsonl` | Structured JSONL event log |
| `{runtime_root}/boot-patched.sh` | Auto-generated patched boot script for snapshot support |
| `{image_root}/cpu-free-YYYYMMDD.qcow2` | Versioned template qcow2 with pre-baked snapshot. Configured per image in `images.yaml`. |
| `{task_data_root}/` | Task data mount point. Configured in `server.yaml`. |

Example (XFS setup — images and runtime on the same XFS disk for reflink):

```
/mnt/xfs/                                                 # XFS disk with reflink=1
/mnt/xfs/images/cpu-free/cpu-free-20260406.qcow2          # Windows template
/mnt/xfs/images/cpu-free-ubuntu/cpu-free-ubuntu-20260408.qcow2  # Ubuntu template
/mnt/xfs/runtime/                                         # runtime_root (slots go here)
/mnt/agenthle-task-data/                                  # task_data_root (separate disk)
```

When `gcs_uri` is configured in `images.yaml` and the template does not exist locally, the server automatically pulls it from GCS on first startup.

## Docker image

The server uses `trycua/cua-qemu-windows:latest` (configurable via `docker_image` in `server.yaml`). This image contains QEMU with Windows support and a CUA computer-server that starts on port 5000.

Pull before first run:

```bash
docker pull trycua/cua-qemu-windows:latest
```

## Installation

```bash
git clone <repo-url>
cd cua-house
uv sync
```

## Configuration

Copy and edit the default config files:

```bash
cp packages/server/src/cua_house_server/config/defaults/server.yaml /etc/cua-house/server.yaml
cp packages/server/src/cua_house_server/config/defaults/images.yaml /etc/cua-house/images.yaml
```

Edit `server.yaml`:

- `host_id`: unique identifier for this host
- `host_external_ip`: set to `auto` on GCE (uses metadata API) or a static IP
- `runtime_root`: path for runtime state
- `task_data_root`: path to task data directory (or null if not used)
- `vm_pool`: list of VM pool entries to pre-boot at startup

Edit `images.yaml`:

- Set `template_qcow2_path` for local images (versioned path, e.g. `cpu-free-20260405.qcow2`)
- Set `enabled: true` for images you want active

## systemd service

Create `/etc/systemd/system/cua-house-server.service`:

```ini
[Unit]
Description=cua-house server
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/cua-house
ExecStart=/opt/cua-house/.venv/bin/cua-house-server \
    --host-config /etc/cua-house/server.yaml \
    --image-catalog /etc/cua-house/images.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable cua-house-server
sudo systemctl start cua-house-server
```

## Firewall

The server listens on a single port (default 8787). All CUA and noVNC traffic is reverse-proxied through this port using host-based routing (`lease-{id}.{base_host}`).

Only port 8787 needs to be open to clients. Internal CUA ports (15000-15999) and noVNC ports (18000-18999) bind to 127.0.0.1 and should not be exposed externally.

## Task data disk provisioning

Task data is organized as `{category}/{task_tag}/{variant}/input/`, `reference/`, `software/`, `output/`. In VM pool mode, `task_data_root` is bind-mounted into each container at `/shared/agenthle` and exposed to guests via Samba (Windows: `E:` drive; Linux: CIFS mount at `/media/user/data/agenthle`).

### Simple setup (single-node, read-write)

Mount a local writable disk at `task_data_root` (e.g., `/mnt/agenthle-task-data`) and populate it from GCS:

```bash
gsutil -m rsync -r gs://agenthle/task-data/ /mnt/agenthle-task-data/
```

### Multi-node setup (shared read-only + OverlayFS)

To share a single task-data disk across multiple KVM nodes without duplication, attach the GCP persistent disk to multiple VMs in `READ_ONLY` mode and use OverlayFS on each node to provide a local writable layer. Writes (e.g., `output/`) land on the local upper layer; reads of `input/`, `reference/`, `software/` transparently pass through to the shared disk.

```bash
# 1. Attach the disk in multi-reader mode
gcloud compute instances attach-disk <node> \
    --disk=<shared-task-data-disk> --device-name=task-data --mode=ro \
    --zone=<zone> --project=<project>

# 2. Mount the shared disk at a separate lower-layer path
sudo mkdir -p /mnt/agenthle-task-data-ro
sudo mount -o ro,noload /dev/disk/by-id/google-task-data /mnt/agenthle-task-data-ro
# (noload skips ext4 journal replay — required for read-only multi-attach)

# 3. Create upper + work dirs on a local writable filesystem (XFS recommended)
sudo mkdir -p /mnt/xfs/task-data-upper /mnt/xfs/task-data-work
sudo chown $(id -u):$(id -g) /mnt/xfs/task-data-upper /mnt/xfs/task-data-work

# 4. Mount the overlay at task_data_root (what the server reads)
sudo mkdir -p /mnt/agenthle-task-data
sudo mount -t overlay overlay \
    -o lowerdir=/mnt/agenthle-task-data-ro,upperdir=/mnt/xfs/task-data-upper,workdir=/mnt/xfs/task-data-work \
    /mnt/agenthle-task-data
```

To persist across reboots, add to `/etc/fstab`:

```
LABEL=<disk-label> /mnt/agenthle-task-data-ro ext4 ro,noload,nofail 0 0
overlay /mnt/agenthle-task-data overlay lowerdir=/mnt/agenthle-task-data-ro,upperdir=/mnt/xfs/task-data-upper,workdir=/mnt/xfs/task-data-work,nofail 0 0
```

The upper layer (`/mnt/xfs/task-data-upper/`) accumulates over time. The server's staging phase resets each task's `output/` dir before every run, so stale outputs don't leak between task executions. You can periodically wipe the entire upper layer if disk usage grows too large:

```bash
sudo rm -rf /mnt/xfs/task-data-upper/* /mnt/xfs/task-data-work/*
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `CUA_HOUSE_TOKEN` | Bearer token for API authentication (optional) |
| `CUA_HOUSE_SERVER_EXTERNAL_IP` | Override auto-detected external IP |
| `GCLOUD_PATH` | Path to gcloud binary (default: `gcloud`) |

## Recovery and cleanup

On startup, the server automatically kills orphaned `cua-house-env-*` Docker containers from previous runs.

If the server crashes, simply restart it. The cleanup step ensures a clean slate.

To manually clean up:

```bash
docker ps -aq --filter name=cua-house-env- | xargs -r docker rm -f
rm -rf /path/to/runtime_root/slots/
```

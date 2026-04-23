# Host setup

How to set up a KVM host to run cua-house-server with Docker+QEMU local runtime.

> **Deploying a multi-node cluster?** This document covers the
> standalone (single-node) model. For the master + worker cluster model
> see [cluster.md](cluster.md). A worker node is identical to a
> standalone host except:
>
> - `mode: worker` in `/etc/cua-house/worker.yaml` and a `cluster:` section pointing
>   at the master
> - `vm_bind_address: 0.0.0.0` so VM ports are reachable across the VPC
> - `snapshot_cache_dir` set to a persistent XFS path (cache survives
>   worker restarts; first task per shape cold-boots and caches)
>
> In cluster mode the worker provisions a fresh VM per master-dispatched
> `AssignTask` and destroys it on task completion — there is no static
> pool to configure. Everything below (filesystem layout, OverlayFS
> task-data layout, images.yaml, docker image) applies unchanged.

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
| `{runtime_root}/` | Runtime state: per-VM disks, logs, events. Configured in the host YAML. |
| `{runtime_root}/slots/` | Per-VM directories (storage/ and logs/) |
| `{runtime_root}/events.jsonl` | Structured JSONL event log |
| `{runtime_root}/boot-patched.sh` | Auto-generated patched boot script for snapshot support |
| `{image_root}/cpu-free-YYYYMMDD.qcow2` | Versioned template qcow2. Configured per image in `images.yaml`; shape-specific savevm tags are created automatically on first cache miss. |
| `{task_data_root}/` | Task data mount point. Configured in the host YAML. |

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

The server uses `trycua/cua-qemu-windows:latest` (configurable via `docker_image` in the host YAML). This image contains QEMU with Windows support and a CUA computer-server that starts on port 5000.

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
- `runtime_root`: path for runtime state (slot directories)
- `snapshot_cache_dir`: **required** for standalone + worker. Persistent XFS path where per-shape cached qcow2s live. See [vm-image-maintenance.md](../operations/vm-image-maintenance.md) for the purge-on-version-bump SOP.
- `task_data_root`: path to task data directory (or null if not used)

Edit `images.yaml`:

- Set `template_qcow2_path` for local images (versioned path, e.g. `cpu-free-20260405.qcow2`)
- Set `enabled: true` for images you want active

## Start manually

Current deployments start `cua-house-server` manually with `setsid nohup`
rather than installing a systemd unit. Adjust paths to match the host.

Standalone example:

```bash
cd /opt/cua-house
setsid nohup uv run python -m cua_house_server.cli \
  --host-config /etc/cua-house/server.yaml \
  --image-catalog /etc/cua-house/images.yaml \
  --host 0.0.0.0 --port 8787 --mode standalone \
  </dev/null >server.log 2>&1 &
disown
```

Cluster workers use `/etc/cua-house/worker.yaml` and
`/etc/cua-house/worker.env`; see [clone-worker.md](clone-worker.md) for
the worker-specific manual start command.

## Firewall

The server listens on a single port (default 8787). All CUA and noVNC traffic is reverse-proxied through this port using host-based routing (`<service>--<lease_id>.{base_host}`).

Only port 8787 needs to be open to clients. Internal CUA ports (15000-15999) and noVNC ports (18000-18999) bind to 127.0.0.1 and should not be exposed externally.

## Task data disk provisioning

Task data is organized as `<domain>/<task>/<variant>/input/`, `reference/`, `software/`, `output/`. This matches the AgentHLE canonical bucket layout `gs://agenthle/<domain>/<task>/<variant>/`; do not insert a `task-data/` path component. For local Docker/QEMU VMs, `task_data_root` is bind-mounted into each container at `/shared/agenthle` and exposed to guests via Samba (Windows: `E:` drive; Linux: CIFS mount at `/media/user/data/agenthle`).

### Simple setup (single-node, read-write)

Mount a local writable disk at `task_data_root` (e.g., `/mnt/agenthle-task-data`) and populate it from the matching canonical GCS prefix:

```bash
gsutil -m rsync -r gs://agenthle/<domain>/<task>/<variant>/ \
  /mnt/agenthle-task-data/<domain>/<task>/<variant>/
```

### Multi-node setup (per-worker read-write disks)

Each KVM worker owns its own task-data disk. This uses more storage than
the old single-disk model, but it lets every worker refresh from GCS
independently before manual startup. Updating task data no longer requires
detaching one disk from the whole cluster. The per-worker disk is
attached `READ_WRITE` to exactly one VM so startup can sync it, but normal
worker execution mounts that disk read-only and writes only to the local
OverlayFS upper layer on `/mnt/xfs`.

```bash
# 1. Create or clone a disk for this worker.
gcloud compute disks create <node>-task-data \
    --source-snapshot=<current-task-data-snapshot> \
    --type=pd-balanced --size=400GB \
    --zone=<zone> --project=<project>

# 2. Attach the disk to exactly one worker in read-write mode.
gcloud compute instances attach-disk <node> \
    --disk=<node>-task-data --device-name=task-data --mode=rw \
    --zone=<zone> --project=<project>

# 3. Mount the data disk read-only as the lower layer and expose a
#    writable OverlayFS view at task_data_root.
sudo mkdir -p /mnt/agenthle-task-data-ro /mnt/agenthle-task-data
sudo mount -o ro,noload /dev/disk/by-id/google-task-data /mnt/agenthle-task-data-ro
sudo mkdir -p /mnt/xfs/task-data-upper /mnt/xfs/task-data-work
sudo chown $(id -u):$(id -g) /mnt/xfs/task-data-upper /mnt/xfs/task-data-work
sudo mount -t overlay overlay \
    -o lowerdir=/mnt/agenthle-task-data-ro,upperdir=/mnt/xfs/task-data-upper,workdir=/mnt/xfs/task-data-work \
    /mnt/agenthle-task-data

# 4. Start manually through the sync wrapper. It unmounts the overlay,
#    remounts the lower disk read-write, syncs GCS, remounts read-only,
#    restores the overlay, then starts the worker.
cd /opt/cua-house
./scripts/start-worker.sh
```

To persist across reboots, add to `/etc/fstab`:

```
/dev/disk/by-id/google-task-data /mnt/agenthle-task-data-ro ext4 ro,noload,nofail 0 0
overlay /mnt/agenthle-task-data overlay lowerdir=/mnt/agenthle-task-data-ro,upperdir=/mnt/xfs/task-data-upper,workdir=/mnt/xfs/task-data-work,nofail 0 0
```

Use `scripts/start-worker.sh` for the normal manual start path; it checks
the mounts, syncs `gs://agenthle` into the per-worker lower disk, returns
that disk to read-only mode, exports `/etc/cua-house/worker.env`, and
starts the worker process with `setsid nohup`.

The task-data bucket must contain task input/reference/software data only.
Do not place qcow2 exports or VM templates under `gs://agenthle`; worker
startup mirrors that bucket into each node's 400G data disk. VM images
belong under `gs://agenthle-images/templates/<image-key>/`.

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

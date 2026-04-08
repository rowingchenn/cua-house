# Host setup

How to set up a KVM host to run cua-house-server with Docker+QEMU local runtime.

## Host requirements

- Linux (tested on Ubuntu 22.04/24.04)
- Docker installed and running
- `/dev/kvm` accessible (nested virtualization enabled if running on a cloud VM)
- Minimum 8 CPUs, 32 GB RAM (for 2+ concurrent VMs with 4 vCPU / 8-16 GB each)
- Fast storage for QEMU overlays (local SSD recommended for snapshot performance)

## Filesystem layout

| Path | Purpose |
|------|---------|
| `{runtime_root}/` | Runtime state: per-VM disks, logs, events. Configured in `server.yaml`. |
| `{runtime_root}/slots/` | Per-VM directories (storage/ and logs/) |
| `{runtime_root}/events.jsonl` | Structured JSONL event log |
| `{runtime_root}/boot-patched.sh` | Auto-generated patched boot script for snapshot support |
| `{image_root}/cpu-free-YYYYMMDD.qcow2` | Versioned template qcow2 with pre-baked snapshot. Configured per image in `images.yaml`. |
| `{task_data_root}/` | Task data mount point. Configured in `server.yaml`. |

Example:

```
/home/user/agenthle-env-runtime/                          # runtime_root
/mnt/agenthle-env-images/cpu-free/cpu-free-20260406.qcow2       # Windows template
/mnt/agenthle-env-images/cpu-free-ubuntu/cpu-free-ubuntu-20260408.qcow2  # Ubuntu template
/mnt/agenthle-task-data/                                  # task_data_root
```

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

If tasks require input/reference data:

1. Mount or create a directory at `task_data_root` (e.g., `/mnt/agenthle-task-data`).
2. Organize data as `{category}/{task_tag}/{variant}/input/`, `reference/`, `software/`.
3. For VM pool mode, this directory is mounted into containers at `/shared/` and mapped as `E:` via Samba inside the guest.

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

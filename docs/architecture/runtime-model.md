# Runtime model

This document details the pre-baked qcow2 model, snapshot lifecycle, and GCP VM lifecycle used by cua-house.

## Pre-baked qcow2 model (local runtime)

### Template qcow2

Each image variant (e.g. `cpu-free`) has a versioned template qcow2 file created offline. It contains a fully installed Windows OS with CUA server and agent tooling, **plus a pre-baked QEMU internal snapshot** (`savevm cpu-free`) stored inside the file.

Location on host: configured via `template_qcow2_path` in the image catalog YAML (e.g., `/home/weichenzhang/agenthle-env-images/cpu-free/cpu-free-20260405.qcow2`).

### Per-VM disk

At server startup, each VM in the pool gets its own full copy of the template:

```bash
cp cpu-free-20260405.qcow2 {runtime_root}/slots/{vm_id}/storage/vm.qcow2
```

This is a standalone qcow2 (not a COW overlay) — the snapshot state is embedded in the file itself. Writes made during a task are isolated to each VM's private copy.

### Startup via `-loadvm` (fast path)

QEMU starts with `-loadvm cpu-free`, which restores the pre-baked snapshot directly:

- No Windows cold boot (~5 min)
- VM is responsive in ~30s (snapshot resume)

The `-loadvm` flag is injected into `boot.sh` via the `LOADVM_SNAPSHOT` container environment variable, which is set in the Docker run command by `DockerQemuRuntime._start_vm_container()`.

### Snapshot lifecycle (task revert)

Between tasks, a QMP `loadvm` reverts the VM to the clean snapshot state:

1. **Server startup**: Docker container starts → QEMU loads `-loadvm cpu-free` → CUA server is responsive (~30s).
2. **Assignment**: scheduler marks VM as READY and assigns it to a queued task.
3. **Task runs**: agent interacts with the VM. All disk writes go to the VM's private `vm.qcow2`.
4. **Completion**: task completes or times out; scheduler calls `revert_vm()`.
5. **loadvm (revert)**: QMP client sends `loadvm cpu-free` → `cont`. This restores exact VM state from the snapshot (~15-30s). CUA server is responsive again near-instantly.
6. **Repeat**: VM returns to READY state, available for the next task.

### Requirements for snapshot support

Three QEMU/Docker settings are required:

| Setting | Purpose |
|---------|---------|
| `CPU_MODEL=host` | Removes `migratable=no` from QEMU CPU config (added by dockur/windows `proc.sh` when unset). Migration/snapshot requires migratable CPU. |
| `HV=N` | Removes `hv_passthrough` Hyper-V flag, which blocks snapshot/migration. |
| Patched `boot.sh` (pflash) | dockur/windows creates UEFI pflash vars as `format=raw`, which does not support snapshots. The patched boot.sh converts pflash vars to qcow2 format at boot time. |
| Patched `boot.sh` (loadvm) | Inserts `if [ -n "$LOADVM_SNAPSHOT" ]; then BOOT_OPTS+=" -loadvm $LOADVM_SNAPSHOT"; fi` just before the QEMU launch line. |

The patched `boot.sh` is generated automatically by `DockerQemuRuntime._ensure_patched_boot_sh()`. It extracts the original from the Docker image, applies both patches, and mounts it at `/run/boot.sh:ro`.

### Storage layout (per VM)

```
{runtime_root}/slots/{vm_id}/
  storage/
    vm.qcow2            # VM's private copy of the template (pre-baked snapshot inside)
    windows.boot         # marker file
  logs/
    docker.log           # container stdout
```

### VM pool state machine

```
BOOTING ──(wait_ready)──► READY ──(task assigned)──► LEASED
                              ▲                           │
                              │                           │ (task complete)
                              │                           ▼
                            READY ◄──(loadvm)────── REVERTING
                              
BROKEN ──(auto-replace)──► BOOTING
```

## GCP VM lifecycle

### Boot disk

Two strategies, configured per image in the catalog:

- **From image** (preferred, ~14s): `gcloud compute instances create --image=<name>`. Creates the boot disk inline from a GCP image.
- **From snapshot** (fallback, ~100s): creates a disk from snapshot first, then attaches it as boot disk.

### Data disk

When `gcp_data_snapshot` is configured:

1. A new disk is created from the snapshot before VM creation.
2. The disk is attached to the VM with `auto-delete=yes`.
3. After boot, the server assigns drive letter E: to the data disk (reassigning if needed).

### Task data isolation

Both local VM pool and GCP use the same NTFS ACL-based isolation:

- **Runtime phase**: enumerate task directory, deny access via `icacls /deny User:(OI)(CI)F` to everything except `input/`, `software/`, and `output/`. Also deny sibling tasks and other categories.
- **Eval phase**: remove the deny on `reference/` so the evaluator can read it.

This approach runs as the `User` account and requires no elevation.

### Cleanup

GCP VMs are created with `auto-delete=yes` on all disks. On reset, `gcloud compute instances delete --quiet` removes the VM and all attached disks. On server startup, orphaned `cua-house-env-*` VMs are discovered and deleted.

## QMP client

The QEMU Machine Protocol client (`packages/server/src/cua_house_server/qmp/client.py`) uses `docker exec` to pipe QMP commands through `nc` inside the container.

Direct TCP port forwarding does not work reliably for QMP through Docker's proxy (connects but data does not flow). The docker exec + nc approach is the proven workaround.

QMP port 7200 is enabled inside containers via the `ARGUMENTS` environment variable in the Docker image.

Key operations:

```python
qmp = QMPClient("cua-house-env-abc123")
await qmp.load_snapshot("cpu-free")   # loadvm -> cont  (task revert)
await qmp.query_status()              # check if VM is running
await qmp.is_alive()                  # bool health check
```

# Pitfalls & Known Issues

Encountered bugs and configuration traps when running cua-house-server on KVM nodes with Docker+QEMU. Documented here so future debugging starts from the answer, not the symptom.

---

## Docker/QEMU Runtime

### 1. dockur/windows boot watchdog kills QEMU after 30s (Ubuntu/loadvm)

**Symptom**: Container logs show `"Timeout while waiting for QEMU to boot the machine!"`, QEMU gets SIGTERM'd ~30s after start. Only affects loadvm; cold boot is fine.

**Root cause**: `entry.sh` runs `( sleep 30; boot ) &`. The `boot()` function in `power.sh` checks if the serial console (`/run/shm/qemu.pty`) has output. With `-loadvm` (especially Linux guests), no serial output is produced, so the watchdog fires.

**Fix**: In patched `boot.sh`, touch `/run/shm/qemu.end` when `LOADVM_SNAPSHOT` is set. The `boot()` function checks `[ -f "$QEMU_END" ] && return 0` at the top. Must use a **delayed touch** (`sleep 10; touch ...`) because `power.sh` is sourced AFTER `boot.sh` and runs `rm -f /run/shm/qemu.*` at source time.

**Code**: `qemu.py` `_ensure_patched_boot_sh()` — Patch 3.

### 2. Second VM always fails: guest network unreachable (ARP incomplete)

**Symptom**: Starting 2+ VMs from the same snapshot — first VM works, second VM's CUA never responds. Ping from container to guest shows `Destination Host Unreachable`, ARP entry is `(incomplete)`. 100% reproducible.

**Root cause**: dockur/windows `network.sh` derives the guest VM IP from the container's Docker IP:
```
Container 172.17.0.2 → guest gets 172.30.0.2
Container 172.17.0.3 → guest gets 172.30.0.3
```

But `-loadvm` restores the snapshot's network state, so the guest always has the IP from bake time (172.30.0.2). The first container happens to assign 172.30.0.2 (match), the second assigns 172.30.0.3 (mismatch). The server tries to reach CUA at the wrong IP.

**Fix**: Set `-e VM_NET_IP=172.30.0.2` on all containers to force the same DHCP assignment. Each container has its own network namespace, so no conflict.

**Code**: `qemu.py` `_start_vm_container()`.

**Note**: If the golden qcow2 snapshot is ever re-baked on a host where the container gets a different IP, this value must match the new snapshot's guest IP.

### 3. QEMU 10 rejects `-loadvm` when pflash vars are raw format

**Symptom**: `"Snapshot does not exist in one or more devices"` error on QEMU start.

**Root cause**: QEMU 10 requires ALL writable drives to have the snapshot tag when using `-loadvm`. The dockur/windows image creates pflash UEFI vars in raw format (no snapshot support). Raw drives can't hold snapshot tags.

**Fix**: In patched `boot.sh`:
1. Convert pflash vars from raw to qcow2: `qemu-img convert -f raw -O qcow2`
2. Create an empty snapshot tag: `qemu-img snapshot -c "$LOADVM_SNAPSHOT"`

**Code**: `qemu.py` `_ensure_patched_boot_sh()` — Patch 1.

### 4. boot.sh is a sourced function, not an exec entrypoint

**Symptom**: Loadvm patch appended after `return 0` is dead code and never executes.

**Root cause**: In newer dockur/windows images, `boot.sh` ends with `return 0` (sourced as a function by `entry.sh`), not `exec qemu-system-x86_64`. Any code after `return 0` is unreachable.

**Fix**: Insert patches BEFORE `return 0`, not after.

### 5. Disk filename `data.qcow2` not `vm.qcow2`

**Symptom**: QEMU inside container can't find the disk image.

**Root cause**: dockur/windows uses `DISK_NAME=data` internally, expecting the disk at `/storage/data.qcow2`. Our code originally named it `vm.qcow2`.

**Fix**: Template copy creates `data.qcow2`.

---

## XFS / Filesystem

### 6. Reflink only works within the same XFS filesystem

**Symptom**: `cp --reflink=always` fails with `Invalid cross-device link` if source and destination are on different filesystems/partitions.

**Root cause**: XFS reflink is a block-level operation — source and dest must share the same block group. Images and runtime slots MUST be on the same XFS disk.

**Fix**: Both `{image_root}` and `{runtime_root}` must point to the same XFS mount (e.g., `/mnt/xfs/images/` and `/mnt/xfs/runtime/`). Use `--reflink=auto` (not `always`) in production code so non-XFS hosts fall back to full copy.

---

## KVM Node Provisioning

### 7. Nested virtualization requires specific CPU platform

**Symptom**: `gcloud compute instances create` fails with `--min-cpu-platform="Intel Haswell"` on n2-standard machines.

**Root cause**: n2 machines require Cascade Lake or later. Haswell is only valid for n1 machines.

**Fix**: Omit `--min-cpu-platform` for n2 machines (Cascade Lake is the default). Or use `--min-cpu-platform="Intel Cascade Lake"` explicitly.

### 8. XFS disk must be formatted with `reflink=1`

**Symptom**: `cp --reflink=always` returns `Operation not supported`.

**Root cause**: XFS reflink is off by default on older `mkfs.xfs`. Must be explicitly enabled at format time; cannot be changed after.

**Fix**: `mkfs.xfs -m reflink=1 -L agenthle-xfs /dev/disk/by-id/google-xfs`

### 9. GCS access requires explicit IAM on the SA

**Symptom**: `gsutil cp gs://agenthle-images/... .` fails with 403.

**Root cause**: The KVM node's service account (`agenthle-vm-service@...`) only had `compute.admin` + `iam.serviceAccountUser`. No storage permissions.

**Fix**: Grant `roles/storage.objectViewer` (or `objectAdmin` for upload) on the bucket:
```bash
gsutil iam ch serviceAccount:SA@PROJECT.iam.gserviceaccount.com:roles/storage.objectViewer gs://BUCKET
```

### 10. SSH via `gcloud compute ssh --command` flaky for long commands

**Symptom**: Exit code 255, connection drops, partial output.

**Root cause**: Long-running commands via `--command` often timeout or get killed by SSH keepalive.

**Fix**: SCP a script to the host, then `ssh --command='bash /tmp/script.sh'`. Keeps the SSH session short and the script runs independently.

---

## Image Baking

### 11a. Local qcow2 drifts from GCS after a re-bake

**Symptom**: New KVM node pool init fails with `Snapshot 'X' does not exist in one or more devices`, OR Linux task data staging fails with `mount.cifs: command not found` even though the fix was already added to the local template. Inspecting the qcow2 on the broken node shows an older bake timestamp than the known-good node.

**Root cause**: `_ensure_local_templates()` pulls the template from the `gcs_uri` configured in `images.yaml`. When someone re-bakes a template locally (e.g. to fix a guest-side bug like `cifs-utils`) and forgets to `gsutil cp` the result back to GCS, the local disk on one host diverges from GCS. Future nodes pull the stale GCS version. Hit in practice:

- `waa-20260408.qcow2`: uploaded to GCS as raw GCP export, baked locally on kvm-02 only. Fixed in the initial `waa` deployment (commit 07dfaf6).
- `cpu-free-ubuntu-20260408.qcow2`: first bake on 2026-04-08 19:33 (no cifs-utils) went to GCS. Re-bake on 2026-04-09 05:50 (with cifs-utils) stayed on kvm-02 only for ~18 hours.

**Fix**: Treat GCS as the **source of truth** for baked templates. After any local re-bake, immediately `gsutil cp` the new qcow2 back to the same `gcs_uri`. The updated Step 5b in `docs/operations/vm-image-maintenance.md` makes this part of the bake workflow. When debugging "works on node A, fails on node B", compare `gsutil stat` Content-Length and local `stat -c %s`: any mismatch means drift.

### 11. Snapshot guest IP is frozen at bake time

**Symptom**: After re-baking a snapshot on a different host, VMs fail to connect because the guest IP changed.

**Root cause**: `savevm` captures the full network state including DHCP lease/static IP. After `loadvm`, the guest uses the bake-time IP regardless of the new container's DHCP config.

**Fix**: Ensure `VM_NET_IP` in `_start_vm_container()` matches the IP the snapshot was baked with. If re-baking, either:
- Bake on a container with a known fixed IP (`-e VM_NET_IP=172.30.0.2`)
- Update the `VM_NET_IP` value in code after re-baking

---

## Task Data Staging

### 12. Staging runs Windows commands on Linux VMs when `os_type` is missing

**Symptom**: Task staging "succeeds" in ~0.3s but data is not mounted. Task's `start()` fails with "Permission denied" or "file not found" at `/media/user/data/agenthle/...`.

**Root cause**: Client doesn't send `os_type` in `TaskRequirement`. Server defaults to `None`, which takes the Windows staging path (PowerShell, `net use E:`, `icacls`). On a Linux VM, these commands fail silently via the CUA API. `_mount_data_linux()` is never called.

**Fix**: Server-side inference in `scheduler/core.py` `_stage_phase()`: if `os_type is None` and snapshot_name contains "ubuntu" or "linux", default to `"linux"`.

### 13. Ubuntu snapshot must have `cifs-utils` for CIFS data mount

**Symptom**: `_mount_data_linux()` runs but `mount -t cifs` fails because `mount.cifs` is not installed in the guest.

**Root cause**: The base Ubuntu image was baked without `cifs-utils`. The CIFS mount helper is not part of a standard Ubuntu install.

**Fix**: Rebake the snapshot: cold-boot → `apt install cifs-utils` → `savevm`. This only needs to be done once per image.

### 14. ext4 read-only multi-attach needs `noload` mount option

**Symptom**: `mount -o ro` fails on a GCP persistent disk attached in READ_ONLY mode to multiple VMs.

**Root cause**: ext4 tries to replay the journal on first mount, which requires write access. A disk that was previously mounted read-write has a dirty journal.

**Fix**: Mount with `mount -o ro,noload` to skip journal replay. Data integrity is guaranteed because the disk is read-only anyway.

### 15. Agents need writable task data, but multi-attach PD is read-only

**Symptom**: Agent task fails with "Permission denied" when creating `output/` under `/media/user/data/agenthle/...`. The shared task-data disk is mounted RO (required for multi-attach), so Samba/CIFS writes fail.

**Root cause**: GCP standard PDs only support multi-attach in READ_ONLY mode. To share a single data disk across multiple KVM nodes, we lose write ability. But task execution requires agents to write `output/` files.

**Fix**: Use OverlayFS on each node — shared RO disk as `lowerdir`, local XFS dir as `upperdir`. Reads pass through to the shared disk (no duplication); writes land on the local upper layer. See `docs/deployment/host-setup.md` → "Multi-node setup".

### 16. OverlayFS upper layer persists across VM reverts

**Symptom**: Re-running the same task shows stale files in `output/` from a previous run, even though the VM was reverted to a clean snapshot.

**Root cause**: VM revert only resets guest state. Output files are written through Samba/CIFS to the host's OverlayFS upper layer, which persists on disk. A re-run of the same task sees the old upper-layer content.

**Fix**: In `_stage_vm_pool()` runtime phase, `rm -rf` the output dir before the task starts. Implemented via `_reset_remote_dir()` (Windows) and `_reset_remote_dir_linux()` (Linux) in `staging.py`.

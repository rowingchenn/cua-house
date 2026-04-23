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

**Symptom**: A newly started worker fails template prewarm or the first task for a shape fails with an old guest-side bug (for example Linux task data staging fails with `mount.cifs: command not found`) even though the fix was already added to the local template. Inspecting the qcow2 on the broken node shows an older bake timestamp than the known-good node.

**Root cause**: `prewarm_templates()` at worker startup pulls the template from the `gcs_uri` configured in `images.yaml`. When someone re-bakes a template locally (e.g. to fix a guest-side bug like `cifs-utils`) and forgets to `gsutil cp` the result back to GCS, the local disk on one host diverges from GCS. Future nodes pull the stale GCS version. Hit in practice:

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

### 12. Staging runs Windows commands on Linux VMs (fixed)

**Status**: Fixed. `os_family` is now a required field in `images.yaml` (per image). The server reads it from the catalog — no client-side `os_type` field, no string-match inference.

### 12a. Reserved dockur ports cannot be used in `published_ports`

**Symptom**: `load_image_catalog` raises `ValueError: port N is reserved by dockur`.

**Root cause**: Ports 5900 (VNC), 5700 (WSS), 7100 (monitor), 8004 (WSD), 8006 (noVNC web) are used by dockur's own services inside the container. The bridge-mode iptables DNAT rule in `network.sh` excludes these ports from guest forwarding. If you declare one of them in `published_ports`, guest traffic would go to the container service instead of the VM.

**Fix**: Choose a different guest port. If your guest service runs on a reserved port, reconfigure it to use an unreserved one.

### 13. Ubuntu snapshot must have `cifs-utils` for CIFS data mount

**Symptom**: `_mount_data_linux()` runs but `mount -t cifs` fails because `mount.cifs` is not installed in the guest.

**Root cause**: The base Ubuntu image was baked without `cifs-utils`. The CIFS mount helper is not part of a standard Ubuntu install.

**Fix**: Rebake the snapshot: cold-boot → `apt install cifs-utils` → `savevm`. This only needs to be done once per image.

### 13a. Linux CIFS mount must use the guest gateway IP

**Symptom**: Linux task staging reports `task_data_injected` and
`stage-runtime` returns 200, but the guest path
`/media/user/data/agenthle/...` is missing. Running the mount manually shows
`mount(2) system call failed: No route to host` for
`//host.lan/Data/agenthle`.

**Root cause**: `host.lan` resolves in userspace, and TCP probes to port 445 can
work, but the kernel CIFS mount path can still fail against that hostname in
the nested guest. The Samba service is reachable at the guest gateway
`172.30.0.1`.

**Fix**: Use `//172.30.0.1/Data/agenthle` for Linux CIFS mounts and make the
mount helper verify `mountpoint -q` after the mount command. Otherwise the
shell can end with a later successful command and staging appears successful
even though the mount failed.

### 14. ext4 read-only task-data lower mount needs `noload`

**Symptom**: `mount -o ro` fails on a worker's task-data disk after it
was temporarily mounted read-write for GCS sync.

**Root cause**: ext4 tries to replay the journal on first read-only
mount, which requires write access. A disk that was previously mounted
read-write for sync can have a dirty journal.

**Fix**: Mount the lower disk with `mount -o ro,noload` after sync.
`scripts/start-worker.sh` does this before restoring the OverlayFS view.

### 15. Agents need writable task data, but the lower PD is read-only

**Symptom**: Agent task fails with "Permission denied" when creating
`output/` under `/media/user/data/agenthle/...`.

**Root cause**: In cluster mode the per-worker task-data PD is normally
mounted read-only at `/mnt/agenthle-task-data-ro`. Task execution must
not mutate that lower disk; it exists only to mirror `gs://agenthle`.

**Fix**: Use OverlayFS on each node: the per-worker RO mount is
`lowerdir`, and `/mnt/xfs/task-data-upper` is `upperdir`. Reads pass
through to the synced data disk; writes land on local XFS. See
`docs/deployment/host-setup.md` -> "Multi-node setup".

### 16. Output must be lease-scoped, not task-scoped

**Symptom**: Two concurrent leases for the same task on one worker see
or overwrite each other's `output/` files. A later retry can also see
stale output from an earlier run.

**Root cause**: The task-data source path (`source_relpath`) is shared
by every lease of that task. If writable output is exposed as
`/data-store/{rel}/output` or as a task-scoped host path, concurrent
leases collide. Guest VM deletion only removes the VM slot/container; it
does not imply that a shared task-data path is safe to delete.

**Fix**: Runtime staging exposes `input/`, `software/`, and `reference/`
from the read-only task-data mount, but exposes `output/` from a
lease-scoped VM slot path:
`/storage/cua-house-lease-output/<lease_id>/<source_relpath>/output`.
The guest still sees the conventional
`agenthle/<source_relpath>/output` path through Samba, but two leases get
different backing directories. `destroy_vm()` removes the slot directory,
so the lease output is cleaned up with the VM.

Historical files under `/mnt/xfs/task-data-upper` can still exist from
older runs or manual debug. Do not rely on VM deletion to remove those;
clean them only when no leases are running on the worker.

---

## Observability / False Signals

### 17. Missing savevm tag in qcow2 masquerades as a 15-minute cold-boot hang

**Symptom**: `slot_ready_timeout` after the full `ready_timeout_s` (default 900s) for every VM of a specific `snapshot_name`. events.jsonl shows the full chain `vm_starting -> slot_vm_ip_detected -> slot_computer_server_wait_started -> slot_ready_timeout` with `observed_boot_markers: ["computer_server_wait_started", "vm_ip_detected"]`, which *looks* like Windows booted but CUA never came up. Other images or shapes work on the same worker.

**Root cause**: Two independent bugs stacked:

1. The template qcow2 has **no savevm snapshot**. This happens when a re-bake accidentally overwrote the qcow2 *after* the savevm tag was created (e.g. `qemu-img convert` drops snapshots unless `-s` is passed) or when someone uploaded a pre-bake copy to GCS. Verify with `qemu-img snapshot -l /path/to/template.qcow2`: the list should contain a row with `TAG = <image_key>`. If the listing is empty, the tag is gone.

2. QEMU `-loadvm <tag>` with a missing tag **exits non-zero at startup**. `/run/entry.sh` (dockur) then exits. The cua-house wrapper `/entry.sh` spawned `/run/entry.sh` in the background as `VM_PID=$!` but never checks whether that child is still alive — it just loops on `curl 172.30.0.2:5000/status` until `ready_timeout_s`. Meanwhile the `slot_vm_ip_detected` event is emitted the moment the wrapper's internal VM_IP detection runs — but that detection is `ps aux | grep dnsmasq | grep -oP '(?<=--dhcp-range=)[0-9.]+'`, i.e. it reads the **dnsmasq command line**, not an actual DHCP lease. dnsmasq starts long before QEMU does, so `vm_ip_detected` fires even for a container whose VM never booted. The event is a **false positive** and cannot be used as evidence that the guest OS actually reached the network stack.

**Diagnostic shortcut**: On a failed slot, `docker exec <container> ps auxf` and look for `qemu-system-x86_64`. If absent, the VM never actually started; do not spend time debugging autologon or in-guest services. Check `qemu-img snapshot -l` on the slot qcow2 if this is a cache-hit path.

**Fix / current status**: This pitfall is now largely mitigated by the automatic savevm behavior introduced with shape-based snapshot tags.

In **cluster mode**, snapshot tags are shape-based (e.g., `4vcpu-8gb-64gb`) and created automatically by the server on first cache miss — the template qcow2 no longer needs a pre-baked savevm tag. When a VM of a new shape is first requested, the server cold-boots it, waits for readiness, then runs `savevm` with the shape-derived tag. Subsequent VMs of the same shape load from that snapshot. The `version` field in `images.yaml` invalidates the cache, forcing a fresh cold boot + savevm cycle.

In **standalone mode**, the template still needs a savevm tag matching the shape stem (i.e., the image key). If missing, the original symptom applies. Verify with `qemu-img snapshot -l /path/to/template.qcow2`.

Remaining improvements to consider:
- Wrapper `/entry.sh` monitors `VM_PID` and exits with an error if the QEMU backgrounded process dies, so containers fail fast instead of looping forever.
- Derive `slot_vm_ip_detected` from an actual DHCP lease (e.g. `/var/lib/misc/dnsmasq.leases`) or from a successful ARP probe, not from the dnsmasq command line.

### 18. Patched boot.sh aborted with `LOADVM_SNAPSHOT: unbound variable` under `set -u`

**Symptom**: Cold-boot bake containers (no `-e LOADVM_SNAPSHOT`) exited almost immediately with `boot.sh: line NNN: LOADVM_SNAPSHOT: unbound variable`; no QEMU process, but wrapper `/entry.sh` kept looping "Waiting for Cua computer-server to be ready".

**Root cause**: `/run/entry.sh` runs under `set -Eeuo pipefail`. The cua-house patches wrote `[ -n "$LOADVM_SNAPSHOT" ]` with no default, so the nounset flag killed the script whenever the env var was not defined.

**Workaround (before fix)**: Pass `-e LOADVM_SNAPSHOT=` (empty value). Documented in vm-image-maintenance.md Step 4 but easy to forget.

**Fix**: All three patch snippets in `qemu.py _ensure_patched_boot_sh()` now use `${LOADVM_SNAPSHOT:-}` so an unset env var is treated as empty instead of crashing the script. Cached `boot-patched.sh` files must be removed so the server regenerates them with the fixed template.

---

## Cluster mode deployment

### 19. Master VM in the wrong VPC

**Symptom**: Worker WebSocket connection attempt to master internal IP times out (not refused). `nc -zv 10.x.x.x 22` from worker succeeds, `nc -zv 10.x.x.x 8787` hangs for the full timeout. Master process is healthy and listening on `0.0.0.0:8787` locally.

**Root cause**: Master VM created on `default` VPC while workers are on `agenthle-vpc`. Both subnets happen to use `10.128.0.0/20` so the internal IP *looks* plausible, but the two VPCs are isolated.

**Fix**: `gcloud compute instances describe <master> --format='value(networkInterfaces[0].network)'`. Must match worker VPC. Recreate with `--network=agenthle-vpc --subnet=agenthle-vpc`.

### 20. Master VM missing `agenthle` target tag

**Symptom**: Same as above — worker-to-master 8787 times out. Master is in the right VPC, TCP/22 works both directions, OS firewall is disabled. The `agenthle-allow-env-server` firewall rule exists and appears in `gcloud compute firewall-rules list`.

**Root cause**: `agenthle-allow-env-server` (and the equivalent for 16000–18999) has `targetTags: [agenthle]`. Fresh VMs created with `gcloud compute instances create` don't apply the tag automatically.

**Fix**: `gcloud compute instances add-tags <master> --tags=agenthle --zone=<zone>`. Repeat for every new worker VM.

### 21. `vm_bind_address` defaults to `127.0.0.1`

**Symptom**: Worker registers with master, VM boots, `TaskBound` returns URLs like `http://worker.ip:16000`. Client hits 16000 → "Connection refused" (not timeout). On the worker, `docker port <container>` shows `5000/tcp -> 127.0.0.1:16000`.

**Root cause**: Docker `-p` flag in `_start_vm_container` binds to `self.config.vm_bind_address` (default `127.0.0.1`). In standalone mode this is intentional — clients reach VMs only through master's reverse proxy on the same host. In cluster mode clients connect directly to the worker across the VPC, so the loopback binding blocks them.

**Fix**: Set `vm_bind_address: 0.0.0.0` in `/etc/cua-house/worker.yaml`.
Worker restart re-creates containers with the new binding. Network-level
access control is delegated to VPC firewall rules
(`agenthle-allow-vm-ports`, 10.0.0.0/8).

### 22. `task_data_root` unwritable by the worker user

**Symptom**: Worker fails startup with `RuntimeError: worker mode: task_data_root /mnt/agenthle-task-data is not writable by current user`. Or the check is bypassed and `stage-runtime` crashes mid-task with `Permission denied` on the first write.

**Root cause**: OverlayFS upper layer (`/mnt/xfs/task-data-upper`) and merged view are commonly created `root:root` during host provisioning. The cua-house process runs as an unprivileged user.

**Fix**: recursively chown only the OverlayFS upper/work dirs:
`sudo chown -R $(id -un):$(id -gn) /mnt/xfs/task-data-upper /mnt/xfs/task-data-work`.
Then chown the merged mount point itself without recursion:
`sudo chown $(id -un):$(id -gn) /mnt/agenthle-task-data`. The worker mode startup check catches this before the first task runs.

### 23. Worker WS reconnect backoff after master restart

**Symptom**: After `/var/log/cua-house/master.log` shows a new `INFO: Uvicorn running...`,
the workers' `/var/log/cua-house/worker.log` keeps printing
`Worker WS disconnected: received 1012 (service restart)` and
`Connect call failed (<master host>, 8787)` for ~30-60s before
recovering. `/v1/cluster/workers` on master returns `[]` during the
window.

**Behavior**: This is normal exponential backoff. Two back-to-back master restarts compound the backoff. No action needed — each worker's reconnect supervisor waits `min(reconnect_min_backoff_s * 2^n + jitter, reconnect_max_backoff_s)` between attempts. Master has no persistent pool state to restore (ephemeral-VM model); workers just start accepting new `AssignTask` messages as they arrive.

### 24. Task stuck at state=ready on master while worker is destroying the VM

**Symptom**: Client successfully called `POST /v1/leases/{id}/complete` through master's lease proxy (HTTP 200). Master's `/v1/tasks/{id}` still returns `state: ready` for several seconds.

**Root cause**: This is expected. Master's task view is a projection that updates in three places only:

1. `ClusterDispatcher.submit_batch` (QUEUED)
2. `_try_place` after `TaskBound` arrives (READY)
3. `handle_task_completed` after the worker emits `TaskCompleted` over WS

Between READY and the worker finishing `destroy_vm` (typically a few seconds: `docker rm -f` + `shutil.rmtree` of the slot), master has no reason to touch the state. The worker's local scheduler owns the LEASED / RESETTING transitions during that window.

**If you need authoritative real-time status**: poll the worker's `GET /v1/leases/{id}/...` (not implemented but trivial to add), or read the worker's `events.jsonl` directly. For normal operation wait for the `TaskCompleted → state=completed` transition — it arrives within ~1s of revert finishing on the worker.

### 25. Cloned worker inherits stale `runtime_root/slots/*` from the source snapshot

**Symptom**: A freshly cloned worker starts successfully, registers with master, accepts an `AssignTask`, but the new VM's storage dir already exists on disk (`EEXIST`-style failures from `_prepare_vm`) or — worse — `docker run` succeeds against a zombie qcow2 from the source host with an unpredictable guest filesystem.

**Root cause**: The GCE boot-disk snapshot `scripts/clone-worker.sh` uses is taken **live** from a running source worker (kvm02). If that worker had any in-flight slots at snapshot time, the slot directories under `/mnt/xfs/runtime-cluster/slots/` are frozen into the snapshot. `cleanup_orphaned_state()` on the cloned worker kills docker containers with matching names (safe), but it does **not** touch the `slots/` directories — those are pure host filesystem state.

**Fix**: Done in `scripts/_clone-worker-bootstrap.sh` — the post-SSH bootstrap block unconditionally `rm -rf`s `/mnt/xfs/runtime-cluster/slots` and legacy checkout runtime slots before the worker is started manually. If you run a manual clone without the bootstrap script, you **must** perform this cleanup yourself or the first `AssignTask` will hit surprising failures.

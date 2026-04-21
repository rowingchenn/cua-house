# Runtime model

Each `provision_vm(image, vcpus, memory_gb, disk_gb)` call returns a
fresh `VMHandle` for one task's lifetime. When the task completes
(client `/complete`, master `ReleaseLease`, or lease expiry), the
scheduler calls `destroy_vm(handle)`, which tears down the container
and reclaims ports + slot disk. There is no READY pool and no revert
path — VMs are one-shot.

What gives us fast subsequent starts is a **per-worker snapshot cache**:
the first task of a given `(image, version, vcpus, memory, disk)`
shape cold-boots for ~5 min and QMP `savevm`s the boot state into the
slot qcow2, which the worker reflinks into the cache. Every
subsequent task of the same shape reflinks the cached qcow2 into a new
slot and starts QEMU with `-loadvm <shape-tag>`, ready in ~30 s.

## Provision path

```
provision_vm(image, vcpus, memory_gb, disk_gb=None):
    shape_tag = f"{vcpus}vcpu-{memory_gb}gb-{disk_gb}gb"
    cached = snapshot_cache.lookup(CacheKey(image, version, vcpus, memory, disk))

    if cached is not None:                    # ── cache hit (~30 s)
        reflink cached → slot.qcow2
        docker run -e LOADVM_SNAPSHOT=shape_tag ...
        wait_ready; qmp connected
        handle.from_cache = True

    else:                                     # ── cache miss (~5 min)
        reflink template → slot.qcow2
        docker run -e LOADVM_SNAPSHOT= ...    # cold boot
        wait_ready; qmp connected
        qmp.savevm(shape_tag)                 # creates the shape tag
        snapshot_cache.write(key, slot.qcow2) # reflinks into cache
        handle.from_cache = False

    return handle  # caller owns it; must pair with destroy_vm
```

### Template qcow2

Each image variant (e.g. `cpu-free`) has a versioned template qcow2
created offline. It holds a clean, fully-installed OS with CUA server
and agent tooling — **no pre-baked snapshot tags**. Workers prewarm
all enabled templates in parallel from GCS at startup before joining
the cluster; pull failures fail-fast the process (systemd / docker
restarts).

Location on host: configured via `template_qcow2_path` in the image
catalog YAML (typically `/mnt/xfs/images/<image>/<image>-<date>.qcow2`).

### Per-task slot

Each `provision_vm` call creates `{runtime_root}/slots/{vm_id}/` with
its own `storage/vm.qcow2` (reflink of either cached qcow2 or
template). Writes during the task are isolated to that slot. On
`destroy_vm` the whole slot directory is removed.

### Snapshot cache layout

```
{snapshot_cache_dir}/
  cpu-free/v20260419/
    4vcpu-8gb-64gb.qcow2      # reflinked from a past cold-boot + savevm
    4vcpu-8gb-64gb.json       # sidecar: qemu_fingerprint, shape metadata, mtime
    4vcpu-16gb-64gb.qcow2     # different shape on same image → separate entry
  cpu-free-ubuntu/v20260419/
    4vcpu-8gb-64gb.qcow2
```

`snapshot_cache_dir` is a **required** config field for worker and
standalone modes — templates re-pulled from GCS every restart defeats
the point of caching. Must be on a filesystem that supports reflink
(XFS or modern btrfs).

Cache invalidation:

| Trigger | Behavior |
|---|---|
| Image version bump | Operator purges cache per [image-version-bump SOP](../operations/vm-image-maintenance.md#image-version-bump-sop) |
| QEMU / docker upgrade | `qemu_fingerprint` sidecar mismatches running binary → `sweep_on_startup` evicts |
| Cache write failure | Non-fatal: task proceeds, next same-shape task cold-boots again |

## Snapshot requirements

QEMU + Docker must be configured so `savevm` works inside the
container. The following are set automatically by
`DockerQemuRuntime._start_vm_container` and `_ensure_patched_boot_sh`:

| Setting | Why |
|---|---|
| `CPU_MODEL=host` | Strips `migratable=no` from QEMU CPU config (added by dockur's `proc.sh` when unset). Migration / snapshot requires migratable CPU. |
| `HV=N` | Removes the `hv_passthrough` Hyper-V flag which blocks snapshots. |
| Patched `boot.sh` (pflash) | dockur writes UEFI pflash vars as `format=raw`, which can't carry snapshots. Patch converts pflash to qcow2 format at boot and creates the shape-tag inside pflash so `-loadvm` can start. |
| Patched `boot.sh` (loadvm) | Inserts `if [ -n "$LOADVM_SNAPSHOT" ]; then BOOT_OPTS+=" -loadvm $LOADVM_SNAPSHOT"; fi` so cache-hit containers resume from the cached snapshot. |
| Hairpin NAT rule | `iptables -t nat -A POSTROUTING -d 172.30.0.2/32 -o docker -j MASQUERADE`: fixes the return-path bug where port-mapped client traffic reaches the guest but replies can't route back because the guest sees the source IP in Docker's hidden 172.17/16 subnet. |

The patched `boot.sh` is generated once per worker process and mounted
into each container at `/run/boot.sh:ro`.

## Per-VM storage layout

```
{runtime_root}/slots/{vm_id}/
  storage/
    vm.qcow2      # slot disk (reflink of template or cached qcow2)
  logs/
    docker.log    # container stdout
```

Removed on `destroy_vm`.

## GCP VM lifecycle

Identical semantic shape (`provision_vm` → bind → `destroy_vm`), no
snapshot cache — GCP VMs are disposable by design. Boot disk strategy:

| Strategy | Latency | Configured via |
|---|---|---|
| From image (preferred) | ~14 s | `gcp.boot_image` |
| From snapshot (fallback) | ~100 s | `gcp.boot_snapshot` |

When `gcp_data_snapshot` is set, a data disk is attached per VM and
auto-deleted on teardown. On boot the server reassigns drive letter
`E:` to the data disk.

Orphaned `cua-house-env-*` VMs from previous crashes are discovered
and deleted by `GCPVMRuntime.cleanup_orphaned_state` at startup.

## Task data isolation

`/data-store:ro` mount + symlink injection: every container gets
`/data-store` mounted read-only over the shared task-data tree. At
staging time the scheduler selectively symlinks the task's
`input/`, `software/`, and `reference/` (eval phase only) directories
into the container's Samba-served path (`/tmp/smb/agenthle/{rel}/`).
The guest sees only those directories — anything not symlinked simply
doesn't exist from the guest's perspective (physical isolation, no
NTFS ACL needed).

`remote_output_dir` is a real writable directory inside the Samba
share (not a symlink), and the writes survive `destroy_vm` by design
(the output is a mount point, not part of the slot qcow2).

## QMP client

`packages/server/src/cua_house_server/qmp/client.py` uses `docker exec`
to pipe QMP commands through `nc` inside the container. Direct TCP
port forwarding does not work reliably for QMP through Docker's proxy
(connects but data does not flow); docker exec + nc is the proven
workaround. QMP port 7200 is enabled inside containers via the
`ARGUMENTS` environment variable.

Used operations:

```python
qmp = QMPClient("cua-house-env-abc123")
await qmp.save_snapshot(shape_tag, timeout=300)  # savevm <tag> after cold boot
await qmp.query_status()                          # health probe
await qmp.is_alive()                              # bool
```

`load_snapshot` is **not** called from runtime code any more — cache
hits use the CLI `-loadvm` flag at container start, not QMP.

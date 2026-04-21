#!/bin/bash
# Remote bootstrap block executed on a fresh worker node via
# `gcloud compute ssh`. This script runs AS THE LOGIN USER (not root)
# and uses sudo for everything that needs privilege.
#
# The calling script (scripts/clone-worker.sh) scp's this file to the
# target as /tmp/clone-worker-bootstrap.sh and invokes it with no args.
# All required config has already been scp'd into /tmp and is installed
# into /etc/cua-house/ below.
#
# The block is idempotent - re-running it on an already-provisioned
# worker should be a no-op. This matters because the clone script may
# need to retry individual phases during debugging.
#
# See docs/deployment/clone-worker.md for the full flow.
set -euo pipefail

log() { printf '[bootstrap %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# ---------- 1. XFS disk format (idempotent) ----------
XFS_DEV="/dev/disk/by-id/google-xfs"
if [[ ! -e "${XFS_DEV}" ]]; then
    log "FATAL: ${XFS_DEV} not present. Check that the instance was created with --disk=device-name=xfs."
    exit 1
fi
if sudo blkid "${XFS_DEV}" >/dev/null 2>&1; then
    EXISTING_LABEL=$(sudo blkid -o value -s LABEL "${XFS_DEV}" || echo "")
    log "XFS disk already formatted (label=${EXISTING_LABEL:-<none>})"
else
    NEW_LABEL="${XFS_LABEL:-agenthle-xfs}"
    log "formatting ${XFS_DEV} as XFS with label ${NEW_LABEL}"
    sudo mkfs.xfs -f -L "${NEW_LABEL}" "${XFS_DEV}"
fi

# ---------- 2. fstab entries (idempotent) ----------
# The XFS mount uses LABEL= so mkfs.xfs above picks it up regardless of
# what the cloned fstab had. task-data-ro is the shared RO PD, overlay
# is the merged view cua-house reads at /mnt/agenthle-task-data.
ensure_fstab() {
    local line="$1"
    if ! grep -qxF "${line}" /etc/fstab; then
        log "adding fstab line: ${line}"
        echo "${line}" | sudo tee -a /etc/fstab >/dev/null
    fi
}

ensure_fstab 'LABEL=agenthle-xfs /mnt/xfs xfs discard,defaults,nofail 0 2'
ensure_fstab '/dev/disk/by-id/google-task-data /mnt/agenthle-task-data-ro ext4 ro,noload,nofail 0 0'
ensure_fstab 'overlay /mnt/agenthle-task-data overlay lowerdir=/mnt/agenthle-task-data-ro,upperdir=/mnt/xfs/task-data-upper,workdir=/mnt/xfs/task-data-work,nofail 0 0'

sudo mkdir -p /mnt/xfs /mnt/agenthle-task-data-ro /mnt/agenthle-task-data

# Mount each target individually so "already mounted" on a boot-time
# fstab auto-mount doesn't kill the whole block. `mount -a` returns
# non-zero if any single mount fails, which is fragile under set -e.
mount_if_unmounted() {
    local target="$1"
    if mountpoint -q "${target}"; then
        log "${target} already mounted"
    else
        log "mounting ${target}"
        sudo mount "${target}"
    fi
}

mount_if_unmounted /mnt/xfs
mount_if_unmounted /mnt/agenthle-task-data-ro

# overlayfs upper + work dirs must exist on XFS before the overlay can
# mount. Create them NOW, between xfs mount and overlay mount.
#
# /mnt/xfs/images also has to exist because /home/weichenzhang/agenthle-env-images
# is a symlink to it (baked into the cloned boot disk). The worker's
# worker startup prewarm will auto-populate it from GCS; we only need
# the dir to exist so the symlink resolves.
sudo mkdir -p /mnt/xfs/task-data-upper /mnt/xfs/task-data-work \
              /mnt/xfs/runtime-cluster /mnt/xfs/images

mount_if_unmounted /mnt/agenthle-task-data

# ---------- 3. Ownership (worker user needs write on task-data + xfs) ----------
log "chown /mnt/xfs + task-data layers to weichenzhang"
sudo chown -R weichenzhang:weichenzhang /mnt/xfs
sudo chown weichenzhang:weichenzhang /mnt/agenthle-task-data || true

# ---------- 4. Stale slot cleanup ----------
# A cloned boot disk may carry slot dirs from the source node. Their
# qcow2 overlays reference vm_ids the new worker would reuse after
# cleanup_orphaned_state() kills the containers - wiping the dirs now
# is safer than trusting docker's container removal to cascade.
log "wiping stale runtime-cluster slots"
sudo rm -rf /mnt/xfs/runtime-cluster/slots
# legacy home-dir runtime from the baked standalone config - also wipe
# if present so it doesn't confuse a poking operator.
if [[ -d /home/weichenzhang/cua-house-mnc/runtime/slots ]]; then
    sudo -u weichenzhang rm -rf /home/weichenzhang/cua-house-mnc/runtime/slots || true
fi

# ---------- 5. Kill any stale nohup cua-house processes ----------
# The boot snapshot was taken while kvm02 was running. pkill here is
# idempotent - if there's nothing matching, it returns non-zero which
# we swallow.
log "killing stale cua_house_server processes (if any)"
sudo pkill -9 -f cua_house_server.cli || true

# ---------- 6. uv sync on the baked repo ----------
# The boot snapshot carries /home/weichenzhang/cua-house-mnc at whatever
# state the source node had. If it's a git clone, try to pull latest;
# if it was scp'd in (as kvm02 was during the initial cluster deploy),
# there's no git remote and we just uv sync what's there. Either way
# uv sync is cheap when nothing changed.
log "uv sync in /home/weichenzhang/cua-house-mnc (git pull if possible)"
sudo -u weichenzhang bash -c '
    set -e
    export PATH=$HOME/.local/bin:$PATH
    cd /home/weichenzhang/cua-house-mnc
    if [[ -d .git ]]; then
        git fetch --quiet origin 2>&1 | tail -3 || true
        git pull --ff-only --quiet 2>&1 | tail -3 || \
            echo "[bootstrap] git pull failed; continuing with baked repo state"
    else
        echo "[bootstrap] not a git repo - using baked source tree as-is"
    fi
    uv sync 2>&1 | tail -3
'

# ---------- 7. Install config files ----------
# clone-worker.sh already scp'd the rendered yaml + env into /tmp.
# Move them into place with the right perms here so secrets don't pass
# through a world-readable scp landing dir.
sudo mkdir -p /etc/cua-house
for f in worker.yaml images.yaml; do
    if [[ -f /tmp/${f} ]]; then
        sudo install -m 0644 /tmp/${f} /etc/cua-house/${f}
        rm -f /tmp/${f}
    fi
done
if [[ -f /tmp/worker.env ]]; then
    sudo install -m 0600 -o root -g root /tmp/worker.env /etc/cua-house/worker.env
    rm -f /tmp/worker.env
fi

# ---------- 8. Dry-run config validation ----------
# Use the --print-register-frame path to catch typos before the operator
# starts the worker manually.
log "validating config via --print-register-frame"
sudo -u weichenzhang bash -c '
    set -e
    export PATH=$HOME/.local/bin:$PATH
    cd /home/weichenzhang/cua-house-mnc
    # worker.env holds CUA_HOUSE_CLUSTER_JOIN_TOKEN; export it for the
    # dry run so the token-provenance log fires on the right code path.
    if [[ -r /etc/cua-house/worker.env ]]; then
        set -a
        # shellcheck source=/dev/null
        source <(sudo cat /etc/cua-house/worker.env)
        set +a
    fi
    uv run python -m cua_house_server.cli \
        --mode worker \
        --print-register-frame \
        --host-config /etc/cua-house/worker.yaml \
        --image-catalog /etc/cua-house/images.yaml \
        > /tmp/register-frame.json
    head -4 /tmp/register-frame.json
    echo "... (full frame at /tmp/register-frame.json)"
'

log "bootstrap complete; worker was not started"
log "manual start:"
log "  cd /home/weichenzhang/cua-house-mnc"
log "  set -a; source <(sudo cat /etc/cua-house/worker.env); set +a"
log "  setsid nohup uv run python -m cua_house_server.cli --host-config /etc/cua-house/worker.yaml --image-catalog /etc/cua-house/images.yaml --host 0.0.0.0 --port 8787 --mode worker </dev/null >worker.log 2>&1 &"
log "  disown"

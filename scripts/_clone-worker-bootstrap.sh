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
WORKER_USER="${CUA_HOUSE_WORKER_USER:-$(id -un)}"
WORKER_GROUP="$(id -gn "${WORKER_USER}")"
WORKER_HOME="$(getent passwd "${WORKER_USER}" | cut -d: -f6)"
SHARED_GROUP="${CUA_HOUSE_SHARED_GROUP:-cua-house}"
REPO_DIR="${CUA_HOUSE_REPO_DIR:-/opt/cua-house}"

log "ensuring shared group ${SHARED_GROUP}"
sudo groupadd -f "${SHARED_GROUP}"
sudo usermod -aG "${SHARED_GROUP}" "${WORKER_USER}" || true

if [[ ! -d "${REPO_DIR}" ]]; then
    for legacy in "${WORKER_HOME}/cua-house-mnc" "${WORKER_HOME}/cua-house"; do
        if [[ -d "${legacy}" ]]; then
            log "migrating legacy checkout ${legacy} -> ${REPO_DIR}"
            sudo mkdir -p "$(dirname "${REPO_DIR}")"
            sudo cp -a "${legacy}" "${REPO_DIR}"
            break
        fi
    done
fi
if [[ ! -d "${REPO_DIR}" ]]; then
    log "FATAL: ${REPO_DIR} not found and no legacy checkout found under ${WORKER_HOME}"
    exit 1
fi
sudo chown -R "${WORKER_USER}:${SHARED_GROUP}" "${REPO_DIR}"
sudo chmod -R g+rwX "${REPO_DIR}"
sudo mkdir -p /var/log/cua-house
sudo chown -R "${WORKER_USER}:${SHARED_GROUP}" /var/log/cua-house
sudo chmod -R g+rwX /var/log/cua-house

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
# what the cloned fstab had. task-data is a per-worker PD normally
# mounted read-only; OverlayFS gives the worker a writable merged view.
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

TASK_DATA_DEV="/dev/disk/by-id/google-task-data"
if [[ ! -e "${TASK_DATA_DEV}" ]]; then
    log "FATAL: ${TASK_DATA_DEV} not present. Check that the instance was created with --disk=device-name=task-data."
    exit 1
fi
if sudo blkid "${TASK_DATA_DEV}" >/dev/null 2>&1; then
    log "task-data disk already formatted"
else
    log "formatting ${TASK_DATA_DEV} as ext4"
    sudo mkfs.ext4 -F "${TASK_DATA_DEV}"
fi

# /mnt/xfs/images is where workers prewarm qcow2 templates from GCS.
sudo mkdir -p /mnt/xfs/task-data-upper /mnt/xfs/task-data-work \
              /mnt/xfs/runtime-cluster /mnt/xfs/images

mount_if_unmounted /mnt/agenthle-task-data-ro
mount_if_unmounted /mnt/agenthle-task-data

# ---------- 3. Ownership (worker user needs write on task-data + xfs) ----------
log "chown /mnt/xfs + overlay task-data to ${WORKER_USER}"
sudo chown -R "${WORKER_USER}:${SHARED_GROUP}" /mnt/xfs
sudo chmod -R g+rwX /mnt/xfs
sudo chown "${WORKER_USER}:${SHARED_GROUP}" /mnt/agenthle-task-data || true

# ---------- 4. Stale slot cleanup ----------
# A cloned boot disk may carry slot dirs from the source node. Their
# qcow2 overlays reference vm_ids the new worker would reuse after
# cleanup_orphaned_state() kills the containers - wiping the dirs now
# is safer than trusting docker's container removal to cascade.
log "wiping stale runtime-cluster slots"
sudo rm -rf /mnt/xfs/runtime-cluster/slots
# legacy home-dir runtime from the baked standalone config - also wipe
# if present so it doesn't confuse a poking operator.
for legacy_slots in "${REPO_DIR}/runtime/slots" "${WORKER_HOME}/cua-house-mnc/runtime/slots" "${WORKER_HOME}/cua-house/runtime/slots"; do
    if [[ -d "${legacy_slots}" ]]; then
        sudo -u "${WORKER_USER}" rm -rf "${legacy_slots}" || true
    fi
done

# ---------- 5. Kill any stale nohup cua-house processes ----------
# The boot snapshot was taken while kvm02 was running. pkill here is
# idempotent - if there's nothing matching, it returns non-zero which
# we swallow.
log "killing stale cua_house_server processes (if any)"
pgrep -f "[c]ua_house_server.cli" | xargs -r sudo kill -9 || true

# ---------- 6. uv sync on the baked repo ----------
# The boot snapshot carries the worker checkout at whatever
# state the source node had. If it's a git clone, try to pull latest;
# if it was scp'd in (as kvm02 was during the initial cluster deploy),
# there's no git remote and we just uv sync what's there. Either way
# uv sync is cheap when nothing changed.
log "uv sync in ${REPO_DIR} (git pull if possible)"
sudo -u "${WORKER_USER}" env REPO_DIR="${REPO_DIR}" bash -c '
    set -e
    export PATH=$HOME/.local/bin:$PATH
    cd "${REPO_DIR}"
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
if [[ -f /tmp/start-worker.sh ]]; then
    sudo mkdir -p "${REPO_DIR}/scripts"
    sudo install -m 0755 -o "${WORKER_USER}" -g "${SHARED_GROUP}" /tmp/start-worker.sh \
        "${REPO_DIR}/scripts/start-worker.sh"
    rm -f /tmp/start-worker.sh
fi
if [[ -f /tmp/worker.env ]]; then
    sudo install -m 0600 -o root -g root /tmp/worker.env /etc/cua-house/worker.env
    rm -f /tmp/worker.env
fi

# ---------- 8. Dry-run config validation ----------
# Use the --print-register-frame path to catch typos before the operator
# starts the worker manually.
log "validating config via --print-register-frame"
sudo -u "${WORKER_USER}" env REPO_DIR="${REPO_DIR}" bash -c '
    set -e
    export PATH=$HOME/.local/bin:$PATH
    cd "${REPO_DIR}"
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
log "  cd ${REPO_DIR}"
log "  ./scripts/start-worker.sh"

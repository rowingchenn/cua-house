#!/bin/bash
# Manually start a cua-house worker after syncing this node's own task-data disk.
#
# This script intentionally does not install or depend on systemd. Run it from
# the worker host when you want the node to join the cluster.
set -euo pipefail

export PATH="${HOME}/.local/bin:/usr/local/bin:${PATH}"

REPO_DIR="${CUA_HOUSE_REPO_DIR:-/opt/cua-house}"
HOST_CONFIG="${CUA_HOUSE_HOST_CONFIG:-/etc/cua-house/worker.yaml}"
IMAGE_CATALOG="${CUA_HOUSE_IMAGE_CATALOG:-/etc/cua-house/images.yaml}"
ENV_FILE="${CUA_HOUSE_ENV_FILE:-/etc/cua-house/worker.env}"
TASK_DATA_ROOT="${CUA_HOUSE_TASK_DATA_ROOT:-/mnt/agenthle-task-data}"
TASK_DATA_LOWER="${CUA_HOUSE_TASK_DATA_LOWER:-/mnt/agenthle-task-data-ro}"
TASK_DATA_UPPER="${CUA_HOUSE_TASK_DATA_UPPER:-/mnt/xfs/task-data-upper}"
TASK_DATA_WORK="${CUA_HOUSE_TASK_DATA_WORK:-/mnt/xfs/task-data-work}"
TASK_DATA_GCS_URI="${CUA_HOUSE_TASK_DATA_GCS_URI:-gs://agenthle}"
TASK_DATA_RSYNC_EXCLUDE="${CUA_HOUSE_TASK_DATA_RSYNC_EXCLUDE:-(^|/)vm-images/|.*\\.gstmp$}"
LOG_FILE="${CUA_HOUSE_WORKER_LOG:-/var/log/cua-house/worker.log}"
SHARED_GROUP="${CUA_HOUSE_SHARED_GROUP:-cua-house}"

log() { printf '[start-worker %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

restore_task_data_mounts() {
    set +e
    if mountpoint -q "${TASK_DATA_ROOT}"; then
        return
    fi
    if mountpoint -q "${TASK_DATA_LOWER}"; then
        sudo umount "${TASK_DATA_LOWER}"
    fi
    sudo mkdir -p "${TASK_DATA_LOWER}" "${TASK_DATA_ROOT}"
    sudo mount -o ro,noload /dev/disk/by-id/google-task-data "${TASK_DATA_LOWER}" 2>/dev/null
    sudo mount "${TASK_DATA_ROOT}" 2>/dev/null
}

trap restore_task_data_mounts ERR INT TERM

if [[ ! -d "${REPO_DIR}" ]]; then
    echo "repo dir not found: ${REPO_DIR}" >&2
    exit 1
fi
sudo mkdir -p "$(dirname "${LOG_FILE}")"
sudo groupadd -f "${SHARED_GROUP}"
sudo chown "$(id -u):${SHARED_GROUP}" "$(dirname "${LOG_FILE}")"
sudo chmod g+rwX "$(dirname "${LOG_FILE}")"

if mountpoint -q "${TASK_DATA_ROOT}"; then
    log "unmounting overlay ${TASK_DATA_ROOT} before syncing lower data disk"
    sudo umount "${TASK_DATA_ROOT}"
fi

sudo mkdir -p "${TASK_DATA_LOWER}" "${TASK_DATA_ROOT}"
if mountpoint -q "${TASK_DATA_LOWER}"; then
    log "unmounting ${TASK_DATA_LOWER} before read-write sync mount"
    sudo umount "${TASK_DATA_LOWER}"
fi
log "mounting ${TASK_DATA_LOWER} read-write for sync"
sudo mount -o rw /dev/disk/by-id/google-task-data "${TASK_DATA_LOWER}"

if [[ ! -w "${TASK_DATA_LOWER}" ]]; then
    log "${TASK_DATA_LOWER} is mounted read-write but only root can modify it; sync will run through sudo"
fi

log "syncing ${TASK_DATA_GCS_URI} -> ${TASK_DATA_LOWER} (exclude: ${TASK_DATA_RSYNC_EXCLUDE})"
if command -v gcloud >/dev/null 2>&1; then
    sudo env PATH="${PATH}" gcloud storage rsync --recursive \
        --exclude="${TASK_DATA_RSYNC_EXCLUDE}" \
        "${TASK_DATA_GCS_URI}" "${TASK_DATA_LOWER}"
elif command -v gsutil >/dev/null 2>&1; then
    sudo env PATH="${PATH}" gsutil -m rsync -r \
        -x "${TASK_DATA_RSYNC_EXCLUDE}" \
        "${TASK_DATA_GCS_URI}" "${TASK_DATA_LOWER}"
else
    echo "neither gcloud nor gsutil is available for task-data sync" >&2
    exit 127
fi

sync
log "returning ${TASK_DATA_LOWER} to read-only mode"
sudo umount "${TASK_DATA_LOWER}"
sudo mount -o ro,noload /dev/disk/by-id/google-task-data "${TASK_DATA_LOWER}"

if ! mountpoint -q "${TASK_DATA_ROOT}"; then
    log "mounting overlay ${TASK_DATA_ROOT}"
    sudo mkdir -p "${TASK_DATA_ROOT}"
    sudo mount "${TASK_DATA_ROOT}"
fi

trap - ERR INT TERM

if [[ ! -w "${TASK_DATA_ROOT}" ]]; then
    log "${TASK_DATA_ROOT} is not writable by $(id -un); fixing OverlayFS upper/work permissions"
    sudo chown -R "$(id -u):${SHARED_GROUP}" "${TASK_DATA_UPPER}" "${TASK_DATA_WORK}"
    sudo chmod -R g+rwX "${TASK_DATA_UPPER}" "${TASK_DATA_WORK}"
fi

if [[ ! -w "${TASK_DATA_ROOT}" ]]; then
    echo "task data root is still not writable: ${TASK_DATA_ROOT}" >&2
    exit 1
fi

cd "${REPO_DIR}"
if [[ -r "${ENV_FILE}" ]]; then
    set -a
    # shellcheck source=/dev/null
    source <(sudo cat "${ENV_FILE}")
    set +a
fi

log "starting cua-house worker"
setsid nohup uv run python -m cua_house_server.cli \
    --host-config "${HOST_CONFIG}" \
    --image-catalog "${IMAGE_CATALOG}" \
    --host 0.0.0.0 --port 8787 --mode worker \
    </dev/null >"${LOG_FILE}" 2>&1 &
disown

log "worker start requested; tail ${LOG_FILE}"

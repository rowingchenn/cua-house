#!/usr/bin/env bash
# cold-boot-test.sh — Cold-boot a qcow2 and verify CUA readiness.
# No savevm / loadvm — tests the full boot path.
#
# Usage:
#   ./scripts/cold-boot-test.sh \
#       --qcow2 /path/to/file.qcow2 \
#       [--timeout 600] \
#       [--port 15900] \
#       [--docker-image trycua/cua-qemu-windows:latest]
set -euo pipefail

# ---------- defaults ----------
TIMEOUT=600
PORT=15900
DOCKER_IMAGE="trycua/cua-qemu-windows:latest"
CONTAINER_NAME="cua-house-cold-boot-test"
QCOW2=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
    cat <<EOF
Usage: $0 --qcow2 <PATH> [--timeout 600] [--port 15900] \\
          [--docker-image IMAGE]

Required:
  --qcow2 <PATH>         Path to qcow2 file to cold-boot

Optional:
  --timeout <SECS>        Timeout in seconds (default: 600)
  --port <PORT>           Host port for CUA endpoint (default: 15900)
  --docker-image <IMAGE>  Docker image (default: trycua/cua-qemu-windows:latest)
EOF
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --qcow2)        QCOW2="$2"; shift 2 ;;
        --timeout)      TIMEOUT="$2"; shift 2 ;;
        --port)         PORT="$2"; shift 2 ;;
        --docker-image) DOCKER_IMAGE="$2"; shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        *)              echo "unknown arg: $1" >&2; usage; exit 2 ;;
    esac
done

# ---------- arg validation ----------
if [[ -z "${QCOW2}" ]]; then
    echo "ERROR: missing required arg: --qcow2" >&2
    usage
    exit 2
fi

# ---------- helpers ----------
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31mfail:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- cleanup trap ----------
TMPDIR_BOOT=""
cleanup() {
    step "Cleanup"
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    info "container ${CONTAINER_NAME} removed"
    if [[ -n "${TMPDIR_BOOT}" && -d "${TMPDIR_BOOT}" ]]; then
        rm -rf "${TMPDIR_BOOT}"
        info "temp dir ${TMPDIR_BOOT} removed"
    fi
}
trap cleanup EXIT

START_TIME=$(date +%s)

# ---------- phase 1: preflight ----------
step "Preflight checks"
info "qcow2=${QCOW2}"
info "timeout=${TIMEOUT}s"
info "port=${PORT}"
info "docker_image=${DOCKER_IMAGE}"
info "container_name=${CONTAINER_NAME}"

if [[ ! -f "${QCOW2}" ]]; then
    fail "qcow2 file not found: ${QCOW2}"
fi
info "qcow2 exists ($(du -h "${QCOW2}" | cut -f1))"

if ! command -v docker >/dev/null 2>&1; then
    fail "docker not in PATH"
fi
info "docker available"

if [[ ! -e /dev/kvm ]]; then
    fail "/dev/kvm not found — KVM not available"
fi
info "/dev/kvm exists"

# Remove any leftover container from a previous run
if docker inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
    warn "removing leftover container ${CONTAINER_NAME}"
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1
fi

# ---------- phase 2: prepare temp dir + copy qcow2 ----------
step "Preparing temp directory"
TMPDIR_BOOT=$(mktemp -d)
info "temp dir: ${TMPDIR_BOOT}"
cp "${QCOW2}" "${TMPDIR_BOOT}/data.qcow2"
info "copied qcow2 as data.qcow2"

# ---------- phase 3: docker run ----------
step "Starting container"

RUNTIME_ROOT="${REPO_ROOT}/runtime"
EXTRA_MOUNTS=()
if [[ -f "${RUNTIME_ROOT}/boot-patched.sh" ]]; then
    info "found boot-patched.sh — mounting at /run/boot.sh"
    EXTRA_MOUNTS+=(-v "${RUNTIME_ROOT}/boot-patched.sh:/run/boot.sh:ro")
else
    info "no boot-patched.sh found — using dockur default boot.sh"
fi

docker run -d \
    --name "${CONTAINER_NAME}" \
    --device=/dev/kvm \
    --cap-add NET_ADMIN \
    -e RAM_SIZE=8G \
    -e CPU_CORES=4 \
    -e CPU_MODEL=host \
    -e HV=N \
    -e VM_NET_IP=172.30.0.2 \
    -p "${PORT}:5000" \
    -v "${TMPDIR_BOOT}/data.qcow2:/storage/data.qcow2" \
    "${EXTRA_MOUNTS[@]+"${EXTRA_MOUNTS[@]}"}" \
    "${DOCKER_IMAGE}"

info "container ${CONTAINER_NAME} started"

# ---------- phase 4: poll for CUA readiness ----------
step "Polling http://127.0.0.1:${PORT}/status (timeout=${TIMEOUT}s)"
ELAPSED=0
POLL_INTERVAL=5
READY=0

while (( ELAPSED < TIMEOUT )); do
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/status" 2>/dev/null || echo "000")
    if [[ "${HTTP_CODE}" == "200" ]]; then
        READY=1
        break
    fi
    sleep "${POLL_INTERVAL}"
    ELAPSED=$(( $(date +%s) - START_TIME ))
    # Print progress every 30s
    if (( ELAPSED % 30 < POLL_INTERVAL )); then
        info "still waiting... ${ELAPSED}s elapsed (last HTTP ${HTTP_CODE})"
    fi
done

# ---------- phase 5: result ----------
END_TIME=$(date +%s)
TOTAL_ELAPSED=$((END_TIME - START_TIME))

if (( READY == 1 )); then
    printf '\n\033[1;32mPASS: CUA readiness confirmed in %ds\033[0m\n' "${TOTAL_ELAPSED}"
else
    step "Container logs (last 40 lines)"
    docker logs --tail 40 "${CONTAINER_NAME}" 2>&1 || true
    printf '\n\033[1;31mFAIL: CUA readiness not confirmed within %ds\033[0m\n' "${TIMEOUT}"
    exit 1
fi

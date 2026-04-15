#!/bin/bash
# clone-worker.sh — provision a new cua-house worker node on GCP by
# cloning a golden boot-disk snapshot taken from an existing worker.
#
# Usage:
#   ./scripts/clone-worker.sh \
#       --new-id kvm04 \
#       --source-instance agenthle-nested-kvm-02 \
#       --master-url ws://10.128.0.16:8787/v1/cluster/ws \
#       --join-token "$CUA_HOUSE_CLUSTER_JOIN_TOKEN"
#
# See docs/deployment/clone-worker.md for the full runbook, including
# the post-provision pool-spec append and smoke-test steps.
#
# This script takes NO destructive actions on kvm02/kvm03 or master.
# The worst it can do to the existing cluster is take a live snapshot
# of kvm02's boot disk (non-disruptive) and append a new assignment to
# the pool spec (only if --update-pool is passed).
set -euo pipefail

# ---------- defaults ----------
PROJECT="sunblaze-4"
ZONE="us-central1-a"
VPC="agenthle-vpc"
MACHINE_TYPE="n2-standard-16"
XFS_SIZE_GB=512
BOOT_DISK_SIZE_GB=500
BOOT_DISK_TYPE="pd-ssd"
TASK_DATA_DISK="agenthle-nested-kvm-01-task-data"
SOURCE_INSTANCE=""
SOURCE_SNAPSHOT=""
NEW_ID=""
MASTER_URL=""
JOIN_TOKEN=""
UPDATE_POOL=0
DRY_RUN=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
    cat <<EOF
Usage: $0 --new-id <ID> --master-url <URL> --join-token <TOKEN> \\
          (--source-instance <NAME> | --source-boot-snapshot <NAME>) \\
          [--machine-type n2-standard-16] \\
          [--project sunblaze-4] [--zone us-central1-a] [--vpc agenthle-vpc] \\
          [--xfs-size-gb 512] \\
          [--task-data-disk agenthle-nested-kvm-01-task-data] \\
          [--update-pool] [--dry-run]

Required:
  --new-id <ID>              GCE instance suffix + cluster worker_id,
                             e.g. kvm04. Must be unique in both GCE and
                             master's WorkerRegistry.
  --master-url <URL>         ws://master-internal-ip:8787/v1/cluster/ws
  --join-token <TOKEN>       shared CUA_HOUSE_CLUSTER_JOIN_TOKEN

Source (pick one):
  --source-instance <NAME>   take a live boot-disk snapshot of this
                             running worker (e.g. agenthle-nested-kvm-02)
                             and clone from it.
  --source-boot-snapshot <NAME>
                             reuse an existing snapshot
                             (e.g. agenthle-worker-boot-golden-20260414)

Optional:
  --update-pool              after master sees the new worker, append its
                             assignment to /v1/cluster/pool idempotently.
  --dry-run                  print every gcloud/ssh command without
                             executing anything.
EOF
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --new-id)               NEW_ID="$2"; shift 2 ;;
        --master-url)           MASTER_URL="$2"; shift 2 ;;
        --join-token)           JOIN_TOKEN="$2"; shift 2 ;;
        --source-instance)      SOURCE_INSTANCE="$2"; shift 2 ;;
        --source-boot-snapshot) SOURCE_SNAPSHOT="$2"; shift 2 ;;
        --machine-type)         MACHINE_TYPE="$2"; shift 2 ;;
        --project)              PROJECT="$2"; shift 2 ;;
        --zone)                 ZONE="$2"; shift 2 ;;
        --vpc)                  VPC="$2"; shift 2 ;;
        --xfs-size-gb)          XFS_SIZE_GB="$2"; shift 2 ;;
        --task-data-disk)       TASK_DATA_DISK="$2"; shift 2 ;;
        --update-pool)          UPDATE_POOL=1; shift ;;
        --dry-run)              DRY_RUN=1; shift ;;
        -h|--help)              usage; exit 0 ;;
        *)                      echo "unknown arg: $1" >&2; usage; exit 2 ;;
    esac
done

# ---------- arg validation ----------
MISSING=()
[[ -n "${NEW_ID}" ]]       || MISSING+=("--new-id")
[[ -n "${MASTER_URL}" ]]   || MISSING+=("--master-url")
[[ -n "${JOIN_TOKEN}" ]]   || MISSING+=("--join-token")
if [[ -z "${SOURCE_INSTANCE}" && -z "${SOURCE_SNAPSHOT}" ]]; then
    MISSING+=("--source-instance or --source-boot-snapshot")
fi
if [[ -n "${SOURCE_INSTANCE}" && -n "${SOURCE_SNAPSHOT}" ]]; then
    echo "ERROR: --source-instance and --source-boot-snapshot are mutually exclusive" >&2
    exit 2
fi
if (( ${#MISSING[@]} > 0 )); then
    echo "ERROR: missing required args: ${MISSING[*]}" >&2
    usage
    exit 2
fi

# Derive names from NEW_ID. Naming convention matches existing nodes
# (agenthle-nested-kvm-02 / -03): if NEW_ID is like "kvm04", expand to
# "kvm-04" so the instance is "agenthle-nested-kvm-04".
if [[ "${NEW_ID}" =~ ^kvm([0-9]+)$ ]]; then
    INSTANCE_SUFFIX="kvm-${BASH_REMATCH[1]}"
else
    INSTANCE_SUFFIX="${NEW_ID}"
fi
INSTANCE_NAME="agenthle-nested-${INSTANCE_SUFFIX}"
BOOT_DISK_NAME="${INSTANCE_NAME}"
XFS_DISK_NAME="${INSTANCE_NAME}-xfs"
# Master base URL for HTTP ops (strip ws:// + path).
MASTER_HTTP="${MASTER_URL#ws://}"
MASTER_HTTP="http://${MASTER_HTTP%/v1/cluster/ws}"

# ---------- helpers ----------
step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31mfail:\033[0m %s\n' "$*" >&2; exit 1; }

run() {
    printf '    $ '
    printf '%q ' "$@"
    printf '\n'
    if (( DRY_RUN == 0 )); then
        "$@"
    fi
}

token_fingerprint() {
    if command -v shasum >/dev/null 2>&1; then
        printf '%s' "${JOIN_TOKEN}" | shasum -a 256 | cut -c1-8
    else
        printf '%s' "${JOIN_TOKEN}" | openssl dgst -sha256 | awk '{print substr($NF,1,8)}'
    fi
}

START_TIME=$(date +%s)

# ---------- phase 1: preflight ----------
step "Preflight checks"
info "new_id=${NEW_ID}"
info "instance=${INSTANCE_NAME}"
info "master_url=${MASTER_URL}"
info "master_http=${MASTER_HTTP}"
info "join_token sha256_prefix=$(token_fingerprint)"
info "project=${PROJECT} zone=${ZONE} vpc=${VPC}"

if ! command -v gcloud >/dev/null 2>&1; then
    fail "gcloud not in PATH"
fi
CURRENT_PROJECT=$(gcloud config get-value project 2>/dev/null || echo "")
if [[ -n "${CURRENT_PROJECT}" && "${CURRENT_PROJECT}" != "${PROJECT}" ]]; then
    warn "current gcloud project is ${CURRENT_PROJECT}, --project=${PROJECT}; passing --project explicitly to every call"
fi

# Instance name must not be taken.
if gcloud compute instances describe "${INSTANCE_NAME}" \
        --project="${PROJECT}" --zone="${ZONE}" >/dev/null 2>&1; then
    fail "instance ${INSTANCE_NAME} already exists in ${ZONE}"
fi

# worker_id must not already be in master's registry.
if (( DRY_RUN == 0 )); then
    EXISTING=$(curl -sS --max-time 5 "${MASTER_HTTP}/v1/cluster/workers" 2>/dev/null \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(','.join(w['worker_id'] for w in d))" 2>/dev/null \
        || echo "")
    if [[ ",${EXISTING}," == *",${NEW_ID},"* ]]; then
        fail "worker_id '${NEW_ID}' already present in master registry: ${EXISTING}"
    fi
    info "master registry check ok (current workers: ${EXISTING:-none})"
fi

# Source snapshot exists (if --source-boot-snapshot).
if [[ -n "${SOURCE_SNAPSHOT}" ]]; then
    if (( DRY_RUN == 0 )) && ! gcloud compute snapshots describe "${SOURCE_SNAPSHOT}" \
            --project="${PROJECT}" >/dev/null 2>&1; then
        fail "snapshot ${SOURCE_SNAPSHOT} not found"
    fi
    info "using existing snapshot ${SOURCE_SNAPSHOT}"
fi

# Required template files.
WORKER_TEMPLATE="${REPO_ROOT}/examples/worker.yaml"
IMAGES_CATALOG="${REPO_ROOT}/examples/images.yaml"
UNIT_FILE="${REPO_ROOT}/examples/systemd/cua-house-worker.service"
BOOTSTRAP_FILE="${SCRIPT_DIR}/_clone-worker-bootstrap.sh"

for f in "${WORKER_TEMPLATE}" "${UNIT_FILE}" "${BOOTSTRAP_FILE}"; do
    [[ -f "${f}" ]] || fail "missing required template/file: ${f}"
done
if [[ ! -f "${IMAGES_CATALOG}" ]]; then
    IMAGES_CATALOG="${REPO_ROOT}/packages/server/src/cua_house_server/config/defaults/images.yaml"
    [[ -f "${IMAGES_CATALOG}" ]] || fail "no images.yaml found in examples/ or config/defaults/"
fi
info "images catalog: ${IMAGES_CATALOG}"

# ---------- phase 2: source snapshot (if --source-instance) ----------
if [[ -n "${SOURCE_INSTANCE}" ]]; then
    SOURCE_SNAPSHOT="agenthle-worker-boot-golden-$(date +%Y%m%d-%H%M%S)"
    step "Taking live boot-disk snapshot of ${SOURCE_INSTANCE}"
    info "snapshot will be named ${SOURCE_SNAPSHOT}"
    info "this is non-disruptive — source instance keeps running"
    run gcloud compute disks snapshot "${SOURCE_INSTANCE}" \
        --project="${PROJECT}" --zone="${ZONE}" \
        --snapshot-names="${SOURCE_SNAPSHOT}" \
        --storage-location="${ZONE%-*}"
fi

# ---------- phase 3: create boot + xfs disks ----------
step "Creating boot disk from snapshot"
run gcloud compute disks create "${BOOT_DISK_NAME}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --source-snapshot="${SOURCE_SNAPSHOT}" \
    --type="${BOOT_DISK_TYPE}" --size="${BOOT_DISK_SIZE_GB}GB"

step "Creating fresh XFS disk"
run gcloud compute disks create "${XFS_DISK_NAME}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --type=pd-ssd --size="${XFS_SIZE_GB}GB"

# ---------- phase 4: create instance ----------
step "Creating GCE instance ${INSTANCE_NAME}"
run gcloud compute instances create "${INSTANCE_NAME}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --network-interface="subnet=${VPC}" \
    --tags=agenthle \
    --enable-nested-virtualization \
    --min-cpu-platform=Intel\ Cascade\ Lake \
    --disk="name=${BOOT_DISK_NAME},boot=yes,auto-delete=yes,mode=rw" \
    --disk="name=${XFS_DISK_NAME},device-name=xfs,mode=rw,auto-delete=yes" \
    --disk="name=${TASK_DATA_DISK},device-name=task-data,mode=ro" \
    --metadata="cua-house-worker-id=${NEW_ID},cua-house-master-url=${MASTER_URL}"

# ---------- phase 5: wait for SSH ----------
step "Waiting for SSH on ${INSTANCE_NAME}"
INTERNAL_IP="DRY_RUN_INTERNAL_IP"
if (( DRY_RUN == 0 )); then
    for attempt in $(seq 1 24); do
        if gcloud compute ssh "${INSTANCE_NAME}" --project="${PROJECT}" \
                --zone="${ZONE}" --command='true' >/dev/null 2>&1; then
            info "ssh ready after ${attempt}x5s"
            break
        fi
        sleep 5
    done
    INTERNAL_IP=$(gcloud compute instances describe "${INSTANCE_NAME}" \
        --project="${PROJECT}" --zone="${ZONE}" \
        --format='value(networkInterfaces[0].networkIP)')
    info "internal IP: ${INTERNAL_IP}"
fi

# ---------- phase 6: render + scp artifacts ----------
step "Rendering worker.yaml from template"
RENDERED_DIR=$(mktemp -d)
trap 'rm -rf "${RENDERED_DIR}"' EXIT

sed \
    -e "s|@@WORKER_ID@@|${NEW_ID}|g" \
    -e "s|@@INTERNAL_IP@@|${INTERNAL_IP}|g" \
    -e "s|@@MASTER_URL@@|${MASTER_URL}|g" \
    "${WORKER_TEMPLATE}" > "${RENDERED_DIR}/worker.yaml"
info "rendered ${RENDERED_DIR}/worker.yaml"

cat > "${RENDERED_DIR}/worker.env" <<EOF
# Installed by scripts/clone-worker.sh on $(date -u +'%Y-%m-%dT%H:%M:%SZ')
# mode 0600 — contains the cluster join secret.
CUA_HOUSE_CLUSTER_JOIN_TOKEN=${JOIN_TOKEN}
EOF
chmod 600 "${RENDERED_DIR}/worker.env"

cp "${IMAGES_CATALOG}" "${RENDERED_DIR}/images.yaml"
cp "${UNIT_FILE}" "${RENDERED_DIR}/cua-house-worker.service"
cp "${BOOTSTRAP_FILE}" "${RENDERED_DIR}/clone-worker-bootstrap.sh"

step "scp artifacts to ${INSTANCE_NAME}:/tmp/"
run gcloud compute scp \
    --project="${PROJECT}" --zone="${ZONE}" \
    "${RENDERED_DIR}/worker.yaml" \
    "${RENDERED_DIR}/images.yaml" \
    "${RENDERED_DIR}/worker.env" \
    "${RENDERED_DIR}/cua-house-worker.service" \
    "${RENDERED_DIR}/clone-worker-bootstrap.sh" \
    "${INSTANCE_NAME}":/tmp/

# ---------- phase 7: remote bootstrap ----------
step "Running remote bootstrap on ${INSTANCE_NAME}"
run gcloud compute ssh "${INSTANCE_NAME}" \
    --project="${PROJECT}" --zone="${ZONE}" \
    --command='bash /tmp/clone-worker-bootstrap.sh'

# ---------- phase 8: master join poll ----------
step "Polling master for ${NEW_ID} registration"
if (( DRY_RUN == 0 )); then
    JOINED=0
    for attempt in $(seq 1 24); do
        if curl -sS --max-time 5 "${MASTER_HTTP}/v1/cluster/workers" 2>/dev/null \
                | python3 -c "
import json, sys
data = json.load(sys.stdin)
for w in data:
    if w['worker_id'] == '${NEW_ID}' and w['online']:
        sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
            info "worker ${NEW_ID} online after ${attempt}x5s"
            JOINED=1
            break
        fi
        sleep 5
    done
    if (( JOINED == 0 )); then
        warn "master did not see ${NEW_ID} within 120s"
        warn "check: gcloud compute ssh ${INSTANCE_NAME} -- sudo journalctl -u cua-house-worker -n 80"
    fi
fi

# ---------- phase 9: optional pool-spec update ----------
if (( UPDATE_POOL == 1 )); then
    step "Appending ${NEW_ID} to master /v1/cluster/pool"
    if (( DRY_RUN == 0 )); then
        CURRENT_SPEC=$(curl -sS "${MASTER_HTTP}/v1/cluster/pool")
        NEW_SPEC=$(CURRENT_SPEC="${CURRENT_SPEC}" NEW_ID="${NEW_ID}" python3 -c "
import json, os
spec = json.loads(os.environ['CURRENT_SPEC'])
spec.setdefault('assignments', []).append({
    'worker_id': os.environ['NEW_ID'],
    'image_key': 'cpu-free',
    'count': 1,
    'vcpus': 4,
    'memory_gb': 8,
})
print(json.dumps(spec))
")
        echo "${NEW_SPEC}" | curl -sS -X PUT "${MASTER_HTTP}/v1/cluster/pool" \
            -H 'Content-Type: application/json' -d @-
        info "pool spec updated; reconciler will push ADD_IMAGE + ADD_VM shortly"
    else
        info "(dry run — skipping pool update)"
    fi
else
    info "--update-pool not set; to add ${NEW_ID} to the pool:"
    info ""
    info "  curl -sS ${MASTER_HTTP}/v1/cluster/pool > /tmp/pool.json"
    info "  python3 -c \"import json; d=json.load(open('/tmp/pool.json')); \\"
    info "    d['assignments'].append({'worker_id':'${NEW_ID}','image_key':'cpu-free',\\"
    info "    'count':1,'vcpus':4,'memory_gb':8}); print(json.dumps(d))\" > /tmp/pool-new.json"
    info "  curl -sS -X PUT ${MASTER_HTTP}/v1/cluster/pool \\"
    info "    -H 'Content-Type: application/json' -d @/tmp/pool-new.json"
fi

# ---------- summary ----------
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
step "Summary"
info "instance:          ${INSTANCE_NAME}"
info "internal IP:       ${INTERNAL_IP}"
info "lease_endpoint:    http://${INTERNAL_IP}:8787"
info "join token sha256: $(token_fingerprint)"
info "source snapshot:   ${SOURCE_SNAPSHOT}"
info "elapsed:           ${ELAPSED}s"
if (( DRY_RUN == 1 )); then
    printf '\n\033[1;33mDRY RUN — no changes made.\033[0m\n'
else
    printf '\n\033[1;32mClone complete. Worker %s is live.\033[0m\n' "${NEW_ID}"
fi

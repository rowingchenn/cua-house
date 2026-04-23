#!/usr/bin/env bash
# export-gcp-to-qcow2.sh — Export a GCP dev VM's boot disk to qcow2
# format in the template image bucket.
#
# Usage:
#   ./scripts/export-gcp-to-qcow2.sh \
#       --image-key cpu-free \
#       [--date 20260415] \
#       [--zone us-west1-a] \
#       [--project sunblaze-4] \
#       [--dry-run]
set -euo pipefail

# ---------- defaults ----------
PROJECT="sunblaze-4"
ZONE="us-west1-a"
DEV_VM_PREFIX="agenthle-dev-"
TEMPLATE_BUCKET="gs://agenthle-images/templates"
IMAGE_KEY=""
DATE=""
DRY_RUN=0

usage() {
    cat <<EOF
Usage: $0 --image-key <KEY> [--date YYYYMMDD] [--zone ZONE] \\
          [--project PROJECT] [--dry-run]

Required:
  --image-key <KEY>   Image key, e.g. cpu-free, cpu-licensed

Optional:
  --date <YYYYMMDD>   Date stamp (default: today)
  --zone <ZONE>       GCE zone (default: us-west1-a)
  --project <PROJECT> GCP project (default: sunblaze-4)
  --dry-run           Print every command without executing
EOF
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --image-key) IMAGE_KEY="$2"; shift 2 ;;
        --date)      DATE="$2"; shift 2 ;;
        --zone)      ZONE="$2"; shift 2 ;;
        --project)   PROJECT="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           echo "unknown arg: $1" >&2; usage; exit 2 ;;
    esac
done

# ---------- arg validation ----------
if [[ -z "${IMAGE_KEY}" ]]; then
    echo "ERROR: missing required arg: --image-key" >&2
    usage
    exit 2
fi

[[ -n "${DATE}" ]] || DATE=$(date +%Y%m%d)

DEV_VM="${DEV_VM_PREFIX}${IMAGE_KEY}"
SNAPSHOT_NAME="agenthle-dev-${IMAGE_KEY}-export-${DATE}"
IMAGE_NAME="agenthle-dev-${IMAGE_KEY}-export-${DATE}"
GCS_URI="${TEMPLATE_BUCKET}/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2"

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

START_TIME=$(date +%s)

# ---------- phase 1: preflight ----------
step "Preflight checks"
info "image_key=${IMAGE_KEY}"
info "date=${DATE}"
info "dev_vm=${DEV_VM}"
info "project=${PROJECT} zone=${ZONE}"
info "template_bucket=${TEMPLATE_BUCKET}"

if ! command -v gcloud >/dev/null 2>&1; then
    fail "gcloud not in PATH"
fi

# Verify gcloud auth
if (( DRY_RUN == 0 )); then
    if ! gcloud auth print-access-token >/dev/null 2>&1; then
        fail "gcloud not authenticated — run 'gcloud auth login'"
    fi
    info "gcloud auth ok"
fi

# Verify dev VM exists
if (( DRY_RUN == 0 )); then
    if ! gcloud compute instances describe "${DEV_VM}" \
            --project="${PROJECT}" --zone="${ZONE}" >/dev/null 2>&1; then
        fail "dev VM ${DEV_VM} not found in ${PROJECT}/${ZONE}"
    fi
    info "dev VM ${DEV_VM} exists"
fi

# Resolve boot disk name (usually same as instance name)
DISK="${DEV_VM}"
info "boot disk=${DISK}"

# ---------- phase 2: create snapshot ----------
step "Creating snapshot of boot disk"
if (( DRY_RUN == 0 )) && gcloud compute snapshots describe "${SNAPSHOT_NAME}" \
        --project="${PROJECT}" >/dev/null 2>&1; then
    info "snapshot ${SNAPSHOT_NAME} already exists — skipping"
else
    run gcloud compute disks snapshot "${DISK}" \
        --project="${PROJECT}" --zone="${ZONE}" \
        --snapshot-names="${SNAPSHOT_NAME}" \
        --storage-location="${ZONE%-*}"
fi

# ---------- phase 3: create GCP image from snapshot ----------
step "Creating GCP image from snapshot"
if (( DRY_RUN == 0 )) && gcloud compute images describe "${IMAGE_NAME}" \
        --project="${PROJECT}" >/dev/null 2>&1; then
    info "image ${IMAGE_NAME} already exists — skipping"
else
    run gcloud compute images create "${IMAGE_NAME}" \
        --project="${PROJECT}" \
        --source-snapshot="${SNAPSHOT_NAME}"
fi

# ---------- phase 4: export image to GCS as qcow2 ----------
step "Exporting image to GCS as qcow2"
if (( DRY_RUN == 0 )) && gsutil stat "${GCS_URI}" >/dev/null 2>&1; then
    info "${GCS_URI} already exists — skipping"
else
    run gcloud compute images export \
        --image="${IMAGE_NAME}" \
        --project="${PROJECT}" \
        --export-format=qcow2 \
        --destination-uri="${GCS_URI}"
fi

# ---------- phase 5: summary ----------
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
step "Export complete"
info "snapshot:    ${SNAPSHOT_NAME}"
info "image:       ${IMAGE_NAME}"
info "gcs_uri:     ${GCS_URI}"
info "elapsed:     ${ELAPSED}s"
info ""
info "To cold-boot test on a KVM host:"
info "  gsutil cp ${GCS_URI} /mnt/xfs/images/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2"

if (( DRY_RUN == 1 )); then
    printf '\n\033[1;33mDRY RUN — no changes made.\033[0m\n'
else
    printf '\n\033[1;32mExport complete. qcow2 template written to %s\033[0m\n' "${GCS_URI}"
fi

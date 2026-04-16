#!/usr/bin/env bash
# upload-template.sh — Upload a baked qcow2 to GCS as the
# source-of-truth template.
#
# Usage:
#   ./scripts/upload-template.sh \
#       --image-key cpu-free \
#       --date 20260415 \
#       --qcow2 /path/to/file.qcow2 \
#       [--dry-run]
set -euo pipefail

# ---------- defaults ----------
TEMPLATE_BUCKET="gs://agenthle-images/templates"
IMAGE_KEY=""
DATE=""
QCOW2=""
DRY_RUN=0

usage() {
    cat <<EOF
Usage: $0 --image-key <KEY> --date <YYYYMMDD> --qcow2 <PATH> [--dry-run]

Required:
  --image-key <KEY>    Image key, e.g. cpu-free, cpu-licensed
  --date <YYYYMMDD>    Date stamp for the template version
  --qcow2 <PATH>       Path to the baked qcow2 file

Optional:
  --dry-run            Print every command without executing
EOF
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --image-key) IMAGE_KEY="$2"; shift 2 ;;
        --date)      DATE="$2"; shift 2 ;;
        --qcow2)     QCOW2="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        *)           echo "unknown arg: $1" >&2; usage; exit 2 ;;
    esac
done

# ---------- arg validation ----------
MISSING=()
[[ -n "${IMAGE_KEY}" ]] || MISSING+=("--image-key")
[[ -n "${DATE}" ]]      || MISSING+=("--date")
[[ -n "${QCOW2}" ]]     || MISSING+=("--qcow2")
if (( ${#MISSING[@]} > 0 )); then
    echo "ERROR: missing required args: ${MISSING[*]}" >&2
    usage
    exit 2
fi

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
info "qcow2=${QCOW2}"
info "gcs_uri=${GCS_URI}"

if [[ ! -f "${QCOW2}" ]]; then
    fail "qcow2 file not found: ${QCOW2}"
fi
info "qcow2 exists ($(du -h "${QCOW2}" | cut -f1))"

if ! command -v gsutil >/dev/null 2>&1; then
    fail "gsutil not in PATH"
fi
info "gsutil available"

# ---------- phase 2: upload ----------
step "Uploading to GCS"
run gsutil -o GSUtil:parallel_composite_upload_threshold=150M \
    cp "${QCOW2}" "${GCS_URI}"

# ---------- phase 3: verify ----------
step "Verifying upload"
if (( DRY_RUN == 0 )); then
    REMOTE_SIZE=$(gsutil stat "${GCS_URI}" 2>/dev/null | grep 'Content-Length' | awk '{print $2}')

    # Cross-platform local file size
    if stat --version >/dev/null 2>&1; then
        # GNU stat (Linux)
        LOCAL_SIZE=$(stat -c '%s' "${QCOW2}")
    else
        # BSD stat (macOS)
        LOCAL_SIZE=$(stat -f '%z' "${QCOW2}")
    fi

    info "local size:  ${LOCAL_SIZE} bytes"
    info "remote size: ${REMOTE_SIZE} bytes"

    if [[ "${LOCAL_SIZE}" != "${REMOTE_SIZE}" ]]; then
        fail "size mismatch! local=${LOCAL_SIZE} remote=${REMOTE_SIZE}"
    fi
    info "size match confirmed"
else
    info "(dry run — skipping verification)"
fi

# ---------- phase 4: summary ----------
END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
step "Upload complete"
info "gcs_uri: ${GCS_URI}"
info "elapsed: ${ELAPSED}s"
info ""
info "images.yaml snippet:"
cat <<EOF
    local:
      template_qcow2_path: /mnt/xfs/images/${IMAGE_KEY}/${IMAGE_KEY}-${DATE}.qcow2
      gcs_uri: ${GCS_URI}
      version: "${DATE}"
EOF

if (( DRY_RUN == 1 )); then
    printf '\n\033[1;33mDRY RUN — no changes made.\033[0m\n'
else
    printf '\n\033[1;32mTemplate uploaded to %s\033[0m\n' "${GCS_URI}"
fi

# GCP infrastructure reference

GCP-specific configuration and conventions for the GCP VM runtime backend.

## Project

All resources live in GCP project `sunblaze-4`.

## Networks

| Network | Purpose |
|---------|---------|
| `agenthle-vpc` | CPU-based evaluation VMs |
| `osworld-vpc` | GPU-based evaluation VMs (L4 GPUs in us-west1-a) |

## Service account

```
agenthle-vm-service@sunblaze-4.iam.gserviceaccount.com
```

Required IAM roles:

- `roles/compute.instanceAdmin.v1` -- create, delete, manage VM instances
- `roles/compute.storageAdmin` -- create disks from snapshots/images
- `roles/iam.serviceAccountUser` -- attach service account to VMs

The cua-house-server host must have `gcloud` authenticated with sufficient permissions, or the service account must be attached to the host VM.

## Image naming conventions

Boot images follow the pattern:

```
agenthle-dev-{variant}-{date}
```

Examples:

- `agenthle-dev-gpu-free-20260403` -- GPU-free Windows image with agent tooling

Images are created by the `admin/bake_image.py` workflow, which provisions a temporary VM, installs tooling, freezes the disk, and creates a GCP image.

## Snapshot naming conventions

Data disk snapshots:

```
agenthle-dev-{variant}-data-snap
```

Boot disk snapshots (fallback when image is unavailable):

```
agenthle-dev-{variant}-boot-snap
```

## Image catalog configuration

Each GCP image entry in `images.yaml` specifies:

```yaml
gpu-free:
  enabled: true
  runtime_mode: gcp
  gcp_project: sunblaze-4
  gcp_zone: us-west1-a
  gcp_network: osworld-vpc
  gcp_service_account: agenthle-vm-service@sunblaze-4.iam.gserviceaccount.com
  gcp_machine_type: g2-standard-4
  gcp_boot_image: agenthle-dev-gpu-free-20260403     # preferred (fast boot)
  gcp_boot_snapshot: null                              # fallback
  gcp_data_snapshot: agenthle-dev-gpu-free-data-snap   # task data disk
  gcp_boot_disk_gb: 64
  gcp_data_disk_gb: 200
  gpu_type: nvidia-l4
  gpu_count: 1
  default_cpu_cores: 4
  default_memory_gb: 16
  max_concurrent_vms: 2
```

- `gcp_boot_image` is preferred over `gcp_boot_snapshot` because creating a VM from an image is significantly faster (~14s vs ~100s).
- `gcp_data_snapshot` is optional. When set, a data disk is created from the snapshot and attached to the VM.

## Data disk management

The data disk contains task input/reference/software files organized by category and task. After the VM boots, the server:

1. Discovers the data disk volume.
2. Reassigns its drive letter to `E:` if needed.
3. Applies NTFS ACL deny rules to restrict the agent to only the current task's allowed directories.

On eval phase, the deny on `reference/` is removed so the evaluator can access ground truth.

## VM lifecycle

1. **Create data disk** from snapshot (if configured).
2. **Create VM** from boot image or snapshot, attach data disk.
3. **Wait for CUA readiness** on `http://{vm_ip}:5000/status`.
4. **Assign drive letter** E: to data disk.
5. **Stage task data** via NTFS ACLs.
6. **Agent uses VM** through lease.
7. **Delete VM** with `--quiet` (auto-deletes all attached disks).

## Orphan cleanup

On server startup, `GCPVMRuntime.cleanup_orphaned_state()` lists all VMs matching `cua-house-env-*` or `agenthle-env-*` across all zones and deletes them.

## Firewall tags

VMs are created with tag `cua-house`. Ensure firewall rules allow ingress on port 5000 from the cua-house-server host to VMs with this tag.

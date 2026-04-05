# cua-house Env Server

`cua-house` carries the extracted host-side VM allocator and reverse proxy that originally lived in AgentHLE's `env_server`.

It runs on a Linux KVM host such as `agenthle-nested-kvm-01`, manages Docker-hosted QEMU Windows guests, and serves assignments back to `run_eval.py` over HTTP.

## Scope

Current scope is intentionally narrow:

- single host only
- host runtime is Docker + QEMU + `/dev/kvm` (local) or GCP VMs (remote)
- public API is one HTTP service port with bearer-token auth for control-plane calls
- environment requests are expressed as:
  - `os_type` from the task definition
  - `image_key`
  - `cpu_cores`
  - `memory_gb`
- logical image catalog supports:
  - `cpu-free`
  - `cpu-license`
  - `gpu-free`
  - `gpu-license`
- currently enabled runtimes: `cpu-free` (local Docker QEMU), `gpu-free` (GCP VM)

## How It Fits the Unified Harness

`run_eval.py` supports two environment modes:

- `static`: direct `vm_ip:cua_port`
- `server`: dynamic allocation through `agenthle-env-server`

When `env.mode: server`, orchestration reads:

```bash
export AGENTHLE_ENV_SERVER_URL=http://<kvm-host>:8787
export AGENTHLE_TOKEN=<shared-token>
```

and then submits one experiment-level batch to env-server.

## Runtime Model

Each logical image has a read-only `golden.qcow2`.

Each live slot has its own runtime directory with:

- `windows.boot`
- `data.qcow2`
- QEMU firmware state
- runtime logs

The VM reset model is QCOW2 backing-file overlay:

- `golden.qcow2` is immutable
- `data.qcow2` is created with a backing path of `/storage/golden.qcow2`
- the real golden image is bind-mounted read-only into the container at that path
- task writes only touch the overlay
- reset means:
  1. stop the current Docker/QEMU container
  2. delete `data.qcow2`
  3. create a fresh overlay on top of the same golden image

This keeps reset cost effectively constant and avoids full disk copies.

## Scheduling Policy

Current scheduler behavior:

- group tasks by `image_key`
- inside a group, prefer smaller `cpu_cores` / `memory_gb`
- start as many tasks concurrently as host capacity allows
- fail impossible requests immediately if they exceed allocatable host capacity

This favors early throughput over strict fairness.

## Cleanup Guarantees

Two failure cases are explicitly handled.

### 1. Orchestration disappears

Each submitted batch has a heartbeat TTL.

- orchestration is expected to refresh it
- if heartbeat expires, env-server marks the batch failed
- any `queued`, `starting`, `ready`, or `leased` tasks are reclaimed
- corresponding Docker/QEMU containers are removed

### 2. Env server restarts

On startup, env-server scans for orphaned state:

- `agenthle-env-*` Docker containers
- stale slot directories

It removes them before accepting new work. The current design intentionally starts fresh after a restart instead of trying to recover old batches.

## API

Main endpoints:

- `POST /v1/batches`
- `GET /v1/batches/{batch_id}`
- `POST /v1/batches/{batch_id}/heartbeat`
- `POST /v1/batches/{batch_id}/cancel`
- `GET /v1/tasks/{task_id}`
- `POST /v1/leases/{lease_id}/heartbeat`
- `POST /v1/leases/{lease_id}/complete`

Assignment payloads include:

- `cua_url`
- `novnc_url`
- `lease_id`
- `slot_id`
- `image_key`

These are leased capability URLs. Public traffic stays on the env-server port; VM CUA/noVNC ports are bound to loopback on the host and are not exposed directly.

## Authentication

Server auth is a shared bearer token:

- server reads `AGENTHLE_TOKEN`
- clients send `Authorization: Bearer <token>`

If `AGENTHLE_TOKEN` is unset, auth is disabled.

## Configuration

Host config:

- `src/cua_house/env_server/configs/agenthle_env_server.yaml`

Image catalog:

- `src/cua_house/env_server/configs/agenthle_env_images.yaml`

Entrypoints:

- `run_cua_house_env_server.py`
- `uv run cua-house-server`

Operational playbooks:

- host provisioning and historical rollout notes still live in the main AgentHLE repository

## Local Development

Run the service:

```bash
uv run python run_cua_house_env_server.py
```

Run env-server tests:

```bash
uv run pytest tests/test_env_server.py -q
```

## Example: Run Eval Against Env Server

```yaml
name: external_batch
runner: external
agent: codex
agent_config: <your agent config>
model: gpt-5.4
max_steps: 80
timeout_s: 1800

env:
  mode: server
  image_key: cpu-free
  cpu_cores: 4
  memory_gb: 16

output:
  root_dir: ./runs

tasks:
  - task_path: ./tasks/demo_web_search
    variance: [0,1]
```

```bash
export AGENTHLE_ENV_SERVER_URL=http://<kvm-host>:8787
export AGENTHLE_TOKEN=<shared-token>
uv run python run_eval.py --exp path/to/external_batch.yaml
```

## Remote Deployment

On `agenthle-nested-kvm-01`, the service is expected to be exposed directly over HTTP through the `agenthle-vpc` network and corresponding GCP firewall rules.

Typical deployment shape:

- host binds `0.0.0.0:8787`
- firewall allows only `tcp:8787`
- orchestration machines call the control plane with `AGENTHLE_ENV_SERVER_URL`
- env-server returns leased `cua_url` / `novnc_url` values that route back through the same public port

The recommended runtime manager for the host is `systemd`, not `tmux`.

For the original machine-building and rollout history, refer back to the AgentHLE monorepo docs.

## Events and Metrics

Env-server is the source of truth for host capacity and VM lifecycle timing. The canonical host log is:

- `<runtime_root>/events.jsonl`

Important event names:

- `batch_submitted`
- `task_queued`
- `task_starting`
- `slot_vm_ip_detected`
- `slot_windows_started`
- `slot_ready`
- `lease_complete_requested`
- `slot_reset_completed`

Important timing fields:

- `queue_wait_s`
- `windows_boot_s`
- `computer_server_wait_s`
- `total_ready_s`
- `reset_s`
- `duration_s` on `task_data_stage_completed`

## Task Data Disk and Staging

For real benchmark tasks, env-server can stage task data from a host-mounted data disk into a clean Windows overlay.

- fixed host root:
  - `task_data_root` from the host config
- fixed layout:
  - `<task_data_root>/<task_category>/<task_tag>/input`
  - `<task_data_root>/<task_category>/<task_tag>/software`
  - `<task_data_root>/<task_category>/<task_tag>/reference`
- fixed phases:
  - `POST /v1/leases/{lease_id}/stage-runtime`
  - `POST /v1/leases/{lease_id}/stage-eval`

### Local Docker QEMU Staging (Samba Bind Mount)

For local Docker QEMU VMs, staging uses Samba bind mounts instead of HTTP upload:

1. at `prepare_slot()` time, the task's `source_relpath` is resolved to a host
   directory under `task_data_root` and stored on the `SlotHandle` as
   `task_data_source_root`
2. `start_slot()` adds Docker bind mounts for the task's `input/` and
   `software/` directories into the container's `/shared/` directory (read-only)
3. the base image (dockur/windows) exposes `/shared/` as a Samba share at
   `\\host.lan\Data` inside the Windows guest
4. `stage-runtime` runs PowerShell `robocopy` inside the guest from
   `\\host.lan\Data\input` and `\\host.lan\Data\software` to the task's
   canonical Windows paths — no zip, no base64, no HTTP transfer
5. `stage-eval` uses `docker cp` to copy `reference/` from the host into the
   running container's `/shared/reference/`, then `robocopy` inside the guest

Reference data is never bind-mounted at container start, so the agent cannot
discover it during the runtime phase.

The legacy HTTP upload path (`_stage_directory_legacy`) is retained as a
code-level fallback but is not used in the default staging flow.

### GCP VM Staging (NTFS ACLs)

For GCP VMs, task data lives on a pre-attached data disk inside the guest.
Staging uses NTFS ACLs to control visibility: non-whitelisted dirs are denied
during runtime, and reference is unlocked at eval time. No file transfer is
needed.

### Host Requirements

On the host, the Samba staging path assumes:

- a dedicated task-data disk is mounted
- `task_data_root` points at that mount
- the host VM service account can read `gs://agenthle`

Recommended host bootstrap:

```bash
sudo mkdir -p /mnt/agenthle-task-data
gcloud storage rsync --recursive gs://agenthle /mnt/agenthle-task-data
```

For the original host-side provisioning flow, including disk mount and service
account requirements, refer back to the AgentHLE monorepo docs.

For GCE, the service account should at minimum have:

- `roles/storage.objectViewer`
- `roles/storage.legacyBucketReader`

`stage-runtime` prepares `input/`, `software/`, and an empty output directory. `stage-eval` copies `reference/` immediately before evaluation.

Important task-data events:

- `task_data_validated`
- `task_data_validation_failed`
- `task_data_stage_started`
- `task_data_stage_completed`
- `task_data_stage_failed`

Use env-server metrics for:

- host capacity planning
- queueing pressure
- cold boot latency
- reset latency
- slot utilization

Use orchestration and runner logs for agent-side timing. The two sides should be joined by `batch_id`, `task_id`, and `lease_id`.

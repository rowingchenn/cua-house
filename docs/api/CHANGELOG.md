# cua-house API CHANGELOG

## 2026-04-21 — ephemeral-VM refactor (commit `52142e8`)

The pool-era API surface collapses into a simpler task-per-VM model.
Consumers of the HTTP / WS APIs must migrate as follows.

### Breaking: deleted endpoints

| Endpoint | Previous purpose | Replacement |
|---|---|---|
| `GET /v1/cluster/pool` | Read desired `ClusterPoolSpec` assignments | *(no replacement — desired-state concept removed)* |
| `PUT /v1/cluster/pool` | Set `ClusterPoolSpec` assignments | *(no replacement)* |

If a dashboard or automation reads `/v1/cluster/pool`, update it to read
`/v1/cluster/status` + `/v1/cluster/workers` — those carry the live
capacity info the pool spec used to imply.

### Breaking: removed fields

**`GET /v1/cluster/workers`**

| Removed | Why |
|---|---|
| `hosted_images` | Every enabled image is guaranteed local after worker prewarm; field was always the enabled set |
| `load_cpu`, `load_memory` | Always-zero TODO fields; never implemented |
| `vm_summaries[i].state`, `vm_summaries[i].warming` | Only lease-bound VMs exist; `state` was always "leased" |

**`GET /v1/cluster/status`**

| Removed | Why |
|---|---|
| `vm_ready` | No READY pool in the ephemeral model |
| `vm_warming` | Cold-boots are in-flight task state, surface via `tasks_by_state.starting` |
| `pool_assignments` | No pool spec |

### New fields

**`GET /v1/cluster/workers`** adds per-worker live capacity + cache view:

```
free_vcpus, free_memory_gb, active_task_count, cached_shapes[]
```

Where `cached_shapes[i]` is `{image_key, image_version, vcpus, memory_gb, disk_gb}`.

**`GET /v1/cluster/status`** adds cluster-wide totals:

```
cluster_total_vcpus, cluster_total_memory_gb,
cluster_free_vcpus,  cluster_free_memory_gb
```

`vm_from_cache` semantics unchanged — counts VMs that started via `loadvm`.

### Unchanged (no migration needed)

* `POST /v1/batches`, `GET /v1/batches/{id}`,
  `POST /v1/batches/{id}/heartbeat`, `POST /v1/batches/{id}/cancel`
* `GET /v1/tasks/{id}`
* `POST /v1/leases/{id}/heartbeat`, `POST /v1/leases/{id}/complete`,
  `POST /v1/leases/{id}/stage-runtime`, `POST /v1/leases/{id}/stage-eval`
* `GET /v1/cluster/tasks`, `GET /v1/cluster/batches`
* `GET /healthz`, `GET /v1/vms`
* Worker-bound WebSocket `/v1/cluster/ws`: wire format preserved
  (payloads use `AssignTask` / `TaskBound` / `TaskCompleted` /
  `ReleaseLease` / `Heartbeat` / `Register` only — `PoolOp` /
  `PoolOpResult` / `VMStateUpdate` types are no longer emitted and
  would be ignored by the new master).

### New failure mode

Tasks whose `(vcpus, memory_gb)` exceed the *single-machine* maximum of
every online worker fail at admission time with:

```json
{"error": "no_worker_fits: task requires X vCPU / Y GiB but largest online worker offers A vCPU / B GiB"}
```

Task state is set to `failed` immediately; the batch proceeds with the
remaining (admissible) tasks. Previously the task would queue forever.

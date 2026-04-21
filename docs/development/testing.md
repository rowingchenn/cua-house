# Testing

## Test organization

Tests live in `packages/server/tests/`:

| Path | Type | What it tests |
|------|------|---------------|
| `cluster/test_protocol.py` | Unit | Envelope + discriminated-union roundtrips for every WS message kind (Register, Heartbeat, AssignTask, TaskBound, TaskCompleted, ReleaseLease, Shutdown) |
| `cluster/test_registry.py` | Unit | `WorkerRegistry` register / heartbeat / cached_shapes / reap_stale / cache-affinity matching |
| `cluster/test_register_frame.py` | Unit | `WorkerClusterClient.build_register_frame` (pure, no network) |
| `test_api_auth.py` | Integration | Bearer-token gating on HTTP endpoints |

The pool-era tests (`test_scheduler.py`, `test_vm_pool_smoke.py`,
`test_dispatcher.py`, `test_worker_pool_ops.py`, `test_pool_reconciler.py`,
`test_pool_spec.py`) were deleted in the ephemeral-VM refactor — they all
targeted the VMState machine / VMPoolEntry / PoolReconciler surface
that no longer exists. Dispatcher coverage should be rebuilt against
the new `ClusterDispatcher` (event-driven placement, capacity ledger,
admission check, cache affinity). Open work item.

## Running tests

```bash
uv run pytest packages/ -v
```

27 tests, no network or Docker required. CI runs the same command.

## End-to-end verification

For the full ephemeral-VM path (provision_vm → bind → destroy_vm +
cache hit on second task + admission reject + worker-disconnect
requeue), submit against a live master + worker and watch the state
transitions. The script below was used to verify the 2026-04-21
deployment; adapt the endpoint for future runs:

```bash
MASTER=http://<master-ip>:8787
B=$(python3 -c 'import uuid;print(uuid.uuid4())')
T=$(python3 -c 'import uuid;print(uuid.uuid4())')
curl -sS -X POST $MASTER/v1/batches \
  -H 'Content-Type: application/json' \
  -d "{\"batch_id\":\"$B\",\"tasks\":[{\"task_id\":\"$T\",\"task_path\":\"/smoke\",
       \"snapshot_name\":\"cpu-free-ubuntu\",\"vcpus\":4,\"memory_gb\":8,\"disk_gb\":64}]}"
# Poll until state=ready; first same-shape task cold-boots ~5min,
# subsequent same-shape tasks resume via loadvm ~30s.
until curl -sS $MASTER/v1/tasks/$T | grep -q '"state":"ready"'; do sleep 5; done
# Complete and verify destroy_vm + capacity release.
L=$(curl -sS $MASTER/v1/tasks/$T | python3 -c 'import sys,json;print(json.load(sys.stdin)["lease_id"])')
curl -sS -X POST $MASTER/v1/leases/$L/complete -H 'Content-Type: application/json' \
  -d '{"final_status":"completed"}'
curl -sS $MASTER/v1/cluster/workers | python3 -m json.tool  # cached_shapes should now include the shape
```

For admission check:

```bash
curl -sS -X POST $MASTER/v1/batches -H 'Content-Type: application/json' \
  -d '{"tasks":[{"task_id":"oversized","task_path":"/x","snapshot_name":"cpu-free",
                 "vcpus":100,"memory_gb":500,"disk_gb":64}]}'
# Expected: task.state == "failed", error starts with "no_worker_fits"
```

## Writing new tests

### Protocol roundtrips

`cluster/test_protocol.py` is the template. Build a message, dump it,
validate through the `WorkerToMaster` / `MasterToWorker` adapter, and
assert the `kind` field survives. No async required.

### Registry / dispatcher unit tests

Registry tests use a `_FakeWS` with `send_json` accumulating into a
list. Dispatcher tests (to be written) should follow the same pattern:
wire a `WorkerRegistry` + `RpcCoordinator` + fake worker that replies
`TaskBound(ok=True)` to `AssignTask` envelopes. The end-to-end
verification script (previous section) is a good reference for the
expected state transitions to cover.

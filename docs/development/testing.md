# Testing

## Test organization

Tests live in `packages/server/tests/`:

| File | Type | What it tests |
|------|------|---------------|
| `test_scheduler.py` | Unit | Scheduler logic, port pool, API auth, runtime prepare/cleanup |
| `test_vm_pool_smoke.py` | Smoke | Real VM pool init with Docker and KVM (not run in CI) |
| `conftest.py` | Fixtures | Shared test fixtures |

## Unit tests

Unit tests use `FakeRuntime`, a mock implementation of the `RuntimeBackend` protocol that exercises the scheduler without Docker, QEMU, or KVM.

`FakeRuntime` is defined in `test_scheduler.py` and provides:

- `prepare_slot` returns a mock handle object
- `start_slot` returns immediately (or with a configurable delay in `SlowFakeRuntime`)
- `reset_slot` is a no-op
- `stage_task_phase` records calls and returns mock results

### What unit tests cover

- Default resource allocation from image spec
- Task grouping by image key
- Impossible resource requests (immediate FAILED)
- Lease completion releases slots
- Task data staging (runtime and eval phases)
- Lease reaper marks expired leases as FAILED
- Starting tasks are not reaped before they become READY
- Multiple small tasks start concurrently
- Batch cancellation
- Batch heartbeat expiry
- Runtime orphan cleanup
- Overlay backing file resolution (symlink handling)
- API bearer token authentication

### Running unit tests

```bash
uv run pytest packages/server/tests/test_scheduler.py -v
```

## Integration tests

API integration tests use `fastapi.testclient.TestClient` to exercise HTTP endpoints. See `test_api_requires_bearer_token` for an example that creates a real `FastAPI` app with the default configs and verifies 401/200 responses.

```bash
uv run pytest -k test_api -v
```

## Smoke tests

`test_vm_pool_smoke.py` tests the real VM pool lifecycle on a KVM host:

- Docker container creation
- QEMU boot
- CUA readiness polling
- QMP snapshot save/load
- Task assignment and revert

These tests require:

- `/dev/kvm` accessible
- Docker running
- Golden QCOW2 image at the configured path
- Sufficient CPU/RAM for at least one VM

They are skipped automatically when KVM is not available. To run explicitly:

```bash
uv run pytest packages/server/tests/test_vm_pool_smoke.py -v --timeout=600
```

## Writing new tests

### For scheduler logic

Use `make_scheduler()` from `test_scheduler.py`:

```python
def test_my_scenario() -> None:
    async def scenario() -> None:
        scheduler = make_scheduler()
        await scheduler.start()
        # ... submit batch, wait for assignment, assert ...
        await scheduler.shutdown()

    asyncio.run(scenario())
```

### For API endpoints

Use `TestClient` with a real app:

```python
from fastapi.testclient import TestClient
from cua_house_server.api.app import create_app

app = create_app(host_config_path=..., image_catalog_path=...)
client = TestClient(app)
response = client.post("/v1/batches", json={...})
```

### For a new runtime backend

Create a fake version of the backend (like `FakeRuntime`) and pass it to the scheduler. This allows testing scheduler interaction without real infrastructure.

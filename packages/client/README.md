# cua-house-client

Async Python client SDK for interacting with a cua-house server.

## Installation

Installed automatically as part of the workspace:

```bash
cd cua-house
uv sync
```

## Configuration

The client reads from environment variables or accepts explicit parameters:

| Parameter | Environment Variable | Legacy Variable | Description |
|-----------|---------------------|-----------------|-------------|
| `base_url` | `CUA_HOUSE_SERVER_URL` | `AGENTHLE_ENV_SERVER_URL` | Server URL (required) |
| `token` | `CUA_HOUSE_TOKEN` | `AGENTHLE_TOKEN` | Bearer token (optional) |
| `timeout` | -- | -- | Request timeout in seconds (default: 30) |

## Usage

```python
import asyncio
from cua_house_client import EnvServerClient
from cua_house_common import BatchCreateRequest, TaskRequirement

async def main():
    client = EnvServerClient(base_url="http://localhost:8787")
    try:
        # Submit a batch
        request = BatchCreateRequest(
            tasks=[
                TaskRequirement(
                    task_id="task-001",
                    task_path="tasks/my_task",
                    image_key="cpu-free",
                ),
            ]
        )
        batch = await client.submit_batch(request)
        batch_id = batch["batch_id"]
        print(f"Batch submitted: {batch_id}")

        # Poll for task readiness
        task = await client.get_task("task-001")
        print(f"Task state: {task['state']}")

        # Send heartbeats to keep the lease alive
        if task.get("lease_id"):
            hb = await client.heartbeat(task["lease_id"])
            print(f"Lease expires at: {hb['expires_at']}")

        # Stage task data for runtime phase
        if task.get("assignment"):
            lease_id = task["assignment"]["lease_id"]
            await client.stage_runtime(lease_id)

            # ... agent does work ...

            # Stage eval data
            await client.stage_eval(lease_id)

            # Complete the lease
            await client.complete(lease_id, final_status="completed")

        # Or cancel the batch
        # await client.cancel_batch(batch_id, reason="no longer needed")
    finally:
        await client.aclose()

asyncio.run(main())
```

## API methods

| Method | Endpoint | Description |
|--------|----------|-------------|
| `submit_batch(request)` | `POST /v1/batches` | Submit a batch of tasks |
| `get_batch(batch_id)` | `GET /v1/batches/{id}` | Get batch status |
| `heartbeat_batch(batch_id)` | `POST /v1/batches/{id}/heartbeat` | Refresh batch TTL |
| `cancel_batch(batch_id)` | `POST /v1/batches/{id}/cancel` | Cancel a batch |
| `get_task(task_id)` | `GET /v1/tasks/{id}` | Get task status |
| `heartbeat(lease_id)` | `POST /v1/leases/{id}/heartbeat` | Refresh lease TTL |
| `complete(lease_id)` | `POST /v1/leases/{id}/complete` | Complete a lease |
| `stage_runtime(lease_id)` | `POST /v1/leases/{id}/stage-runtime` | Stage task data for runtime phase |
| `stage_eval(lease_id)` | `POST /v1/leases/{id}/stage-eval` | Stage task data for eval phase |

## Dependencies

- `cua-house-common`
- `httpx>=0.27.0`

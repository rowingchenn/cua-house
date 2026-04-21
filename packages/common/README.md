# cua-house-common

Shared models and utilities used by both `cua-house-server` and `cua-house-client`.

## What it provides

### State enums

- `TaskState` -- lifecycle states for tasks: `QUEUED`, `STARTING`, `READY`, `LEASED`, `RESETTING`, `COMPLETED`, `FAILED`
- `BatchState` -- lifecycle states for batches: `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`

### Request models

- `TaskRequirement` -- specifies a task's resource needs, image key, and optional task data configuration
- `TaskRequirement.TaskDataRequest` -- nested model for task data staging parameters (source path, input/software/reference/output directories)
- `BatchCreateRequest` -- list of `TaskRequirement` items to submit as a batch
- `LeaseCompleteRequest` -- signal task completion with status and details
- `BatchCancelRequest` -- cancel a batch with a reason

### Response models

- `TaskStatus` -- full task state including assignment, timestamps, and error info
- `BatchStatus` -- batch state with list of task statuses
- `TaskAssignment` -- connection info for a leased VM (CUA URL, noVNC URL, lease ID, slot ID)
- `LeaseHeartbeatResponse` -- heartbeat acknowledgment with new expiry time
- `BatchHeartbeatResponse` -- batch heartbeat acknowledgment
- `LeaseStageResponse` -- result of task data staging (file count, bytes staged)

### Utilities

- `JsonlEventLogger` -- thread-safe append-only JSONL event logger
- `utcnow()` -- timezone-aware UTC datetime helper

## Usage

```python
from cua_house_common import (
    BatchCreateRequest,
    TaskRequirement,
    TaskState,
    TaskStatus,
    JsonlEventLogger,
)

# Create a batch request
request = BatchCreateRequest(
    tasks=[
        TaskRequirement(
            task_id="task-001",
            task_path="tasks/my_task",
            image_key="cpu-free",
        ),
    ]
)

# Check task state
assert TaskState.QUEUED == "queued"

# Log events
logger = JsonlEventLogger(Path("events.jsonl"), component="my_component")
logger.emit("task_started", task_id="task-001")
```

## Dependencies

- `pydantic>=2.8.0`

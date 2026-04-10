"""Example: submit a batch, poll for readiness, stage data, and complete."""

from __future__ import annotations

import asyncio
import os

from cua_house_client import EnvServerClient
from cua_house_common import BatchCreateRequest, TaskRequirement


async def main() -> None:
    server_url = os.environ.get("CUA_HOUSE_SERVER_URL", "http://localhost:8787")
    client = EnvServerClient(base_url=server_url)

    try:
        # 1. Submit a batch with one task
        request = BatchCreateRequest(
            tasks=[
                TaskRequirement(
                    task_id="example-task-001",
                    task_path="tasks/demo",
                    image_key="cpu-free",
                    cpu_cores=4,
                    memory_gb=16,
                ),
            ]
        )
        batch = await client.submit_batch(request)
        batch_id = batch["batch_id"]
        print(f"batch submitted: {batch_id}")

        # 2. Poll until task is ready
        while True:
            task = await client.get_task("example-task-001")
            state = task["state"]
            print(f"task state: {state}")

            if state == "ready" or state == "leased":
                break
            if state in ("completed", "failed"):
                print(f"task ended early: {task.get('error', 'no error')}")
                return

            # Send batch heartbeat to keep it alive
            await client.heartbeat_batch(batch_id)
            await asyncio.sleep(5)

        # 3. Get assignment info
        assignment = task["assignment"]
        lease_id = assignment["lease_id"]
        urls = assignment["urls"]
        novnc_url = assignment.get("novnc_url")
        print(f"lease: {lease_id}")
        print(f"urls: {urls}")
        print(f"novnc url: {novnc_url}")

        # 4. Stage task data for runtime phase (if task has task_data)
        stage_result = await client.stage_runtime(lease_id)
        print(f"staged: {stage_result}")

        # 5. Keep the lease alive while the agent works
        for _ in range(10):
            hb = await client.heartbeat(lease_id)
            print(f"heartbeat ok, expires: {hb['expires_at']}")
            await asyncio.sleep(10)

        # 6. Stage eval data (unlocks reference directory)
        eval_result = await client.stage_eval(lease_id)
        print(f"eval staged: {eval_result}")

        # 7. Complete the lease
        await client.complete(lease_id, final_status="completed", details={"score": 0.95})
        print("lease completed")

    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())

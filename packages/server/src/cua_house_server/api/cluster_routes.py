"""Read-only JSON API for the master cluster control plane.

Mounted only in master mode. Exposes workers + tasks + batches + an
aggregate cluster status view for operators and the monitoring dashboard.

No mutation endpoints: the old ``GET/PUT /v1/cluster/pool`` pair is gone
along with the ``ClusterPoolSpec`` concept — in the ephemeral-VM model
there is no desired-pool-state to read or set, only a task queue that
drains onto workers on demand.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from cua_house_common.models import BatchState, TaskState
from cua_house_server.cluster.dispatcher import ClusterDispatcher
from cua_house_server.cluster.registry import WorkerRegistry


def build_cluster_api_router(
    *,
    registry: WorkerRegistry,
    dispatcher: ClusterDispatcher,
) -> APIRouter:
    router = APIRouter(prefix="/v1/cluster")

    @router.get("/workers")
    async def list_workers() -> list[dict[str, Any]]:
        sessions = await registry.snapshot()
        result = []
        for s in sessions:
            free_cpu, free_mem = dispatcher._worker_free_capacity(s)
            active = len(dispatcher._worker_load.get(s.worker_id, {}))
            result.append(
                {
                    "worker_id": s.worker_id,
                    "online": s.online,
                    "runtime_version": s.runtime_version,
                    "capacity": s.capacity.model_dump(),
                    "free_vcpus": free_cpu,
                    "free_memory_gb": free_mem,
                    "active_task_count": active,
                    "connected_at": s.connected_at,
                    "last_heartbeat": s.last_heartbeat,
                    "vm_summaries": [vm.model_dump() for vm in s.vm_summaries],
                    "cached_shapes": [cs.model_dump() for cs in s.cached_shapes],
                }
            )
        return result

    @router.get("/tasks")
    async def list_tasks(state: str | None = None) -> list[dict[str, Any]]:
        parsed: TaskState | None = None
        if state is not None:
            try:
                parsed = TaskState(state)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid state: {state!r}. valid: {[s.value for s in TaskState]}",
                ) from exc
        tasks = await dispatcher.list_tasks(state=parsed)
        return [t.model_dump(mode="json") for t in tasks]

    @router.get("/batches")
    async def list_batches(state: str | None = None) -> list[dict[str, Any]]:
        parsed: BatchState | None = None
        if state is not None:
            try:
                parsed = BatchState(state)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid state: {state!r}. valid: {[s.value for s in BatchState]}",
                ) from exc
        batches = await dispatcher.list_batches(state=parsed)
        return [b.model_dump(mode="json") for b in batches]

    @router.get("/status")
    async def cluster_status() -> dict[str, Any]:
        sessions = await registry.snapshot()
        online_sessions = [s for s in sessions if s.online]
        all_vms = [vm for s in online_sessions for vm in s.vm_summaries]
        tasks = await dispatcher.list_tasks()
        task_state_counts = {s.value: 0 for s in TaskState}
        for t in tasks:
            task_state_counts[t.state.value] = task_state_counts.get(t.state.value, 0) + 1

        cluster_total_vcpus = sum(s.capacity.total_vcpus for s in online_sessions)
        cluster_total_memory_gb = sum(
            s.capacity.total_memory_gb for s in online_sessions
        )
        cluster_free_vcpus = 0
        cluster_free_memory_gb = 0
        for s in online_sessions:
            free_cpu, free_mem = dispatcher._worker_free_capacity(s)
            cluster_free_vcpus += free_cpu
            cluster_free_memory_gb += free_mem

        return {
            "workers_total": len(sessions),
            "workers_online": len(online_sessions),
            "vm_instances": len(all_vms),
            "vm_from_cache": sum(1 for vm in all_vms if vm.from_cache),
            "cluster_total_vcpus": cluster_total_vcpus,
            "cluster_total_memory_gb": cluster_total_memory_gb,
            "cluster_free_vcpus": cluster_free_vcpus,
            "cluster_free_memory_gb": cluster_free_memory_gb,
            "tasks_total": len(tasks),
            "tasks_by_state": task_state_counts,
        }

    return router

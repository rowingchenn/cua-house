"""Read/write JSON API for the master cluster control plane.

Only mounted when the process runs in master mode. Exposes enough surface
for operators (and a future dashboard) to inspect workers, read/set the
desired pool spec, and poke the reconciler.

Desired state is in-memory for Phase 2 — a future PR can back it with a
persistent store (sqlite, consul, etc.) without changing the route shape.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from cua_house_server.cluster.pool_spec import ClusterPoolSpec, PoolAssignment
from cua_house_server.cluster.registry import WorkerRegistry


class PoolAssignmentModel(BaseModel):
    worker_id: str
    image_key: str
    count: int
    vcpus: int
    memory_gb: int
    disk_gb: int = 64


class PoolSpecModel(BaseModel):
    assignments: list[PoolAssignmentModel]


def build_cluster_api_router(
    *,
    registry: WorkerRegistry,
    pool_spec: ClusterPoolSpec,
) -> APIRouter:
    router = APIRouter(prefix="/v1/cluster")

    @router.get("/workers")
    async def list_workers() -> list[dict[str, Any]]:
        sessions = await registry.snapshot()
        return [
            {
                "worker_id": s.worker_id,
                "online": s.online,
                "runtime_version": s.runtime_version,
                "capacity": s.capacity.model_dump(),
                "hosted_images": sorted(s.hosted_images),
                "load_cpu": s.load_cpu,
                "load_memory": s.load_memory,
                "connected_at": s.connected_at,
                "last_heartbeat": s.last_heartbeat,
                "vm_summaries": [vm.model_dump() for vm in s.vm_summaries],
            }
            for s in sessions
        ]

    @router.get("/pool")
    async def get_pool() -> PoolSpecModel:
        return PoolSpecModel(
            assignments=[
                PoolAssignmentModel(
                    worker_id=a.worker_id,
                    image_key=a.image_key,
                    count=a.count,
                    vcpus=a.vcpus,
                    memory_gb=a.memory_gb,
                    disk_gb=a.disk_gb,
                )
                for a in pool_spec.assignments
            ]
        )

    @router.put("/pool")
    async def put_pool(spec: PoolSpecModel) -> PoolSpecModel:
        pool_spec.set(
            [
                PoolAssignment(
                    worker_id=a.worker_id,
                    image_key=a.image_key,
                    count=a.count,
                    vcpus=a.vcpus,
                    memory_gb=a.memory_gb,
                    disk_gb=a.disk_gb,
                )
                for a in spec.assignments
            ]
        )
        return spec

    @router.get("/status")
    async def cluster_status() -> dict[str, Any]:
        sessions = await registry.snapshot()
        online = sum(1 for s in sessions if s.online)
        total_vms = sum(len(s.vm_summaries) for s in sessions)
        return {
            "workers_total": len(sessions),
            "workers_online": online,
            "vm_instances": total_vms,
            "pool_assignments": len(pool_spec.assignments),
        }

    return router

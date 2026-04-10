"""Runtime backend protocol for cua-house-server."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from cua_house_common.models import TaskRequirement
from cua_house_server.config.loader import ImageSpec
from cua_house_server.data.staging import StageResult


@runtime_checkable
class RuntimeBackend(Protocol):
    """Interface that all runtime backends (QEMU, GCP, etc.) must implement."""

    def cleanup_orphaned_state(self) -> None:
        """Remove any leftover state from previous server runs."""
        ...

    def prepare_slot(
        self,
        *,
        slot_id: str,
        image: ImageSpec,
        cpu_cores: int,
        memory_gb: int,
        cua_port: int,
        novnc_port: int,
        lease_id: str,
        task_id: str,
        task_data: TaskRequirement.TaskDataRequest | None = None,
    ) -> Any:
        """Prepare a slot for a task (create overlay, directories, etc.)."""
        ...

    async def start_slot(self, handle: Any) -> None:
        """Start the slot (boot VM, wait for readiness)."""
        ...

    async def reset_slot(self, handle: Any, image: ImageSpec) -> None:
        """Reset/destroy a slot after task completion."""
        ...

    def vm_published_url(self, handle: Any, guest_port: int) -> str:
        """Return the local URL for a specific published guest port."""
        ...

    def novnc_local_url(self, handle: Any) -> str:
        """Return the local noVNC URL for a slot."""
        ...

    def validate_runtime_task_data(
        self,
        *,
        task_id: str,
        task_data: TaskRequirement.TaskDataRequest | None,
    ) -> None:
        """Validate that task data is available before scheduling."""
        ...

    async def stage_task_phase(
        self,
        *,
        handle: Any,
        task_id: str,
        lease_id: str,
        task_data: TaskRequirement.TaskDataRequest | None,
        phase: str,
        container_name: str | None = None,
        os_family: str | None = None,
    ) -> StageResult:
        """Stage task data into the guest for a given phase."""
        ...

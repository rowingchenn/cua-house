"""SlotProvider abstraction for multi-backend task dispatch.

A ``SlotProvider`` is the seam between the task scheduler (which decides what
to run) and the execution backend (which decides where and how). Today the
scheduler is hard-wired against ``DockerQemuRuntime`` and ``GCPVMRuntime``;
introducing this interface lets Phase 2 plug in a ``WorkerSlotProvider`` that
forwards assignments over a WebSocket to a remote worker node, without the
scheduler caring about the difference.

Phase 1 defines the protocol and adapters over the existing runtimes. The
scheduler is NOT rewired to use it yet — that rewire is part of Phase 2, done
once alongside the WorkerSlotProvider so it can be validated against all
three implementations at the same time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from cua_house_server.config.loader import ImageSpec
from cua_house_server.runtimes.gcp import GCPVMRuntime
from cua_house_server.runtimes.qemu import DockerQemuRuntime


@dataclass
class SlotCandidate:
    """A provider's report that it can (and wants to) run a given task.

    ``load`` is a provider-defined scalar used by the scheduler to break ties
    between multiple eligible providers. Lower means prefer this provider.
    """

    provider_id: str
    slot_ref: Any  # opaque pointer into the provider's internal state
    load: float


@runtime_checkable
class SlotProvider(Protocol):
    """A source of task execution slots.

    Implementations wrap a concrete runtime backend (local docker/qemu, GCP
    on-demand VM, remote worker over WS) and expose a uniform surface to the
    scheduler.
    """

    provider_id: str

    def supports_image(self, image_key: str) -> bool:
        """True if this provider can run tasks for the given image."""
        ...

    async def find_candidate(
        self,
        *,
        image: ImageSpec,
        cpu_cores: int,
        memory_gb: int,
    ) -> SlotCandidate | None:
        """Return a candidate slot if one can be offered right now."""
        ...

    async def assign(
        self,
        candidate: SlotCandidate,
        *,
        task_id: str,
        lease_id: str,
        image: ImageSpec,
    ) -> Any:
        """Commit the candidate to a concrete task and return a handle."""
        ...

    async def release(
        self,
        handle: Any,
        *,
        final_status: str,
    ) -> None:
        """Return the slot to the pool (or tear it down)."""
        ...


class LocalSlotProvider:
    """Adapter exposing ``DockerQemuRuntime`` as a ``SlotProvider``.

    Phase 1 leaves the scheduler in direct control of the runtime; this
    adapter is defined so Phase 2 can swap it in without changing its shape.
    """

    provider_id = "local"

    def __init__(self, runtime: DockerQemuRuntime) -> None:
        self.runtime = runtime

    def supports_image(self, image_key: str) -> bool:  # pragma: no cover - phase 2
        raise NotImplementedError("wired in Phase 2 alongside scheduler refactor")


class GCPOverflowSlotProvider:
    """Adapter exposing ``GCPVMRuntime`` as a ``SlotProvider``.

    Used by master for GPU/overflow fallback when no worker can host an image.
    """

    provider_id = "gcp"

    def __init__(self, runtime: GCPVMRuntime) -> None:
        self.runtime = runtime

    def supports_image(self, image_key: str) -> bool:  # pragma: no cover - phase 2
        raise NotImplementedError("wired in Phase 2 alongside scheduler refactor")

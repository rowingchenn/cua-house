"""WebSocket wire protocol between master and worker.

All traffic on ``/v1/cluster/ws`` is encoded as JSON ``Envelope`` objects.
The ``payload`` field is a discriminated union on ``kind``; new message types
should be added to ``WorkerToMaster`` or ``MasterToWorker`` below.

Design notes:

* The protocol is symmetric at the transport level (both sides send and
  receive Envelopes) but the payload unions are directional — ``Register``
  only flows from worker, ``AssignTask`` only from master, etc. This lets
  static type checkers catch misuse.
* ``msg_id`` is monotonic per side used for correlating responses to
  requests (e.g. ``TaskBound.correlation_id == AssignTask.msg_id``).
  Fire-and-forget messages leave ``correlation_id`` null.
* We intentionally avoid gRPC. FastAPI already pulls in ``starlette``'s
  WebSocket support and Pydantic handles schema; adding protobuf tooling
  would be overkill for an internal-VPC protocol.

Ephemeral-VM model: an ``AssignTask`` is the only VM-lifecycle message
master sends. The worker provisions a VM on receipt, binds the task, and
replies with ``TaskBound`` (carrying the worker-assigned ``vm_id`` +
``from_cache`` flag for telemetry). ``ReleaseLease`` tells the worker to
destroy the VM for a given lease.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

# ── Shared sub-models ───────────────────────────────────────────────────


class WorkerCapacity(BaseModel):
    """Static resource ceiling reported at registration."""

    total_vcpus: int
    total_memory_gb: int
    total_disk_gb: int
    reserved_vcpus: int = 0
    reserved_memory_gb: int = 0


class CachedShape(BaseModel):
    """A single `(image, version, shape)` entry present in a worker's snapshot cache.

    Reported in heartbeats so master can rank workers by cache affinity at
    placement time: a task whose shape matches a `CachedShape` on worker W
    resumes via loadvm (~30s) instead of cold-boot (~5min).
    """

    image_key: str
    image_version: str
    vcpus: int
    memory_gb: int
    disk_gb: int


class WorkerVMSummary(BaseModel):
    """Snapshot of a single in-flight VM on a worker, sent with heartbeats.

    Every VM is bound to a task in the ephemeral-VM model; `lease_id` is
    therefore always populated. Kept as a flat list (not keyed) so the
    wire format survives a worker restarting in the middle of the batch.
    """

    vm_id: str
    image_key: str
    image_version: str = "unversioned"
    vcpus: int
    memory_gb: int
    disk_gb: int
    from_cache: bool = False
    lease_id: str | None = None
    # Public reach info — used by master to construct TaskAssignment.urls so
    # clients can talk to the VM directly (no double-hop through master).
    public_host: str | None = None
    published_ports: dict[int, int] = Field(default_factory=dict)
    novnc_port: int | None = None


# ── Worker → Master payloads ────────────────────────────────────────────


class Register(BaseModel):
    kind: Literal["register"] = "register"
    worker_id: str
    runtime_version: str
    capacity: WorkerCapacity
    hosted_images: list[str] = Field(default_factory=list)


class Heartbeat(BaseModel):
    kind: Literal["heartbeat"] = "heartbeat"
    vm_summaries: list[WorkerVMSummary] = Field(default_factory=list)
    cached_shapes: list[CachedShape] = Field(default_factory=list)


class TaskBound(BaseModel):
    """Worker confirms a task has been provisioned + bound.

    Reply to ``AssignTask``. ``vm_id`` is worker-assigned (master doesn't
    track VMs). ``from_cache=True`` means the VM started via loadvm from
    an already-warmed cache entry; False means cold-boot + savevm wrote a
    new cache entry for the shape.
    """

    kind: Literal["task_bound"] = "task_bound"
    task_id: str
    lease_id: str
    vm_id: str = ""
    ok: bool = True
    error: str | None = None
    from_cache: bool = False
    lease_endpoint: str | None = None
    urls: dict[int, str] = Field(default_factory=dict)
    novnc_url: str | None = None


class TaskCompleted(BaseModel):
    """Worker reports a lease has terminated (destroy_vm done).

    Fires in two situations:

    * Client-driven: client POSTs ``/v1/leases/{id}/complete`` to the
      worker's lease API; the worker runs destroy_vm and sends this.
    * Master-driven: ``ReleaseLease`` cancel path finishes the same way.

    ``final_status`` is ``completed`` | ``failed`` | ``abandoned``.
    """

    kind: Literal["task_completed"] = "task_completed"
    task_id: str
    lease_id: str
    final_status: str
    error: str | None = None


class TaskReleased(BaseModel):
    """Acknowledgment for a master-initiated ReleaseLease RPC."""

    kind: Literal["task_released"] = "task_released"
    lease_id: str
    ok: bool = True
    error: str | None = None


class TaskPhaseResult(BaseModel):
    kind: Literal["task_phase_result"] = "task_phase_result"
    lease_id: str
    phase: str
    ok: bool
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


WorkerToMaster = Annotated[
    Register
    | Heartbeat
    | TaskBound
    | TaskCompleted
    | TaskReleased
    | TaskPhaseResult,
    Field(discriminator="kind"),
]


# ── Master → Worker payloads ────────────────────────────────────────────


class AssignTask(BaseModel):
    """Master tells a worker to provision a VM and bind a task to it.

    Worker:
      1. Calls `runtime.provision_vm(image, vcpus, memory_gb, disk_gb)`.
         Cache hit → loadvm ~30s; miss → cold boot ~5min + savevm.
      2. Registers the task with the local scheduler (for lease HTTP APIs).
      3. Replies with `TaskBound` carrying the worker-assigned vm_id +
         from_cache flag + URL info for the client.

    On worker-side failure (provision error, image unknown, unfit shape),
    reply is `TaskBound(ok=False, error=...)` — master treats it as a
    transient failure and may re-place the task on a different worker.
    """

    kind: Literal["assign_task"] = "assign_task"
    task_id: str
    lease_id: str
    image_key: str
    image_version: str | None = None
    task_path: str | None = None
    vcpus: int
    memory_gb: int
    disk_gb: int
    task_data: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StagePhase(BaseModel):
    kind: Literal["stage_phase"] = "stage_phase"
    lease_id: str
    phase: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ReleaseLease(BaseModel):
    """Master tells a worker to terminate a lease and destroy its VM."""

    kind: Literal["release_lease"] = "release_lease"
    lease_id: str
    final_status: str


class Shutdown(BaseModel):
    kind: Literal["shutdown"] = "shutdown"
    graceful: bool = True


MasterToWorker = Annotated[
    AssignTask | StagePhase | ReleaseLease | Shutdown,
    Field(discriminator="kind"),
]


# ── Envelope ────────────────────────────────────────────────────────────


class Envelope(BaseModel):
    """Generic transport envelope.

    Payload is kept as an untyped dict at the envelope layer so the same
    Envelope type can travel in both directions. Callers validate the
    payload against the appropriate ``WorkerToMaster`` / ``MasterToWorker``
    adapter once they know which side of the connection they're on.
    """

    msg_id: str
    correlation_id: str | None = None
    payload: dict[str, Any]


__all__ = [
    "AssignTask",
    "CachedShape",
    "Envelope",
    "Heartbeat",
    "MasterToWorker",
    "Register",
    "ReleaseLease",
    "Shutdown",
    "StagePhase",
    "TaskBound",
    "TaskCompleted",
    "TaskPhaseResult",
    "TaskReleased",
    "WorkerCapacity",
    "WorkerToMaster",
    "WorkerVMSummary",
]

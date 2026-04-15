"""WebSocket wire protocol between master and worker.

All traffic on ``/v1/cluster/ws`` is encoded as JSON ``Envelope`` objects.
The ``payload`` field is a discriminated union on ``kind``; new message types
should be added to ``WorkerToMaster`` or ``MasterToWorker`` below.

Design notes:

* The protocol is symmetric at the transport level (both sides send and
  receive Envelopes) but the payload unions are directional — ``Register``
  only flows from worker, ``AssignTask`` only from master, etc. This lets
  static type checkers catch misuse.
* ``msg_id`` is a worker/master-local monotonic counter used for correlating
  responses to requests (e.g. ``PoolOpResult.correlation_id == PoolOp.msg_id``).
  Fire-and-forget messages leave ``correlation_id`` null.
* We intentionally avoid gRPC. FastAPI already pulls in ``starlette``'s
  WebSocket support and Pydantic handles schema; adding protobuf tooling
  would be overkill for an internal-VPC protocol.
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


class WorkerVMSummary(BaseModel):
    """Snapshot of a single VM in the worker's pool, sent with heartbeats."""

    vm_id: str
    image_key: str
    vcpus: int
    memory_gb: int
    state: str  # mirrors VMState enum values
    lease_id: str | None = None
    # Public reach info — used by master to construct TaskAssignment.urls so
    # clients can talk to the VM directly (Phase 4 ships without double-hop).
    public_host: str | None = None
    published_ports: dict[int, int] = Field(default_factory=dict)
    novnc_port: int | None = None


class PoolOpArgs(BaseModel):
    """Arguments for a single pool mutation op.

    All fields are optional at the schema level; the valid subset depends on
    ``kind``. Enforcement lives in the worker op executor, not here, so that
    we can add new op kinds without cascading validator changes.
    """

    image_key: str | None = None
    image_spec: dict[str, Any] | None = None  # serialized ImageSpec
    vm_id: str | None = None
    vcpus: int | None = None
    memory_gb: int | None = None
    count: int | None = None


# ── Worker → Master payloads ────────────────────────────────────────────


class Register(BaseModel):
    kind: Literal["register"] = "register"
    worker_id: str
    runtime_version: str
    capacity: WorkerCapacity
    hosted_images: list[str] = Field(default_factory=list)


class Heartbeat(BaseModel):
    kind: Literal["heartbeat"] = "heartbeat"
    load_cpu: float = 0.0
    load_memory: float = 0.0
    vm_summaries: list[WorkerVMSummary] = Field(default_factory=list)


class VMStateUpdate(BaseModel):
    kind: Literal["vm_state_update"] = "vm_state_update"
    vm_id: str
    state: str
    lease_id: str | None = None
    error: str | None = None


class PoolOpResult(BaseModel):
    kind: Literal["pool_op_result"] = "pool_op_result"
    op_id: str
    ok: bool
    error: str | None = None
    produced_vm_id: str | None = None


class TaskPhaseResult(BaseModel):
    kind: Literal["task_phase_result"] = "task_phase_result"
    lease_id: str
    phase: str
    ok: bool
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TaskBound(BaseModel):
    """Worker confirms a task is bound to a VM and ready to accept client traffic.

    ``lease_endpoint`` is the worker's HTTP base URL (e.g.
    ``http://35.193.3.23:8787``). Master copies it into ``TaskAssignment``
    so clients POST lease-scoped operations directly at the worker.
    """

    kind: Literal["task_bound"] = "task_bound"
    task_id: str
    lease_id: str
    vm_id: str
    ok: bool = True
    error: str | None = None
    lease_endpoint: str | None = None
    urls: dict[int, str] = Field(default_factory=dict)
    novnc_url: str | None = None


class TaskReleased(BaseModel):
    """Worker acknowledges a master-initiated ReleaseLease (cancel path)."""

    kind: Literal["task_released"] = "task_released"
    lease_id: str
    ok: bool = True
    error: str | None = None


class TaskCompleted(BaseModel):
    """Worker reports a lease finished through the client HTTP complete path.

    Sent asynchronously (not as an RPC reply) when the client calls
    ``POST /v1/leases/{id}/complete`` on the worker and the VM finishes
    reverting. Master's ClusterDispatcher consumes this to update the batch
    state it's aggregating.
    """

    kind: Literal["task_completed"] = "task_completed"
    task_id: str
    lease_id: str
    final_status: str  # completed | failed | abandoned
    error: str | None = None


WorkerToMaster = Annotated[
    Register
    | Heartbeat
    | VMStateUpdate
    | PoolOpResult
    | TaskPhaseResult
    | TaskBound
    | TaskReleased
    | TaskCompleted,
    Field(discriminator="kind"),
]


# ── Master → Worker payloads ────────────────────────────────────────────


class AssignTask(BaseModel):
    kind: Literal["assign_task"] = "assign_task"
    task_id: str
    lease_id: str
    vm_id: str
    image_key: str
    task_path: str | None = None
    vcpus: int | None = None
    memory_gb: int | None = None
    task_data: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StagePhase(BaseModel):
    kind: Literal["stage_phase"] = "stage_phase"
    lease_id: str
    phase: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ReleaseLease(BaseModel):
    kind: Literal["release_lease"] = "release_lease"
    lease_id: str
    final_status: str  # completed | failed | abandoned


class PoolOp(BaseModel):
    kind: Literal["pool_op"] = "pool_op"
    op_id: str
    op: Literal["ADD_IMAGE", "REMOVE_IMAGE", "ADD_VM", "REMOVE_VM"]
    args: PoolOpArgs


class Shutdown(BaseModel):
    kind: Literal["shutdown"] = "shutdown"
    graceful: bool = True


MasterToWorker = Annotated[
    AssignTask | StagePhase | ReleaseLease | PoolOp | Shutdown,
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
    "Envelope",
    "Heartbeat",
    "MasterToWorker",
    "PoolOp",
    "PoolOpArgs",
    "PoolOpResult",
    "Register",
    "ReleaseLease",
    "Shutdown",
    "StagePhase",
    "TaskBound",
    "TaskCompleted",
    "TaskPhaseResult",
    "TaskReleased",
    "VMStateUpdate",
    "WorkerCapacity",
    "WorkerToMaster",
    "WorkerVMSummary",
]

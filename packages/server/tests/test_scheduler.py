from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from cua_house_common.models import BatchCreateRequest, TaskRequirement, TaskState, utcnow
from cua_house_server._internal.port_pool import PortPool
from cua_house_server.config.loader import HostRuntimeConfig, ImageSpec, LocalImageConfig
from cua_house_server.scheduler.core import EnvScheduler
from cua_house_server.scheduler.models import VMRecord, VMState


class FakeRuntime:
    def __init__(self, config: HostRuntimeConfig):
        self.config = config
        self.stage_calls = []

    def cleanup_orphaned_state(self) -> None:
        return None

    def prepare_slot(
        self,
        *,
        slot_id: str,
        image: ImageSpec,
        vcpus: int,
        memory_gb: int,
        cua_port: int,
        novnc_port: int,
        lease_id: str,
        task_id: str,
        task_data=None,
    ):
        class Handle:
            pass

        handle = Handle()
        handle.slot_id = slot_id
        handle.image_key = image.key
        handle.vcpus = vcpus
        handle.memory_gb = memory_gb
        handle.published_ports = {cua_port: cua_port}
        handle.novnc_port = novnc_port
        handle.container_name = f"cua-house-env-{slot_id}"
        handle.task_id = task_id
        handle.lease_id = lease_id
        handle.storage_dir = Path("/tmp")
        handle.logs_dir = Path("/tmp")
        return handle

    async def start_slot(self, handle) -> None:
        return None

    async def reset_slot(self, handle, image: ImageSpec) -> None:
        return None

    def validate_runtime_task_data(self, *, task_id: str, task_data) -> None:
        return None

    async def revert_vm(self, handle) -> None:
        return None

    async def replace_broken_vm(self, handle, image):
        return handle

    async def stage_task_phase(self, *, handle, task_id: str, lease_id: str, task_data, phase: str, **kwargs):
        self.stage_calls.append((lease_id, task_id, phase, task_data))
        class Result:
            skipped = False
            file_count = 3
            bytes_staged = 4096
        return Result()

    def vm_published_url(self, handle, guest_port: int) -> str:
        return f"http://127.0.0.1:{handle.published_ports[guest_port]}"

    def vm_novnc_local_url(self, handle) -> str:
        return f"http://127.0.0.1:{handle.novnc_port}"

    @staticmethod
    def _run(cmd, *, check=True):
        return None


class SlowFakeRuntime(FakeRuntime):
    async def start_slot(self, handle) -> None:
        await asyncio.sleep(0.2)


def make_scheduler() -> EnvScheduler:
    host = HostRuntimeConfig(
        host_id="host-1",
        host_external_ip="127.0.0.1",
        public_base_host="127.0.0.1.sslip.io",
        runtime_root=Path("/tmp/cua-house-env-tests"),
        task_data_root=Path("/tmp/agenthle-task-data"),
        docker_image="trycua/cua-qemu-windows:latest",
        host_reserved_vcpus=2,
        host_reserved_memory_gb=8,
        batch_heartbeat_ttl_s=30,
        heartbeat_ttl_s=30,
        ready_timeout_s=60,
        readiness_poll_interval_s=0.1,
        idle_slot_ttl_s=300,
        published_port_range=(16000, 16010),
        novnc_port_range=(18000, 18010),
    )
    images = {
        "cpu-free": ImageSpec(
            key="cpu-free",
            enabled=True,
            os_family="windows",
            published_ports=(5000,),
            local=LocalImageConfig(template_qcow2_path=Path("/tmp/golden.qcow2"), default_vcpus=4, default_memory_gb=16),
        ),
        "cpu-huge": ImageSpec(
            key="cpu-huge",
            enabled=True,
            os_family="windows",
            published_ports=(5000,),
            local=LocalImageConfig(template_qcow2_path=Path("/tmp/golden.qcow2"), default_vcpus=9999, default_memory_gb=9999),
        ),
    }
    rt = FakeRuntime(host)
    sched = EnvScheduler(runtime=rt, host_config=host, images=images, runtimes={"local": rt})
    # Pre-register fake pool VMs so local dispatch finds them
    _register_fake_pool(sched, images, n_per_image=4)
    return sched


def _register_fake_pool(sched: EnvScheduler, images: dict, n_per_image: int = 2) -> None:
    """Register fake pool VMs so local dispatch works in tests."""
    port = 16000
    for key, image in images.items():
        if not image.enabled:
            continue
        for i in range(n_per_image):
            vm_id = f"fake-{key}-{i}"
            published_ports = {gp: port for gp in image.published_ports}
            port += len(image.published_ports)

            class FakeHandle:
                pass

            handle = FakeHandle()
            handle.vm_id = vm_id
            handle.snapshot_name = key
            handle.vcpus = image.default_vcpus
            handle.memory_gb = image.default_memory_gb
            handle.published_ports = published_ports
            handle.novnc_port = 18000 + i
            handle.container_name = f"cua-house-env-{vm_id}"
            handle.task_id = ""
            handle.lease_id = ""

            vm = VMRecord(
                vm_id=vm_id,
                snapshot_name=key,
                state=VMState.READY,
                vcpus=image.default_vcpus,
                memory_gb=image.default_memory_gb,
                container_name=handle.container_name,
                published_ports=published_ports,
                novnc_port=handle.novnc_port,
            )
            sched._vms[vm_id] = vm
            sched._vm_handles[vm_id] = handle


def make_slow_scheduler() -> EnvScheduler:
    host = HostRuntimeConfig(
        host_id="host-1",
        host_external_ip="127.0.0.1",
        public_base_host="127.0.0.1.sslip.io",
        runtime_root=Path("/tmp/cua-house-env-tests"),
        task_data_root=Path("/tmp/agenthle-task-data"),
        docker_image="trycua/cua-qemu-windows:latest",
        host_reserved_vcpus=2,
        host_reserved_memory_gb=8,
        batch_heartbeat_ttl_s=1,
        heartbeat_ttl_s=1,
        ready_timeout_s=60,
        readiness_poll_interval_s=0.1,
        idle_slot_ttl_s=300,
        published_port_range=(16100, 16110),
        novnc_port_range=(18100, 18110),
    )
    images = {
        "cpu-free": ImageSpec(
            key="cpu-free",
            enabled=True,
            os_family="windows",
            published_ports=(5000,),
            local=LocalImageConfig(template_qcow2_path=Path("/tmp/golden.qcow2"), default_vcpus=4, default_memory_gb=16),
        ),
    }
    rt = SlowFakeRuntime(host)
    sched = EnvScheduler(runtime=rt, host_config=host, images=images, runtimes={"local": rt})
    _register_fake_pool(sched, images, n_per_image=4)
    return sched


def test_port_pool_allocate_release() -> None:
    pool = PortPool(15000, 15001)
    a = pool.allocate()
    b = pool.allocate()
    assert {a, b} == {15000, 15001}
    pool.release(a)
    assert pool.allocate() == a


async def wait_for_assignment(scheduler: EnvScheduler, task_id: str) -> None:
    for _ in range(50):
        task = await scheduler.get_task(task_id)
        if task.assignment is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} did not receive assignment")


def test_scheduler_defaults_and_grouping() -> None:
    async def scenario() -> None:
        scheduler = make_scheduler()
        await scheduler.start()
        batch = await scheduler.submit_batch(
            BatchCreateRequest(
                tasks=[
                    TaskRequirement(task_id="task-a", task_path="tasks/a", snapshot_name="cpu-free"),
                    TaskRequirement(task_id="task-b", task_path="tasks/b", snapshot_name="cpu-free"),
                ]
            )
        )
        assert batch.tasks[0].vcpus == 4
        assert batch.tasks[0].memory_gb == 16

        await wait_for_assignment(scheduler, "task-a")
        await wait_for_assignment(scheduler, "task-b")
        task_a = await scheduler.get_task("task-a")
        task_b = await scheduler.get_task("task-b")
        assert task_a.assignment is not None
        assert task_b.assignment is not None
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_scheduler_prefers_smaller_tasks_when_capacity_is_tight() -> None:
    async def scenario() -> None:
        scheduler = make_scheduler()
        await scheduler.start()
        await scheduler.submit_batch(
            BatchCreateRequest(
                tasks=[
                    TaskRequirement(
                        task_id="task-big",
                        task_path="tasks/big",
                        snapshot_name="cpu-huge",  # oversize image in test catalog
                    ),
                    TaskRequirement(
                        task_id="task-small",
                        task_path="tasks/small",
                        snapshot_name="cpu-free",
                    ),
                ]
            )
        )
        await wait_for_assignment(scheduler, "task-small")
        small = await scheduler.get_task("task-small")
        big = await scheduler.get_task("task-big")
        assert small.assignment is not None
        assert big.state == TaskState.FAILED
        assert "exceeding host allocatable capacity" in (big.error or "")
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_impossible_task_is_failed_immediately() -> None:
    async def scenario() -> None:
        scheduler = make_scheduler()
        await scheduler.start()
        batch = await scheduler.submit_batch(
            BatchCreateRequest(
                tasks=[
                    TaskRequirement(
                        task_id="task-impossible",
                        task_path="tasks/impossible",
                        snapshot_name="cpu-huge",  # oversize image in test catalog
                    )
                ]
            )
        )
        task = batch.tasks[0]
        assert task.state == TaskState.FAILED
        assert "exceeding host allocatable capacity" in (task.error or "")
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_lease_complete_releases_slot() -> None:
    async def scenario() -> None:
        scheduler = make_scheduler()
        await scheduler.start()
        await scheduler.submit_batch(
            BatchCreateRequest(tasks=[TaskRequirement(task_id="task-a", task_path="tasks/a", snapshot_name="cpu-free")])
        )
        await wait_for_assignment(scheduler, "task-a")
        task = await scheduler.get_task("task-a")
        assert task.assignment is not None
        await scheduler.complete(task.assignment.lease_id, final_status="completed")
        for _ in range(50):
            task = await scheduler.get_task("task-a")
            if task.state == TaskState.COMPLETED:
                break
            await asyncio.sleep(0.01)
        assert task.state == TaskState.COMPLETED
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_stage_runtime_and_eval_return_counts() -> None:
    async def scenario() -> None:
        scheduler = make_scheduler()
        await scheduler.start()
        await scheduler.submit_batch(
            BatchCreateRequest(
                tasks=[
                    TaskRequirement(
                        task_id="task-a",
                        task_path="tasks/a",
                        snapshot_name="cpu-free",
                        task_data=TaskRequirement.TaskDataRequest(
                            requires_task_data=True,
                            task_category="finance/taxform_1_1",
                            task_tag="variant_1",
                            source_relpath="finance/taxform_1_1/variant_1",
                            input_dir=r"C:\Users\User\Desktop\finance\taxform_1_1\variant_1\input",
                            reference_dir=r"C:\Users\User\Desktop\finance\taxform_1_1\variant_1\reference",
                            remote_output_dir=r"C:\Users\User\Desktop\finance\taxform_1_1\variant_1\output",
                        ),
                    )
                ]
            )
        )
        await wait_for_assignment(scheduler, "task-a")
        task = await scheduler.get_task("task-a")
        assert task.assignment is not None
        runtime_result = await scheduler.stage_runtime(task.assignment.lease_id)
        eval_result = await scheduler.stage_eval(task.assignment.lease_id)
        assert runtime_result.file_count == 3
        assert runtime_result.bytes_staged == 4096
        assert eval_result.phase == "eval"
        await scheduler.shutdown()

    asyncio.run(scenario())

def test_lease_reaper_marks_expired_lease_failed() -> None:
    async def scenario() -> None:
        scheduler = make_scheduler()
        await scheduler.start()
        await scheduler.submit_batch(
            BatchCreateRequest(tasks=[TaskRequirement(task_id="task-a", task_path="tasks/a", snapshot_name="cpu-free")])
        )
        await wait_for_assignment(scheduler, "task-a")
        task = await scheduler.get_task("task-a")
        assert task.assignment is not None
        lease = scheduler._leases[task.assignment.lease_id]
        lease.expires_at = utcnow() - timedelta(seconds=1)
        await scheduler.reap_expired_leases_once()
        for _ in range(50):
            task = await scheduler.get_task("task-a")
            if task.state == TaskState.FAILED:
                break
            await asyncio.sleep(0.01)
        assert task.state == TaskState.FAILED
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_pool_task_is_ready_immediately() -> None:
    """With pre-registered pool VMs, tasks go directly to READY (no STARTING phase)."""
    async def scenario() -> None:
        scheduler = make_slow_scheduler()
        await scheduler.start()
        await scheduler.submit_batch(
            BatchCreateRequest(tasks=[TaskRequirement(task_id="task-a", task_path="tasks/a", snapshot_name="cpu-free")])
        )
        await wait_for_assignment(scheduler, "task-a")
        task = await scheduler.get_task("task-a")
        assert task.state == TaskState.READY
        assert task.assignment is not None
        assert task.lease_id == task.assignment.lease_id
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_scheduler_assigns_multiple_tasks_concurrently() -> None:
    """With pool VMs available, all tasks get assigned in parallel."""
    async def scenario() -> None:
        scheduler = make_slow_scheduler()
        await scheduler.start()
        await scheduler.submit_batch(
            BatchCreateRequest(
                tasks=[
                    TaskRequirement(task_id="task-a", task_path="tasks/a", snapshot_name="cpu-free"),
                    TaskRequirement(task_id="task-b", task_path="tasks/b", snapshot_name="cpu-free"),
                    TaskRequirement(task_id="task-c", task_path="tasks/c", snapshot_name="cpu-free"),
                ]
            )
        )
        await asyncio.sleep(0.05)
        states = [(await scheduler.get_task(task_id)).state for task_id in ("task-a", "task-b", "task-c")]
        assert states == [TaskState.READY, TaskState.READY, TaskState.READY]
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_cancel_batch_fails_queued_and_starting_tasks() -> None:
    async def scenario() -> None:
        scheduler = make_slow_scheduler()
        await scheduler.start()
        batch = await scheduler.submit_batch(
            BatchCreateRequest(
                tasks=[
                    TaskRequirement(task_id="task-a", task_path="tasks/a", snapshot_name="cpu-free"),
                    TaskRequirement(task_id="task-b", task_path="tasks/b", snapshot_name="cpu-free"),
                ]
            )
        )
        assert batch.batch_id
        await asyncio.sleep(0.05)
        await scheduler.cancel_batch(batch.batch_id, reason="orchestration interrupted")
        # Wait for async revert tasks to complete
        await asyncio.sleep(0.2)
        task_a = await scheduler.get_task("task-a")
        task_b = await scheduler.get_task("task-b")
        assert task_a.state == TaskState.FAILED
        assert task_b.state == TaskState.FAILED
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_batch_heartbeat_expiry_cleans_starting_tasks() -> None:
    async def scenario() -> None:
        scheduler = make_slow_scheduler()
        await scheduler.start()
        batch = await scheduler.submit_batch(
            BatchCreateRequest(
                tasks=[
                    TaskRequirement(task_id="task-a", task_path="tasks/a", snapshot_name="cpu-free"),
                ]
            )
        )
        scheduler._batch_expires_at[batch.batch_id] = utcnow() - timedelta(seconds=1)
        await scheduler.reap_expired_batches_once()
        task = await scheduler.get_task("task-a")
        assert task.state == TaskState.FAILED
        assert task.error == "batch heartbeat expired"
        await scheduler.shutdown()

    asyncio.run(scenario())


def test_runtime_cleanup_orphaned_state(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    slot_storage = runtime_root / "slots" / "slot-1" / "storage"
    slot_storage.mkdir(parents=True, exist_ok=True)
    (slot_storage / "data.qcow2").write_text("dirty", encoding="utf-8")

    config = HostRuntimeConfig(
        host_id="host-1",
        host_external_ip="127.0.0.1",
        public_base_host="127.0.0.1.sslip.io",
        runtime_root=runtime_root,
        task_data_root=tmp_path / "task-data",
        docker_image="trycua/cua-qemu-windows:latest",
        host_reserved_vcpus=2,
        host_reserved_memory_gb=8,
        batch_heartbeat_ttl_s=30,
        heartbeat_ttl_s=30,
        ready_timeout_s=60,
        readiness_poll_interval_s=0.1,
        idle_slot_ttl_s=300,
        published_port_range=(16000, 16010),
        novnc_port_range=(18000, 18010),
    )

    from cua_house_server.runtimes.qemu import DockerQemuRuntime

    runtime = DockerQemuRuntime(config)
    calls: list[list[str]] = []

    def fake_run(cmd, *, cwd=None, check=True):
        calls.append(cmd)

        class Result:
            stdout = "abc123\n"
            returncode = 0

        return Result()

    runtime._run = fake_run  # type: ignore[method-assign]
    runtime.cleanup_orphaned_state()

    assert calls[0] == ["docker", "ps", "-aq", "--filter", "name=cua-house-env-"]
    assert calls[1] == ["docker", "rm", "-f", "abc123"]
    assert runtime.slots_root.exists()
    assert not any(runtime.slots_root.iterdir())


def test_runtime_prepare_slot_uses_container_visible_backing_file(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    golden_real = tmp_path / "images" / "golden-real.qcow2"
    golden_real.parent.mkdir(parents=True, exist_ok=True)
    golden_real.write_text("golden", encoding="utf-8")
    golden = tmp_path / "images" / "golden.qcow2"
    golden.symlink_to(golden_real)

    config = HostRuntimeConfig(
        host_id="host-1",
        host_external_ip="127.0.0.1",
        public_base_host="127.0.0.1.sslip.io",
        runtime_root=runtime_root,
        task_data_root=tmp_path / "task-data",
        docker_image="trycua/cua-qemu-windows:latest",
        host_reserved_vcpus=2,
        host_reserved_memory_gb=8,
        batch_heartbeat_ttl_s=30,
        heartbeat_ttl_s=30,
        ready_timeout_s=60,
        readiness_poll_interval_s=0.1,
        idle_slot_ttl_s=300,
        published_port_range=(16000, 16010),
        novnc_port_range=(18000, 18010),
    )

    from cua_house_server.runtimes.qemu import DockerQemuRuntime

    runtime = DockerQemuRuntime(config)
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(cmd, *, cwd=None, check=True):
        calls.append((cmd, cwd))

        class Result:
            stdout = ""
            returncode = 0

        return Result()

    runtime._run = fake_run  # type: ignore[method-assign]
    handle = runtime._prepare_vm(
        vm_id="slot-1",
        image=ImageSpec(
            key="cpu-free",
            enabled=True,
            os_family="windows",
            published_ports=(5000,),
            local=LocalImageConfig(template_qcow2_path=golden, default_vcpus=4, default_memory_gb=16),
        ),
        vcpus=4,
        memory_gb=16,
        published_ports={5000: 16000},
        novnc_port=18000,
        snapshot_name="cpu-free",
    )

    assert handle.published_ports == {5000: 16000}
    assert handle.novnc_port == 18000
    # cp --reflink=auto should have been called
    assert any("cp" in str(c) for c in calls)


def test_api_requires_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv("CUA_HOUSE_TOKEN", "secret-token")
    from cua_house_server.api.app import create_app

    config_dir = Path(__file__).resolve().parents[1] / "src" / "cua_house_server" / "config" / "defaults"

    app = create_app(
        host_config_path=config_dir / "server.yaml",
        image_catalog_path=config_dir / "images.yaml",
    )
    client = TestClient(app)

    response = client.get("/healthz")
    assert response.status_code == 401

    response = client.get("/healthz", headers={"Authorization": "Bearer secret-token"})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

"""Host runtime for managing QEMU Docker Windows slots."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from cua_house.common.events import JsonlEventLogger

from .models import TaskRequirement, VMPoolEntry
from .qmp_client import QMPClient
from .task_data import StageResult, TaskDataManager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ImageSpec:
    key: str
    enabled: bool
    default_cpu_cores: int
    default_memory_gb: int
    runtime_mode: str = "local"  # "local" | "gcp"
    # local mode
    golden_qcow2_path: Path | None = None
    # gcp mode
    gcp_project: str | None = None
    gcp_zone: str | None = None
    gcp_network: str | None = None
    gcp_service_account: str | None = None
    gcp_machine_type: str | None = None
    gcp_boot_image: str | None = None       # GCP image name for boot disk
    gcp_boot_snapshot: str | None = None     # fallback: snapshot for boot disk
    gcp_data_snapshot: str | None = None     # snapshot for data disk
    gcp_boot_disk_gb: int = 64
    gcp_data_disk_gb: int = 200
    gpu_type: str | None = None
    gpu_count: int = 0
    max_concurrent_vms: int = 4


@dataclass(slots=True)
class HostRuntimeConfig:
    host_id: str
    host_external_ip: str
    public_base_host: str
    runtime_root: Path
    task_data_root: Path | None
    docker_image: str
    host_reserved_cpu_cores: int
    host_reserved_memory_gb: int
    batch_heartbeat_ttl_s: int
    heartbeat_ttl_s: int
    ready_timeout_s: int
    readiness_poll_interval_s: float
    idle_slot_ttl_s: int
    cua_port_range: tuple[int, int]
    novnc_port_range: tuple[int, int]
    # VM pool (snapshot-based local runtime)
    vm_pool: list[VMPoolEntry] = field(default_factory=list)
    snapshot_name: str = "clean-ready"
    snapshot_save_timeout_s: int = 300
    snapshot_revert_timeout_s: int = 300
    cua_ready_after_revert_timeout_s: int = 30


@dataclass(slots=True)
class SlotHandle:
    slot_id: str
    image_key: str
    cpu_cores: int
    memory_gb: int
    cua_port: int
    novnc_port: int
    storage_dir: Path
    logs_dir: Path
    golden_qcow2_path: Path
    container_name: str
    task_id: str
    lease_id: str
    prepared_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    start_monotonic: float | None = None
    task_data_source_root: Path | None = None
    boot_markers: dict[str, datetime] = field(default_factory=dict)


@dataclass
class VMHandle:
    """Handle for a persistent VM instance in the snapshot pool."""

    vm_id: str
    image_key: str
    cpu_cores: int
    memory_gb: int
    cua_port: int
    novnc_port: int
    storage_dir: Path
    logs_dir: Path
    golden_qcow2_path: Path
    container_name: str
    snapshot_name: str = "clean-ready"
    qmp: QMPClient | None = None
    boot_started_at: datetime | None = None
    ready_at: datetime | None = None
    start_monotonic: float | None = None
    boot_markers: dict[str, datetime] = field(default_factory=dict)
    # Compat fields so scheduler/runtime can duck-type VMHandle as SlotHandle
    task_id: str = ""
    lease_id: str = ""
    started_at: datetime | None = None
    task_data_source_root: Path | None = None

    @property
    def slot_id(self) -> str:
        """Alias for vm_id — makes VMHandle duck-type compatible with SlotHandle."""
        return self.vm_id


def _load_yaml(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_host_runtime_config(path: str | Path) -> HostRuntimeConfig:
    raw = _load_yaml(path)
    host_external_ip = raw["host_external_ip"]
    if host_external_ip == "auto":
        host_external_ip = resolve_gce_external_ip()
    public_base_host = raw.get("public_base_host", "auto")
    if public_base_host == "auto":
        public_base_host = f"{host_external_ip}.sslip.io"
    return HostRuntimeConfig(
        host_id=raw["host_id"],
        host_external_ip=host_external_ip,
        public_base_host=public_base_host,
        runtime_root=Path(raw["runtime_root"]),
        task_data_root=Path(raw["task_data_root"]) if raw.get("task_data_root") else None,
        docker_image=raw.get("docker_image", "trycua/cua-qemu-windows:latest"),
        host_reserved_cpu_cores=int(raw.get("host_reserved_cpu_cores", 2)),
        host_reserved_memory_gb=int(raw.get("host_reserved_memory_gb", 8)),
        batch_heartbeat_ttl_s=int(raw.get("batch_heartbeat_ttl_s", 120)),
        heartbeat_ttl_s=int(raw.get("heartbeat_ttl_s", 60)),
        ready_timeout_s=int(raw.get("ready_timeout_s", 900)),
        readiness_poll_interval_s=float(raw.get("readiness_poll_interval_s", 5)),
        idle_slot_ttl_s=int(raw.get("idle_slot_ttl_s", 300)),
        cua_port_range=tuple(raw.get("cua_port_range", [15000, 15999])),
        novnc_port_range=tuple(raw.get("novnc_port_range", [18000, 18999])),
        vm_pool=[
            VMPoolEntry(**entry) for entry in raw.get("vm_pool", [])
        ],
        snapshot_name=raw.get("snapshot_name", "clean-ready"),
        snapshot_save_timeout_s=int(raw.get("snapshot_save_timeout_s", 300)),
        snapshot_revert_timeout_s=int(raw.get("snapshot_revert_timeout_s", 300)),
        cua_ready_after_revert_timeout_s=int(raw.get("cua_ready_after_revert_timeout_s", 30)),
    )


def load_image_catalog(path: str | Path) -> dict[str, ImageSpec]:
    raw = _load_yaml(path)
    images = raw.get("images", {})
    catalog: dict[str, ImageSpec] = {}
    for key, spec in images.items():
        runtime_mode = spec.get("runtime_mode", "local")
        image = ImageSpec(
            key=key,
            enabled=bool(spec.get("enabled", False)),
            default_cpu_cores=int(spec.get("default_cpu_cores", 4)),
            default_memory_gb=int(spec.get("default_memory_gb", 16)),
            runtime_mode=runtime_mode,
        )
        if runtime_mode == "local":
            image.golden_qcow2_path = Path(spec["golden_qcow2_path"])
        elif runtime_mode == "gcp":
            image.gcp_project = spec.get("gcp_project")
            image.gcp_zone = spec.get("gcp_zone")
            image.gcp_network = spec.get("gcp_network")
            image.gcp_service_account = spec.get("gcp_service_account")
            image.gcp_machine_type = spec.get("gcp_machine_type")
            image.gcp_boot_image = spec.get("gcp_boot_image")
            image.gcp_boot_snapshot = spec.get("gcp_boot_snapshot")
            image.gcp_data_snapshot = spec.get("gcp_data_snapshot")
            image.gcp_boot_disk_gb = int(spec.get("gcp_boot_disk_gb", 64))
            image.gcp_data_disk_gb = int(spec.get("gcp_data_disk_gb", 200))
            image.gpu_type = spec.get("gpu_type")
            image.gpu_count = int(spec.get("gpu_count", 0))
            image.max_concurrent_vms = int(spec.get("max_concurrent_vms", 4))
        catalog[key] = image
    return catalog


class DockerQemuRuntime:
    """Creates, starts, resets, and destroys QEMU Docker VM slots."""

    def __init__(self, config: HostRuntimeConfig, event_logger: JsonlEventLogger | None = None):
        self.config = config
        self.config.runtime_root.mkdir(parents=True, exist_ok=True)
        self.event_logger = event_logger or JsonlEventLogger(
            self.config.runtime_root / "events.jsonl",
            component="env_server",
        )
        self.task_data = TaskDataManager(self.config.task_data_root, self.event_logger)

    @property
    def slots_root(self) -> Path:
        return self.config.runtime_root / "slots"

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
    ) -> SlotHandle:
        if image.golden_qcow2_path is None:
            raise ValueError(f"golden_qcow2_path is required for local image {image.key}")
        golden_qcow2_path = image.golden_qcow2_path.resolve(strict=True)

        slot_root = self.slots_root / slot_id
        storage_dir = slot_root / "storage"
        logs_dir = slot_root / "logs"
        storage_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        (storage_dir / "windows.boot").touch(exist_ok=True)
        slot_golden = storage_dir / "golden.qcow2"
        if slot_golden.exists() or slot_golden.is_symlink():
            slot_golden.unlink()

        overlay = storage_dir / "data.qcow2"
        if overlay.exists():
            overlay.unlink()
        self._run(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-b",
                str(golden_qcow2_path),
                "-F",
                "qcow2",
                "data.qcow2",
            ],
            cwd=storage_dir,
        )
        self._run(
            [
                "qemu-img",
                "rebase",
                "-u",
                "-b",
                "/storage/golden.qcow2",
                "-F",
                "qcow2",
                "data.qcow2",
            ],
            cwd=storage_dir,
        )
        task_data_source_root: Path | None = None
        if (
            task_data is not None
            and task_data.requires_task_data
            and task_data.source_relpath
            and self.config.task_data_root is not None
        ):
            candidate = (self.config.task_data_root / task_data.source_relpath).resolve()
            if candidate.exists():
                task_data_source_root = candidate

        self.event_logger.emit(
            "slot_prepared",
            slot_id=slot_id,
            task_id=task_id,
            lease_id=lease_id,
            image_key=image.key,
            cpu_cores=cpu_cores,
            memory_gb=memory_gb,
            storage_dir=storage_dir,
            golden_qcow2_path=golden_qcow2_path,
            cua_port=cua_port,
            novnc_port=novnc_port,
            task_data_source_root=task_data_source_root,
        )

        return SlotHandle(
            slot_id=slot_id,
            image_key=image.key,
            cpu_cores=cpu_cores,
            memory_gb=memory_gb,
            cua_port=cua_port,
            novnc_port=novnc_port,
            storage_dir=storage_dir,
            logs_dir=logs_dir,
            golden_qcow2_path=golden_qcow2_path,
            container_name=f"agenthle-env-{slot_id}",
            task_id=task_id,
            lease_id=lease_id,
            task_data_source_root=task_data_source_root,
        )

    async def start_slot(self, handle: SlotHandle) -> None:
        self._run(["docker", "rm", "-f", handle.container_name], check=False)
        log_path = handle.logs_dir / "docker.log"
        handle.started_at = datetime.now(UTC)
        handle.start_monotonic = time.perf_counter()
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            handle.container_name,
            "--device=/dev/kvm",
            "--cap-add",
            "NET_ADMIN",
            "-v",
            f"{handle.storage_dir}:/storage",
            "-v",
            f"{handle.golden_qcow2_path}:/storage/golden.qcow2:ro",
            "-p",
            f"127.0.0.1:{handle.cua_port}:5000",
            "-p",
            f"127.0.0.1:{handle.novnc_port}:8006",
            "-e",
            f"RAM_SIZE={handle.memory_gb}G",
            "-e",
            f"CPU_CORES={handle.cpu_cores}",
        ]
        if handle.task_data_source_root is not None:
            input_dir = handle.task_data_source_root / "input"
            if input_dir.is_dir():
                cmd.extend(["-v", f"{input_dir}:/shared/input:ro"])
            software_dir = handle.task_data_source_root / "software"
            if software_dir.is_dir():
                cmd.extend(["-v", f"{software_dir}:/shared/software:ro"])
        cmd.append(self.config.docker_image)
        logger.info("Starting slot %s (%s)", handle.slot_id, handle.container_name)
        self.event_logger.emit(
            "slot_starting",
            slot_id=handle.slot_id,
            task_id=handle.task_id,
            lease_id=handle.lease_id,
            image_key=handle.image_key,
            cpu_cores=handle.cpu_cores,
            memory_gb=handle.memory_gb,
            container_name=handle.container_name,
        )
        result = self._run(cmd)
        log_path.write_text(result.stdout or "", encoding="utf-8")
        await self.wait_ready(handle)

    async def wait_ready(self, handle: SlotHandle) -> None:
        url = f"http://127.0.0.1:{handle.cua_port}/status"
        deadline = asyncio.get_running_loop().time() + self.config.ready_timeout_s
        last_error: str | None = None
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                self._emit_boot_markers(handle)
                if asyncio.get_running_loop().time() > deadline:
                    self.event_logger.emit(
                        "slot_ready_timeout",
                        slot_id=handle.slot_id,
                        task_id=handle.task_id,
                        lease_id=handle.lease_id,
                        last_error=last_error,
                        observed_boot_markers=sorted(handle.boot_markers),
                    )
                    raise RuntimeError(f"slot {handle.slot_id} did not become ready: {last_error}")
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        self._emit_boot_markers(handle)
                        ready_at = datetime.now(UTC)
                        total_ready_s = None
                        if handle.start_monotonic is not None:
                            total_ready_s = time.perf_counter() - handle.start_monotonic
                        windows_started = handle.boot_markers.get("windows_started")
                        boot_manager = handle.boot_markers.get("boot_manager")
                        vm_ip_detected = handle.boot_markers.get("vm_ip_detected")
                        self.event_logger.emit(
                            "slot_ready",
                            slot_id=handle.slot_id,
                            task_id=handle.task_id,
                            lease_id=handle.lease_id,
                            total_ready_s=total_ready_s,
                            boot_manager_s=self._seconds_between(handle.started_at, boot_manager),
                            vm_ip_detected_s=self._seconds_between(handle.started_at, vm_ip_detected),
                            windows_boot_s=self._seconds_between(handle.started_at, windows_started),
                            computer_server_wait_s=self._seconds_between(windows_started, ready_at),
                        )
                        return
                    last_error = f"http {response.status_code}"
                except Exception as exc:  # pragma: no cover - network timing varies
                    last_error = str(exc)

                inspect = self._run(
                    ["docker", "inspect", handle.container_name, "--format", "{{.State.Status}}"],
                    check=False,
                )
                status = (inspect.stdout or "").strip()
                if status in {"exited", "dead"}:
                    raise RuntimeError(f"slot {handle.slot_id} container exited before ready")
                await asyncio.sleep(self.config.readiness_poll_interval_s)

    async def reset_slot(self, handle: SlotHandle, image: ImageSpec) -> None:
        logger.info("Resetting slot %s", handle.slot_id)
        reset_started = time.perf_counter()
        self.event_logger.emit(
            "slot_reset_started",
            slot_id=handle.slot_id,
            task_id=handle.task_id,
            lease_id=handle.lease_id,
        )
        self._run(["docker", "rm", "-f", handle.container_name], check=False)
        self.prepare_slot(
            slot_id=handle.slot_id,
            image=image,
            cpu_cores=handle.cpu_cores,
            memory_gb=handle.memory_gb,
            cua_port=handle.cua_port,
            novnc_port=handle.novnc_port,
            lease_id=handle.lease_id,
            task_id=handle.task_id,
        )
        self.event_logger.emit(
            "slot_reset_completed",
            slot_id=handle.slot_id,
            task_id=handle.task_id,
            lease_id=handle.lease_id,
            reset_s=time.perf_counter() - reset_started,
        )

    def cua_local_url(self, handle: SlotHandle) -> str:
        return f"http://127.0.0.1:{handle.cua_port}"

    def novnc_local_url(self, handle: SlotHandle) -> str:
        return f"http://127.0.0.1:{handle.novnc_port}"

    def _emit_boot_markers(self, handle: SlotHandle) -> None:
        result = self._run(
            ["docker", "logs", "--timestamps", handle.container_name],
            check=False,
        )
        output = result.stdout or ""
        for line in output.splitlines():
            marker = self._parse_boot_marker(line)
            if marker is None or marker in handle.boot_markers:
                continue
            ts = self._parse_docker_timestamp(line)
            if ts is None:
                continue
            handle.boot_markers[marker] = ts
            self.event_logger.emit(
                f"slot_{marker}",
                slot_id=handle.slot_id,
                task_id=handle.task_id,
                lease_id=handle.lease_id,
                seconds_since_start=self._seconds_between(handle.started_at, ts),
            )

    @staticmethod
    def _parse_boot_marker(line: str) -> str | None:
        if 'BdsDxe: starting Boot0007 "Windows Boot Manager"' in line:
            return "boot_manager"
        if "Detected VM IP:" in line:
            return "vm_ip_detected"
        if "Waiting for Cua computer-server to be ready" in line:
            return "computer_server_wait_started"
        if "Windows started successfully" in line:
            return "windows_started"
        return None

    @staticmethod
    def _parse_docker_timestamp(line: str) -> datetime | None:
        if " " not in line:
            return None
        prefix = line.split(" ", 1)[0]
        try:
            return datetime.fromisoformat(prefix.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return None

    @staticmethod
    def _seconds_between(start: datetime | None, end: datetime | None) -> float | None:
        if start is None or end is None:
            return None
        return (end - start).total_seconds()

    def destroy_idle_slot(self, slot_id: str) -> None:
        slot_root = self.slots_root / slot_id
        if slot_root.exists():
            shutil.rmtree(slot_root)

    def cleanup_orphaned_state(self) -> None:
        result = self._run(
            ["docker", "ps", "-aq", "--filter", "name=agenthle-env-"],
            check=False,
        )
        container_ids = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if container_ids:
            logger.warning("Cleaning up %d orphaned agenthle-env containers on startup", len(container_ids))
            self._run(["docker", "rm", "-f", *container_ids], check=False)
        if self.slots_root.exists():
            shutil.rmtree(self.slots_root)
        self.slots_root.mkdir(parents=True, exist_ok=True)

    def validate_runtime_task_data(
        self,
        *,
        task_id: str,
        task_data: TaskRequirement.TaskDataRequest | None,
    ) -> None:
        self.task_data.validate_runtime_data(task_id=task_id, task_data=task_data)

    async def stage_task_phase(
        self,
        *,
        handle: SlotHandle | VMHandle,
        task_id: str,
        lease_id: str,
        task_data: TaskRequirement.TaskDataRequest | None,
        phase: str,
        container_name: str | None = None,
    ) -> StageResult:
        cname = container_name or handle.container_name
        if isinstance(handle, VMHandle):
            cua_url = self.vm_cua_local_url(handle)
        else:
            cua_url = self.cua_local_url(handle)
        return await self.task_data.stage_phase(
            lease_id=lease_id,
            task_id=task_id,
            cua_url=cua_url,
            task_data=task_data,
            phase=phase,
            container_name=cname,
            vm_pool=isinstance(handle, VMHandle),
        )

    # ── VM pool (snapshot-based persistent VMs) ────────────────────

    async def initialize_pool(
        self,
        pool_entries: list[VMPoolEntry],
        images: dict[str, ImageSpec],
    ) -> list[VMHandle]:
        """Cold-boot N containers, wait for readiness, take snapshots.

        Called once at server start.  All VMs boot in parallel.
        Returns VMHandle list with state READY and QMP connected.
        """
        from uuid import uuid4

        from .port_pool import PortPool

        cua_ports = PortPool(*self.config.cua_port_range)
        novnc_ports = PortPool(*self.config.novnc_port_range)

        handles: list[VMHandle] = []
        for entry in pool_entries:
            image = images.get(entry.image_key)
            if image is None or not image.enabled:
                logger.warning("Skipping disabled/unknown image %s in vm_pool", entry.image_key)
                continue
            cpu = entry.cpu_cores or image.default_cpu_cores
            mem = entry.memory_gb or image.default_memory_gb
            for _ in range(entry.count):
                vm_id = str(uuid4())
                cua_port = cua_ports.allocate()
                novnc_port = novnc_ports.allocate()
                handle = self._prepare_vm(
                    vm_id=vm_id,
                    image=image,
                    cpu_cores=cpu,
                    memory_gb=mem,
                    cua_port=cua_port,
                    novnc_port=novnc_port,
                )
                handles.append(handle)

        # Boot all VMs in parallel
        async def _boot_one(h: VMHandle) -> None:
            await self._start_vm_container(h)
            await self.wait_ready(h)
            h.ready_at = datetime.now(UTC)
            # Connect QMP and take initial snapshot
            h.qmp = QMPClient(h.container_name)
            self.event_logger.emit(
                "vm_snapshot_started",
                vm_id=h.vm_id,
                container_name=h.container_name,
                snapshot_name=h.snapshot_name,
            )
            snap_start = time.perf_counter()
            await h.qmp.save_snapshot(
                h.snapshot_name,
                timeout=self.config.snapshot_save_timeout_s,
            )
            self.event_logger.emit(
                "vm_snapshot_completed",
                vm_id=h.vm_id,
                container_name=h.container_name,
                snapshot_name=h.snapshot_name,
                snapshot_s=time.perf_counter() - snap_start,
            )

        results = await asyncio.gather(
            *[_boot_one(h) for h in handles],
            return_exceptions=True,
        )
        ready_handles: list[VMHandle] = []
        for handle, result in zip(handles, results):
            if isinstance(result, Exception):
                logger.error("Failed to boot VM %s: %s", handle.vm_id, result)
                self._run(["docker", "rm", "-f", handle.container_name], check=False)
                self.event_logger.emit(
                    "vm_boot_failed",
                    vm_id=handle.vm_id,
                    container_name=handle.container_name,
                    error=str(result),
                )
            else:
                ready_handles.append(handle)

        logger.info(
            "VM pool initialized: %d/%d VMs ready",
            len(ready_handles), len(handles),
        )
        return ready_handles

    def _prepare_vm(
        self,
        *,
        vm_id: str,
        image: ImageSpec,
        cpu_cores: int,
        memory_gb: int,
        cua_port: int,
        novnc_port: int,
    ) -> VMHandle:
        """Create overlay and directories for a persistent VM."""
        if image.golden_qcow2_path is None:
            raise ValueError(f"golden_qcow2_path required for image {image.key}")
        golden = image.golden_qcow2_path.resolve(strict=True)

        vm_root = self.slots_root / vm_id
        storage_dir = vm_root / "storage"
        logs_dir = vm_root / "logs"
        storage_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        (storage_dir / "windows.boot").touch(exist_ok=True)

        # Create QCOW2 overlay
        overlay = storage_dir / "data.qcow2"
        if overlay.exists():
            overlay.unlink()
        self._run(
            ["qemu-img", "create", "-f", "qcow2", "-b", str(golden),
             "-F", "qcow2", "data.qcow2"],
            cwd=storage_dir,
        )
        self._run(
            ["qemu-img", "rebase", "-u", "-b", "/storage/golden.qcow2",
             "-F", "qcow2", "data.qcow2"],
            cwd=storage_dir,
        )

        container_name = f"agenthle-env-{vm_id}"
        return VMHandle(
            vm_id=vm_id,
            image_key=image.key,
            cpu_cores=cpu_cores,
            memory_gb=memory_gb,
            cua_port=cua_port,
            novnc_port=novnc_port,
            storage_dir=storage_dir,
            logs_dir=logs_dir,
            golden_qcow2_path=golden,
            container_name=container_name,
            snapshot_name=self.config.snapshot_name,
        )

    async def _start_vm_container(self, handle: VMHandle) -> None:
        """Launch Docker container for a VM.

        Container persists across tasks — only created once during pool init.
        """
        self._run(["docker", "rm", "-f", handle.container_name], check=False)
        handle.boot_started_at = datetime.now(UTC)
        handle.start_monotonic = time.perf_counter()

        # Generate patched boot.sh for qcow2 pflash support
        patched_boot = self._ensure_patched_boot_sh()

        cmd = [
            "docker", "run", "-d",
            "--name", handle.container_name,
            "--device=/dev/kvm",
            "--cap-add", "NET_ADMIN",
            "-v", f"{handle.storage_dir}:/storage",
            "-v", f"{handle.golden_qcow2_path}:/storage/golden.qcow2:ro",
            # Mount entire task-data disk (read-only) for all VMs
            "-v", f"{self.config.task_data_root}:/shared:ro",
            # Patched boot.sh: converts pflash vars to qcow2 for snapshot support
            "-v", f"{patched_boot}:/run/boot.sh:ro",
            "-p", f"127.0.0.1:{handle.cua_port}:5000",
            "-p", f"127.0.0.1:{handle.novnc_port}:8006",
            "-e", f"RAM_SIZE={handle.memory_gb}G",
            "-e", f"CPU_CORES={handle.cpu_cores}",
            # Snapshot-compatible CPU settings
            "-e", "CPU_MODEL=host",  # removes migratable=no
            "-e", "HV=N",  # removes hv_passthrough
            self.config.docker_image,
        ]

        log_path = handle.logs_dir / "docker.log"
        self.event_logger.emit(
            "vm_starting",
            vm_id=handle.vm_id,
            container_name=handle.container_name,
            image_key=handle.image_key,
            cpu_cores=handle.cpu_cores,
            memory_gb=handle.memory_gb,
        )
        result = self._run(cmd)
        log_path.write_text(result.stdout or "", encoding="utf-8")

    def _ensure_patched_boot_sh(self) -> Path:
        """Create a patched boot.sh that converts pflash vars to qcow2.

        QEMU savevm/loadvm requires all writable drives to support snapshots.
        The dockur/windows base image creates pflash vars as raw format, which
        doesn't support snapshots.  This patch converts it to qcow2 at boot.
        """
        patched = self.config.runtime_root / "boot-patched.sh"
        if patched.exists():
            return patched

        # Extract boot.sh from the Docker image
        self._run(["docker", "create", "--name", "tmp-boot-extract", self.config.docker_image, "true"], check=False)
        try:
            result = self._run(
                ["docker", "cp", "tmp-boot-extract:/run/boot.sh", str(patched)],
            )
        finally:
            self._run(["docker", "rm", "tmp-boot-extract"], check=False)

        content = patched.read_text(encoding="utf-8")
        old_line = 'BOOT_OPTS+=" -drive file=$DEST.vars,if=pflash,unit=1,format=raw"'
        new_lines = (
            '# Convert pflash vars to qcow2 for snapshot support\n'
            '    if [ -f "$DEST.vars" ] && ! qemu-img info "$DEST.vars" 2>/dev/null | grep -q "file format: qcow2"; then\n'
            '      qemu-img convert -f raw -O qcow2 "$DEST.vars" "$DEST.vars.q2"\n'
            '      mv "$DEST.vars.q2" "$DEST.vars"\n'
            '    fi\n'
            '    BOOT_OPTS+=" -drive file=$DEST.vars,if=pflash,unit=1,format=qcow2"'
        )
        if old_line not in content:
            logger.warning("Could not patch boot.sh — pflash line not found. Snapshots may fail.")
            return patched
        content = content.replace(old_line, new_lines)
        patched.write_text(content, encoding="utf-8")
        patched.chmod(0o755)
        logger.info("Created patched boot.sh at %s", patched)
        return patched

    async def revert_vm(self, handle: VMHandle) -> None:
        """Revert VM to clean snapshot state via QMP loadvm.

        After loadvm, the VM is in its exact snapshot state — disk, RAM, CPU
        all restored.  CUA server should be responsive almost immediately.
        """
        if handle.qmp is None:
            raise RuntimeError(f"VM {handle.vm_id} has no QMP client")

        revert_start = time.perf_counter()
        self.event_logger.emit(
            "vm_revert_started",
            vm_id=handle.vm_id,
            container_name=handle.container_name,
            snapshot_name=handle.snapshot_name,
        )

        await handle.qmp.load_snapshot(
            handle.snapshot_name,
            timeout=self.config.snapshot_revert_timeout_s,
        )

        # Wait for CUA to be responsive (should be near-instant after loadvm)
        await self._wait_cua_ready(handle, timeout=self.config.cua_ready_after_revert_timeout_s)

        revert_s = time.perf_counter() - revert_start
        self.event_logger.emit(
            "vm_revert_completed",
            vm_id=handle.vm_id,
            container_name=handle.container_name,
            snapshot_name=handle.snapshot_name,
            revert_s=revert_s,
        )
        logger.info("VM %s reverted in %.1fs", handle.vm_id, revert_s)

    async def _wait_cua_ready(self, handle: VMHandle, timeout: float = 30) -> None:
        """Poll CUA /status until 200.  Used after loadvm (short timeout)."""
        url = f"http://127.0.0.1:{handle.cua_port}/status"
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx.AsyncClient(timeout=10) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return
                except Exception:
                    pass
                await asyncio.sleep(1)
        raise RuntimeError(f"CUA not ready on VM {handle.vm_id} after {timeout}s")

    async def replace_broken_vm(
        self, handle: VMHandle, image: ImageSpec,
    ) -> VMHandle:
        """Destroy a broken VM and create a fresh replacement."""
        logger.warning("Replacing broken VM %s (%s)", handle.vm_id, handle.container_name)
        self._run(["docker", "rm", "-f", handle.container_name], check=False)
        slot_root = self.slots_root / handle.vm_id
        if slot_root.exists():
            shutil.rmtree(slot_root)

        new_handle = self._prepare_vm(
            vm_id=handle.vm_id,
            image=image,
            cpu_cores=handle.cpu_cores,
            memory_gb=handle.memory_gb,
            cua_port=handle.cua_port,
            novnc_port=handle.novnc_port,
        )
        await self._start_vm_container(new_handle)
        await self.wait_ready(new_handle)
        new_handle.qmp = QMPClient(new_handle.container_name)
        await new_handle.qmp.save_snapshot(
            new_handle.snapshot_name,
            timeout=self.config.snapshot_save_timeout_s,
        )
        return new_handle

    def vm_cua_local_url(self, handle: VMHandle) -> str:
        return f"http://127.0.0.1:{handle.cua_port}"

    def vm_novnc_local_url(self, handle: VMHandle) -> str:
        return f"http://127.0.0.1:{handle.novnc_port}"

    @staticmethod
    def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"command failed ({result.returncode}): {' '.join(cmd)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result


@dataclass(slots=True)
class GCPSlotHandle:
    """Slot handle for GCP-managed VM instances."""

    slot_id: str
    image_key: str
    cpu_cores: int
    memory_gb: int
    task_id: str
    lease_id: str
    vm_name: str = ""
    vm_ip: str = ""
    vm_zone: str = ""
    vm_project: str = ""
    boot_disk_name: str = ""
    data_disk_name: str = ""
    # SlotHandle compat (unused but needed for scheduler duck-typing)
    cua_port: int = 5000
    novnc_port: int = 0
    storage_dir: Path = field(default_factory=lambda: Path("/dev/null"))
    logs_dir: Path = field(default_factory=lambda: Path("/dev/null"))
    golden_qcow2_path: Path = field(default_factory=lambda: Path("/dev/null"))
    container_name: str = ""
    prepared_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    start_monotonic: float | None = None
    boot_markers: dict[str, datetime] = field(default_factory=dict)


class GCPVMRuntime:
    """Create and manage GCP VM instances as evaluation slots."""

    def __init__(
        self,
        config: HostRuntimeConfig,
        event_logger: JsonlEventLogger | None = None,
        gcloud_path: str = "gcloud",
    ):
        self.config = config
        self.event_logger = event_logger or JsonlEventLogger(
            config.runtime_root / "events.jsonl",
            component="env_server_gcp",
        )
        self._gcloud = gcloud_path

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
    ) -> GCPSlotHandle:
        vm_name = f"agenthle-env-{slot_id[:8]}"
        handle = GCPSlotHandle(
            slot_id=slot_id,
            image_key=image.key,
            cpu_cores=cpu_cores,
            memory_gb=memory_gb,
            task_id=task_id,
            lease_id=lease_id,
            vm_name=vm_name,
            vm_zone=image.gcp_zone or "",
            vm_project=image.gcp_project or "",
            boot_disk_name=f"{vm_name}-boot",
            data_disk_name=f"{vm_name}-data",
            cua_port=5000,
            novnc_port=0,
        )
        self.event_logger.emit(
            "slot_prepared",
            slot_id=slot_id,
            task_id=task_id,
            lease_id=lease_id,
            image_key=image.key,
            runtime_mode="gcp",
            vm_name=vm_name,
        )
        return handle

    async def start_slot(self, handle: GCPSlotHandle) -> None:
        image = self._image_for_handle(handle)
        handle.started_at = datetime.now(UTC)
        handle.start_monotonic = time.perf_counter()

        # 1. Create data disk from snapshot (if configured)
        if image.gcp_data_snapshot:
            self._run_gcloud([
                "compute", "disks", "create", handle.data_disk_name,
                f"--project={image.gcp_project}",
                f"--zone={image.gcp_zone}",
                f"--source-snapshot={image.gcp_data_snapshot}",
                f"--size={image.gcp_data_disk_gb}GB",
                "--type=pd-balanced",
            ])

        # 2. Create VM — prefer --image (faster) over --disk from snapshot
        cmd = [
            "compute", "instances", "create", handle.vm_name,
            f"--project={image.gcp_project}",
            f"--zone={image.gcp_zone}",
            f"--machine-type={image.gcp_machine_type}",
            f"--network={image.gcp_network}",
            f"--service-account={image.gcp_service_account}",
            "--scopes=https://www.googleapis.com/auth/cloud-platform",
            "--maintenance-policy=TERMINATE",
            "--tags=agenthle",
        ]
        if image.gcp_boot_image:
            # Fast path: create boot disk from image inline (~14s vs ~100s from snapshot)
            cmd.extend([
                f"--image={image.gcp_boot_image}",
                f"--boot-disk-size={image.gcp_boot_disk_gb}GB",
                "--boot-disk-type=pd-ssd",
            ])
        elif image.gcp_boot_snapshot:
            # Fallback: pre-create boot disk from snapshot, then attach
            self._run_gcloud([
                "compute", "disks", "create", handle.boot_disk_name,
                f"--project={image.gcp_project}",
                f"--zone={image.gcp_zone}",
                f"--source-snapshot={image.gcp_boot_snapshot}",
                f"--size={image.gcp_boot_disk_gb}GB",
                "--type=pd-ssd",
            ])
            cmd.append(f"--disk=name={handle.boot_disk_name},boot=yes,auto-delete=yes")
        if image.gcp_data_snapshot:
            cmd.append(f"--disk=name={handle.data_disk_name},auto-delete=yes")
        if image.gpu_type and image.gpu_count > 0:
            cmd.append(f"--accelerator=type={image.gpu_type},count={image.gpu_count}")
        self._run_gcloud(cmd)

        self.event_logger.emit(
            "slot_starting",
            slot_id=handle.slot_id,
            task_id=handle.task_id,
            lease_id=handle.lease_id,
            image_key=handle.image_key,
            vm_name=handle.vm_name,
            runtime_mode="gcp",
        )

        # 4. Get external IP
        handle.vm_ip = self._get_vm_ip(handle)
        logger.info("GCP VM %s started with IP %s", handle.vm_name, handle.vm_ip)

        # 5. Wait for cua-server readiness
        await self._wait_ready(handle)

        # 6. Ensure data disk is assigned drive letter E:
        if image.gcp_data_snapshot:
            await self._assign_data_disk_drive_letter(handle)

    async def _assign_data_disk_drive_letter(self, handle: GCPSlotHandle) -> None:
        """Find the data disk drive letter and reassign to E: if needed."""
        cua_url = self.cua_local_url(handle)
        async with httpx.AsyncClient(timeout=30) as client:
            # Find all non-C: volumes with a drive letter (the data disk)
            result = await self._run_remote(
                client, cua_url,
                self._powershell(
                    "Get-Volume | Where-Object {$_.DriveLetter -and $_.DriveLetter -ne 'C' -and $_.DriveType -eq 'Fixed'} "
                    "| Select-Object -ExpandProperty DriveLetter"
                ),
            )
            letters = [l.strip() for l in (result.get("stdout", "") or "").strip().splitlines() if l.strip()]
            logger.info("Data disk volumes found: %s", letters)

            if not letters:
                logger.warning("No data disk volume found on VM %s", handle.vm_name)
                return

            current = letters[0]
            if current != "E":
                logger.info("Reassigning data disk from %s: to E:", current)
                await self._run_remote(
                    client, cua_url,
                    self._powershell(
                        f"Get-Partition | Where-Object {{$_.DriveLetter -eq '{current}'}} | Set-Partition -NewDriveLetter E"
                    ),
                )
                # Verify
                result2 = await self._run_remote(client, cua_url, "dir E:\\ /B")
                if result2.get("return_code", 1) == 0:
                    logger.info("Data disk reassigned to E: successfully")
                else:
                    logger.error("Failed to verify E: after reassignment")
            else:
                logger.info("Data disk already on E:")

    async def _wait_ready(self, handle: GCPSlotHandle) -> None:
        url = f"http://{handle.vm_ip}:5000/status"
        deadline = asyncio.get_running_loop().time() + self.config.ready_timeout_s
        last_error: str | None = None
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                if asyncio.get_running_loop().time() > deadline:
                    self.event_logger.emit(
                        "slot_ready_timeout",
                        slot_id=handle.slot_id,
                        task_id=handle.task_id,
                        lease_id=handle.lease_id,
                        last_error=last_error,
                        runtime_mode="gcp",
                    )
                    raise RuntimeError(f"GCP slot {handle.slot_id} ({handle.vm_name}) did not become ready: {last_error}")
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        total_ready_s = None
                        if handle.start_monotonic is not None:
                            total_ready_s = time.perf_counter() - handle.start_monotonic
                        self.event_logger.emit(
                            "slot_ready",
                            slot_id=handle.slot_id,
                            task_id=handle.task_id,
                            lease_id=handle.lease_id,
                            total_ready_s=total_ready_s,
                            vm_name=handle.vm_name,
                            vm_ip=handle.vm_ip,
                            runtime_mode="gcp",
                        )
                        return
                    last_error = f"http {response.status_code}"
                except Exception as exc:
                    last_error = str(exc)
                await asyncio.sleep(self.config.readiness_poll_interval_s)

    async def reset_slot(self, handle: GCPSlotHandle, image: ImageSpec) -> None:
        logger.info("Resetting GCP slot %s (deleting VM %s)", handle.slot_id, handle.vm_name)
        reset_started = time.perf_counter()
        self.event_logger.emit(
            "slot_reset_started",
            slot_id=handle.slot_id,
            task_id=handle.task_id,
            lease_id=handle.lease_id,
            runtime_mode="gcp",
        )
        # Delete VM (auto-delete=yes will also delete attached disks)
        self._run_gcloud([
            "compute", "instances", "delete", handle.vm_name,
            f"--project={image.gcp_project}",
            f"--zone={image.gcp_zone}",
            "--quiet",
        ], check=False)
        self.event_logger.emit(
            "slot_reset_completed",
            slot_id=handle.slot_id,
            task_id=handle.task_id,
            lease_id=handle.lease_id,
            reset_s=time.perf_counter() - reset_started,
            runtime_mode="gcp",
        )

    def cua_local_url(self, handle: GCPSlotHandle) -> str:
        return f"http://{handle.vm_ip}:5000"

    def novnc_local_url(self, handle: GCPSlotHandle) -> str:
        return ""

    def cleanup_orphaned_state(self) -> None:
        """Delete any agenthle-env-* VMs and disks left from previous runs."""
        # Find orphaned VMs across all zones used by GCP images
        zones_projects: set[tuple[str, str]] = set()
        # We don't have image catalog here, so scan for VMs by name pattern
        result = self._run_gcloud([
            "compute", "instances", "list",
            "--filter=name~^agenthle-env-",
            "--format=value(name,zone)",
        ], check=False)
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    vm_name, zone = parts[0], parts[1]
                    logger.warning("Cleaning up orphaned GCP VM: %s in %s", vm_name, zone)
                    self._run_gcloud([
                        "compute", "instances", "delete", vm_name,
                        f"--zone={zone}",
                        "--quiet",
                    ], check=False)

    def validate_runtime_task_data(
        self,
        *,
        task_id: str,
        task_data: TaskRequirement.TaskDataRequest | None,
    ) -> None:
        # For GCP mode, task data is on the attached data disk — no host-side validation needed.
        pass

    async def stage_task_phase(
        self,
        *,
        handle: GCPSlotHandle,
        task_id: str,
        lease_id: str,
        task_data: TaskRequirement.TaskDataRequest | None,
        phase: str,
    ) -> StageResult:
        """Control access to task data via NTFS ACLs on the data disk.

        Whitelist strategy (runs as User, no elevation needed):
        - runtime phase: enumerate the task directory and deny access to every
          subdirectory that is NOT input/, software/, or output/. Also deny all
          sibling task directories so the agent cannot peek at other tasks.
        - eval phase: remove the deny on reference/ so evaluator can read it.
        """
        if task_data is None or not task_data.requires_task_data:
            return StageResult(skipped=True)

        cua_url = self.cua_local_url(handle)
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0)) as client:
            if phase == "runtime":
                # Whitelist: only input/, software/, output/ are accessible
                whitelist = set()
                for d in (task_data.input_dir, task_data.software_dir, task_data.remote_output_dir):
                    if d:
                        whitelist.add(d.rstrip("\\").rsplit("\\", 1)[-1].lower())

                task_dir = task_data.input_dir.rstrip("\\").rsplit("\\", 1)[0] if task_data.input_dir else None
                if task_dir:
                    # 1. Deny non-whitelisted subdirs in task dir
                    subdirs = await self._list_subdirs(client, cua_url, task_dir)
                    for subdir in subdirs:
                        if subdir.lower() not in whitelist:
                            await self._run_remote(
                                client, cua_url,
                                f'icacls "{task_dir}\\{subdir}" /deny User:(OI)(CI)F /Q',
                            )

                    # 2. Deny sibling tasks in same category
                    category_dir = task_dir.rsplit("\\", 1)[0]
                    task_name = task_dir.rsplit("\\", 1)[-1]
                    siblings = await self._list_subdirs(client, cua_url, category_dir)
                    for sibling in siblings:
                        if sibling != task_name:
                            await self._run_remote(
                                client, cua_url,
                                f'icacls "{category_dir}\\{sibling}" /deny User:(OI)(CI)F /Q',
                            )

                    # 3. Deny other categories
                    data_root = category_dir.rsplit("\\", 1)[0]
                    category_name = category_dir.rsplit("\\", 1)[-1]
                    categories = await self._list_subdirs(client, cua_url, data_root)
                    for cat in categories:
                        if cat != category_name:
                            await self._run_remote(
                                client, cua_url,
                                f'icacls "{data_root}\\{cat}" /deny User:(OI)(CI)F /Q',
                            )

                self.event_logger.emit(
                    "task_data_acl_locked",
                    lease_id=lease_id,
                    task_id=task_id,
                    input_dir=task_data.input_dir,
                    software_dir=task_data.software_dir,
                )
            else:  # eval
                if task_data.reference_dir:
                    await self._run_remote(
                        client, cua_url,
                        f'icacls "{task_data.reference_dir}" /remove:d User /Q',
                    )
                    self.event_logger.emit(
                        "task_data_reference_unlocked",
                        lease_id=lease_id,
                        task_id=task_id,
                        reference_dir=task_data.reference_dir,
                    )
        return StageResult(file_count=0, bytes_staged=0)

    async def _list_subdirs(self, client: httpx.AsyncClient, cua_url: str, path: str) -> list[str]:
        """List subdirectory names using cmd dir (more reliable than PowerShell via CUA)."""
        result = await self._run_remote(client, cua_url, f'dir "{path}" /AD /B')
        stdout = (result.get("stdout", "") or "").strip()
        if not stdout or result.get("return_code", 1) != 0:
            return []
        return [s.strip() for s in stdout.splitlines() if s.strip()]

    def destroy_vm(self, handle: GCPSlotHandle) -> None:
        """Force-delete a VM. Used for error cleanup."""
        self._run_gcloud([
            "compute", "instances", "delete", handle.vm_name,
            f"--project={handle.vm_project}",
            f"--zone={handle.vm_zone}",
            "--quiet",
        ], check=False)

    def _get_vm_ip(self, handle: GCPSlotHandle) -> str:
        result = self._run_gcloud([
            "compute", "instances", "describe", handle.vm_name,
            f"--project={handle.vm_project}",
            f"--zone={handle.vm_zone}",
            "--format=value(networkInterfaces[0].accessConfigs[0].natIP)",
        ])
        ip = (result.stdout or "").strip()
        if not ip:
            raise RuntimeError(f"Could not get external IP for VM {handle.vm_name}")
        return ip

    def _image_for_handle(self, handle: GCPSlotHandle) -> ImageSpec:
        """Look up image spec. Caller must ensure images are loaded."""
        # This is set by the scheduler which passes the image to start_slot.
        # For now, store images at init time.
        if not hasattr(self, "_images"):
            raise RuntimeError("GCPVMRuntime._images not set — call set_images() first")
        return self._images[handle.image_key]

    def set_images(self, images: dict[str, ImageSpec]) -> None:
        self._images = images

    def _run_gcloud(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = [self._gcloud, *args]
        result = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"gcloud failed ({result.returncode}): {' '.join(cmd)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    async def _run_remote(self, client: httpx.AsyncClient, cua_url: str, command: str) -> dict:
        """Run a command on the guest via CUA API."""
        import json as _json

        response = await client.post(
            f"{cua_url.rstrip('/')}/cmd",
            json={"command": "run_command", "params": {"command": command}},
        )
        response.raise_for_status()
        result = None
        for line in response.text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                result = _json.loads(line[6:])
            except _json.JSONDecodeError:
                continue
        if result is None:
            raise RuntimeError(f"no valid response for remote command")
        if result.get("success") is False:
            raise RuntimeError(f"remote command failed: {result.get('error', 'unknown')}")
        return result

    @staticmethod
    def _powershell(script: str) -> str:
        compact = "; ".join(line.strip() for line in script.strip().splitlines() if line.strip())
        return f'powershell -NoProfile -Command "{compact}"'


def resolve_gce_external_ip() -> str:
    explicit = os.environ.get("AGENTHLE_ENV_SERVER_EXTERNAL_IP")
    if explicit:
        return explicit

    try:
        response = httpx.get(
            "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip",
            headers={"Metadata-Flavor": "Google"},
            timeout=5,
        )
        response.raise_for_status()
        return response.text.strip()
    except Exception:
        logger.warning("Failed to resolve GCE external IP from metadata; falling back to 127.0.0.1")
        return "127.0.0.1"

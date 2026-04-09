"""Docker/QEMU runtime backend for local VM pool (snapshot-based)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from cua_house_common.events import JsonlEventLogger
from cua_house_common.models import TaskRequirement, VMPoolEntry
from cua_house_server.config.loader import HostRuntimeConfig, ImageSpec
from cua_house_server.data.staging import StageResult, TaskDataManager
from cua_house_server.qmp.client import QMPClient

logger = logging.getLogger(__name__)


@dataclass
class VMHandle:
    """Handle for a persistent VM instance in the snapshot pool."""

    vm_id: str
    snapshot_name: str
    cpu_cores: int
    memory_gb: int
    cua_port: int
    novnc_port: int
    storage_dir: Path
    logs_dir: Path
    disk_path: Path
    container_name: str
    qmp: QMPClient | None = None
    boot_started_at: datetime | None = None
    ready_at: datetime | None = None
    start_monotonic: float | None = None
    boot_markers: dict[str, datetime] = field(default_factory=dict)
    task_id: str = ""
    lease_id: str = ""
    started_at: datetime | None = None
    task_data_source_root: Path | None = None

    @property
    def slot_id(self) -> str:
        """Alias for vm_id -- used by scheduler for uniform lease tracking."""
        return self.vm_id


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

    async def wait_ready(self, handle: VMHandle) -> None:
        url = f"http://127.0.0.1:{handle.cua_port}/status"
        deadline = asyncio.get_running_loop().time() + self.config.ready_timeout_s
        last_error: str | None = None
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                self._emit_boot_markers(handle)
                if asyncio.get_running_loop().time() > deadline:
                    self.event_logger.emit(
                        "slot_ready_timeout",
                        slot_id=handle.vm_id,
                        task_id=handle.task_id,
                        lease_id=handle.lease_id,
                        last_error=last_error,
                        observed_boot_markers=sorted(handle.boot_markers),
                    )
                    raise RuntimeError(f"VM {handle.vm_id} did not become ready: {last_error}")
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
                        started_at = handle.boot_started_at
                        self.event_logger.emit(
                            "slot_ready",
                            slot_id=handle.vm_id,
                            task_id=handle.task_id,
                            lease_id=handle.lease_id,
                            total_ready_s=total_ready_s,
                            boot_manager_s=self._seconds_between(started_at, boot_manager),
                            vm_ip_detected_s=self._seconds_between(started_at, vm_ip_detected),
                            windows_boot_s=self._seconds_between(started_at, windows_started),
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
                    raise RuntimeError(f"VM {handle.vm_id} container exited before ready")
                await asyncio.sleep(self.config.readiness_poll_interval_s)

    def _emit_boot_markers(self, handle: VMHandle) -> None:
        result = self._run(
            ["docker", "logs", "--timestamps", handle.container_name],
            check=False,
        )
        output = result.stdout or ""
        slot_or_vm_id = handle.vm_id
        for line in output.splitlines():
            marker = self._parse_boot_marker(line)
            if marker is None or marker in handle.boot_markers:
                continue
            ts = self._parse_docker_timestamp(line)
            if ts is None:
                continue
            handle.boot_markers[marker] = ts
            started_at = handle.boot_started_at
            self.event_logger.emit(
                f"slot_{marker}",
                slot_id=slot_or_vm_id,
                task_id=handle.task_id,
                lease_id=handle.lease_id,
                seconds_since_start=self._seconds_between(started_at, ts),
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

    def cleanup_orphaned_state(self) -> None:
        result = self._run(
            ["docker", "ps", "-aq", "--filter", "name=cua-house-env-"],
            check=False,
        )
        container_ids = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if container_ids:
            logger.warning("Cleaning up %d orphaned cua-house-env containers on startup", len(container_ids))
            self._run(["docker", "rm", "-f", *container_ids], check=False)
        # Also clean up legacy agenthle-env- containers
        result_legacy = self._run(
            ["docker", "ps", "-aq", "--filter", "name=agenthle-env-"],
            check=False,
        )
        legacy_ids = [line.strip() for line in (result_legacy.stdout or "").splitlines() if line.strip()]
        if legacy_ids:
            logger.warning("Cleaning up %d orphaned agenthle-env containers on startup", len(legacy_ids))
            self._run(["docker", "rm", "-f", *legacy_ids], check=False)
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
        handle: VMHandle,
        task_id: str,
        lease_id: str,
        task_data: TaskRequirement.TaskDataRequest | None,
        phase: str,
        container_name: str | None = None,
        os_type: str | None = None,
    ) -> StageResult:
        cname = container_name or handle.container_name
        cua_url = self.vm_cua_local_url(handle)
        return await self.task_data.stage_phase(
            lease_id=lease_id,
            task_id=task_id,
            cua_url=cua_url,
            task_data=task_data,
            phase=phase,
            container_name=cname,
            vm_pool=True,
            os_type=os_type,
        )

    # -- VM pool (snapshot-based persistent VMs) -----------------------

    async def initialize_pool(
        self,
        pool_entries: list[VMPoolEntry],
        images: dict[str, ImageSpec],
    ) -> list[VMHandle]:
        """Start N containers from pre-baked qcow2 templates via loadvm.

        Called once at server start.  All VMs boot in parallel.
        Returns VMHandle list with state READY and QMP connected.
        Ready in ~30s (loadvm) instead of ~4-5 min (cold boot).
        """
        from uuid import uuid4

        from cua_house_server._internal.port_pool import PortPool

        cua_ports = PortPool(*self.config.cua_port_range)
        novnc_ports = PortPool(*self.config.novnc_port_range)

        # Ensure template qcow2 files exist locally (pull from GCS if needed)
        await self._ensure_local_templates(images)

        handles: list[VMHandle] = []
        prepare_args: list[dict] = []
        for entry in pool_entries:
            # snapshot_name doubles as the image catalog key for local VMs
            image = images.get(entry.snapshot_name)
            if image is None or not image.enabled:
                logger.warning("Skipping disabled/unknown image %s in vm_pool", entry.snapshot_name)
                continue
            for _ in range(entry.count):
                vm_id = str(uuid4())
                cua_port = cua_ports.allocate()
                novnc_port = novnc_ports.allocate()
                prepare_args.append(dict(
                    vm_id=vm_id,
                    image=image,
                    cpu_cores=entry.cpu_cores,
                    memory_gb=entry.memory_gb,
                    cua_port=cua_port,
                    novnc_port=novnc_port,
                    snapshot_name=entry.snapshot_name,
                ))

        # Copy template qcow2 files in parallel (instant on XFS via reflink)
        logger.info("Copying %d template qcow2 files in parallel...", len(prepare_args))
        copy_start = time.perf_counter()
        prepare_results = await asyncio.gather(
            *[asyncio.to_thread(self._prepare_vm, **args) for args in prepare_args],
        )
        copy_s = time.perf_counter() - copy_start
        logger.info("Template copies completed in %.1fs", copy_s)
        handles = list(prepare_results)

        # Start all VMs in parallel via loadvm (pre-baked snapshot)
        async def _boot_one(h: VMHandle) -> None:
            await self._start_vm_container(h)
            await self.wait_ready(h)
            h.ready_at = datetime.now(UTC)
            h.qmp = QMPClient(h.container_name)
            self.event_logger.emit(
                "vm_ready",
                vm_id=h.vm_id,
                container_name=h.container_name,
                snapshot_name=h.snapshot_name,
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

    async def _ensure_local_templates(self, images: dict[str, ImageSpec]) -> None:
        """Ensure template qcow2 files exist locally, pulling from GCS if needed.

        On a fresh node, templates are downloaded from GCS on first startup.
        Subsequent startups skip the download (files already on local XFS disk).
        """
        for key, image in images.items():
            if not image.enabled or image.local is None:
                continue
            local_path = image.local.template_qcow2_path
            gcs_uri = image.local.gcs_uri
            if local_path.exists():
                logger.info("Template %s exists locally: %s", key, local_path)
                continue
            if gcs_uri is None:
                raise FileNotFoundError(
                    f"Template {local_path} not found and no gcs_uri configured for {key}"
                )
            logger.info("Pulling template %s from %s ...", key, gcs_uri)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            pull_start = time.perf_counter()
            await asyncio.to_thread(
                self._run, ["gsutil", "-m", "cp", gcs_uri, str(local_path)],
            )
            pull_s = time.perf_counter() - pull_start
            size_gb = local_path.stat().st_size / 1e9
            logger.info("Template %s pulled in %.1fs (%.1f GB)", key, pull_s, size_gb)
            self.event_logger.emit(
                "template_pulled",
                image_key=key,
                gcs_uri=gcs_uri,
                pull_s=pull_s,
                size_gb=round(size_gb, 1),
            )

    def _prepare_vm(
        self,
        *,
        vm_id: str,
        image: ImageSpec,
        cpu_cores: int,
        memory_gb: int,
        cua_port: int,
        novnc_port: int,
        snapshot_name: str,
    ) -> VMHandle:
        """Copy the template qcow2 and set up directories for a pool VM."""
        if image.template_qcow2_path is None:
            raise ValueError(f"template_qcow2_path required for image {image.key}")
        template = image.template_qcow2_path.resolve(strict=True)

        vm_root = self.slots_root / vm_id
        storage_dir = vm_root / "storage"
        logs_dir = vm_root / "logs"
        storage_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Copy template qcow2 (contains pre-baked savevm snapshot).
        # Uses --reflink=auto: instant on XFS, falls back to full copy on ext4.
        # Docker image expects "data.qcow2" (DISK_NAME=data in dockur/windows)
        disk = storage_dir / "data.qcow2"
        if disk.exists():
            disk.unlink()
        self._run(["cp", "--reflink=auto", str(template), str(disk)])

        container_name = f"cua-house-env-{vm_id}"
        return VMHandle(
            vm_id=vm_id,
            snapshot_name=snapshot_name,
            cpu_cores=cpu_cores,
            memory_gb=memory_gb,
            cua_port=cua_port,
            novnc_port=novnc_port,
            storage_dir=storage_dir,
            logs_dir=logs_dir,
            disk_path=disk,
            container_name=container_name,
        )

    async def _start_vm_container(self, handle: VMHandle) -> None:
        """Launch Docker container for a VM.

        Container persists across tasks -- only created once during pool init.
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
            # storage_dir contains vm.qcow2 (pre-baked, no separate golden needed)
            "-v", f"{handle.storage_dir}:/storage",
            # Mount task-data under /shared/agenthle so VM sees E:\agenthle\...
            # (rw needed for NTFS ACL staging via icacls in _apply_runtime_acls)
            "-v", f"{self.config.task_data_root}:/shared/agenthle:rw",
            # Patched boot.sh: converts pflash vars to qcow2 + loadvm support
            "-v", f"{patched_boot}:/run/boot.sh:ro",
            "-p", f"127.0.0.1:{handle.cua_port}:5000",
            "-p", f"127.0.0.1:{handle.novnc_port}:8006",
            "-e", f"RAM_SIZE={handle.memory_gb}G",
            "-e", f"CPU_CORES={handle.cpu_cores}",
            # Snapshot-compatible CPU settings
            "-e", "CPU_MODEL=host",  # removes migratable=no
            "-e", "HV=N",  # removes hv_passthrough
            # Fix guest IP: dockur derives VM IP from container IP, but loadvm
            # restores the snapshot's IP. Force all containers to use 172.30.0.2
            # so DHCP/ARP matches the snapshot state.
            "-e", "VM_NET_IP=172.30.0.2",
            # Load the pre-baked snapshot on QEMU start (skips Windows cold boot)
            "-e", f"LOADVM_SNAPSHOT={handle.snapshot_name}",
            self.config.docker_image,
        ]

        log_path = handle.logs_dir / "docker.log"
        self.event_logger.emit(
            "vm_starting",
            vm_id=handle.vm_id,
            container_name=handle.container_name,
            snapshot_name=handle.snapshot_name,
            cpu_cores=handle.cpu_cores,
            memory_gb=handle.memory_gb,
        )
        result = self._run(cmd)
        log_path.write_text(result.stdout or "", encoding="utf-8")

    def _ensure_patched_boot_sh(self) -> Path:
        """Patch the Docker image's boot.sh for snapshot support.

        Three patches applied to the dockur/windows boot.sh:

        1. **pflash qcow2 + snapshot tag**: QEMU -loadvm requires the snapshot
           tag to exist in ALL writable drives. The docker image creates pflash
           UEFI vars as raw format (no snapshot support). We convert to qcow2
           and create an empty snapshot tag so -loadvm succeeds.

        2. **-loadvm flag**: Inject -loadvm $LOADVM_SNAPSHOT into QEMU args
           so VMs resume from the pre-baked snapshot instead of cold-booting.

        3. **Disable boot watchdog**: The dockur/windows entry.sh runs
           ``( sleep 30; boot ) &`` which kills QEMU if the serial console
           has no output after 30s. With -loadvm (especially Linux guests),
           no serial output is produced, so the watchdog would kill the VM.
           We touch $QEMU_END to make boot() return immediately.
        """
        patched = self.config.runtime_root / "boot-patched.sh"
        if patched.exists():
            return patched

        # Extract boot.sh from the Docker image
        self._run(["docker", "create", "--name", "tmp-boot-extract", self.config.docker_image, "true"], check=False)
        try:
            self._run(["docker", "cp", "tmp-boot-extract:/run/boot.sh", str(patched)])
        finally:
            self._run(["docker", "rm", "tmp-boot-extract"], check=False)

        content = patched.read_text(encoding="utf-8")

        # Patch 1: pflash vars — convert raw→qcow2, create empty snapshot tag
        old_pflash = 'BOOT_OPTS+=" -drive file=$DEST.vars,if=pflash,unit=1,format=raw"'
        new_pflash = (
            '# cua-house: pflash must be qcow2 with matching snapshot tag for -loadvm\n'
            '    if [ -f "$DEST.vars" ] && ! qemu-img info "$DEST.vars" 2>/dev/null | grep -q "file format: qcow2"; then\n'
            '      qemu-img convert -f raw -O qcow2 "$DEST.vars" "$DEST.vars.q2"\n'
            '      mv "$DEST.vars.q2" "$DEST.vars"\n'
            '    fi\n'
            '    if [ -n "$LOADVM_SNAPSHOT" ] && ! qemu-img snapshot -l "$DEST.vars" 2>/dev/null | grep -q "$LOADVM_SNAPSHOT"; then\n'
            '      qemu-img snapshot -c "$LOADVM_SNAPSHOT" "$DEST.vars" 2>/dev/null || true\n'
            '    fi\n'
            '    BOOT_OPTS+=" -drive file=$DEST.vars,if=pflash,unit=1,format=qcow2"'
        )
        if old_pflash not in content:
            logger.warning("Could not apply pflash patch to boot.sh -- line not found.")
        else:
            content = content.replace(old_pflash, new_pflash)

        # Patch 2: inject -loadvm before boot.sh returns
        loadvm_snippet = (
            '\n# cua-house: resume from pre-baked snapshot\n'
            'if [ -n "$LOADVM_SNAPSHOT" ]; then\n'
            '  BOOT_OPTS+=" -loadvm $LOADVM_SNAPSHOT"\n'
            'fi\n\n'
        )
        if 'return 0' in content:
            content = content.replace('return 0', loadvm_snippet + 'return 0', 1)
        elif 'exec qemu-system-x86_64' in content:
            content = content.replace('exec qemu-system-x86_64', loadvm_snippet + 'exec qemu-system-x86_64', 1)
        else:
            logger.warning("Could not find insertion point in boot.sh for loadvm patch.")
            content = content.rstrip() + loadvm_snippet

        # Patch 3: disable boot watchdog for loadvm
        # entry.sh runs `( sleep 30; boot ) &` which kills QEMU if serial
        # console has no output after 30s (Ubuntu/loadvm produces none).
        # boot() checks `[ -f /run/shm/qemu.end ] && return 0`.
        # We use a delayed touch because power.sh (sourced AFTER boot.sh)
        # runs `rm -f /run/shm/qemu.*` at source time.
        watchdog_snippet = (
            '\n# cua-house: disable boot watchdog (no serial output with loadvm)\n'
            'if [ -n "$LOADVM_SNAPSHOT" ]; then\n'
            '  ( sleep 10; touch /run/shm/qemu.end ) &\n'
            'fi\n'
        )
        # Insert at very end of boot.sh (before final return 0)
        content = content.rstrip()
        if content.endswith('return 0'):
            content = content[:-len('return 0')] + watchdog_snippet + '\nreturn 0'
        else:
            content += watchdog_snippet

        patched.write_text(content, encoding="utf-8")
        patched.chmod(0o755)
        logger.info("Created patched boot.sh at %s", patched)
        return patched

    async def revert_vm(self, handle: VMHandle) -> None:
        """Revert VM to clean snapshot state via QMP loadvm.

        After loadvm, the VM is in its exact snapshot state -- disk, RAM, CPU
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
        """Destroy a broken VM and create a fresh replacement from template."""
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
            snapshot_name=handle.snapshot_name,
        )
        await self._start_vm_container(new_handle)
        await self.wait_ready(new_handle)
        new_handle.qmp = QMPClient(new_handle.container_name)
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

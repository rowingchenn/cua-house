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
from cua_house_common.models import TaskRequirement
from cua_house_server.config.loader import HostRuntimeConfig, ImageSpec
from cua_house_server.data.staging import StageResult, TaskDataManager
from cua_house_server.qmp.client import QMPClient

logger = logging.getLogger(__name__)


@dataclass
class VMHandle:
    """Handle for a persistent VM instance in the snapshot pool."""

    vm_id: str
    snapshot_name: str
    vcpus: int
    memory_gb: int
    disk_gb: int
    # guest_port → host_loopback_port for every port in the image's published_ports.
    published_ports: dict[int, int]
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
    from_cache: bool = False

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
        self._mutation_lock = asyncio.Lock()
        from cua_house_server._internal.port_pool import PortPool
        self._published_port_pool = PortPool(*self.config.published_port_range)
        self._novnc_port_pool = PortPool(*self.config.novnc_port_range)
        self._hotplug_handles: dict[str, VMHandle] = {}
        # Snapshot cache for shape-keyed loadvm acceleration (cluster mode).
        from cua_house_server.runtimes.snapshot_cache import SnapshotCache, compute_qemu_fingerprint
        cache_dir = config.snapshot_cache_dir
        try:
            fp = compute_qemu_fingerprint(config.docker_image)
        except Exception:
            fp = "unknown"
        self._snapshot_cache = SnapshotCache(cache_dir, fp)
        self._qemu_fingerprint = fp

    @property
    def slots_root(self) -> Path:
        return self.config.runtime_root / "slots"

    def list_cached_shapes(self) -> list:
        """Shapes currently present in this worker's snapshot cache.

        Returned list is a snapshot; entries are `SnapshotCache.CacheKey`
        tuples (image_key, image_version, vcpus, memory_gb, disk_gb).
        Consumed by heartbeat to inform master-side cache-affinity ranking.
        """
        return self._snapshot_cache.list_entries()

    async def wait_ready(self, handle: VMHandle) -> None:
        # Probe the first published port for /status readiness.
        primary_port = next(iter(handle.published_ports.values()))
        url = f"http://127.0.0.1:{primary_port}/status"
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
        os_family: str | None = None,
    ) -> StageResult:
        cname = container_name or handle.container_name
        cua_url = self.vm_published_url(handle, next(iter(handle.published_ports)))
        return await self.task_data.stage_phase(
            lease_id=lease_id,
            task_id=task_id,
            cua_url=cua_url,
            task_data=task_data,
            phase=phase,
            container_name=cname,
            use_symlink_inject=True,
            os_family=os_family,
        )

    # -- template provisioning ----------------------------------------

    async def pull_template(self, image_key: str, image: ImageSpec) -> None:
        """Ensure a single image's qcow2 template exists locally.

        Idempotent: returns immediately if the file already exists. Pulls from
        GCS otherwise. Called at worker startup (via ``prewarm_templates``)
        so tasks never wait on a GCS download.
        """
        if not image.enabled or image.local is None:
            return
        local_path = image.local.template_qcow2_path
        gcs_uri = image.local.gcs_uri
        if local_path.exists():
            logger.info("Template %s exists locally: %s", image_key, local_path)
            return
        if gcs_uri is None:
            raise FileNotFoundError(
                f"Template {local_path} not found and no gcs_uri configured for {image_key}"
            )
        logger.info("Pulling template %s from %s ...", image_key, gcs_uri)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        pull_start = time.perf_counter()
        await asyncio.to_thread(
            self._run, ["gsutil", "-m", "cp", gcs_uri, str(local_path)],
        )
        pull_s = time.perf_counter() - pull_start
        size_gb = local_path.stat().st_size / 1e9
        logger.info("Template %s pulled in %.1fs (%.1f GB)", image_key, pull_s, size_gb)
        self.event_logger.emit(
            "template_pulled",
            image_key=image_key,
            gcs_uri=gcs_uri,
            pull_s=pull_s,
            size_gb=round(size_gb, 1),
        )

    async def prewarm_templates(self, images: dict[str, ImageSpec]) -> None:
        """Pull every enabled local image's template in parallel.

        Runs at worker startup before the WS Register. Any failure raises
        and propagates to the caller so the manually started worker
        process can fail fast. Idempotent per-image pull, so
        re-runs after a partial previous run only fetch what's missing.
        """
        targets = [
            (key, image)
            for key, image in images.items()
            if image.enabled and image.local is not None
        ]
        if not targets:
            return
        logger.info("prewarming %d templates: %s", len(targets), [k for k, _ in targets])
        start = time.perf_counter()
        await asyncio.gather(
            *[self.pull_template(key, image) for key, image in targets]
        )
        elapsed_s = time.perf_counter() - start
        logger.info("prewarm completed in %.1fs", elapsed_s)
        self.event_logger.emit(
            "templates_prewarmed",
            image_count=len(targets),
            elapsed_s=round(elapsed_s, 1),
        )

    def _prepare_vm(
        self,
        *,
        vm_id: str,
        image: ImageSpec,
        vcpus: int,
        memory_gb: int,
        disk_gb: int,
        published_ports: dict[int, int],
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

        # Copy template qcow2 to slot.  The template is a clean base image;
        # shape-based snapshot tags are created at runtime (not pre-baked).
        # Uses --reflink=auto: instant on XFS, falls back to full copy on ext4.
        # Docker image expects "data.qcow2" (DISK_NAME=data in dockur/windows)
        disk = storage_dir / "data.qcow2"
        if disk.exists():
            disk.unlink()
        self._run(["cp", "--reflink=auto", str(template), str(disk)])

        # If the requested disk_gb is larger than the template's virtual size,
        # grow the slot qcow2. Never shrink — loadvm guarantees the guest sees
        # at least the shape we booted with. qemu-img resize is cheap on qcow2.
        try:
            info = self._run(["qemu-img", "info", "--output=json", str(disk)])
            import json as _json
            virtual_bytes = int(_json.loads(info.stdout or "{}").get("virtual-size", 0))
            want_bytes = int(disk_gb) * 1024 ** 3
            if want_bytes > virtual_bytes > 0:
                self._run(["qemu-img", "resize", str(disk), f"{disk_gb}G"])
        except Exception as exc:
            logger.warning("qemu-img resize skipped for %s: %s", disk, exc)

        container_name = f"cua-house-env-{vm_id}"
        return VMHandle(
            vm_id=vm_id,
            snapshot_name=snapshot_name,
            vcpus=vcpus,
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            published_ports=published_ports,
            novnc_port=novnc_port,
            storage_dir=storage_dir,
            logs_dir=logs_dir,
            disk_path=disk,
            container_name=container_name,
        )

    def _prepare_vm_from_source(
        self,
        *,
        vm_id: str,
        source_qcow2: Path,
        image: ImageSpec,
        vcpus: int,
        memory_gb: int,
        disk_gb: int,
        published_ports: dict[int, int],
        novnc_port: int,
        snapshot_name: str,
    ) -> VMHandle:
        """Like _prepare_vm but copies from an arbitrary qcow2 (cache hit)."""
        vm_root = self.slots_root / vm_id
        storage_dir = vm_root / "storage"
        logs_dir = vm_root / "logs"
        storage_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        disk = storage_dir / "data.qcow2"
        if disk.exists():
            disk.unlink()
        self._run(["cp", "--reflink=auto", str(source_qcow2), str(disk)])
        container_name = f"cua-house-env-{vm_id}"
        return VMHandle(
            vm_id=vm_id,
            snapshot_name=snapshot_name,
            vcpus=vcpus,
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            published_ports=published_ports,
            novnc_port=novnc_port,
            storage_dir=storage_dir,
            logs_dir=logs_dir,
            disk_path=disk,
            container_name=container_name,
        )

    async def _start_vm_container(
        self, handle: VMHandle, *, use_loadvm: bool = True,
    ) -> None:
        """Launch Docker container for a VM.

        Container persists across tasks -- only created once during pool init.
        When ``use_loadvm`` is False (cache-miss cold boot), the container
        starts without ``LOADVM_SNAPSHOT`` and QEMU performs a full cold boot
        instead of resuming from a snapshot.
        """
        self._run(["docker", "rm", "-f", handle.container_name], check=False)
        handle.boot_started_at = datetime.now(UTC)
        handle.start_monotonic = time.perf_counter()

        patched_boot = self._ensure_patched_boot_sh()

        cmd = [
            "docker", "run", "-d",
            "--name", handle.container_name,
            "--device=/dev/kvm",
            "--cap-add", "NET_ADMIN",
            "-v", f"{handle.storage_dir}:/storage",
            "-v", f"{self.config.task_data_root}:/data-store:ro",
            "-v", f"{patched_boot}:/run/boot.sh:ro",
            "-p", f"{self.config.vm_bind_address}:{handle.novnc_port}:8006",
        ]
        for guest_port, host_port in handle.published_ports.items():
            cmd.extend(["-p", f"{self.config.vm_bind_address}:{host_port}:{guest_port}"])
        cmd.extend([
            "-e", f"RAM_SIZE={handle.memory_gb}G",
            "-e", f"CPU_CORES={handle.vcpus}",
            "-e", "CPU_MODEL=host",
            "-e", "HV=N",
            "-e", "VM_NET_IP=172.30.0.2",
        ])
        if use_loadvm:
            cmd.extend(["-e", f"LOADVM_SNAPSHOT={handle.snapshot_name}"])
        cmd.append(self.config.docker_image)

        log_path = handle.logs_dir / "docker.log"
        self.event_logger.emit(
            "vm_starting",
            vm_id=handle.vm_id,
            container_name=handle.container_name,
            snapshot_name=handle.snapshot_name,
            vcpus=handle.vcpus,
            memory_gb=handle.memory_gb,
        )
        result = self._run(cmd)
        log_path.write_text(result.stdout or "", encoding="utf-8")

        # Fix hairpin NAT for Docker port-mapped traffic.
        #
        # dockur's iptables PREROUTING DNAT correctly rewrites dst from the
        # container IP (172.17.0.2) to the guest IP (172.30.0.2), but the
        # existing MASQUERADE rule only covers traffic leaving via eth0.
        # Port-mapped packets are forwarded from eth0 to the internal "docker"
        # bridge instead, so the guest sees the original source IP (in the
        # 172.17.0.0/16 Docker subnet) which it cannot route back to.
        # Adding MASQUERADE on the bridge egress ensures the guest sees its
        # own gateway (172.30.0.1) as the source and replies route correctly;
        # conntrack handles the bidirectional NAT reversal automatically.
        self._run(
            [
                "docker", "exec", handle.container_name,
                "iptables", "-t", "nat", "-A", "POSTROUTING",
                "-d", "172.30.0.2/32", "-o", "docker", "-j", "MASQUERADE",
            ],
            check=False,
        )

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
            '    if [ -n "${LOADVM_SNAPSHOT:-}" ] && ! qemu-img snapshot -l "$DEST.vars" 2>/dev/null | grep -q "$LOADVM_SNAPSHOT"; then\n'
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
            'if [ -n "${LOADVM_SNAPSHOT:-}" ]; then\n'
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
            'if [ -n "${LOADVM_SNAPSHOT:-}" ]; then\n'
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

    def vm_published_url(self, handle: VMHandle, guest_port: int) -> str:
        host_port = handle.published_ports[guest_port]
        return f"http://127.0.0.1:{host_port}"

    def vm_novnc_local_url(self, handle: VMHandle) -> str:
        return f"http://127.0.0.1:{handle.novnc_port}"

    # -- Hot-plug (cluster mode) ---------------------------------------

    async def provision_vm(
        self,
        *,
        image: ImageSpec,
        vcpus: int,
        memory_gb: int,
        disk_gb: int | None = None,
    ) -> VMHandle:
        """Provision a single VM with snapshot-cache acceleration.

        Cache hit (shape previously booted on this worker):
          reflink cached qcow2 → slot, docker run with -loadvm, ~seconds.
        Cache miss (first-time shape):
          reflink base template → slot, docker run cold-boot (~4-5 min),
          QMP savevm into slot qcow2, reflink slot → cache for next time.

        The cold-boot VM immediately serves tasks — the cache write happens
        *after* CUA readiness so the first task doesn't wait for double I/O.

        Callers own the returned handle and must pair each successful call
        with `destroy_vm(handle)` when the task that requested it is done.
        """
        from uuid import uuid4
        from cua_house_server.runtimes.snapshot_cache import CacheKey, shape_stem

        resolved_disk_gb = disk_gb if disk_gb is not None else image.default_disk_gb
        # Snapshot tag follows the VM shape, not the image key. This is an
        # internal QEMU detail — never persisted in docs, GCS, or config.
        snapshot = shape_stem(vcpus, memory_gb, resolved_disk_gb)
        cache_key = CacheKey(
            image_key=image.key,
            image_version=image.version,
            vcpus=vcpus,
            memory_gb=memory_gb,
            disk_gb=resolved_disk_gb,
        )
        cached_path = self._snapshot_cache.lookup(cache_key)
        cache_hit = cached_path is not None

        async with self._mutation_lock:
            await self.pull_template(image.key, image)
            vm_id = str(uuid4())
            published_ports = {
                guest_port: self._published_port_pool.allocate()
                for guest_port in image.published_ports
            }
            novnc_port = self._novnc_port_pool.allocate()

            if cache_hit:
                handle = await asyncio.to_thread(
                    self._prepare_vm_from_source,
                    vm_id=vm_id,
                    source_qcow2=cached_path,
                    image=image,
                    vcpus=vcpus,
                    memory_gb=memory_gb,
                    disk_gb=resolved_disk_gb,
                    published_ports=published_ports,
                    novnc_port=novnc_port,
                    snapshot_name=snapshot,
                )
                handle.from_cache = True
                await self._start_vm_container(handle, use_loadvm=True)
            else:
                handle = await asyncio.to_thread(
                    self._prepare_vm,
                    vm_id=vm_id,
                    image=image,
                    vcpus=vcpus,
                    memory_gb=memory_gb,
                    disk_gb=resolved_disk_gb,
                    published_ports=published_ports,
                    novnc_port=novnc_port,
                    snapshot_name=snapshot,
                )
                handle.from_cache = False
                await self._start_vm_container(handle, use_loadvm=False)

        try:
            await self.wait_ready(handle)
            handle.ready_at = datetime.now(UTC)
            handle.qmp = QMPClient(handle.container_name)
        except Exception:
            self._run(["docker", "rm", "-f", handle.container_name], check=False)
            for port in published_ports.values():
                self._published_port_pool.release(port)
            self._novnc_port_pool.release(novnc_port)
            raise

        if not cache_hit:
            try:
                logger.info("cache miss for %s — savevm + cache write", cache_key.stem)
                # savevm on a ~50GB qcow2 with running state takes minutes on
                # first run. 300s is generous for disks up to several hundred GB.
                await handle.qmp.save_snapshot(handle.snapshot_name, timeout=300)
                self._snapshot_cache.write(cache_key, handle.disk_path)
            except Exception:
                logger.warning("savevm/cache-write failed; VM still usable", exc_info=True)

        self._hotplug_handles[vm_id] = handle
        boot_s = time.perf_counter() - (handle.start_monotonic or time.perf_counter())
        self.event_logger.emit(
            "vm_hot_added",
            vm_id=vm_id,
            snapshot_name=snapshot,
            vcpus=vcpus,
            memory_gb=memory_gb,
            disk_gb=resolved_disk_gb,
            from_cache=cache_hit,
            boot_s=round(boot_s, 1),
        )
        return handle

    async def destroy_vm(self, handle: VMHandle) -> None:
        """Destroy a provisioned VM. Idempotent; unknown handles are no-ops.

        Tears down the container, wipes the slot directory, and releases the
        host-loopback ports back to the runtime's pools. In the ephemeral-VM
        model this is called once per task on completion; there is no
        "revert to ready" path.
        """
        # Drop from tracking table first so double-calls short-circuit.
        if self._hotplug_handles.pop(handle.vm_id, None) is None:
            return
        async with self._mutation_lock:
            self._run(["docker", "rm", "-f", handle.container_name], check=False)
            slot_root = self.slots_root / handle.vm_id
            if slot_root.exists():
                shutil.rmtree(slot_root, ignore_errors=True)
            for port in handle.published_ports.values():
                self._published_port_pool.release(port)
            self._novnc_port_pool.release(handle.novnc_port)
        self.event_logger.emit(
            "vm_destroyed",
            vm_id=handle.vm_id,
            snapshot_name=handle.snapshot_name,
        )

    def hotplug_handle(self, vm_id: str) -> VMHandle | None:
        return self._hotplug_handles.get(vm_id)

    def list_hotplug_handles(self) -> list[VMHandle]:
        return list(self._hotplug_handles.values())

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

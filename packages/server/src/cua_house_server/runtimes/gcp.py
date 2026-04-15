"""GCP VM runtime backend for cloud-based evaluation slots."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from cua_house_common.events import JsonlEventLogger
from cua_house_common.models import TaskRequirement
from cua_house_server.config.loader import HostRuntimeConfig, ImageSpec
from cua_house_server.data.staging import StageResult

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GCPSlotHandle:
    """Slot handle for GCP-managed VM instances."""

    slot_id: str
    image_key: str
    vcpus: int
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
        vcpus: int,
        memory_gb: int,
        cua_port: int,
        novnc_port: int,
        lease_id: str,
        task_id: str,
        task_data: TaskRequirement.TaskDataRequest | None = None,
    ) -> GCPSlotHandle:
        vm_name = f"cua-house-env-{slot_id[:8]}"
        handle = GCPSlotHandle(
            slot_id=slot_id,
            image_key=image.key,
            vcpus=vcpus,
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

        # 2. Create VM -- prefer --image (faster) over --disk from snapshot
        machine_type = image.gcp_machine_type
        cmd = [
            "compute", "instances", "create", handle.vm_name,
            f"--project={image.gcp_project}",
            f"--zone={image.gcp_zone}",
            f"--machine-type={machine_type}",
            f"--network={image.gcp_network}",
            f"--service-account={image.gcp_service_account}",
            "--scopes=https://www.googleapis.com/auth/cloud-platform",
            "--maintenance-policy=TERMINATE",
            "--tags=cua-house",
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
        """Delete any cua-house-env-* and agenthle-env-* VMs left from previous runs."""
        for pattern in ["cua-house-env-", "agenthle-env-"]:
            result = self._run_gcloud([
                "compute", "instances", "list",
                f"--filter=name~^{pattern}",
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
        # For GCP mode, task data is on the attached data disk -- no host-side validation needed.
        pass

    async def stage_task_phase(
        self,
        *,
        handle: GCPSlotHandle,
        task_id: str,
        lease_id: str,
        task_data: TaskRequirement.TaskDataRequest | None,
        phase: str,
        container_name: str | None = None,
        os_family: str | None = None,
    ) -> StageResult:
        """Control access to task data via ACLs on the data disk.

        Whitelist strategy:
        - runtime phase: enumerate the task directory and deny access to every
          subdirectory that is NOT input/, software/, or output/. Also deny all
          sibling task directories so the agent cannot peek at other tasks.
        - eval phase: remove the deny on reference/ so evaluator can read it.

        Windows: NTFS ACLs via icacls.  Linux: chmod.
        """
        if task_data is None or not task_data.requires_task_data:
            return StageResult(skipped=True)

        is_linux = os_family == "linux"
        sep = "/" if is_linux else "\\"
        cua_url = self.cua_local_url(handle)
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0)) as client:
            if phase == "runtime":
                whitelist = set()
                for d in (task_data.input_dir, task_data.software_dir, task_data.remote_output_dir):
                    if d:
                        whitelist.add(d.rstrip(sep).rsplit(sep, 1)[-1].lower())

                task_dir = task_data.input_dir.rstrip(sep).rsplit(sep, 1)[0] if task_data.input_dir else None
                if task_dir:
                    # 1. Deny non-whitelisted subdirs in task dir
                    subdirs = await self._list_subdirs(client, cua_url, task_dir, is_linux=is_linux)
                    for subdir in subdirs:
                        if subdir.lower() not in whitelist:
                            await self._deny_dir(client, cua_url, f"{task_dir}{sep}{subdir}", is_linux=is_linux)

                    # 2. Deny sibling tasks in same category
                    category_dir = task_dir.rsplit(sep, 1)[0]
                    task_name = task_dir.rsplit(sep, 1)[-1]
                    siblings = await self._list_subdirs(client, cua_url, category_dir, is_linux=is_linux)
                    for sibling in siblings:
                        if sibling != task_name:
                            await self._deny_dir(client, cua_url, f"{category_dir}{sep}{sibling}", is_linux=is_linux)

                    # 3. Deny other categories
                    data_root = category_dir.rsplit(sep, 1)[0]
                    category_name = category_dir.rsplit(sep, 1)[-1]
                    categories = await self._list_subdirs(client, cua_url, data_root, is_linux=is_linux)
                    for cat in categories:
                        if cat != category_name:
                            await self._deny_dir(client, cua_url, f"{data_root}{sep}{cat}", is_linux=is_linux)

                self.event_logger.emit(
                    "task_data_acl_locked",
                    lease_id=lease_id,
                    task_id=task_id,
                    input_dir=task_data.input_dir,
                    software_dir=task_data.software_dir,
                )
            else:  # eval
                if task_data.reference_dir:
                    await self._allow_dir(client, cua_url, task_data.reference_dir, is_linux=is_linux)
                    self.event_logger.emit(
                        "task_data_reference_unlocked",
                        lease_id=lease_id,
                        task_id=task_id,
                        reference_dir=task_data.reference_dir,
                    )
        return StageResult(file_count=0, bytes_staged=0)

    async def _deny_dir(
        self, client: httpx.AsyncClient, cua_url: str, path: str, *, is_linux: bool,
    ) -> None:
        if is_linux:
            await self._run_remote(client, cua_url, f'chmod -R 000 "{path}"')
        else:
            await self._run_remote(client, cua_url, f'icacls "{path}" /deny User:(OI)(CI)F /Q')

    async def _allow_dir(
        self, client: httpx.AsyncClient, cua_url: str, path: str, *, is_linux: bool,
    ) -> None:
        if is_linux:
            await self._run_remote(client, cua_url, f'chmod -R 755 "{path}"')
        else:
            await self._run_remote(client, cua_url, f'icacls "{path}" /remove:d User /Q')

    async def _list_subdirs(
        self, client: httpx.AsyncClient, cua_url: str, path: str, *, is_linux: bool = False,
    ) -> list[str]:
        if is_linux:
            result = await self._run_remote(client, cua_url, f'ls -1 "{path}" 2>/dev/null')
        else:
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
        if not hasattr(self, "_images"):
            raise RuntimeError("GCPVMRuntime._images not set -- call set_images() first")
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
                result = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
        if result is None:
            raise RuntimeError("no valid response for remote command")
        if result.get("success") is False:
            raise RuntimeError(f"remote command failed: {result.get('error', 'unknown')}")
        return result

    @staticmethod
    def _powershell(script: str) -> str:
        compact = "; ".join(line.strip() for line in script.strip().splitlines() if line.strip())
        return f'powershell -NoProfile -Command "{compact}"'

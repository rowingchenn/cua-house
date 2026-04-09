"""Host-side task-data validation and guest staging helpers."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from cua_house_common.events import JsonlEventLogger
from cua_house_common.models import TaskRequirement


logger = logging.getLogger(__name__)

PhaseName = Literal["runtime", "eval"]

SAMBA_SHARE_ROOT = r"\\host.lan\Data"
LINUX_DATA_MOUNT = "/media/user/data/agenthle"
LINUX_SAMBA_SOURCE = "//host.lan/Data/agenthle"


@dataclass(slots=True)
class StageResult:
    file_count: int = 0
    bytes_staged: int = 0
    skipped: bool = False


class TaskDataManager:
    """Validate and stage task data from the host data disk into a leased guest."""

    def __init__(self, task_data_root: Path | None, event_logger: JsonlEventLogger):
        self.task_data_root = task_data_root
        self.event_logger = event_logger

    def validate_runtime_data(
        self,
        *,
        task_id: str,
        task_data: TaskRequirement.TaskDataRequest | None,
    ) -> None:
        if task_data is None or not task_data.requires_task_data:
            return
        source_root = self._source_root(task_data)
        if not source_root.exists():
            self.event_logger.emit(
                "task_data_validation_failed",
                task_id=task_id,
                phase="runtime",
                source_root=source_root,
                error="task data root missing",
            )
            raise ValueError(f"task data root missing: {source_root}")
        if task_data.input_dir and not (source_root / "input").exists():
            self.event_logger.emit(
                "task_data_validation_failed",
                task_id=task_id,
                phase="runtime",
                source_root=source_root,
                error="input directory missing",
            )
            raise ValueError(f"task input directory missing: {source_root / 'input'}")
        self.event_logger.emit(
            "task_data_validated",
            task_id=task_id,
            phase="runtime",
            source_root=source_root,
            task_category=task_data.task_category,
            task_tag=task_data.task_tag,
        )

    async def stage_phase(
        self,
        *,
        lease_id: str,
        task_id: str,
        cua_url: str,
        task_data: TaskRequirement.TaskDataRequest | None,
        phase: PhaseName,
        container_name: str | None = None,
        vm_pool: bool = False,
        os_type: str | None = None,
    ) -> StageResult:
        if task_data is None or not task_data.requires_task_data:
            return StageResult(skipped=True)

        started = asyncio.get_running_loop().time()
        self.event_logger.emit(
            "task_data_stage_started",
            lease_id=lease_id,
            task_id=task_id,
            phase=phase,
            task_category=task_data.task_category,
            task_tag=task_data.task_tag,
            vm_pool=vm_pool,
        )

        try:
            if vm_pool:
                result = await self._stage_vm_pool(
                    cua_url=cua_url,
                    task_data=task_data,
                    phase=phase,
                    os_type=os_type,
                )
            else:
                result = await self._stage_samba(
                    cua_url=cua_url,
                    task_data=task_data,
                    phase=phase,
                    container_name=container_name,
                )
        except Exception as exc:
            self.event_logger.emit(
                "task_data_stage_failed",
                lease_id=lease_id,
                task_id=task_id,
                phase=phase,
                task_category=task_data.task_category,
                task_tag=task_data.task_tag,
                error=str(exc),
                duration_s=asyncio.get_running_loop().time() - started,
            )
            raise

        self.event_logger.emit(
            "task_data_stage_completed",
            lease_id=lease_id,
            task_id=task_id,
            phase=phase,
            task_category=task_data.task_category,
            task_tag=task_data.task_tag,
            file_count=result.file_count,
            bytes_staged=result.bytes_staged,
            duration_s=asyncio.get_running_loop().time() - started,
        )
        return result

    # ------------------------------------------------------------------
    # VM pool staging: E: drive mapping + NTFS ACLs (aligned with GCP)
    # ------------------------------------------------------------------

    async def _stage_vm_pool(
        self,
        *,
        cua_url: str,
        task_data: TaskRequirement.TaskDataRequest,
        phase: PhaseName,
        os_type: str | None = None,
    ) -> StageResult:
        """Stage task data for VM-pool VMs.

        Windows: E: drive mapping via Samba + NTFS ACLs (icacls).
        Linux: CIFS mount at /media/user/data/agenthle + chmod.
        """
        is_linux = os_type in ("linux", "ubuntu")
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0)) as client:
            if phase == "runtime":
                if is_linux:
                    await self._mount_data_linux(client, cua_url)
                    # Reset output dir so stale files from prior runs don't
                    # leak in (host upper layer persists across VM reverts).
                    if task_data.remote_output_dir:
                        await self._reset_remote_dir_linux(client, cua_url, task_data.remote_output_dir)
                    await self._apply_runtime_acls_linux(client, cua_url, task_data)
                else:
                    await self._map_e_drive(client, cua_url)
                    # Reset output dir (same reasoning as Linux branch).
                    if task_data.remote_output_dir:
                        await self._reset_remote_dir(client, cua_url, task_data.remote_output_dir)
                    await self._apply_runtime_acls(client, cua_url, task_data)

            else:  # eval
                if task_data.reference_dir:
                    if is_linux:
                        await self._run_remote(
                            client, cua_url,
                            f'chmod -R 755 "{task_data.reference_dir}"',
                        )
                    else:
                        await self._run_remote(
                            client, cua_url,
                            f'icacls "{task_data.reference_dir}" /remove:d User /Q',
                        )
                    self.event_logger.emit(
                        "task_data_reference_unlocked",
                        reference_dir=task_data.reference_dir,
                    )

        return StageResult(file_count=0, bytes_staged=0)

    async def _map_e_drive(self, client: httpx.AsyncClient, cua_url: str) -> None:
        """Map ``\\\\host.lan\\Data`` as E: drive inside the guest."""
        await self._run_remote(
            client, cua_url,
            self._powershell(
                "$ErrorActionPreference='Stop'; "
                "net use E: '\\\\host.lan\\Data' /persistent:no 2>&1 | Out-Null; "
                "if (-not (Test-Path 'E:\\')) { throw 'E: drive not mapped' }"
            ),
        )

    async def _apply_runtime_acls(
        self,
        client: httpx.AsyncClient,
        cua_url: str,
        task_data: TaskRequirement.TaskDataRequest,
    ) -> None:
        """Apply NTFS ACLs to restrict agent access (same logic as GCPVMRuntime).

        Whitelist: only input/, software/, output/ are accessible.
        Deny: reference/ in task dir, sibling tasks, other categories.
        """
        whitelist = set()
        for d in (task_data.input_dir, task_data.software_dir, task_data.remote_output_dir):
            if d:
                whitelist.add(d.rstrip("\\").rsplit("\\", 1)[-1].lower())

        task_dir = task_data.input_dir.rstrip("\\").rsplit("\\", 1)[0] if task_data.input_dir else None
        if not task_dir:
            return

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
            input_dir=task_data.input_dir,
            software_dir=task_data.software_dir,
        )

    async def _list_subdirs(self, client: httpx.AsyncClient, cua_url: str, path: str) -> list[str]:
        """List subdirectory names using cmd dir (Windows)."""
        result = await self._run_remote(client, cua_url, f'dir "{path}" /AD /B')
        stdout = (result.get("stdout", "") or "").strip()
        if not stdout or result.get("return_code", 1) != 0:
            return []
        return [s.strip() for s in stdout.splitlines() if s.strip()]

    # ------------------------------------------------------------------
    # Linux VM pool staging helpers
    # ------------------------------------------------------------------

    async def _mount_data_linux(self, client: httpx.AsyncClient, cua_url: str) -> None:
        """Mount Samba share via CIFS inside the Linux guest."""
        await self._run_remote(
            client, cua_url,
            f"sudo mkdir -p {LINUX_DATA_MOUNT} && "
            f"mountpoint -q {LINUX_DATA_MOUNT} || "
            f"sudo mount -t cifs {LINUX_SAMBA_SOURCE} {LINUX_DATA_MOUNT} "
            f"-o guest,uid=1000,gid=1000,file_mode=0755,dir_mode=0755",
        )

    async def _apply_runtime_acls_linux(
        self,
        client: httpx.AsyncClient,
        cua_url: str,
        task_data: TaskRequirement.TaskDataRequest,
    ) -> None:
        """Deny access to non-whitelisted dirs using chmod (Linux equivalent of icacls)."""
        whitelist = set()
        for d in (task_data.input_dir, task_data.software_dir, task_data.remote_output_dir):
            if d:
                whitelist.add(d.rstrip("/").rsplit("/", 1)[-1].lower())

        task_dir = task_data.input_dir.rstrip("/").rsplit("/", 1)[0] if task_data.input_dir else None
        if not task_dir:
            return

        # 1. Deny non-whitelisted subdirs in task dir
        subdirs = await self._list_subdirs_linux(client, cua_url, task_dir)
        for subdir in subdirs:
            if subdir.lower() not in whitelist:
                await self._run_remote(
                    client, cua_url,
                    f'chmod -R 000 "{task_dir}/{subdir}"',
                )

        # 2. Deny sibling tasks in same category
        category_dir = task_dir.rsplit("/", 1)[0]
        task_name = task_dir.rsplit("/", 1)[-1]
        siblings = await self._list_subdirs_linux(client, cua_url, category_dir)
        for sibling in siblings:
            if sibling != task_name:
                await self._run_remote(
                    client, cua_url,
                    f'chmod -R 000 "{category_dir}/{sibling}"',
                )

        # 3. Deny other categories
        data_root = category_dir.rsplit("/", 1)[0]
        category_name = category_dir.rsplit("/", 1)[-1]
        categories = await self._list_subdirs_linux(client, cua_url, data_root)
        for cat in categories:
            if cat != category_name:
                await self._run_remote(
                    client, cua_url,
                    f'chmod -R 000 "{data_root}/{cat}"',
                )

        self.event_logger.emit(
            "task_data_acl_locked",
            input_dir=task_data.input_dir,
            software_dir=task_data.software_dir,
        )

    async def _list_subdirs_linux(self, client: httpx.AsyncClient, cua_url: str, path: str) -> list[str]:
        """List subdirectory names using ls (Linux)."""
        result = await self._run_remote(client, cua_url, f'ls -1 "{path}" 2>/dev/null')
        stdout = (result.get("stdout", "") or "").strip()
        if not stdout or result.get("return_code", 1) != 0:
            return []
        return [s.strip() for s in stdout.splitlines() if s.strip()]

    async def _ensure_remote_dir_linux(self, client: httpx.AsyncClient, cua_url: str, remote_dir: str) -> None:
        """Create directory on Linux guest."""
        await self._run_remote(client, cua_url, f'mkdir -p "{remote_dir}"')

    async def _reset_remote_dir_linux(self, client: httpx.AsyncClient, cua_url: str, remote_dir: str) -> None:
        """Remove and recreate directory on Linux guest (clears stale content)."""
        await self._run_remote(client, cua_url, f'rm -rf "{remote_dir}" && mkdir -p "{remote_dir}"')

    # ------------------------------------------------------------------
    # Samba-based staging (original per-container path)
    # ------------------------------------------------------------------

    async def _stage_samba(
        self,
        *,
        cua_url: str,
        task_data: TaskRequirement.TaskDataRequest,
        phase: PhaseName,
        container_name: str | None = None,
    ) -> StageResult:
        """Stage task data via Samba bind mounts + robocopy (original path)."""
        source_root = self._source_root(task_data)
        staged_files = 0
        staged_bytes = 0

        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=60.0)) as client:
            if phase == "runtime":
                if task_data.reference_dir:
                    await self._remove_remote_dir(client, cua_url, task_data.reference_dir)
                if task_data.remote_output_dir:
                    await self._reset_remote_dir(client, cua_url, task_data.remote_output_dir)
                if task_data.input_dir:
                    host_dir = source_root / "input"
                    if not host_dir.exists():
                        raise FileNotFoundError(f"required task data directory missing: {host_dir}")
                    count, size = self._count_tree(host_dir)
                    if count > 0:
                        await self._stage_from_samba(client, cua_url, "input", task_data.input_dir)
                    staged_files += count
                    staged_bytes += size
                if task_data.software_dir:
                    host_dir = source_root / "software"
                    if host_dir.exists():
                        count, size = self._count_tree(host_dir)
                        if count > 0:
                            await self._stage_from_samba(client, cua_url, "software", task_data.software_dir)
                        staged_files += count
                        staged_bytes += size
            else:
                if task_data.reference_dir:
                    host_dir = source_root / "reference"
                    if not host_dir.exists():
                        raise FileNotFoundError(f"required reference directory missing: {host_dir}")
                    count, size = self._count_tree(host_dir)
                    if count > 0:
                        if container_name is None:
                            raise ValueError("container_name is required for eval-phase staging via docker cp")
                        self._docker_cp(container_name, host_dir, "/shared/reference")
                        await self._stage_from_samba(client, cua_url, "reference", task_data.reference_dir)
                    staged_files += count
                    staged_bytes += size

        return StageResult(file_count=staged_files, bytes_staged=staged_bytes)

    def _source_root(self, task_data: TaskRequirement.TaskDataRequest) -> Path:
        if self.task_data_root is None:
            raise ValueError("server task_data_root is not configured")
        if not task_data.source_relpath:
            raise ValueError("task data source_relpath is missing")
        return (self.task_data_root / task_data.source_relpath).resolve()

    # ------------------------------------------------------------------
    # Samba-based staging (primary path)
    # ------------------------------------------------------------------

    async def _stage_from_samba(
        self,
        client: httpx.AsyncClient,
        cua_url: str,
        share_subdir: str,
        remote_dir: str,
    ) -> None:
        """Copy files from the Samba share inside the guest using robocopy."""
        samba_source = f"{SAMBA_SHARE_ROOT}\\{share_subdir}"
        await self._run_remote(
            client,
            cua_url,
            self._powershell(
                f"""
                $ErrorActionPreference='Stop'
                $src = '{self._ps(remote_path=samba_source)}'
                $dest = '{self._ps(remote_path=remote_dir)}'
                if (Test-Path -LiteralPath $dest) {{
                    Remove-Item -LiteralPath $dest -Recurse -Force
                }}
                New-Item -ItemType Directory -Force -Path $dest | Out-Null
                $null = robocopy $src $dest /E /NFL /NDL /NJH /NJS /R:5 /W:3
                if ($LASTEXITCODE -ge 8) {{ throw "robocopy failed with exit code $LASTEXITCODE" }}
                $global:LASTEXITCODE = 0
                """,
            ),
        )

    @staticmethod
    def _docker_cp(container_name: str, source: Path, dest_in_container: str) -> None:
        """Copy a host directory into a running Docker container."""
        result = subprocess.run(
            ["docker", "cp", f"{source}/.", f"{container_name}:{dest_in_container}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"docker cp to {container_name}:{dest_in_container} failed "
                f"(rc={result.returncode}): {result.stderr}"
            )

    # ------------------------------------------------------------------
    # Legacy HTTP-upload staging (kept as fallback)
    # ------------------------------------------------------------------

    async def _stage_directory_legacy(
        self,
        client: httpx.AsyncClient,
        cua_url: str,
        *,
        lease_id: str,
        phase: PhaseName,
        source_dir: Path,
        remote_dir: str,
        required: bool,
    ) -> tuple[int, int]:
        if not source_dir.exists():
            if required:
                raise FileNotFoundError(f"required task data directory missing: {source_dir}")
            return 0, 0

        file_count, total_bytes = self._count_tree(source_dir)
        await self._reset_remote_dir(client, cua_url, remote_dir)
        if file_count == 0:
            return 0, 0

        archive_path = self._build_archive(source_dir, lease_id=lease_id, phase=phase)
        try:
            guest_temp_root = rf"C:\Users\User\AppData\Local\Temp\cua_house_stage\{lease_id}"
            await self._ensure_remote_dir(client, cua_url, guest_temp_root)
            guest_zip_path = rf"{guest_temp_root}\{phase}_{source_dir.name}.zip"
            await self._upload_bytes(client, cua_url, guest_zip_path, archive_path.read_bytes())
            await self._run_remote(
                client,
                cua_url,
                self._powershell(
                    f"""
                    $ErrorActionPreference='Stop'
                    $zip = '{self._ps(remote_path=guest_zip_path)}'
                    $dest = '{self._ps(remote_path=remote_dir)}'
                    Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
                    Remove-Item -LiteralPath $zip -Force
                    """,
                ),
            )
        finally:
            archive_path.unlink(missing_ok=True)
        return file_count, total_bytes

    # ------------------------------------------------------------------
    # Remote helpers (shared by both paths)
    # ------------------------------------------------------------------

    async def _reset_remote_dir(self, client: httpx.AsyncClient, cua_url: str, remote_dir: str) -> None:
        await self._run_remote(
            client,
            cua_url,
            self._powershell(
                f"""
                $ErrorActionPreference='Stop'
                $target = '{self._ps(remote_path=remote_dir)}'
                if (Test-Path -LiteralPath $target) {{
                    Remove-Item -LiteralPath $target -Recurse -Force
                }}
                New-Item -ItemType Directory -Force -Path $target | Out-Null
                """,
            ),
        )

    async def _ensure_remote_dir(self, client: httpx.AsyncClient, cua_url: str, remote_dir: str) -> None:
        await self._run_remote(
            client,
            cua_url,
            self._powershell(
                f"""
                $ErrorActionPreference='Stop'
                New-Item -ItemType Directory -Force -Path '{self._ps(remote_path=remote_dir)}' | Out-Null
                """,
            ),
        )

    async def _remove_remote_dir(self, client: httpx.AsyncClient, cua_url: str, remote_dir: str) -> None:
        await self._run_remote(
            client,
            cua_url,
            self._powershell(
                f"""
                $ErrorActionPreference='Stop'
                $target = '{self._ps(remote_path=remote_dir)}'
                if (Test-Path -LiteralPath $target) {{
                    Remove-Item -LiteralPath $target -Recurse -Force
                }}
                """,
            ),
        )

    async def _upload_bytes(self, client: httpx.AsyncClient, cua_url: str, remote_path: str, content: bytes) -> None:
        chunk_size = 2 * 1024 * 1024
        append = False
        for offset in range(0, len(content), chunk_size):
            chunk = content[offset : offset + chunk_size]
            await self._send_command(
                client,
                cua_url,
                "write_bytes",
                {
                    "path": remote_path,
                    "content_b64": base64.b64encode(chunk).decode("ascii"),
                    "append": append,
                },
            )
            append = True

    async def _run_remote(self, client: httpx.AsyncClient, cua_url: str, command: str) -> dict:
        return await self._send_command(client, cua_url, "run_command", {"command": command})

    async def _send_command(
        self,
        client: httpx.AsyncClient,
        cua_url: str,
        command: str,
        params: dict,
    ) -> dict:
        response = await client.post(f"{cua_url.rstrip('/')}/cmd", json={"command": command, "params": params})
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
            raise RuntimeError(f"no valid response for command {command!r}")
        if result.get("success") is False:
            raise RuntimeError(f"command {command!r} failed: {result.get('error', 'unknown')}")
        return result

    @staticmethod
    def _powershell(script: str) -> str:
        compact = "; ".join(line.strip() for line in script.strip().splitlines() if line.strip())
        return f"powershell -NoProfile -Command \"{compact}\""

    @staticmethod
    def _ps(*, remote_path: str) -> str:
        return remote_path.replace("'", "''")

    @staticmethod
    def _count_tree(source_dir: Path) -> tuple[int, int]:
        file_count = 0
        total_bytes = 0
        for path in source_dir.rglob("*"):
            if not path.is_file():
                continue
            file_count += 1
            total_bytes += path.stat().st_size
        return file_count, total_bytes

    @staticmethod
    def _build_archive(source_dir: Path, *, lease_id: str, phase: PhaseName) -> Path:
        tmp = tempfile.NamedTemporaryFile(prefix=f"cua_house_{lease_id}_{phase}_{source_dir.name}_", suffix=".zip", delete=False)
        tmp.close()
        archive_path = Path(tmp.name)
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(source_dir.rglob("*")):
                if not path.is_file():
                    continue
                zf.write(path, arcname=path.relative_to(source_dir))
        return archive_path

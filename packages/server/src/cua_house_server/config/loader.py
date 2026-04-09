"""Configuration loading for cua-house-server."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from cua_house_common.models import VMPoolEntry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LocalImageConfig:
    """Local QEMU runtime configuration for an image."""

    template_qcow2_path: Path
    gcs_uri: str | None = None
    default_cpu_cores: int = 4
    default_memory_gb: int = 8


@dataclass(slots=True)
class GCPImageConfig:
    """GCP runtime configuration for an image."""

    project: str
    zone: str
    network: str
    service_account: str
    boot_image: str | None = None
    boot_snapshot: str | None = None
    boot_disk_gb: int = 64
    data_snapshot: str | None = None
    data_disk_gb: int = 200
    gpu_type: str | None = None
    gpu_count: int = 0
    default_machine_type: str = "e2-standard-2"
    max_concurrent_vms: int = 4


@dataclass(slots=True)
class ImageSpec:
    """Image specification supporting dual local/gcp modes.

    At least one of ``local`` or ``gcp`` must be set.  The scheduler
    picks the runtime based on which sub-configs are present and which
    runtimes the server has available.
    """

    key: str
    enabled: bool
    local: LocalImageConfig | None = None
    gcp: GCPImageConfig | None = None

    # --- Backwards-compatible helpers ---

    @property
    def runtime_mode(self) -> str:
        """Primary runtime mode (local preferred if both present)."""
        if self.local is not None:
            return "local"
        return "gcp"

    @property
    def default_cpu_cores(self) -> int:
        if self.local:
            return self.local.default_cpu_cores
        return 4

    @property
    def default_memory_gb(self) -> int:
        if self.local:
            return self.local.default_memory_gb
        return 16

    @property
    def template_qcow2_path(self) -> Path | None:
        return self.local.template_qcow2_path if self.local else None

    @property
    def golden_qcow2_path(self) -> Path | None:
        """Alias for template_qcow2_path (used by qemu runtime)."""
        return self.template_qcow2_path

    # GCP convenience accessors (for gcp.py backward compat)
    @property
    def gcp_project(self) -> str | None:
        return self.gcp.project if self.gcp else None

    @property
    def gcp_zone(self) -> str | None:
        return self.gcp.zone if self.gcp else None

    @property
    def gcp_network(self) -> str | None:
        return self.gcp.network if self.gcp else None

    @property
    def gcp_service_account(self) -> str | None:
        return self.gcp.service_account if self.gcp else None

    @property
    def gcp_machine_type(self) -> str | None:
        return self.gcp.default_machine_type if self.gcp else None

    @property
    def gcp_boot_image(self) -> str | None:
        return self.gcp.boot_image if self.gcp else None

    @property
    def gcp_boot_snapshot(self) -> str | None:
        return self.gcp.boot_snapshot if self.gcp else None

    @property
    def gcp_data_snapshot(self) -> str | None:
        return self.gcp.data_snapshot if self.gcp else None

    @property
    def gcp_boot_disk_gb(self) -> int:
        return self.gcp.boot_disk_gb if self.gcp else 64

    @property
    def gcp_data_disk_gb(self) -> int:
        return self.gcp.data_disk_gb if self.gcp else 200

    @property
    def gpu_type(self) -> str | None:
        return self.gcp.gpu_type if self.gcp else None

    @property
    def gpu_count(self) -> int:
        return self.gcp.gpu_count if self.gcp else 0

    @property
    def max_concurrent_vms(self) -> int:
        return self.gcp.max_concurrent_vms if self.gcp else 4


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
    snapshot_save_timeout_s: int = 300
    snapshot_revert_timeout_s: int = 300
    cua_ready_after_revert_timeout_s: int = 30


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
        snapshot_save_timeout_s=int(raw.get("snapshot_save_timeout_s", 300)),
        snapshot_revert_timeout_s=int(raw.get("snapshot_revert_timeout_s", 300)),
        cua_ready_after_revert_timeout_s=int(raw.get("cua_ready_after_revert_timeout_s", 30)),
    )


def load_image_catalog(path: str | Path) -> dict[str, ImageSpec]:
    raw = _load_yaml(path)
    images = raw.get("images", {})
    catalog: dict[str, ImageSpec] = {}
    for key, spec in images.items():
        local_cfg = None
        gcp_cfg = None

        # New nested format: local: {...}, gcp: {...}
        if "local" in spec:
            local_raw = spec["local"]
            local_cfg = LocalImageConfig(
                template_qcow2_path=Path(local_raw.get("template_qcow2_path") or local_raw.get("golden_qcow2_path", "")),
                gcs_uri=local_raw.get("gcs_uri"),
                default_cpu_cores=int(local_raw.get("default_cpu_cores", 4)),
                default_memory_gb=int(local_raw.get("default_memory_gb", 8)),
            )
        if "gcp" in spec:
            gcp_raw = spec["gcp"]
            gcp_cfg = GCPImageConfig(
                project=gcp_raw["project"],
                zone=gcp_raw["zone"],
                network=gcp_raw["network"],
                service_account=gcp_raw["service_account"],
                boot_image=gcp_raw.get("boot_image"),
                boot_snapshot=gcp_raw.get("boot_snapshot"),
                boot_disk_gb=int(gcp_raw.get("boot_disk_gb", 64)),
                data_snapshot=gcp_raw.get("data_snapshot"),
                data_disk_gb=int(gcp_raw.get("data_disk_gb", 200)),
                gpu_type=gcp_raw.get("gpu_type"),
                gpu_count=int(gcp_raw.get("gpu_count", 0)),
                default_machine_type=gcp_raw.get("default_machine_type", "e2-standard-2"),
                max_concurrent_vms=int(gcp_raw.get("max_concurrent_vms", 4)),
            )

        # Legacy flat format: runtime_mode + top-level fields
        if local_cfg is None and gcp_cfg is None:
            runtime_mode = spec.get("runtime_mode", "local")
            if runtime_mode == "local":
                local_cfg = LocalImageConfig(
                    template_qcow2_path=Path(spec.get("template_qcow2_path") or spec.get("golden_qcow2_path", "")),
                    gcs_uri=spec.get("gcs_uri"),
                    default_cpu_cores=int(spec.get("default_cpu_cores", 4)),
                    default_memory_gb=int(spec.get("default_memory_gb", 8)),
                )
            elif runtime_mode == "gcp":
                gcp_cfg = GCPImageConfig(
                    project=spec.get("gcp_project", ""),
                    zone=spec.get("gcp_zone", ""),
                    network=spec.get("gcp_network", ""),
                    service_account=spec.get("gcp_service_account", ""),
                    boot_image=spec.get("gcp_boot_image"),
                    boot_snapshot=spec.get("gcp_boot_snapshot"),
                    boot_disk_gb=int(spec.get("gcp_boot_disk_gb", 64)),
                    data_snapshot=spec.get("gcp_data_snapshot"),
                    data_disk_gb=int(spec.get("gcp_data_disk_gb", 200)),
                    gpu_type=spec.get("gpu_type"),
                    gpu_count=int(spec.get("gpu_count", 0)),
                    default_machine_type=spec.get("gcp_machine_type", "e2-standard-2"),
                    max_concurrent_vms=int(spec.get("max_concurrent_vms", 4)),
                )

        catalog[key] = ImageSpec(
            key=key,
            enabled=bool(spec.get("enabled", False)),
            local=local_cfg,
            gcp=gcp_cfg,
        )
    return catalog


def resolve_gce_external_ip() -> str:
    explicit = os.environ.get("CUA_HOUSE_SERVER_EXTERNAL_IP")
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

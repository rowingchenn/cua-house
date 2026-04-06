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
class ImageSpec:
    key: str
    enabled: bool
    default_cpu_cores: int
    default_memory_gb: int
    runtime_mode: str = "local"  # "local" | "gcp"
    # local mode
    template_qcow2_path: Path | None = None
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
            image.template_qcow2_path = Path(spec["template_qcow2_path"])
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

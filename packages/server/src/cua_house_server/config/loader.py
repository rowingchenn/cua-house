"""Configuration loading for cua-house-server."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)


# Ports reserved by the dockur container infrastructure (VNC, monitor, web,
# websockets). The dockur bridge-mode iptables DNAT rule excludes these from
# guest forwarding (see network.sh `getHostPorts`), so they cannot be used as
# `published_ports` — guest traffic on these ports would never reach the VM.
RESERVED_DOCKUR_PORTS: frozenset[int] = frozenset({5900, 5700, 7100, 8004, 8006})

VALID_OS_FAMILIES: frozenset[str] = frozenset({"windows", "linux"})


@dataclass(slots=True)
class LocalImageConfig:
    """Local QEMU runtime configuration for an image."""

    template_qcow2_path: Path
    gcs_uri: str | None = None
    version: str = "unversioned"
    default_vcpus: int = 4
    default_memory_gb: int = 8
    default_disk_gb: int = 64


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


@dataclass(slots=True)
class ImageSpec:
    """Image specification supporting dual local/gcp modes.

    At least one of ``local`` or ``gcp`` must be set.  The scheduler
    picks the runtime based on which sub-configs are present and which
    runtimes the server has available.

    `os_family` and `published_ports` are required image-static facts. They
    live at the top level (not under `local:`/`gcp:`) because they describe
    the guest VM, not the runtime that hosts it.
    """

    key: str
    enabled: bool
    os_family: str
    published_ports: tuple[int, ...]
    local: LocalImageConfig | None = None
    gcp: GCPImageConfig | None = None

    # --- Convenience helpers ---

    @property
    def runtime_mode(self) -> str:
        """Primary runtime mode (local preferred if both present)."""
        if self.local is not None:
            return "local"
        return "gcp"

    @property
    def default_vcpus(self) -> int:
        if self.local:
            return self.local.default_vcpus
        return 4

    @property
    def default_memory_gb(self) -> int:
        if self.local:
            return self.local.default_memory_gb
        return 16

    @property
    def default_disk_gb(self) -> int:
        if self.local:
            return self.local.default_disk_gb
        return self.gcp.boot_disk_gb if self.gcp else 64

    @property
    def version(self) -> str:
        """Image content version string. Bumps invalidate the snapshot cache.

        Operators set this in the catalog alongside the qcow2 path — typical
        convention is the bake date (e.g. ``"20260414"``). ``"unversioned"``
        means the cache never evicts on content bumps; use for dev only.
        """
        if self.local:
            return self.local.version
        return "unversioned"

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


@dataclass(slots=True)
class HostRuntimeConfig:
    host_id: str
    host_external_ip: str
    public_base_host: str
    runtime_root: Path
    task_data_root: Path | None
    docker_image: str
    host_reserved_vcpus: int
    host_reserved_memory_gb: int
    batch_heartbeat_ttl_s: int
    heartbeat_ttl_s: int
    ready_timeout_s: int
    readiness_poll_interval_s: float
    # Single host-loopback port pool used for ALL guest ports an image declares
    # in `published_ports`. Each VM consumes len(published_ports) ports from
    # this range. Pick a range large enough for max_vms × max_ports_per_image.
    published_port_range: tuple[int, int]
    # Separate pool for the noVNC container service (one per VM, always 8006
    # inside the container).
    novnc_port_range: tuple[int, int]
    # Cluster mode. "standalone" preserves single-node behavior; "master" runs
    # a control plane that coordinates workers; "worker" dials into a master.
    mode: str = "standalone"
    cluster: "ClusterConfig | None" = None
    # Host IP the docker `-p` flag binds published_ports + novnc to. Defaults
    # to 127.0.0.1 (standalone mode: clients hit master's reverse proxy on
    # the same host and the proxy talks to loopback). In worker mode set to
    # 0.0.0.0 so clients in the VPC can reach VM services directly on the
    # worker's public IP.
    vm_bind_address: str = "127.0.0.1"
    snapshot_cache_dir: Path = field(default_factory=lambda: Path("/mnt/xfs/snapshot-cache"))


@dataclass(slots=True)
class ClusterConfig:
    """Cluster topology settings (only read when mode != 'standalone').

    - master role: ``master_bind_path`` is the URL path used by the FastAPI
      WebSocket endpoint (defaults to ``/v1/cluster/ws``). Workers connect via
      any routable transport (TCP/HTTP(S)) to this app.
    - worker role: ``master_url`` is the ws(s):// URL of the master's cluster
      endpoint; ``worker_id`` uniquely identifies this node.
    - Both roles read ``join_token`` from the ``CUA_HOUSE_CLUSTER_JOIN_TOKEN``
      env var by default; the config value is an override for tests.
    """

    master_bind_path: str = "/v1/cluster/ws"
    master_url: str | None = None
    worker_id: str | None = None
    join_token: str | None = None
    # Public address this worker advertises in TaskBound URLs. Defaults to
    # the host's external IP, but operators can override to use a VPC
    # internal IP or a DNS hostname — whichever is the right side of the
    # client/worker network boundary. Plain "host:port" or a full base URL.
    worker_public_host: str | None = None
    # Public HTTP port workers listen on for lease API. Must match the
    # uvicorn --port on this node. Defaults to 8787.
    worker_public_port: int = 8787
    heartbeat_interval_s: float = 5.0
    heartbeat_ttl_s: float = 30.0
    reconnect_min_backoff_s: float = 1.0
    reconnect_max_backoff_s: float = 30.0


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
        host_reserved_vcpus=int(raw.get("host_reserved_vcpus", 2)),
        host_reserved_memory_gb=int(raw.get("host_reserved_memory_gb", 8)),
        batch_heartbeat_ttl_s=int(raw.get("batch_heartbeat_ttl_s", 120)),
        heartbeat_ttl_s=int(raw.get("heartbeat_ttl_s", 60)),
        ready_timeout_s=int(raw.get("ready_timeout_s", 900)),
        readiness_poll_interval_s=float(raw.get("readiness_poll_interval_s", 5)),
        published_port_range=tuple(raw.get("published_port_range", [16000, 16999])),
        novnc_port_range=tuple(raw.get("novnc_port_range", [18000, 18999])),
        mode=str(raw.get("mode", "standalone")),
        cluster=_load_cluster_config(raw.get("cluster")),
        vm_bind_address=str(raw.get("vm_bind_address", "127.0.0.1")),
        snapshot_cache_dir=_resolve_snapshot_cache_dir(raw),
    )


def _resolve_snapshot_cache_dir(raw: dict) -> Path:
    """Master doesn't need a cache (never provisions); worker/standalone require one.

    Cache persistence is load-bearing for workers — templates re-pulled
    from GCS on every restart defeats the point. Master mode runs no
    local runtime, so we accept a stub path.
    """
    value = raw.get("snapshot_cache_dir")
    if value:
        return Path(value)
    mode = str(raw.get("mode", "standalone"))
    if mode == "master":
        # Never used; keeps the dataclass field non-optional.
        return Path("/var/empty/cua-house-master-no-cache")
    raise ValueError(
        "host config missing required field 'snapshot_cache_dir'. "
        "See docs/operations/vm-image-maintenance.md for the recommended "
        "path (/mnt/xfs/snapshot-cache on XFS-backed volumes)."
    )


def _load_cluster_config(raw: dict | None) -> ClusterConfig | None:
    if raw is None:
        return None
    return ClusterConfig(
        master_bind_path=str(raw.get("master_bind_path", "/v1/cluster/ws")),
        master_url=raw.get("master_url"),
        worker_id=raw.get("worker_id"),
        join_token=raw.get("join_token") or os.environ.get("CUA_HOUSE_CLUSTER_JOIN_TOKEN"),
        worker_public_host=raw.get("worker_public_host"),
        worker_public_port=int(raw.get("worker_public_port", 8787)),
        heartbeat_interval_s=float(raw.get("heartbeat_interval_s", 5.0)),
        heartbeat_ttl_s=float(raw.get("heartbeat_ttl_s", 30.0)),
        reconnect_min_backoff_s=float(raw.get("reconnect_min_backoff_s", 1.0)),
        reconnect_max_backoff_s=float(raw.get("reconnect_max_backoff_s", 30.0)),
    )


def load_image_catalog(path: str | Path) -> dict[str, ImageSpec]:
    raw = _load_yaml(path)
    images = raw.get("images", {})
    catalog: dict[str, ImageSpec] = {}
    for key, spec in images.items():
        enabled = bool(spec.get("enabled", False))

        # os_family + published_ports — required for all images
        os_family = spec.get("os_family", "")
        if enabled and os_family not in VALID_OS_FAMILIES:
            raise ValueError(
                f"image '{key}': os_family must be one of {sorted(VALID_OS_FAMILIES)}, got {os_family!r}"
            )
        os_family = os_family or "windows"  # harmless default for disabled images

        raw_ports = spec.get("published_ports", [])
        if enabled and not raw_ports:
            raise ValueError(f"image '{key}': published_ports is required (non-empty list[int])")
        published_ports = tuple(int(p) for p in raw_ports) if raw_ports else (5000,)
        if enabled:
            for port in published_ports:
                if not (1 <= port <= 65535):
                    raise ValueError(f"image '{key}': invalid port {port}")
                if port in RESERVED_DOCKUR_PORTS:
                    raise ValueError(
                        f"image '{key}': port {port} is reserved by dockur (HOST_PORTS). "
                        f"Reserved set: {sorted(RESERVED_DOCKUR_PORTS)}"
                    )
            if len(published_ports) != len(set(published_ports)):
                raise ValueError(f"image '{key}': duplicate entries in published_ports")

        local_cfg = None
        gcp_cfg = None

        if "local" in spec:
            local_raw = spec["local"]
            local_cfg = LocalImageConfig(
                template_qcow2_path=Path(local_raw.get("template_qcow2_path") or ""),
                gcs_uri=local_raw.get("gcs_uri"),
                version=str(local_raw.get("version", "unversioned")),
                default_vcpus=int(local_raw.get("default_vcpus", 4)),
                default_memory_gb=int(local_raw.get("default_memory_gb", 8)),
                default_disk_gb=int(local_raw.get("default_disk_gb", 64)),
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
            )

        catalog[key] = ImageSpec(
            key=key,
            enabled=enabled,
            os_family=os_family,
            published_ports=published_ports,
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

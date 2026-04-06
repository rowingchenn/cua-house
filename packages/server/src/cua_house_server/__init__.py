"""cua-house-server: computer-use VM sandbox orchestration server."""

from __future__ import annotations

__version__ = "0.1.0"

# Public API surface -- importable as ``from cua_house_server import ...``
from cua_house_server.api.app import create_app
from cua_house_server.config.loader import (
    HostRuntimeConfig,
    ImageSpec,
    load_host_runtime_config,
    load_image_catalog,
)
from cua_house_server.scheduler.core import EnvScheduler
from cua_house_server.runtimes.qemu import DockerQemuRuntime, VMHandle
from cua_house_server.runtimes.gcp import GCPVMRuntime
from cua_house_server.qmp.client import QMPClient

__all__ = [
    "create_app",
    "DockerQemuRuntime",
    "EnvScheduler",
    "GCPVMRuntime",
    "HostRuntimeConfig",
    "ImageSpec",
    "load_host_runtime_config",
    "load_image_catalog",
    "QMPClient",
    "VMHandle",
]

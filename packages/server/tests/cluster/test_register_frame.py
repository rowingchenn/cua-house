"""WorkerClusterClient.build_register_frame coverage.

The dry-run ``--print-register-frame`` CLI flag and the live connect
path both call this classmethod. Test it in isolation (no WS, no
scheduler) so misconfig surfaces fast.
"""

from __future__ import annotations

from pathlib import Path

from cua_house_server.cluster.protocol import Register
from cua_house_server.cluster.worker_client import WorkerClusterClient
from cua_house_server.config.loader import ClusterConfig, HostRuntimeConfig


def _host_config() -> HostRuntimeConfig:
    return HostRuntimeConfig(
        host_id="kvm-test-worker",
        host_external_ip="10.128.0.99",
        public_base_host="10.128.0.99",
        runtime_root=Path("/tmp/cua-house-test-runtime"),
        task_data_root=None,
        docker_image="",
        host_reserved_vcpus=2,
        host_reserved_memory_gb=4,
        batch_heartbeat_ttl_s=60,
        heartbeat_ttl_s=60,
        ready_timeout_s=60,
        readiness_poll_interval_s=1,
        published_port_range=(16000, 16010),
        novnc_port_range=(18000, 18010),
    )


def _cluster_config() -> ClusterConfig:
    return ClusterConfig(
        master_url="ws://master.test:8787/v1/cluster/ws",
        worker_id="kvm-test",
        join_token="dummy",
    )


def test_build_register_frame_returns_register_model() -> None:
    frame = WorkerClusterClient.build_register_frame(
        _host_config(), _cluster_config(),
    )
    assert isinstance(frame, Register)
    assert frame.kind == "register"
    assert frame.worker_id == "kvm-test"
    assert frame.runtime_version == "0.1.0"
    assert frame.hosted_images == []


def test_build_register_frame_capacity_fields_populated() -> None:
    frame = WorkerClusterClient.build_register_frame(
        _host_config(), _cluster_config(),
    )
    # psutil fills these from the machine running the test; we don't
    # assert exact numbers but they must be non-negative and the
    # reserved fields must match HostRuntimeConfig verbatim.
    assert frame.capacity.total_vcpus >= 1
    assert frame.capacity.total_memory_gb >= 1
    assert frame.capacity.total_disk_gb >= 1
    assert frame.capacity.reserved_vcpus == 2
    assert frame.capacity.reserved_memory_gb == 4


def test_build_register_frame_passes_through_hosted_images_sorted() -> None:
    frame = WorkerClusterClient.build_register_frame(
        _host_config(),
        _cluster_config(),
        hosted_images=["zzz", "aaa", "mmm"],
    )
    assert frame.hosted_images == ["aaa", "mmm", "zzz"]


def test_build_register_frame_empty_worker_id_becomes_empty_string() -> None:
    # ClusterConfig allows worker_id=None so __init__ raises. Here we
    # bypass the runtime validation to exercise the frame builder's own
    # fallback, which renders None as "" to satisfy Pydantic str typing.
    cluster = ClusterConfig(
        master_url="ws://master.test:8787/v1/cluster/ws",
        worker_id=None,
    )
    frame = WorkerClusterClient.build_register_frame(_host_config(), cluster)
    assert frame.worker_id == ""

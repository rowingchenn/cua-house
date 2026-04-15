"""Unit tests for the worker-local snapshot cache."""

import json

import pytest

from cua_house_server.runtimes.snapshot_cache import CacheKey, SnapshotCache


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "snapshot-cache"


@pytest.fixture
def cache(cache_dir):
    return SnapshotCache(cache_dir, qemu_fingerprint="fp-test-abc")


def _key(
    image_key="cpu-free",
    image_version="1",
    vcpus=4,
    memory_gb=8,
    disk_gb=64,
) -> CacheKey:
    return CacheKey(
        image_key=image_key,
        image_version=image_version,
        vcpus=vcpus,
        memory_gb=memory_gb,
        disk_gb=disk_gb,
    )


def test_lookup_miss_returns_none(cache):
    assert cache.lookup(_key()) is None


def test_write_then_lookup_hit(cache, tmp_path):
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"\x00" * 1024)
    key = _key()
    result = cache.write(key, source)
    assert result is not None
    assert result.exists()
    assert result.stat().st_size == 1024

    hit = cache.lookup(key)
    assert hit is not None
    assert hit == result


def test_lookup_miss_after_version_bump(cache, tmp_path):
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"\x00" * 512)
    key_v1 = _key(image_version="1")
    cache.write(key_v1, source)
    assert cache.lookup(key_v1) is not None

    key_v2 = _key(image_version="2")
    assert cache.lookup(key_v2) is None


def test_lookup_miss_after_shape_change(cache, tmp_path):
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"\x00" * 512)
    key_4c = _key(vcpus=4, memory_gb=8)
    cache.write(key_4c, source)
    assert cache.lookup(key_4c) is not None

    key_8c = _key(vcpus=8, memory_gb=16)
    assert cache.lookup(key_8c) is None


def test_fingerprint_mismatch_evicts_on_lookup(cache, cache_dir, tmp_path):
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"\x00" * 256)
    key = _key()
    cache.write(key, source)
    assert cache.lookup(key) is not None

    stale_cache = SnapshotCache(cache_dir, qemu_fingerprint="fp-different")
    assert stale_cache.lookup(key) is None

    assert cache.lookup(key) is None


def test_sweep_on_startup_evicts_stale_fingerprints(cache, cache_dir, tmp_path):
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"\x00" * 128)
    cache.write(_key(image_key="img-a"), source)
    cache.write(_key(image_key="img-b"), source)
    assert len(list(cache_dir.rglob("*.qcow2"))) == 2

    upgraded_cache = SnapshotCache(cache_dir, qemu_fingerprint="fp-new-binary")
    evicted = upgraded_cache.sweep_on_startup()
    assert len(evicted) == 2
    assert len(list(cache_dir.rglob("*.qcow2"))) == 0


def test_sweep_preserves_matching_fingerprints(cache, cache_dir, tmp_path):
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"\x00" * 128)
    cache.write(_key(), source)
    evicted = cache.sweep_on_startup()
    assert evicted == []
    assert cache.lookup(_key()) is not None


def test_purge_version(cache, cache_dir, tmp_path):
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"\x00" * 128)
    cache.write(_key(image_version="1", vcpus=4), source)
    cache.write(_key(image_version="1", vcpus=8), source)
    cache.write(_key(image_version="2"), source)

    removed = cache.purge_version("cpu-free", "1")
    assert removed == 2
    assert cache.lookup(_key(image_version="1", vcpus=4)) is None
    assert cache.lookup(_key(image_version="2")) is not None


def test_write_failure_returns_none(cache, tmp_path):
    source = tmp_path / "nonexistent.qcow2"
    result = cache.write(_key(), source)
    assert result is None


def test_cache_key_stem():
    k = _key(vcpus=8, memory_gb=16, disk_gb=128)
    assert k.stem == "8vcpu-16gb-128gb"


def test_cache_key_dir_path():
    k = _key(image_key="cpu-free", image_version="20260413")
    assert str(k.dir_path) == "cpu-free/v20260413"

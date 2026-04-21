"""Worker-local snapshot cache keyed by (image, version, shape).

GCS holds base images only. The first time a worker boots a VM for a
never-seen shape, it cold-boots (~4-5 min) and QMP savevm's into the
slot qcow2. That qcow2 is then reflinked into the cache directory so
the next add_vm for the same shape can loadvm in seconds.

Cache directory layout::

    <cache_dir>/
      cpu-free/
        v20260413/
          4vcpu-8gb-64gb.qcow2      # full savevm state
          4vcpu-8gb-64gb.json       # metadata sidecar
          8vcpu-16gb-64gb.qcow2
        v20260501/                  # new image version
          ...

The qemu_fingerprint (sha256 of qemu version + docker image id) is
stored in each sidecar; on startup, entries whose fingerprint doesn't
match the running binary are evicted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def shape_stem(vcpus: int, memory_gb: int, disk_gb: int) -> str:
    """Return the canonical shape string used as QEMU snapshot tag and cache key."""
    return f"{vcpus}vcpu-{memory_gb}gb-{disk_gb}gb"


@dataclass(frozen=True)
class CacheKey:
    image_key: str
    image_version: str
    vcpus: int
    memory_gb: int
    disk_gb: int

    @property
    def dir_path(self) -> Path:
        return Path(self.image_key) / f"v{self.image_version}"

    @property
    def stem(self) -> str:
        return shape_stem(self.vcpus, self.memory_gb, self.disk_gb)


class SnapshotCache:
    """Filesystem-backed snapshot cache."""

    def __init__(self, cache_dir: Path, qemu_fingerprint: str) -> None:
        self.cache_dir = cache_dir
        self.qemu_fingerprint = qemu_fingerprint
        cache_dir.mkdir(parents=True, exist_ok=True)

    def lookup(self, key: CacheKey) -> Path | None:
        qcow2 = self.cache_dir / key.dir_path / f"{key.stem}.qcow2"
        sidecar = qcow2.with_suffix(".json")
        if not qcow2.exists():
            return None
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text())
                if meta.get("qemu_fingerprint") != self.qemu_fingerprint:
                    logger.info(
                        "cache fingerprint mismatch for %s (have %s, want %s); evicting",
                        qcow2, meta.get("qemu_fingerprint"), self.qemu_fingerprint,
                    )
                    qcow2.unlink(missing_ok=True)
                    sidecar.unlink(missing_ok=True)
                    return None
            except Exception:
                pass
        return qcow2

    def write(
        self,
        key: CacheKey,
        source_qcow2: Path,
    ) -> Path | None:
        dest_dir = self.cache_dir / key.dir_path
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{key.stem}.qcow2"
        sidecar = dest.with_suffix(".json")
        try:
            if dest.exists():
                dest.unlink()
            try:
                subprocess.run(
                    ["cp", "--reflink=auto", str(source_qcow2), str(dest)],
                    check=True, capture_output=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                import shutil
                shutil.copy2(str(source_qcow2), str(dest))
            meta = {
                "qemu_fingerprint": self.qemu_fingerprint,
                "created_at": time.time(),
                "size_bytes": dest.stat().st_size,
                "shape": {
                    "image_key": key.image_key,
                    "image_version": key.image_version,
                    "vcpus": key.vcpus,
                    "memory_gb": key.memory_gb,
                    "disk_gb": key.disk_gb,
                },
            }
            sidecar.write_text(json.dumps(meta, indent=2))
            logger.info("snapshot cache write: %s (%.1f GB)", dest, dest.stat().st_size / 1e9)
            return dest
        except Exception:
            logger.warning("snapshot cache write failed for %s", dest, exc_info=True)
            return None

    def sweep_on_startup(self) -> list[str]:
        evicted: list[str] = []
        if not self.cache_dir.exists():
            return evicted
        for sidecar in self.cache_dir.rglob("*.json"):
            try:
                meta = json.loads(sidecar.read_text())
                if meta.get("qemu_fingerprint") != self.qemu_fingerprint:
                    qcow2 = sidecar.with_suffix(".qcow2")
                    qcow2.unlink(missing_ok=True)
                    sidecar.unlink(missing_ok=True)
                    evicted.append(str(qcow2))
            except Exception:
                pass
        if evicted:
            logger.info("snapshot cache startup sweep evicted %d entries", len(evicted))
        return evicted

    def list_entries(self) -> list[CacheKey]:
        """Enumerate every valid entry in the cache.

        Returned entries' qemu_fingerprint matches the running binary; stale
        entries are ignored (but not evicted — that's what sweep_on_startup
        is for). Intended for workers to report cached_shapes back to master.
        """
        entries: list[CacheKey] = []
        if not self.cache_dir.exists():
            return entries
        for sidecar in self.cache_dir.rglob("*.json"):
            try:
                meta = json.loads(sidecar.read_text())
            except Exception:
                continue
            if meta.get("qemu_fingerprint") != self.qemu_fingerprint:
                continue
            shape = meta.get("shape") or {}
            try:
                entries.append(CacheKey(
                    image_key=str(shape["image_key"]),
                    image_version=str(shape["image_version"]),
                    vcpus=int(shape["vcpus"]),
                    memory_gb=int(shape["memory_gb"]),
                    disk_gb=int(shape["disk_gb"]),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return entries

    def purge_version(self, image_key: str, image_version: str) -> int:
        target = self.cache_dir / image_key / f"v{image_version}"
        if not target.exists():
            return 0
        import shutil
        count = sum(1 for _ in target.glob("*.qcow2"))
        shutil.rmtree(target, ignore_errors=True)
        logger.info("purged cache version %s/%s (%d entries)", image_key, image_version, count)
        return count


def compute_qemu_fingerprint(docker_image: str) -> str:
    parts: list[str] = []
    try:
        result = subprocess.run(
            ["qemu-system-x86_64", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        parts.append(result.stdout.strip())
    except Exception:
        parts.append("qemu-unknown")
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", docker_image, "-f", "{{.Id}}"],
            capture_output=True, text=True, timeout=10,
        )
        parts.append(result.stdout.strip())
    except Exception:
        parts.append("docker-unknown")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

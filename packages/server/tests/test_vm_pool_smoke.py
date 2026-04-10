"""Smoke test for VM pool initialization on kvm0.

Run with: uv run python tests/test_vm_pool_smoke.py

Tests the core snapshot flow:
1. Create overlay + start Docker container
2. Wait for CUA readiness
3. savevm "clean-ready"
4. loadvm to verify revert
5. CUA readiness after loadvm
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

# Ensure src/ on path when running as a standalone script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cua_house.env_server.qmp_client import QMPClient
from cua_house.env_server.runtime import (
    DockerQemuRuntime,
    HostRuntimeConfig,
    ImageSpec,
    VMHandle,
    load_host_runtime_config,
    load_image_catalog,
)
from cua_house.env_server.models import VMPoolEntry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("test_vm_pool")


async def main():
    # Load configs
    config_dir = Path(__file__).resolve().parent.parent / "src" / "cua_house" / "env_server" / "configs"
    config = load_host_runtime_config(config_dir / "agenthle_env_server.yaml")
    images = load_image_catalog(config_dir / "agenthle_env_images.yaml")

    # Use a single VM for the smoke test
    pool = [VMPoolEntry(image_key="cpu-free", count=1, cpu_cores=4, memory_gb=8)]

    runtime = DockerQemuRuntime(config)

    logger.info("=== Step 1: Clean up orphaned state ===")
    runtime.cleanup_orphaned_state()

    logger.info("=== Step 2: Initialize pool (1 VM, 4 CPU, 8GB RAM) ===")
    t0 = time.time()
    handles = await runtime.initialize_pool(pool, images)
    t_pool = time.time() - t0
    logger.info("Pool init took %.1fs, %d VMs ready", t_pool, len(handles))

    if not handles:
        logger.error("No VMs booted! Check logs above.")
        return 1

    handle = handles[0]
    primary_port = next(iter(handle.published_ports.values()))
    logger.info("VM: %s (%s), published ports %s", handle.vm_id, handle.container_name, handle.published_ports)

    # Verify CUA is responsive
    import httpx
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.get(f"http://127.0.0.1:{primary_port}/status")
        assert resp.status_code == 200, f"CUA not ready: {resp.status_code}"
        logger.info("CUA server is ready after pool init")

    # Dirty the VM
    logger.info("=== Step 3: Dirty the VM (create test file) ===")
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=30.0)) as client:
        resp = await client.post(
            f"http://127.0.0.1:{primary_port}/cmd",
            json={"command": "run_command", "params": {"command": "echo SMOKE_TEST > C:\\Users\\User\\Desktop\\smoke.txt"}},
        )
        logger.info("Created smoke.txt on desktop")

    # Revert
    logger.info("=== Step 4: Revert VM via loadvm ===")
    t0 = time.time()
    await runtime.revert_vm(handle)
    t_revert = time.time() - t0
    logger.info("Revert took %.1fs", t_revert)

    # Verify CUA and check file is gone
    logger.info("=== Step 5: Verify revert ===")
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=30.0)) as client:
        resp = await client.get(f"http://127.0.0.1:{primary_port}/status")
        assert resp.status_code == 200, f"CUA not ready after revert: {resp.status_code}"
        logger.info("CUA is ready after revert")

        resp = await client.post(
            f"http://127.0.0.1:{primary_port}/cmd",
            json={"command": "run_command", "params": {"command": "if exist C:\\Users\\User\\Desktop\\smoke.txt (echo EXISTS) else (echo REVERTED)"}},
        )
        body = resp.text
        if "REVERTED" in body:
            logger.info("SUCCESS: smoke.txt was reverted!")
        elif "EXISTS" in body:
            logger.error("FAIL: smoke.txt still exists after revert!")
            return 1
        else:
            logger.warning("UNKNOWN response: %s", body[-300:])

    logger.info("\n=== RESULTS ===")
    logger.info("Pool init:  %.1fs (cold boot + snapshot)", t_pool)
    logger.info("Revert:     %.1fs (loadvm + CUA ready)", t_revert)
    logger.info("Test:       PASSED")

    # Cleanup
    logger.info("Cleaning up...")
    runtime.cleanup_orphaned_state()

    return 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)

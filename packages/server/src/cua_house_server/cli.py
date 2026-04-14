"""CLI entrypoints for cua-house-server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import uvicorn

from cua_house_server.api.app import create_app


def _default_config_dir() -> Path:
    return Path(__file__).resolve().parent / "config" / "defaults"


def parse_args() -> argparse.Namespace:
    config_dir = _default_config_dir()
    parser = argparse.ArgumentParser(description="Run the cua-house-server")
    parser.add_argument(
        "--host-config",
        default=str(config_dir / "server.yaml"),
        help="Path to host runtime config YAML",
    )
    parser.add_argument(
        "--image-catalog",
        default=str(config_dir / "images.yaml"),
        help="Path to image catalog YAML",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--mode",
        choices=["standalone", "master", "worker"],
        default=None,
        help="Cluster role (overrides config). standalone is the default single-node mode.",
    )
    parser.add_argument(
        "--master-url",
        default=None,
        help="ws(s):// URL of the master cluster endpoint (worker mode only).",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="Stable identifier for this worker node (worker mode only).",
    )
    parser.add_argument(
        "--print-register-frame",
        action="store_true",
        help=(
            "Worker-mode dry run: load configs, validate cluster fields, build "
            "the Register frame this worker would send to master, print it as "
            "JSON to stdout, and exit 0 without starting uvicorn or connecting. "
            "Used by scripts/clone-worker.sh to catch config typos at "
            "provisioning time."
        ),
    )
    return parser.parse_args()


def _print_register_frame(args: argparse.Namespace) -> int:
    """Dry-run path: validate worker config and print the Register frame.

    Avoids calling create_app (which would start scheduler / mount HTTP
    routes). Loads only what's needed to build the frame: HostRuntimeConfig,
    image catalog (so the worker-mode fail-fast checks fire), and
    ClusterConfig. Any ValueError / RuntimeError bubbles up and exits
    non-zero so the caller can fail fast on misconfig.
    """
    from cua_house_server.cluster.worker_client import WorkerClusterClient
    from cua_house_server.config.loader import (
        ClusterConfig,
        load_host_runtime_config,
        load_image_catalog,
    )

    host_config = load_host_runtime_config(args.host_config)
    _ = load_image_catalog(args.image_catalog)  # parse errors surface here

    if args.mode is not None:
        host_config.mode = args.mode
    if host_config.mode != "worker":
        print(
            "--print-register-frame requires --mode worker (or mode: worker in config)",
            file=sys.stderr,
        )
        return 2
    if host_config.cluster is None:
        host_config.cluster = ClusterConfig()
    if args.master_url is not None:
        host_config.cluster.master_url = args.master_url
    if args.worker_id is not None:
        host_config.cluster.worker_id = args.worker_id
    if host_config.cluster.master_url is None:
        print("cluster.master_url is required in worker mode", file=sys.stderr)
        return 2
    if host_config.cluster.worker_id is None:
        print("cluster.worker_id is required in worker mode", file=sys.stderr)
        return 2

    frame = WorkerClusterClient.build_register_frame(
        host_config, host_config.cluster, hosted_images=[],
    )
    payload = {
        "register": frame.model_dump(),
        "config": {
            "mode": host_config.mode,
            "master_url": host_config.cluster.master_url,
            "worker_id": host_config.cluster.worker_id,
            "worker_public_host": (
                host_config.cluster.worker_public_host
                or host_config.host_external_ip
            ),
            "worker_public_port": host_config.cluster.worker_public_port,
            "task_data_root": (
                str(host_config.task_data_root)
                if host_config.task_data_root is not None
                else None
            ),
            "runtime_root": str(host_config.runtime_root),
            "vm_bind_address": host_config.vm_bind_address,
        },
    }
    print(json.dumps(payload, indent=2))
    return 0


def main() -> None:
    args = parse_args()
    if args.print_register_frame:
        sys.exit(_print_register_frame(args))
    app = create_app(
        host_config_path=args.host_config,
        image_catalog_path=args.image_catalog,
        mode_override=args.mode,
        master_url_override=args.master_url,
        worker_id_override=args.worker_id,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

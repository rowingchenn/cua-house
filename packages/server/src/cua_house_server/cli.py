"""CLI entrypoints for cua-house-server."""

from __future__ import annotations

import argparse
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = create_app(host_config_path=args.host_config, image_catalog_path=args.image_catalog)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

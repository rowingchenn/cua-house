# cua-house

Computer-use VM sandbox orchestrator.

cua-house manages Docker+QEMU Windows VMs and GCP VMs for agent evaluation and training. It provides lease-based allocation, reverse proxying, snapshot-based fast revert, and task data staging with NTFS ACL isolation.

## Architecture

The project is a uv monorepo with three packages:

| Package | Path | Description |
|---------|------|-------------|
| **cua-house-common** | `packages/common/` | Shared Pydantic models, state enums, event logger |
| **cua-house-client** | `packages/client/` | Async HTTP client SDK for interacting with the server |
| **cua-house-server** | `packages/server/` | FastAPI orchestration server (scheduler, runtimes, proxy, admin) |

Supporting directories:

- `bridges/` -- non-Python bridge assets for agent integration (MCP server, OpenClaw plugin)
- `examples/` -- example configs and client usage
- `docs/` -- architecture, deployment, and development documentation

## Quick start

```bash
cd cua-house
uv sync
uv run cua-house-server
```

The server listens on port 8787 by default. Pass `--host-config` and `--image-catalog` to override the default config files.

```bash
uv run cua-house-server --host-config /path/to/server.yaml --image-catalog /path/to/images.yaml
```

## Running tests

```bash
uv run pytest
```

## Documentation

See `docs/` for detailed documentation:

- [Architecture overview](docs/architecture/overview.md)
- [Runtime model](docs/architecture/runtime-model.md)
- [Host setup](docs/deployment/host-setup.md)
- [GCP infrastructure](docs/deployment/gcp-infra.md)
- [Development setup](docs/development/setup.md)
- [Testing](docs/development/testing.md)

## License

MIT

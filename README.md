# cua-house

Computer-use VM sandbox orchestrator.

cua-house manages Docker+QEMU Windows VMs and GCP VMs for agent evaluation and training. It provides lease-based allocation, reverse proxying, snapshot-based fast revert, and task data staging with NTFS ACL isolation.

It can run as a **single-node standalone server** or as a **multi-node cluster** with a master control plane orchestrating a fleet of worker nodes. See [Cluster deployment](docs/deployment/cluster.md) for the cluster model and [Cluster architecture](docs/architecture/cluster.md) for the design.

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

### Cluster mode

The same binary runs as `master`, `worker`, or the default `standalone` via `--mode`. A minimal cluster is one master VM plus one or more worker VMs that pull VM templates from GCS on demand.

```bash
# master: coordinates workers, accepts batches, stays out of the task data path
uv run cua-house-server --mode master --host-config master.yaml --image-catalog images.yaml

# worker: joins the master over WebSocket and hosts dynamic VM pool
CUA_HOUSE_CLUSTER_JOIN_TOKEN=... \
  uv run cua-house-server --mode worker --host-config worker.yaml --image-catalog images.yaml
```

See [docs/deployment/cluster.md](docs/deployment/cluster.md) for the full recipe (VPC, firewall, task-data OverlayFS, smoke test).

## Running tests

```bash
uv run pytest
```

## Documentation

See `docs/` for detailed documentation:

- [Architecture overview](docs/architecture/overview.md)
- [Runtime model](docs/architecture/runtime-model.md)
- [Cluster architecture](docs/architecture/cluster.md)
- [Host setup](docs/deployment/host-setup.md)
- [Cluster deployment](docs/deployment/cluster.md)
- [GCP infrastructure](docs/deployment/gcp-infra.md)
- [Development setup](docs/development/setup.md)
- [Testing](docs/development/testing.md)

## License

MIT

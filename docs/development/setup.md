# Development setup

## Clone and install

```bash
git clone <repo-url>
cd cua-house
uv sync
```

`uv sync` installs all three workspace packages and dev dependencies into a single virtual environment.

## Repository structure

```
cua-house/
  pyproject.toml             # workspace root (uv monorepo)
  uv.lock                    # lockfile
  packages/
    common/                  # cua-house-common: shared models, enums
      src/cua_house_common/
        models.py            # Pydantic API models, state enums
        events.py            # JSONL event logger
    client/                  # cua-house-client: async httpx SDK
      src/cua_house_client/
        client.py            # EnvServerClient
    server/                  # cua-house-server: orchestration server
      src/cua_house_server/
        cli.py               # CLI entrypoint (argparse + uvicorn)
        api/                 # FastAPI app, routes, auth, reverse proxy
        scheduler/           # state machine, dispatch loop
        runtimes/            # RuntimeBackend protocol, DockerQemuRuntime, GCPVMRuntime
        qmp/                 # QMP client for QEMU snapshots
        data/                # task data staging
        config/              # YAML loader, defaults/
        admin/               # image bake workflow
        _internal/           # port pool
      tests/
        cluster/             # protocol, registry, register-frame unit tests
  bridges/                   # non-Python bridge assets
    cua_mcp_server/          # Node.js MCP server for Claude/Codex
    openclaw_plugin/         # OpenClaw CUA plugin
  docs/                      # documentation
  examples/                  # example configs and client code
```

## Running the server locally

```bash
uv run cua-house-server
```

This uses the default configs in `packages/server/src/cua_house_server/config/defaults/`. To use custom configs:

```bash
uv run cua-house-server \
    --host-config /path/to/server.yaml \
    --image-catalog /path/to/images.yaml
```

The server starts on `0.0.0.0:8787` by default. Override with `--host` and `--port`.

Note: without `/dev/kvm` and the golden QCOW2 image, the server will start but VMs will fail to boot. For development without KVM, the test suite uses `FakeRuntime` to exercise the scheduler logic.

## Running tests

```bash
uv run pytest                        # all tests
uv run pytest packages/server/       # server package only
uv run pytest -k test_scheduler      # by pattern
uv run pytest -v                     # verbose output
```

## Linting and formatting

```bash
uv run ruff check                    # lint
uv run ruff format --check           # check formatting
uv run ruff format                   # auto-format
```

## Environment variables

For local development, these are optional:

| Variable | Purpose | Default |
|----------|---------|---------|
| `CUA_HOUSE_TOKEN` | API bearer token | None (auth disabled) |
| `CUA_HOUSE_SERVER_URL` | Client SDK server URL | None |
| `CUA_HOUSE_SERVER_EXTERNAL_IP` | Override auto-detected IP | `127.0.0.1` fallback |
| `GCLOUD_PATH` | Path to gcloud binary | `gcloud` |

## IDE setup

The project uses ruff for linting and formatting. Configure your editor to use:

- Line length: 100
- Target version: Python 3.12
- Lint rules: E, F, B, I

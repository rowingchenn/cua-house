# cua-house

`cua-house` is the standalone extraction of the AgentHLE `env_server` stack.

It provides:

- a FastAPI control plane for batch / lease management
- local Docker + QEMU slot orchestration
- optional GCP-backed VM runtime support
- reverse proxying for leased CUA and noVNC endpoints
- an admin image-bake helper plus bundled bridge assets

## Layout

- `src/cua_house/env_server/`: core scheduler, runtimes, API, client, configs
- `src/cua_house/common/`: shared helpers kept with the extracted package
- `src/cua_house/remote/`: remote CUA admin helpers used by image bake
- `tests/`: extracted env-server tests

## Quick Start

```bash
cd cua-house
uv sync
uv run cua-house-server
```

Default config files live at:

- `src/cua_house/env_server/configs/agenthle_env_server.yaml`
- `src/cua_house/env_server/configs/agenthle_env_images.yaml`

Run tests with:

```bash
uv run pytest -q
```

The operational host playbooks still live in the main AgentHLE repository. This repo only carries the extracted runtime code and its immediate assets.

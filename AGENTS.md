# Agent development rules for cua-house

Rules for AI coding assistants working in this repository.

## First message protocol

1. Read `README.md` to understand the project.
2. Ask or determine which package/module the task involves.
3. Read the relevant package `README.md` (e.g., `packages/server/README.md`).
4. If working on the server, also read `packages/server/AGENTS.md`.

## Architecture map

```
cua-house/
  packages/
    common/      cua-house-common   shared models, enums, event logger
    client/      cua-house-client   async httpx SDK
    server/      cua-house-server   orchestration server
      api/         FastAPI app, routes, auth, reverse proxy
      scheduler/   task/batch state machine, dispatch loop, lease reaper
      runtimes/    DockerQemuRuntime, GCPVMRuntime, RuntimeBackend protocol
      qmp/         QEMU Machine Protocol client (docker exec + nc)
      data/        task data validation and guest staging (Samba, NTFS ACL)
      config/      YAML config loader, ImageSpec, HostRuntimeConfig
      admin/       image bake workflow
      _internal/   port pool and internal utilities
  bridges/         non-Python bridge assets (MCP server, OpenClaw plugin)
  docs/            architecture, deployment, development docs
  examples/        example configs and client usage
```

## Commands

```bash
uv sync                              # install all workspace packages
uv run pytest                        # run all tests
uv run pytest packages/server/       # run server tests only
uv run ruff check                    # lint
uv run ruff format --check           # check formatting
uv run cua-house-server              # start server on port 8787
```

## Code quality rules

- Python 3.12+ required.
- Every module must start with `from __future__ import annotations`.
- Avoid `Any` types where a concrete type or protocol can be used.
- Use `ruff` for formatting and linting. Line length is 100.
- Lint rules: `E`, `F`, `B`, `I` (pyflakes, pycodestyle, bugbear, isort).
- Use `StrEnum` for state enums, `pydantic.BaseModel` for API models.
- Use `dataclass(slots=True)` for internal data holders.

## Git rules

- Never use `git add -A` or `git add .`. Always stage specific files.
- Never commit without explicit user approval.
- Never commit before thorough testing is completed.
- Use imperative mood in commit messages (e.g., "add snapshot revert timeout config").
- Never delete files or directories without explicit user confirmation.

## Testing

- Unit suite (`packages/server/tests/`) covers protocol roundtrips, `WorkerRegistry` behavior, and `WorkerClusterClient.build_register_frame`. No Docker / KVM / network dependency; runs in CI.
- End-to-end validation against a live master + worker lives in [`docs/development/testing.md`](docs/development/testing.md) — admission check, cache-hit / cache-miss provisioning, capacity ledger, worker-disconnect requeue.
- Always run `uv run pytest` before proposing commits.

## Style

- Concise, technical prose. No emoji.
- Lowercase error messages (e.g., `raise ValueError("task data root missing")`).
- Log messages use `%s` formatting, not f-strings.
- Use `logger = logging.getLogger(__name__)` at module level.

## Adding a new runtime backend

1. Define a new class implementing the `RuntimeBackend` protocol in `packages/server/src/cua_house_server/runtimes/base.py`.
2. Implement all protocol methods: `cleanup_orphaned_state`, `prepare_slot`, `start_slot`, `reset_slot`, `cua_local_url`, `novnc_local_url`, `validate_runtime_task_data`, `stage_task_phase`.
3. Register the new runtime in `api/app.py` based on `ImageSpec.runtime_mode`.
4. Add corresponding image entries to the image catalog YAML.
5. Write unit tests with a fake/mock version of the backend.

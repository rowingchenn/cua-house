# Contributing to cua-house

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

## Setup

```bash
git clone <repo-url>
cd cua-house
uv sync
```

This installs all workspace packages (`cua-house-common`, `cua-house-client`, `cua-house-server`) and dev dependencies (pytest, ruff).

## Running the server locally

```bash
uv run cua-house-server
```

Default configs are in `packages/server/src/cua_house_server/config/defaults/`. Override with `--host-config` and `--image-catalog`.

## Running tests

```bash
uv run pytest                        # all tests
uv run pytest packages/server/       # server tests only
uv run pytest -k test_protocol       # specific test pattern
```

Full unit suite runs offline (no Docker / KVM / network). End-to-end
validation against a live master + worker is documented in
[`docs/development/testing.md`](docs/development/testing.md).

## Code style

This project uses `ruff` with line length 100 and target Python 3.12.

```bash
uv run ruff check                    # lint
uv run ruff format --check           # check formatting
uv run ruff format                   # auto-format
```

Enabled lint rules: `E` (pycodestyle), `F` (pyflakes), `B` (bugbear), `I` (isort).

## Adding a new package

1. Create a directory under `packages/`, e.g., `packages/my-pkg/`.
2. Add a `pyproject.toml` with hatchling build backend and `[project]` metadata.
3. Create `src/cua_house_my_pkg/__init__.py`.
4. The root `pyproject.toml` uses `members = ["packages/*"]`, so the new package is discovered automatically.
5. If other workspace packages depend on it, add a `[tool.uv.sources]` entry and a dependency reference.

## Adding a new runtime backend

1. Read the `RuntimeBackend` protocol in `packages/server/src/cua_house_server/runtimes/base.py`.
2. Create a new module in `runtimes/` implementing all protocol methods.
3. Register the runtime in `packages/server/src/cua_house_server/api/app.py`
   based on the relevant `ImageSpec` section (`local`, `gcp`, or a new
   section you add to the loader).
4. Add image entries in the image catalog YAML with the runtime-specific
   nested config.
5. Write unit tests using a mock/fake version of the backend.

## PR workflow

1. Branch off `main`.
2. Make changes. Run tests (`uv run pytest`) and linting (`uv run ruff check`).
3. Commit with imperative-mood messages (e.g., "add Android QEMU runtime backend").
4. Open a pull request. Describe what changed and why.
5. Address review feedback, rebase if needed, merge.

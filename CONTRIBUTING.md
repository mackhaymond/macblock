# Contributing

## Prerequisites

- macOS (this project configures system DNS and launchd)
- `uv` (Python tooling)
- `just` (task runner)

Optional but recommended:
- `direnv` (auto environment setup)

## Setup

```bash
git clone https://github.com/mackhaymond/macblock.git
cd macblock

# Optional: automatically sync deps + set env vars
brew install direnv

direnv allow

# Install deps
just sync
```

## Common commands

```bash
just            # list available tasks
just ci         # format-check, lint, pyright, tests
just lint-fix   # auto-fix lint issues + format
just test       # run pytest
just run status # run the CLI
```

## Lockfile policy

This repo commits `uv.lock`. CI uses `uv sync --locked` and will fail if `pyproject.toml` changes without an updated lockfile.

- If you change dependencies in `pyproject.toml`, run `uv lock` and commit the updated `uv.lock`.
- Then run `uv sync --dev` (or `just sync`) to refresh your local environment.

## Dev dependencies

Dev dependencies are declared twice in `pyproject.toml`:

- `[project.optional-dependencies].dev` for `pip install -e ".[dev]"`
- `[dependency-groups].dev` for `uv sync --dev`

Keep the two lists in sync.

## Commit messages

Use Conventional Commits:

- `feat: ...`
- `fix: ...`
- `docs: ...`
- `refactor: ...`
- `test: ...`
- `chore: ...`

## Release process (maintainers)

See `docs/RELEASING.md`.

# CLAUDE.md — miniwdl-spawn

`miniwdl-spawn` is a **miniwdl container backend** that runs WDL tasks on
ephemeral EC2 instances via [spore-host/spawn](https://github.com/spore-host/spawn).
The WDL analog of `nf-spawn`. Part of the spore.host suite (spore-host#395).

## Versioning & changelog (required)

Follows **[Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)** and keeps a
**[Keep a Changelog](https://keepachangelog.com/en/1.1.0/)**-format `CHANGELOG.md`
(spore.host-wide policy).

**Every user-facing change updates `CHANGELOG.md`** in the same PR under
`## [Unreleased]` (Added/Changed/Deprecated/Removed/Fixed/Security).

**On release:**
1. Rename `## [Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD`; open a fresh Unreleased; update links.
2. SemVer: MAJOR breaking / MINOR feature / PATCH fix (pre-1.0 breaking → MINOR).
3. **Bump `version` in `pyproject.toml` to match** — the release workflow fails if the tag
   and `pyproject.toml` version drift.
4. Tag `vX.Y.Z` → the Release workflow builds + publishes.

## Build & test

Python package (3.9+ runtime; mypy targets 3.10). Needs the `spawn` and `truffle`
CLIs on PATH at runtime, plus AWS credentials, for real runs.

- `pip install -e ".[dev]"` — install with dev deps
- `pytest` — pure-function unit tests (no AWS; the bulk of coverage)
- `ruff check .` && `mypy miniwdl_spawn` — lint + type-check

## Architecture

- `backend.py` — `SpawnContainer(WDL.runtime.task_container.TaskContainer)`; the
  miniwdl-facing adapter. Implements `global_init`, `process_runtime`, `_run`.
- `launch.py` / `staging.py` / `sizing.py` / `completion.py` — **pure** helpers
  (no I/O), unit-tested. Keep new logic here, not in `backend.py`, so it stays testable.

Discovered by miniwdl via the `miniwdl.plugin.container_backend` entry point
(`spawn = miniwdl_spawn.backend:SpawnContainer`) in `pyproject.toml`.

## Cost safety

Real runs launch billable EC2 instances. Any real-AWS test MUST set a TTL,
terminate explicitly, and leak-check afterward (no orphaned instances). The
backend always launches with `--on-complete terminate` and a TTL.

## Reuse / lineage

This mirrors `nf-spawn`'s proven design (same `spawn` CLI contract, same
`.exitcode`-in-S3 completion, same input-localization). When in doubt, check how
`nf-spawn/src/main/groovy/io/nextflow/spawn/SpawnTaskHandler.groovy` solved it.

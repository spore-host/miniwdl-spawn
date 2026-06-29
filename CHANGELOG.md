# Changelog

All notable changes to **miniwdl-spawn** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial `miniwdl-spawn` container backend (spore-host#395): a miniwdl
  `miniwdl.plugin.container_backend` plugin named `spawn` that runs each WDL task
  on an ephemeral EC2 instance via spore-host/spawn — the WDL analog of nf-spawn.
  - `SpawnContainer(TaskContainer)` — `_run()` implements the **S3 workdir
    bridge**: stage the task's `command` + local `work/` tree (inputs included)
    up to a per-attempt S3 prefix, launch an instance that reconstructs miniwdl's
    exact `/mnt/miniwdl_task_container` tree and runs the command the way miniwdl's
    local backend does (`/bin/bash ../command >> ../stdout.txt 2>> ../stderr.txt`,
    cwd `work/`, in the `runtime.docker` image when set), then pull
    `work/`+`stdout.txt`+`stderr.txt` back into the local host_dir so miniwdl
    collects outputs as usual. Completion is detected via a durable `.exitcode`
    object in S3 (uploaded last; survives the instance self-terminating).
  - Auto-sizing from `runtime { cpu, memory }` via `truffle search --pick-first`
    (override with `runtime.spawn_instance_type`).
  - Retries isolated by a `try-N` S3 prefix; `terminating()` cancels the instance
    (region-safe) and returns 130; results pulled for both success and failure.
  - `runtime` keys: `spawn_instance_type`, `spawn_spot`, `spawn_ttl`,
    `spawn_region`, `spawn_az`, `spawn_fsx`, `spawn_ami`. Config:
    `SPAWN_WORKDIR_S3` (required), `SPAWN_REGION`, `SPAWN_TTL`.
  - Pure-function unit tests (launch argv, S3 transfer argv/keys, staging script,
    sizing, completion) + backend call-order test; no AWS. End-to-end on real AWS
    is validated separately (spore-host#395 Phase 4).

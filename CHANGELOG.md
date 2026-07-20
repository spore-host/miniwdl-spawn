# Changelog

All notable changes to **miniwdl-spawn** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-19

### Changed
- **miniwdl-spawn now dispatches each task through `spawn task run`** instead of
  orchestrating the launch itself (spawn#386 adapter migration). It builds a
  spawn **TaskSpec** and runs `spawn task run` (detached), polls
  `spawn task status --check-complete`, and reads the **CompletionRecord** for the
  exit code. spawn now owns instance sizing (truffle), the S3 staging, the
  container run (Docker install + `docker run` for a `runtime.docker` image), the
  durable completion record, and a **scoped least-privilege IAM profile** (was
  `--iam-policy s3:FullAccess`) — so miniwdl-spawn no longer reimplements any of
  it. The task still runs exactly as miniwdl's local backend does
  (`/bin/bash ../command >> ../stdout.txt 2>> ../stderr.txt`, cwd `work/`).
- **The on-instance container dir moved from `/mnt/miniwdl_task_container` to
  `/var/tmp/miniwdl_task_container`.** spawn runs the task as the instance's
  unprivileged login user, which cannot create dirs under the root-owned `/mnt`;
  `/var/tmp` is world-writable and disk-backed (not tmpfs). The backend overrides
  `container_dir` so the absolute paths miniwdl bakes into the command resolve.
- **The `spawn_instance_type` runtime hint now maps to a truffle family
  allow-list** (e.g. `c7i.4xlarge` → the `c7i` family) rather than pinning the
  exact type; spawn's sizer picks the cheapest fit within it. (Exact-pin support
  is tracked as a spawn TaskSpec follow-up.)
- `truffle` is no longer required on `PATH` (spawn sizes the instance itself);
  `spawn` and `aws` are still required.

### Fixed
- Tasks with no input files no longer fail on the instance with a `cd: … work: No
  such file or directory` error. An empty local `work/` uploads as nothing, so
  stage-in never recreated it on the instance; the command now `mkdir -p work/`
  before entering it. (Found by the real-AWS `examples/hello.wdl` smoke.)

### Removed
- Bundled launch/staging/completion/sizing machinery (`launch.py`, `staging.py`,
  `completion.py`, `sizing.py`) — spawn owns these now.
- The `spawn_az` / `spawn_fsx` / `spawn_ami` runtime hints (they drove
  `spawn launch` flags that `spawn task run` does not yet accept; they will
  return once TaskSpec grows the corresponding fields).

## [0.1.0] - 2026-07-04

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
  - Pre-run fixes for the real-AWS path: single-instance teardown uses
    `spawn terminate <name> --yes` (not `spawn cancel`, which is for sweeps);
    the task instance is launched with `--iam-policy s3:FullAccess` so it can
    read/write the S3 bridge bucket; and a docker-ensure preamble installs
    Docker on stock AL2023 when the task has a `runtime.docker` image.
  - Pure-function unit tests (launch argv, S3 transfer argv/keys, staging script,
    sizing, completion) + backend call-order test; no AWS. End-to-end on real AWS
    is validated separately (spore-host#395 Phase 4).

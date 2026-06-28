# Changelog

All notable changes to **miniwdl-spawn** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial `miniwdl-spawn` container backend (spore-host#395): a miniwdl
  `miniwdl.plugin.container_backend` plugin named `spawn` that runs each WDL task
  on an ephemeral EC2 instance via spore-host/spawn — the WDL analog of nf-spawn.
  - `SpawnContainer(TaskContainer)` — `_run()` dispatches a task to `spawn launch`
    and polls a durable `.exitcode` object in S3 for completion.
  - Auto-sizing from `runtime { cpu, memory }` via `truffle search --pick-first`
    (override with `runtime.spawn_instance_type`).
  - Staging-script builder (S3 input localization, Docker run, output sync,
    `.exitcode`-last completion) ported from nf-spawn.
  - `runtime` keys: `spawn_instance_type`, `spawn_spot`, `spawn_ttl`,
    `spawn_region`, `spawn_az`, `spawn_fsx`, `spawn_ami`.
  - Pure-function unit tests (launch argv, staging, sizing, completion); no AWS.

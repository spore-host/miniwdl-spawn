# miniwdl-spawn

A [miniwdl](https://github.com/chanzuckerberg/miniwdl) **container backend** that
runs each WDL task on its own ephemeral EC2 instance via
[spore-host/spawn](https://github.com/spore-host/spawn) — purpose-sized,
auto-terminated when the task completes. It is the WDL analog of
[nf-spawn](https://github.com/spore-host/nf-spawn) for Nextflow.

> Status: early (v0.1.0). The pure dispatch/staging/sizing/completion logic and
> miniwdl backend wiring are implemented and unit-tested; end-to-end on real AWS
> is being validated (spore-host#395).

## How it works

`miniwdl` discovers backends through the `miniwdl.plugin.container_backend`
entry point. `miniwdl-spawn` registers one named **`spawn`**. For each WDL task,
its `SpawnContainer._run()`:

1. **Sizes** the instance from the task's `runtime { cpu, memory }` — picking the
   cheapest fitting type via `truffle search --pick-first` (override with
   `runtime.spawn_instance_type`). *(This is something nf-spawn can't do: Nextflow's
   `ext.instanceType` is manual; WDL's runtime block is declarative.)*
2. **Builds a staging script** (the instance's user-data): sync the task's S3
   workdir down, localize inputs, run the command (in Docker when the task has a
   `docker` image), capture the exit code, sync outputs back, and upload
   `.exitcode` **last**.
3. **Launches** via `spawn launch … --on-complete terminate --user-data-file …`
   (non-blocking).
4. **Polls** `s3://<workdir>/.exitcode` for completion — durable, so it survives
   the instance self-terminating — and maps the code to task success/failure.
5. **Cancels** (`spawn cancel`) if miniwdl aborts.

## Install

```bash
pip install miniwdl-spawn          # installs miniwdl too
# requires the `spawn` and `truffle` CLIs on PATH, and AWS credentials
```

## Use

```bash
export SPAWN_WORKDIR_S3=s3://my-bucket/miniwdl-runs   # required: shared task I/O
export SPAWN_REGION=us-east-1                          # optional (default us-east-1)

miniwdl run gatk-germline.wdl -i inputs.json \
  --cfg scheduler.container_backend=spawn
```

### Per-task `runtime` keys

| `runtime` key | Effect |
|---|---|
| `cpu`, `memory` | Auto-pick cheapest fitting instance (via truffle) |
| `docker` | Run the task command inside this image |
| `spawn_instance_type` | Force an exact instance type (skips auto-sizing) |
| `spawn_spot` | Use Spot pricing |
| `spawn_ttl` | Hard termination deadline (e.g. `"8h"`) |
| `spawn_region`, `spawn_az` | Pin region / AZ |
| `spawn_fsx` | Mount a shared FSx filesystem id (wide fan-out reference data) |
| `spawn_ami` | Launch from a specific AMI |

## Configuration

| Setting | Env | `--cfg` | Default |
|---|---|---|---|
| S3 workdir prefix (**required**) | `SPAWN_WORKDIR_S3` | `[spawn] workdir_s3` | — |
| Region | `SPAWN_REGION` | `[spawn] region` | `us-east-1` |
| Default TTL | `SPAWN_TTL` | `[spawn] ttl` | `4h` |

## Develop

```bash
pip install -e ".[dev]"
pytest            # pure-function unit tests (no AWS)
ruff check . && mypy miniwdl_spawn
```

## License

Apache-2.0.

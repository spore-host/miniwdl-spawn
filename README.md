# miniwdl-spawn

A [miniwdl](https://github.com/chanzuckerberg/miniwdl) **container backend** that
runs each WDL task on its own ephemeral EC2 instance via
[spore-host/spawn](https://github.com/spore-host/spawn) — purpose-sized,
auto-terminated when the task completes. It is the WDL analog of
[nf-spawn](https://github.com/spore-host/nf-spawn) for Nextflow.

> Status: early. The dispatch logic and miniwdl backend wiring are implemented and
> unit-tested; end-to-end on real AWS is validated (spore-host#395).

## How it works

`miniwdl` discovers backends through the `miniwdl.plugin.container_backend`
entry point. `miniwdl-spawn` registers one named **`spawn`**. For each WDL task,
its `SpawnContainer._run()`:

1. **Stages** the task's `command` file + local `work/` tree (inputs included) up
   to a per-attempt S3 prefix.
2. **Builds a spawn TaskSpec** — the shared workflow-adapter contract (spawn#386):
   the command runs exactly as miniwdl's local backend does
   (`/bin/bash ../command >> ../stdout.txt 2>> ../stderr.txt`, cwd `work/`), the
   task's `runtime { cpu, memory }` become the TaskSpec `resources` (spawn's sizer
   picks the cheapest fit via truffle — something nf-spawn can't do, since
   Nextflow's `ext.instanceType` is manual and WDL's runtime block is
   declarative), and a `docker` image becomes the TaskSpec `container`.
3. **Dispatches** `spawn task run` (detached). spawn sizes the instance, stages
   the tree back down at `container_dir`, runs the command (installing Docker and
   running the container on demand), writes a durable **completion record** to
   `s3://spawn-results-<acct>-<region>/tasks/<id>/completion.json`, and the
   instance self-terminates.
4. **Polls** `spawn task status --check-complete` for completion (durable — it
   survives the instance self-terminating), reads the exit code from the
   completion record, then pulls `work/`+`stdout.txt`+`stderr.txt` back so miniwdl
   collects outputs as usual.
5. **Cancels** the instance (`spawn terminate`) if miniwdl aborts.

## Install

```bash
pip install miniwdl-spawn          # installs miniwdl too
# requires the `spawn` CLI on PATH, and AWS credentials (spawn sizes via truffle itself)
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
| `cpu`, `memory` | Auto-pick cheapest fitting instance (spawn sizes via truffle) |
| `docker` | Run the task command inside this image |
| `spawn_instance_type` | Steer the instance **family** (e.g. `c7i.4xlarge` → `c7i`); spawn picks the cheapest fit within it |
| `spawn_spot` | Use Spot pricing (falls back to on-demand) |
| `spawn_ttl` | Hard termination deadline (e.g. `"8h"`) |
| `spawn_region` | Pin region |
| `spawn_architecture` | Constrain CPU architecture (`x86_64` / `arm64`) |

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

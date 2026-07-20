"""Build a spawn TaskSpec for one WDL task and parse its CompletionRecord.

miniwdl-spawn no longer orchestrates launch/staging/completion itself — it shells
out to ``spawn task run``, which owns S3 staging, the container run (Docker
install + ``docker run`` for a ``runtime.docker`` image), sizing (truffle), the
scoped IAM profile, and the durable completion record. This module is the
translation layer: it maps a miniwdl task's fields to the TaskSpec JSON shape
spawn expects, and reads the CompletionRecord back. Pure (no I/O, no AWS),
unit-tested without a cluster.

TaskSpec contract (spore-host/spawn pkg/taskproto): {task_id, command []string,
container?, resources{cpu,memory_gib,gpus,architecture,families,...},
inputs[]{source,destination}, outputs[]{source,destination},
lifecycle{ttl,on_complete}, env{}}. Manifests copy s3://<->local; a trailing
slash on the source means recursive.

The miniwdl subtlety: miniwdl hands ``_run`` a ``command`` *shell script* (not
argv) and bakes absolute container paths under ``container_dir`` into it, running
it cwd=``work`` as ``/bin/bash ../command >> ../stdout.txt 2>> ../stderr.txt``.
We keep that exact invocation, wrapped as a single ``bash -lc`` inner string, and
identity-mount the S3 work prefix at ``container_dir`` so those baked paths
resolve. The subclass overrides ``container_dir`` to a user-writable ``/var/tmp``
path (spawn runs the task as the unprivileged login user, which cannot create
dirs under root-owned ``/mnt``).
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Mapping, Optional

# A valid shell identifier for an env var name. spawn re-validates env keys and
# hard-fails the spec on an invalid one, so we drop non-identifier keys here.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Family prefix of an instance type, e.g. "c7i" from "c7i.4xlarge".
_FAMILY_RE = re.compile(r"^([a-z][a-z0-9]*?[0-9]+[a-z]*)\.")


def build_run_command(container_dir: str) -> str:
    """The inner shell command that runs the task exactly as miniwdl's local
    backend does: cwd = ``<container_dir>/work``, ``/bin/bash ../command`` with
    append-redirection to ``../stdout.txt`` / ``../stderr.txt``. Pure.

    Both ``command`` and the ``work/`` tree are staged under ``container_dir`` by
    the input manifest, so ``../command`` resolves whether the task runs bare on
    the host or inside spawn's ``docker run`` (which identity-bind-mounts the
    manifest dirs). We pre-create the redirect targets so the ``>>`` and the
    output sync always have files to act on.
    """
    cd = container_dir.rstrip("/")
    return (
        f": > {shlex.quote(cd + '/stdout.txt')} "
        f"&& : > {shlex.quote(cd + '/stderr.txt')} "
        f"&& cd {shlex.quote(cd + '/work')} "
        "&& /bin/bash ../command >> ../stdout.txt 2>> ../stderr.txt"
    )


def instance_type_family(instance_type: Optional[str]) -> Optional[str]:
    """Extract the family prefix from an instance type ("c7i" from "c7i.4xlarge"),
    or None. Maps the WDL ``spawn_instance_type`` runtime hint onto TaskSpec
    ``resources.families`` — spawn has no exact instance-type pin, so the hint
    steers the family and spawn's sizer picks the cheapest fit within it. Lossy:
    it does NOT pin the exact size."""
    if not instance_type:
        return None
    m = _FAMILY_RE.match(instance_type.strip())
    return m.group(1) if m else None


def memory_reservation_to_gib(mem_bytes: object) -> Optional[float]:
    """Coerce miniwdl's ``runtime_values['memory_reservation']`` (bytes) to GiB,
    or None if missing/unparseable/non-positive. miniwdl stores runtime.memory as
    ``memory_reservation`` in bytes (NOT ``memory``)."""
    if mem_bytes is None:
        return None
    try:
        val = float(mem_bytes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    return val / (1024**3)


def clean_env(environment: Optional[Mapping[str, str]]) -> dict:
    """Keep only env entries whose key is a valid shell identifier (spawn rejects
    the spec otherwise). Values are unrestricted.

    NOTE: miniwdl's task env is written into the ``command`` file itself (as
    ``export`` lines, matching its cli_subprocess contract), so callers generally
    pass ``{}`` here — the task's own env travels with the command, and this is
    only for spawn-level env if ever needed."""
    if not environment:
        return {}
    return {k: str(v) for k, v in environment.items() if _ENV_KEY_RE.match(k)}


def build_task_spec(
    *,
    task_id: str,
    container_dir: str,
    work_s3_uri: str,
    docker_image: str = "",
    cpu: Optional[int] = None,
    memory_reservation: object = None,
    architecture: Optional[str] = None,
    instance_hint: Optional[str] = None,
    spot: bool = False,
    ttl: str = "4h",
    on_complete: str = "terminate",
) -> dict:
    """Build the TaskSpec dict for one WDL task. Pure.

    ``work_s3_uri`` is the per-attempt S3 work prefix; miniwdl-spawn uploads the
    ``command`` file and the staged ``work/`` tree there before launch, and pulls
    results from there after. The prefix is identity-mounted into the instance at
    ``container_dir`` so the absolute paths miniwdl baked into ``command``
    resolve. The command runs ``/bin/bash ../command`` from ``work/`` — the exact
    miniwdl local-backend invocation.
    """
    work_src = work_s3_uri if work_s3_uri.endswith("/") else work_s3_uri + "/"
    cd = container_dir.rstrip("/")

    inner = build_run_command(cd)
    command = ["/bin/bash", "-lc", inner]

    resources: dict = {}
    if cpu and int(cpu) > 0:
        resources["cpu"] = int(cpu)
    mem_gib = memory_reservation_to_gib(memory_reservation)
    if mem_gib is not None:
        resources["memory_gib"] = mem_gib
    if architecture:
        resources["architecture"] = architecture
    fam = instance_type_family(instance_hint)
    if fam:
        resources["families"] = [fam]
    if spot:
        resources["purchase"] = "spot"
        resources["fallback"] = "on_demand"

    spec: dict = {
        "task_id": task_id,
        "command": command,
        "resources": resources,
        # Identity-mount the whole attempt prefix at container_dir: it holds both
        # the `command` file and the `work/` subtree, so `../command` (relative to
        # work/) resolves. Trailing slash on the source ⇒ recursive copy.
        "inputs": [{"source": work_src, "destination": cd}],
        # Sync the reconstructed tree back so miniwdl collects work/ + stdout/stderr.
        "outputs": [{"source": cd + "/", "destination": work_src}],
        "lifecycle": {"ttl": ttl, "on_complete": on_complete},
    }
    if docker_image.strip():
        spec["container"] = docker_image.strip()
    return spec


# ---- completion, from `spawn task status --check-complete` / -o json ----------

def check_complete_to_status(returncode: int) -> Optional[str]:
    """Map ``spawn task status --check-complete`` exit code to a status.

    spawn's contract: 0=completed, 1=failed, 2=running, 3=error. Returns
    "completed"/"failed" on 0/1, None on 2 (not done — poll again), and RAISES on
    3 (spawn couldn't determine status) or any unrecognized code (a contract
    change we want to hear about loudly)."""
    if returncode == 0:
        return "completed"
    if returncode == 1:
        return "failed"
    if returncode == 2:
        return None
    raise RuntimeError(
        f"`spawn task status --check-complete` returned error/unknown code {returncode}"
    )


def parse_completion_record(stdout: str) -> dict:
    """Parse the CompletionRecord JSON emitted by ``spawn task status <id> -o
    json`` (or ``spawn task run --wait -o json``). Returns the dict; raises on
    invalid JSON. Callers read ``exit_code`` (int) and ``state``
    ("completed"/"failed")."""
    rec = json.loads(stdout)
    if not isinstance(rec, dict):
        raise RuntimeError("completion record is not a JSON object")
    return rec

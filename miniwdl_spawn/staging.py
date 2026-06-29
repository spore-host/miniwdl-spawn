"""Build the per-task staging script that runs on the ephemeral instance.

The script is passed to ``spawn launch --user-data-file`` and reconstructs
miniwdl's exact container tree so the command's hard-coded container paths
resolve. On the instance it:

  1. recreates ``/mnt/miniwdl_task_container/{command,work/}`` (NOT under /tmp,
     which is tmpfs/RAM on AL2023), world-writable so a non-root container user
     can write outputs;
  2. downloads ``command`` and the whole ``work/`` tree (inputs included, at
     ``work/_miniwdl_inputs/...``) from S3;
  3. runs the command EXACTLY as miniwdl's local backend does — cwd = ``work``,
     ``/bin/bash ../command >> ../stdout.txt 2>> ../stderr.txt`` — bare, or inside
     the WDL ``runtime.docker`` image bind-mounting the container dir at the same
     path so ``../command`` resolves identically;
  4. captures the real exit code, syncs ``work/`` + ``stdout.txt`` + ``stderr.txt``
     back, then uploads ``.exitcode`` *last* (so its presence in S3 always trails
     the outputs — the durable completion signal);
  5. signals spored so the instance self-terminates.

All functions are pure string builders (no I/O), unit-tested without AWS.

NOTE: ``build_input_staging`` / ``normalize_s3_uri`` are retained for a future
zero-copy reference-data path (attached EBS volumes / shared FSx, mirroring
nf-spawn ``ext.volumes``/``ext.fsx``); the core bridge does not use them today —
inputs arrive via the ``work/`` sync.
"""

from __future__ import annotations

import shlex
from typing import Mapping, Optional

# Must match miniwdl's TaskContainer.container_dir so the command's absolute
# container paths (/mnt/miniwdl_task_container/work/...) resolve on the instance,
# whether the task runs bare or inside Docker.
CONTAINER_DIR = "/mnt/miniwdl_task_container"


def _q(s: str) -> str:
    return shlex.quote(s)


def normalize_s3_uri(uri: str) -> str:
    """Fix the ``s3:///bucket/key`` (empty authority) form some libraries emit."""
    if uri.startswith("s3:///"):
        return "s3://" + uri[len("s3:///") :]
    return uri


def build_run_line(docker_image: str, run_options: str = "") -> str:
    """Run the command the way miniwdl's cli_subprocess does: cwd = work,
    ``/bin/bash ../command`` with append-redirection to ../stdout.txt/../stderr.txt.
    Bare when no image, else inside Docker bind-mounting CONTAINER_DIR at the same
    path (so ``../command`` resolves to /mnt/miniwdl_task_container/command)."""
    redir = "/bin/bash ../command >> ../stdout.txt 2>> ../stderr.txt"
    if not docker_image.strip():
        return f'( cd "${{CD}}/work" && {redir} )\n'
    opts = (run_options.strip() + " ") if run_options.strip() else ""
    return (
        'docker run --rm -v "${CD}":"${CD}" -w "${CD}/work" '
        + opts
        + f"{_q(docker_image.strip())} /bin/bash -c {_q(redir)}\n"
    )


def build_setup_script(docker_image: str) -> str:
    """Per-task bootstrap that runs before staging. Ensures Docker when the task
    has a container image — stock AL2023 (the default spawn AMI) has none. Ported
    from nf-spawn's buildSetupScript; idempotent (``command -v docker`` guard), so
    a Docker-preinstalled AMI is a no-op. aws CLI + spored ship on AL2023 already.
    """
    if not docker_image.strip():
        return ""
    return (
        "# miniwdl-spawn: ensure Docker (stock AL2023 has none); idempotent.\n"
        "if ! command -v docker >/dev/null 2>&1; then\n"
        '  echo "miniwdl-spawn: installing Docker..." >&2\n'
        '  sudo dnf install -y docker || { echo "miniwdl-spawn: docker install failed" >&2; exit 1; }\n'
        "fi\n"
        "sudo systemctl enable --now docker 2>/dev/null || sudo systemctl start docker "
        '|| { echo "miniwdl-spawn: could not start docker" >&2; exit 1; }\n'
    )


def build_input_staging(
    inputs: Mapping[str, str], mount_basenames: Optional[Mapping[str, str]] = None
) -> str:
    """[Deferred/optional] Zero-copy reference-data localization (attached volume
    symlink) / S3 copy, for a future FSx/volumes path. Unused by the core bridge."""
    if not inputs:
        return ""
    mount_basenames = mount_basenames or {}
    out = ["# (optional) reference-data localization\n"]
    for stage_name, source in inputs.items():
        dest = f'"${{CD}}/work/"{_q(stage_name)}'
        base = stage_name.rsplit("/", 1)[-1]
        mount = mount_basenames.get(base)
        if mount:
            out.append(f"ln -sfn {_q(mount)} {dest}\n")
            continue
        uri = normalize_s3_uri(source)
        if uri.startswith("s3://"):
            rec = " --recursive" if uri.endswith("/") else ""
            out.append(f'aws s3 cp {_q(uri)} {dest} --region "${{AWS_REGION}}"{rec} --quiet\n')
    return "".join(out)


def build_staging_script(
    *,
    workdir_s3: str,
    region: str,
    docker_image: str = "",
    run_options: str = "",
    setup: str = "",
) -> str:
    """Assemble the full user-data staging script. Pure.

    ``workdir_s3`` is the per-attempt prefix (…/<run_id>/try-N); the script reads
    ``<prefix>/command`` + ``<prefix>/work`` and writes results back there.
    """
    sb: list[str] = ["#!/bin/bash\n", "set -uo pipefail\n\n"]
    sb.append(f"WORKDIR_S3={_q(workdir_s3)}\n")
    sb.append(f"AWS_REGION={_q(region)}\n")
    sb.append(f"CD={CONTAINER_DIR}\n\n")

    # Recreate the miniwdl container tree on the EBS root (not tmpfs), writable by
    # a non-root container user.
    sb.append('sudo mkdir -p "${CD}/work"\n')
    sb.append('sudo chown -R "$(id -u):$(id -g)" "${CD}"\n')
    sb.append('chmod -R 0777 "${CD}"\n\n')

    if setup.strip():
        sb.append(setup.rstrip() + "\n\n")

    # 1. Pull command + the whole work/ tree (inputs already inside it).
    sb.append(
        'aws s3 cp "${WORKDIR_S3}/command" "${CD}/command" --region "${AWS_REGION}" --quiet '
        '|| { echo "miniwdl-spawn: failed to fetch command" >&2; exit 1; }\n'
    )
    sb.append('aws s3 sync "${WORKDIR_S3}/work" "${CD}/work" --region "${AWS_REGION}" --quiet\n\n')

    # 2. Pre-create stdout/stderr so the >> redirection + back-cp always have targets.
    sb.append(': > "${CD}/stdout.txt"\n')
    sb.append(': > "${CD}/stderr.txt"\n\n')

    # 3. Run exactly like miniwdl's local backend; capture the real exit code.
    sb.append(build_run_line(docker_image, run_options))
    sb.append("TASK_RC=$?\n")
    sb.append('echo "${TASK_RC}" > "${CD}/.exitcode"\n\n')

    # 4. Push results back: work/ + stdout + stderr FIRST, .exitcode LAST.
    sb.append('aws s3 sync "${CD}/work" "${WORKDIR_S3}/work" --region "${AWS_REGION}" --quiet\n')
    sb.append(
        'aws s3 cp "${CD}/stdout.txt" "${WORKDIR_S3}/stdout.txt" --region "${AWS_REGION}" --quiet\n'
    )
    sb.append(
        'aws s3 cp "${CD}/stderr.txt" "${WORKDIR_S3}/stderr.txt" --region "${AWS_REGION}" --quiet\n'
    )
    sb.append(
        'aws s3 cp "${CD}/.exitcode" "${WORKDIR_S3}/.exitcode" --region "${AWS_REGION}" --quiet\n\n'
    )

    # 5. Signal completion so spored terminates the instance.
    sb.append('if [ "${TASK_RC}" -eq 0 ]; then S=success; else S=failed; fi\n')
    sb.append('spored complete --status "${S}" 2>/dev/null || touch /tmp/SPAWN_COMPLETE\n')
    return "".join(sb)

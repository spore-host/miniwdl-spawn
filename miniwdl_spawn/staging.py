"""Build the per-task staging script that runs on the ephemeral instance.

Ported from nf-spawn's buildStagingScript. The script is passed to
``spawn launch --user-data-file`` and, on the instance:

  1. syncs the task's S3 workdir down to a local dir (on the EBS root, NOT /tmp
     which is tmpfs/RAM on AL2023),
  2. localizes declared inputs (S3 copy, or symlink an attached-volume mount),
  3. runs the WDL command (inside Docker when the task has a container image,
     bind-mounting the workdir at the same path so relative paths resolve),
  4. captures the real exit code,
  5. syncs outputs back, then uploads ``.exitcode`` *last* (so its presence in
     S3 always trails the outputs — the completion signal),
  6. signals spored so the instance terminates.

All functions here are pure string builders (no I/O), unit-tested without AWS.
"""

from __future__ import annotations

import shlex
from typing import Mapping, Optional

LOCAL_DIR = "/var/lib/miniwdl-spawn-work"


def _q(s: str) -> str:
    """POSIX-shell quote."""
    return shlex.quote(s)


def normalize_s3_uri(uri: str) -> str:
    """Fix the ``s3:///bucket/key`` (empty authority) form some libraries emit."""
    if uri.startswith("s3:///"):
        return "s3://" + uri[len("s3:///") :]
    return uri


def build_input_staging(
    inputs: Mapping[str, str], mount_basenames: Optional[Mapping[str, str]] = None
) -> str:
    """Localize each declared input (stage_name -> source URI) into LOCAL_DIR.

    Three cases (mirroring nf-spawn): (a) an attached-volume mount whose basename
    matches the stage name -> symlink (zero-copy); (b) a local/file:// path ->
    symlink; (c) an s3:// source -> ``aws s3 cp``. Pure.
    """
    if not inputs:
        return ""
    mount_basenames = mount_basenames or {}
    lines = ["# Localize declared inputs by source URI.\n"]
    for stage_name, source in inputs.items():
        dest = f'"${{LOCAL_DIR}}/"{_q(stage_name)}'
        parent = stage_name.rsplit("/", 1)[0] if "/" in stage_name else ""
        mkparent = f'mkdir -p "${{LOCAL_DIR}}/"{_q(parent)}\n' if parent else ""

        base = stage_name.rsplit("/", 1)[-1]
        mount = mount_basenames.get(base)
        if mount:  # (a) zero-copy symlink to an attached volume
            lines.append(mkparent)
            lines.append(
                f"if [ -e {_q(mount)} ]; then ln -sfn {_q(mount)} {dest}; "
                f'else echo "miniwdl-spawn: mount {mount} missing" >&2; exit 1; fi\n'
            )
            continue

        uri = normalize_s3_uri(source)
        if not uri.startswith("s3://"):  # (b) local path -> symlink
            local = uri[len("file://") :] if uri.startswith("file://") else uri
            if local.startswith("/"):
                lines.append(mkparent)
                lines.append(f"if [ -e {_q(local)} ]; then ln -sfn {_q(local)} {dest}; fi\n")
            continue

        # (c) S3 source -> copy (recursive if it looks like a prefix/dir)
        recursive = " --recursive" if uri.endswith("/") else ""
        lines.append(mkparent)
        lines.append(
            f'aws s3 cp {_q(uri)} {dest} --region "${{AWS_REGION}}"{recursive} --quiet '
            f'|| {{ echo "miniwdl-spawn: failed to stage {stage_name}" >&2; exit 1; }}\n'
        )
    lines.append("\n")
    return "".join(lines)


def build_run_line(command_file: str, docker_image: str, run_options: str = "") -> str:
    """Run the task command, bare or inside Docker, capturing stdout/stderr."""
    if not docker_image.strip():
        return f"bash {command_file} 1>.command.out 2>.command.err\n"
    opts = (run_options.strip() + " ") if run_options.strip() else ""
    return (
        'docker run --rm -v "${LOCAL_DIR}":"${LOCAL_DIR}" -w "${LOCAL_DIR}" '
        + opts
        + f"{shlex.quote(docker_image.strip())} bash {command_file} "
        "1>.command.out 2>.command.err\n"
    )


def build_staging_script(
    *,
    workdir_s3: str,
    region: str,
    command: str,
    docker_image: str = "",
    inputs: Optional[Mapping[str, str]] = None,
    mount_basenames: Optional[Mapping[str, str]] = None,
    run_options: str = "",
    setup: str = "",
) -> str:
    """Assemble the full user-data staging script. Pure."""
    sb: list[str] = ["#!/bin/bash\n", "set -uo pipefail\n\n"]
    sb.append(f"WORKDIR_S3={_q(workdir_s3)}\n")
    sb.append(f"AWS_REGION={_q(region)}\n")
    sb.append(f"LOCAL_DIR={LOCAL_DIR}\n\n")
    sb.append('sudo mkdir -p "${LOCAL_DIR}" && sudo chown "$(id -u):$(id -g)" "${LOCAL_DIR}"\n')
    sb.append('chmod 0777 "${LOCAL_DIR}"\n\n')

    if setup.strip():
        sb.append(setup.rstrip() + "\n\n")

    # 1. Sync the task workdir down (command + miniwdl metadata), then cd in.
    sb.append('aws s3 sync "${WORKDIR_S3}" "${LOCAL_DIR}/" --region "${AWS_REGION}" --quiet\n')
    sb.append('cd "${LOCAL_DIR}"\n\n')

    # 2. Localize declared inputs.
    sb.append(build_input_staging(inputs or {}, mount_basenames))

    # 3. Materialize + run the command; capture the real exit code.
    sb.append("cat > .command.sh <<'MINIWDL_SPAWN_EOF'\n")
    sb.append(command.rstrip("\n") + "\n")
    sb.append("MINIWDL_SPAWN_EOF\n")
    sb.append("chmod +x .command.sh\n")
    sb.append(build_run_line(".command.sh", docker_image, run_options))
    sb.append("TASK_RC=$?\n")
    sb.append('echo "${TASK_RC}" > .exitcode\n\n')

    # 4. Sync outputs back FIRST (excluding .exitcode), then upload .exitcode ALONE
    #    so its appearance in S3 always trails the outputs.
    sb.append(
        'aws s3 sync "${LOCAL_DIR}/" "${WORKDIR_S3}" --region "${AWS_REGION}" '
        '--exclude ".exitcode" --quiet\n'
    )
    sb.append(
        'aws s3 cp .exitcode "${WORKDIR_S3%/}/.exitcode" --region "${AWS_REGION}" --quiet\n\n'
    )

    # 5. Signal completion so spored terminates the instance.
    sb.append('if [ "${TASK_RC}" -eq 0 ]; then S=success; else S=failed; fi\n')
    sb.append('spored complete --status "${S}" 2>/dev/null || touch /tmp/SPAWN_COMPLETE\n')
    return "".join(sb)

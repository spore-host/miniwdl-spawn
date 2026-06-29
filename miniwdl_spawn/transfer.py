"""S3 workdir bridge: stage a task's command + work dir up to S3 and pull
results back down (spore-host#395).

miniwdl is local-filesystem based: before ``_run`` it lays out ``{host_dir}/work/``
(with inputs under ``work/_miniwdl_inputs/...``) and the command string hard-codes
container paths under ``/mnt/miniwdl_task_container``; after ``_run`` it reads
``{host_dir}/stdout.txt`` / ``stderr.txt`` and globs ``{host_dir}/work/`` locally.
This module builds the (pure) S3 keys and ``aws s3`` argv that move those bytes
between the local host_dir, S3, and the ephemeral instance. The thin subprocess
calls live in backend.py; everything here is pure and unit-tested without AWS.
"""

from __future__ import annotations

import shlex
from typing import Mapping


def task_s3_prefix(base: str, run_id: str, try_counter: int) -> str:
    """S3 prefix for one task attempt. ``try-N`` isolates retries (miniwdl bumps
    try_counter on reset, producing work2/ etc.) so a stale attempt's .exitcode is
    never read as the next attempt's."""
    return f"{base.rstrip('/')}/{run_id}/try-{try_counter}"


def build_command_file_contents(command: str, env: Mapping[str, str]) -> str:
    """The contents of the ``command`` file, matching miniwdl's cli_subprocess
    contract: env exports first, then the command body verbatim."""
    lines = [f"export {k}={shlex.quote(str(v))}\n" for k, v in (env or {}).items()]
    body = command if command.endswith("\n") else command + "\n"
    return "".join(lines) + body


def build_upload_command_argv(local_command_file: str, s3_prefix: str, region: str) -> list[str]:
    """``aws s3 cp <local command> <prefix>/command``. Pure."""
    return [
        "aws", "s3", "cp", local_command_file, f"{s3_prefix}/command",
        "--region", region or "us-east-1", "--quiet",
    ]


def build_upload_work_argv(local_work_dir: str, s3_prefix: str, region: str) -> list[str]:
    """``aws s3 sync <local work dir> <prefix>/work`` (recursive, incl.
    _miniwdl_inputs/*). Pure."""
    return [
        "aws", "s3", "sync", local_work_dir, f"{s3_prefix}/work",
        "--region", region or "us-east-1", "--quiet",
    ]


def build_download_results_argv(
    s3_prefix: str,
    local_work_dir: str,
    local_stdout_txt: str,
    local_stderr_txt: str,
    region: str,
) -> list[list[str]]:
    """Argv list to pull results back into the (try-aware) local host paths:
    sync work/ down, then cp stdout.txt / stderr.txt. The caller passes
    ``host_work_dir()`` / ``host_stdout_txt()`` / ``host_stderr_txt()`` so this
    stays pure and ignorant of miniwdl internals. No ``--delete`` (don't risk the
    local tree). Pure."""
    r = region or "us-east-1"
    return [
        ["aws", "s3", "sync", f"{s3_prefix}/work", local_work_dir, "--region", r, "--quiet"],
        ["aws", "s3", "cp", f"{s3_prefix}/stdout.txt", local_stdout_txt, "--region", r, "--quiet"],
        ["aws", "s3", "cp", f"{s3_prefix}/stderr.txt", local_stderr_txt, "--region", r, "--quiet"],
    ]

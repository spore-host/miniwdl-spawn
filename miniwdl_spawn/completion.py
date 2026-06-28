"""Durable task-completion signalling via an ``.exitcode`` object in S3.

Ported from nf-spawn's readExitCodeFromS3/buildExitcodeProbeCommand. The
ephemeral instance self-terminates on task completion (``--on-complete
terminate``), so we cannot poll it over SSH after the fact — a probe would hit a
dead box forever (the nf-spawn #34 lesson). Instead the staging script uploads
``<workdir>/.exitcode`` as the *last* action; its presence in S3 is the durable
"done" signal that outlives the instance, and its contents are the exit status.
"""

from __future__ import annotations

from typing import Optional


def exitcode_uri(workdir_s3: str) -> str:
    """Return the S3 URI of the ``.exitcode`` object for a task workdir."""
    base = workdir_s3[:-1] if workdir_s3.endswith("/") else workdir_s3
    return f"{base}/.exitcode"


def build_exitcode_probe_argv(workdir_s3: str, region: str) -> list[str]:
    """Build the ``aws s3 cp <uri> -`` argv that fetches .exitcode to stdout.

    A non-zero exit from this command means the object doesn't exist yet
    (NoSuchKey / 404) -> task still running. Pure.
    """
    return ["aws", "s3", "cp", exitcode_uri(workdir_s3), "-", "--region", region or "us-east-1"]


def parse_exit_code(stdout: Optional[str]) -> Optional[int]:
    """Parse the integer exit code from .exitcode contents.

    Returns None if absent/blank/unparseable (treat as "not finished"), matching
    nf-spawn's parseExitCode. Tolerates trailing whitespace/newline.
    """
    if stdout is None:
        return None
    s = stdout.strip()
    if not s:
        return None
    # Be tolerant of a trailing newline or extra tokens; take the first token.
    token = s.split()[0]
    try:
        return int(token)
    except ValueError:
        return None

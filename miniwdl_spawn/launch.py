"""Build the ``spawn launch`` / ``spawn cancel`` argv for a WDL task.

Ported from nf-spawn's buildLaunchCommand. Uses the same stable spawn CLI
contract nf-spawn relies on: ``--on-complete terminate`` (instance self-destructs
when the task signals done), ``--user-data-file`` (NOT ``--user-data`` — the
nf-spawn #13 lesson), and the non-blocking ``--wait-for-*=false`` + ``-y`` flags
so the launch call returns immediately and we poll completion out-of-band.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LaunchSpec:
    """Inputs for one task's ``spawn launch`` invocation."""

    name: str
    instance_type: str
    region: str
    user_data_file: str
    ttl: str = "4h"
    spot: bool = False
    ami: str = ""
    volume_size: int = 0
    az: str = ""
    fsx_id: str = ""
    fsx_mount_point: str = ""
    # EBS snapshot mounts, each "snap-...:/mount[:ro|rw]" (already formatted).
    attach_volumes: list[str] = field(default_factory=list)


def build_launch_argv(spec: LaunchSpec) -> list[str]:
    """Build the ``spawn launch`` argv. Pure."""
    argv = [
        "spawn",
        "launch",
        spec.name,
        "--instance-type",
        spec.instance_type,
        "--region",
        spec.region,
        "--ttl",
        spec.ttl,
        "--on-complete",
        "terminate",
        "--user-data-file",
        spec.user_data_file,
        "--wait-for-running=false",
        "--wait-for-ssh=false",
        "-y",
    ]
    if spec.ami:
        argv += ["--ami", spec.ami]
    if spec.volume_size and spec.volume_size > 0:
        argv += ["--volume-size", str(spec.volume_size)]
    for v in spec.attach_volumes or []:
        argv += ["--attach-volume", v]
    if spec.az:
        argv += ["--az", spec.az]
    if spec.fsx_id:
        argv += ["--fsx-id", spec.fsx_id]
        if spec.fsx_mount_point:
            argv += ["--fsx-mount-point", spec.fsx_mount_point]
    if spec.spot:
        argv += ["--spot"]
    return argv


def build_cancel_argv(name: str, region: Optional[str]) -> list[str]:
    """Build ``spawn cancel <name> --region <region>``.

    Always include --region (the nf-spawn #58 lesson: a region-less cancel can
    silently fail and leak a billable instance).
    """
    argv = ["spawn", "cancel", name]
    if region:
        argv += ["--region", region]
    return argv

from miniwdl_spawn.launch import LaunchSpec, build_cancel_argv, build_launch_argv


def test_launch_argv_core_flags():
    argv = build_launch_argv(
        LaunchSpec(name="wdl-x", instance_type="c7g.4xlarge", region="us-east-1",
                   user_data_file="/tmp/s.sh", ttl="2h")
    )
    # Non-blocking + auto-terminate + user-data-FILE (not --user-data), the nf-spawn #13 lesson.
    assert argv[:3] == ["spawn", "launch", "wdl-x"]
    assert "--user-data-file" in argv
    assert argv[argv.index("--user-data-file") + 1] == "/tmp/s.sh"
    assert "--user-data" not in argv
    assert "--on-complete" in argv and argv[argv.index("--on-complete") + 1] == "terminate"
    assert "--wait-for-running=false" in argv and "--wait-for-ssh=false" in argv
    assert "-y" in argv
    assert argv[argv.index("--instance-type") + 1] == "c7g.4xlarge"
    assert argv[argv.index("--ttl") + 1] == "2h"
    # S3 access for the workdir bridge bucket (default spored role lacks it).
    assert argv[argv.index("--iam-policy") + 1] == "s3:FullAccess"


def test_launch_argv_optional_flags():
    argv = build_launch_argv(
        LaunchSpec(name="n", instance_type="t3.medium", region="us-west-2",
                   user_data_file="/tmp/s.sh", spot=True, ami="ami-123", volume_size=200,
                   az="us-west-2a", fsx_id="fs-abc", fsx_mount_point="/fsx",
                   attach_volumes=["snap-1:/ref:ro"])
    )
    assert "--spot" in argv
    assert argv[argv.index("--ami") + 1] == "ami-123"
    assert argv[argv.index("--volume-size") + 1] == "200"
    assert argv[argv.index("--az") + 1] == "us-west-2a"
    assert argv[argv.index("--fsx-id") + 1] == "fs-abc"
    assert argv[argv.index("--fsx-mount-point") + 1] == "/fsx"
    assert argv[argv.index("--attach-volume") + 1] == "snap-1:/ref:ro"


def test_launch_argv_omits_unset():
    argv = build_launch_argv(
        LaunchSpec(name="n", instance_type="t3.medium", region="us-east-1",
                   user_data_file="/tmp/s.sh")
    )
    for absent in ("--spot", "--ami", "--volume-size", "--az", "--fsx-id"):
        assert absent not in argv


def test_cancel_uses_terminate_not_sweep_cancel():
    # `spawn cancel` is for sweeps; single-instance teardown is `spawn terminate`.
    # Always region-scoped (#58 leak lesson) + --yes (non-interactive).
    assert build_cancel_argv("wdl-x", "eu-west-1") == [
        "spawn", "terminate", "wdl-x", "--region", "eu-west-1", "--yes"
    ]
    assert build_cancel_argv("wdl-x", None) == ["spawn", "terminate", "wdl-x", "--yes"]

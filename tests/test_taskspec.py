"""Unit tests for the pure TaskSpec builder + CompletionRecord parsing."""

import pytest

from miniwdl_spawn import taskspec

CD = "/var/tmp/miniwdl_task_container"


def _spec(**over):
    base = dict(
        task_id="wdl-run-1",
        container_dir=CD,
        work_s3_uri="s3://wd/run/try-1",
    )
    base.update(over)
    return taskspec.build_task_spec(**base)


def test_command_runs_bash_command_from_work_dir():
    spec = _spec()
    cmd = spec["command"]
    assert cmd[0] == "/bin/bash" and cmd[1] == "-lc"
    inner = cmd[2]
    # mkdir work/ (empty-input safety), pre-create stdout/stderr, cd into work/,
    # run ../command with redirects
    assert f"mkdir -p {CD}/work" in inner
    assert f"{CD}/work" in inner
    assert "/bin/bash ../command >> ../stdout.txt 2>> ../stderr.txt" in inner
    assert f"{CD}/stdout.txt" in inner and f"{CD}/stderr.txt" in inner


def test_input_manifest_identity_mounts_prefix_at_container_dir():
    spec = _spec()
    # whole attempt prefix (with trailing slash → recursive) → container_dir
    assert spec["inputs"] == [{"source": "s3://wd/run/try-1/", "destination": CD}]
    # output source is container_dir with a trailing slash → spawn syncs the tree back
    assert spec["outputs"] == [{"source": CD + "/", "destination": "s3://wd/run/try-1/"}]


def test_host_run_has_no_container_key():
    spec = _spec(docker_image="")
    assert "container" not in spec


def test_container_key_set_when_docker_image_given():
    spec = _spec(docker_image="ubuntu:22.04")
    assert spec["container"] == "ubuntu:22.04"


def test_resources_from_cpu_and_memory_reservation():
    spec = _spec(cpu=8, memory_reservation=32 * 1024**3)
    assert spec["resources"]["cpu"] == 8
    assert spec["resources"]["memory_gib"] == pytest.approx(32.0)


def test_resources_omitted_when_absent():
    spec = _spec()
    assert spec["resources"] == {}


def test_spot_maps_to_purchase_with_fallback():
    spec = _spec(spot=True)
    assert spec["resources"]["purchase"] == "spot"
    assert spec["resources"]["fallback"] == "on_demand"


def test_instance_hint_maps_to_family_not_exact():
    spec = _spec(instance_hint="c7i.4xlarge")
    assert spec["resources"]["families"] == ["c7i"]


def test_architecture_passthrough():
    spec = _spec(architecture="arm64")
    assert spec["resources"]["architecture"] == "arm64"


def test_lifecycle_defaults_terminate():
    spec = _spec(ttl="2h")
    assert spec["lifecycle"] == {"ttl": "2h", "on_complete": "terminate"}


def test_memory_reservation_to_gib_handles_junk():
    assert taskspec.memory_reservation_to_gib(None) is None
    assert taskspec.memory_reservation_to_gib(0) is None
    assert taskspec.memory_reservation_to_gib("nope") is None
    assert taskspec.memory_reservation_to_gib(1024**3) == pytest.approx(1.0)


def test_instance_type_family_edge_cases():
    assert taskspec.instance_type_family(None) is None
    assert taskspec.instance_type_family("garbage") is None
    assert taskspec.instance_type_family("m7i.large") == "m7i"


def test_check_complete_to_status_contract():
    assert taskspec.check_complete_to_status(0) == "completed"
    assert taskspec.check_complete_to_status(1) == "failed"
    assert taskspec.check_complete_to_status(2) is None
    with pytest.raises(RuntimeError):
        taskspec.check_complete_to_status(3)


def test_parse_completion_record():
    rec = taskspec.parse_completion_record('{"task_id":"t","exit_code":7,"state":"failed"}')
    assert rec["exit_code"] == 7 and rec["state"] == "failed"
    with pytest.raises(Exception):
        taskspec.parse_completion_record("not json")

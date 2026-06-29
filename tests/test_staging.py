import base64

from miniwdl_spawn.staging import (
    CONTAINER_DIR,
    build_run_line,
    build_staging_script,
    normalize_s3_uri,
)


def test_normalize_s3_uri_fixes_empty_authority():
    assert normalize_s3_uri("s3:///bucket/key") == "s3://bucket/key"
    assert normalize_s3_uri("s3://bucket/key") == "s3://bucket/key"


def test_container_dir_matches_miniwdl():
    # Must equal miniwdl's TaskContainer.container_dir so the command's hard-coded
    # /mnt/miniwdl_task_container/... paths resolve on the instance.
    assert CONTAINER_DIR == "/mnt/miniwdl_task_container"


def test_run_line_bare_uses_command_redirection_in_workdir():
    line = build_run_line("")
    assert 'cd "${CD}/work"' in line
    assert "/bin/bash ../command >> ../stdout.txt 2>> ../stderr.txt" in line
    assert "docker run" not in line


def test_run_line_docker_binds_container_dir_and_resolves_command():
    line = build_run_line("ubuntu:24.04", run_options="--gpus all")
    assert "docker run --rm" in line
    assert '-v "${CD}":"${CD}"' in line          # bind container dir at same path
    assert '-w "${CD}/work"' in line             # cwd = work, so ../command resolves
    assert "--gpus all" in line
    assert "ubuntu:24.04" in line
    assert "/bin/bash ../command >> ../stdout.txt 2>> ../stderr.txt" in line


def test_staging_script_layout_and_ordering():
    script = build_staging_script(
        workdir_s3="s3://b/runs/r1/try-1", region="us-east-1", docker_image=""
    )
    # Reconstructs the miniwdl tree on the EBS root (not /tmp), world-writable.
    assert 'CD=/mnt/miniwdl_task_container' in script
    assert 'sudo mkdir -p "${CD}/work"' in script
    assert 'chmod -R 0777 "${CD}"' in script
    assert "/tmp/" not in script.split("spored complete")[0]  # workdir not under /tmp

    # Pull command + work down; pre-create empty stdout/stderr for the >> targets.
    down_cmd = script.index('aws s3 cp "${WORKDIR_S3}/command"')
    down_work = script.index('aws s3 sync "${WORKDIR_S3}/work"')
    assert ': > "${CD}/stdout.txt"' in script and ': > "${CD}/stderr.txt"' in script

    # Capture rc; push work + stdout + stderr back, then .exitcode LAST.
    assert 'echo "${TASK_RC}" > "${CD}/.exitcode"' in script
    up_work = script.index('aws s3 sync "${CD}/work" "${WORKDIR_S3}/work"')
    up_out = script.index('aws s3 cp "${CD}/stdout.txt"')
    up_exit = script.index('aws s3 cp "${CD}/.exitcode"')
    assert down_cmd < down_work < up_work < up_out < up_exit  # .exitcode strictly last
    assert "spored complete" in script


def test_staging_script_docker_when_image_present():
    script = build_staging_script(
        workdir_s3="s3://b/p", region="us-east-1", docker_image="biocontainers/samtools:1.19"
    )
    assert "docker run --rm" in script
    assert "biocontainers/samtools:1.19" in script


def test_staging_script_base64_roundtrips():
    # spawn ships user-data base64; ensure the script encodes cleanly.
    script = build_staging_script(workdir_s3="s3://b/p", region="us-east-1", docker_image="")
    base64.b64encode(script.encode())

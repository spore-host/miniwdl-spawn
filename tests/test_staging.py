import base64

from miniwdl_spawn.staging import (
    build_input_staging,
    build_run_line,
    build_staging_script,
    normalize_s3_uri,
)


def test_normalize_s3_uri_fixes_empty_authority():
    assert normalize_s3_uri("s3:///bucket/key") == "s3://bucket/key"
    assert normalize_s3_uri("s3://bucket/key") == "s3://bucket/key"


def test_input_staging_s3_copy():
    s = build_input_staging({"reads.fq": "s3://data/reads.fq"})
    # shlex.quote leaves shell-safe strings unquoted; assert on the command, not quoting.
    assert "aws s3 cp s3://data/reads.fq" in s
    assert "--recursive" not in s  # not a prefix


def test_input_staging_directory_is_recursive():
    s = build_input_staging({"refdir": "s3://data/refs/"})
    assert "--recursive" in s


def test_input_staging_local_path_symlinks():
    s = build_input_staging({"db": "file:///opt/db"})
    assert "ln -sfn /opt/db" in s
    assert "aws s3 cp" not in s


def test_input_staging_attached_volume_zero_copy():
    # stage name basename matches an attached-volume mount -> symlink, no copy.
    s = build_input_staging(
        {"kraken2": "s3://huge/kraken2/"},
        mount_basenames={"kraken2": "/opt/databases/kraken2"},
    )
    assert "ln -sfn /opt/databases/kraken2" in s
    assert "aws s3 cp" not in s


def test_input_staging_quotes_unsafe_paths():
    # A space-containing source must be shell-quoted to stay one argument.
    s = build_input_staging({"weird name": "s3://data/a b.txt"})
    assert "'s3://data/a b.txt'" in s
    assert "'weird name'" in s


def test_run_line_bare_vs_docker():
    bare = build_run_line(".command.sh", "")
    assert bare.startswith("bash .command.sh")
    docker = build_run_line(".command.sh", "ubuntu:24.04", run_options="--gpus all")
    assert "docker run --rm" in docker
    assert "--gpus all" in docker
    assert "ubuntu:24.04" in docker


def test_staging_script_orders_outputs_before_exitcode():
    script = build_staging_script(
        workdir_s3="s3://b/work/aa/bb", region="us-east-1",
        command="echo hi > out.txt", docker_image="",
    )
    # Inputs synced down before the task; outputs synced back; .exitcode uploaded LAST.
    down = script.index('aws s3 sync "${WORKDIR_S3}"')
    up_outputs = script.index('aws s3 sync "${LOCAL_DIR}/"')
    up_exit = script.index("aws s3 cp .exitcode")
    assert down < up_outputs < up_exit
    # Exit code captured + completion signalled.
    assert 'echo "${TASK_RC}" > .exitcode' in script
    assert "spored complete" in script
    # Not run under Docker when no image.
    assert "docker run" not in script


def test_staging_script_uses_docker_when_image_present():
    script = build_staging_script(
        workdir_s3="s3://b/w", region="us-east-1",
        command="samtools --version", docker_image="biocontainers/samtools:1.19",
    )
    assert "docker run --rm" in script
    assert "biocontainers/samtools:1.19" in script


def test_staging_script_is_valid_bash_heredoc():
    # The command is embedded via a quoted heredoc; ensure the terminator is present
    # and the command body round-trips (no accidental interpolation).
    cmd = "python3 -c 'print(1+1)'\necho done"
    script = build_staging_script(
        workdir_s3="s3://b/w", region="us-east-1", command=cmd, docker_image="",
    )
    assert "<<'MINIWDL_SPAWN_EOF'" in script
    assert cmd in script
    assert "MINIWDL_SPAWN_EOF\n" in script
    # sanity: base64 round-trip (the way spawn ships user-data) doesn't choke
    base64.b64encode(script.encode())

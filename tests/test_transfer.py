from miniwdl_spawn.transfer import (
    build_command_file_contents,
    build_download_results_argv,
    build_upload_command_argv,
    build_upload_work_argv,
    task_s3_prefix,
)


def test_task_s3_prefix_keys_by_run_and_try():
    assert task_s3_prefix("s3://b/runs", "abc123", 1) == "s3://b/runs/abc123/try-1"
    assert task_s3_prefix("s3://b/runs/", "abc123", 1) == "s3://b/runs/abc123/try-1"
    # Retries land in a distinct prefix so attempt-1 .exitcode can't be misread.
    assert task_s3_prefix("s3://b/runs", "abc123", 2) != task_s3_prefix("s3://b/runs", "abc123", 1)


def test_command_file_contents_env_then_command():
    out = build_command_file_contents("samtools sort in.bam\n", {"REF": "/ref/h.fa", "THREADS": 8})
    assert "export REF=/ref/h.fa\n" in out
    assert "export THREADS=8\n" in out
    # exports precede the command body; body preserved.
    assert out.index("export REF") < out.index("samtools sort in.bam")
    assert out.endswith("samtools sort in.bam\n")


def test_command_file_contents_quotes_unsafe_env():
    out = build_command_file_contents("echo hi", {"MSG": "a b; rm -rf /"})
    assert "export MSG='a b; rm -rf /'\n" in out
    assert out.endswith("echo hi\n")  # trailing newline added


def test_command_file_no_env():
    assert build_command_file_contents("echo hi\n", {}) == "echo hi\n"


def test_upload_argvs():
    assert build_upload_command_argv("/h/command", "s3://b/p", "us-east-1") == [
        "aws", "s3", "cp", "/h/command", "s3://b/p/command", "--region", "us-east-1", "--quiet"
    ]
    assert build_upload_work_argv("/h/work", "s3://b/p", "us-west-2") == [
        "aws", "s3", "sync", "/h/work", "s3://b/p/work", "--region", "us-west-2", "--quiet"
    ]


def test_download_results_argvs():
    argvs = build_download_results_argv(
        "s3://b/p", "/h/work", "/h/stdout.txt", "/h/stderr.txt", "us-east-1"
    )
    assert len(argvs) == 3
    # work synced down (not cp); stdout/stderr cp'd to the try-aware local paths.
    assert argvs[0] == ["aws", "s3", "sync", "s3://b/p/work", "/h/work",
                        "--region", "us-east-1", "--quiet"]
    assert argvs[1] == ["aws", "s3", "cp", "s3://b/p/stdout.txt", "/h/stdout.txt",
                        "--region", "us-east-1", "--quiet"]
    assert argvs[2][3:5] == ["s3://b/p/stderr.txt", "/h/stderr.txt"]
    # No --delete on the download (never risk the local tree).
    assert all("--delete" not in a for a in argvs)


def test_download_default_region():
    argvs = build_download_results_argv("s3://b/p", "/w", "/o", "/e", "")
    assert argvs[0][-2] == "us-east-1"

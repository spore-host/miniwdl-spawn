from miniwdl_spawn.completion import (
    build_exitcode_probe_argv,
    exitcode_uri,
    parse_exit_code,
)


def test_exitcode_uri_normalizes_trailing_slash():
    assert exitcode_uri("s3://b/work/ab") == "s3://b/work/ab/.exitcode"
    assert exitcode_uri("s3://b/work/ab/") == "s3://b/work/ab/.exitcode"


def test_probe_argv():
    assert build_exitcode_probe_argv("s3://b/work/ab", "us-west-2") == [
        "aws", "s3", "cp", "s3://b/work/ab/.exitcode", "-", "--region", "us-west-2"
    ]


def test_probe_argv_default_region():
    assert build_exitcode_probe_argv("s3://b/x", "")[-1] == "us-east-1"


def test_parse_exit_code():
    assert parse_exit_code("0\n") == 0
    assert parse_exit_code("1") == 1
    assert parse_exit_code("  137  ") == 137
    # Absent / blank / unparseable => None ("not finished yet").
    assert parse_exit_code(None) is None
    assert parse_exit_code("") is None
    assert parse_exit_code("   ") is None
    assert parse_exit_code("not-a-number") is None

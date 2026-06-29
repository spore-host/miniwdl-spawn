"""Backend orchestration tests: assert _run's call ORDER and terminating/cancel
behavior with subprocess + filesystem stubbed (no AWS, no miniwdl run loop)."""

import pytest

from miniwdl_spawn import backend


class _Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _make_container(tmp_path, monkeypatch):
    """Build a SpawnContainer without invoking miniwdl's __init__ machinery."""
    c = backend.SpawnContainer.__new__(backend.SpawnContainer)
    c.run_id = "run_abc"
    c.host_dir = str(tmp_path)
    (tmp_path / "work").mkdir()
    c.try_counter = 1
    c.input_path_map = {}
    c.runtime_values = {"env": {}}
    c._workdir_s3_base = "s3://bkt/runs"
    c._region = "us-east-1"
    c._ttl = "4h"
    c._poll_interval = 0  # don't sleep in tests
    # host_* helpers from the real base class
    monkeypatch.setattr(c, "host_work_dir", lambda: str(tmp_path / "work"), raising=False)
    monkeypatch.setattr(c, "host_stdout_txt", lambda: str(tmp_path / "stdout.txt"), raising=False)
    monkeypatch.setattr(c, "host_stderr_txt", lambda: str(tmp_path / "stderr.txt"), raising=False)
    monkeypatch.setattr(c, "_resolve_instance_type", lambda: "t3.medium", raising=False)
    return c


def test_run_call_order_and_returns_exit_code(tmp_path, monkeypatch):
    c = _make_container(tmp_path, monkeypatch)
    monkeypatch.setattr(backend.shutil, "which", lambda _: "/usr/bin/aws")

    calls = []

    def fake_run(argv, check=False, capture_output=True, text=True):
        calls.append(argv)
        # The .exitcode probe (aws s3 cp .../.exitcode -) returns "0" once.
        if argv[:3] == ["aws", "s3", "cp"] and argv[3].endswith("/.exitcode") and argv[4] == "-":
            return _Result(returncode=0, stdout="0\n")
        return _Result(returncode=0, stdout="")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)

    rc = c._run(_logger(), lambda: False, "echo hi")
    assert rc == 0

    kinds = [_classify(a) for a in calls]
    # command upload -> work upload -> launch -> probe -> downloads (work+stdout+stderr)
    assert kinds.index("upload-command") < kinds.index("upload-work")
    assert kinds.index("upload-work") < kinds.index("launch")
    assert kinds.index("launch") < kinds.index("probe")
    assert kinds.index("probe") < kinds.index("download-work")
    assert "download-stdout" in kinds and "download-stderr" in kinds
    # command file was written locally
    assert (tmp_path / "command").read_text().strip() == "echo hi"


def test_run_terminating_cancels_and_returns_130(tmp_path, monkeypatch):
    c = _make_container(tmp_path, monkeypatch)
    monkeypatch.setattr(backend.shutil, "which", lambda _: "/usr/bin/aws")
    calls = []
    monkeypatch.setattr(
        backend.subprocess, "run",
        lambda argv, **kw: calls.append(argv) or _Result(returncode=0, stdout=""),
    )
    rc = c._run(_logger(), lambda: True, "echo hi")  # terminating() True immediately
    assert rc == 130
    assert any(a[:2] == ["spawn", "terminate"] for a in calls)


def test_resolve_instance_type_reads_miniwdl_runtime_keys(tmp_path, monkeypatch):
    # miniwdl stores runtime.cpu as "cpu" and runtime.memory as "memory_reservation"
    # (bytes), NOT "memory". Guard against reading the wrong key (silent no-sizing).
    c = backend.SpawnContainer.__new__(backend.SpawnContainer)
    c.runtime_values = {"cpu": 8, "memory_reservation": 32 * 1024**3}
    captured = {}

    def fake_pick(**kw):
        captured.update(kw)
        return "c7i.2xlarge"

    monkeypatch.setattr(backend.sizing, "pick_instance_type", fake_pick)
    assert c._resolve_instance_type() == "c7i.2xlarge"
    assert captured["cpu"] == 8
    assert captured["memory"] == 32 * 1024**3  # passed through from memory_reservation


def test_resolve_instance_type_override(tmp_path, monkeypatch):
    c = backend.SpawnContainer.__new__(backend.SpawnContainer)
    c.runtime_values = {"spawn_instance_type": "m7i.4xlarge", "cpu": 2}
    captured = {}
    monkeypatch.setattr(
        backend.sizing, "pick_instance_type",
        lambda **kw: captured.update(kw) or "m7i.4xlarge",
    )
    c._resolve_instance_type()
    assert captured["override"] == "m7i.4xlarge"


def test_run_requires_workdir_and_aws(tmp_path, monkeypatch):
    c = _make_container(tmp_path, monkeypatch)
    c._workdir_s3_base = ""
    with pytest.raises(RuntimeError, match="no S3 workdir"):
        c._run(_logger(), lambda: False, "echo")

    c._workdir_s3_base = "s3://bkt/runs"
    monkeypatch.setattr(backend.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="aws.*CLI"):
        c._run(_logger(), lambda: False, "echo")


# --- helpers ---------------------------------------------------------------
def _logger():
    import logging
    return logging.getLogger("test")


def _classify(argv):
    s = " ".join(argv)
    if argv[:2] == ["spawn", "launch"]:
        return "launch"
    if argv[:2] == ["spawn", "cancel"]:
        return "cancel"
    # aws s3 cp/sync ...: arg index 2 is cp|sync, 3 is src, 4 is dst (or "-" for probe).
    if argv[:2] == ["aws", "s3"]:
        op, src, dst = argv[2], argv[3], argv[4]
        if dst == "-" and src.endswith("/.exitcode"):
            return "probe"
        if op == "cp" and dst.endswith("/command"):
            return "upload-command"
        if op == "sync" and not src.startswith("s3://"):
            return "upload-work"
        if op == "sync" and src.startswith("s3://"):
            return "download-work"
        if op == "cp" and dst.endswith("stdout.txt"):
            return "download-stdout"
        if op == "cp" and dst.endswith("stderr.txt"):
            return "download-stderr"
    return "other:" + s

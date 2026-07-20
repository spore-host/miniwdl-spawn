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
    c.container_dir = backend.CONTAINER_DIR
    c.runtime_values = {"env": {}}
    c._workdir_s3_base = "s3://bkt/runs"
    c._region = "us-east-1"
    c._ttl = "4h"
    c._poll_interval = 0  # don't sleep in tests
    # host_* helpers from the real base class
    monkeypatch.setattr(c, "host_work_dir", lambda: str(tmp_path / "work"), raising=False)
    monkeypatch.setattr(c, "host_stdout_txt", lambda: str(tmp_path / "stdout.txt"), raising=False)
    monkeypatch.setattr(c, "host_stderr_txt", lambda: str(tmp_path / "stderr.txt"), raising=False)
    return c


def test_run_call_order_and_returns_exit_code(tmp_path, monkeypatch):
    c = _make_container(tmp_path, monkeypatch)
    monkeypatch.setattr(backend.shutil, "which", lambda _: "/usr/bin/x")

    calls = []

    def fake_run(argv, check=False, capture_output=True, text=True):
        calls.append(argv)
        # `spawn task status --check-complete` → 0 (completed) so the poll ends.
        if argv[:3] == ["spawn", "task", "status"] and "--check-complete" in argv:
            return _Result(returncode=0)
        # `spawn task status -o json` → a CompletionRecord with exit_code 0.
        if argv[:3] == ["spawn", "task", "status"] and "json" in argv:
            return _Result(returncode=0, stdout='{"task_id":"t","exit_code":0,"state":"completed"}')
        return _Result(returncode=0, stdout="")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)

    rc = c._run(_logger(), lambda: False, "echo hi")
    assert rc == 0

    kinds = [_classify(a) for a in calls]
    # command upload -> work upload -> task run -> status-probe -> downloads
    assert kinds.index("upload-command") < kinds.index("upload-work")
    assert kinds.index("upload-work") < kinds.index("task-run")
    assert kinds.index("task-run") < kinds.index("status-probe")
    assert kinds.index("status-probe") < kinds.index("download-work")
    assert "download-stdout" in kinds and "download-stderr" in kinds
    # command file was written locally
    assert (tmp_path / "command").read_text().strip() == "echo hi"
    # dispatched DETACHED — no --wait on the task run call.
    run_call = next(a for a in calls if _classify(a) == "task-run")
    assert "--wait" not in run_call


def test_run_failure_exit_code_still_pulls_results(tmp_path, monkeypatch):
    c = _make_container(tmp_path, monkeypatch)
    monkeypatch.setattr(backend.shutil, "which", lambda _: "/usr/bin/x")
    calls = []

    def fake_run(argv, check=False, capture_output=True, text=True):
        calls.append(argv)
        if argv[:3] == ["spawn", "task", "status"] and "--check-complete" in argv:
            return _Result(returncode=1)  # failed
        if argv[:3] == ["spawn", "task", "status"] and "json" in argv:
            return _Result(returncode=1, stdout='{"task_id":"t","exit_code":42,"state":"failed"}')
        return _Result(returncode=0, stdout="")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    rc = c._run(_logger(), lambda: False, "false")
    assert rc == 42
    kinds = [_classify(a) for a in calls]
    assert "download-work" in kinds  # results pulled even on failure


def test_run_terminating_cancels_and_returns_130(tmp_path, monkeypatch):
    c = _make_container(tmp_path, monkeypatch)
    monkeypatch.setattr(backend.shutil, "which", lambda _: "/usr/bin/x")
    calls = []
    monkeypatch.setattr(
        backend.subprocess, "run",
        lambda argv, **kw: calls.append(argv) or _Result(returncode=0, stdout=""),
    )
    rc = c._run(_logger(), lambda: True, "echo hi")  # terminating() True immediately
    assert rc == 130
    assert any(a[:2] == ["spawn", "terminate"] for a in calls)


def test_task_id_folds_in_try_counter(tmp_path, monkeypatch):
    c = _make_container(tmp_path, monkeypatch)
    c.try_counter = 3
    monkeypatch.setattr(backend.shutil, "which", lambda _: "/usr/bin/x")
    calls = []

    def fake_run(argv, check=False, capture_output=True, text=True):
        calls.append(argv)
        if argv[:3] == ["spawn", "task", "status"] and "--check-complete" in argv:
            return _Result(returncode=0)
        if argv[:3] == ["spawn", "task", "status"] and "json" in argv:
            return _Result(returncode=0, stdout='{"exit_code":0,"state":"completed"}')
        return _Result(returncode=0, stdout="")

    monkeypatch.setattr(backend.subprocess, "run", fake_run)
    c._run(_logger(), lambda: False, "echo hi")
    status = next(a for a in calls if a[:3] == ["spawn", "task", "status"])
    assert status[3] == "wdl-run-abc-3"  # run_id sanitized + try_counter


def test_run_requires_workdir_and_clis(tmp_path, monkeypatch):
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
    if argv[:3] == ["spawn", "task", "run"]:
        return "task-run"
    if argv[:3] == ["spawn", "task", "status"]:
        return "status-probe"
    if argv[:2] == ["spawn", "terminate"]:
        return "terminate"
    # aws s3 cp/sync ...: arg index 2 is cp|sync, 3 is src, 4 is dst.
    if argv[:2] == ["aws", "s3"]:
        op, src, dst = argv[2], argv[3], argv[4]
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

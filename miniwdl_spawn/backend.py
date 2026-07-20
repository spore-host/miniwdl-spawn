"""SpawnContainer: a miniwdl container backend that runs each task on spawn.

Registered via the ``miniwdl.plugin.container_backend`` entry point as ``spawn``.
Subclasses ``WDL.runtime.task_container.TaskContainer`` and implements the single
abstract method ``_run`` to dispatch the task to an ephemeral EC2 instance through
``spawn task run`` — the shared workflow-adapter protocol (spawn#386). spawn owns
S3 staging, the container run (Docker install + ``docker run`` for a
``runtime.docker`` image), sizing (truffle), the scoped IAM profile, and the
durable completion record; this module builds a TaskSpec and reads the
CompletionRecord back.

The task command is dispatched DETACHED (``spawn task run`` without ``--wait``) so
``_run`` can keep polling miniwdl's ``terminating()`` callback and cancel the
instance if the run is aborted; completion is observed via ``spawn task status``.

The pure building blocks live in taskspec.py / transfer.py (unit-tested without
AWS); this module is the thin miniwdl-facing adapter that wires them to the task's
runtime{} and workdir.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Dict, Optional

from WDL import Value
from WDL.runtime import config
from WDL.runtime.task_container import TaskContainer

from . import taskspec, transfer

logger = logging.getLogger("miniwdl-spawn")

# The on-instance dir miniwdl's container tree is reconstructed at. Must be
# user-writable: spawn runs the task command as the instance's unprivileged login
# user (`su - <user>`), which cannot create dirs under the root-owned `/mnt`.
# `/var/tmp` is world-writable (1777) and disk-backed (not tmpfs, unlike `/tmp`).
# We override miniwdl's default `/mnt/miniwdl_task_container` so the absolute
# container paths it bakes into the command resolve on the instance.
CONTAINER_DIR = "/var/tmp/miniwdl_task_container"


class SpawnContainer(TaskContainer):
    """Run a WDL task on an ephemeral EC2 instance via spore-host/spawn."""

    # ---- class-level config (read once) ----------------------------------
    _region: str = "us-east-1"
    _ttl: str = "4h"
    _workdir_s3_base: str = ""  # e.g. s3://my-bucket/miniwdl-runs ; required for real runs
    _poll_interval: float = 15.0

    def __init__(self, cfg: config.Loader, run_id: str, host_dir: str) -> None:
        super().__init__(cfg, run_id, host_dir)
        # Move off the root-owned /mnt default onto a user-writable path BEFORE
        # miniwdl maps any input paths (it derives them from self.container_dir),
        # so the command's baked absolute paths are writable on the instance.
        self.container_dir = CONTAINER_DIR

    @classmethod
    def global_init(cls, cfg: config.Loader, logger: logging.Logger) -> None:
        """One-time init: read spawn-related config. No container daemon to start."""
        # miniwdl config sections are optional; tolerate their absence.
        def _get(section: str, key: str, default: str) -> str:
            try:
                val = cfg.get(section, key)
                return val if val else default
            except Exception:
                return default

        cls._region = os.environ.get("SPAWN_REGION") or _get("spawn", "region", cls._region)
        cls._ttl = os.environ.get("SPAWN_TTL") or _get("spawn", "ttl", cls._ttl)
        cls._workdir_s3_base = os.environ.get("SPAWN_WORKDIR_S3") or _get(
            "spawn", "workdir_s3", cls._workdir_s3_base
        )

    @classmethod
    def detect_resource_limits(
        cls, cfg: config.Loader, logger: logging.Logger
    ) -> Dict[str, int]:
        """EC2 makes "the cloud" the resource pool; advertise a generous ceiling so
        miniwdl's scheduler doesn't cap concurrency on the (irrelevant) head node."""
        return {"cpu": 1 << 20, "mem_bytes": 1 << 60}

    # ---- per-task runtime handling ---------------------------------------
    def process_runtime(
        self, logger: logging.Logger, runtime_eval: "Dict[str, Value.Base]"
    ) -> None:
        """Read standard cpu/memory/docker plus our spawn_* runtime keys."""
        super().process_runtime(logger, runtime_eval)

        def _val(key: str):
            v = runtime_eval.get(key)
            return getattr(v, "value", v) if v is not None else None

        rv = self.runtime_values
        for key in (
            "spawn_instance_type",
            "spawn_ttl",
            "spawn_region",
            "spawn_architecture",
        ):
            val = _val(key)
            if val is not None:
                rv[key] = val
        spot = _val("spawn_spot")
        if spot is not None:
            rv["spawn_spot"] = bool(spot)
        # cpu/memory are populated by the base class when present.

    # ---- the dispatch ----------------------------------------------------
    def _run(
        self, logger: logging.Logger, terminating: Callable[[], bool], command: str
    ) -> int:
        """Run the task on an ephemeral EC2 instance; return its exit status.

        The S3 workdir bridge: stage the command + local ``work/`` tree up to S3,
        dispatch ``spawn task run`` (which reconstructs the tree on the instance,
        runs the task, and writes a durable completion record), poll for
        completion, then pull ``work/`` + stdout/stderr back into the local
        host_dir so miniwdl can collect outputs as usual. Requires
        ``SPAWN_WORKDIR_S3`` (or ``[spawn] workdir_s3``).
        """
        if not self._workdir_s3_base:
            raise RuntimeError(
                "miniwdl-spawn: no S3 workdir configured. Set SPAWN_WORKDIR_S3 "
                "(or [spawn] workdir_s3) to an s3:// prefix the run can read/write."
            )
        if shutil.which("aws") is None:
            raise RuntimeError("miniwdl-spawn: the `aws` CLI is required on PATH for S3 staging.")
        if shutil.which("spawn") is None:
            raise RuntimeError("miniwdl-spawn: the `spawn` CLI is required on PATH.")

        rv = self.runtime_values
        region = str(rv.get("spawn_region") or self._region)
        # Fold try_counter into the task_id: it names the instance AND keys the
        # completion record (tasks/<id>/completion.json), so a retry must not read
        # the previous attempt's record.
        task_id = f"wdl-{self.run_id}-{self.try_counter}".replace("_", "-")[:60]
        s3_prefix = transfer.task_s3_prefix(self._workdir_s3_base, self.run_id, self.try_counter)

        # 1. Ensure inputs are inside the local work/ tree, then write the command
        #    file (env exports + command) into host_dir — both are then uploaded.
        if self.input_path_map:
            self.copy_input_files(logger)
        command_file = os.path.join(self.host_dir, "command")
        with open(command_file, "w") as f:
            f.write(transfer.build_command_file_contents(command, rv.get("env", {})))

        # 2. Stage command + work/ up to S3. spawn identity-mounts the whole
        #    prefix at container_dir, so <prefix>/command → <cd>/command and
        #    <prefix>/work → <cd>/work.
        self._run_argv(transfer.build_upload_command_argv(command_file, s3_prefix, region), check=True)
        self._run_argv(
            transfer.build_upload_work_argv(self.host_work_dir(), s3_prefix, region), check=True
        )

        # 3. Build the TaskSpec and dispatch `spawn task run` (detached).
        spec = taskspec.build_task_spec(
            task_id=task_id,
            container_dir=self.container_dir,
            work_s3_uri=s3_prefix,
            docker_image=str(rv.get("docker", "")),
            cpu=rv.get("cpu"),
            memory_reservation=rv.get("memory_reservation"),
            architecture=rv.get("spawn_architecture"),
            instance_hint=rv.get("spawn_instance_type"),
            spot=bool(rv.get("spawn_spot", False)),
            ttl=str(rv.get("spawn_ttl") or self._ttl),
            on_complete="terminate",
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix=f"miniwdl-spawn-{task_id}-", delete=False
        ) as fh:
            json.dump(spec, fh)
            spec_file = fh.name

        try:
            logger.info("miniwdl-spawn: dispatching %s via `spawn task run`", task_id)
            # Launch DETACHED (no --wait): spawn sizes, launches, and the instance
            # writes its own completion record. We poll below so we can react to
            # terminating() and cancel.
            self._run_argv(
                ["spawn", "task", "run", "--spec", spec_file, "--region", region], check=True
            )

            # 4. Poll the durable completion record; pull results on completion.
            status = self._poll_completion(task_id, region, terminating)
            if status is None:  # terminating() fired
                return 130
            code = self._fetch_exit_code(task_id, region)
            # Pull results for BOTH success and failure — miniwdl reads
            # stdout/stderr/work even to build a CommandFailed.
            self._pull_results(s3_prefix, region, logger)
            return code
        finally:
            try:
                os.unlink(spec_file)
            except OSError:
                pass

    def _poll_completion(
        self, task_id: str, region: str, terminating: Callable[[], bool]
    ) -> Optional[str]:
        """Poll ``spawn task status --check-complete`` until the task finishes.
        Returns "completed"/"failed", or None if ``terminating()`` fired (after
        cancelling the instance). Raises on a spawn error."""
        probe = [
            "spawn", "task", "status", task_id, "--region", region, "--check-complete",
        ]
        while True:
            if terminating():
                self._cancel(task_id, region)
                return None
            out = self._run_argv(probe, check=False)
            status = taskspec.check_complete_to_status(out.returncode)
            if status is not None:
                return status
            time.sleep(self._poll_interval)

    def _fetch_exit_code(self, task_id: str, region: str) -> int:
        """Read the task's exit code from the CompletionRecord (`spawn task status
        <id> -o json`). Defaults to 1 if the record can't be read/parsed."""
        out = self._run_argv(
            ["spawn", "task", "status", task_id, "--region", region, "-o", "json"], check=False
        )
        try:
            rec = taskspec.parse_completion_record(out.stdout)
            return int(rec.get("exit_code", 1))
        except Exception:
            logger.warning("miniwdl-spawn: could not parse completion record for %s", task_id)
            return 1

    def _pull_results(self, s3_prefix: str, region: str, logger: logging.Logger) -> None:
        for argv in transfer.build_download_results_argv(
            s3_prefix, self.host_work_dir(), self.host_stdout_txt(), self.host_stderr_txt(), region
        ):
            self._run_argv(argv, check=False)

    def _run_argv(self, argv, check):
        return subprocess.run(argv, check=check, capture_output=True, text=True)

    def _cancel(self, task_id: str, region: str) -> None:
        """Best-effort terminate of the task's instance (named after task_id)."""
        try:
            self._run_argv(
                ["spawn", "terminate", task_id, "--region", region, "--yes"], check=False
            )
        except Exception as e:  # best-effort
            logger.warning("miniwdl-spawn: cancel of %s failed: %s", task_id, e)

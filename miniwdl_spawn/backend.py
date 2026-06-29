"""SpawnContainer: a miniwdl container backend that runs each task on spawn.

Registered via the ``miniwdl.plugin.container_backend`` entry point as ``spawn``.
Subclasses ``WDL.runtime.task_container.TaskContainer`` and implements the single
abstract method ``_run`` to dispatch the task to an ephemeral EC2 instance through
the ``spawn`` CLI, then poll a durable ``.exitcode`` object in S3 for completion.

The pure building blocks live in launch.py / staging.py / sizing.py /
completion.py (unit-tested without AWS); this module is the thin miniwdl-facing
adapter that wires them to the task's runtime{} and workdir.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Dict

from WDL import Value
from WDL.runtime import config
from WDL.runtime.task_container import TaskContainer

from . import completion, launch, sizing, staging, transfer

logger = logging.getLogger("miniwdl-spawn")


class SpawnContainer(TaskContainer):
    """Run a WDL task on an ephemeral EC2 instance via spore-host/spawn."""

    # ---- class-level config (read once) ----------------------------------
    _region: str = "us-east-1"
    _ttl: str = "4h"
    _workdir_s3_base: str = ""  # e.g. s3://my-bucket/miniwdl-runs ; required for real runs
    _poll_interval: float = 15.0

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
            "spawn_az",
            "spawn_fsx",
            "spawn_ami",
        ):
            val = _val(key)
            if val is not None:
                rv[key] = val
        spot = _val("spawn_spot")
        if spot is not None:
            rv["spawn_spot"] = bool(spot)
        # cpu/memory are populated by the base class when present.

    def _resolve_instance_type(self) -> str:
        rv = self.runtime_values
        # miniwdl's base process_runtime stores runtime.cpu as "cpu" (int) and
        # runtime.memory as "memory_reservation" (bytes) — NOT "memory".
        return sizing.pick_instance_type(
            override=rv.get("spawn_instance_type"),
            cpu=rv.get("cpu"),
            memory=rv.get("memory_reservation"),
            architecture=rv.get("spawn_architecture"),
        )

    # ---- the dispatch ----------------------------------------------------
    def _run(
        self, logger: logging.Logger, terminating: Callable[[], bool], command: str
    ) -> int:
        """Run the task on an ephemeral EC2 instance; return its exit status.

        The S3 workdir bridge: stage the command + local ``work/`` tree up to S3,
        launch an instance that reconstructs the miniwdl container tree and runs
        the task, then pull ``work/`` + stdout/stderr back into the local host_dir
        so miniwdl can collect outputs as usual. Requires ``SPAWN_WORKDIR_S3``
        (or ``[spawn] workdir_s3``).
        """
        if not self._workdir_s3_base:
            raise RuntimeError(
                "miniwdl-spawn: no S3 workdir configured. Set SPAWN_WORKDIR_S3 "
                "(or [spawn] workdir_s3) to an s3:// prefix the run can read/write."
            )
        if shutil.which("aws") is None:
            raise RuntimeError("miniwdl-spawn: the `aws` CLI is required on PATH for S3 staging.")

        rv = self.runtime_values
        region = rv.get("spawn_region") or self._region
        name = f"wdl-{self.run_id}".replace("_", "-")[:60]
        s3_prefix = transfer.task_s3_prefix(self._workdir_s3_base, self.run_id, self.try_counter)

        # 1. Ensure inputs are inside the local work/ tree, then write the command
        #    file (env exports + command) into host_dir — both are then uploaded.
        if self.input_path_map:
            self.copy_input_files(logger)
        command_file = os.path.join(self.host_dir, "command")
        with open(command_file, "w") as f:
            f.write(transfer.build_command_file_contents(command, rv.get("env", {})))

        # 2. Stage command + work/ up to S3.
        self._run_argv(transfer.build_upload_command_argv(command_file, s3_prefix, region), check=True)
        self._run_argv(
            transfer.build_upload_work_argv(self.host_work_dir(), s3_prefix, region), check=True
        )

        # 3. Build the per-task staging script + launch.
        docker_image = str(rv.get("docker", ""))
        script = staging.build_staging_script(
            workdir_s3=s3_prefix,
            region=region,
            docker_image=docker_image,
            setup=staging.build_setup_script(docker_image),
        )
        spec = launch.LaunchSpec(
            name=name,
            instance_type=self._resolve_instance_type(),
            region=region,
            user_data_file="",  # set below
            ttl=str(rv.get("spawn_ttl") or self._ttl),
            spot=bool(rv.get("spawn_spot", False)),
            ami=str(rv.get("spawn_ami", "")),
            az=str(rv.get("spawn_az", "")),
            fsx_id=str(rv.get("spawn_fsx", "")),
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", prefix=f"miniwdl-spawn-{name}-", delete=False
        ) as fh:
            fh.write(script)
            spec.user_data_file = fh.name

        try:
            logger.info("miniwdl-spawn: launching %s (%s)", name, spec.instance_type)
            self._run_argv(launch.build_launch_argv(spec), check=True)

            # 4. Poll the durable .exitcode object; pull results on completion.
            probe = completion.build_exitcode_probe_argv(s3_prefix, region)
            while True:
                if terminating():
                    self._cancel(name, region)
                    return 130
                out = self._run_argv(probe, check=False)
                if out.returncode == 0:
                    code = completion.parse_exit_code(out.stdout)
                    if code is not None:
                        # Pull results for BOTH success and failure — miniwdl reads
                        # stdout/stderr/work even to build a CommandFailed.
                        self._pull_results(s3_prefix, region, logger)
                        return code
                time.sleep(self._poll_interval)
        finally:
            try:
                os.unlink(spec.user_data_file)
            except OSError:
                pass

    def _pull_results(self, s3_prefix: str, region: str, logger: logging.Logger) -> None:
        for argv in transfer.build_download_results_argv(
            s3_prefix, self.host_work_dir(), self.host_stdout_txt(), self.host_stderr_txt(), region
        ):
            self._run_argv(argv, check=False)

    def _run_argv(self, argv, check):
        return subprocess.run(argv, check=check, capture_output=True, text=True)

    def _cancel(self, name: str, region: str) -> None:
        try:
            self._run_argv(launch.build_cancel_argv(name, region), check=False)
        except Exception as e:  # best-effort
            logger.warning("miniwdl-spawn: cancel of %s failed: %s", name, e)

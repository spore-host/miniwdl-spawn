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
import subprocess
import tempfile
import time
from typing import Callable, Dict

from WDL import Value
from WDL.runtime import config
from WDL.runtime.task_container import TaskContainer

from . import completion, launch, sizing, staging

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
        return sizing.pick_instance_type(
            override=rv.get("spawn_instance_type"),
            cpu=rv.get("cpu"),
            memory=rv.get("memory"),
            architecture=rv.get("spawn_architecture"),
        )

    # ---- the dispatch ----------------------------------------------------
    def _run(
        self, logger: logging.Logger, terminating: Callable[[], bool], command: str
    ) -> int:
        """Dispatch the task to a spawned instance; return its exit status.

        NOTE (Phase 3): the S3 workdir bridge — mapping this task's miniwdl
        host_dir to ``_workdir_s3_base/<run_id>`` and round-tripping inputs/outputs
        — is wired here. Requires ``SPAWN_WORKDIR_S3`` (or [spawn] workdir_s3).
        """
        if not self._workdir_s3_base:
            raise RuntimeError(
                "miniwdl-spawn: no S3 workdir configured. Set SPAWN_WORKDIR_S3 "
                "(or [spawn] workdir_s3) to an s3:// prefix the run can read/write."
            )

        rv = self.runtime_values
        region = rv.get("spawn_region") or self._region
        name = f"wdl-{self.run_id}".replace("_", "-")[:60]
        workdir_s3 = f"{self._workdir_s3_base.rstrip('/')}/{self.run_id}"

        script = staging.build_staging_script(
            workdir_s3=workdir_s3,
            region=region,
            command=command,
            docker_image=str(rv.get("docker", "")),
        )

        spec = launch.LaunchSpec(
            name=name,
            instance_type=self._resolve_instance_type(),
            region=region,
            user_data_file="",  # set below to the temp file path
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
            argv = launch.build_launch_argv(spec)
            logger.info("miniwdl-spawn: launching %s (%s)", name, spec.instance_type)
            subprocess.run(argv, check=True, capture_output=True, text=True)

            probe = completion.build_exitcode_probe_argv(workdir_s3, region)
            while True:
                if terminating():
                    self._cancel(name, region)
                    return 130
                out = subprocess.run(probe, capture_output=True, text=True)
                if out.returncode == 0:
                    code = completion.parse_exit_code(out.stdout)
                    if code is not None:
                        return code
                time.sleep(self._poll_interval)
        finally:
            try:
                os.unlink(spec.user_data_file)
            except OSError:
                pass

    def _cancel(self, name: str, region: str) -> None:
        try:
            subprocess.run(launch.build_cancel_argv(name, region), capture_output=True, text=True)
        except Exception as e:  # best-effort
            logger.warning("miniwdl-spawn: cancel of %s failed: %s", name, e)

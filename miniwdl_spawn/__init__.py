"""miniwdl-spawn: run each WDL task on an ephemeral EC2 instance via spore-host/spawn.

A miniwdl container backend (entry point ``miniwdl.plugin.container_backend`` =
``spawn``) that dispatches each task to a purpose-sized, auto-terminated EC2
instance through the ``spawn`` CLI — the WDL analog of nf-spawn for Nextflow.
"""

__version__ = "0.1.0"

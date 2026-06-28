"""Map a WDL task's runtime{} resources to an EC2 instance type.

A WDL win nf-spawn couldn't do: WDL's ``runtime { cpu, memory }`` is declarative,
so we can auto-pick the cheapest instance that fits via truffle. An explicit
``runtime.spawn_instance_type`` always wins; if truffle is unavailable we fall
back to a configurable default. Pure except for the one subprocess call to
truffle (injected for tests).
"""

from __future__ import annotations

import math
import shutil
import subprocess
from typing import Callable, Optional

# Default when neither an explicit type nor a successful truffle lookup is available.
DEFAULT_INSTANCE_TYPE = "t3.medium"


def memory_to_gib(mem: object) -> Optional[float]:
    """Coerce a WDL ``memory`` runtime value to GiB.

    WDL memory is an Int (bytes) or a String like "32 GB" / "16 GiB" / "512 MB".
    Returns None if it can't be parsed (caller then omits the --min-memory filter).
    """
    if mem is None:
        return None
    if isinstance(mem, (int, float)):
        return float(mem) / (1024**3)  # bytes -> GiB
    s = str(mem).strip()
    if not s:
        return None
    # Split leading number from unit suffix.
    num = ""
    i = 0
    while i < len(s) and (s[i].isdigit() or s[i] in ".eE+-"):
        num += s[i]
        i += 1
    try:
        val = float(num)
    except ValueError:
        return None
    unit = s[i:].strip().lower()
    factors = {
        "": 1 / (1024**3),  # bare number = bytes
        "b": 1 / (1024**3),
        "kb": 1e3 / (1024**3),
        "k": 1e3 / (1024**3),
        "kib": 1024 / (1024**3),
        "mb": 1e6 / (1024**3),
        "m": 1e6 / (1024**3),
        "mib": 1024**2 / (1024**3),
        "gb": 1e9 / (1024**3),
        "g": 1e9 / (1024**3),
        "gib": 1.0,
        "tb": 1e12 / (1024**3),
        "t": 1e12 / (1024**3),
        "tib": 1024.0,
    }
    factor = factors.get(unit)
    if factor is None:
        return None
    return val * factor


def build_truffle_argv(
    min_vcpu: Optional[int], min_memory_gib: Optional[float], architecture: Optional[str]
) -> list[str]:
    """Build the ``truffle search`` argv that returns the cheapest fitting type.

    ``--pick-first`` makes truffle emit only the top result's instance type, which
    truffle documents as "useful for piping to spawn". ``--show-price`` makes the
    default sort cheapest-first. Pure.
    """
    argv = ["truffle", "search", "--pick-first", "--show-price"]
    if min_vcpu and min_vcpu > 0:
        argv += ["--min-vcpu", str(int(min_vcpu))]
    if min_memory_gib and min_memory_gib > 0:
        # Round up so we never under-provision a fractional GiB request.
        argv += ["--min-memory", str(int(math.ceil(min_memory_gib)))]
    if architecture:
        argv += ["--architecture", architecture]
    return argv


def pick_instance_type(
    *,
    override: Optional[str] = None,
    cpu: Optional[int] = None,
    memory: object = None,
    architecture: Optional[str] = None,
    default: str = DEFAULT_INSTANCE_TYPE,
    runner: Optional[Callable[[list[str]], str]] = None,
) -> str:
    """Resolve the instance type for a task.

    Precedence: ``override`` (runtime.spawn_instance_type) > truffle cheapest-fit
    (from cpu/memory) > ``default``. ``runner`` runs the truffle argv and returns
    its stdout (injected in tests); when None, a real subprocess is used iff
    truffle is on PATH.
    """
    if override:
        return override.strip()

    min_mem_gib = memory_to_gib(memory)
    if (cpu is None or cpu <= 0) and min_mem_gib is None:
        return default  # nothing to size on

    argv = build_truffle_argv(cpu, min_mem_gib, architecture)

    if runner is None:
        if shutil.which("truffle") is None:
            return default

        def runner(a: list[str]) -> str:
            return subprocess.run(
                a, capture_output=True, text=True, timeout=120, check=True
            ).stdout

    try:
        out = runner(argv)
    except Exception:
        return default
    picked = (out or "").strip().splitlines()
    # --pick-first emits a single instance type; take the first non-empty line.
    for line in picked:
        line = line.strip()
        if line:
            return line
    return default

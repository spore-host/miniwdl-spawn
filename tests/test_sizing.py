from miniwdl_spawn.sizing import (
    DEFAULT_INSTANCE_TYPE,
    build_truffle_argv,
    memory_to_gib,
    pick_instance_type,
)


def test_memory_to_gib_units():
    assert memory_to_gib("32 GB") == 32 * 1e9 / (1024**3)
    assert memory_to_gib("16 GiB") == 16.0
    assert memory_to_gib("512 MB") == 512 * 1e6 / (1024**3)
    assert memory_to_gib(8 * 1024**3) == 8.0  # bytes (int)
    assert memory_to_gib(None) is None
    assert memory_to_gib("") is None
    assert memory_to_gib("lots") is None


def test_truffle_argv_rounds_memory_up_and_includes_filters():
    argv = build_truffle_argv(8, 30.1, "arm64")
    assert argv[:4] == ["truffle", "search", "--pick-first", "--show-price"]
    assert argv[argv.index("--min-vcpu") + 1] == "8"
    assert argv[argv.index("--min-memory") + 1] == "31"  # ceil(30.1)
    assert argv[argv.index("--architecture") + 1] == "arm64"


def test_truffle_argv_omits_zero_filters():
    argv = build_truffle_argv(None, None, None)
    assert "--min-vcpu" not in argv and "--min-memory" not in argv and "--architecture" not in argv


def test_override_wins_without_calling_truffle():
    called = []
    picked = pick_instance_type(
        override="m7i.2xlarge", cpu=64, memory="256 GB",
        runner=lambda a: called.append(a) or "should-not-be-used",
    )
    assert picked == "m7i.2xlarge"
    assert called == []  # truffle never invoked when override present


def test_picks_truffle_top_result():
    picked = pick_instance_type(
        cpu=8, memory="32 GB", architecture="x86_64",
        runner=lambda argv: "c7i.2xlarge\n",
    )
    assert picked == "c7i.2xlarge"


def test_falls_back_to_default_when_no_resources():
    # No cpu and no memory => nothing to size on => default, truffle not called.
    assert pick_instance_type(runner=lambda a: "nope") == DEFAULT_INSTANCE_TYPE


def test_falls_back_to_default_on_truffle_error():
    def boom(_argv):
        raise RuntimeError("truffle exploded")

    assert pick_instance_type(cpu=4, memory="8 GB", runner=boom) == DEFAULT_INSTANCE_TYPE

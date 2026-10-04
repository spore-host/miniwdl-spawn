"""lifecycle.cost_limit (miniwdl-spawn#12)."""

from __future__ import annotations

from miniwdl_spawn import backend, taskspec


def _spec(**kw) -> dict:
    base = dict(
        task_id="align-1",
        container_dir="/mnt/miniwdl_task_container",
        work_s3_uri="s3://b/runs/x",
    )
    base.update(kw)
    return taskspec.build_task_spec(**base)


def test_cost_limit_is_emitted():
    """The second belt: spored enforces TTL and cost independently, first to fire
    wins. Without a cap the only ceiling is the TTL (4h default), so a fan-out of
    N tasks has a worst case of N x 4h x the instance rate."""
    assert _spec(cost_limit=0.05)["lifecycle"]["cost_limit"] == 0.05


def test_cost_limit_omitted_when_unset():
    """Absent, not null — so spawn's own default applies and the spec stays
    minimal for the adapters that never set it."""
    assert "cost_limit" not in _spec()["lifecycle"]
    assert "cost_limit" not in _spec(cost_limit=None)["lifecycle"]


def test_zero_or_negative_is_treated_as_unset():
    """A zero cap would mean "terminate immediately", never what someone typing
    0 intends."""
    assert "cost_limit" not in _spec(cost_limit=0)["lifecycle"]
    assert "cost_limit" not in _spec(cost_limit=-1)["lifecycle"]


def test_ttl_and_on_complete_are_unaffected():
    assert _spec(ttl="30m", cost_limit=1.0)["lifecycle"] == {
        "ttl": "30m",
        "on_complete": "terminate",
        "cost_limit": 1.0,
    }


def test_string_values_are_coerced():
    """Config and env deliver strings; the spec is JSON, so the value has to be a
    number rather than "0.25"."""
    v = _spec(cost_limit="0.25")["lifecycle"]["cost_limit"]
    assert v == 0.25 and isinstance(v, float)


def test_unparseable_cost_limit_degrades_rather_than_raising():
    """A typo in miniwdl.cfg must not take down a workflow — it degrades to
    "bounded by TTL only", which is the previous behaviour, with a warning."""
    assert backend._parse_cost_limit("not-a-number") is None
    assert backend._parse_cost_limit("") is None
    assert backend._parse_cost_limit(None) is None


def test_parse_cost_limit_accepts_sane_values():
    assert backend._parse_cost_limit("0.05") == 0.05
    assert backend._parse_cost_limit(2) == 2.0
    # Non-positive is "no cap", consistent with the spec builder.
    assert backend._parse_cost_limit("0") is None
    assert backend._parse_cost_limit(-3) is None

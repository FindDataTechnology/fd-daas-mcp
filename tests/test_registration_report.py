"""Registration report: registered / failed / skipped_optional."""
from __future__ import annotations

from daas.fd_daas_mcp import registry

CORE = {"alerts", "cron", "composite", "daas", "dashboard", "gateway"}


def test_report_has_three_keys():
    registry.build()
    rep = registry.build_report()
    assert set(rep.keys()) == {"registered", "failed", "skipped_optional"}


def test_every_core_group_has_at_least_one_registered_tool():
    registry.build()
    rep = registry.build_report()
    from collections import defaultdict
    by_group = defaultdict(int)
    for g, _name in rep["registered"]:
        by_group[g] += 1
    for g in CORE:
        assert by_group[g] >= 1, f"core group {g!r} registered no tools"


def test_failed_has_no_core_group_entry():
    """A core-group tool failing to load/register is a loud failure."""
    registry.build()
    rep = registry.build_report()
    core_failures = [f for f in rep["failed"] if f[0] in CORE]
    assert not core_failures, f"core-group registration failures: {core_failures}"


def test_registered_count_matches_build_length():
    tools = registry.build()
    rep = registry.build_report()
    assert len(rep["registered"]) == len(tools)


def test_absent_optional_group_recorded_as_skipped_not_failed(fake_optional_source):
    """An optional group whose dep is absent is skipped_optional, not failed."""
    registry.build()
    rep = registry.build_report()
    skipped_groups = [g for g, _reason in rep["skipped_optional"]]
    assert "__fake_optional__" in skipped_groups
    # And it must NOT appear as a failure.
    fake_failures = [f for f in rep["failed"] if f[0] == "__fake_optional__"]
    assert not fake_failures, f"absent optional treated as failure: {fake_failures}"


def test_note_failed_appends_to_report():
    """Server-side app.tool failures are surfaced via note_failed."""
    registry.build()
    before = len(registry.build_report()["failed"])
    registry.note_failed("daas", "some_tool", "ValueError: bad")
    after = len(registry.build_report()["failed"])
    assert after == before + 1
    last = registry.build_report()["failed"][-1]
    assert last[0] == "daas" and last[1] == "some_tool"
    assert "app.tool" in last[2]


def test_core_groups_lists_six_core():
    cg = set(registry.core_groups())
    assert CORE <= cg

"""Registry build, namespacing, collisions, leaf isolation, and cron suppression."""
from __future__ import annotations

import threading

from daas.fd_daas_mcp import registry

CORE = {"alerts", "cron", "composite", "daas", "dashboard", "gateway"}
EXPECTED_COLLISIONS = {
    "create", "list", "get", "update", "delete",
}


def test_build_returns_at_least_155_tools():
    tools = registry.build()
    assert len(tools) >= 155, f"too few tools: {len(tools)}"


def test_all_six_core_groups_present():
    tools = registry.build()
    groups = {g for g, _, _ in tools}
    missing = CORE - groups
    assert not missing, f"missing core groups: {missing}"


def test_known_collisions_present_as_bare_names_in_two_plus_groups():
    """Colliding bare tool names appear in 2+ groups (and register namespaced)."""
    tools = registry.build()
    from collections import defaultdict
    where: dict[str, set[str]] = defaultdict(set)
    for g, name, _ in tools:
        where[name].add(g)
    for bare in EXPECTED_COLLISIONS:
        assert bare in where, f"expected collision {bare!r} not found in any group"
        assert len(where[bare]) >= 2, (
            f"expected {bare!r} in 2+ groups, got {where[bare]}")
    # The registry's own collisions() should report exactly these.
    coll = registry.collisions()
    assert EXPECTED_COLLISIONS <= set(coll.keys())


def test_namespaced_names_are_group_tool():
    tools = registry.build()
    for g, name, _ in tools:
        assert registry.namespaced(g, name) == f"{g}_{name}"


def test_leaf_isolation_resolves_distinct_files():
    leaf = registry.leaf_isolation_check()
    assert leaf, "leaf_isolation_check returned nothing"
    for leaf_name, files in leaf.items():
        paths = set(files.values())
        assert len(paths) == len(files), f"{leaf_name} has duplicate file refs: {files}"
        assert len(paths) >= 2, f"{leaf_name} not isolated (only {len(paths)} file): {files}"


def test_no_scheduler_thread_after_build():
    """Cron suppression: building the registry must not start an APScheduler thread."""
    registry.build()
    threads = [t.name for t in threading.enumerate()
               if "apscheduler" in t.name.lower() or "scheduler" in t.name.lower()]
    assert not threads, f"scheduler thread started during build: {threads}"


def test_build_cache_is_idempotent():
    a = registry.build()
    b = registry.build()
    assert a is b, "build() should return the cached list on second call"


def test_reset_cache_clears_cache_and_report():
    registry.build()
    assert registry._BUILD_CACHE is not None
    assert registry._BUILD_REPORT is not None
    registry.reset_cache()
    assert registry._BUILD_CACHE is None
    assert registry._BUILD_REPORT is None

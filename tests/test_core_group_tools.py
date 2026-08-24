"""Per-core-group tool invocation coverage.

Every core group (alerts/cron/composite/daas/dashboard/gateway) must have at
least one tool *invoked through its registered handler*, not only counted in
the registration report. Guards against a group silently going dark where
``registry.build()`` lists its tools but every handler is unresolvable or
uncallable.

The handlers are the callables returned by ``registry.build()``; calling them
end-to-end (priming the daas schema first so every group's tables exist in the
throwaway DB) proves the wiring is live and the CLI can JSON-print the result.
"""
from __future__ import annotations

import asyncio
import inspect
import json

from daas.fd_daas_mcp import registry

CORE = ["alerts", "cron", "composite", "daas", "dashboard", "gateway"]

# One read-only listing tool per core group. Chosen to need no network and to
# tolerate an empty (but schema-initialized) throwaway DB. Bare tool names -
# registry namespaces them as <group>_<name> at the server/CLI layer only.
PREFERRED = {
    "alerts": "list_channels",      # env-based; no DB
    "cron": "list_db_tasks",        # cron init_db creates its schema at import
    "composite": "list",            # table created by daas create_all (shared Base)
    "daas": "list_sources",         # daas create_all seeds the full schema
    "dashboard": "list_databases",  # filesystem listing; no DB
    "gateway": "list_data_mcps",     # gateway tables created by daas create_all
}


def _by_group() -> dict[str, dict[str, object]]:
    g: dict[str, dict[str, object]] = {}
    for grp, name, fn in registry.build():
        g.setdefault(grp, {})[name] = fn
    return g


def _call(fn, *args, **kwargs):
    if inspect.iscoroutinefunction(fn):
        return asyncio.run(fn(*args, **kwargs))
    return fn(*args, **kwargs)


def test_every_core_group_registers_tools():
    by_group = _by_group()
    for grp in CORE:
        assert grp in by_group and by_group[grp], f"core group {grp!r} registered no tools"


def test_every_core_group_has_an_invokable_tool():
    """Each core group has at least one handler that is actually callable
    end-to-end without an unhandled wiring error."""
    by_group = _by_group()
    # Prime the full schema: daas create_all builds every shared-Base table in
    # the throwaway DB so composite/gateway/cron listing tools find their tables.
    daas_fn = by_group["daas"].get("list_sources")
    if daas_fn is not None:
        _call(daas_fn)
    for grp in CORE:
        tools = by_group[grp]
        fn = tools.get(PREFERRED[grp]) or next(iter(tools.values()))
        # The assertion is "no unhandled exception": the handler is live.
        result = _call(fn)
        # Listing tools return a JSON-serializable structure the CLI can print.
        assert isinstance(result, (dict, list, str, int, float, bool)), (
            f"{grp}/{getattr(fn, '__name__', fn)} returned non-structured {type(result)!r}")
        json.dumps(result)  # CLI JSON-prints results; must serialize


def test_each_preferred_listing_tool_exists():
    """The preferred listing tool is present per group (catches a rename that
    would silently drop the invocation coverage to a fallback tool)."""
    by_group = _by_group()
    missing = {grp: PREFERRED[grp] for grp in CORE if PREFERRED[grp] not in by_group.get(grp, {})}
    assert not missing, f"preferred listing tools missing: {missing}"

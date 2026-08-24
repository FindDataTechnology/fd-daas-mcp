"""Offline self-check for the consolidated fd-daas-mcp server.

``run_invariants()`` is offline: no network, no LLM. It verifies:
  1. registered tools (>= 155 baseline; 6 core groups always present)
  2. the known collisions are namespaced (present as bare names in 2+ groups)
  3. colliding leaf modules (registry_service, database) resolve to distinct files
  4. no APScheduler thread started (cron suppression worked)
  5. registration report: no core-group tool in ``failed``; optional-skipped listed

``main()`` additionally runs a best-effort gateway health probe (network:
pings each http upstream, auto-flips transport on failure/recovery). A
degraded upstream is a warning, not a hard failure — the gateway may be
intentionally down when selfcheck runs (e.g. CI without the data-fetch
server).

The invariant logic lives in :func:`run_invariants` so it can be invoked both
from the ``__main__`` CLI and from ``tests/test_selfcheck.py`` - same contract,
no drift. The network probe is deliberately outside ``run_invariants`` so the
offline contract (and the 7-check test) is preserved.

Run: ``fd-daas-mcp/.venv/bin/python -m daas.fd_daas_mcp.selfcheck``
"""
from __future__ import annotations

import os
import sys
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from daas.fd_daas_mcp import registry

# Load the repo .env so DAAS_DATABASE_URL is set before any group loads (cron's
# init_db opens the DB at import). cwd may be fd-daas-mcp/ under `uv run
# --directory`, so resolve the repo root from this file's location and make a
# relative sqlite URL absolute against it.
_REPO = Path(__file__).resolve().parents[3]
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO / ".env")
except ImportError:
    pass
_u = os.environ.get("DAAS_DATABASE_URL", "")
if _u.startswith("sqlite:///") and not _u.startswith("sqlite:////"):
    _rel = _u[len("sqlite:///"):]
    if not os.path.isabs(_rel):
        os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_REPO}/{_rel}"

CORE = {"alerts", "cron", "composite", "daas", "dashboard", "gateway"}
# Groups documented as dropped (not tracked for restore). See registry.py.
# (pdf was restored as the local vector-search group - see registry.py SOURCES
# + openspec/changes/add-pdf-vector-search; no longer listed as dropped.)
DROPPED = {
    "scrapling": "2026-07-12-fold-scrapling-add-firecrawl",
    "firecrawl": "2026-07-12-fold-scrapling-add-firecrawl",
    "massive": "2026-07-06-add-massive-datasources",
}
EXPECTED_COLLISIONS = {
    "create", "list", "get", "update", "delete",
}


def run_invariants() -> dict[str, Any]:
    """Run every selfcheck invariant and return a structured result.

    Returns ``{"ok": bool, "checks": [...], "report": {...},
    "tool_count": int, "group_counts": {...}}`` where each check is
    ``{"name", "ok", "detail"}``. ``ok`` is True only if every check passed.
    """
    registry.reset_cache()
    tools = registry.build()
    counts = Counter(g for g, _, _ in tools)
    rep = registry.build_report()

    checks: list[dict[str, Any]] = []

    # [1] tool count + core groups present
    missing_core = CORE - set(counts.keys())
    # ponytail: P4 dissolved leader + dropped 6 generic gateway aliases
    # (gateway 13->7); baseline lowered from 170 to 155.
    ok1 = (not missing_core) and len(tools) >= 155
    checks.append({
        "name": "tool-count",
        "ok": ok1,
        "detail": f"{len(tools)} tools; groups={dict(counts)}; missing_core={sorted(missing_core)}",
    })

    # [2] collisions namespaced
    coll = registry.collisions()
    missing_coll = EXPECTED_COLLISIONS - set(coll.keys())
    checks.append({
        "name": "collisions",
        "ok": not missing_coll,
        "detail": f"{len(coll)} collisions={sorted(coll.keys())}; missing={sorted(missing_coll)}",
    })

    # [3] leaf-module isolation
    leaf = registry.leaf_isolation_check()
    leaf_ok = True
    leaf_detail: list[str] = []
    for name, files in leaf.items():
        paths = set(files.values())
        ok = len(paths) == len(files) and len(paths) >= 2
        leaf_ok = leaf_ok and ok
        leaf_detail.append(f"{name}: {len(paths)} distinct ({'OK' if ok else 'FAIL'})")
    checks.append({"name": "leaf-isolation", "ok": leaf_ok, "detail": "; ".join(leaf_detail)})

    # [4] no scheduler thread after load (cron suppression)
    threads = [t.name for t in threading.enumerate()
               if "apscheduler" in t.name.lower() or "scheduler" in t.name.lower()]
    checks.append({
        "name": "no-scheduler-thread",
        "ok": not threads,
        "detail": f"{threads or 'none'}",
    })

    # [5] registration report: no core-group failure; show skipped_optional
    core = set(registry.core_groups())
    core_failures = [f for f in rep["failed"] if f[0] in core]
    checks.append({
        "name": "report-no-core-failure",
        "ok": not core_failures,
        "detail": (f"failed={rep['failed']} core_failures={core_failures} "
                   f"skipped_optional={rep['skipped_optional']}"),
    })

    # [6] pdf optional group: registered when the [pdf] extra is present,
    # skipped_optional when absent. Either is OK; a load failure is not.
    _pdf_dep = registry._can_import("sqlite_vec")
    _skipped_groups = {g for g, _ in rep["skipped_optional"]}
    if _pdf_dep:
        ok6 = "pdf" in counts and "pdf" not in _skipped_groups
        detail6 = f"pdf extra present -> pdf registered ({counts.get('pdf', 0)} tools)"
    else:
        ok6 = "pdf" in _skipped_groups and "pdf" not in counts
        detail6 = "pdf extra absent -> pdf skipped_optional"
    checks.append({"name": "pdf-optional-state", "ok": ok6, "detail": detail6})

    # [7] default DB path never resolves inside the installed package
    # (database-autobootstrap): with DAAS_DATABASE_URL unset, the writable
    # default must be cwd or ~/.fd-daas-mcp/daas.db, never the in-package path.
    checks.append(_check_default_db_not_in_package())

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "report": rep,
        "tool_count": len(tools),
        "group_counts": dict(counts),
    }


def _check_default_db_not_in_package() -> dict[str, Any]:
    """With DAAS_DATABASE_URL unset, the resolved default DB path must NOT be
    inside the installed package directory (read-only under a normal install)."""
    fd_home = Path(__file__).resolve().parents[2]  # fd-daas-mcp/
    for sub in ("daas-mcp", "models"):
        p = fd_home / sub
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    try:
        from daas_database import (  # type: ignore
            default_db_path,
            inside_installed_package,
            resolve_db_url,
        )
        saved_url = os.environ.pop("DAAS_DATABASE_URL", None)
        saved_reg = os.environ.pop("DAAS_REGISTRY_DB", None)
        try:
            path = default_db_path()
            url = resolve_db_url(None)
            in_pkg = inside_installed_package(path)
            ok = not in_pkg
            detail = f"default={path}; in_package={in_pkg}; url={url}"
        finally:
            if saved_url is not None:
                os.environ["DAAS_DATABASE_URL"] = saved_url
            if saved_reg is not None:
                os.environ["DAAS_REGISTRY_DB"] = saved_reg
    except Exception as e:  # noqa: BLE001
        ok = False
        detail = f"check errored: {type(e).__name__}: {e}"
    return {"name": "default-db-not-in-package", "ok": ok, "detail": detail}


def _gateway_health_line() -> str:
    """Best-effort gateway health probe line for the selfcheck report.

    Unlike :func:`run_invariants` (which is fully offline), this helper
    touches the network: it pings each enabled gateway upstream's HTTP
    endpoint and auto-flips transport on failure/recovery (mirroring the
    client-pool fallback). A degraded upstream is a **warning**, not a
    failure — the gateway may be intentionally down when selfcheck runs
    (e.g. CI without the data-fetch server). Never raises; returns a
    ``[SKIP]`` line if the probe module is unavailable or errored.
    """
    gateway = Path(__file__).resolve().parents[2] / "gateway-mcp"
    if str(gateway) not in sys.path:
        sys.path.insert(0, str(gateway))
    try:
        from gateway_tools import gateway_health_sync  # type: ignore
    except Exception as e:  # noqa: BLE001
        return f"[SKIP] gateway-health: probe unavailable ({type(e).__name__}: {e})"
    try:
        result = gateway_health_sync()
    except Exception as e:  # noqa: BLE001
        return f"[SKIP] gateway-health: probe errored ({type(e).__name__}: {e})"
    ups = result.get("upstreams", [])
    if not ups:
        return "[OK] gateway-health: no enabled upstreams to probe"
    degraded = [u for u in ups if "degraded" in u.get("action", "")]
    line = "; ".join(
        f"{u['name']}={u.get('transport_after', u.get('transport_before'))}"
        f"({u.get('action', '?')})"
        for u in ups
    )
    flag = "DEGRADED" if degraded else "OK"
    return f"[{flag}] gateway-health: {len(ups)} upstream(s): {line}"


def main() -> int:
    result = run_invariants()
    for c in result["checks"]:
        flag = "OK" if c["ok"] else "FAIL"
        print(f"[{flag}] {c['name']}: {c['detail']}")
    print(f"\ntotal: {result['tool_count']} tools across {len(result['group_counts'])} groups")
    # Best-effort gateway health probe (network). Runs after the offline
    # invariants so a degraded upstream never masks an invariant failure.
    print(_gateway_health_line())
    if result["ok"]:
        print("\n=== SELF-CHECK PASSED ===")
        return 0
    print("\n=== SELF-CHECK FAILED ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())

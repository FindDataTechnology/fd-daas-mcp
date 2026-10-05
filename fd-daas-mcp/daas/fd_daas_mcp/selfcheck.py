"""Offline self-check for the consolidated fd-daas-mcp server.

``run_invariants()`` is offline: no network, no LLM. It verifies:
  1. registered tools (>= 155 baseline; 6 core groups always present)
  2. the known collisions are namespaced (present as bare names in 2+ groups)
  3. colliding leaf modules (registry_service, database) resolve to distinct files
  4. no APScheduler thread started (cron suppression worked)
  5. registration report: no core-group tool in ``failed``; optional-skipped listed
  6. pdf optional group: registered or skipped_optional, never failed
  7. default DB path never resolves inside the installed package
  8. tool-surface manifest covers every registry group/tool (a new tool must be
     graded in ``tool_surface.yaml``; commercial requires an explicit entry)
  9. tool-surface manifest has no ghost entries (manifest-only tools/groups fail)

``main()`` additionally runs a best-effort gateway health probe (network:
pings each http upstream, auto-flips transport on failure/recovery). A
degraded upstream is a warning, not a hard failure — the gateway may be
intentionally down when selfcheck runs (e.g. CI without the data-fetch
server).

The invariant logic lives in :func:`run_invariants` so it can be invoked both
from the ``__main__`` CLI and from ``tests/test_selfcheck.py`` - same contract,
no drift. The network probe is deliberately outside ``run_invariants`` so the
offline contract (and the check-list test) is preserved.

The tool-surface manifest (checks 8/9, facet-mcp-foundation-v1 task 2.2) lives
at ``tool_surface.yaml`` next to this module. Consumer-side CI references
:func:`commercial_tool_set` — the manifest's explicit commercial set as
namespaced ``<group>_<tool>`` names — and asserts its own exposure set is a
subset of it.

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

# Tool-surface manifest (facet-mcp-foundation-v1): exposure levels, host forms,
# and the manifest file sitting next to this module.
SURFACE_LEVELS = {"commercial", "internal"}
SURFACE_HOSTS = {"shared-readonly-catalog", "per-deployment-workspace", "local-stdio"}
MANIFEST_PATH = Path(__file__).resolve().parent / "tool_surface.yaml"


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

    # [8][9] tool-surface manifest <-> registry (facet-mcp-foundation-v1 2.2):
    # coverage (every registry tool graded; commercial must be explicit) and
    # ghosts (no manifest-only entries). A malformed manifest fails both.
    try:
        manifest = load_manifest()
        load_error = None
    except Exception as e:  # noqa: BLE001
        manifest, load_error = None, f"{type(e).__name__}: {e}"
    if manifest is None:
        checks.append({"name": "tool-surface-coverage", "ok": False,
                       "detail": f"manifest load failed: {load_error}"})
        checks.append({"name": "tool-surface-no-ghosts", "ok": False,
                       "detail": f"manifest load failed: {load_error}"})
        commercial: set[str] = set()
    else:
        cov, ghost = _tool_surface_checks(manifest, tools, rep["skipped_optional"])
        checks.append(cov)
        checks.append(ghost)
        commercial = commercial_tool_set(manifest=manifest)

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "report": rep,
        "tool_count": len(tools),
        "group_counts": dict(counts),
        "commercial_tools": sorted(commercial),
    }


def load_manifest(path: str | Path | None = None) -> dict[str, Any]:
    """Parse and shape-check the tool-surface manifest.

    The default path is ``tool_surface.yaml`` next to this module. Raises
    ``ValueError`` with a actionable message on structural violations (bad
    version, unknown level/host, group without any grading, empty skills) so
    both the selfcheck and consumer-side CI fail loudly on a malformed file.
    """
    import yaml  # noqa: PLC0415 - kept local so importing selfcheck never hard-requires pyyaml

    p = Path(path) if path is not None else MANIFEST_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{p}: manifest must be a YAML mapping")
    if data.get("manifest_version") != 1:
        raise ValueError(f"{p}: unsupported manifest_version={data.get('manifest_version')!r}")
    if not isinstance(data.get("server"), str) or not data["server"]:
        raise ValueError(f"{p}: 'server' must be a non-empty string")
    groups = data.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise ValueError(f"{p}: 'groups' must be a non-empty mapping")
    for gname, gspec in groups.items():
        gspec = gspec or {}
        level = gspec.get("level")
        tools = gspec.get("tools") or {}
        if level is not None and level not in SURFACE_LEVELS:
            raise ValueError(f"{p}: group '{gname}' level {level!r} not in {sorted(SURFACE_LEVELS)}")
        if level is None and not tools:
            raise ValueError(f"{p}: group '{gname}' needs a 'level' or a per-tool 'tools' map")
        for tname, tlevel in tools.items():
            if tlevel not in SURFACE_LEVELS:
                raise ValueError(f"{p}: {gname}_{tname} level {tlevel!r} not in {sorted(SURFACE_LEVELS)}")
    hosts = data.get("hosts")
    if (not isinstance(hosts, list) or not hosts
            or any(h not in SURFACE_HOSTS for h in hosts)):
        raise ValueError(f"{p}: 'hosts' must be a non-empty list within {sorted(SURFACE_HOSTS)}")
    skills = data.get("skills")
    if not isinstance(skills, dict):
        raise ValueError(f"{p}: 'skills' must be a mapping with consume/create lists")
    for role in ("consume", "create"):
        names = skills.get(role)
        if (not isinstance(names, list) or not names
                or any(not isinstance(s, str) or not s for s in names)):
            raise ValueError(f"{p}: skills.{role} must be a non-empty list of skill names")
    return data


def resolve_surface_level(manifest: dict[str, Any], group: str, tool: str) -> str | None:
    """Effective exposure level for ``(group, tool)``: the explicit per-tool
    entry wins, else the group-level ``level``, else ``None`` (unclassified)."""
    gspec = (manifest.get("groups") or {}).get(group) or {}
    level = (gspec.get("tools") or {}).get(tool)
    if level is None:
        level = gspec.get("level")
    return level


def commercial_tool_set(manifest: dict[str, Any] | None = None,
                        path: str | Path | None = None) -> set[str]:
    """The manifest's commercial set as namespaced ``<group>_<tool>`` names.

    Consumer-side CI (e.g. the business-mcp daas domain) asserts its exposure
    set is a subset of this. Only EXPLICIT ``commercial`` entries count: the
    selfcheck's coverage rule forbids inheriting commercial from a group-level
    posture, so the explicit set is the complete commercial surface. Reads the
    manifest only (no registry build, no group module loading), so it is cheap
    and safe to import from foreign repos.
    """
    m = manifest if manifest is not None else load_manifest(path)
    return {
        registry.namespaced(group, tool)
        for group, gspec in (m.get("groups") or {}).items()
        for tool, level in ((gspec or {}).get("tools") or {}).items()
        if level == "commercial"
    }


def _tool_surface_checks(manifest: dict[str, Any],
                         tools: list[tuple[str, str, Any]],
                         skipped_optional: list) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the two manifest<->registry checks (facet-mcp-foundation-v1 2.2).

    Returns ``(coverage_check, ghost_check)``:

    - coverage: every registry group exists in the manifest and every registry
      tool resolves to a level; additionally a tool may only be commercial via
      an EXPLICIT per-tool entry, so group-level ``commercial`` postures must
      enumerate all their tools and a newly added tool can never silently
      become commercial (it fails as unclassified/explicit-required instead).
    - ghosts: every manifest group exists in ``registry.SOURCES`` and every
      explicit manifest tool entry exists in the registry. Optional groups
      skipped this run (dep absent) are excused from the tool-entry comparison.
    """
    registry_groups: dict[str, set[str]] = {}
    for g, name, _ in tools:
        registry_groups.setdefault(g, set()).add(name)
    skipped = {g for g, _ in skipped_optional}
    manifest_groups = manifest.get("groups") or {}

    problems: list[str] = []
    for g in sorted(registry_groups):
        gspec = manifest_groups.get(g)
        if gspec is None:
            problems.append(f"group '{g}' not in manifest")
            continue
        explicit = gspec.get("tools") or {}
        for t in sorted(registry_groups[g]):
            level = resolve_surface_level(manifest, g, t)
            if level is None:
                problems.append(f"{registry.namespaced(g, t)}: unclassified (add it to the manifest)")
            elif level == "commercial" and t not in explicit:
                problems.append(f"{registry.namespaced(g, t)}: commercial via group default "
                                f"(explicit per-tool entry required)")

    ghosts: list[str] = []
    for g, gspec in manifest_groups.items():
        gspec = gspec or {}
        if g not in registry.SOURCES:
            ghosts.append(f"group '{g}' not in registry SOURCES")
            continue
        if g in skipped:
            continue  # optional group not loaded this run (dep absent)
        reg = registry_groups.get(g, set())
        for t in sorted(gspec.get("tools") or {}):
            if t not in reg:
                ghosts.append(f"{registry.namespaced(g, t)}: manifest entry absent from registry")

    total = sum(len(v) for v in registry_groups.values())
    commercial_n = len(commercial_tool_set(manifest=manifest))
    cap = lambda items: "; ".join(items[:12]) + (f" ... (+{len(items) - 12} more)" if len(items) > 12 else "")
    coverage = {
        "name": "tool-surface-coverage",
        "ok": not problems,
        "detail": (f"{total} tools across {len(registry_groups)} groups covered; "
                   f"commercial={commercial_n}") if not problems else cap(problems),
    }
    ghost_check = {
        "name": "tool-surface-no-ghosts",
        "ok": not ghosts,
        "detail": ("no ghost entries") if not ghosts else cap(ghosts),
    }
    return coverage, ghost_check


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

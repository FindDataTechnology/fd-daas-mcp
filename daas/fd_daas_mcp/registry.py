"""Tool registry for the consolidated fd-daas-mcp server.

Each source group's tool code lives in-package at ``fd-daas-mcp/<group>-mcp/``
(moved, not rewritten, from the former ``mcp/<group>-mcp/`` dirs). This module
imports each group's tool functions with per-group ``sys.modules`` isolation and
returns ``[(group, tool_name, func)]`` for the server/CLI to register under the
collision-free ``<group>_<tool>`` namespace.

Two harvest modes (per source):
  ``inline=True``  -> tool fns are ``@app.tool``-decorated in ``server.py`` (load server.py)
  ``inline=False`` -> tool fns are imported into ``server.py`` from ``*_tools.py`` (load those)
  ``suppress=True`` -> neutralize cron's dangerous import-time side effects (load_schedules /
                       shutdown_scheduler) but keep ``init_db()`` (idempotent DDL, required for
                       the full schema e.g. ``schedules.data_job_id``).
"""
from __future__ import annotations

import ast
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("fd-daas-mcp")

REPO = Path(__file__).resolve().parents[3]  # daas/fd_daas_mcp/registry.py -> repo root
FD_HOME = REPO / "fd-daas-mcp"

SOURCES: dict[str, dict[str, Any]] = {
    "alerts":    {"dir": "alerts-mcp",    "inline": True},
    "cron":      {"dir": "cron-mcp",      "inline": True,  "suppress": True},
    "composite": {"dir": "composite-mcp", "inline": False},
    "daas":      {"dir": "daas-mcp",      "inline": False},
    "dashboard": {"dir": "dashboard-mcp", "inline": True},
    "gateway":   {"dir": "gateway-mcp",   "inline": False},
    # Research group: persisted research bundle (entity+indicator collections,
    # rules, dashboard, cron pipeline + generated markdown report). inline=False
    # like daas - server.py imports tools from research_tools.py and registers
    # them via app.tool(<name>); surfaces as research_<name>. See
    # openspec/changes/add-concept-research.
    "research":  {"dir": "research-mcp",  "inline": False},
    # Workflow group: manifest-driven ordered runs (workflows table + engine).
    # inline=False like daas/research - server.py imports tools from
    # workflow_tools.py and registers via app.tool(<name>); surfaces as
    # workflow_<name>. See openspec/changes/rearchitect-daas-layered-mcps.
    "workflow":  {"dir": "workflow-mcp",  "inline": False},
    # Optional groups: build() loads them only when ``dep`` imports, else records
    # the group as skipped_optional (INFO, not a failure). The pdf group is local
    # PDF/text vector search (sentence-transformers + sqlite-vec + pdfplumber);
    # gated on sqlite_vec (the truly-absent dep; sentence-transformers is already
    # in the venv transitively, so gating on it would load-but-error). See
    # openspec/changes/add-pdf-vector-search.
    "pdf":       {"dir": "pdf-mcp",       "inline": False, "optional": True, "dep": "sqlite_vec"},
    # To add another OPTIONAL group, give it ``"optional": True`` and ``"dep": "<import>"``.
    #
    # Dropped groups - lost with the prior fd-daas-mcp and not tracked for
    # restore here. Each has an archived openspec spec to restore from:
    #   scrapling, firecrawl -> archive/2026-07-12-fold-scrapling-add-firecrawl
    #   massive              -> archive/2026-07-06-add-massive-datasources
    # (pdf was restored - see the `pdf` SOURCES entry above + the `pdf` extra in
    # pyproject.toml; no longer listed as dropped.)
}

_GROUP_DIR_SEGMENTS = tuple(f"/{s['dir']}/" for s in SOURCES.values())

_KEEP_PREFIXES = (
    "fastmcp", "mcp", "sqlalchemy", "pydantic", "starlette", "click", "pandas",
    "dotenv", "models", "uvicorn", "anyio", "httpx", "apscheduler", "greenlet",
    "typing_extensions", "typing", "json", "logging", "pathlib", "importlib",
    "ast", "sys", "os", "re", "datetime", "collections", "functools",
    "enum", "dataclasses", "contextlib", "inspect", "threading", "time",
    "sqlalchemy.", "pydantic.", "fastmcp.", "apscheduler.", "anyio.",
    "starlette.", "pandas.", "click.", "dotenv.", "httpx.", "uvicorn.",
)


def _evict_source_modules() -> None:
    for key in list(sys.modules.keys()):
        if key.startswith("_fdsrc_"):
            del sys.modules[key]
            continue
        mod = sys.modules.get(key)
        if mod is None:
            continue
        f = getattr(mod, "__file__", None) or ""
        if any(seg in f for seg in _GROUP_DIR_SEGMENTS):
            del sys.modules[key]


def _load_module_unique(name: str, path: Path) -> Any:
    full = f"_fdsrc_{name}"
    spec = importlib.util.spec_from_file_location(full, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _toplevel_nodes(stmts):
    """Yield AST statement nodes, descending into if/try/with/for/while blocks
    but yielding function/class defs (so decorators can be inspected) without
    descending into their bodies."""
    for node in stmts:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node
            continue
        if isinstance(node, ast.If):
            yield node
            yield from _toplevel_nodes(node.body)
            yield from _toplevel_nodes(node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            yield node
            yield from _toplevel_nodes(node.body)
        elif isinstance(node, (ast.For, ast.While)):
            yield node
            yield from _toplevel_nodes(node.body)
            yield from _toplevel_nodes(node.orelse)
        elif isinstance(node, ast.Try):
            yield node
            yield from _toplevel_nodes(node.body)
            for h in node.handlers:
                yield from _toplevel_nodes(h.body)
            yield from _toplevel_nodes(node.orelse)
            yield from _toplevel_nodes(node.finalbody)
        else:
            yield node


def _is_app_attr(node: ast.Ast, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "app"
    )


def _parse_server(server_path: Path) -> tuple[list[str], list[str]]:
    tree = ast.parse(server_path.read_text(encoding="utf-8"))
    tool_names: list[str] = []
    imports: list[str] = []
    for node in _toplevel_nodes(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if _is_app_attr(target, "tool"):
                    tool_names.append(node.name)
                    break
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if _is_app_attr(call.func, "tool") or _is_app_attr(call.func, "add_tool"):
                if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                    tool_names.append(call.args[0].value)
                elif call.args and isinstance(call.args[0], ast.Name):
                    tool_names.append(call.args[0].id)
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                imports.append(node.module)
    return tool_names, imports


def _local_modules(import_map: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for mod in import_map:
        top = mod.split(".")[0]
        if top in seen or top in sys.builtin_module_names:
            continue
        seen.add(top)
        out.append(top)
    return out


def load_source(group: str) -> tuple[list[tuple[str, str, Callable]], list[str]]:
    spec = SOURCES[group]
    src_dir = FD_HOME / spec["dir"]
    server_path = src_dir / "server.py"
    tool_names, import_map = _parse_server(server_path)

    sys.path.insert(0, str(src_dir))
    try:
        loaded: dict[str, Any] = {}

        if spec.get("suppress"):
            db = _load_module_unique(f"{group}__database", src_dir / "database.py")
            sched = _load_module_unique(f"{group}__scheduler", src_dir / "scheduler.py")
            sched.load_schedules = lambda *a, **k: None  # type: ignore[attr-defined]
            sched.shutdown_scheduler = lambda *a, **k: None  # type: ignore[attr-defined]
            sys.modules["database"] = db
            sys.modules["scheduler"] = sched

        if spec["inline"]:
            loaded["server"] = _load_module_unique(f"{group}__server", server_path)
        else:
            for modname in _local_modules(import_map):
                p = src_dir / f"{modname}.py"
                if p.exists():
                    loaded[modname] = _load_module_unique(f"{group}__{modname}", p)

        out: list[tuple[str, str, Callable]] = []
        missing: list[str] = []
        for name in tool_names:
            fn = None
            for mod in loaded.values():
                fn = getattr(mod, name, None)
                if callable(fn):
                    break
            if callable(fn):
                out.append((group, name, fn))
            else:
                missing.append(name)
        if missing:
            logger.warning("%s: %d tool(s) unresolvable: %s",
                           group, len(missing), ", ".join(missing[:10]))
        return out, missing
    finally:
        sys.path.pop(0)
        _evict_source_modules()


_BUILD_CACHE: list[tuple[str, str, Callable]] | None = None
_BUILD_REPORT: dict[str, list] | None = None


def _can_import(modname: str) -> bool:
    """Best-effort import check for optional-group dependency gating."""
    try:
        __import__(modname)
        return True
    except Exception:
        return False


def build() -> list[tuple[str, str, Callable]]:
    global _BUILD_CACHE, _BUILD_REPORT
    if _BUILD_CACHE is not None:
        return _BUILD_CACHE

    models_dir = REPO / "fd-daas-mcp" / "models"
    if str(models_dir) not in sys.path:
        sys.path.insert(0, str(models_dir))

    report: dict[str, list] = {"registered": [], "failed": [], "skipped_optional": []}
    all_tools: list[tuple[str, str, Callable]] = []
    for group in SOURCES:
        spec = SOURCES[group]
        # Optional groups load only when their backing dep is importable; an
        # absent dep is recorded as skipped_optional (INFO), not a failure.
        if spec.get("optional"):
            dep = spec.get("dep")
            if dep and not _can_import(dep):
                report["skipped_optional"].append((group, f"dep {dep!r} not importable"))
                logger.info("optional source %s skipped (dep %s absent)", group, dep)
                continue
        try:
            tools, missing = load_source(group)
        except Exception as e:  # noqa: BLE001
            report["failed"].append((group, "*", f"load error: {type(e).__name__}: {e}"))
            logger.warning("source %s failed to load (skipped): %s", group, e)
            continue
        for g, name, fn in tools:
            all_tools.append((g, name, fn))
            report["registered"].append((g, name))
        for name in missing:
            report["failed"].append((group, name, "unresolvable at load"))

    logger.info("registry: %d tools across %d sources (failed=%d, skipped_optional=%d)",
                len(all_tools), len(SOURCES),
                len(report["failed"]), len(report["skipped_optional"]))
    _BUILD_CACHE = all_tools
    _BUILD_REPORT = report
    return all_tools


def build_report() -> dict[str, list]:
    """Structured registration report: registered / failed / skipped_optional.

    Populated by ``build()`` (load stage) and ``note_failed()`` (server
    ``app.tool`` stage). ``registered`` lists tools that loaded; ``failed`` lists
    load-time and app.tool-registration failures as ``(group, name, error)``;
    ``skipped_optional`` lists optional groups whose dependency was absent.
    """
    if _BUILD_REPORT is None:
        build()
    return _BUILD_REPORT  # type: ignore[return-value]


def note_failed(group: str, name: str, error: str) -> None:
    """Record a tool that failed to register with the FastMCP app (server-side).

    Called from ``server.py``'s per-tool registration loop so an app.tool failure
    is surfaced in the report rather than only logged.
    """
    if _BUILD_REPORT is None:
        build()
    _BUILD_REPORT["failed"].append((group, name, f"app.tool: {error}"))


def core_groups() -> list[str]:
    """Groups that are not optional - a failure here fails the selfcheck loudly."""
    return [g for g, s in SOURCES.items() if not s.get("optional")]


def reset_cache() -> None:
    global _BUILD_CACHE, _BUILD_REPORT
    _BUILD_CACHE = None
    _BUILD_REPORT = None


def namespaced(group: str, tool_name: str) -> str:
    return f"{group}_{tool_name}"


def collisions() -> dict[str, list[str]]:
    from collections import defaultdict
    where: dict[str, list[str]] = defaultdict(list)
    for group, name, _ in build():
        where[name].append(group)
    return {n: gs for n, gs in where.items() if len(gs) > 1}


def leaf_isolation_check() -> dict[str, dict[str, str]]:
    targets = {
        "database": [("cron", "database.py"), ("workflow", "database.py")],
    }
    out: dict[str, dict[str, str]] = {}
    for leaf, specs in targets.items():
        out[leaf] = {}
        for group, fname in specs:
            src_dir = FD_HOME / SOURCES[group]["dir"]
            sys.path.insert(0, str(src_dir))
            try:
                mod = _load_module_unique(f"leaf_{group}_{leaf}", src_dir / fname)
                out[leaf][group] = getattr(mod, "__file__", "")
            except Exception:
                pass
            finally:
                sys.path.pop(0)
                _evict_source_modules()
    return out

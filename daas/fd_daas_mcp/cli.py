"""Click CLI for fd-daas-mcp - auto-generated from the same registry as the server.

Usage:
  fd-daas-mcp init [--db-url URL] [--seed|--no-seed] [--json]   # provision the DB
  fd-daas-mcp doctor [--db-url URL] [--json]                     # read-only diagnostic
  fd-daas-mcp <group> <tool> [key=value ...] [--json]           # any registered tool
  fd-daas-mcp                       # REPL mode (needs [repl] extra for history)
  fd-daas-mcp --help                # authoritative live surface (no drift)

``init`` and ``doctor`` are top-level commands registered eagerly and dispatched
BEFORE the tool registry is built (they must be runnable on a fresh install that
has no database yet). Tool subcommands are built lazily on first access so
``init``/``doctor`` don't pay the registry-build cost.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

import click

from daas.fd_daas_mcp import registry


def _parse_value(v: str) -> Any:
    try:
        return json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return v


def _print(result: Any, as_json: bool) -> None:
    if isinstance(result, (dict, list)) or as_json:
        try:
            click.echo(json.dumps(result, default=str, ensure_ascii=False, indent=2))
            return
        except (TypeError, ValueError):
            pass
    click.echo(str(result))


def _make_command(name: str, func: Any) -> click.Command:
    sig = inspect.signature(func)
    params = list(sig.parameters.values())
    help_text = ""
    if func.__doc__:
        help_text = func.__doc__.strip().split("\n\n", 1)[0][:200]

    @click.command(name=name, help=help_text or name)
    @click.argument("kv", nargs=-1)
    @click.option("--json", "as_json", is_flag=True, help="Print raw JSON output")
    def _cmd(kv, as_json):
        kwargs: dict[str, Any] = {}
        for pair in kv:
            if "=" not in pair:
                click.echo(f"error: argument {pair!r} is not key=value", err=True)
                sys.exit(2)
            k, v = pair.split("=", 1)
            kwargs[k] = _parse_value(v)
        for p in params:
            if p.name not in kwargs and p.default is inspect.Parameter.empty:
                click.echo(f"error: missing required parameter {p.name}", err=True)
                sys.exit(2)
        try:
            if inspect.iscoroutinefunction(func):
                result = asyncio.run(func(**kwargs))
            else:
                result = func(**kwargs)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            click.echo(f"error: {type(e).__name__}: {e}", err=True)
            sys.exit(1)
        _print(result, as_json)

    return _cmd


# Core catalog tables ``init``/``doctor`` verify against. Keep this aligned with
# models/models.py (DaasSource/DaasFunction/IndicatorRule/Observation/Rule/...).
# Listed by name so a missing table is named explicitly.
_CORE_TABLES = [
    "sources", "daas_functions", "daas_function_columns", "categories",
    "indicator_rules", "observations",
    "rules",
    "entity_collections", "entity_collection_items",
    "indicator_collections", "indicator_collection_items",
    "dashboards",
]


def _bootstrap_paths() -> None:
    """Make ``daas-mcp/`` (daas_database, seed_starter_catalog) and ``models/``
    importable for the init/doctor commands. Idempotent."""
    fd_home = Path(__file__).resolve().parents[2]  # fd-daas-mcp/
    for sub in ("models", "daas-mcp"):
        p = fd_home / sub
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _sqlite_path_from_url(url: str) -> str | None:
    """Best-effort filesystem path for a sqlite URL (None for :memory: / non-sqlite)."""
    if ":memory:" in url:
        return None
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "", 1)
    return None


class _LazyRegistryGroup(click.Group):
    """Click group that lazily builds + registers tool subcommands on first
    access (``--help`` / tool dispatch), so ``init`` and ``doctor`` (registered
    eagerly) can run without building the tool registry."""

    _built = False

    def _ensure_built(self) -> None:
        if self._built:
            return
        self._built = True
        for group, name, func in registry.build():
            if group not in self.commands:
                self.add_command(click.Group(name=group, help=f"{group} group"))
            self.commands[group].add_command(_make_command(name, func))

    def list_commands(self, ctx) -> list[str]:  # type: ignore[override]
        self._ensure_built()
        return super().list_commands(ctx)

    def resolve_command(self, ctx, args):  # type: ignore[override]
        # Skip the (expensive) registry build for init/doctor - they must run
        # before the tool registry is built and don't need the tool tree.
        if not (args and args[0] in ("init", "doctor")):
            self._ensure_built()
        return super().resolve_command(ctx, args)


@click.group(cls=_LazyRegistryGroup, invoke_without_command=True)
@click.option("--transport", type=click.Choice(["stdio", "http"]), default=None)
@click.option("--host", default=None)
@click.option("--port", default=None, type=int)
@click.pass_context
def cli(ctx: click.Context, transport, host, port) -> None:
    """fd-daas-mcp - the consolidated DAAS MCP CLI. Run with no subcommand for REPL, or `serve` for HTTP."""
    if ctx.invoked_subcommand is None:
        # Check if served via CLI args (e.g., `fd-daas-mcp --transport http`)
        if transport == "http" or os.environ.get("MCP_TRANSPORT") == "http":
            from daas.fd_daas_mcp.server import main as server_main
            server_main(transport=os.environ.get("MCP_TRANSPORT") or transport,
                        host=os.environ.get("MCP_HOST") or host or "127.0.0.1",
                        port=int(os.environ.get("MCP_PORT") or port or 8311))
            return
        _repl()


def _repl() -> None:
    session = None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory

        hist = Path.home() / ".cache" / "fd-daas-mcp" / "history"
        hist.parent.mkdir(parents=True, exist_ok=True)
        session = PromptSession(history=FileHistory(str(hist)))
    except ImportError:
        pass
    click.echo("fd-daas-mcp REPL. Ctrl-D to exit. `--help` lists groups/tools.")
    while True:
        try:
            line = (session.prompt("fd-daas-mcp> ") if session else input("fd-daas-mcp> ")).strip()
        except (EOFError, KeyboardInterrupt):
            click.echo("")
            break
        if not line:
            continue
        try:
            cli.main(line.split(), standalone_mode=False)
        except click.exceptions.UsageError as e:
            click.echo(str(e), err=True)
        except SystemExit:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Top-level first-run commands (registered eagerly; run before the registry is
# built). They provision / diagnose the database without needing any tool.
# ─────────────────────────────────────────────────────────────────────────────

@click.command("init", help="Provision the DAAS database: create file + full schema + optional starter seed.")
@click.option("--db-url", default=None, help="One-shot DB URL to provision (default: DAAS_DATABASE_URL or writable default).")
@click.option("--seed/--no-seed", "seed_flag", default=None, help="Force seed (--seed) or skip (--no-seed). Default: seed iff sources table is empty.")
@click.option("--json", "as_json", is_flag=True, help="Print a JSON summary.")
def init_cmd(db_url: str | None, seed_flag: bool | None, as_json: bool) -> None:
    _bootstrap_paths()
    from sqlalchemy import inspect, text

    from daas_database import provision_database  # type: ignore
    from seed_starter_catalog import seed_starter_catalog, should_seed  # type: ignore

    try:
        db, url = provision_database(db_url)
    except Exception as e:  # noqa: BLE001 - surface + exit non-zero
        msg = {"error": f"{type(e).__name__}: {e}", "db_url": db_url or "(default)"}
        if as_json:
            click.echo(json.dumps(msg, ensure_ascii=False))
        else:
            click.echo(f"error: provisioning failed: {msg['error']} (db_url={msg['db_url']})", err=True)
        sys.exit(1)

    session = db.get_session()
    try:
        sources_before = session.execute(text("SELECT COUNT(*) FROM sources")).scalar() or 0
        force = seed_flag is True
        no_seed = seed_flag is False
        do_seed = should_seed(session, force=force, no_seed=no_seed)
        seed_result = seed_starter_catalog(session) if do_seed else None
        sources_after = session.execute(text("SELECT COUNT(*) FROM sources")).scalar() or 0
        tables = inspect(db.engine).get_table_names()
        present = [t for t in _CORE_TABLES if t in tables]
        summary = {
            "db_url": url,
            "schema_tables_present": len(present),
            "schema_tables_expected": len(_CORE_TABLES),
            "schema_complete": len(present) == len(_CORE_TABLES),
            "missing_tables": [t for t in _CORE_TABLES if t not in tables],
            "sources_before": sources_before,
            "sources_after": sources_after,
            "seed": seed_result,
        }
    finally:
        session.close()

    if as_json:
        click.echo(json.dumps(summary, default=str, ensure_ascii=False, indent=2))
    else:
        click.echo(f"DAAS database: {url}")
        click.echo(f"schema: {len(present)}/{len(_CORE_TABLES)} core tables present")
        if seed_result:
            click.echo(f"seed: inserted {seed_result['inserted']}, skipped {seed_result['skipped']} "
                       f"(all enabled=False - supply creds/libs to activate)")
        else:
            click.echo("seed: skipped")
        click.echo(f"sources: {sources_after}")
        click.echo("next: point Claude Code at .mcp.json, or run `fd-daas-mcp doctor`.")
    sys.exit(0)


@click.command("doctor", help="Read-only diagnostic of the DAAS database state (no writes).")
@click.option("--db-url", default=None, help="DB URL to inspect (default: DAAS_DATABASE_URL or writable default).")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def doctor_cmd(db_url: str | None, as_json: bool) -> None:
    _bootstrap_paths()
    from sqlalchemy import create_engine, inspect, text

    from daas_database import resolve_db_url  # type: ignore

    url = resolve_db_url(db_url)
    path = _sqlite_path_from_url(url)
    is_memory = (path is None and ":memory:" in url)
    file_exists = is_memory or (path is not None and os.path.exists(path))
    diag: dict[str, Any] = {
        "db_url": url,
        "file_exists": file_exists,
        "schema_tables_expected": len(_CORE_TABLES),
    }

    # Missing sqlite file: report without opening the engine - opening would
    # CREATE the file (a write), violating doctor's read-only invariant.
    if path is not None and not file_exists:
        diag.update(
            schema_tables_present=0,
            schema_complete=False,
            missing_tables=list(_CORE_TABLES),
            row_counts={},
            optional_extras=_optional_extras(),
            healthy=False,
        )
        _print_doctor(diag, as_json)
        sys.exit(1)

    try:
        # create_engine alone does NOT create tables (only create_all does); for
        # an existing file, SELECTs do not modify it. doctor is read-only.
        engine = create_engine(url)
        tables = inspect(engine).get_table_names()
        present = [t for t in _CORE_TABLES if t in tables]
        diag["schema_tables_present"] = len(present)
        diag["schema_complete"] = len(present) == len(_CORE_TABLES)
        diag["missing_tables"] = [t for t in _CORE_TABLES if t not in tables]
        counts: dict[str, int] = {}
        with engine.connect() as conn:
            for t in ("sources", "daas_functions", "indicator_rules", "observations"):
                if t in tables:
                    counts[t] = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0
        diag["row_counts"] = counts
        diag["optional_extras"] = _optional_extras()
        diag["healthy"] = diag["schema_complete"]
    except Exception as e:  # noqa: BLE001 - report + exit non-zero
        diag["error"] = f"{type(e).__name__}: {e}"
        diag["healthy"] = False

    _print_doctor(diag, as_json)
    sys.exit(0 if diag.get("healthy") else 1)


def _optional_extras() -> dict[str, bool]:
    """Which optional extras are installed (gates tool groups like pdf)."""
    extras: dict[str, bool] = {}
    try:
        import sqlite_vec  # noqa: F401
        extras["sqlite_vec"] = True
    except ImportError:
        extras["sqlite_vec"] = False
    return extras


def _print_doctor(diag: dict[str, Any], as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(diag, default=str, ensure_ascii=False, indent=2))
        return
    click.echo(f"DAAS database: {diag.get('db_url')}")
    click.echo(f"file_exists: {diag.get('file_exists')}")
    click.echo(f"schema: {diag.get('schema_tables_present')}/{diag.get('schema_tables_expected')} core tables")
    if diag.get("missing_tables"):
        click.echo(f"missing: {', '.join(diag['missing_tables'])}")
    click.echo(f"row_counts: {diag.get('row_counts')}")
    click.echo(f"optional_extras: {diag.get('optional_extras')}")
    if "error" in diag:
        click.echo(f"error: {diag['error']}")
    if not diag.get("healthy"):
        click.echo("NOT healthy - run `fd-daas-mcp init`.")


# init/doctor are registered eagerly (cheap); tool subcommands are built lazily
# by _LazyRegistryGroup on first access.
cli.add_command(init_cmd)
cli.add_command(doctor_cmd)


def _pdf_branch(argv: list[str]) -> int:
    """In-process cron branches --pdf-ingest / --pdf-search for the optional
    pdf group (mirror daas-mcp/server.py's --run-rule). Reachable as:
    ``python -m daas.fd_daas_mcp.cli --pdf-ingest <path|url>`` /
    ``--pdf-search "<query>"``. Returns a structured error when the [pdf]
    extra is absent."""
    _pdf_dir = Path(__file__).resolve().parents[2] / "pdf-mcp"
    if str(_pdf_dir) not in sys.path:
        sys.path.insert(0, str(_pdf_dir))
    try:
        from pdf_tools import cli_ingest, cli_search  # type: ignore
    except Exception as e:  # noqa: BLE001
        click.echo(json.dumps({"error": f"pdf extra not installed: {e}. uv sync --extra pdf"}))
        return 1
    if "--pdf-ingest" in argv:
        i = argv.index("--pdf-ingest")
        if i + 1 >= len(argv):
            click.echo(json.dumps({"error": "--pdf-ingest requires a <path|url> argument"}))
            return 2
        return cli_ingest(argv[i + 1])
    if "--pdf-search" in argv:
        i = argv.index("--pdf-search")
        if i + 1 >= len(argv):
            click.echo(json.dumps({"error": "--pdf-search requires a <query> argument"}))
            return 2
        return cli_search(argv[i + 1])
    return 0


if __name__ == "__main__":
    if "--pdf-ingest" in sys.argv or "--pdf-search" in sys.argv:
        sys.exit(_pdf_branch(sys.argv[1:]))
    cli()

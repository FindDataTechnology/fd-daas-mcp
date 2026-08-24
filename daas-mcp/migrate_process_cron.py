"""One-shot migration: rewrite cron-mcp task commands from `mcp/process-mcp` to
`mcp/daas-mcp` after the process-tool relocation.

cron-mcp stores shell `command` strings as data in the `tasks` table. Any task
that drove `process-mcp --run-rule <name>` / `--run-indicator <name>` would
spawn a server.py that no longer exists after the move. This script rewrites
the `--directory` path in those commands in place.

Idempotent: re-running on already-migrated rows is a no-op (they no longer
contain `mcp/process-mcp`). `--revert` restores `mcp/daas-mcp` → `mcp/process-mcp`
for rollback.

Usage:
  uv run --directory mcp/daas-mcp python migrate_process_cron.py --dry-run
  uv run --directory mcp/daas-mcp python migrate_process_cron.py
  uv run --directory mcp/daas-mcp python migrate_process_cron.py --revert
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# make mcp/models + this dir importable when run via `uv run --directory`
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "models"))

from sqlalchemy import create_engine, text  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "daas.db"

_OLD = "mcp/process-mcp"
_NEW = "mcp/daas-mcp"


def _resolve_url(url: str) -> str:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"sqlite:///{(_REPO_ROOT / path).resolve()}"
    return url


def _default_url() -> str:
    url = os.environ.get("DAAS_DATABASE_URL")
    if url:
        return _resolve_url(url)
    return f"sqlite:///{_DEFAULT_DB_PATH}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="list affected rows, write nothing")
    ap.add_argument("--revert", action="store_true", help="restore mcp/daas-mcp → mcp/process-mcp")
    ap.add_argument("--database-url", default=None, help="override DAAS_DATABASE_URL")
    args = ap.parse_args()

    url = _resolve_url(args.database_url) if args.database_url else _default_url()
    engine = create_engine(url)

    # cron-mcp's table is `tasks` with a `command` column. Guard if absent.
    insp = engine.dialect
    from sqlalchemy import inspect

    has_tasks = inspect(engine).has_table("tasks")
    if not has_tasks:
        print(f"no `tasks` table in {url} — nothing to migrate")
        return 0

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, name, command FROM tasks")).fetchall()

    needle = _NEW if args.revert else _OLD
    replacement = _OLD if args.revert else _NEW
    affected = [(r[0], r[1], r[2]) for r in rows if r[2] and needle in r[2]]

    if not affected:
        print(f"no task rows reference {needle!r} — nothing to do")
        return 0

    label = "would rewrite" if args.dry_run else "rewrote"
    print(f"{label} {len(affected)} task row(s) ({needle!r} → {replacement!r}):")
    for tid, name, cmd in affected:
        new_cmd = cmd.replace(needle, replacement)
        print(f"  [id={tid}] {name}: {cmd}")
        print(f"        → {new_cmd}")
        if not args.dry_run:
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE tasks SET command = :c WHERE id = :id"),
                    {"c": new_cmd, "id": tid},
                )

    if args.dry_run:
        print("(dry-run; no rows written)")
    else:
        # verify
        with engine.connect() as conn:
            leftover = conn.execute(
                text("SELECT count(*) FROM tasks WHERE command LIKE :p"),
                {"p": f"%{needle}%"},
            ).scalar()
        print(f"done. rows still referencing {needle!r}: {leftover}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

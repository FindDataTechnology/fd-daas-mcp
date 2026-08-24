"""Entity-collection sync — re-evaluate rule-based entity collection membership
on a schedule, recording every add_in / remove_out in `entity_collection_changes`.

Two roles, mirroring `entity_sync.py`:

  1. Runtime entry point (what cron-mcp runs):
        uv run --directory mcp/daas-mcp python server.py --sync-entity-collection <name>
     This script's `--sync <name>` is a thin alias that calls the same
     `sync_entity_collection` service method in-process — handy for ad-hoc runs
     and `--dry-run`.

  2. Management entry point (register/unregister the schedule):
        entity_collection_sync.py --register-cron <name>
        entity_collection_sync.py --unregister-cron <name>

`--register-cron <name>` idempotently inserts a cron-mcp `Task`
(`entity-collection-sync-<name>`) + `Schedule`
(`entity-collection-sync-<name>-daily`, daily off-minute cron, timezone from
env), deduplicating on the names. The schedule takes effect on the next
cron-mcp start (cron-mcp loads schedules via load_schedules() at startup).

Usage:
    entity_collection_sync.py --sync sse-stocks
    entity_collection_sync.py --sync sse-stocks --dry-run
    entity_collection_sync.py --register-cron sse-stocks
    entity_collection_sync.py --unregister-cron sse-stocks
    entity_collection_sync.py --db-url sqlite:///x.db
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent.parent  # mcp/daas-mcp/ → mcp/ → repo root
try:
    load_dotenv(_REPO_ROOT / ".env")
    load_dotenv(_THIS.parent / ".env", override=True)
except ImportError:
    pass

sys.path.insert(0, str(_THIS.parent))

from sqlalchemy import text  # noqa: E402

from daas_database import Database  # noqa: E402
from registry_service import EntityCollectionService  # noqa: E402
from models import Schedule, Task  # noqa: E402

# Daily, 04:23 local-ish (off the :00 mark so the fleet doesn't sync at once).
# Editable after registration via the dashboard /cron page.
CRON_EXPR = "23 4 * * *"


def _task_name(collection_name: str) -> str:
    return f"entity-collection-sync-{collection_name}"


def _schedule_name(collection_name: str) -> str:
    return f"entity-collection-sync-{collection_name}-daily"


def _task_command(collection_name: str) -> str:
    return (
        f"uv run --directory mcp/daas-mcp python server.py "
        f"--sync-entity-collection {collection_name}"
    )


def sync_once(session, collection_name: str, dry_run: bool) -> int:
    """Run `sync_entity_collection(name)` in-process and print the summary."""
    svc = EntityCollectionService(session)
    if dry_run:
        # Dry-run: report the rule + current member count without writing.
        from models import EntityCollection, EntityCollectionItem
        coll = (
            session.query(EntityCollection)
            .filter(EntityCollection.name == collection_name)
            .first()
        )
        if coll is None:
            print(json.dumps({"error": f"collection '{collection_name}' not found"}))
            return 1
        current = (
            session.query(EntityCollectionItem)
            .filter(EntityCollectionItem.collection_id == coll.id)
            .count()
        )
        if coll.rule_json:
            intended = len(svc._rule_entity_ids(coll.rule_json))
            rule_kind = "json"
        elif coll.rule_script:
            intended = len(svc._script_entity_ids(coll.rule_script))
            rule_kind = "script"
        else:
            intended = None
            rule_kind = None
        has_rule = bool(coll.rule_json or coll.rule_script)
        print(json.dumps({
            "name": coll.name,
            "rule_kind": rule_kind,
            "rule": coll.rule_json,
            "rule_script": coll.rule_script,
            "current_members": current,
            "intended_members": intended,
            "dry_run": True,
            "note": "no writes performed" if has_rule else "manual collection (sync is a no-op)",
        }, ensure_ascii=False))
        return 0
    result = svc.sync_entity_collection(collection_name)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if "error" not in result else 1


def register_cron(session, collection_name: str) -> int:
    """Idempotently insert a cron-mcp Task + Schedule for the collection."""
    task_name = _task_name(collection_name)
    sched_name = _schedule_name(collection_name)
    print(f"Registering cron task + schedule for collection '{collection_name}'...")
    task = session.query(Task).filter(Task.name == task_name).first()
    if task is None:
        task = Task(
            name=task_name,
            description=f"Daily sync of entity collection '{collection_name}' "
            f"(rule-based membership re-evaluation → add_in/remove_out).",
            command=_task_command(collection_name),
            timeout=600,
        )
        session.add(task)
        session.commit()
        print(f"  created task '{task_name}' (id={task.id})")
    else:
        print(f"  task '{task_name}' already exists (id={task.id}) — left unchanged")

    sched = session.query(Schedule).filter(Schedule.name == sched_name).first()
    if sched is None:
        sched = Schedule(
            name=sched_name,
            cron_expr=CRON_EXPR,
            task_name=task_name,
            timezone=os.environ.get("CRON_TIMEZONE", "UTC"),
            enabled=1,
        )
        session.add(sched)
        session.commit()
        print(f"  created schedule '{sched_name}' (cron='{CRON_EXPR}', tz={sched.timezone})")
    else:
        print(f"  schedule '{sched_name}' already exists — left unchanged")

    print(
        "  NOTE: the schedule takes effect on the next cron-mcp start "
        "(cron-mcp loads schedules via load_schedules() at startup)."
    )
    return 0


def unregister_cron(session, collection_name: str) -> int:
    """Delete the cron-mcp Task + Schedule for the collection."""
    task_name = _task_name(collection_name)
    sched_name = _schedule_name(collection_name)
    deleted_schedules = 0
    sched = session.query(Schedule).filter(Schedule.name == sched_name).first()
    if sched is not None:
        session.delete(sched)
        deleted_schedules += 1
    task = session.query(Task).filter(Task.name == task_name).first()
    if task is not None:
        session.delete(task)
    session.commit()
    print(
        f"Unregistered cron for '{collection_name}': "
        f"{deleted_schedules} schedule(s) + {'1' if task else '0'} task(s) removed."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sync / schedule entity collections.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--sync", metavar="NAME", help="run sync_entity_collection(NAME) once, in-process")
    g.add_argument("--register-cron", metavar="NAME", help="install daily sync task+schedule for NAME")
    g.add_argument("--unregister-cron", metavar="NAME", help="remove the sync task+schedule for NAME")
    p.add_argument("--dry-run", action="store_true", help="with --sync: print the plan, perform no writes")
    p.add_argument("--db-url", help="override DAAS_DATABASE_URL for this run")
    args = p.parse_args(argv)

    if args.db_url:
        os.environ["DAAS_DATABASE_URL"] = args.db_url
        Database._instance = None  # reset singleton so override takes effect

    db = Database()
    session = db.get_session()

    if args.sync:
        return sync_once(session, args.sync, args.dry_run)
    if args.register_cron:
        return register_cron(session, args.register_cron)
    if args.unregister_cron:
        return unregister_cron(session, args.unregister_cron)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

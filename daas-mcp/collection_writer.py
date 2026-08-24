"""Thin CLI sidecar used by the dashboard's Next.js API routes to write to
`daas.db`. One process per call — slow but simple, and writes are infrequent.

Usage:
  python collection_writer.py <command> --json '{"...": "..."}'

Commands:
  create        : {"name": "...", "description": "..."}
  rename        : {"old_name": "...", "new_name": "..."}
  update        : {"name": "...", "new_name"?: "...", "description"?: "..."}
  delete        : {"name": "..."}
  add-item      : {"collection_name": "...", "source_name": "...", "section_name": "..." | null, "score"?: float}
  remove-item   : {"collection_name": "...", "source_name": "...", "section_name": "..." | null}
  reorder       : {"collection_name": "...", "ordered_item_ids": [int, ...]}
  set-source-score : {"name": "...", "score": float | null}
  set-item-score   : {"collection_name": "...", "source_name": "...", "section_name": "..." | null, "score": float | null}
  create-entity-collection  : {"name": "...", "description"?: "...", "rule"?: "...json string...", "rule_script"?: "...path (repo-root relative)..."}
  update-entity-collection  : {"name": "...", "new_name"?: "...", "description"?: "...", "rule"?: "...", "rule_script"?: "...path...", "clear_rule"?: bool}
  delete-entity-collection  : {"name": "..."}
  add-entity-item           : {"collection_name": "...", "entity_id"?: int, "entity_type"?: str, "code"?: str, "reason"?: str}
  remove-entity-item        : {"collection_name": "...", "entity_id"?: int, "entity_type"?: str, "code"?: str, "reason"?: str}
  reorder-entity-items      : {"collection_name": "...", "ordered_item_ids": [int, ...]}
  sync-entity-collection    : {"name": "..."}
  set-indicator-score            : {"name": "...", "score": float | null}
  create-indicator-collection    : {"name": "...", "description"?: "..."}
  update-indicator-collection    : {"name": "...", "new_name"?: "...", "description"?: "..."}
  delete-indicator-collection    : {"name": "..."}
  add-indicator-item             : {"collection_name": "...", "indicator_name": "...", "score"?: float, "reason"?: str}
  remove-indicator-item          : {"collection_name": "...", "indicator_name": "...", "reason"?: str}
  reorder-indicator-items        : {"collection_name": "...", "ordered_item_ids": [int, ...]}
  set-indicator-collection-item-score : {"collection_name": "...", "indicator_name": "...", "score": float | null}

Output: one JSON line on stdout. On error, exit code != 0 and `{"error": "..."}`
on stderr (also mirrored to stdout for callers that don't separate streams).
ponytail: single-file CLI, no click, no fancy dispatch.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make sibling modules importable when invoked via `uv run ... python collection_writer.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load .env exactly like server.py does so DAAS_DATABASE_URL is set.
# The repo-root .env (daas/.env) is where DAAS_DATABASE_URL is
# actually defined; parent.parent is mcp/ (whose .env doesn't exist), so use
# parents[2] to reach the repo root. Per-MCP .env still overrides.
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]  # daas/
load_dotenv(REPO_ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=True)

from daas_database import get_database  # noqa: E402
from registry_service import (  # noqa: E402
    EntityCollectionService,
    IndicatorCollectionService,
    RegistryService,
)


def _service() -> RegistryService:
    return RegistryService(get_database().get_session())


def _ec_service() -> EntityCollectionService:
    return EntityCollectionService(get_database().get_session())


def _ic_service() -> IndicatorCollectionService:
    return IndicatorCollectionService(get_database().get_session())


def _fail(msg: str) -> None:
    payload = json.dumps({"error": msg})
    print(payload, file=sys.stderr)
    print(payload)  # mirror for naive callers
    sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "create",
            "rename",
            "update",
            "delete",
            "add-item",
            "remove-item",
            "reorder",
            "set-source-score",
            "set-item-score",
            "create-entity-collection",
            "update-entity-collection",
            "delete-entity-collection",
            "add-entity-item",
            "remove-entity-item",
            "reorder-entity-items",
            "sync-entity-collection",
            "set-indicator-score",
            "create-indicator-collection",
            "update-indicator-collection",
            "delete-indicator-collection",
            "add-indicator-item",
            "remove-indicator-item",
            "reorder-indicator-items",
            "set-indicator-collection-item-score",
        ],
    )
    parser.add_argument(
        "--json",
        dest="json_args",
        default="{}",
        help="JSON object of arguments for the command",
    )
    ns = parser.parse_args(argv)

    try:
        args = json.loads(ns.json_args)
    except json.JSONDecodeError as e:
        _fail(f"Invalid --json payload: {e}")
        return 1

    # entity-collection subcommands dispatch to EntityCollectionService; the
    # rest to the datasource-collection RegistryService.
    ec_commands = {
        "create-entity-collection",
        "update-entity-collection",
        "delete-entity-collection",
        "add-entity-item",
        "remove-entity-item",
        "reorder-entity-items",
        "sync-entity-collection",
    }
    ic_commands = {
        "create-indicator-collection",
        "update-indicator-collection",
        "delete-indicator-collection",
        "add-indicator-item",
        "remove-indicator-item",
        "reorder-indicator-items",
        "set-indicator-collection-item-score",
    }
    if ns.command in ec_commands:
        svc = _ec_service()
    elif ns.command in ic_commands:
        svc = _ic_service()
    elif ns.command == "set-indicator-score":
        svc = None  # dispatched to ProcessDatabase below
    else:
        svc = _service()
    try:
        if ns.command == "create":
            out = svc.create_collection(
                name=args["name"],
                description=args.get("description"),
            )
        elif ns.command == "rename":
            out = svc.rename_collection(
                old_name=args["old_name"],
                new_name=args["new_name"],
            )
        elif ns.command == "update":
            out = svc.update_collection(
                name=args["name"],
                new_name=args.get("new_name"),
                description=args.get("description"),
            )
        elif ns.command == "delete":
            out = svc.delete_collection(name=args["name"])
        elif ns.command == "add-item":
            out = svc.add_to_collection(
                collection_name=args["collection_name"],
                source_name=args["source_name"],
                section_name=args.get("section_name"),
                score=args.get("score"),
            )
        elif ns.command == "remove-item":
            out = svc.remove_from_collection(
                collection_name=args["collection_name"],
                source_name=args["source_name"],
                section_name=args.get("section_name"),
            )
        elif ns.command == "reorder":
            out = svc.reorder_collection_items(
                collection_name=args["collection_name"],
                ordered_item_ids=list(args["ordered_item_ids"]),
            )
        elif ns.command == "set-source-score":
            # score=null (or omitted) clears the default score.
            score = args.get("score")
            out = svc.update_datasource(
                name=args["name"],
                score=score,
                clear_score=(score is None),
            )
        elif ns.command == "set-item-score":
            # score=null (or omitted) clears the per-item override.
            out = svc.set_collection_item_score(
                collection_name=args["collection_name"],
                source_name=args["source_name"],
                section_name=args.get("section_name"),
                score=args.get("score"),
            )
        elif ns.command == "create-entity-collection":
            rule = args.get("rule")
            out = svc.create_entity_collection(
                name=args["name"],
                description=args.get("description"),
                rule=json.loads(rule) if rule else None,
                rule_script=args.get("rule_script"),
            )
        elif ns.command == "update-entity-collection":
            rule = args.get("rule")
            out = svc.update_entity_collection(
                name=args["name"],
                new_name=args.get("new_name"),
                description=args.get("description"),
                rule=json.loads(rule) if rule else None,
                clear_rule=bool(args.get("clear_rule", False)),
                rule_script=args.get("rule_script"),
            )
        elif ns.command == "delete-entity-collection":
            out = svc.delete_entity_collection(name=args["name"])
        elif ns.command == "add-entity-item":
            out = svc.add_entity_to_collection(
                collection_name=args["collection_name"],
                entity_id=args.get("entity_id"),
                entity_type=args.get("entity_type"),
                code=args.get("code"),
                reason=args.get("reason"),
            )
        elif ns.command == "remove-entity-item":
            out = svc.remove_entity_from_collection(
                collection_name=args["collection_name"],
                entity_id=args.get("entity_id"),
                entity_type=args.get("entity_type"),
                code=args.get("code"),
                reason=args.get("reason"),
            )
        elif ns.command == "reorder-entity-items":
            out = svc.reorder_entity_collection_items(
                collection_name=args["collection_name"],
                ordered_item_ids=list(args["ordered_item_ids"]),
            )
        elif ns.command == "sync-entity-collection":
            out = svc.sync_entity_collection(name=args["name"])
        elif ns.command == "set-indicator-score":
            # Dispatched to ProcessDatabase (indicator default score, not a
            # collection concept). score=null (or omitted) clears → inherit
            # the datasource's sources.score.
            from process_database import get_db as get_process_db

            out = get_process_db().set_indicator_score(
                name=args["name"],
                score=args.get("score"),
            )
        elif ns.command == "create-indicator-collection":
            out = svc.create_indicator_collection(
                name=args["name"],
                description=args.get("description"),
            )
        elif ns.command == "update-indicator-collection":
            out = svc.update_indicator_collection(
                name=args["name"],
                new_name=args.get("new_name"),
                description=args.get("description"),
            )
        elif ns.command == "delete-indicator-collection":
            out = svc.delete_indicator_collection(name=args["name"])
        elif ns.command == "add-indicator-item":
            out = svc.add_indicator_to_collection(
                collection_name=args["collection_name"],
                indicator_name=args["indicator_name"],
                score=args.get("score"),
                reason=args.get("reason"),
            )
        elif ns.command == "remove-indicator-item":
            out = svc.remove_indicator_from_collection(
                collection_name=args["collection_name"],
                indicator_name=args["indicator_name"],
                reason=args.get("reason"),
            )
        elif ns.command == "reorder-indicator-items":
            out = svc.reorder_indicator_collection_items(
                collection_name=args["collection_name"],
                ordered_item_ids=list(args["ordered_item_ids"]),
            )
        elif ns.command == "set-indicator-collection-item-score":
            # score=null (or omitted) clears the per-item override.
            out = svc.set_indicator_collection_item_score(
                collection_name=args["collection_name"],
                indicator_name=args["indicator_name"],
                score=args.get("score"),
            )
        else:
            _fail(f"Unknown command: {ns.command}")
            return 1
    except KeyError as e:
        _fail(f"Missing required arg: {e}")
        return 1
    except Exception as e:
        _fail(f"{type(e).__name__}: {e}")
        return 1

    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

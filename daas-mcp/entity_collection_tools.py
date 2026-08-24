"""Entity-collection tools for daas-mcp — named collections of entities
(stocks + countries) with add-in / remove-out audit logging and rule-based
sync.

Thin wrappers over EntityCollectionService, mirroring entity_tools.py. The
heavy lifting (membership diff, change recording, rule evaluation) lives in
EntityCollectionService so it shares one session with the rest of the daas
domain and is reused by collection_writer.py (the dashboard write sidecar).
"""
from __future__ import annotations

import json
from typing import Optional

from daas_database import get_database
from registry_service import EntityCollectionService


def _svc() -> EntityCollectionService:
    return EntityCollectionService(get_database().get_session())


def _ok(result: dict) -> dict:
    return result


def _err(e: Exception) -> dict:
    return {"success": False, "error": str(e)}


def create_entity_collection(
    name: str,
    description: Optional[str] = None,
    rule: Optional[str] = None,
    rule_script: Optional[str] = None,
    rule_id: Optional[int] = None,
) -> dict:
    """Create a named entity collection (watchlist / portfolio).

    Args:
        name: Unique collection name.
        description: Optional description.
        rule: Optional JSON object string encoding the declarative membership
            rule (`entity_type`, `exchange`, `country_code`, `codes`,
            `name_regex`). When set, `sync_entity_collection` re-derives
            members by applying the rule to the `entities` table.
        rule_script: Optional path (repo-root relative, e.g.
            `mcp/daas-mcp/rules/entity_collections/x.py`) to a Python rule
            script defining `members(ctx)`. Alternative to `rule`; mutually
            exclusive. When set, `sync_entity_collection` executes the script
            (which can read any daas.db table via `ctx.query(sql)`) and diffs
            its result. The path is stored in the DB so the rule can be
            re-run from a workflow or cron without re-passing the script.
    """
    svc = _svc()
    rule_obj = json.loads(rule) if rule else None
    if sum(x is not None for x in (rule, rule_script, rule_id)) > 1:
        return {"error": "a collection may have at most one of rule_id, rule, rule_script"}
    try:
        return _ok(
            svc.create_entity_collection(
                name,
                description=description,
                rule=rule_obj,
                rule_script=rule_script,
                rule_id=rule_id,
            )
        )
    except Exception as e:
        return _err(e)


def list_entity_collections() -> dict:
    """List every entity collection with its item count."""
    svc = _svc()
    try:
        return _ok({"collections": svc.list_entity_collections()})
    except Exception as e:
        return _err(e)


def get_entity_collection(name: str) -> dict:
    """Get one collection with its current member entities (ordered)."""
    svc = _svc()
    try:
        return _ok(svc.get_entity_collection(name))
    except Exception as e:
        return _err(e)


def update_entity_collection(
    name: str,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    rule: Optional[str] = None,
    clear_rule: bool = False,
    rule_script: Optional[str] = None,
    rule_id: Optional[int] = None,
) -> dict:
    """Partially update an entity collection's name and/or description and/or
    rule. At least one field must be provided. `clear_rule=True` resets the
    rule to NULL (manual collection).

    Args:
        rule: JSON object string for the declarative rule. Setting it clears
            any existing `rule_script` (mutually exclusive).
        rule_script: Path to a Python rule script. Setting it clears any
            existing declarative `rule` (mutually exclusive).
        clear_rule: Reset both `rule` and `rule_script` to NULL.
    """
    svc = _svc()
    rule_obj = json.loads(rule) if rule else None
    if sum(x is not None for x in (rule, rule_script, rule_id)) > 1:
        return {"error": "a collection may have at most one of rule_id, rule, rule_script"}
    try:
        return _ok(
            svc.update_entity_collection(
                name,
                new_name=new_name,
                description=description,
                rule=rule_obj,
                clear_rule=clear_rule,
                rule_script=rule_script,
                rule_id=rule_id,
            )
        )
    except Exception as e:
        return _err(e)


def delete_entity_collection(name: str) -> dict:
    """Delete a collection and cascade to its items + changes."""
    svc = _svc()
    try:
        return _ok(svc.delete_entity_collection(name))
    except Exception as e:
        return _err(e)


def add_entity_to_collection(
    collection_name: str,
    entity_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    code: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict:
    """Add an entity to a collection. Resolves the entity by `entity_id`, or
    by `(entity_type, code)`. Records an `add_in` event in
    `entity_collection_changes` (source='manual'). No-op (no event recorded)
    if the entity is already a member.

    Args:
        collection_name: The collection name.
        entity_id: The entity id (alternative to entity_type+code).
        entity_type: 'stock' or 'country' (use with `code`).
        code: The entity's canonical code (use with `entity_type`).
        reason: Optional reason recorded with the add_in event.
    """
    svc = _svc()
    try:
        return _ok(
            svc.add_entity_to_collection(
                collection_name,
                entity_id=entity_id,
                entity_type=entity_type,
                code=code,
                reason=reason,
            )
        )
    except Exception as e:
        return _err(e)


def remove_entity_from_collection(
    collection_name: str,
    entity_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    code: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict:
    """Remove an entity from a collection. Records a `remove_out` event in
    `entity_collection_changes` (source='manual'). No-op (no event recorded)
    if the entity is not a member."""
    svc = _svc()
    try:
        return _ok(
            svc.remove_entity_from_collection(
                collection_name,
                entity_id=entity_id,
                entity_type=entity_type,
                code=code,
                reason=reason,
            )
        )
    except Exception as e:
        return _err(e)


def list_entity_collection_items(collection_name: str) -> dict:
    """List the current members of a collection, ordered by sort_order, each
    enriched with full entity detail (code, name, ticker, exchange, ...)."""
    svc = _svc()
    try:
        return _ok(svc.list_entity_collection_items(collection_name))
    except Exception as e:
        return _err(e)


def reorder_entity_collection_items(
    collection_name: str, ordered_item_ids: list[int]
) -> dict:
    """Rewrite member sort_order to match the given ordered list of
    `entity_collection_items.id`. Must contain exactly the current item ids."""
    svc = _svc()
    try:
        return _ok(
            svc.reorder_entity_collection_items(collection_name, ordered_item_ids)
        )
    except Exception as e:
        return _err(e)


def list_entity_collection_changes(
    collection_name: Optional[str] = None,
    entity_id: Optional[int] = None,
    action: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Query the add-in / remove-out audit log (`entity_collection_changes`),
    newest first, each enriched with collection name + entity code/name.

    Args:
        collection_name: Filter to one collection.
        entity_id: Filter to one entity (across all collections).
        action: 'add_in' or 'remove_out'.
        source: 'manual' or 'cron'.
        limit: Page size (default 100, max 500).
        offset: Page offset.
    """
    svc = _svc()
    try:
        return _ok(
            svc.list_entity_collection_changes(
                collection_name=collection_name,
                entity_id=entity_id,
                action=action,
                source=source,
                limit=limit,
                offset=offset,
            )
        )
    except Exception as e:
        return _err(e)


def sync_entity_collection(name: str) -> dict:
    """Re-derive the member set for a rule-based collection by applying its
    `rule_json` to the `entities` table, diff vs current members, apply
    add_in for new matches and remove_out for non-matches (source='cron'),
    and record every transition. Returns `{added, removed, unchanged}`.
    A manual collection (rule_json=NULL) is a no-op.
    """
    svc = _svc()
    try:
        return _ok(svc.sync_entity_collection(name))
    except Exception as e:
        return _err(e)


def cli_sync_entity_collection(name: str) -> int:
    """CLI entry: run `sync_entity_collection(name)` in-process, print a JSON
    summary, return an exit code. For cron-mcp shell tasks
    (`python server.py --sync-entity-collection <name>`)."""
    result = sync_entity_collection(name)
    print(json.dumps(result))
    return 0 if not result.get("error") else 1

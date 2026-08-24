"""Indicator-collection tools for daas-mcp — named collections of indicators
with per-item score overrides and add-in/remove-out audit logging.

Thin wrappers over IndicatorCollectionService, mirroring entity_collection_tools.py.
The heavy lifting (3-level score resolution, membership diff, change recording)
lives in IndicatorCollectionService so it shares one session with the rest of
the daas domain and is reused by collection_writer.py (the dashboard write
sidecar).

Effective score for a member = `COALESCE(item.score, indicator_rules.score,
sources.score)` — item override → indicator default → datasource default.
"""
from __future__ import annotations

import json
from typing import Optional

from daas_database import get_database
from registry_service import IndicatorCollectionService


def _svc() -> IndicatorCollectionService:
    return IndicatorCollectionService(get_database().get_session())


def _ok(result: dict) -> dict:
    return result


def _err(e: Exception) -> dict:
    return {"success": False, "error": str(e)}


def create_indicator_collection(
    name: str,
    description: Optional[str] = None,
    rule_id: Optional[int] = None,
) -> dict:
    """Create a named indicator collection (a reusable bundle of indicators).

    Args:
        name: Unique collection name.
        description: Optional description.
        rule_id: Optional id of a `rules` row (target='indicator_names') whose
            evaluation drives membership via `sync_indicator_collection`.
    """
    svc = _svc()
    try:
        return _ok(svc.create_indicator_collection(name, description=description, rule_id=rule_id))
    except Exception as e:
        return _err(e)


def list_indicator_collections() -> dict:
    """List every indicator collection with its item count."""
    svc = _svc()
    try:
        return _ok({"collections": svc.list_indicator_collections()})
    except Exception as e:
        return _err(e)


def get_indicator_collection(name: str) -> dict:
    """Get one collection with its current items (ordered, each with the
    resolved effective score + raw item/indicator/datasource scores)."""
    svc = _svc()
    try:
        return _ok(svc.get_indicator_collection(name))
    except Exception as e:
        return _err(e)


def update_indicator_collection(
    name: str,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    rule_id: Optional[int] = None,
    clear_rule: bool = False,
) -> dict:
    """Partially update an indicator collection's name, description, and/or
    rule. At least one field must be provided. `clear_rule=True` resets
    `rule_id` to NULL (manual collection)."""
    svc = _svc()
    try:
        return _ok(
            svc.update_indicator_collection(
                name,
                new_name=new_name,
                description=description,
                rule_id=rule_id,
                clear_rule=clear_rule,
            )
        )
    except Exception as e:
        return _err(e)


def delete_indicator_collection(name: str) -> dict:
    """Delete a collection and cascade to its items + changes."""
    svc = _svc()
    try:
        return _ok(svc.delete_indicator_collection(name))
    except Exception as e:
        return _err(e)


def add_indicator_to_collection(
    collection_name: str,
    indicator_name: str,
    score: Optional[float] = None,
    reason: Optional[str] = None,
) -> dict:
    """Add an indicator to a collection. Resolves the indicator by `name`.

    Args:
        collection_name: The collection to add to.
        indicator_name: The indicator rule name (indicator_rules.name) to add.
        score: Optional per-collection score override (float); NULL = inherit
            the indicator's default `indicator_rules.score`.
        reason: Optional reason recorded in the audit log.
    """
    svc = _svc()
    try:
        return _ok(
            svc.add_indicator_to_collection(
                collection_name, indicator_name, score=score, reason=reason
            )
        )
    except Exception as e:
        return _err(e)


def remove_indicator_from_collection(
    collection_name: str,
    indicator_name: str,
    reason: Optional[str] = None,
) -> dict:
    """Remove an indicator from a collection. Records a `remove_out` event in
    the audit log. No-op (`not_member`) if the indicator is not in the collection."""
    svc = _svc()
    try:
        return _ok(
            svc.remove_indicator_from_collection(
                collection_name, indicator_name, reason=reason
            )
        )
    except Exception as e:
        return _err(e)


def list_indicator_collection_items(collection_name: str) -> dict:
    """List the items of a collection, ordered by sort_order, each with the
    resolved effective score + raw item/indicator/datasource scores."""
    svc = _svc()
    try:
        return _ok(svc.list_indicator_collection_items(collection_name))
    except Exception as e:
        return _err(e)


def reorder_indicator_collection_items(
    collection_name: str, ordered_item_ids: list[int]
) -> dict:
    """Rewrite item sort_order to match the given ordered list of
    `indicator_collection_items.id`. Must contain exactly the current item
    ids of this collection — partial reorders are rejected."""
    svc = _svc()
    try:
        return _ok(svc.reorder_indicator_collection_items(collection_name, ordered_item_ids))
    except Exception as e:
        return _err(e)


def set_indicator_collection_item_score(
    collection_name: str,
    indicator_name: str,
    score: Optional[float] = None,
) -> dict:
    """Set or clear the per-collection score override on an existing item,
    identified by (collection_name, indicator_name).

    `score` = a float sets the override; `score = null` clears it (the item
    then inherits the indicator's default `indicator_rules.score`, which
    itself inherits the datasource default when NULL). Returns the updated
    item dict with the resolved effective score.
    """
    svc = _svc()
    try:
        return _ok(
            svc.set_indicator_collection_item_score(
                collection_name, indicator_name, score=score
            )
        )
    except Exception as e:
        return _err(e)


def list_indicator_collection_changes(
    collection_name: Optional[str] = None,
    action: Optional[str] = None,
    source: Optional[str] = None,
    indicator_name: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Query the add-in / remove-out audit log (`indicator_collection_changes`),
    newest first. Each row is enriched with the collection name.

    Args:
        collection_name: Filter to one collection.
        action: Filter to 'add_in' or 'remove_out'.
        source: Filter to 'manual' or 'cron'.
        indicator_name: Filter to one indicator (by name).
        limit: Page size (1-500, default 100).
        offset: Page offset.
    """
    svc = _svc()
    try:
        return _ok(
            svc.list_indicator_collection_changes(
                collection_name=collection_name,
                action=action,
                source=source,
                indicator_name=indicator_name,
                limit=limit,
                offset=offset,
            )
        )
    except Exception as e:
        return _err(e)


def sync_indicator_collection(name: str) -> dict:
    """Re-derive the member set for a rule-based indicator collection by
    evaluating its rule (target='indicator_names') via the RuleEngine, diff vs
    current members, apply add_in/remove_out (source='cron'), and record every
    transition. Returns `{added, removed, unchanged}`. A manual collection
    (rule_id NULL) is a no-op.
    """
    svc = _svc()
    try:
        return _ok(svc.sync_indicator_collection(name))
    except Exception as e:
        return _err(e)


def cli_sync_indicator_collection(name: str) -> int:
    """CLI entry: run `sync_indicator_collection(name)` in-process, print a JSON
    summary, return an exit code. For cron-mcp shell tasks
    (`python server.py --sync-indicator-collection <name>`)."""
    result = sync_indicator_collection(name)
    print(json.dumps(result))
    return 0 if not result.get("error") else 1

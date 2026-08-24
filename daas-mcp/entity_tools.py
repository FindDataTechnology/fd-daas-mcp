"""Entity tools for daas-mcp — search/get/list entities, and link them to
daas datasources to answer "what data can I get for this entity".

Thin wrappers over RegistryService, mirroring daas_tools.py. The heavy
lifting (coverage resolution, identifier substitution, column aggregation)
lives in RegistryService so it shares one session with the rest of the
daas domain.
"""
from __future__ import annotations

import json
from typing import Optional

from daas_database import get_database
from registry_service import RegistryService


def _get_service() -> RegistryService:
    db = get_database()
    return RegistryService(db.get_session())


def _ok(result: dict) -> dict:
    return result


def _err(e: Exception) -> dict:
    return {"success": False, "error": str(e)}


def search_entities(
    query: str, entity_type: Optional[str] = None, limit: int = 20
) -> dict:
    """Search entities (stocks + countries) by name, ticker, code, or alias.

    Args:
        query: Substring to match (case-insensitive) against name, ticker,
            code, and the aliases list.
        entity_type: Optional filter — 'stock' or 'country'.
        limit: Max results (default 20, max 100).
    """
    svc = _get_service()
    try:
        results = svc.search_entities(query, entity_type=entity_type, limit=limit)
        return {"entities": results, "count": len(results)}
    except Exception as e:
        return _err(e)


def get_entity(
    entity_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    code: Optional[str] = None,
) -> dict:
    """Get full detail for one entity, including aliases and metadata.

    Per D5/3.5 the entity master is fd-open-data-mcp. Resolve by natural
    key (entity_type, code) when given (gateway-first, local fallback);
    otherwise fall back to entity_id (local).

    Args:
        entity_id: The entity id (alternative to entity_type+code).
        entity_type: 'stock' or 'country' (use with `code`).
        code: The entity's canonical code (use with `entity_type`).
    """
    svc = _get_service()
    try:
        e = svc.get_entity(entity_id=entity_id, entity_type=entity_type, code=code)
        if e is None:
            key = f"{entity_type}/{code}" if (entity_type and code) else f"id {entity_id}"
            return {"success": False, "error": f"entity {key} not found"}
        return _ok({"entity": e})
    except Exception as e:
        return _err(e)


def list_entities(
    entity_type: Optional[str] = None,
    exchange: Optional[str] = None,
    country_code: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List entities filtered by type / exchange / country, paginated.

    Args:
        entity_type: 'stock' or 'country'.
        exchange: e.g. 'SSE', 'NASDAQ', 'HKEX'.
        country_code: ISO 3166-1 alpha-2, e.g. 'CN', 'US'.
        limit: Page size (default 100, max 500).
        offset: Page offset.
    """
    svc = _get_service()
    try:
        return _ok(svc.list_entities(
            entity_type=entity_type,
            exchange=exchange,
            country_code=country_code,
            limit=limit,
            offset=offset,
        ))
    except Exception as e:
        return _err(e)


def get_entity_coverage(
    entity_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    code: Optional[str] = None,
) -> dict:
    """For each datasource linked to the entity, return the identifier to
    use, the available sections (routing instructions = how to get the
    data, with an identifier-prefilled variant), and the column count/list
    aggregated from daas_function_columns. Sources without registered
    functions get a column_hint naming the sibling MCP + tool.

    Per D5/3.5 entity resolution proxies through fd-open-data-mcp. Accept
    a natural key (entity_type, code) as an alternative to entity_id.

    This answers: "I have company X — which datasources cover it, how many
    columns can I get, and how do I fetch it?"
    """
    svc = _get_service()
    try:
        return _ok(
            svc.get_entity_coverage(
                entity_id=entity_id, entity_type=entity_type, code=code
            )
        )
    except Exception as e:
        return _err(e)


def link_entity_datasource(
    entity_id: int,
    source_name: str,
    identifier_in_source: Optional[str] = None,
    coverage: str = "full",
    metadata: Optional[str] = None,
) -> dict:
    """Link an entity to a daas datasource, recording the identifier to use
    inside that datasource (e.g. for AAPL → yfinance: 'AAPL'). Upserts on
    (entity_id, source_name).

    Args:
        entity_id: The entity id.
        source_name: The daas datasource name (e.g. 'edgar', 'yfinance').
        identifier_in_source: The value to plug into the datasource's lookup tool.
        coverage: 'full' | 'partial' | 'none' (default 'full').
        metadata: Optional JSON object string of extra link metadata.
    """
    svc = _get_service()
    md = json.loads(metadata) if metadata else None
    try:
        return _ok(svc.link_entity_datasource(
            entity_id,
            source_name,
            identifier_in_source=identifier_in_source,
            coverage=coverage,
            metadata=md,
        ))
    except Exception as e:
        return _err(e)


def unlink_entity_datasource(entity_id: int, source_name: str) -> dict:
    """Delete the link between an entity and a datasource."""
    svc = _get_service()
    try:
        return _ok(svc.unlink_entity_datasource(entity_id, source_name))
    except Exception as e:
        return _err(e)

"""
MCP tools for daas-mcp — search, detail, fetch, list_sources, list_categories.

These are plain functions decorated with FastMCP @tool.
"""
from __future__ import annotations

import json
from typing import Optional

from daas_database import get_database
from registry_service import RegistryService


def _get_service() -> RegistryService:
    db = get_database()
    return RegistryService(db.get_session())


def list_sources() -> dict:
    """List all configured DAAS data sources with function counts.

    Returns sources like akshare, worldbank, ckan, cnstats
    with install status and available function counts.
    """
    svc = _get_service()
    sources = svc.list_sources()
    return {"sources": sources}


def search_functions(query: str, source: Optional[str] = None, limit: int = 20) -> dict:
    """Search DAAS functions by name, category, or description across all sources.

    Args:
        query: Search term — matches function name, category, and description.
        source: Optional source name filter (akshare, worldbank, ckan, cnstats).
        limit: Maximum results to return (default 20, max 100).
    """
    svc = _get_service()
    limit = min(limit, 100)
    results = svc.search_functions(query, source=source, limit=limit)
    return {"count": len(results), "results": results}


def get_function_detail(function_name: str) -> dict:
    """Get full details for a DAAS function — parameters, output columns, description.

    Args:
        function_name: Namespaced function name (e.g., 'worldbank_gdp', 'akshare_stock_zh_a_hist').
    """
    svc = _get_service()
    func = svc.get_function_detail(function_name)
    if func is None:
        return {"error": f"Function '{function_name}' not found"}
    return {"function": func}


def list_categories(source: Optional[str] = None) -> dict:
    """List all DAAS function categories with counts, optionally filtered by source.

    Args:
        source: Optional source name filter (akshare, worldbank, ckan, cnstats).
    """
    svc = _get_service()
    cats = svc.list_categories(source=source)
    return {"categories": cats}


def fetch_data(function_name: str, params_json: str = "{}") -> dict:
    """Execute a DAAS data function and return results as JSON.

    Routes to the correct source adapter based on function name prefix.

    Args:
        function_name: Namespaced function name (e.g., 'worldbank_ny_gdp_mktp_cd').
        params_json: JSON object string of parameter name→value pairs.
                     Example: '{"country": "CHN", "time": "2020:2023"}'
    """
    import os
    import sys

    # Resolve harness path
    _MCP_ROOT = os.path.dirname(os.path.abspath(__file__))
    _HARNESS_ROOT = os.path.join(os.path.dirname(_MCP_ROOT), "daas-agent-harness")
    if _HARNESS_ROOT not in sys.path:
        sys.path.insert(0, _HARNESS_ROOT)

    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError as e:
        return {"error": f"Invalid params_json: {e}"}

    try:
        from daas.daas.sources.router import SourceRouter
        from daas.daas.core.exceptions import DAASError

        router = SourceRouter()
        result = router.route(function_name, **params)
    except DAASError as e:
        return {"error": str(e)}
    except ImportError as e:
        return {"error": f"Dependency not available: {e}", "hint": "Install optional source packages"}
    except Exception as e:
        return {"error": f"Execution error: {type(e).__name__}: {e}"}

    return _serialize_result(result)


def _serialize_result(result) -> dict:
    """Convert a function result to a JSON-serializable dict."""
    try:
        import pandas as pd
    except ImportError:
        return {"type": "unknown", "data": str(result)}

    if isinstance(result, pd.DataFrame):
        clean = result.where(result.notna(), None)
        return {
            "type": "dataframe",
            "shape": list(result.shape),
            "columns": list(result.columns),
            "data": clean.to_dict(orient="records"),
        }
    elif isinstance(result, pd.Series):
        clean = result.where(result.notna(), None)
        return {
            "type": "series",
            "length": len(result),
            "name": str(result.name) if result.name else None,
            "data": clean.to_dict(),
        }
    elif isinstance(result, (dict, list)):
        return {"type": type(result).__name__, "data": result}
    else:
        return {"type": "scalar", "data": str(result)}


# ════════════════════════════════════════════════════════════════
# Management tools — datasource CRUD, category tree, forms/sections,
# collections, multi-level search (additive to the read API above)
# ════════════════════════════════════════════════════════════════


def _ok(result: dict) -> dict:
    return result


def _err(e: Exception) -> dict:
    return {"error": str(e)}


def create_datasource(
    name: str,
    label: str,
    description: Optional[str] = None,
    url: Optional[str] = None,
    config_json: Optional[str] = None,
    category_id: Optional[int] = None,
    enabled: bool = True,
    score: Optional[float] = None,
) -> dict:
    """Create a new managed datasource.

    Args:
        name: Unique datasource name (e.g. 'edgar').
        label: Human-readable label.
        description: Optional description.
        url: Optional source URL.
        config_json: Optional JSON object string of source config.
        category_id: Optional category id to assign this datasource to.
        enabled: Whether the datasource is enabled (default true).
        score: Optional default priority/quality weight (float, e.g. 0.9). NULL means unset.
    """
    svc = _get_service()
    config = json.loads(config_json) if config_json else None
    try:
        return _ok(svc.create_datasource(name, label, description, url, config, category_id, enabled, score))
    except Exception as e:
        return _err(e)


def update_datasource(
    name: Optional[str] = None,
    datasource_id: Optional[int] = None,
    label: Optional[str] = None,
    description: Optional[str] = None,
    url: Optional[str] = None,
    config_json: Optional[str] = None,
    enabled: Optional[bool] = None,
    category_id: Optional[int] = None,
    clear_category: bool = False,
    score: Optional[float] = None,
    clear_score: bool = False,
) -> dict:
    """Update mutable fields of a datasource, identified by name or id.

    Pass clear_category=true to unset the category (move to root level).
    Pass score=<float> to set the default score; pass clear_score=true to reset
    the score back to NULL (unset). Only supplied fields are changed.
    """
    svc = _get_service()
    config = json.loads(config_json) if config_json else None
    try:
        return _ok(
            svc.update_datasource(
                name=name,
                datasource_id=datasource_id,
                label=label,
                description=description,
                url=url,
                config=config,
                enabled=enabled,
                category_id=category_id,
                clear_category=clear_category,
                score=score,
                clear_score=clear_score,
            )
        )
    except Exception as e:
        return _err(e)


def delete_datasource(
    name: Optional[str] = None, datasource_id: Optional[int] = None
) -> dict:
    """Delete a datasource and cascade-delete its forms, sections, and
    collection-item references. Identify by name or id."""
    svc = _get_service()
    try:
        return _ok(svc.delete_datasource(name=name, datasource_id=datasource_id))
    except Exception as e:
        return _err(e)


def create_category(
    name: str,
    label: Optional[str] = None,
    parent_id: Optional[int] = None,
    sort_order: Optional[int] = None,
) -> dict:
    """Create a category, optionally under a parent (building the tree)."""
    svc = _get_service()
    try:
        return _ok(svc.create_category(name, label, parent_id, sort_order))
    except Exception as e:
        return _err(e)


def move_category(category_id: int, parent_id: Optional[int] = None) -> dict:
    """Re-parent a category. Rejects moves that would create a cycle
    (into own descendant) or self-parenting. Pass parent_id=null to make
    it a root."""
    svc = _get_service()
    try:
        return _ok(svc.move_category(category_id, parent_id))
    except Exception as e:
        return _err(e)


def delete_category(category_id: int) -> dict:
    """Delete a category. Rejected if it has child categories. Datasources
    assigned to it are orphaned to root level (category_id set to null)."""
    svc = _get_service()
    try:
        return _ok(svc.delete_category(category_id))
    except Exception as e:
        return _err(e)


def get_category_tree(root_id: Optional[int] = None) -> dict:
    """Return the category tree (or a subtree from root_id) as a nested
    structure, each node annotated with datasource_count."""
    svc = _get_service()
    try:
        return {"categories": svc.get_category_tree(root_id=root_id)}
    except Exception as e:
        return _err(e)


def add_form(
    source_name: str, form_type: str, label: Optional[str] = None
) -> dict:
    """Add a form (e.g. '10-K', '8-K') to a datasource."""
    svc = _get_service()
    try:
        return _ok(svc.add_form(source_name, form_type, label))
    except Exception as e:
        return _err(e)


def add_section(
    form_id: int,
    section_name: str,
    instruction: Optional[str] = None,
    sort_order: Optional[int] = None,
) -> dict:
    """Add a section to a form, carrying an extraction instruction.

    Args:
        form_id: The form id (from add_form/list_forms).
        section_name: e.g. 'Item 1 Business', 'Item 7 MD&A'.
        instruction: Free-text extraction prompt/rule for this section.
    """
    svc = _get_service()
    try:
        return _ok(svc.add_section(form_id, section_name, instruction, sort_order))
    except Exception as e:
        return _err(e)


def list_forms(source_name: str) -> dict:
    """List all forms of a datasource, each with nested sections
    (including instruction)."""
    svc = _get_service()
    try:
        return {"source": source_name, "forms": svc.list_forms(source_name)}
    except Exception as e:
        return _err(e)


def create_collection(name: str, description: Optional[str] = None) -> dict:
    """Create a named datasource collection."""
    svc = _get_service()
    try:
        return _ok(svc.create_collection(name, description))
    except Exception as e:
        return _err(e)


def add_to_collection(
    collection_name: str,
    source_name: str,
    section_name: Optional[str] = None,
    score: Optional[float] = None,
) -> dict:
    """Add a datasource (or a specific datasource-section) to a collection.

    Omit section_name to add the whole datasource; supply section_name to
    add only that section. Optional score sets a per-collection score override
    on the new item (NULL = inherit the datasource's default score)."""
    svc = _get_service()
    try:
        return _ok(svc.add_to_collection(collection_name, source_name, section_name, score))
    except Exception as e:
        return _err(e)


def set_collection_item_score(
    collection_name: str,
    source_name: str,
    section_name: Optional[str] = None,
    score: Optional[float] = None,
) -> dict:
    """Set or clear the per-collection score override on an existing collection
    item, identified by (collection_name, source_name, optional section_name).

    Pass score=<float> to set the override; pass score=null (omit) to clear it
    so the item falls back to the datasource's default score. Returns the
    updated item with its resolved effective score, the raw item_score override,
    and the source_default_score."""
    svc = _get_service()
    try:
        return _ok(
            svc.set_collection_item_score(collection_name, source_name, section_name, score)
        )
    except Exception as e:
        return _err(e)


def list_collection_items(collection_name: str) -> dict:
    """List a collection's items, each resolved to source name and (if set)
    section name + instruction."""
    svc = _get_service()
    try:
        return _ok(svc.list_collection(collection_name))
    except Exception as e:
        return _err(e)


def remove_from_collection(
    collection_name: str,
    source_name: str,
    section_name: Optional[str] = None,
) -> dict:
    """Remove an item from a collection by (collection, source, optional
    section). Omit section_name to remove the whole-datasource item."""
    svc = _get_service()
    try:
        return _ok(svc.remove_from_collection(collection_name, source_name, section_name))
    except Exception as e:
        return _err(e)


def list_collections() -> dict:
    """List every datasource collection with its item count."""
    svc = _get_service()
    try:
        return {"collections": svc.list_collections()}
    except Exception as e:
        return _err(e)


def rename_collection(old_name: str, new_name: str) -> dict:
    """Rename a collection. The new name must be unique. Items are preserved
    (they reference by collection_id)."""
    svc = _get_service()
    try:
        return _ok(svc.rename_collection(old_name, new_name))
    except Exception as e:
        return _err(e)


def update_collection(
    name: str,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Partially update a collection's name and/or description.

    At least one of `new_name` / `description` must be provided; omitted
    fields are left unchanged. When `new_name` differs from the current
    name it must be unique. Raises if the collection is not found.
    """
    svc = _get_service()
    try:
        return _ok(svc.update_collection(name, new_name=new_name, description=description))
    except Exception as e:
        return _err(e)


def delete_collection(name: str) -> dict:
    """Delete a collection. Cascades to its `datasource_collection_items`
    rows (datasources themselves are untouched)."""
    svc = _get_service()
    try:
        return _ok(svc.delete_collection(name))
    except Exception as e:
        return _err(e)


def reorder_collection_items(
    collection_name: str, ordered_item_ids: list[int]
) -> dict:
    """Rewrite the sort_order of items in `collection_name` to match the
    given list. `ordered_item_ids` MUST include every existing item id in
    the collection exactly once — partial reorders are rejected."""
    svc = _get_service()
    try:
        return _ok(svc.reorder_collection_items(collection_name, ordered_item_ids))
    except Exception as e:
        return _err(e)


def search_datasources(
    category_id: Optional[int] = None,
    include_subtree: bool = True,
    source_name: Optional[str] = None,
    form: Optional[str] = None,
    section: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """Search/filter datasources across levels — category (with optional
    subtree), source, form, and section — every filter optional, plus a
    free-text query across source label/description, form label, and
    section name/instruction.

    Args:
        category_id: Filter to this category.
        include_subtree: Include descendant categories (default true).
        source_name: Filter to one datasource.
        form: Drill to a form_type (e.g. '10-K').
        section: Drill to sections matching this name (substring).
        query: Free-text across source/form/section fields.
        limit: Max results (default 100, max 500).
    """
    svc = _get_service()
    try:
        results = svc.search_datasources(
            category_id=category_id,
            include_subtree=include_subtree,
            source_name=source_name,
            form=form,
            section=section,
            query=query,
            limit=limit,
        )
        return {"count": len(results), "results": results}
    except Exception as e:
        return _err(e)

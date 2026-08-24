"""Research tools for the consolidated fd-daas-mcp server - a persisted
research bundle that links an entity collection, indicator collection, rules,
a dashboard, and a cron pipeline collection under one name, plus a generated
markdown report.

Thin tool functions over direct ORM access (shared ``models`` package) +
in-process sibling-tool orchestration for refresh. Tools are registered by
``server.py`` via ``app.tool(<name>)`` and surface as ``research_<name>`` on
the consolidated server.

Note: the list tool is named ``list`` (so it registers as ``research_list``);
this shadows the builtin within this module, so no ``list()`` calls are used
here - prefer comprehensions / ``[*gen]`` / ``.all()``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from models import (
    Dashboard,
    EntityCollection,
    EntityCollectionItem,
    IndicatorCollection,
    IndicatorCollectionItem,
    IndicatorRule,
    Observation,
    PipelineCollection,
    PipelineCollectionItem,
    Research,
    Schedule,
    Task,
)

from research_database import get_database
from research_report import assemble_markdown

REPO_ROOT = Path(__file__).resolve().parents[2]

# component_type -> component_refs JSON key (for the auxiliary-ref kinds)
_REF_KEY = {
    "rule": "rules",
    "scraw_table": "scraw_tables",
    "indicator": "indicators",
}
# component_type -> (model, lookup field, research column) for the named kinds
_NAMED = {
    "entity_collection": (EntityCollection, "name", "entity_collection_name"),
    "indicator_collection": (IndicatorCollection, "name", "indicator_collection_name"),
    "dashboard": (Dashboard, "slug", "dashboard_slug"),
    "pipeline": (PipelineCollection, "name", "pipeline_collection_name"),
}


def _session():
    return get_database().get_session()


def _err(e: Exception) -> dict:
    return {"error": str(e)}


def _sibling_tools() -> dict:
    """Look up registered sibling tool functions from the registry cache.

    Called lazily inside tool bodies (never at import time, to avoid
    build-time recursion). Only sync siblings are safe to call directly; async
    siblings would return a coroutine and are avoided (see design.md §8).
    Uses the consolidated-server package path ``daas.fd_daas_mcp.registry``
    (the launch is ``python -m daas.fd_daas_mcp.server`` with ``daas`` as the
    top package).
    """
    from daas.fd_daas_mcp.registry import build, namespaced

    return {namespaced(g, n): fn for g, n, fn in build()}


# ── bundle resolution ───────────────────────────────────────────────


def _resolve_bundle(session, r: Research) -> dict:
    """Counts + live lookups for research_get (dangling components reported)."""
    out: dict = {}
    dangling: list = []

    out["entity_count"] = None
    if r.entity_collection_name:
        ec = session.query(EntityCollection).filter_by(name=r.entity_collection_name).first()
        if ec:
            out["entity_count"] = session.query(EntityCollectionItem).filter_by(collection_id=ec.id).count()
        else:
            dangling.append(f"entity_collection:{r.entity_collection_name}")

    out["indicator_count"] = None
    if r.indicator_collection_name:
        ic = session.query(IndicatorCollection).filter_by(name=r.indicator_collection_name).first()
        if ic:
            out["indicator_count"] = session.query(IndicatorCollectionItem).filter_by(collection_id=ic.id).count()
        else:
            dangling.append(f"indicator_collection:{r.indicator_collection_name}")

    out["dashboard"] = None
    if r.dashboard_slug:
        d = session.query(Dashboard).filter_by(slug=r.dashboard_slug).first()
        if d:
            out["dashboard"] = {"slug": d.slug, "name": d.name, "file_url": d.file_url}
        else:
            dangling.append(f"dashboard:{r.dashboard_slug}")

    out["pipeline"] = None
    if r.pipeline_collection_name:
        pc = session.query(PipelineCollection).filter_by(name=r.pipeline_collection_name).first()
        if pc:
            items = session.query(PipelineCollectionItem).filter_by(collection_id=pc.id).all()
            statuses = [i.last_status for i in items if i.last_status]
            out["pipeline"] = {
                "item_count": len(items),
                "last_status": statuses[-1] if statuses else None,
            }
        else:
            dangling.append(f"pipeline:{r.pipeline_collection_name}")

    out["dangling"] = dangling
    return out


def _resolve_report_data(session, r: Research) -> dict:
    """Full member rows for research_generate_report."""
    entities = []
    if r.entity_collection_name:
        ec = session.query(EntityCollection).filter_by(name=r.entity_collection_name).first()
        if ec:
            rows = (
                session.query(EntityCollectionItem)
                .filter(EntityCollectionItem.collection_id == ec.id)
                .order_by(EntityCollectionItem.sort_order)
                .all()
            )
            for item in rows:
                # entities/entity_datasource_links are dropped (fd-open-data-mcp is the
                # entity master); only the natural key survives locally. name/ticker/
                # exchange now resolve via the gateway and are omitted from the report.
                entities.append(
                    {
                        "entity_type": item.entity_type,
                        "code": item.code,
                    }
                )

    indicators = []
    if r.indicator_collection_name:
        ic = session.query(IndicatorCollection).filter_by(name=r.indicator_collection_name).first()
        if ic:
            rows = (
                session.query(IndicatorCollectionItem, IndicatorRule)
                .join(IndicatorRule, IndicatorCollectionItem.indicator_id == IndicatorRule.id)
                .filter(IndicatorCollectionItem.collection_id == ic.id)
                .order_by(IndicatorCollectionItem.sort_order)
                .all()
            )
            for _item, rule in rows:
                latest = (
                    session.query(Observation)
                    .filter_by(
                        source=rule.datasource,
                        function_name=rule.function_name,
                        indicator=rule.indicator_name,
                    )
                    .order_by(Observation.date.desc())
                    .first()
                )
                indicators.append(
                    {
                        "indicator_name": rule.indicator_name,
                        "op": rule.op,
                        "params": rule.params_json,
                        "rule_name": rule.name,
                        "latest_date": latest.date if latest else None,
                        "latest_value": latest.value if latest else None,
                    }
                )

    dashboard = None
    if r.dashboard_slug:
        d = session.query(Dashboard).filter_by(slug=r.dashboard_slug).first()
        if d:
            dashboard = {"name": d.name, "file_url": d.file_url, "intro": d.intro}

    pipeline = []
    if r.pipeline_collection_name:
        pc = session.query(PipelineCollection).filter_by(name=r.pipeline_collection_name).first()
        if pc:
            items = (
                session.query(PipelineCollectionItem)
                .filter_by(collection_id=pc.id)
                .order_by(PipelineCollectionItem.id)
                .all()
            )
            for it in items:
                pipeline.append(
                    {
                        "name": it.name,
                        "cron_expr": it.cron_expr,
                        "enabled": bool(it.enabled),
                        "last_status": it.last_status,
                        "last_run_at": it.last_run_at.isoformat() if it.last_run_at else None,
                    }
                )

    return {"entities": entities, "indicators": indicators, "dashboard": dashboard, "pipeline": pipeline}


def _parse_refs(component_refs) -> dict:
    if isinstance(component_refs, str):
        return json.loads(component_refs)
    return component_refs or {}


def _validate_or_create(session, model, name: str, create_missing: bool, label: str):
    """Return None on ok (exists or created), else an error dict."""
    if session.query(model).filter_by(name=name).first():
        return None
    if create_missing:
        session.add(model(name=name))
        session.flush()
        return None
    return {"error": f"{label} {name!r} not found; pass create_missing=True to create it"}


# ── tools ───────────────────────────────────────────────────────────


def create(
    name: str,
    description: Optional[str] = None,
    status: str = "draft",
    entity_collection_name: Optional[str] = None,
    indicator_collection_name: Optional[str] = None,
    dashboard_slug: Optional[str] = None,
    pipeline_collection_name: Optional[str] = None,
    component_refs: Optional[str] = None,
    create_missing: bool = False,
) -> dict:
    """Create a persisted research bundle. Named collections are attached by
    name; with ``create_missing=True`` missing entity/indicator/pipeline
    collections are created empty. A ``dashboard_slug`` must already exist
    (the tool does not build dashboard HTML). ``component_refs`` is a JSON
    object string: ``{"rules": [...], "scraw_tables": [...], "indicators": [...]}``.
    """
    session = _session()
    try:
        if session.query(Research).filter_by(name=name).first():
            return {"error": f"research {name!r} already exists"}

        for model, col, label in (
            (EntityCollection, entity_collection_name, "entity collection"),
            (IndicatorCollection, indicator_collection_name, "indicator collection"),
            (PipelineCollection, pipeline_collection_name, "pipeline collection"),
        ):
            if col:
                err = _validate_or_create(session, model, col, create_missing, label)
                if err:
                    return err

        if dashboard_slug and not session.query(Dashboard).filter_by(slug=dashboard_slug).first():
            return {"error": f"dashboard slug {dashboard_slug!r} not found"}

        r = Research(
            name=name,
            description=description,
            status=status or "draft",
            entity_collection_name=entity_collection_name,
            indicator_collection_name=indicator_collection_name,
            dashboard_slug=dashboard_slug,
            pipeline_collection_name=pipeline_collection_name,
            component_refs=_parse_refs(component_refs),
        )
        session.add(r)
        session.commit()
        return r.to_dict()
    except Exception as e:
        session.rollback()
        return _err(e)
    finally:
        session.close()


def get(name: str) -> dict:
    """Return a research + resolved bundle (live counts, dashboard url,
    pipeline status, dangling-component warnings)."""
    session = _session()
    try:
        r = session.query(Research).filter_by(name=name).first()
        if not r:
            return {"error": f"research {name!r} not found"}
        out = r.to_dict()
        out.update(_resolve_bundle(session, r))
        return out
    except Exception as e:
        return _err(e)
    finally:
        session.close()


def list(status: Optional[str] = None) -> dict:  # noqa: A001 (intentional: research_list)
    """List every research (optionally filtered by status) with attached
    component names."""
    session = _session()
    try:
        q = session.query(Research)
        if status:
            q = q.filter_by(status=status)
        rows = q.order_by(Research.id).all()
        return {
            "researches": [
                {
                    "name": r.name,
                    "status": r.status,
                    "description": r.description,
                    "entity_collection_name": r.entity_collection_name,
                    "indicator_collection_name": r.indicator_collection_name,
                    "dashboard_slug": r.dashboard_slug,
                    "pipeline_collection_name": r.pipeline_collection_name,
                }
                for r in rows
            ]
        }
    except Exception as e:
        return _err(e)
    finally:
        session.close()


def update(
    name: str,
    description: Optional[str] = None,
    status: Optional[str] = None,
    entity_collection_name: Optional[str] = None,
    indicator_collection_name: Optional[str] = None,
    dashboard_slug: Optional[str] = None,
    pipeline_collection_name: Optional[str] = None,
    component_refs: Optional[str] = None,
) -> dict:
    """Partially update a research. ``None`` means leave a field unchanged;
    to detach a named component use ``remove_component``. Any newly-set
    component name is validated to exist. ``component_refs`` (JSON string)
    replaces the auxiliary refs."""
    session = _session()
    try:
        r = session.query(Research).filter_by(name=name).first()
        if not r:
            return {"error": f"research {name!r} not found"}

        if description is not None:
            r.description = description
        if status is not None:
            r.status = status

        for col, model, label, rcol in (
            (entity_collection_name, EntityCollection, "entity collection", "entity_collection_name"),
            (indicator_collection_name, IndicatorCollection, "indicator collection", "indicator_collection_name"),
            (pipeline_collection_name, PipelineCollection, "pipeline collection", "pipeline_collection_name"),
        ):
            if col is not None:
                if col and not session.query(model).filter_by(name=col).first():
                    return {"error": f"{label} {col!r} not found"}
                setattr(r, rcol, col)

        if dashboard_slug is not None:
            if dashboard_slug and not session.query(Dashboard).filter_by(slug=dashboard_slug).first():
                return {"error": f"dashboard slug {dashboard_slug!r} not found"}
            r.dashboard_slug = dashboard_slug

        if component_refs is not None:
            r.component_refs = _parse_refs(component_refs)

        session.commit()
        return r.to_dict()
    except Exception as e:
        session.rollback()
        return _err(e)
    finally:
        session.close()


def delete(name: str, remove_dashboard: bool = False, remove_pipeline: bool = True) -> dict:
    """Delete a research. Removes the on-disk report file. With
    ``remove_pipeline=True`` (default) deletes the owned pipeline collection
    + orphaned cron schedules/tasks. With ``remove_dashboard=True`` (default
    False) deletes the referenced dashboard via the sibling tool. Shared
    entity/indicator collections, rules, scraw tables, and observations are
    NEVER deleted."""
    session = _session()
    try:
        r = session.query(Research).filter_by(name=name).first()
        if not r:
            return {"error": f"research {name!r} not found"}

        result = {
            "deleted": name,
            "report_file_removed": False,
            "pipeline_removed": False,
            "dashboard_removed": False,
        }

        if r.report_path:
            try:
                p = Path(r.report_path)
                if p.exists():
                    p.unlink()
                    result["report_file_removed"] = True
            except Exception as e:  # noqa: BLE001
                result["report_file_error"] = str(e)

        if remove_pipeline and r.pipeline_collection_name:
            pc = session.query(PipelineCollection).filter_by(name=r.pipeline_collection_name).first()
            if pc:
                items = session.query(PipelineCollectionItem).filter_by(collection_id=pc.id).all()
                task_names = [it.task_name for it in items if it.task_name]
                session.delete(pc)  # cascades to items
                if task_names:
                    session.query(Schedule).filter(Schedule.task_name.in_(task_names)).delete(
                        synchronize_session=False
                    )
                    session.query(Task).filter(Task.name.in_(task_names)).delete(synchronize_session=False)
                result["pipeline_removed"] = True

        if remove_dashboard and r.dashboard_slug:
            try:
                fn = _sibling_tools().get("dashboard_delete")
                if fn:
                    fn(slug=r.dashboard_slug)
                    result["dashboard_removed"] = True
                else:
                    result["dashboard_error"] = "dashboard_delete tool unavailable"
            except Exception as e:  # noqa: BLE001
                result["dashboard_error"] = str(e)

        session.delete(r)
        session.commit()
        return result
    except Exception as e:
        session.rollback()
        return _err(e)
    finally:
        session.close()


def generate_report(name: str) -> dict:
    """Assemble a markdown report from the bundle's live components, persist it
    into ``report_md`` and write it to ``researches/<name>.md`` (dir from
    ``RESEARCH_DIR``, default repo-root ``researches/``). Regeneration
    overwrites both. Returns ``{name, report_path, char_count}``."""
    session = _session()
    try:
        r = session.query(Research).filter_by(name=name).first()
        if not r:
            return {"error": f"research {name!r} not found"}

        data = _resolve_report_data(session, r)
        generated_at = datetime.now(timezone.utc).isoformat()
        md = assemble_markdown(
            name=r.name,
            description=r.description,
            status=r.status,
            generated_at=generated_at,
            entities=data["entities"],
            indicators=data["indicators"],
            dashboard=data["dashboard"],
            pipeline=data["pipeline"],
            component_refs=r.component_refs or {},
        )

        research_dir = Path(os.environ.get("RESEARCH_DIR") or (REPO_ROOT / "researches"))
        if "/" in r.name or "\\" in r.name or r.name in ("", ".", ".."):
            return {"error": f"invalid research name for file path: {r.name!r}"}
        research_dir.mkdir(parents=True, exist_ok=True)
        fpath = research_dir / f"{r.name}.md"
        fpath.write_text(md, encoding="utf-8")

        r.report_md = md
        r.report_path = str(fpath)
        session.commit()
        return {"name": r.name, "report_path": str(fpath), "char_count": len(md)}
    except Exception as e:
        session.rollback()
        return _err(e)
    finally:
        session.close()


def refresh(name: str) -> dict:
    """Re-run the research's data pipeline: recompute each indicator in the
    indicator collection (via ``daas_run_indicator``), sync rule-based
    collections (via ``daas_sync_entity_collection``/``daas_sync_indicator_collection``),
    and report each pipeline item's current status. Returns a per-component
    status report; per-item errors are reported, not raised."""
    session = _session()
    try:
        r = session.query(Research).filter_by(name=name).first()
        if not r:
            return {"error": f"research {name!r} not found"}

        tools = _sibling_tools()
        run_indicator = tools.get("daas_run_indicator")
        sync_entity = tools.get("daas_sync_entity_collection")
        sync_indicator = tools.get("daas_sync_indicator_collection")

        indicators_out = []
        if r.indicator_collection_name:
            ic = session.query(IndicatorCollection).filter_by(name=r.indicator_collection_name).first()
            if ic:
                for it in session.query(IndicatorCollectionItem).filter_by(collection_id=ic.id).all():
                    rule = session.query(IndicatorRule).filter_by(id=it.indicator_id).first()
                    if not rule:
                        indicators_out.append({"rule_id": it.indicator_id, "status": "error", "error": "rule not found"})
                        continue
                    if not run_indicator:
                        indicators_out.append({"name": rule.name, "status": "unavailable", "error": "daas_run_indicator tool missing"})
                        continue
                    try:
                        res = run_indicator(name=rule.name)
                        indicators_out.append({"name": rule.name, "indicator": rule.indicator_name, "status": "ok", "result": res})
                    except Exception as e:  # noqa: BLE001
                        indicators_out.append({"name": rule.name, "status": "error", "error": str(e)})

        synced = []
        if r.entity_collection_name and sync_entity:
            ec = session.query(EntityCollection).filter_by(name=r.entity_collection_name).first()
            if ec and (ec.rule_id or ec.rule_json or ec.rule_script):
                try:
                    synced.append({"collection": r.entity_collection_name, "type": "entity", "result": sync_entity(name=r.entity_collection_name)})
                except Exception as e:  # noqa: BLE001
                    synced.append({"collection": r.entity_collection_name, "type": "entity", "error": str(e)})
        if r.indicator_collection_name and sync_indicator:
            ic2 = session.query(IndicatorCollection).filter_by(name=r.indicator_collection_name).first()
            if ic2 and ic2.rule_id:
                try:
                    synced.append({"collection": r.indicator_collection_name, "type": "indicator", "result": sync_indicator(name=r.indicator_collection_name)})
                except Exception as e:  # noqa: BLE001
                    synced.append({"collection": r.indicator_collection_name, "type": "indicator", "error": str(e)})

        pipeline_out = []
        if r.pipeline_collection_name:
            pc = session.query(PipelineCollection).filter_by(name=r.pipeline_collection_name).first()
            if pc:
                for it in session.query(PipelineCollectionItem).filter_by(collection_id=pc.id).all():
                    pipeline_out.append(
                        {
                            "name": it.name,
                            "enabled": bool(it.enabled),
                            "last_status": it.last_status,
                            "last_run_at": it.last_run_at.isoformat() if it.last_run_at else None,
                            "last_row_count": it.last_row_count,
                        }
                    )

        return {
            "name": r.name,
            "indicators": indicators_out,
            "collections_synced": synced,
            "pipeline": pipeline_out,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return _err(e)
    finally:
        session.close()


def add_component(name: str, component_type: str, component_name: str) -> dict:
    """Attach an existing component to a research by name. ``component_type`` ∈
    entity_collection | indicator_collection | dashboard | pipeline | rule |
    scraw_table | indicator. The underlying object is not created - it must
    already exist (for the named kinds)."""
    session = _session()
    try:
        r = session.query(Research).filter_by(name=name).first()
        if not r:
            return {"error": f"research {name!r} not found"}

        if component_type in _NAMED:
            model, field, col = _NAMED[component_type]
            if not session.query(model).filter_by(**{field: component_name}).first():
                return {"error": f"{component_type} {component_name!r} not found"}
            setattr(r, col, component_name)
        elif component_type in _REF_KEY:
            key = _REF_KEY[component_type]
            refs = dict(r.component_refs or {})
            lst = refs.get(key) or []
            if component_name not in lst:
                refs[key] = [*lst, component_name]
            r.component_refs = refs
        else:
            return {"error": f"unknown component_type {component_type!r}"}

        session.commit()
        return r.to_dict()
    except Exception as e:
        session.rollback()
        return _err(e)
    finally:
        session.close()


def remove_component(name: str, component_type: str, component_name: str) -> dict:
    """Detach a component from a research. The underlying object is NOT
    deleted. For named kinds, clears the research column; for auxiliary refs,
    removes the name from ``component_refs``."""
    session = _session()
    try:
        r = session.query(Research).filter_by(name=name).first()
        if not r:
            return {"error": f"research {name!r} not found"}

        if component_type in _NAMED:
            _model, _field, col = _NAMED[component_type]
            setattr(r, col, None)
        elif component_type in _REF_KEY:
            key = _REF_KEY[component_type]
            refs = dict(r.component_refs or {})
            lst = refs.get(key) or []
            refs[key] = [x for x in lst if x != component_name]
            r.component_refs = refs
        else:
            return {"error": f"unknown component_type {component_type!r}"}

        session.commit()
        return r.to_dict()
    except Exception as e:
        session.rollback()
        return _err(e)
    finally:
        session.close()

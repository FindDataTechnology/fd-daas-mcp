"""Unified rule tools for daas-mcp - CRUD + test/run over the `rules` table.

Seven tools (registered as `daas_create_rule` ... `daas_run_rule`):
  create_rule / list_rules / get_rule / update_rule / delete_rule /
  test_rule (dry-run) / run_rule (persist).

The `llm` rule type with `target='rows'` reuses `process_tools.extract_text`
and upserts into `process_results` (incremental on `config_json.last_rowid`),
preserving the semantics of the removed `process_api.run_rule`. Member-target
rules (entity_ids/indicator_names) are evaluated by `RuleEngine` and persisted
by the collection sync tools, not here.

Thin wrappers over a `RuleService` that shares one `daas_database` session per
call - mirroring entity_collection_tools / indicator_collection_tools.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import process_tools as T
from daas_database import get_database
from models import EntityCollection, IndicatorCollection, ProcessResult, Rule
from process_database import validate_identifier
from rule_engine import RuleEngine

_RULE_TYPES = {"json", "script", "position", "llm"}
_TARGETS = {"entity_ids", "indicator_names", "rows"}
_DEFAULT_BATCH = 500


def _svc_session():
    return get_database().get_session()


def _ok(result: dict) -> dict:
    return result


def _err(e: Exception) -> dict:
    return {"success": False, "error": str(e)}


def _parse_config(config_json: Any) -> dict:
    if isinstance(config_json, dict):
        return config_json
    if isinstance(config_json, str):
        try:
            return json.loads(config_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"config_json is not valid JSON: {e}") from e
    raise ValueError("config_json must be a JSON object (dict or JSON string)")


def _validate_config(rule_type: str, target: str, config: dict, session) -> None:
    """Type-specific config validation (raises ValueError on bad config)."""
    if rule_type == "script":
        from pathlib import Path
        from daas_database import _REPO_ROOT

        sp = config.get("script_path")
        if not sp:
            raise ValueError("script rule config requires 'script_path'")
        p = Path(sp)
        if not p.is_absolute():
            p = (_REPO_ROOT / sp).resolve()
        if not p.exists():
            raise FileNotFoundError(f"rule script not found: {sp!r} (resolved to {p})")
    elif rule_type == "llm":
        st = config.get("source_table")
        tc = config.get("text_column")
        if st and tc:
            validate_identifier(st)
            validate_identifier(tc)
            with session.bind.connect() as conn:
                from sqlalchemy import text
                tbl = conn.execute(
                    text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
                    {"n": st},
                ).fetchone()
                if tbl is None:
                    raise ValueError(f"source table not found: {st}")
                cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({st})")).fetchall()]
                if tc not in cols:
                    raise ValueError(f"text_column not found in source table: {tc}")
    elif rule_type == "position":
        if not config.get("selector_type") or not config.get("selector"):
            raise ValueError("position rule config requires 'selector_type' and 'selector'")
        if config.get("selector_type") not in {"css", "xpath", "regex", "jsonpath"}:
            raise ValueError("position selector_type must be css/xpath/regex/jsonpath")


# ── CRUD ────────────────────────────────────────────────────────


def create_rule(
    name: str,
    rule_type: str,
    config_json: Any,
    target: str = "entity_ids",
    description: Optional[str] = None,
    enabled: bool = True,
) -> dict:
    """Create a rule.

    Args:
        name: unique rule name.
        rule_type: json | script | position | llm.
        config_json: type-specific config (JSON object string or dict).
        target: entity_ids | indicator_names | rows.
        description: optional.
        enabled: whether run_rule/sync will process this rule.
    """
    if rule_type not in _RULE_TYPES:
        return {"error": f"rule_type must be one of {sorted(_RULE_TYPES)}"}
    if target not in _TARGETS:
        return {"error": f"target must be one of {sorted(_TARGETS)}"}
    try:
        config = _parse_config(config_json)
    except (ValueError, FileNotFoundError) as e:
        return {"error": str(e)}
    session = _svc_session()
    try:
        _validate_config(rule_type, target, config, session)
        existing = session.query(Rule).filter(Rule.name == name).first()
        if existing is not None:
            return {"error": f"rule name already exists: {name}"}
        rule = Rule(
            name=name,
            rule_type=rule_type,
            target=target,
            config_json=config,
            description=description,
            enabled=enabled,
        )
        session.add(rule)
        session.commit()
        session.refresh(rule)
        return _ok(rule.to_dict())
    except Exception as e:
        session.rollback()
        return _err(e)
    finally:
        session.close()


def list_rules(rule_type: Optional[str] = None) -> dict:
    """List all rules, optionally filtered by rule_type."""
    session = _svc_session()
    try:
        q = session.query(Rule)
        if rule_type:
            q = q.filter(Rule.rule_type == rule_type)
        return _ok({"rules": [r.to_dict() for r in q.order_by(Rule.name).all()]})
    except Exception as e:
        return _err(e)
    finally:
        session.close()


def get_rule(name: str) -> dict:
    """Return one rule by name."""
    session = _svc_session()
    try:
        rule = session.query(Rule).filter(Rule.name == name).first()
        if rule is None:
            return {"error": f"rule not found: {name}"}
        return _ok(rule.to_dict())
    except Exception as e:
        return _err(e)
    finally:
        session.close()


def update_rule(
    name: str,
    rule_type: Optional[str] = None,
    target: Optional[str] = None,
    config_json: Any = None,
    description: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """Partially update a rule. At least one field must be provided. The rule
    name cannot be renamed."""
    if all(x is None for x in (rule_type, target, config_json, description, enabled)):
        return {"error": "at least one field is required"}
    if rule_type is not None and rule_type not in _RULE_TYPES:
        return {"error": f"rule_type must be one of {sorted(_RULE_TYPES)}"}
    if target is not None and target not in _TARGETS:
        return {"error": f"target must be one of {sorted(_TARGETS)}"}
    session = _svc_session()
    try:
        rule = session.query(Rule).filter(Rule.name == name).first()
        if rule is None:
            return {"error": f"rule not found: {name}"}
        if config_json is not None:
            config = _parse_config(config_json)
            _validate_config(rule_type or rule.rule_type, target or rule.target, config, session)
            rule.config_json = config
        if rule_type is not None:
            rule.rule_type = rule_type
        if target is not None:
            rule.target = target
        if description is not None:
            rule.description = description
        if enabled is not None:
            rule.enabled = enabled
        session.commit()
        session.refresh(rule)
        return _ok(rule.to_dict())
    except Exception as e:
        session.rollback()
        return _err(e)
    finally:
        session.close()


def delete_rule(name: str) -> dict:
    """Delete a rule. Referencing collections' rule_id is set to NULL; the
    rule's process_results rows are removed."""
    session = _svc_session()
    try:
        rule = session.query(Rule).filter(Rule.name == name).first()
        if rule is None:
            return {"error": f"rule not found: {name}"}
        rid = rule.id
        # Null referencing collections (defensive: the FK is ON DELETE SET NULL
        # on fresh DBs, but ALTER-added columns on legacy DBs may not enforce it).
        session.query(EntityCollection).filter(EntityCollection.rule_id == rid).update(
            {"rule_id": None}
        )
        session.query(IndicatorCollection).filter(IndicatorCollection.rule_id == rid).update(
            {"rule_id": None}
        )
        session.query(ProcessResult).filter(ProcessResult.rule_id == rid).delete()
        session.delete(rule)
        session.commit()
        return _ok({"deleted": name, "rule_id": rid})
    except Exception as e:
        session.rollback()
        return _err(e)
    finally:
        session.close()


# ── test (dry-run) / run (persist) ──────────────────────────────


def test_rule(name: str, limit: Optional[int] = None) -> dict:
    """Dry-run: evaluate the rule WITHOUT persisting. For member targets
    (entity_ids/indicator_names) returns the derived set. For `target='rows'`
    (llm), extracts from a single source row (no process_results write) as a
    sample."""
    session = _svc_session()
    try:
        rule = session.query(Rule).filter(Rule.name == name).first()
        if rule is None:
            return {"error": f"rule not found: {name}"}
        if not rule.enabled:
            return {"error": f"rule disabled: {name}"}
        db_url = str(session.bind.url)
        if rule.target == "rows":
            sample = _run_llm_rows(rule, session, batch=1, persist=False)
            return _ok({"name": name, "sample": sample})
        items = RuleEngine.evaluate(rule, session, db_url, limit=limit)
        return _ok({"name": name, "rule_type": rule.rule_type, "target": rule.target, "count": len(items), "items": items})
    except Exception as e:
        return _err(e)
    finally:
        session.close()


def run_rule(name: str, batch: int = _DEFAULT_BATCH) -> dict:
    """Evaluate + persist. For `target='rows'` (llm): incremental extraction
    into process_results + advance last_rowid. For member targets: returns the
    derived set (membership is applied by daas_sync_*_collection, not here)."""
    session = _svc_session()
    try:
        rule = session.query(Rule).filter(Rule.name == name).first()
        if rule is None:
            return {"error": f"rule not found: {name}"}
        if not rule.enabled:
            return {"error": f"rule disabled: {name}"}
        db_url = str(session.bind.url)
        if rule.target == "rows":
            return _ok(_run_llm_rows(rule, session, batch=batch, persist=True))
        items = RuleEngine.evaluate(rule, session, db_url)
        return _ok({"name": name, "rule_type": rule.rule_type, "target": rule.target, "count": len(items), "items": items})
    except Exception as e:
        session.rollback()
        return _err(e)
    finally:
        session.close()


def _run_llm_rows(rule: Rule, session, batch: int, persist: bool) -> dict:
    """Incremental LLM extraction for an llm/rows rule. Reads source rows with
    rowid > config.last_rowid, extracts each via process_tools.extract_text,
    upserts into process_results (when persist), and advances last_rowid."""
    from sqlalchemy import text

    config = rule.config_json or {}
    source_table = config.get("source_table")
    text_column = config.get("text_column")
    if not source_table or not text_column:
        raise ValueError("llm rows rule config requires source_table and text_column")
    validate_identifier(source_table)
    validate_identifier(text_column)
    schema = config.get("schema_json") or config.get("schema") or {}
    prompt = config.get("prompt")
    model = config.get("model")
    max_chars = config.get("max_chars", 12000)
    last_rowid = config.get("last_rowid", 0)

    sql = text(
        f'SELECT rowid, "{text_column}" FROM "{source_table}" '
        f"WHERE rowid > :cursor ORDER BY rowid LIMIT :batch"
    )
    with session.bind.connect() as conn:
        rows = conn.execute(sql, {"cursor": last_rowid, "batch": batch}).fetchall()

    if not rows:
        return {"rule": rule.name, "processed": 0, "failed": 0, "next_rowid": last_rowid, "up_to_date": True}

    processed = 0
    failed = 0
    max_rowid = last_rowid
    for rowid, text in rows:
        max_rowid = max(max_rowid, rowid)
        result = T.extract_text(text or "", schema, prompt=prompt, model=model, max_chars=max_chars)
        if persist:
            existing = (
                session.query(ProcessResult)
                .filter(
                    ProcessResult.rule_id == rule.id,
                    ProcessResult.source_table == source_table,
                    ProcessResult.source_rowid == rowid,
                )
                .first()
            )
            payload = (
                {"records": result["records"], "count": result.get("count", 0)}
                if "records" in result
                else {"error": result.get("error", "unknown"), "detail": result.get("detail")}
            )
            if existing is None:
                session.add(
                    ProcessResult(
                        rule_id=rule.id,
                        source_table=source_table,
                        source_rowid=rowid,
                        extracted_json=payload,
                        model=model,
                    )
                )
            else:
                existing.extracted_json = payload
                existing.model = model
        if "records" in result:
            processed += 1
        else:
            failed += 1

    if persist:
        config["last_rowid"] = max_rowid
        rule.config_json = config
        # SQLAlchemy's JSON column does not track in-place dict mutations.
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(rule, "config_json")
        session.commit()
    return {
        "rule": rule.name,
        "processed": processed,
        "failed": failed,
        "next_rowid": max_rowid,
        "up_to_date": len(rows) < batch,
    }


# ── CLI branch (cron-driven) ────────────────────────────────────


def cli_run_rule(name: str) -> int:
    """Run a rule in-process, print a JSON summary, return an exit code. For
    cron-mcp shell tasks (`python server.py --run-rule <name>`)."""
    summary = run_rule(name)
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0 if "error" not in summary else 1

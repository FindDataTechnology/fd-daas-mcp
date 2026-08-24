"""rule_tools tests: CRUD round-trip, create_rule validation (llm source table/
column, script file, bad type), test_rule dry-run, run_rule for target='rows'
(writes process_results + advances last_rowid), delete_rule nulls collection rule_id.

Calls rule_tools functions directly (they use get_database() + RuleEngine, not
the registry, so no eviction dance needed). daas-mcp/ is added to sys.path for
the imports, then removed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DAAS_MCP = Path(__file__).resolve().parents[1] / "daas-mcp"
sys.path.insert(0, str(_DAAS_MCP))
from daas_database import get_database  # noqa: E402
from models import EntityCollection, ProcessResult, Rule  # noqa: E402
import process_tools  # noqa: E402
import rule_tools  # noqa: E402
sys.path.remove(str(_DAAS_MCP))


def _make_scraw_table() -> str:
    """Create a throwaway scraw_zz_test table with a body column + 2 rows."""
    from sqlalchemy import text

    sess = get_database().get_session()
    with sess.bind.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS scraw_zz_test"))
        conn.execute(text("CREATE TABLE scraw_zz_test (body TEXT)"))
        conn.execute(
            text(
                "INSERT INTO scraw_zz_test (body) VALUES ('news about 600519'), ('news about 000001')"
            )
        )
    return "scraw_zz_test"


def _cleanup_rules() -> None:
    sess = get_database().get_session()
    sess.query(Rule).filter(Rule.name.like("zz_test_%")).delete()
    sess.query(EntityCollection).filter(EntityCollection.name.like("zz_test_%")).delete()
    sess.commit()


def test_crud_round_trip_json_rule():
    _cleanup_rules()
    res = rule_tools.create_rule(
        name="zz_test_json",
        rule_type="json",
        config_json='{"entity_type":"stock","exchange":"SSE"}',
    )
    assert not res.get("error"), res
    assert res["rule_type"] == "json"

    listed = rule_tools.list_rules()
    assert "zz_test_json" in [r["name"] for r in listed["rules"]]

    got = rule_tools.get_rule(name="zz_test_json")
    assert got["name"] == "zz_test_json"

    upd = rule_tools.update_rule(name="zz_test_json", description="updated")
    assert upd["description"] == "updated"

    dele = rule_tools.delete_rule(name="zz_test_json")
    assert dele.get("deleted") == "zz_test_json"
    _cleanup_rules()


def test_create_rule_rejects_bad_rule_type_and_target():
    assert "error" in rule_tools.create_rule(name="zz_test_bad", rule_type="nope", config_json="{}")
    assert "error" in rule_tools.create_rule(
        name="zz_test_bad", rule_type="json", target="nope", config_json="{}"
    )
    _cleanup_rules()


def test_create_rule_llm_validates_source_table_and_column():
    _make_scraw_table()
    # missing column -> rejected
    res = rule_tools.create_rule(
        name="zz_test_llm_bad",
        rule_type="llm",
        target="rows",
        config_json='{"source_table":"scraw_zz_test","text_column":"nope","schema_json":{}}',
    )
    assert "error" in res and "text_column" in res["error"], res
    # missing table -> rejected
    res = rule_tools.create_rule(
        name="zz_test_llm_bad2",
        rule_type="llm",
        target="rows",
        config_json='{"source_table":"scraw_zz_does_not_exist","text_column":"body","schema_json":{}}',
    )
    assert "error" in res and "source table" in res["error"], res
    # valid
    res = rule_tools.create_rule(
        name="zz_test_llm",
        rule_type="llm",
        target="rows",
        config_json='{"source_table":"scraw_zz_test","text_column":"body","schema_json":{}}',
    )
    assert not res.get("error"), res
    _cleanup_rules()


def test_create_rule_script_validates_file():
    res = rule_tools.create_rule(
        name="zz_test_script_bad",
        rule_type="script",
        config_json='{"script_path":"/nope/missing.py"}',
    )
    assert "error" in res and "not found" in res["error"], res
    _cleanup_rules()


def test_test_rule_is_dry_run():
    _cleanup_rules()
    rule_tools.create_rule(
        name="zz_test_dry",
        rule_type="json",
        config_json='{"entity_type":"stock","codes":["600519"]}',
    )
    out = rule_tools.test_rule(name="zz_test_dry")
    assert "error" not in out, out
    assert out["count"] == 1, out
    _cleanup_rules()


def test_run_rule_rows_writes_process_results_and_advances_cursor(monkeypatch):
    _make_scraw_table()
    _cleanup_rules()
    monkeypatch.setattr(
        process_tools,
        "extract_text",
        lambda text, schema, **k: {"records": [{"code": text[-6:]}], "count": 1},
    )
    res = rule_tools.create_rule(
        name="zz_test_run",
        rule_type="llm",
        target="rows",
        config_json='{"source_table":"scraw_zz_test","text_column":"body","schema_json":{}}',
    )
    assert not res.get("error"), res

    out = rule_tools.run_rule(name="zz_test_run", batch=10)
    assert out["processed"] == 2, out

    sess = get_database().get_session()
    rule = sess.query(Rule).filter(Rule.name == "zz_test_run").first()
    n = sess.query(ProcessResult).filter(ProcessResult.rule_id == rule.id).count()
    assert n == 2, n
    assert rule.config_json.get("last_rowid", 0) > 0

    # second run is up_to_date (cursor advanced past both rows)
    out2 = rule_tools.run_rule(name="zz_test_run", batch=10)
    assert out2["up_to_date"] is True and out2["processed"] == 0, out2
    _cleanup_rules()


def test_delete_rule_nulls_collection_rule_id():
    _cleanup_rules()
    rule_tools.create_rule(
        name="zz_test_del",
        rule_type="json",
        config_json='{"entity_type":"stock","exchange":"SSE"}',
    )
    sess = get_database().get_session()
    rule = sess.query(Rule).filter(Rule.name == "zz_test_del").first()
    sess.add(EntityCollection(name="zz_test_del_coll", rule_id=rule.id))
    sess.commit()

    dele = rule_tools.delete_rule(name="zz_test_del")
    assert dele.get("deleted") == "zz_test_del"

    sess.expire_all()
    coll = sess.query(EntityCollection).filter_by(name="zz_test_del_coll").first()
    assert coll is not None and coll.rule_id is None
    _cleanup_rules()

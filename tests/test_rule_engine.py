"""RuleEngine unit tests: json + script evaluators, disabled rule, missing
script, script without members, ctx.query read-only enforcement, and the
phase-2 deferral of position/llm types.

These tests call RuleEngine.evaluate directly (no registry build), so the
daas-mcp source dir stays on sys.path for the duration of the test session.

Post-entity-drop (design D5 / task 3.7): rule targets resolve to natural-key
(entity_type, code) tuples, not int entity ids. Filter-only json rules
(exchange/country_code/name_regex) can no longer match without the local
`entities` table and return []; only the explicit `codes` form is resolvable.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_DAAS_MCP = Path(__file__).resolve().parents[1] / "daas-mcp"
if str(_DAAS_MCP) not in sys.path:
    sys.path.insert(0, str(_DAAS_MCP))

from daas_database import get_database  # noqa: E402
from rule_engine import RuleEngine, RuleContext  # noqa: E402


def _json(config: dict) -> SimpleNamespace:
    return SimpleNamespace(
        rule_type="json", target="entity_ids", config_json=config, enabled=True
    )


def _script(path: str) -> SimpleNamespace:
    return SimpleNamespace(
        rule_type="script",
        target="entity_ids",
        config_json={"script_path": path},
        enabled=True,
    )


def test_json_rule_codes_resolve_to_tuples():
    sess = get_database().get_session()
    ids = RuleEngine.evaluate(_json({"entity_type": "stock", "codes": ["600519"]}), sess)
    assert ids == [("stock", "600519")]


def test_json_rule_filter_only_returns_empty():
    # exchange/name_regex filters need the local `entities` table (dropped),
    # so filter-only json rules resolve to nothing.
    sess = get_database().get_session()
    assert RuleEngine.evaluate(_json({"name_regex": "茅台"}), sess) == []


def test_script_rule_normalizes_codes_to_tuples():
    sess = get_database().get_session()
    script = Path(tempfile.mkdtemp()) / "pool.py"
    script.write_text("def members(ctx):\n    return ['600519']\n")
    ids = RuleEngine.evaluate(_script(str(script)), sess, str(sess.bind.url))
    assert ids == [("stock", "600519")]


def test_script_rule_passes_codes_through():
    # No local `entities` master to validate against post-migration (D5/3.7):
    # string codes pass through as ("stock", code) verbatim, unknown or not.
    sess = get_database().get_session()
    script = Path(tempfile.mkdtemp()) / "pool.py"
    script.write_text("def members(ctx):\n    return ['600519', 'NOPE']\n")
    ids = RuleEngine.evaluate(_script(str(script)), sess, str(sess.bind.url))
    assert ids == [("stock", "600519"), ("stock", "NOPE")]


def test_disabled_rule_returns_empty():
    sess = get_database().get_session()
    rule = SimpleNamespace(
        rule_type="json", target="entity_ids", config_json={"codes": ["600519"]}, enabled=False
    )
    assert RuleEngine.evaluate(rule, sess) == []


def test_missing_script_raises_filenotfound():
    sess = get_database().get_session()
    with pytest.raises(FileNotFoundError):
        RuleEngine.evaluate(_script("/nope/does_not_exist.py"), sess, str(sess.bind.url))


def test_script_without_members_raises_typeerror():
    sess = get_database().get_session()
    script = Path(tempfile.mkdtemp()) / "p.py"
    script.write_text("X = 1\n")
    with pytest.raises(TypeError):
        RuleEngine.evaluate(_script(str(script)), sess, str(sess.bind.url))


def test_ctx_query_is_read_only():
    sess = get_database().get_session()
    ctx = RuleContext(str(sess.bind.url))
    try:
        with pytest.raises(sqlite3.OperationalError):
            ctx.query("DELETE FROM observations WHERE indicator='zz_none'")
    finally:
        ctx.close()


def test_position_rule_regex_extracts_codes():
    sess = get_database().get_session()
    rule = SimpleNamespace(
        rule_type="position",
        target="entity_ids",
        config_json={
            "source": {"type": "text", "value": "codes: 600519 and 000001 here"},
            "selector_type": "regex",
            "selector": r"\b(\d{6})\b",
        },
        enabled=True,
    )
    items = RuleEngine.evaluate(rule, sess)
    assert set(items) == {("stock", "600519"), ("stock", "000001")}


def test_position_rule_css_extracts_codes():
    sess = get_database().get_session()
    rule = SimpleNamespace(
        rule_type="position",
        target="entity_ids",
        config_json={
            "source": {"type": "text", "value": "<table><tr><td class='c'>600519</td></tr></table>"},
            "selector_type": "css",
            "selector": "td.c",
            "extract": "text",
        },
        enabled=True,
    )
    items = RuleEngine.evaluate(rule, sess)
    assert items == [("stock", "600519")]


def test_llm_rule_member_mapping(monkeypatch):
    sess = get_database().get_session()
    import process_tools

    monkeypatch.setattr(
        process_tools,
        "extract_text",
        lambda *a, **k: {"records": [{"code": "600519"}], "count": 1},
    )
    rule = SimpleNamespace(
        rule_type="llm",
        target="entity_ids",
        config_json={
            "text": "some news mentioning 600519",
            "schema_json": {},
            "mapping": {"code_from": "code"},
        },
        enabled=True,
    )
    items = RuleEngine.evaluate(rule, sess)
    assert items == [("stock", "600519")]

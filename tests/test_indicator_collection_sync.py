"""Indicator-collection sync (new in phase 1) via the registry-loaded tool.

Indicator collections gain rule-driven membership for the first time. A rule
with target='indicator_names' (here a `script` rule returning indicator names)
drives `sync_indicator_collection`, which diffs and records add_in/remove_out
with source='cron'.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_DAAS_MCP = Path(__file__).resolve().parents[1] / "daas-mcp"
sys.path.insert(0, str(_DAAS_MCP))
from daas_database import get_database  # noqa: E402
from models import IndicatorRule, IndicatorCollection, Rule  # noqa: E402
sys.path.remove(str(_DAAS_MCP))

from daas.fd_daas_mcp import registry  # noqa: E402


def _tools() -> dict:
    return {n: fn for _, n, fn in registry.build()}


def _seed_indicators() -> None:
    sess = get_database().get_session()
    sess.query(IndicatorRule).delete()
    for name in ("zz_ind_a", "zz_ind_b"):
        sess.add(
            IndicatorRule(
                name=name,
                datasource="zz_test_source",
                function_name="zz_fn",
                source_table="scraw_zz_test",
                date_column="date",
                value_column="close",
                op="level",
                indicator_name=name,
            )
        )
    sess.commit()


def _write_names_script(names: list[str]) -> str:
    p = Path(tempfile.mkdtemp()) / "indicators.py"
    p.write_text(f"def members(ctx):\n    return {names!r}\n")
    return str(p)


def _make_rule(script_path: str, name: str = "zz_test_ind_rule") -> int:
    sess = get_database().get_session()
    sess.query(Rule).filter(Rule.name == name).delete()
    rule = Rule(
        name=name,
        rule_type="script",
        target="indicator_names",
        config_json={"script_path": script_path},
    )
    sess.add(rule)
    sess.commit()
    return rule.id


def _member_names(tools: dict, coll: str) -> set:
    items = tools["list_indicator_collection_items"](collection_name=coll)
    return {i["indicator_name"] for i in items.get("items", [])}


def test_sync_adds_indicator_members():
    _seed_indicators()
    tools = _tools()
    rid = _make_rule(_write_names_script(["zz_ind_a", "zz_ind_b"]))
    tools["create_indicator_collection"](name="zz_test_ind", rule_id=rid)
    out = tools["sync_indicator_collection"](name="zz_test_ind")
    assert out["action"] == "synced", out
    assert set(out["added"]) == {"zz_ind_a", "zz_ind_b"}, out
    assert _member_names(tools, "zz_test_ind") == {"zz_ind_a", "zz_ind_b"}


def test_sync_removes_non_matching_indicators():
    _seed_indicators()
    tools = _tools()
    script = _write_names_script(["zz_ind_a", "zz_ind_b"])
    rid = _make_rule(script)
    tools["create_indicator_collection"](name="zz_test_ind_rm", rule_id=rid)
    tools["sync_indicator_collection"](name="zz_test_ind_rm")
    # Rewrite the script to drop zz_ind_b, then re-sync.
    Path(script).write_text("def members(ctx):\n    return ['zz_ind_a']\n")
    out = tools["sync_indicator_collection"](name="zz_test_ind_rm")
    assert "zz_ind_b" in out["removed"], out
    assert _member_names(tools, "zz_test_ind_rm") == {"zz_ind_a"}


def test_sync_is_idempotent():
    _seed_indicators()
    tools = _tools()
    rid = _make_rule(_write_names_script(["zz_ind_a"]))
    tools["create_indicator_collection"](name="zz_test_ind_idem", rule_id=rid)
    tools["sync_indicator_collection"](name="zz_test_ind_idem")
    second = tools["sync_indicator_collection"](name="zz_test_ind_idem")
    assert second["added"] == [] and second["removed"] == [], second


def test_sync_manual_indicator_collection_is_noop():
    _seed_indicators()
    tools = _tools()
    tools["create_indicator_collection"](name="zz_test_ind_manual")
    out = tools["sync_indicator_collection"](name="zz_test_ind_manual")
    assert out["action"] == "manual_collection", out

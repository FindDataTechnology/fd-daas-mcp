"""Entity-collection sync via the registry-loaded (evicted) tool.

The regression: before the fix, `sync_entity_collection` raised
`No module named 'entity_rule_script'` because the consolidation registry
pops `daas-mcp/` off sys.path and evicts the group's modules after `build()`,
so the bare deferred import at sync time could not resolve. These tests build
the registry (triggering eviction) and then call the tool fn, asserting the
sync completes without that error.

To make the eviction realistic, `daas-mcp/` is removed from sys.path after the
pre-build imports (the conftest-added `fd-daas-mcp/` and `fd-daas-mcp/models`
paths remain, but those do not expose `entity_rule_script`/`rule_engine`).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_DAAS_MCP = Path(__file__).resolve().parents[1] / "daas-mcp"
sys.path.insert(0, str(_DAAS_MCP))
from daas_database import get_database  # noqa: E402
from models import EntityCollection, Rule  # noqa: E402
sys.path.remove(str(_DAAS_MCP))

from daas.fd_daas_mcp import registry  # noqa: E402


def _tools() -> dict:
    return {n: fn for _, n, fn in registry.build()}


def _script_returning(codes: list[str]) -> str:
    p = Path(tempfile.mkdtemp()) / "pool.py"
    p.write_text(f"def members(ctx):\n    return {codes!r}\n")
    return str(p)


def test_sync_script_rule_no_entity_rule_script_error():
    """The regression: syncing a script-rule collection via the evicted tool
    must not raise 'No module named entity_rule_script'."""
    tools = _tools()
    res = tools["create_entity_collection"](
        name="zz_test_script", rule_script=_script_returning(["600519"])
    )
    assert not res.get("error"), res
    out = tools["sync_entity_collection"](name="zz_test_script")
    assert "No module named" not in json.dumps(out), out
    assert out["action"] == "synced", out
    items = tools["list_entity_collection_items"](collection_name="zz_test_script")
    codes = [m["code"] for m in items.get("members", [])]
    assert "600519" in codes, codes


def test_sync_rule_id_precedence_over_legacy_rule_script():
    """rule_id takes precedence over a legacy rule_script on the same row."""
    sess = get_database().get_session()
    rule = Rule(
        name="zz_test_rule_sse",
        rule_type="json",
        target="entity_ids",
        config_json={"entity_type": "stock", "codes": ["600519"]},
    )
    sess.add(rule)
    sess.commit()
    tools = _tools()
    # legacy rule_script would select 000001
    tools["create_entity_collection"](
        name="zz_test_prec", rule_script=_script_returning(["000001"])
    )
    # Attach rule_id (600519) directly via the session, LEAVING rule_script set,
    # so _resolve_rule_for_collection's precedence (rule_id first) is exercised.
    coll = sess.query(EntityCollection).filter_by(name="zz_test_prec").first()
    coll.rule_id = rule.id
    sess.commit()
    out = tools["sync_entity_collection"](name="zz_test_prec")
    assert "No module named" not in json.dumps(out), out
    items = tools["list_entity_collection_items"](collection_name="zz_test_prec")
    codes = [m["code"] for m in items.get("members", [])]
    assert "600519" in codes and "000001" not in codes, (codes, out)


def test_sync_manual_collection_is_noop():
    tools = _tools()
    tools["create_entity_collection"](name="zz_test_manual")
    out = tools["sync_entity_collection"](name="zz_test_manual")
    assert out["action"] == "manual_collection", out


def test_sync_is_idempotent():
    tools = _tools()
    tools["create_entity_collection"](
        name="zz_test_idem", rule_script=_script_returning(["600519"])
    )
    first = tools["sync_entity_collection"](name="zz_test_idem")
    second = tools["sync_entity_collection"](name="zz_test_idem")
    assert second["added"] == [] and second["removed"] == [], second
    assert first["added"], first


def test_cli_sync_entity_collection_subcommand():
    """The Click CLI `daas sync_entity_collection` subcommand runs end-to-end
    (exercises the CLI wrapper, not just the tool fn)."""
    from click.testing import CliRunner

    from daas.fd_daas_mcp.cli import cli

    tools = _tools()
    tools["create_entity_collection"](
        name="zz_test_cli", rule='{"entity_type":"stock","codes":["600519"]}'
    )
    result = CliRunner().invoke(
        cli, ["daas", "sync_entity_collection", "name=zz_test_cli", "--json"]
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["action"] == "synced", parsed


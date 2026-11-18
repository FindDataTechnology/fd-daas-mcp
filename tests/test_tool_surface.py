"""Tool-surface manifest tests (facet-mcp-foundation-v1 task 2.2).

Covers the manifest↔registry contract beyond ``test_selfcheck.py``'s
pass/fail smoke: the 8 business anchors stay commercial, execution/write
tools stay internal, and both drift directions (unclassified registry tool,
ghost manifest entry) are caught with the offending tool named.
"""
from __future__ import annotations

import copy

from daas.fd_daas_mcp import registry, selfcheck

# business-mcp daas domain exposes exactly these 8 (contract anchors).
ANCHORS = {
    "daas_list_indicators", "daas_get_indicator",
    "daas_search_entities", "daas_get_entity",
    "daas_list_rules", "daas_list_sources",
    "daas_search_functions", "daas_get_function_detail",
}


def test_manifest_shape():
    m = selfcheck.load_manifest()
    assert m["server"] == "fd-daas-mcp"
    assert set(m["groups"]) == set(registry.SOURCES)
    skills = m["skills"]
    assert len(skills["consume"]) == 8 and len(skills["create"]) == 5
    assert set(m["hosts"]) == {
        "shared-readonly-catalog", "per-deployment-workspace", "local-stdio",
    }


def test_commercial_set_anchors_and_discipline():
    commercial = selfcheck.commercial_tool_set()
    assert ANCHORS <= commercial
    # execution / write / scheduling tools never commercial
    for banned in ("daas_fetch_data", "daas_create_datasource", "daas_run_rule",
                   "daas_calculate_indicator", "daas_delete_indicator",
                   "cron_create_schedule", "cron_run_now", "workflow_run",
                   "dashboard_register", "gateway_call_data_mcp"):
        assert banned not in commercial


def test_run_invariants_surface_checks_pass():
    result = selfcheck.run_invariants()
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["tool-surface-coverage"]["ok"] is True
    assert by_name["tool-surface-no-ghosts"]["ok"] is True
    assert ANCHORS <= set(result["commercial_tools"])


def _surface_results(manifest):
    tools = registry.build()
    rep = registry.build_report()
    return selfcheck._tool_surface_checks(manifest, tools, rep["skipped_optional"])


def test_drift_unclassified_tool_fails_with_name():
    m = selfcheck.load_manifest()
    # A new daas tool the manifest does not know: group posture is commercial,
    # so without an explicit entry it must fail (never silently commercial).
    drifted = copy.deepcopy(m)
    del drifted["groups"]["daas"]["tools"]["fetch_data"]
    cov, _ = _surface_results(drifted)
    assert cov["ok"] is False
    assert "daas_fetch_data" in cov["detail"]


def test_drift_ghost_entry_fails_with_name():
    m = selfcheck.load_manifest()
    drifted = copy.deepcopy(m)
    drifted["groups"]["daas"]["tools"]["no_such_tool"] = "commercial"
    _, ghost = _surface_results(drifted)
    assert ghost["ok"] is False
    assert "daas_no_such_tool" in ghost["detail"]

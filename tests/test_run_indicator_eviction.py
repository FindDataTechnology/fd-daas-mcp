"""Regression: `run_indicator` (and `calculate`) reached a deferred
``import indicator_tools`` that failed under the consolidated registry's
module eviction (``No module named 'indicator_tools'``) - the same class of
regression the rule_engine path-based load fixed for entity_rule_script.

These tests build the registry (triggering eviction) then call the
registry-loaded `daas_run_indicator` tool and assert it computes + upserts
observations rather than raising ModuleNotFoundError. process_database now
loads indicator_tools via a path-based helper (see _load_indicator_tools).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_DAAS_MCP = Path(__file__).resolve().parents[1] / "daas-mcp"
sys.path.insert(0, str(_DAAS_MCP))
from daas_database import get_database  # noqa: E402
from models import IndicatorRule, Observation  # noqa: E402
sys.path.remove(str(_DAAS_MCP))

from daas.fd_daas_mcp import registry  # noqa: E402


def _tools() -> dict:
    return {n: fn for _, n, fn in registry.build()}


@pytest.fixture(autouse=True)
def _seed_indicator():
    from sqlalchemy import text

    eng = get_database().engine
    with eng.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS scraw_evict_daily"))
        conn.execute(text("CREATE TABLE scraw_evict_daily (date TEXT, close REAL)"))
        for i in range(8):
            conn.execute(
                text("INSERT INTO scraw_evict_daily (date, close) VALUES (:d, :v)"),
                {"d": f"2024-02-{i + 1:02d}", "v": 50.0 + i},
            )
    sess = get_database().get_session()
    sess.query(Observation).filter_by(indicator="evict_sma3").delete()
    sess.query(IndicatorRule).filter_by(name="evict_rule").delete()
    sess.add(
        IndicatorRule(
            name="evict_rule",
            datasource="akshare",
            function_name="evict_fn",
            source_table="scraw_evict_daily",
            date_column="date",
            value_column="close",
            op="sma",
            params_json={"window": 3},
            indicator_name="evict_sma3",
        )
    )
    sess.commit()
    sess.close()
    yield
    sess = get_database().get_session()
    sess.query(Observation).filter_by(indicator="evict_sma3").delete()
    sess.query(IndicatorRule).filter_by(name="evict_rule").delete()
    sess.commit()
    sess.close()


def test_run_indicator_works_after_eviction():
    """daas_run_indicator (registry-loaded) computes + upserts observations
    instead of raising ModuleNotFoundError for indicator_tools."""
    res = _tools()["run_indicator"](name="evict_rule")
    assert "error" not in res, res
    sess = get_database().get_session()
    cnt = sess.query(Observation).filter_by(indicator="evict_sma3").count()
    latest = (
        sess.query(Observation)
        .filter_by(indicator="evict_sma3")
        .order_by(Observation.date.desc())
        .first()
    )
    sess.close()
    assert cnt == 6  # sma3 over 8 rows -> 6 values
    assert latest is not None
    # last 3 closes: 55,56,57 -> mean 56.0
    assert abs(float(latest.value) - 56.0) < 1e-6

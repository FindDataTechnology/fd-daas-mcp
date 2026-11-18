"""Tests for the DAAS catalog -> Postgres metadata synchronizer.

Contract under test (facet-mcp-foundation-v1 task 4.1):
- table whitelist is a hardcoded closed set (observations / scraw_* / any
  other catalog-external table can never leak into the target);
- source reading adapts SQLite shapes (0/1 ints, JSON text, DATETIME strings);
- sync is a full-replace (DELETE then INSERT) that is idempotent by
  construction: replaying the same source converges to identical payloads
  (verified against a recording stub connection — no live PG in tests).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from daas.fd_daas_mcp import sync_postgres


# --- whitelist -----------------------------------------------------------

def test_whitelist_is_exactly_the_v1_catalog_tables():
    assert set(sync_postgres.TABLES) == {
        "sources", "daas_functions", "indicator_rules", "rules",
    }


@pytest.mark.parametrize("table", [
    "observations", "executions", "tasks", "schedules", "workflow_runs",
    "scraw_aapl_daily", "alert_events", "entity_collections",
    "datasource_collections", "pdf_documents", "gateway_upstreams",
])
def test_whitelist_excludes_observation_and_state_tables(table):
    assert table not in sync_postgres.TABLES


def test_sync_rejects_tables_outside_whitelist():
    class _Never:
        def transaction(self):  # pragma: no cover - must never be reached
            raise AssertionError("transaction must not start on invalid input")

    with pytest.raises(ValueError, match="outside the v1 whitelist"):
        sync_postgres.sync({"observations": [{"id": 1}]}, _Never())


# --- source reading / adaptation -----------------------------------------

def _make_source_db(path, extra_tables: bool = False) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY, name TEXT, label TEXT, description TEXT,
            url TEXT, enabled BOOLEAN, config JSON, created_at DATETIME,
            updated_at DATETIME, category_id INTEGER, score REAL);
        CREATE TABLE daas_functions (
            id INTEGER PRIMARY KEY, source_id INTEGER, name TEXT, label TEXT,
            description TEXT, category TEXT, parameters JSON,
            output_type TEXT, created_at DATETIME, updated_at DATETIME,
            frequency TEXT);
        CREATE TABLE rules (
            id INTEGER PRIMARY KEY, name TEXT, rule_type TEXT, target TEXT,
            config_json JSON, description TEXT, enabled BOOLEAN,
            created_at DATETIME, updated_at DATETIME);
        CREATE TABLE indicator_rules (
            id INTEGER PRIMARY KEY, name TEXT, datasource TEXT,
            function_name TEXT, source_table TEXT, date_column TEXT,
            value_column TEXT, op TEXT, params_json JSON,
            indicator_name TEXT, enabled BOOLEAN, created_at DATETIME,
            updated_at DATETIME, score REAL);
    """)
    if extra_tables:
        # look-alikes that must never be picked up by the whitelist
        conn.executescript("""
            CREATE TABLE observations (id INTEGER PRIMARY KEY, payload TEXT);
            CREATE TABLE scraw_xxx_daily (id INTEGER PRIMARY KEY, v REAL);
        """)
        conn.execute("INSERT INTO observations VALUES (1, 'secret')")
    conn.execute(
        "INSERT INTO sources VALUES (1, 'ckan', 'CKAN', NULL, 'https://x', 1, ?, "
        "'2026-06-23 20:36:20', '2026-07-04 06:49:20.079242', NULL, 0.5)",
        (json.dumps({"type": "scraw", "crawl": {"per_page": 18}}),),
    )
    conn.execute(
        "INSERT INTO daas_functions VALUES (2, 1, 'latest', NULL, 'd', 'timeseries', "
        "?, 'float', '2026-06-23 20:36:20', NULL, 'daily')",
        (json.dumps({"window": 5}),),
    )
    conn.execute(
        "INSERT INTO indicator_rules VALUES (3, 'r1', 'ckan', 'latest', 't', 'd', 'v', "
        "'above', ?, 'ind', 0, NULL, NULL, NULL)",
        (json.dumps({"threshold": 10}),),
    )
    conn.execute(
        "INSERT INTO rules VALUES (4, 'q', 'mask', 'source', ?, 'desc', 1, NULL, NULL)",
        (json.dumps({"keep": ["a"]}),),
    )
    conn.commit()
    conn.close()


def test_read_source_reads_only_whitelisted_and_adapts(tmp_path):
    db = tmp_path / "src.db"
    _make_source_db(db, extra_tables=True)
    rows = sync_postgres.read_source(db)

    assert set(rows) == set(sync_postgres.TABLES)  # observations/scraw_* absent
    src = rows["sources"][0]
    assert src["enabled"] is True                      # 0/1 -> bool
    assert src["config"] == {"type": "scraw", "crawl": {"per_page": 18}}
    assert (src["created_at"].year, src["created_at"].month) == (2026, 6)
    assert src["updated_at"].microsecond == 79242      # fractional seconds kept
    assert rows["daas_functions"][0]["parameters"] == {"window": 5}
    assert rows["indicator_rules"][0]["enabled"] is False
    assert rows["rules"][0]["config_json"] == {"keep": ["a"]}


def test_read_source_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        sync_postgres.read_source(tmp_path / "nope.db")


# --- full-replace sync idempotency (stub connection, no live PG) ---------

class RecordingCursor:
    def __init__(self, log):
        self._log = log

    def executemany(self, sql, payload):
        self._log.append(("executemany", sql, [tuple(r) for r in payload]))


class RecordingConn:
    """Minimal psycopg-3-shaped stub: transaction() + execute() + cursor()."""

    def __init__(self):
        self.log: list[tuple] = []
        self._in_txn = False

    def transaction(self):
        self._in_txn = True
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._in_txn = False
        return False

    def execute(self, sql):
        self.log.append(("execute", sql))

    def cursor(self):
        return RecordingCursor(self.log)


def _source_db(tmp_path: Path) -> Path:
    """Canonical minimal source fixture (same shape as the real daas.db)."""
    db = tmp_path / "src.db"
    _make_source_db(db)
    return db


def test_full_replace_shape_and_order(tmp_path):
    conn = RecordingConn()
    rows = sync_postgres.read_source(_source_db(tmp_path))
    counts = sync_postgres.sync(rows, conn)

    assert counts == {"sources": 1, "daas_functions": 1,
                      "indicator_rules": 1, "rules": 1}
    kinds = [entry[0] for entry in conn.log]
    # one transaction, all DELETEs first (children before parents), then INSERTs
    assert kinds[0] == "execute"
    deletes = [e for e in conn.log if e[0] == "execute"]
    assert [e[1] for e in deletes] == [
        "DELETE FROM rules", "DELETE FROM indicator_rules",
        "DELETE FROM daas_functions", "DELETE FROM sources",
    ]
    inserts = [e for e in conn.log if e[0] == "executemany"]
    assert [e[1].split()[2] for e in inserts] == [
        "sources", "daas_functions", "indicator_rules", "rules",
    ]
    # JSONB columns carry an explicit cast; bools are real booleans
    assert "::jsonb" in inserts[0][1]
    src_row = inserts[0][2][0]
    assert src_row[5] is True and isinstance(src_row[6], str)  # enabled, config


def test_sync_is_idempotent_on_replay(tmp_path):
    rows = sync_postgres.read_source(_source_db(tmp_path))
    first, second = RecordingConn(), RecordingConn()
    counts_1 = sync_postgres.sync(rows, first)
    counts_2 = sync_postgres.sync(rows, second)

    assert counts_1 == counts_2
    payloads_1 = [e[2] for e in first.log if e[0] == "executemany"]
    payloads_2 = [e[2] for e in second.log if e[0] == "executemany"]
    assert payloads_1 == payloads_2  # same source -> byte-identical replay


def test_reconcile_passes_and_fails(tmp_path):
    rows = sync_postgres.read_source(_source_db(tmp_path))
    counts = {name: len(rows[name]) for name in sync_postgres.TABLE_ORDER}
    assert sync_postgres.reconcile(rows, counts) is True
    bad = dict(counts, indicator_rules=counts["indicator_rules"] - 1)
    assert sync_postgres.reconcile(rows, bad) is False

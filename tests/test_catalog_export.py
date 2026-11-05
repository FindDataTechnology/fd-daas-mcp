"""Tests for the DAAS catalog export (supply-catalog data-anchor feed).

Contract under test (paas change add-supply-catalog-unification task 2.1):
- export count equals the catalog tables' enabled-row counts (reconciliation
  the facet puller's G1 gate compares against);
- two exports of an unchanged library are idempotent (anchors byte-identical;
  only as_of moves);
- disabled rows and observation/series tables never leak into the feed;
- ids are stable (primary-key derived, replayable across exports).
"""
from __future__ import annotations

import sqlite3

import pytest

from daas.fd_daas_mcp import catalog_export


DDL = """
CREATE TABLE indicator_rules (
  id INTEGER PRIMARY KEY, name TEXT, indicator_name TEXT, datasource TEXT,
  function_name TEXT, source_table TEXT, date_column TEXT, value_column TEXT,
  op TEXT, params_json TEXT, enabled INTEGER
);
CREATE TABLE sources (
  id INTEGER PRIMARY KEY, name TEXT, label TEXT, description TEXT, url TEXT,
  enabled INTEGER
);
CREATE TABLE entity_collection_items (
  id INTEGER PRIMARY KEY, collection_id INTEGER, entity_type TEXT, code TEXT,
  sort_order INTEGER
);
CREATE TABLE observations (id INTEGER PRIMARY KEY, value REAL);
"""


@pytest.fixture()
def seeded_db(tmp_path):
    db_path = tmp_path / "daas.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(DDL)
        db.executemany(
            "INSERT INTO indicator_rules (id,name,indicator_name,datasource,function_name,"
            "source_table,date_column,value_column,op,params_json,enabled) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, "gdp_rule", "GDP", "nbs", "f1", "t", "d", "v", "sum", "{}", 1),
                (2, "cpi_rule", "CPI", "nbs", "f2", "t", "d", "v", "sum", "{}", 1),
                (3, "off_rule", "OFF", "nbs", "f3", "t", "d", "v", "sum", "{}", 0),
            ],
        )
        db.executemany(
            "INSERT INTO sources (id,name,label,description,url,enabled) VALUES (?,?,?,?,?,?)",
            [
                (1, "nbs", "国家统计局", "official stats", "https://nbs", 1),
                (2, "dead", "已下线源", "", "https://x", 0),
            ],
        )
        db.executemany(
            "INSERT INTO entity_collection_items (id,collection_id,entity_type,code,sort_order) VALUES (?,?,?,?,?)",
            [(1, 1, "region", "CN11", 0), (2, 1, "region", "CN31", 1)],
        )
        db.executemany("INSERT INTO observations (id,value) VALUES (?,?)", [(1, 3.14)])
        db.commit()
    return str(db_path)


def _anchors_without_asof(payload):
    return {k: v for k, v in payload.items() if k != "as_of"}


def test_count_matches_catalog_tables(seeded_db):
    payload = catalog_export.export_catalog(seeded_db)
    with sqlite3.connect(f"file:{seeded_db}?mode=ro", uri=True) as db:
        enabled_ind = db.execute("SELECT COUNT(*) FROM indicator_rules WHERE enabled=1").fetchone()[0]
        enabled_src = db.execute("SELECT COUNT(*) FROM sources WHERE enabled=1").fetchone()[0]
        ents = db.execute("SELECT COUNT(*) FROM entity_collection_items").fetchone()[0]
    expected = enabled_ind + enabled_src + ents
    assert len(payload["anchors"]) == expected == 5


def test_export_is_idempotent(seeded_db):
    first = catalog_export.export_catalog(seeded_db)
    second = catalog_export.export_catalog(seeded_db)
    assert _anchors_without_asof(first) == _anchors_without_asof(second)
    assert first["as_of"] and second["as_of"]


def test_disabled_and_series_never_leak(seeded_db):
    payload = catalog_export.export_catalog(seeded_db)
    names = {a["name"] for a in payload["anchors"]}
    assert "OFF" not in names and "已下线源" not in names
    assert all(a["kind"] in catalog_export.ANCHOR_KINDS for a in payload["anchors"])


def test_ids_are_stable_primary_key_derived(seeded_db):
    ids = {a["id"] for a in catalog_export.export_catalog(seeded_db)["anchors"]}
    assert ids == {"ind-1", "ind-2", "src-1", "ent-1", "ent-2"}

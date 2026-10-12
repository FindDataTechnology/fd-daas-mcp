"""customer_dataset_quality — dataset quality snapshot (wire-asset-catalog
§3, engine side).

The engine-side half of the quality visibility contract: the platform never
reads cell data directly, it only receives the aggregate payload this tool
emits. Covers the required behaviors:

- five contract outputs on a dataset with a duplicate pk value and NULLs:
  row_count / computed_at / pk_uniqueness (2 distinct of 3 rows → 0.666…) /
  per-column null rates (exact when under the sample cap) / sample_limit_hit;
- honesty shapes: no declared pk → pk_declared=False and pk_uniqueness=None
  (未声明键列 — no ratio invented); empty dataset → vacuous-unique 1.0 and
  null_rate 0.0 with sampled_rows=0; unknown key → the group's existing
  数据集不存在 error;
- sampling: a >10,000-row dataset → sample_limit_hit=True, sampled_rows
  exactly 10,000, null rates computed on the sample only;
- payload red line: serialized payload contains none of the seeded sentinel
  cell values — aggregate counts/ratios and names only;
- effective-table semantics + read-only-ness: counts follow applied
  corrections, and a quality call appends nothing to the provenance WAL.

Dialect note: no PostgreSQL harness exists for the customer-dataset group
yet (test_sync_postgres covers only the catalog metadata synchronizer), so
this suite runs on SQLite; PG coverage relies on the tool's dialect-neutral
SQL review (COUNT(*) / COUNT(DISTINCT col) / sampled LIMIT subquery only —
see ``_quality_query_all``).

Isolation: each test points ``DAAS_DATABASE_URL`` at its own ``tmp_path``
database (same pattern as test_customer_dataset_incremental.py — every
registry rebuild re-imports the group modules, so the first
``get_database()`` call binds to this URL).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from daas.fd_daas_mcp import registry

COLS = [
    {"name": "id", "type": "INTEGER"},
    {"name": "region", "type": "TEXT"},
    {"name": "amount", "type": "REAL"},
    {"name": "note", "type": "TEXT"},
]

# one duplicate pk value (id=1 twice) + NULLs in region/amount
ROWS_DUP_PK = [
    {"id": 1, "region": "华东", "amount": 10.5, "note": "a1"},
    {"id": 1, "region": "华北", "amount": None, "note": "b2"},
    {"id": 2, "region": None, "amount": 30.25, "note": "b3"},
]

CONTRACT_KEYS = {
    "dataset_key",
    "row_count",
    "computed_at",
    "pk_declared",
    "pk_uniqueness",
    "null_rates",
    "sample_limit_hit",
}


@pytest.fixture(autouse=True)
def _fresh_engine_db(tmp_path, monkeypatch):
    """Per-test engine database under tmp_path (see module docstring)."""
    monkeypatch.setenv("DAAS_DATABASE_URL", f"sqlite:///{tmp_path}/quality_engine.db")
    yield


def _t() -> dict:
    return {
        registry.namespaced(g, n): fn
        for g, n, fn in registry.build(profile="cell")
    }


def _ingest(key: str, rows: list, pk: list | None = ["id"], columns: list | None = None) -> None:
    t = _t()
    begun = t["customer_dataset_ingest_begin"](
        dataset_key=key, columns=columns or COLS, pk_columns=pk
    )
    assert "error" not in begun, begun
    if rows:
        chunked = t["customer_dataset_ingest_chunk"](
            ingest_id=begun["ingest_id"], rows=rows, start_index=0
        )
        assert "error" not in chunked, chunked
    committed = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" not in committed, committed


def _quality(key: str) -> dict:
    r = _t()["customer_dataset_quality"](dataset_key=key)
    assert "error" not in r, r
    return r


def _rates_by_column(payload: dict) -> dict:
    return {nr["column"]: nr for nr in payload["null_rates"]}


# ── contract outputs on a duplicate-pk dataset ────────────────────────


def test_duplicate_pk_null_rates_and_contract_shape():
    key = f"qdup_{os.urandom(4).hex()}"
    _ingest(key, ROWS_DUP_PK, pk=["id"])

    assert "customer_dataset_quality" in _t()  # registered under the cell profile
    p = _quality(key)
    assert set(p.keys()) == CONTRACT_KEYS

    assert p["dataset_key"] == key
    assert p["row_count"] == 3
    # ISO-8601 UTC (Z suffix, parseable)
    assert p["computed_at"].endswith("Z")
    datetime.fromisoformat(p["computed_at"].replace("Z", "+00:00"))

    # 3 rows, 2 distinct pk values → 0.666…
    assert p["pk_declared"] is True
    assert p["pk_uniqueness"]["columns"] == ["id"]
    assert p["pk_uniqueness"]["unique_ratio"] == pytest.approx(2 / 3)

    # exact rates (3 rows ≤ the 10,000 sample cap → no capping)
    assert p["sample_limit_hit"] is False
    rates = _rates_by_column(p)
    assert [nr["column"] for nr in p["null_rates"]] == ["id", "region", "amount", "note"]
    assert rates["id"]["null_rate"] == 0.0
    assert rates["region"]["null_rate"] == pytest.approx(1 / 3)
    assert rates["amount"]["null_rate"] == pytest.approx(1 / 3)
    assert rates["note"]["null_rate"] == 0.0
    assert all(nr["sampled_rows"] == 3 for nr in p["null_rates"])


def test_composite_pk_distinct_subquery():
    cols = [{"name": "a", "type": "INTEGER"}, {"name": "b", "type": "INTEGER"}]
    rows = [{"a": 1, "b": 1}, {"a": 1, "b": 1}, {"a": 1, "b": 2}]
    key = f"qcmp_{os.urandom(4).hex()}"
    _ingest(key, rows, pk=["a", "b"], columns=cols)
    p = _quality(key)
    assert p["pk_uniqueness"]["columns"] == ["a", "b"]
    # 3 rows, 2 distinct (a,b) tuples → 0.666… (portable DISTINCT-subquery form)
    assert p["pk_uniqueness"]["unique_ratio"] == pytest.approx(2 / 3)


# ── honesty shapes ────────────────────────────────────────────────────


def test_no_declared_pk_pk_uniqueness_null():
    key = f"qnopk_{os.urandom(4).hex()}"
    _ingest(key, ROWS_DUP_PK, pk=None)
    p = _quality(key)
    assert p["pk_declared"] is False
    assert p["pk_uniqueness"] is None  # 未声明键列 → no ratio value
    # null rates still reported, exact
    rates = _rates_by_column(p)
    assert rates["amount"]["null_rate"] == pytest.approx(1 / 3)
    assert p["sample_limit_hit"] is False


def test_empty_dataset_vacuously_unique():
    key = f"qempty_{os.urandom(4).hex()}"
    t = _t()
    created = t["customer_dataset_create_declared"](
        dataset_key=key, columns=COLS, pk_columns=["id"]
    )
    assert "error" not in created, created
    p = _quality(key)
    assert p["row_count"] == 0
    assert p["pk_declared"] is True
    assert p["pk_uniqueness"]["unique_ratio"] == 1.0  # vacuously unique
    assert all(nr["null_rate"] == 0.0 and nr["sampled_rows"] == 0 for nr in p["null_rates"])
    assert p["sample_limit_hit"] is False


def test_unknown_key_honest_error():
    r = _t()["customer_dataset_quality"](dataset_key="ghost_no_such")
    assert r.get("success") is False
    assert "数据集不存在" in r["error"]


# ── sampling cap ──────────────────────────────────────────────────────


def test_sample_limit_hit_on_large_dataset():
    key = f"qbig_{os.urandom(4).hex()}"
    # 10,050 rows; note is NULL on every 10th row — any 10,000 consecutive
    # rows of the insert-ordered table contain exactly 1,000 of them.
    rows = [
        {"id": i, "region": f"区{i}", "amount": float(i), "note": None if i % 10 == 0 else f"n{i}"}
        for i in range(10_050)
    ]
    _ingest(key, rows, pk=["id"])
    p = _quality(key)
    assert p["row_count"] == 10_050
    assert p["sample_limit_hit"] is True
    assert all(nr["sampled_rows"] == 10_000 for nr in p["null_rates"])
    rates = _rates_by_column(p)
    assert rates["note"]["null_rate"] == pytest.approx(1_000 / 10_000)
    assert rates["id"]["null_rate"] == 0.0
    # count-side metrics stay exact over the FULL table, not the sample
    assert p["pk_uniqueness"]["unique_ratio"] == 1.0


# ── payload red line: no data values ever ─────────────────────────────


def test_payload_carries_no_data_values():
    sentinel_region = "哨兵区域XYZ-QZY-998"
    sentinel_note = "sentinel-note-998877665544"
    rows = [
        {"id": 1, "region": sentinel_region, "amount": 987654.321, "note": sentinel_note},
        {"id": 2, "region": sentinel_region, "amount": None, "note": sentinel_note},
    ]
    key = f"qred_{os.urandom(4).hex()}"
    _ingest(key, rows, pk=["id"])
    blob = json.dumps(_quality(key), ensure_ascii=False, default=str)
    assert sentinel_region not in blob
    assert sentinel_note not in blob
    assert "987654" not in blob  # no amount value leaked either
    # and the null-rate math still used those rows honestly
    p = json.loads(blob)
    assert _rates_by_column(p)["amount"]["null_rate"] == pytest.approx(1 / 2)


# ── effective table + read-only observation ───────────────────────────


def test_counts_follow_corrections_and_no_wal_events():
    rows = [
        {"id": 1, "region": "华东", "amount": 1.0, "note": "a"},
        {"id": 2, "region": "华北", "amount": 2.0, "note": "b"},
        {"id": 3, "region": "华南", "amount": 3.0, "note": "c"},
    ]
    key = f"qeff_{os.urandom(4).hex()}"
    _ingest(key, rows, pk=["id"])
    t = _t()
    deleted = t["customer_dataset_correction_add"](
        dataset_key=key, op="delete", anchor="pk:[2]", actor="test"
    )
    assert "error" not in deleted, deleted

    db_path = Path(os.environ["DAAS_DATABASE_URL"][len("sqlite:///"):])

    def _wal_count() -> int:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM cell_event_wal").fetchone()[0]
        finally:
            conn.close()

    before = _wal_count()
    p = _quality(key)
    # the effective table (base + corrections) is what quality measures
    assert p["row_count"] == 2
    assert p["pk_uniqueness"]["unique_ratio"] == 1.0
    assert _rates_by_column(p)["region"]["null_rate"] == 0.0
    # read-only observation: a quality call appends nothing to the WAL
    assert _wal_count() == before

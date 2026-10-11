"""Cross-backend consistency for the customer-dataset engine
(openspec: wire-cell-postgres-storage, task 2).

Runs identically on both legs (``FD_ENGINE_TEST_BACKEND`` selects the data
root; default sqlite). Four invariants:

1. placeholder safety — the engine's SQLite-dialect SQL texts carry ``?`` only
   as bind placeholders (never inside string literals) and never a literal
   ``%``, so the PG leg's plain ``?`` → ``%s`` rewrite is exact;
2. strict typing — values bound under a declared column type round-trip to the
   strict Python type on BOTH backends (TEXT eats ints as their text form,
   REAL eats ints as floats, INTEGER eats whole floats as ints — sqlite's
   column-affinity semantics, now explicit in ``_coerce``);
3. the query deadline guard — a query past its timeout aborts with the SAME
   domain-error text on both backends (sqlite progress handler / PG
   statement_timeout);
4. WAL identity continuity — event seqs are strictly increasing and the
   ``after_seq`` cursor semantics hold (AUTOINCREMENT on sqlite, identity
   column on PG — engine-identical).
"""
from __future__ import annotations

import ast
import re
import sys
import uuid
from pathlib import Path

import pytest

import conftest
from daas.fd_daas_mcp import registry

_CD_DIR = Path(__file__).resolve().parents[1] / "customer-dataset-mcp"
sys.path.insert(0, str(_CD_DIR))
try:
    import customer_dataset_tools as tools  # noqa: E402
    import db_dialect  # noqa: E402
finally:
    sys.path.pop(0)

_TIMEOUT_TEXT = "查询超时（>{:.0f}s），请收窄过滤条件或减少数据量"


def _t() -> dict:
    return {
        registry.namespaced(g, n): fn
        for g, n, fn in registry.build(profile="cell")
    }


def _key(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ── 1. placeholder safety ─────────────────────────────────────────────


def _sqlish_string_literals(path: Path) -> list[str]:
    """String literals of the engine module that look like SQL statements
    (adjacent literals are merged by the parser; f-strings are excluded —
    they are the SQL *construction* points, asserted functionally below)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    starters = (
        "SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE",
        "PRAGMA ", "ALTER TABLE", "DROP TABLE", "DROP SCHEMA",
    )
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value.lstrip().upper()
            if s.startswith(starters):
                out.append(node.value)
    return out


def test_no_question_mark_inside_sql_string_literals():
    literals = _sqlish_string_literals(_CD_DIR / "customer_dataset_tools.py")
    assert literals, "static scan found no SQL literals — the scan is broken"
    for lit in literals:
        for quoted in re.findall(r"'[^']*'", lit):
            assert "?" not in quoted, f"literal '?' inside a SQL string literal: {lit!r}"


def test_no_literal_percent_in_engine_sql():
    # a literal % would be misread by psycopg's pyformat binding after the
    # ? -> %s rewrite (the engine never needs one — LIKE patterns arrive as
    # bind parameters)
    for lit in _sqlish_string_literals(_CD_DIR / "customer_dataset_tools.py"):
        assert "%" not in lit, f"literal % in SQL text: {lit!r}"


def test_translate_sql_is_exact_on_representative_statements():
    # representative SQL texts built by the engine's own builders: the ? → %s
    # rewrite must consume every placeholder, inject no stray % and keep the
    # placeholder count equal (params bind 1:1)
    where_pk, params_pk = tools._anchor_where('pk:[1, "a"]', ["id", "name"])
    assert params_pk == [1, "a"]
    cases = [
        where_pk,
        tools._anchor_where("hash:abcd", [])[0],
        tools._build_where(
            [
                {"column": "c", "op": "eq", "value": 1},
                {"column": "c", "op": "in", "values": [1, 2]},
                {"column": "c", "op": "like", "value": "a%"},
                {"column": "c", "op": "isnull"},
            ],
            ["c"],
            {"c": "TEXT"},
        )[0],
        "INSERT INTO cell_event_wal (event_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
        "SELECT columns_json, pk_columns_json, name FROM customer_datasets WHERE key=?",
        "UPDATE customer_dataset_ingests SET status='aborted', finished_at=?,"
        " error='superseded by new ingest' WHERE dataset_key=? AND status='open'",
    ]
    for sql in cases:
        translated, _ = db_dialect.translate_sql(sql, ())
        assert "?" not in translated, f"untranslated placeholder left in {translated!r}"
        assert translated.count("%s") == sql.count("?"), translated
        # a literal % already in the text must be doubled (psycopg placeholder
        # escaping), never left bare and never swallowed
        assert translated.replace("%s", "").count("%") == 2 * sql.count("%"), translated


def test_wal_ddl_is_backend_faithful():
    ddl = db_dialect.wal_table_sql()
    if conftest.TEST_BACKEND == "postgres":
        assert "GENERATED ALWAYS AS IDENTITY" in ddl and "AUTOINCREMENT" not in ddl
    else:
        assert "AUTOINCREMENT" in ddl


# ── 2. strict typing under declared column types ──────────────────────


def test_declared_type_strictness_round_trips():
    key = _key("ty")
    t = _t()
    cols = [
        {"name": "name", "type": "TEXT"},
        {"name": "amount", "type": "REAL"},
        {"name": "qty", "type": "INTEGER"},
    ]
    begun = t["customer_dataset_ingest_begin"](dataset_key=key, columns=cols)
    assert "error" not in begun, begun
    chunked = t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"],
        # loosely-typed upload: int into TEXT, int into REAL, whole float into
        # INTEGER — the affinity-equivalent storage is strict types
        rows=[{"name": 123, "amount": 5, "qty": 2.0}],
        start_index=0,
    )
    assert "error" not in chunked, chunked
    committed = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" not in committed, committed

    row = t["customer_dataset_preview"](dataset_key=key)["rows"][0]
    assert row["name"] == "123" and isinstance(row["name"], str)
    assert row["amount"] == 5.0 and isinstance(row["amount"], float)
    assert row["qty"] == 2 and isinstance(row["qty"], int)

    # the same coercion applies to correction writes (update + insert)
    anchor = row["__row_id"]
    updated = t["customer_dataset_correction_add"](
        dataset_key=key, op="update", anchor=anchor, values={"name": 456}
    )
    assert "error" not in updated, updated
    after = t["customer_dataset_preview"](dataset_key=key)["rows"][0]
    assert after["name"] == "456" and isinstance(after["name"], str)


# ── 3. the query deadline guard (same domain error on both legs) ──────


def test_query_deadline_domain_error_is_identical():
    if conftest.TEST_BACKEND == "postgres":
        slow_sql = "SELECT pg_sleep(3)"
        timeout = 0.05
    else:
        slow_sql = (
            "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt"
            " WHERE x < 50000000) SELECT COUNT(*) FROM cnt"
        )
        timeout = 0.01
    with pytest.raises(tools.CustomerDatasetError, match=re.escape(_TIMEOUT_TEXT.format(timeout))):
        tools._raw_query_timed(slow_sql, timeout=timeout)
    # and a fast query through the same guarded path still succeeds
    assert tools._raw_query_timed("SELECT 1 AS one", timeout=5.0) == [(1,)]


# ── 4. WAL identity continuity ────────────────────────────────────────


def test_wal_seq_strictly_increases_and_cursor_semantics_hold():
    key = _key("wseq")
    t = _t()
    # this file shares the session data root — delimit our own WAL delta via
    # the cursor (the very semantics under test) instead of assuming a virgin WAL
    pre = t["customer_dataset_provenance_wal_pending"]()
    base = pre["last_seq"]
    begun = t["customer_dataset_ingest_begin"](
        dataset_key=key,
        columns=[{"name": "id", "type": "INTEGER"}, {"name": "v", "type": "REAL"}],
        pk_columns=["id"],
    )
    t["customer_dataset_ingest_chunk"](
        ingest_id=begun["ingest_id"], rows=[{"id": 1, "v": 1.0}], start_index=0
    )
    committed = t["customer_dataset_ingest_commit"](ingest_id=begun["ingest_id"])
    assert "error" not in committed, committed

    first = t["customer_dataset_provenance_wal_pending"](after_seq=base)
    assert [e["type"] for e in first["events"]] == ["dataset.snapshot.refreshed"]
    cursor = first["last_seq"]
    assert cursor > base

    for value in (2.0, 3.0):
        added = t["customer_dataset_correction_add"](
            dataset_key=key, op="update", anchor="pk:[1]", values={"v": value}
        )
        assert "error" not in added, added

    page = t["customer_dataset_provenance_wal_pending"](after_seq=cursor)
    seqs = [e["seq"] for e in page["events"]]
    assert [e["type"] for e in page["events"]] == ["dataset.correction.added"] * 2
    assert seqs == sorted(seqs) and len(set(seqs)) == 2  # strictly increasing
    assert all(s > cursor for s in seqs)
    # the seq > cursor query predicate really is exclusive at the boundary
    assert t["customer_dataset_provenance_wal_pending"](after_seq=seqs[-1])["events"] == []
    acked = t["customer_dataset_provenance_wal_ack"](upto_seq=seqs[-1])
    # everything un-acked up to seqs[-1] flips: earlier leftovers + this delta
    assert acked == {"acked": len(pre["events"]) + len(first["events"]) + len(seqs)}

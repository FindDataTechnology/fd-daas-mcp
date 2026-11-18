"""DAAS catalog metadata synchronizer: SQLite source -> data-machine Postgres.

Syncs the catalog-class tables of the DAAS library (this repo's ``daas.db``)
into the ``daas`` database on the Windows data machine Postgres (same host as
the business-mcp federated domains, reachable via the
``FDBIZ_DOMAIN_PG_URL`` connection pattern — per-domain database name on one
server). facet-mcp-foundation-v1 task 4.1; contract:
``openspec/changes/facet-mcp-foundation-v1/specs/daas-supply-domain/spec.md``.

Scope (v1, hardcoded whitelist — never derived from the source schema so a
new source table can never leak into the target):

    sources, daas_functions, indicator_rules, rules

Observation/crawl/state tables (``observations``, ``scraw_*``, ``executions``,
``tasks``, ``schedules``, ``workflow_*``, ...) are deliberately excluded
(v1 supplies metadata only; observations are a second-phase decision).

Sync semantics — **full replace in a single transaction**:

    BEGIN; DELETE (children first); INSERT (parents first); COMMIT;

Rationale (documented per contract): all four source tables *do* have stable
integer primary keys (``id``) and unique business names, so ON CONFLICT upsert
was a candidate — but it cannot capture deletions on the source side (a rule
removed in DAAS would linger in PG forever). The tables are tiny (hundreds of
rows), so full replace is cheap, keeps ``id`` values identical to the source,
and running it inside one transaction makes the swap atomic for readers
(fdbiz_ro never sees an empty or half-updated catalog). Re-running is
idempotent by construction: same source -> same row set, same counts.

The target schema (created once by an operator, mirrored from the SQLite
schema with PG types: VARCHAR kept, bare VARCHAR -> TEXT, JSON -> JSONB,
DATETIME -> TIMESTAMP (naive, as stored), BOOLEAN kept, REAL -> DOUBLE
PRECISION, plus ``synced_at timestamptz NOT NULL DEFAULT now()`` per table)
is NOT managed here — this module only populates rows.

Dependencies: stdlib ``sqlite3`` reads the source; psycopg 3 writes to PG
(install extra ``[sync]``). psycopg is imported lazily so the module stays
importable without it (tests exercise source-read + SQL shape with a stub).

Credentials never enter code — pass the DSN via ``--dsn`` or the
``DAAS_SYNC_PG_DSN`` env var. Use a role with write access to the ``daas``
database only (the postgres superuser or a dedicated sync role);
``fdbiz_ro`` is intentionally read-only and cannot run this sync.

Usage::

    python -m daas.fd_daas_mcp.sync_postgres \
        --source /path/to/daas.db \
        --dsn postgresql://user:password@100.64.0.5:5432/daas

Run: ``fd-daas-mcp/.venv/bin/python -m daas.fd_daas_mcp.sync_postgres --help``
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: Default SQLite source (the DAAS library at the repo layout documented in
#: the workspace CLAUDE.md). Overridable with --source.
DEFAULT_SOURCE = Path("/Users/chengsishi/finddata/DAAS/daas.db")

DSN_ENV = "DAAS_SYNC_PG_DSN"

#: Insert order — parents before children (daas_functions.source_id has an
#: FK to sources in the PG mirror). Deletion runs in the reversed order.
TABLE_ORDER: tuple[str, ...] = ("sources", "daas_functions", "indicator_rules", "rules")


@dataclass(frozen=True)
class TableSpec:
    """One whitelisted table: column order + per-column type adapters.

    ``columns`` is the shared column list used for both the SQLite SELECT and
    the PG INSERT (it matches the PG mirror DDL column order, minus the
    server-side ``synced_at``). ``synced_at`` is left to its DEFAULT now().
    """

    name: str
    columns: tuple[str, ...]
    #: 0/1 integers in SQLite -> real booleans in PG.
    booleans: frozenset[str] = frozenset()
    #: JSON text in SQLite -> parsed at read time (validates well-formedness
    #  early); serialized back + %s::jsonb cast at insert time.
    jsons: frozenset[str] = frozenset()
    #: SQLite DATETIME strings -> datetime (psycopg adapts to TIMESTAMP).
    datetimes: frozenset[str] = frozenset()

    def adapt(self, column: str, value: object) -> object:
        """Convert one SQLite value to its PG-side Python representation."""
        if value is None:
            return None
        if column in self.booleans:
            return bool(value)
        if column in self.jsons:
            return json.loads(value)  # type: ignore[arg-type]
        if column in self.datetimes:
            if isinstance(value, datetime):
                return value
            try:
                return datetime.fromisoformat(str(value))
            except ValueError as exc:
                raise ValueError(
                    f"{self.name}.{column}: not an ISO datetime: {value!r}"
                ) from exc
        return value


TABLES: dict[str, TableSpec] = {
    spec.name: spec
    for spec in (
        TableSpec(
            name="sources",
            columns=("id", "name", "label", "description", "url", "enabled",
                     "config", "created_at", "updated_at", "category_id", "score"),
            booleans=frozenset({"enabled"}),
            jsons=frozenset({"config"}),
            datetimes=frozenset({"created_at", "updated_at"}),
        ),
        TableSpec(
            name="daas_functions",
            columns=("id", "source_id", "name", "label", "description", "category",
                     "parameters", "output_type", "created_at", "updated_at",
                     "frequency"),
            jsons=frozenset({"parameters"}),
            datetimes=frozenset({"created_at", "updated_at"}),
        ),
        TableSpec(
            name="indicator_rules",
            columns=("id", "name", "datasource", "function_name", "source_table",
                     "date_column", "value_column", "op", "params_json",
                     "indicator_name", "enabled", "created_at", "updated_at",
                     "score"),
            booleans=frozenset({"enabled"}),
            jsons=frozenset({"params_json"}),
            datetimes=frozenset({"created_at", "updated_at"}),
        ),
        TableSpec(
            name="rules",
            columns=("id", "name", "rule_type", "target", "config_json",
                     "description", "enabled", "created_at", "updated_at"),
            booleans=frozenset({"enabled"}),
            jsons=frozenset({"config_json"}),
            datetimes=frozenset({"created_at", "updated_at"}),
        ),
    )
}


def read_source(source: str | Path) -> dict[str, list[dict]]:
    """Read the whitelisted tables from the SQLite source.

    Opens the database read-only (``file:...?mode=ro``) — the sync must never
    write to (or create WAL churn in) the live DAAS library. Returns
    ``{table_name: [row_dict, ...]}`` with values already adapted for PG.
    """
    path = Path(source).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"source database not found: {path}")
    uri = f"file:{path}?mode=ro"
    out: dict[str, list[dict]] = {}
    conn = sqlite3.connect(uri, uri=True)
    try:
        for name in TABLE_ORDER:
            spec = TABLES[name]
            col_list = ", ".join(spec.columns)
            rows = conn.execute(
                f"SELECT {col_list} FROM {spec.name} ORDER BY id"  # noqa: S608 — table/column names are the hardcoded whitelist
            ).fetchall()
            out[name] = [
                {col: spec.adapt(col, value) for col, value in zip(spec.columns, row)}
                for row in rows
            ]
    finally:
        conn.close()
    return out


def sync(rows_by_table: dict[str, list[dict]], conn) -> dict[str, int]:
    """Full-replace sync of already-adapted rows into PG over one transaction.

    ``conn`` is an open psycopg 3 connection (injected, so tests can pass a
    stub). All four tables are replaced in a single transaction: readers on
    the ``fdbiz_ro`` side never observe an intermediate state, and the FK
    ``daas_functions.source_id -> sources.id`` is satisfied by the fixed
    delete-children-first / insert-parents-first order. Idempotent: the
    DELETE+INSERT pair converges to exactly the source rows on every run.

    Returns ``{table_name: rows_inserted}``.
    """
    # Fail fast on a stray table name before touching the target.
    unknown = set(rows_by_table) - set(TABLES)
    if unknown:
        raise ValueError(f"tables outside the v1 whitelist: {sorted(unknown)}")

    counts: dict[str, int] = {}
    with conn.transaction():
        for name in reversed(TABLE_ORDER):
            conn.execute(f"DELETE FROM {name}")  # noqa: S608 — whitelist only
        for name in TABLE_ORDER:
            spec = TABLES[name]
            rows = rows_by_table.get(name, [])
            col_list = ", ".join(spec.columns)
            # JSONB columns take serialized text + an explicit cast: psycopg
            # cannot adapt dict/list against a bare %s placeholder, and the
            # cast keeps the wire format driver-visible and testable.
            placeholders = ", ".join(
                f"%s::jsonb" if col in spec.jsons else "%s" for col in spec.columns
            )
            sql = f"INSERT INTO {name} ({col_list}) VALUES ({placeholders})"  # noqa: S608
            payload = [
                tuple(
                    json.dumps(row[col]) if col in spec.jsons and row[col] is not None else row[col]
                    for col in spec.columns
                )
                for row in rows
            ]
            if payload:
                conn.cursor().executemany(sql, payload)
            counts[name] = len(payload)
    return counts


def reconcile(rows_by_table: dict[str, list[dict]], counts: dict[str, int]) -> bool:
    """Source-vs-inserted row-count check (the first-run audit)."""
    return all(len(rows_by_table.get(name, [])) == counts.get(name, -1)
               for name in TABLE_ORDER)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m daas.fd_daas_mcp.sync_postgres",
        description="Sync DAAS catalog tables (whitelisted) to the data-machine Postgres `daas` database.",
    )
    parser.add_argument("--source", default=str(DEFAULT_SOURCE),
                        help=f"SQLite source path (default: {DEFAULT_SOURCE})")
    parser.add_argument("--dsn", default=None,
                        help=f"Postgres DSN for the target `daas` database (default: ${DSN_ENV})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read the source and print per-table counts; do not touch PG")
    args = parser.parse_args(argv)

    rows_by_table = read_source(args.source)
    for name in TABLE_ORDER:
        print(f"source {name}: {len(rows_by_table[name])} rows")
    if args.dry_run:
        return 0

    dsn = args.dsn or os.environ.get(DSN_ENV)
    if not dsn:
        parser.error(f"no DSN: pass --dsn or set ${DSN_ENV}")

    import psycopg  # lazy: keeps the module importable without the [sync] extra

    with psycopg.connect(dsn) as conn:
        counts = sync(rows_by_table, conn)
    ok = reconcile(rows_by_table, counts)
    for name in TABLE_ORDER:
        mark = "ok" if len(rows_by_table[name]) == counts[name] else "MISMATCH"
        print(f"target {name}: {counts[name]} rows [{mark}]")
    print("reconciliation: PASS" if ok else "reconciliation: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

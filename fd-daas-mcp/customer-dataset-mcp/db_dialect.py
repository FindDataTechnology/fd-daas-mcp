"""Backend dialect adapter for the customer-dataset engine
(openspec: wire-cell-postgres-storage).

The engine's SQL surface is SQLite-shaped — ``?`` placeholders, ``PRAGMA
table_info``, ``sqlite_master`` introspection, ``BEGIN IMMEDIATE`` … — because
the cell data root was born as one SQLite file. This module concentrates every
backend difference behind small helpers so ``customer_dataset_tools.py`` stays
dialect-agnostic:

- ``is_postgres()``: derived from ``get_database().engine``'s dialect name,
  cached per engine object (the engine is a singleton per module instance and
  tests re-import modules fresh, so the cache follows the engine identity).
  The sqlite path NEVER imports psycopg.
- ``wrap_cursor(cur)``: sqlite returns the cursor untouched (byte-identical
  behavior); PostgreSQL returns a proxy that (a) rewrites ``?`` → ``%s``,
  (b) rewrites ``PRAGMA table_info(<t>)`` into a pg_catalog query returning
  the exact six-column sqlite shape (cid, name, type, notnull, dflt_value,
  pk — callers index ``r[1]`` for the column name), (c) rewrites
  ``FROM sqlite_master`` into an equivalent pg_catalog subquery aliased
  ``sqlite_master`` (columns ``name``/``type``), (d) supplies ``lastrowid``
  for the engine's ``INSERT INTO customer_dataset_corrections`` sites via
  ``RETURNING id`` and (e) translates psycopg exception classes to their
  sqlite3 counterparts so callers/tests keep one exception vocabulary.
  Everything else delegates to the wrapped cursor.
- transaction / timed-read / DDL / operator helpers used at the engine's
  wiring points (see ``customer_dataset_tools.py``).

Frozen contract: the sqlite path must behave byte-identically to the
pre-adapter engine — every helper returns the legacy value verbatim when the
engine is not PostgreSQL.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

# Same sibling-directory dance as customer_dataset_tools.py: resolve the
# daas-mcp dir for the engine singleton regardless of sys.path order (registry
# load_source pops the group dir when done). The import is bound at MODULE
# time, exactly like the engine's own ``from daas_database import ...`` — the
# registry evicts group modules from sys.modules after each build and pops the
# path entries, so a call-time import would not find it.
_DAAS_MCP_DIR = Path(__file__).resolve().parents[1] / "daas-mcp"
if str(_DAAS_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_DAAS_MCP_DIR))

from daas_database import get_database  # noqa: E402

# ── backend detection ─────────────────────────────────────────────────

_engine_cache: tuple[Any, bool] = (None, False)


def is_postgres() -> bool:
    """True when the cell data root engine is PostgreSQL (postgres+psycopg://)."""
    global _engine_cache
    engine = get_database().engine
    if _engine_cache[0] is not engine:
        _engine_cache = (engine, engine.url.get_dialect().name == "postgresql")
    return _engine_cache[1]


# ── SQL text translation (PostgreSQL leg only) ────────────────────────

# Repo SQL texts never embed a literal ``?`` inside string literals
# (tests/test_dialect_consistency.py pins this), so the global ``?`` → ``%s``
# replace is exact; a literal ``%`` (e.g. a LIKE pattern) is doubled before the
# replace so psycopg's client-side binding keeps it literal.
_PRAGMA_TABLE_INFO_RE = re.compile(r"^\s*PRAGMA\s+table_info\s*\(\s*(.+?)\s*\)\s*;?\s*$", re.IGNORECASE)
_FROM_SQLITE_MASTER_RE = re.compile(r"\bFROM\s+sqlite_master\b", re.IGNORECASE)

# A pg_catalog subquery with sqlite_master's two consumed columns (name, type),
# aliased AS sqlite_master so the engine's (and tests') existing WHERE clauses
# — ``type='table' AND name=?``, ``name LIKE 'cust_x%'`` — run verbatim.
_SQLITE_MASTER_SUBQUERY = (
    "(SELECT c.relname AS name,"
    " CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' WHEN 'v' THEN 'view' END AS type"
    " FROM pg_catalog.pg_class c"
    " JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = current_schema()) AS sqlite_master"
)

# PRAGMA table_info(<t>) replacement: the exact sqlite six-column shape
# (cid, name, type, notnull, dflt_value, pk) in sqlite's ordinal order.
# One parameter: the table name (to_regclass resolves it via the search_path).
_PG_TABLE_INFO_SQL = (
    "SELECT a.attnum - 1 AS cid,"
    " a.attname AS name,"
    " format_type(a.atttypid, a.atttypmod) AS type,"
    " CASE WHEN a.attnotnull THEN 1 ELSE 0 END AS notnull,"
    " pg_get_expr(ad.adbin, a.attrelid) AS dflt_value,"
    " CASE WHEN COALESCE((SELECT i.indisprimary FROM pg_catalog.pg_index i"
    " WHERE i.indrelid = a.attrelid AND a.attnum = ANY(i.indkey) LIMIT 1), false)"
    " THEN 1 ELSE 0 END AS pk"
    " FROM pg_catalog.pg_attribute a"
    " LEFT JOIN pg_catalog.pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum"
    " WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 AND NOT a.attisdropped"
    " ORDER BY a.attnum"
)


def _unquote_ident(arg: str) -> str:
    arg = arg.strip()
    if len(arg) >= 2 and arg[0] == '"' and arg[-1] == '"':
        return arg[1:-1].replace('""', '"')
    return arg


def translate_sql(sql: str, params: tuple | list = ()) -> tuple[str, tuple | list]:
    """PostgreSQL leg: translate the engine's SQLite-dialect SQL text.

    Returns ``(sql, params)``: placeholders become ``%s``, PRAGMA table_info
    becomes the pg_catalog six-column query (injecting its own parameter) and
    ``FROM sqlite_master`` becomes the equivalent pg_catalog subquery. A
    literal ``%`` already present in the text (e.g. a LIKE pattern) is doubled
    first — psycopg's client-side binding reads ``%`` as a placeholder
    introducer, and only the ``%s`` we mint from ``?`` are real placeholders.
    """
    m = _PRAGMA_TABLE_INFO_RE.match(sql)
    if m:
        return _PG_TABLE_INFO_SQL, [_unquote_ident(m.group(1))]
    out = _FROM_SQLITE_MASTER_RE.sub("FROM " + _SQLITE_MASTER_SUBQUERY, sql)
    out = out.replace("%", "%%")
    return out.replace("?", "%s"), params


# ── cursor proxy (PostgreSQL leg only) ────────────────────────────────

# INSERTs whose generated key the engine reads back via cur.lastrowid — the
# psycopg cursor has no lastrowid, so the proxy appends RETURNING id and
# remembers the value.
_LASTROWID_TABLE_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+customer_dataset_corrections\b", re.IGNORECASE
)


def _to_sqlite_exception(exc: BaseException) -> Optional[Exception]:
    """Map a psycopg exception to the sqlite3 exception of the same DB-API
    class name (UniqueViolation → sqlite3.IntegrityError, QueryCanceled →
    sqlite3.OperationalError, …) so engine callers and tests observe one
    vocabulary on both backends. Returns None when ``exc`` is not a psycopg
    error."""
    try:
        import psycopg  # noqa: PLC0415 - PG leg only
    except ImportError:
        return None
    if not isinstance(exc, psycopg.Error):
        return None
    names = {cls.__name__ for cls in type(exc).__mro__}
    for sqlite_cls in (
        sqlite3.IntegrityError,
        sqlite3.OperationalError,
        sqlite3.ProgrammingError,
        sqlite3.DataError,
        sqlite3.InternalError,
        sqlite3.NotSupportedError,
        sqlite3.DatabaseError,
        sqlite3.InterfaceError,
    ):
        if sqlite_cls.__name__ in names:
            return sqlite_cls(str(exc))
    return sqlite3.Error(str(exc))


class _PGCursorProxy:
    """Delegating cursor proxy: intercepts execute/executemany (SQL text
    translation), supplies lastrowid for the corrections INSERTs and
    translates psycopg exceptions to sqlite3 classes. Everything else
    (fetchone/fetchall/rowcount/description/connection/…) passes through."""

    def __init__(self, cur):
        self._cur = cur
        self._last_insert_id: Optional[int] = None

    # – interception surface –
    def execute(self, sql: str, params: tuple | list = ()):  # noqa: ANN001
        sql2, params2 = translate_sql(sql, params)
        returning = (
            " RETURNING id"
            if _LASTROWID_TABLE_RE.match(sql2) and " RETURNING " not in sql2.upper()
            else ""
        )
        try:
            self._cur.execute(sql2 + returning, params2)
        except Exception as exc:  # noqa: BLE001
            mapped = _to_sqlite_exception(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        self._last_insert_id = None
        if returning:
            row = self._cur.fetchone()
            if row is not None:
                self._last_insert_id = int(row[0])
        return self._cur

    def executemany(self, sql: str, seq):  # noqa: ANN001
        sql2, _ = translate_sql(sql)
        try:
            self._cur.executemany(sql2, seq)
        except Exception as exc:  # noqa: BLE001
            mapped = _to_sqlite_exception(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        return self._cur

    @property
    def lastrowid(self) -> Optional[int]:
        """sqlite3 cursor API shim: the id RETURNed by the last corrections
        INSERT (None for statements that did not request one)."""
        return self._last_insert_id

    # – delegation –
    def __getattr__(self, name):
        return getattr(self._cur, name)


def wrap_cursor(cur):
    """sqlite: the cursor itself (behavior byte-identical, no proxy). PG: the
    translating proxy above."""
    if not is_postgres():
        return cur
    return _PGCursorProxy(cur)


# ── timed reads (statement_timeout guard, wired into _raw_query_timed) ─


def pg_query_timed(conn, sql: str, params: tuple | list, timeout_s: float) -> list[tuple]:
    """PostgreSQL equivalent of the sqlite progress-handler deadline: set a
    session-level ``statement_timeout`` before the query and restore the
    previous value after (pooled-connection hygiene). Raises the RAW psycopg
    exception on failure (QueryCanceled on deadline) — the engine maps it to
    the exact domain-error text the sqlite leg produces."""
    cur = conn.cursor()
    cur.execute("SHOW statement_timeout")
    previous = cur.fetchone()[0]
    cur.execute(
        "SELECT set_config('statement_timeout', %s, false)",
        (str(int(float(timeout_s) * 1000)),),
    )
    try:
        sql2, params2 = translate_sql(sql, tuple(params))
        cur.execute(sql2, tuple(params2))
        return cur.fetchall()
    finally:
        try:
            cur.execute("SELECT set_config('statement_timeout', %s, false)", (previous,))
        except Exception:  # noqa: BLE001 - restore is best-effort hygiene
            pass


def is_timeout_error(exc: BaseException) -> bool:
    """True for the psycopg errors the sqlite leg's progress handler would
    have produced (statement deadline → QueryCanceled) plus the lock-class
    OperationalErrors (deadlock / lock not available) that carry the same
    'abort the query, surface an honest domain error' semantics."""
    try:
        import psycopg  # noqa: PLC0415 - PG leg only
        from psycopg import errors as pg_errors  # noqa: PLC0415
    except ImportError:
        return False
    if not isinstance(exc, psycopg.Error):
        return False
    return isinstance(
        exc, (pg_errors.QueryCanceled, pg_errors.DeadlockDetected, pg_errors.LockNotAvailable)
    )


# ── introspection / DDL / operator helpers (dialect-branched) ─────────


def table_exists_sql(name: str) -> tuple[str, list]:
    """The engine's sqlite_master existence probe, parameterized (same rows,
    same semantics as the legacy literal form). Always returned in the
    engine's ``?`` dialect — on the PG leg the statement runs through
    ``wrap_cursor``, whose single-pass translation swaps the catalog and the
    placeholder (never emit ``%s`` here: the proxy would escape it into a
    literal and the parameter would lose its placeholder)."""
    return ("SELECT name FROM sqlite_master WHERE type='table' AND name=?", [name])


_WAL_TABLE_SQL_SQLITE = (
    "CREATE TABLE IF NOT EXISTS cell_event_wal ("
    " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
    " event_id TEXT NOT NULL UNIQUE,"
    " type TEXT NOT NULL,"
    " payload TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " acked INTEGER NOT NULL DEFAULT 0)"
)

_WAL_TABLE_SQL_PG = (
    "CREATE TABLE IF NOT EXISTS cell_event_wal ("
    " seq BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
    " event_id TEXT NOT NULL UNIQUE,"
    " type TEXT NOT NULL,"
    " payload TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " acked INTEGER NOT NULL DEFAULT 0)"
)


def wal_table_sql() -> str:
    """The provenance WAL DDL — sqlite's AUTOINCREMENT verbatim; PostgreSQL's
    identity column (inserts never carry seq, so both are engine-equivalent)."""
    return _WAL_TABLE_SQL_PG if is_postgres() else _WAL_TABLE_SQL_SQLITE


_PG_COLUMN_TYPES = {
    # sqlite INTEGER/REAL are 64-bit; PG INTEGER is int4 and REAL is float4 —
    # the faithful mappings keep value round-trips and row-hash parity.
    "INTEGER": "BIGINT",
    "REAL": "DOUBLE PRECISION",
    "TEXT": "TEXT",
}


def column_type_sql(declared: str) -> str:
    """Declared engine type (INTEGER/REAL/TEXT) → physical column type. sqlite:
    verbatim. PG: 64-bit faithful mappings."""
    if not is_postgres():
        return declared
    return _PG_COLUMN_TYPES.get(declared, declared)


def like_op() -> str:
    """sqlite LIKE is case-insensitive for ASCII — ILIKE is the PostgreSQL
    operator with the same semantics."""
    return "ILIKE" if is_postgres() else "LIKE"


def is_op() -> str:
    """Null-safe key equality: sqlite's bare ``IS`` vs PostgreSQL's
    ``IS NOT DISTINCT FROM`` (plain ``IS`` is a syntax error there)."""
    return "IS NOT DISTINCT FROM" if is_postgres() else "IS"


_NUMERIC_TYPES = {"INTEGER", "REAL"}


def join_key_predicate(left_ref: str, left_type: str, right_ref: str, right_type: str) -> str:
    """The link join's key-equality predicate.

    sqlite: ``<left> IS <right>`` verbatim (column affinity already makes an
    INTEGER 1 compare equal to a TEXT '1'). PostgreSQL has no affinity: when
    one side is numeric and the other TEXT, the numeric side is cast to text —
    reproducing the affinity outcome for well-formed keys (1 ≍ '1') while
    misses (1 vs 'abc') stay misses on both backends."""
    if not is_postgres():
        return f"{left_ref} IS {right_ref}"
    lt = (left_type or "TEXT").upper()
    rt = (right_type or "TEXT").upper()
    if lt in _NUMERIC_TYPES and rt not in _NUMERIC_TYPES:
        return f"CAST({left_ref} AS TEXT) {is_op()} {right_ref}"
    if rt in _NUMERIC_TYPES and lt not in _NUMERIC_TYPES:
        return f"{left_ref} {is_op()} CAST({right_ref} AS TEXT)"
    return f"{left_ref} {is_op()} {right_ref}"


def agg_expr(func: str, quoted_col: str) -> str:
    """Aggregate expression: sqlite verbatim; on PG, avg() over an integer
    column returns numeric (Decimal in Python) — cast to double precision so
    both backends hand the engine a float, exactly like sqlite's avg."""
    if is_postgres() and func == "avg":
        return f"AVG({quoted_col})::double precision"
    return f"{func.upper()}({quoted_col})"

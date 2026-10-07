"""Customer-dataset tools for daas-mcp — wire-customer-local-data (task group 1).

Client-owned data (upload or connector-synced) materialized inside the cell
data root, three-tier physical layout per dataset::

    cust_<key>            effective table  = base snapshot + correction overlay
    cust_<key>__base      raw snapshot     (original values stay queryable)
    cust_<key>__staging   in-flight full snapshot (ingest window)

Corrections (update/delete/insert) are row-anchored: ``pk:<json-array>`` when the
dataset declares primary-key columns, else ``hash:<sha256[:16]>`` over the
canonical row JSON — computed here, uniformly for uploads and connector pushes.
``ingest_commit`` swaps the staging table into the base atomically (single
SQLite transaction), rebuilds the effective table and replays every active
correction; anchors that no longer match are marked dangling (kept, never
silently dropped). The customer source is never written to.

All writes go through one raw sqlite connection per call (explicit
BEGIN IMMEDIATE … COMMIT); the ORM session is used for reads only.

Provenance WAL (wire-provenance-ledger, design D3 — two-stage collection):
every mutation that the platform provenance ledger cares about also appends
one row to ``cell_event_wal`` INSIDE the same transaction as the change
itself, so data and event commit or roll back together (crash ⇒ zero lost
events, zero orphan events). Event types: ``dataset.correction.added`` /
``dataset.correction.reverted`` / ``dataset.correction.dangling`` /
``dataset.snapshot.refreshed`` / ``dataset.deleted``. The platform-side
forwarder drains un-acked rows via ``provenance_wal_pending`` /
``provenance_wal_ack``. The WAL is written UNCONDITIONALLY: the engine does
not know platform provenance tiers — a dataset on the ``off`` tier is
skipped by the platform-side forwarder (which simply does not forward those
events into the ledger), never by the engine (always writing is cheap and
safe). The WAL itself carries no tamper-proofing: immutability is enforced
at the platform ledger; here only the ``acked`` forwarding bookkeeping bit
is mutable by design.

Frozen interface: openspec/changes/wire-customer-local-data/reports/interfaces.md §2.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Group dirs are self-contained (research-mcp precedent: own database accessor),
# but the cell data root is ONE SQLite file shared with the daas group — resolve
# the sibling daas-mcp dir for the engine singleton regardless of sys.path
# order (load_source pops the group dir when done).
_DAAS_MCP_DIR = Path(__file__).resolve().parents[1] / "daas-mcp"
if str(_DAAS_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_DAAS_MCP_DIR))

from daas_database import get_database  # noqa: E402
from models import CustomerDataset, CustomerDatasetCorrection, CustomerDatasetIngest  # noqa: E402

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_COL_RE = re.compile(r'^[^\x00-\x1f"]{1,64}$')
_ENGINE_TYPES = {"INTEGER", "REAL", "TEXT"}
_RESERVED_COL_PREFIX = "__"
DEFAULT_MAX_ROWS = 500_000

_DANGLING_NO_MATCH = "行锚在最新快照中无匹配行"
_DANGLING_NO_COLUMN = "修正涉及的列在新快照中不存在"


class CustomerDatasetError(Exception):
    """Domain error for the customer-dataset tools."""


# ── small helpers ────────────────────────────────────────────────────


def _now_db() -> str:
    """SQLite storage format SQLAlchemy's DateTime type parses back."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _max_rows() -> int:
    try:
        return int(os.environ.get("CUSTOMER_DATASET_MAX_ROWS", DEFAULT_MAX_ROWS))
    except ValueError:
        return DEFAULT_MAX_ROWS


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _coerce(value: Any) -> Any:
    """Normalize a cell value for sqlite3 (bools → int, containers → JSON)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def _norm(value: Any) -> Any:
    """Canonical form for hashing: whole floats collapse to ints so a value's
    identity does not depend on its numeric spelling."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _row_hash(columns: list[str], row: dict) -> str:
    canon = json.dumps(
        [[c, _norm(row.get(c))] for c in columns],
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


# ── provenance WAL (wire-provenance-ledger D3: same-transaction collection) ──

_WAL_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS cell_event_wal ("
    " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
    " event_id TEXT NOT NULL UNIQUE,"
    " type TEXT NOT NULL,"
    " payload TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " acked INTEGER NOT NULL DEFAULT 0)"
)


def _now_wal() -> str:
    """UTC ISO-8601 with Z suffix (event timestamps are platform-facing)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _wal_append(cur, type: str, payload: dict) -> None:
    """Append one provenance event, ON THE CALLER'S OPEN TRANSACTION CURSOR.

    Never opens its own transaction: being inside the caller's ``_tx()`` block
    IS the point — the WAL row and the business change commit or roll back
    together. Lazy table creation follows the group's existing pattern
    (staging tables are CREATE TABLE'd inside ``_tx()`` too); IF NOT EXISTS
    makes every call idempotent and old databases upgrade on first event.
    """
    cur.execute(_WAL_TABLE_SQL)
    cur.execute(
        "INSERT INTO cell_event_wal (event_id, type, payload, created_at)"
        " VALUES (?, ?, ?, ?)",
        (
            "evt_" + uuid.uuid4().hex,
            type,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            _now_wal(),
        ),
    )


def _wal_table_exists(cur) -> bool:
    """True once the WAL table has been created (old DBs before any event)."""
    return (
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cell_event_wal'"
        ).fetchone()
        is not None
    )


def _anchor_where(anchor: str, pk_columns: list[str]) -> tuple[str, list]:
    """anchor → (WHERE sql fragment, params). Raises on unusable anchors."""
    if not isinstance(anchor, str) or not anchor:
        raise CustomerDatasetError("行锚不能为空")
    if anchor.startswith("pk:"):
        if not pk_columns:
            raise CustomerDatasetError("该数据集未声明主键列，不能使用 pk: 行锚")
        try:
            vals = json.loads(anchor[3:])
        except json.JSONDecodeError as exc:
            raise CustomerDatasetError(f"非法 pk 行锚: {anchor!r}") from exc
        if not isinstance(vals, list) or len(vals) != len(pk_columns):
            raise CustomerDatasetError(f"pk 行锚与主键列数不匹配: {anchor!r}")
        where = " AND ".join(f"{_quote(p)} = ?" for p in pk_columns)
        return where, list(vals)
    if anchor.startswith("hash:"):
        return f'{_quote("__row_hash")} = ?', [anchor[5:]]
    if anchor.startswith("ins:"):
        # inserted rows carry their anchor verbatim in __row_hash
        return f'{_quote("__row_hash")} = ?', [anchor]
    raise CustomerDatasetError(f"未知行锚形态: {anchor!r}（应为 pk:… / hash:… / ins:…）")


@contextmanager
def _tx():
    """Explicit BEGIN IMMEDIATE … COMMIT around one raw sqlite connection.

    The sqlite3 module only auto-begins transactions for DML, not DDL — the
    commit path mixes both (RENAME/CREATE + INSERT), so the transaction is
    managed by hand to keep the snapshot swap atomic.
    """
    conn = get_database().engine.raw_connection()
    try:
        conn.isolation_level = None
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
        except BaseException:
            try:
                cur.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - rollback best effort
                pass
            raise
        cur.execute("COMMIT")
    finally:
        conn.close()


def _raw_query_one(sql: str, params: tuple = ()) -> Optional[tuple]:
    conn = get_database().engine.raw_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchone()
    finally:
        conn.close()


def _raw_query_all(sql: str, params: tuple = ()) -> list[tuple]:
    conn = get_database().engine.raw_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def _validate_key(dataset_key: str) -> str:
    if not isinstance(dataset_key, str) or not _KEY_RE.match(dataset_key):
        raise CustomerDatasetError(
            f"非法数据集 key: {dataset_key!r}（要求 ^[a-z][a-z0-9_]{{0,39}}$）"
        )
    return dataset_key


def _validate_columns(columns: list) -> list[dict]:
    if not columns:
        raise CustomerDatasetError("columns 不能为空")
    seen: set[str] = set()
    out: list[dict] = []
    for spec in columns:
        name = (spec or {}).get("name")
        ctype = str((spec or {}).get("type", "")).upper()
        if not isinstance(name, str) or not _COL_RE.match(name):
            raise CustomerDatasetError(f"非法列名: {name!r}")
        if name.startswith(_RESERVED_COL_PREFIX):
            raise CustomerDatasetError(f"列名不能使用保留前缀 {_RESERVED_COL_PREFIX!r}: {name!r}")
        if name in seen:
            raise CustomerDatasetError(f"列名重复: {name!r}")
        if ctype not in _ENGINE_TYPES:
            raise CustomerDatasetError(f"非法列类型 {ctype!r}（允许 {sorted(_ENGINE_TYPES)}）")
        seen.add(name)
        out.append({"name": name, "type": ctype})
    return out


def _table_name(dataset_key: str, suffix: str = "") -> str:
    return f"cust_{dataset_key}{suffix}"


def _meta_raw(cur, dataset_key: str) -> Optional[tuple[list, list, Optional[str]]]:
    """(columns, pk_columns, name) straight from the metadata table, read on the
    caller's own connection (safe inside an open write transaction)."""
    row = cur.execute(
        "SELECT columns_json, pk_columns_json, name FROM customer_datasets WHERE key=?",
        (dataset_key,),
    ).fetchone()
    if row is None:
        return None
    columns = [c["name"] for c in (json.loads(row[0]) if row[0] else [])]
    pk_columns = list(json.loads(row[1])) if row[1] else []
    return columns, pk_columns, row[2]


class CustomerDatasetService:
    """Thin orchestration over raw SQL: one connection + one transaction per
    mutation (data and metadata together, so swaps stay atomic); ORM session
    for reads only."""

    def __init__(self, session):
        self._session = session

    # ── metadata readers (ORM) ───────────────────────────────────────

    def _meta(self, dataset_key: str, required: bool = True) -> Optional[CustomerDataset]:
        row = (
            self._session.query(CustomerDataset)
            .filter(CustomerDataset.key == dataset_key)
            .first()
        )
        if row is None and required:
            raise CustomerDatasetError(f"数据集不存在: {dataset_key!r}")
        return row

    # ── ingest ───────────────────────────────────────────────────────

    def ingest_begin(
        self,
        dataset_key: str,
        columns: list,
        pk_columns: Optional[list] = None,
        row_count: Optional[int] = None,
        actor: str = "",
    ) -> dict:
        dataset_key = _validate_key(dataset_key)
        columns = _validate_columns(columns)
        pk_columns = list(pk_columns or [])
        col_names = [c["name"] for c in columns]
        for pk in pk_columns:
            if pk not in col_names:
                raise CustomerDatasetError(f"主键列不在 columns 中: {pk!r}")

        ingest_id = str(uuid.uuid4())
        staging = _table_name(dataset_key, "__staging")
        now = _now_db()
        with _tx() as cur:
            # any in-flight ingest for this key is superseded
            cur.execute(
                "UPDATE customer_dataset_ingests SET status='aborted', finished_at=?,"
                " error='superseded by new ingest' WHERE dataset_key=? AND status='open'",
                (now, dataset_key),
            )
            cur.execute(f"DROP TABLE IF EXISTS {_quote(staging)}")
            col_defs = ", ".join(f"{_quote(c['name'])} {c['type']}" for c in columns)
            cur.execute(
                f'CREATE TABLE {_quote(staging)} ({col_defs}, {_quote("__row_hash")} TEXT NOT NULL)'
            )
            # register/refresh the dataset metadata now: chunk readers and the
            # commit replay both depend on the declared column list.
            cur.execute(
                "INSERT INTO customer_datasets"
                " (key, name, columns_json, pk_columns_json, row_count, size_bytes, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 0, 0, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET"
                "  columns_json=excluded.columns_json, pk_columns_json=excluded.pk_columns_json,"
                "  updated_at=excluded.updated_at",
                (
                    dataset_key,
                    dataset_key,
                    json.dumps(columns, ensure_ascii=False),
                    json.dumps(pk_columns, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            cur.execute(
                "INSERT INTO customer_dataset_ingests (id, dataset_key, status, row_count, actor, started_at)"
                " VALUES (?, ?, 'open', ?, ?, ?)",
                (ingest_id, dataset_key, row_count, actor, now),
            )
        self._session.expire_all()
        return {"ingest_id": ingest_id, "staging_table": staging}

    def ingest_chunk(self, ingest_id: str, rows: list, start_index: int) -> dict:
        if not isinstance(rows, list) or not rows:
            raise CustomerDatasetError("rows 不能为空")
        with _tx() as cur:
            found = cur.execute(
                "SELECT dataset_key, status FROM customer_dataset_ingests WHERE id=?",
                (ingest_id,),
            ).fetchone()
            if not found:
                raise CustomerDatasetError(f"ingest 不存在: {ingest_id!r}")
            dataset_key, status = found
            if status != "open":
                raise CustomerDatasetError(f"ingest 状态为 {status}，不能写入")
            meta = _meta_raw(cur, dataset_key)
            if meta is None:
                raise CustomerDatasetError("数据集元数据缺失（begin 未成功登记）")
            col_names = meta[0]
            staging = _table_name(dataset_key, "__staging")
            stored = cur.execute(f"SELECT COUNT(*) FROM {_quote(staging)}").fetchone()[0]
            if int(start_index) != stored:
                raise CustomerDatasetError(f"start_index 不符：期望 {stored}，收到 {start_index}")
            inserts: list[tuple] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise CustomerDatasetError("rows 中的元素必须是对象")
                vals = [_coerce(row.get(c)) for c in col_names]
                inserts.append(tuple(vals) + (_row_hash(col_names, row),))
            collist = ", ".join(_quote(c) for c in col_names + ["__row_hash"])
            placeholders = ", ".join("?" for _ in col_names + ["__row_hash"])
            cur.executemany(
                f"INSERT INTO {_quote(staging)} ({collist}) VALUES ({placeholders})", inserts
            )
        return {"accepted": len(rows)}

    def ingest_commit(self, ingest_id: str) -> dict:
        with _tx() as cur:
            found = cur.execute(
                "SELECT dataset_key, status FROM customer_dataset_ingests WHERE id=?",
                (ingest_id,),
            ).fetchone()
            if not found:
                raise CustomerDatasetError(f"ingest 不存在: {ingest_id!r}")
            dataset_key, status = found
            if status != "open":
                raise CustomerDatasetError(f"ingest 状态为 {status}，不能提交")

            staging = _table_name(dataset_key, "__staging")
            base = _table_name(dataset_key, "__base")
            effective = _table_name(dataset_key)
            total = cur.execute(f"SELECT COUNT(*) FROM {_quote(staging)}").fetchone()[0]
            limit = _max_rows()
            if total > limit:
                raise CustomerDatasetError(f"行数 {total} 超过上限 {limit}（拒收，旧快照保留）")

            meta = _meta_raw(cur, dataset_key)
            if meta is None:
                raise CustomerDatasetError("数据集元数据缺失（begin 未成功登记）")
            columns, pk_columns, _name = meta

            # atomic swap: staging → base, then rebuild the effective table
            cur.execute(f"DROP TABLE IF EXISTS {_quote(base)}")
            cur.execute(f'ALTER TABLE {_quote(staging)} RENAME TO {_quote(base)}')
            cur.execute(f"DROP TABLE IF EXISTS {_quote(effective)}")
            cur.execute(f'CREATE TABLE {_quote(effective)} AS SELECT * FROM {_quote(base)}')

            # provenance: the snapshot landed — same transaction as the swap
            _wal_append(
                cur, "dataset.snapshot.refreshed", {"dataset_key": dataset_key, "rows": total}
            )

            replayed, _dangling_now = self._replay_corrections(
                cur, dataset_key, effective, columns, pk_columns
            )
            size_bytes = self._size_of(cur, effective)
            now = _now_db()
            cur.execute(
                "UPDATE customer_datasets SET row_count=?, size_bytes=?, last_commit_at=?, updated_at=?"
                " WHERE key=?",
                (total, size_bytes, now, now, dataset_key),
            )
            self._upsert_softrefs(cur, dataset_key)
            cur.execute(
                "UPDATE customer_dataset_ingests SET status='committed', row_count=?, finished_at=?"
                " WHERE id=?",
                (total, now, ingest_id),
            )
            dangling_total = cur.execute(
                "SELECT COUNT(*) FROM customer_dataset_corrections"
                " WHERE dataset_key=? AND reverted_at IS NULL AND dangling_at IS NOT NULL",
                (dataset_key,),
            ).fetchone()[0]
        self._session.expire_all()
        return {
            "rows": total,
            "size_bytes": size_bytes,
            "corrections_replayed": replayed,
            "corrections_dangling": dangling_total,
        }

    def ingest_abort(self, ingest_id: str) -> dict:
        with _tx() as cur:
            found = cur.execute(
                "SELECT dataset_key, status FROM customer_dataset_ingests WHERE id=?",
                (ingest_id,),
            ).fetchone()
            if not found:
                raise CustomerDatasetError(f"ingest 不存在: {ingest_id!r}")
            dataset_key, status = found
            if status == "open":
                cur.execute(f"DROP TABLE IF EXISTS {_quote(_table_name(dataset_key, '__staging'))}")
                cur.execute(
                    "UPDATE customer_dataset_ingests SET status='aborted', finished_at=? WHERE id=?",
                    (_now_db(), ingest_id),
                )
            elif status != "aborted":
                raise CustomerDatasetError(f"ingest 状态为 {status}，不能中止")
        return {"aborted": True}

    # ── internal: replay / size / softrefs ───────────────────────────

    @staticmethod
    def _size_of(cur, table: str) -> int:
        info = cur.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
        if not info:
            return 0
        expr = " + ".join(f"LENGTH(COALESCE(CAST({_quote(r[1])} AS TEXT), ''))" for r in info)
        return int(cur.execute(f"SELECT COALESCE(SUM({expr}), 0) FROM {_quote(table)}").fetchone()[0])

    def _replay_corrections(
        self, cur, dataset_key: str, effective: str, columns: list[str], pk_columns: list[str]
    ) -> tuple[int, int]:
        """Replay every active correction onto the rebuilt effective table.
        Returns (applied, dangling-after). Successfully replayed corrections
        get their dangling mark cleared."""
        rows = cur.execute(
            "SELECT id, op, anchor, values_json FROM customer_dataset_corrections"
            " WHERE dataset_key=? AND reverted_at IS NULL ORDER BY id",
            (dataset_key,),
        ).fetchall()
        applied = 0
        dangling = 0
        for cid, op, anchor, values_json in rows:
            vals = json.loads(values_json) if values_json else {}
            matched = 0
            reason: Optional[str] = None
            if op == "insert":
                cols = [c for c in vals if c in columns]
                if not cols:
                    reason = _DANGLING_NO_COLUMN
                else:
                    collist = ", ".join(_quote(c) for c in cols + ["__row_hash"])
                    placeholders = ", ".join("?" for _ in cols + ["__row_hash"])
                    cur.execute(
                        f"INSERT INTO {_quote(effective)} ({collist}) VALUES ({placeholders})",
                        [_coerce(vals[c]) for c in cols] + [anchor],
                    )
                    matched = 1
            else:
                where = params = None
                try:
                    where, params = _anchor_where(anchor, pk_columns)
                except CustomerDatasetError as exc:
                    reason = str(exc)
                if where is not None:
                    if op == "update":
                        set_cols = [c for c in vals if c in columns]
                        if not set_cols:
                            reason = _DANGLING_NO_COLUMN
                        else:
                            sets = ", ".join(f"{_quote(c)} = ?" for c in set_cols)
                            cur.execute(
                                f"UPDATE {_quote(effective)} SET {sets} WHERE {where}",
                                [_coerce(vals[c]) for c in set_cols] + list(params),
                            )
                            matched = cur.rowcount
                    else:  # delete
                        cur.execute(f"DELETE FROM {_quote(effective)} WHERE {where}", params)
                        matched = cur.rowcount
            if matched:
                applied += 1
                cur.execute(
                    "UPDATE customer_dataset_corrections"
                    " SET dangling_at=NULL, dangling_reason=NULL WHERE id=?",
                    (cid,),
                )
            else:
                dangling += 1
                reason = reason or _DANGLING_NO_MATCH
                cur.execute(
                    "UPDATE customer_dataset_corrections"
                    " SET dangling_at=?, dangling_reason=? WHERE id=?",
                    (_now_db(), reason, cid),
                )
                # provenance: the correction went dangling in this same
                # transaction (fires from both ingest_commit and revert paths)
                _wal_append(
                    cur,
                    "dataset.correction.dangling",
                    {"dataset_key": dataset_key, "correction_id": cid, "reason": reason},
                )
        return applied, dangling

    @staticmethod
    def _upsert_softrefs(cur, dataset_key: str) -> None:
        """Soft-reference metadata rows so the rule chain (sources.name) and
        the dashboard table browser (datasources) both see the dataset."""
        name = _table_name(dataset_key)
        db_url = str(get_database().engine.url)
        now = _now_db()
        cur.execute(
            "INSERT INTO sources (name, label, description, enabled, config, created_at, updated_at)"
            " VALUES (?, ?, ?, 1, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET description=excluded.description, updated_at=excluded.updated_at",
            (name, dataset_key, "客户数据集（wire-customer-local-data）", json.dumps({"kind": "customer_dataset"}), now, now),
        )
        cur.execute(
            "INSERT INTO datasources (name, db_type, connection_string, description, is_readonly, created_at, updated_at)"
            " VALUES (?, 'sqlite', ?, ?, 1, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET connection_string=excluded.connection_string, updated_at=excluded.updated_at",
            (name, db_url, "客户数据集（wire-customer-local-data）", now, now),
        )

    # ── read surface ─────────────────────────────────────────────────

    def overview(self, dataset_key: str) -> dict:
        meta = self._meta(_validate_key(dataset_key))
        return meta.to_dict()

    def preview(
        self, dataset_key: str, limit: int = 50, offset: int = 0, include_corrected: bool = True
    ) -> dict:
        dataset_key = _validate_key(dataset_key)
        meta = self._meta(dataset_key)
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        table = _table_name(dataset_key) if include_corrected else _table_name(dataset_key, "__base")
        columns = [c["name"] for c in (meta.columns_json or [])]
        pk_columns = list(meta.pk_columns_json or [])
        collist = ", ".join(_quote(c) for c in columns + ["__row_hash"])
        rows = _raw_query_all(
            f"SELECT {collist} FROM {_quote(table)} LIMIT ? OFFSET ?", (limit, offset)
        )
        total = _raw_query_one(f"SELECT COUNT(*) FROM {_quote(table)}")[0]
        out_rows = []
        for raw in rows:
            record = {c: raw[i] for i, c in enumerate(columns)}
            row_hash = str(raw[len(columns)])
            if row_hash.startswith("ins:"):
                anchor = row_hash  # inserted rows keep their correction anchor
            elif pk_columns:
                anchor = "pk:" + json.dumps(
                    [record.get(p) for p in pk_columns],
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
            else:
                anchor = "hash:" + row_hash
            out_rows.append({**record, "__row_id": anchor})
        return {
            "columns": meta.columns_json or [{"name": c, "type": "TEXT"} for c in columns],
            "rows": out_rows,
            "total": int(total),
        }

    # ── corrections ──────────────────────────────────────────────────

    def correction_add(
        self,
        dataset_key: str,
        op: str,
        anchor: Optional[str],
        values: Optional[dict],
        actor: str = "",
    ) -> dict:
        dataset_key = _validate_key(dataset_key)
        meta = self._meta(dataset_key)
        columns = [c["name"] for c in (meta.columns_json or [])]
        pk_columns = list(meta.pk_columns_json or [])
        op = (op or "").strip()
        if op not in ("update", "delete", "insert"):
            raise CustomerDatasetError(f"非法修正类型: {op!r}")
        values = dict(values or {})
        for c in values:
            if c not in columns:
                raise CustomerDatasetError(f"列不存在: {c!r}")
        if op in ("update", "delete") and not anchor:
            raise CustomerDatasetError(f"{op} 修正必须携带行锚")
        if op in ("update", "insert") and not values:
            raise CustomerDatasetError(f"{op} 修正必须携带 values")

        effective = _table_name(dataset_key)
        now = _now_db()
        with _tx() as cur:
            if op == "update":
                where, params = _anchor_where(anchor, pk_columns)
                cur.execute(f"SELECT * FROM {_quote(effective)} WHERE {where} LIMIT 1", params)
                old = cur.fetchone()
                old_values = None
                if old is not None:
                    old_cols = [d[0] for d in cur.description]
                    old_values = {k: v for k, v in zip(old_cols, old) if k != "__row_hash"}
                sets = ", ".join(f"{_quote(c)} = ?" for c in values)
                cur.execute(
                    f"UPDATE {_quote(effective)} SET {sets} WHERE {where}",
                    [_coerce(values[c]) for c in values] + list(params),
                )
                matched = cur.rowcount
                cur.execute(
                    "INSERT INTO customer_dataset_corrections"
                    " (dataset_key, op, anchor, values_json, old_values_json, actor, created_at, dangling_at, dangling_reason)"
                    " VALUES (?, 'update', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        dataset_key,
                        anchor,
                        json.dumps(values, ensure_ascii=False),
                        json.dumps(old_values, ensure_ascii=False) if old_values is not None else None,
                        actor,
                        now,
                        None if matched else now,
                        None if matched else _DANGLING_NO_MATCH,
                    ),
                )
                correction_id = cur.lastrowid
            elif op == "delete":
                where, params = _anchor_where(anchor, pk_columns)
                cur.execute(f"DELETE FROM {_quote(effective)} WHERE {where}", params)
                matched = cur.rowcount
                cur.execute(
                    "INSERT INTO customer_dataset_corrections"
                    " (dataset_key, op, anchor, actor, created_at, dangling_at, dangling_reason)"
                    " VALUES (?, 'delete', ?, ?, ?, ?, ?)",
                    (
                        dataset_key,
                        anchor,
                        actor,
                        now,
                        None if matched else now,
                        None if matched else _DANGLING_NO_MATCH,
                    ),
                )
                correction_id = cur.lastrowid
            else:  # insert — two-step to mint ins:<correction_id>
                cur.execute(
                    "INSERT INTO customer_dataset_corrections"
                    " (dataset_key, op, anchor, values_json, actor, created_at)"
                    " VALUES (?, 'insert', '', ?, ?, ?)",
                    (dataset_key, json.dumps(values, ensure_ascii=False), actor, now),
                )
                correction_id = cur.lastrowid
                anchor = f"ins:{correction_id}"
                cur.execute(
                    "UPDATE customer_dataset_corrections SET anchor=? WHERE id=?",
                    (anchor, correction_id),
                )
                collist = ", ".join(_quote(c) for c in values) + f", {_quote('__row_hash')}"
                placeholders = ", ".join("?" for _ in values) + ", ?"
                cur.execute(
                    f"INSERT INTO {_quote(effective)} ({collist}) VALUES ({placeholders})",
                    [_coerce(values[c]) for c in values] + [anchor],
                )
            # provenance: correction recorded + applied — same transaction.
            # (For inserts `anchor` is the minted ins:<correction_id> by now.)
            _wal_append(
                cur,
                "dataset.correction.added",
                {
                    "dataset_key": dataset_key,
                    "op": op,
                    "anchor": anchor,
                    "correction_id": correction_id,
                    "actor": actor,
                },
            )
        self._session.expire_all()
        return {"correction_id": correction_id}

    def correction_list(
        self, dataset_key: str, include_reverted: bool = False, include_dangling: bool = True
    ) -> dict:
        dataset_key = _validate_key(dataset_key)
        self._meta(dataset_key)
        q = self._session.query(CustomerDatasetCorrection).filter(
            CustomerDatasetCorrection.dataset_key == dataset_key
        )
        if not include_reverted:
            q = q.filter(CustomerDatasetCorrection.reverted_at.is_(None))
        if not include_dangling:
            q = q.filter(CustomerDatasetCorrection.dangling_at.is_(None))
        rows = q.order_by(CustomerDatasetCorrection.id.asc()).all()
        return {"corrections": [r.to_dict() for r in rows]}

    def correction_dangling(self, dataset_key: str) -> dict:
        dataset_key = _validate_key(dataset_key)
        self._meta(dataset_key)
        rows = (
            self._session.query(CustomerDatasetCorrection)
            .filter(
                CustomerDatasetCorrection.dataset_key == dataset_key,
                CustomerDatasetCorrection.reverted_at.is_(None),
                CustomerDatasetCorrection.dangling_at.is_not(None),
            )
            .order_by(CustomerDatasetCorrection.id.asc())
            .all()
        )
        return {"corrections": [r.to_dict() for r in rows]}

    def correction_revert(self, dataset_key: str, correction_id: int, actor: str = "") -> dict:
        dataset_key = _validate_key(dataset_key)
        meta = self._meta(dataset_key)
        columns = [c["name"] for c in (meta.columns_json or [])]
        pk_columns = list(meta.pk_columns_json or [])
        base = _table_name(dataset_key, "__base")
        effective = _table_name(dataset_key)
        with _tx() as cur:
            found = cur.execute(
                "SELECT id FROM customer_dataset_corrections"
                " WHERE id=? AND dataset_key=? AND reverted_at IS NULL",
                (int(correction_id), dataset_key),
            ).fetchone()
            if not found:
                raise CustomerDatasetError(f"修正不存在或已回滚: {correction_id}")
            cur.execute(
                "UPDATE customer_dataset_corrections SET reverted_at=? WHERE id=?",
                (_now_db(), int(correction_id)),
            )
            # provenance: revert recorded — same transaction
            _wal_append(
                cur,
                "dataset.correction.reverted",
                {"dataset_key": dataset_key, "correction_id": int(correction_id), "actor": actor},
            )
            exists = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (base,)
            ).fetchone()
            if exists is not None:
                cur.execute(f"DROP TABLE IF EXISTS {_quote(effective)}")
                cur.execute(f'CREATE TABLE {_quote(effective)} AS SELECT * FROM {_quote(base)}')
                self._replay_corrections(cur, dataset_key, effective, columns, pk_columns)
                size_bytes = self._size_of(cur, effective)
                cur.execute(
                    "UPDATE customer_datasets SET size_bytes=?, updated_at=? WHERE key=?",
                    (size_bytes, _now_db(), dataset_key),
                )
        self._session.expire_all()
        return {"reverted": True}

    # ── lifecycle ────────────────────────────────────────────────────

    def delete(self, dataset_key: str, actor: str = "") -> dict:
        dataset_key = _validate_key(dataset_key)
        name = _table_name(dataset_key)
        now = _now_db()
        with _tx() as cur:
            for suffix in ("", "__base", "__staging"):
                cur.execute(f"DROP TABLE IF EXISTS {_quote(_table_name(dataset_key, suffix))}")
            cur.execute(
                "INSERT INTO customer_dataset_ingests (id, dataset_key, status, started_at, finished_at, actor, error)"
                " VALUES (?, ?, 'deleted', ?, ?, ?, ?)",
                (str(uuid.uuid4()), dataset_key, now, now, actor, "dataset deleted"),
            )
            cur.execute("DELETE FROM customer_dataset_corrections WHERE dataset_key=?", (dataset_key,))
            cur.execute("DELETE FROM customer_datasets WHERE key=?", (dataset_key,))
            cur.execute("DELETE FROM sources WHERE name=?", (name,))
            cur.execute("DELETE FROM datasources WHERE name=?", (name,))
            # provenance: the dataset is gone but its WAL row survives this
            # transaction — the deleted event is exactly what the ledger wants
            _wal_append(cur, "dataset.deleted", {"dataset_key": dataset_key, "actor": actor})
        self._session.expire_all()
        return {"deleted": True}


# ── MCP tool wrappers (registered in server.py) ──────────────────────


def _svc() -> CustomerDatasetService:
    return CustomerDatasetService(get_database().get_session())


def _ok(result: dict) -> dict:
    return result


def _err(e: Exception) -> dict:
    return {"success": False, "error": str(e)}


def ingest_begin(
    dataset_key: str,
    columns: list,
    pk_columns: Optional[list] = None,
    row_count: Optional[int] = None,
    actor: str = "",
) -> dict:
    """Begin a full-snapshot ingest for a customer dataset (registers/refreshes
    the dataset metadata and creates the staging table). Supersedes any
    in-flight ingest for the same key.

    Args:
        dataset_key: Slug `^[a-z][a-z0-9_]{0,39}$`, unique per cell.
        columns: Column declarations `[{name, type}]`; type ∈ INTEGER/REAL/TEXT.
        pk_columns: Optional primary-key column names (row anchor: pk-first).
        row_count: Optional expected total row count (informational).
        actor: Who initiated the ingest (audit).
    """
    svc = _svc()
    try:
        return _ok(svc.ingest_begin(dataset_key, columns, pk_columns=pk_columns, row_count=row_count, actor=actor))
    except Exception as e:
        return _err(e)


def ingest_chunk(ingest_id: str, rows: list, start_index: int) -> dict:
    """Append one chunk of rows to an open ingest. `start_index` must equal the
    number of rows already staged (guards against replays)."""
    svc = _svc()
    try:
        return _ok(svc.ingest_chunk(ingest_id, rows, start_index))
    except Exception as e:
        return _err(e)


def ingest_commit(ingest_id: str) -> dict:
    """Atomically swap the staged snapshot into the base table, rebuild the
    effective table and replay all active corrections (dangling anchors are
    marked, never dropped). Returns rows/size/corrections stats."""
    svc = _svc()
    try:
        return _ok(svc.ingest_commit(ingest_id))
    except Exception as e:
        return _err(e)


def ingest_abort(ingest_id: str) -> dict:
    """Abort an open ingest and drop its staging table (base/effective stay)."""
    svc = _svc()
    try:
        return _ok(svc.ingest_abort(ingest_id))
    except Exception as e:
        return _err(e)


def overview(dataset_key: str) -> dict:
    """Dataset metadata: columns, pk_columns, rows, size_bytes, last_commit_at."""
    svc = _svc()
    try:
        return _ok(svc.overview(dataset_key))
    except Exception as e:
        return _err(e)


def preview(
    dataset_key: str, limit: int = 50, offset: int = 0, include_corrected: bool = True
) -> dict:
    """Paged row preview with `__row_id` anchors. `include_corrected=false`
    reads the raw snapshot (original values stay queryable)."""
    svc = _svc()
    try:
        return _ok(svc.preview(dataset_key, limit=limit, offset=offset, include_corrected=include_corrected))
    except Exception as e:
        return _err(e)


def correction_add(
    dataset_key: str, op: str, anchor: Optional[str] = None, values: Optional[dict] = None, actor: str = ""
) -> dict:
    """Add an overlay correction (update/delete/insert); it applies to the
    effective table immediately and is replayed after every future commit."""
    svc = _svc()
    try:
        return _ok(svc.correction_add(dataset_key, op, anchor, values, actor=actor))
    except Exception as e:
        return _err(e)


def correction_list(
    dataset_key: str, include_reverted: bool = False, include_dangling: bool = True
) -> dict:
    """Correction history (audit trail) for a dataset."""
    svc = _svc()
    try:
        return _ok(svc.correction_list(dataset_key, include_reverted=include_reverted, include_dangling=include_dangling))
    except Exception as e:
        return _err(e)


def correction_dangling(dataset_key: str) -> dict:
    """Corrections whose row anchor no longer matches the latest snapshot."""
    svc = _svc()
    try:
        return _ok(svc.correction_dangling(dataset_key))
    except Exception as e:
        return _err(e)


def correction_revert(dataset_key: str, correction_id: int, actor: str = "") -> dict:
    """Revert one correction (mark reverted; rebuild the effective table from
    the base snapshot plus the remaining active corrections)."""
    svc = _svc()
    try:
        return _ok(svc.correction_revert(dataset_key, correction_id, actor=actor))
    except Exception as e:
        return _err(e)


def delete(dataset_key: str, actor: str = "") -> dict:
    """Delete a dataset: drop its three tables, metadata, corrections and
    soft-reference rows. Ingest audit rows are kept."""
    svc = _svc()
    try:
        return _ok(svc.delete(dataset_key, actor=actor))
    except Exception as e:
        return _err(e)


# ── provenance WAL forwarder tools (wire-provenance-ledger D3) ───────


def provenance_wal_pending(after_seq: int = 0, limit: int = 200) -> dict:
    """Read un-forwarded WAL events for the platform-side provenance
    forwarder: rows with ``acked = 0`` and ``seq > after_seq``, ascending by
    ``seq`` (cursor style — hand the returned ``last_seq`` to
    ``provenance_wal_ack``, then keep polling with it as ``after_seq``).
    ``limit`` is clamped to [1, 1000]. Events stay readable until acked, so a
    forwarder crash between read and ack loses nothing. On a database that
    predates the WAL (table not yet created) returns an empty page.

    Args:
        after_seq: Exclusive lower seq bound (forwarder cursor).
        limit: Max events per page, clamped to [1, 1000].
    """
    try:
        after_seq = max(0, int(after_seq))
        limit = max(1, min(int(limit), 1000))
        with _tx() as cur:  # own transaction — never shares a business one
            rows: list = []
            if _wal_table_exists(cur):
                rows = cur.execute(
                    "SELECT seq, event_id, type, payload, created_at FROM cell_event_wal"
                    " WHERE seq > ? AND acked = 0 ORDER BY seq ASC LIMIT ?",
                    (after_seq, limit),
                ).fetchall()
        events = [
            {
                "seq": int(r[0]),
                "event_id": r[1],
                "type": r[2],
                "payload": json.loads(r[3]),
                "created_at": r[4],
            }
            for r in rows
        ]
        return {"events": events, "last_seq": events[-1]["seq"] if events else after_seq}
    except Exception as e:
        return _err(e)


def provenance_wal_ack(upto_seq: int) -> dict:
    """Ack WAL events up to and including ``upto_seq`` (forwarder confirms
    they are durably in the platform ledger). Idempotent: re-acking returns
    ``{"acked": 0}``. Flipping the ``acked`` bit is the only mutation the WAL
    ever allows — tamper-proofing lives at the platform ledger, not here.

    Args:
        upto_seq: Highest seq to ack (typically pending's ``last_seq``).
    """
    try:
        with _tx() as cur:  # own transaction — never shares a business one
            if not _wal_table_exists(cur):
                return {"acked": 0}
            cur.execute(
                "UPDATE cell_event_wal SET acked = 1 WHERE seq <= ? AND acked = 0",
                (int(upto_seq),),
            )
            return {"acked": int(cur.rowcount)}
    except Exception as e:
        return _err(e)
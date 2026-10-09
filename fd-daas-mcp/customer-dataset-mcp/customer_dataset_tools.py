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
import sqlite3
import sys
import time
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

# ── structured query surface (wire-customer-data-engine D1) ──────────
# Whitelisted filter operators → parameterized SQL fragments only; values are
# never interpolated into SQL text. The row cap and the progress-handler
# timeout are the two guards every query path goes through.
_QUERY_OPS = frozenset(
    {"eq", "ne", "gt", "gte", "lt", "lte", "in", "notin", "like", "isnull", "notnull"}
)
_QUERY_OPS_SQL = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_AGG_FUNCS = frozenset({"count", "sum", "avg", "min", "max"})
QUERY_ROW_CAP = 1000
QUERY_TIMEOUT_S = 5.0

# ── write surface (wire-customer-data-engine §2, ADR-0006) ───────────
#: every edit record names its write channel (console member / data-plane
#: key / agent) — one overlay mechanism, channel-attributed semantics
_CHANNELS = frozenset({"console", "data-key", "agent"})
WRITE_BATCH_CAP = 1000

#: advertised by ``overview`` — the fd-wire data plane probes this instead of
#: guessing which engine tools exist (design D1; task 1.3)
ENGINE_CAPABILITIES = {
    "query": True,
    "aggregate": True,
    "write_batch": True,
    "create_declared": True,
    "add_column": True,
    "derive": True,
    "derive_refresh": True,
    "join_indicators": True,
}


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


def _build_where(filters: Optional[list], columns: list[str]) -> tuple[str, list]:
    """filters → (WHERE sql fragment, params). AND composition only (v1);
    every operator is whitelisted and every value parameterized."""
    clauses: list[str] = []
    params: list = []
    for f in filters or []:
        if not isinstance(f, dict):
            raise CustomerDatasetError(f"过滤条件必须是对象: {f!r}")
        col = f.get("column")
        op = str(f.get("op", "")).lower()
        if col not in columns:
            raise CustomerDatasetError(f"过滤列不存在: {col!r}")
        if op not in _QUERY_OPS:
            raise CustomerDatasetError(f"非法过滤操作符: {op!r}（允许 {sorted(_QUERY_OPS)}）")
        qcol = _quote(col)
        if op in ("isnull", "notnull"):
            clauses.append(f"{qcol} IS NULL" if op == "isnull" else f"{qcol} IS NOT NULL")
        elif op in ("in", "notin"):
            vals = f.get("values", f.get("value"))
            if not isinstance(vals, list) or not vals:
                raise CustomerDatasetError(f"{op} 过滤的 values 必须为非空数组")
            ph = ", ".join("?" for _ in vals)
            sql_op = "IN" if op == "in" else "NOT IN"
            clauses.append(f"{qcol} {sql_op} ({ph})")
            params.extend(_coerce(v) for v in vals)
        elif op == "like":
            clauses.append(f"{qcol} LIKE ?")
            params.append(str(f.get("value")))
        else:
            clauses.append(f"{qcol} {_QUERY_OPS_SQL[op]} ?")
            params.append(_coerce(f.get("value")))
    return (" AND ".join(clauses), params) if clauses else ("", [])


def _build_order(sort: Optional[list], columns: list[str]) -> str:
    if not sort:
        return ""
    parts: list[str] = []
    for s in sort:
        if not isinstance(s, dict):
            raise CustomerDatasetError(f"排序项必须是对象: {s!r}")
        col = s.get("column")
        direction = str(s.get("dir", "asc")).lower()
        if col not in columns:
            raise CustomerDatasetError(f"排序列不存在: {col!r}")
        if direction not in ("asc", "desc"):
            raise CustomerDatasetError(f"非法排序方向: {direction!r}（允许 asc/desc）")
        parts.append(f"{_quote(col)} {direction.upper()}")
    return " ORDER BY " + ", ".join(parts)


def _raw_query_timed(
    sql: str, params: tuple | list = (), timeout: float = QUERY_TIMEOUT_S
) -> list[tuple]:
    """Read with a progress-handler deadline: a query that runs past
    ``timeout`` seconds aborts and surfaces as an honest domain error instead
    of pinning the cell's single writer."""
    conn = get_database().engine.raw_connection()
    try:
        deadline = time.monotonic() + float(timeout)
        timed_out = False

        def _guard() -> int:
            nonlocal timed_out
            if time.monotonic() >= deadline:
                timed_out = True
                return 1
            return 0

        conn.set_progress_handler(_guard, 10_000)
        cur = conn.cursor()
        try:
            cur.execute(sql, tuple(params))
            return cur.fetchall()
        except sqlite3.OperationalError as exc:
            if timed_out:
                raise CustomerDatasetError(
                    f"查询超时（>{float(timeout):.0f}s），请收窄过滤条件或减少数据量"
                ) from exc
            raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def _anchor_for(record: dict, row_hash: str, pk_columns: list[str]) -> str:
    """Row anchor exactly as ``preview`` computes it (pk-first, hash fallback,
    inserted rows keep their minted ``ins:`` anchor)."""
    if row_hash.startswith("ins:"):
        return row_hash
    if pk_columns:
        return "pk:" + json.dumps(
            [record.get(p) for p in pk_columns],
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    return "hash:" + row_hash


# ── indicator gateway (§4: platform series via the wire entrance) ────


class IndicatorGatewayError(CustomerDatasetError):
    """The join channel could not resolve or fetch platform indicator data."""


JOIN_MAX_CODES = 10
JOIN_MAX_REGIONS = 64


def _join_date_periods(value: Any) -> tuple[str, str, str]:
    """A row date → (exact 'YYYY-MM-DD', month 'YYYY-MM', year 'YYYY').
    Tolerates TEXT dates stored with a time component."""
    s = str(value or "")
    return s[:10], s[:7], s[:4]


class _EntranceGateway:
    """Platform-indicator fetches through the wire entrance (registry), with
    the cell's own long-lived key — the registry meters those reads to this
    customer, the same track as artifact materialization (design D4, Q5).
    HTTP never happens inside a sqlite write transaction (§4 call order)."""

    def __init__(self, url: str, key: str) -> None:
        self._url = url
        self._key = key

    def _call(self, tool: str, args: dict) -> Any:
        import asyncio

        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        async def _run() -> Any:
            transport = StreamableHttpTransport(
                self._url, headers={"Authorization": f"Bearer {self._key}"}
            )
            async with Client(transport) as client:
                result = await client.call_tool(tool, args)
            data = getattr(result, "data", None)
            if data is not None:
                return data
            content = getattr(result, "content", None) or []
            texts = [getattr(item, "text", None) for item in content]
            texts = [t for t in texts if t is not None]
            if not texts:
                return None
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return texts[0]

        try:
            return asyncio.run(_run())
        except IndicatorGatewayError:
            raise
        except Exception as exc:  # noqa: BLE001 — transport/protocol errors, honestly wrapped
            raise IndicatorGatewayError(
                f"指标拉取通道调用失败（{tool}）: {type(exc).__name__}: {exc}"
            ) from exc

    def resolve_codes(self, codes: list[str]) -> dict[str, int]:
        """code → concept_id via the entrance's concept catalog."""
        concepts = self._call("list_concepts", {})
        if not isinstance(concepts, list):
            raise IndicatorGatewayError(f"list_concepts 返回形态异常: {type(concepts).__name__}")
        by_code: dict[str, int] = {}
        for c in concepts:
            if isinstance(c, dict) and c.get("code") and c.get("id") is not None:
                by_code[str(c["code"])] = int(c["id"])
        missing = [c for c in codes if c not in by_code]
        if missing:
            raise IndicatorGatewayError(f"指标 code 未在目录中解析到: {missing}")
        return {c: by_code[c] for c in codes}

    def resolve_entity(self, name: str) -> Optional[tuple[str, int]]:
        """region name → (entity_type, entity_id); None when unresolvable."""
        out = self._call("daas_search_entities", {"keyword": name, "limit": 1})
        results = out.get("results") if isinstance(out, dict) else None
        if not results:
            return None
        first = results[0]
        return str(first.get("entity_type", "")), int(first.get("id"))

    def read_range(
        self, concept_ids: list[int], entity_type: str, entity_id: int, start: str, end: str
    ) -> dict[int, list[dict]]:
        out = self._call(
            "read_range",
            {
                "concept_ids": concept_ids,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "start": start,
                "end": end,
            },
        )
        if not isinstance(out, dict):
            raise IndicatorGatewayError("read_range 返回形态异常")
        return {int(k): v for k, v in out.items()}


_gateway_factory = None  # tests inject a fake; production leaves this None


def _get_indicator_gateway():
    if _gateway_factory is not None:
        return _gateway_factory()
    url = os.environ.get("FD_WIRE_ENTRANCE_URL", "")
    key = os.environ.get("FD_CELL_WGK_KEY", "")
    if not url or not key:
        raise IndicatorGatewayError(
            "指标拉取通道未配置（FD_WIRE_ENTRANCE_URL / FD_CELL_WGK_KEY），无法执行指标连接派生"
        )
    return _EntranceGateway(url, key)


def _join_series_index(points: list[dict]) -> dict[str, Any]:
    """Indicator points → period lookup map. Frequency is inferred from the
    points' own shape: a point on day-01 contributes its month key (monthly
    series), a point on Jan-01 contributes its year key (annual series);
    every point contributes its exact date. Daily series (various days)
    therefore only match exactly — no neighbor-day bleed. Lookup order at
    join time is exact → month → year."""
    index: dict[str, Any] = {}
    for p in points or []:
        if not isinstance(p, dict) or p.get("value") is None:
            continue
        exact, month, year = _join_date_periods(p.get("date"))
        index.setdefault(exact, p["value"])
        if exact[8:10] == "01":
            index.setdefault(month, p["value"])
        if exact[5:10] == "01-01":
            index.setdefault(year, p["value"])
    return index


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
        name: Optional[str] = None,
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
                    name or dataset_key,
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
        return {**meta.to_dict(), "capabilities": dict(ENGINE_CAPABILITIES)}

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
            out_rows.append(
                {**record, "__row_id": _anchor_for(record, str(raw[len(columns)]), pk_columns)}
            )
        return {
            "columns": meta.columns_json or [{"name": c, "type": "TEXT"} for c in columns],
            "rows": out_rows,
            "total": int(total),
        }

    def query(
        self,
        dataset_key: str,
        filters: Optional[list] = None,
        sort: Optional[list] = None,
        limit: int = 100,
        offset: int = 0,
        count_only: bool = False,
        distinct_column: Optional[str] = None,
        raw: bool = False,
    ) -> dict:
        """Structured query over the effective table (``raw=True`` reads the
        base snapshot). Filters AND-compose; every operator whitelisted;
        response rows capped at QUERY_ROW_CAP with an honest ``truncated``
        flag and the exact ``total``."""
        dataset_key = _validate_key(dataset_key)
        meta = self._meta(dataset_key)
        columns = [c["name"] for c in (meta.columns_json or [])]
        pk_columns = list(meta.pk_columns_json or [])
        table = _table_name(dataset_key, "__base" if raw else "")
        where, params = _build_where(filters, columns)
        where_sql = f" WHERE {where}" if where else ""

        if distinct_column is not None:
            if distinct_column not in columns:
                raise CustomerDatasetError(f"去重列不存在: {distinct_column!r}")
            qcol = _quote(distinct_column)
            vals = [
                r[0]
                for r in _raw_query_timed(
                    f"SELECT DISTINCT {qcol} FROM {_quote(table)}{where_sql}"
                    f" ORDER BY 1 LIMIT {QUERY_ROW_CAP}",
                    params,
                )
            ]
            total = _raw_query_timed(
                f"SELECT COUNT(DISTINCT {qcol}) FROM {_quote(table)}{where_sql}", params
            )[0][0]
            return {
                "column": distinct_column,
                "values": vals,
                "count": int(total),
                "truncated": int(total) > len(vals),
            }

        if count_only:
            total = _raw_query_timed(
                f"SELECT COUNT(*) FROM {_quote(table)}{where_sql}", params
            )[0][0]
            return {"count": int(total)}

        limit = max(1, min(int(limit), QUERY_ROW_CAP))
        offset = max(0, int(offset))
        order_sql = _build_order(sort, columns)
        collist = ", ".join(_quote(c) for c in columns + ["__row_hash"])
        rows = _raw_query_timed(
            f"SELECT {collist} FROM {_quote(table)}{where_sql}{order_sql}"
            f" LIMIT ? OFFSET ?",
            list(params) + [limit, offset],
        )
        total = _raw_query_timed(
            f"SELECT COUNT(*) FROM {_quote(table)}{where_sql}", params
        )[0][0]
        out_rows = []
        for raw_row in rows:
            record = {c: raw_row[i] for i, c in enumerate(columns)}
            out_rows.append(
                {
                    **record,
                    "__row_id": _anchor_for(record, str(raw_row[len(columns)]), pk_columns),
                }
            )
        return {
            "columns": meta.columns_json or [],
            "rows": out_rows,
            "total": int(total),
            "limit": limit,
            "offset": offset,
            "truncated": int(total) > offset + len(out_rows),
        }

    def aggregate(
        self,
        dataset_key: str,
        group_by: list,
        measures: list,
        filters: Optional[list] = None,
        raw: bool = False,
    ) -> dict:
        """GROUP BY over the effective table (``raw=True`` reads the base
        snapshot). Measures: count/sum/avg/min/max; sum/avg reject TEXT
        columns (honest type guard); group rows capped at QUERY_ROW_CAP."""
        dataset_key = _validate_key(dataset_key)
        meta = self._meta(dataset_key)
        col_types = {c["name"]: str(c.get("type", "TEXT")).upper() for c in (meta.columns_json or [])}
        if not isinstance(group_by, list) or not group_by:
            raise CustomerDatasetError("group_by 必须为非空数组")
        for g in group_by:
            if g not in col_types:
                raise CustomerDatasetError(f"分组列不存在: {g!r}")
        if not isinstance(measures, list) or not measures:
            raise CustomerDatasetError("measures 必须为非空数组")
        exprs: list[str] = []
        keys: list[str] = []
        for m in measures:
            if not isinstance(m, dict):
                raise CustomerDatasetError(f"度量必须是对象: {m!r}")
            func = str(m.get("func", "")).lower()
            col = m.get("column")
            if func not in _AGG_FUNCS:
                raise CustomerDatasetError(f"非法聚合函数: {func!r}（允许 {sorted(_AGG_FUNCS)}）")
            if func == "count" and col is None:
                exprs.append("COUNT(*)")
                keys.append("count")
                continue
            if col not in col_types:
                raise CustomerDatasetError(f"聚合列不存在: {col!r}")
            if func in ("sum", "avg") and col_types.get(col) == "TEXT":
                raise CustomerDatasetError(f"{func} 不支持 TEXT 列: {col!r}")
            exprs.append(f"{func.upper()}({_quote(col)})")
            keys.append(f"{func}__{col}")

        table = _table_name(dataset_key, "__base" if raw else "")
        where, params = _build_where(filters, list(col_types))
        where_sql = f" WHERE {where}" if where else ""
        group_sql = ", ".join(_quote(g) for g in group_by)
        measure_sql = ", ".join(exprs)
        rows = _raw_query_timed(
            f"SELECT {group_sql}, {measure_sql} FROM {_quote(table)}{where_sql}"
            f" GROUP BY {group_sql} LIMIT {QUERY_ROW_CAP}",
            params,
        )
        total = _raw_query_timed(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM {_quote(table)}{where_sql}"
            f" GROUP BY {group_sql})",
            params,
        )[0][0]
        groups = []
        for r in rows:
            rec = {g: r[i] for i, g in enumerate(group_by)}
            for i, k in enumerate(keys, start=len(group_by)):
                rec[k] = r[i]
            groups.append(rec)
        return {
            "group_by": group_by,
            "groups": groups,
            "group_count": int(total),
            "truncated": int(total) > len(groups),
        }

    # ── corrections ──────────────────────────────────────────────────

    @staticmethod
    def _validate_edit_op(
        op: str,
        anchor: Optional[str],
        values: Optional[dict],
        columns: list[str],
    ) -> tuple[str, dict]:
        """Shape validation shared by single edits and batch writes."""
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
        return op, values

    @staticmethod
    def _correction_apply_tx(
        cur,
        dataset_key: str,
        columns: list[str],
        pk_columns: list[str],
        op: str,
        anchor: Optional[str],
        values: dict,
        actor: str,
        channel: str,
        actor_id: Optional[str],
        actor_label: Optional[str],
        strict_anchors: bool,
    ) -> int:
        """Apply + record ONE overlay edit ON THE CALLER'S OPEN TRANSACTION
        CURSOR — shared by ``correction_add`` (single) and ``write_batch``
        (batch), so both channels share identical edit semantics (ADR-0006).

        ``strict_anchors=True`` (batch/business writes): an update/delete that
        matches no row REJECTS — business writes must hit their anchors;
        dangling is a connector-world concept, not an app-write outcome.
        Single console edits keep the record-as-dangling behavior.
        Returns the minted correction id."""
        if channel not in _CHANNELS:
            raise CustomerDatasetError(f"非法写入通道: {channel!r}（允许 {sorted(_CHANNELS)}）")
        effective = _table_name(dataset_key)
        now = _now_db()
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
            if strict_anchors and not matched:
                raise CustomerDatasetError(f"行锚未命中任何行: {anchor!r}")
            cur.execute(
                "INSERT INTO customer_dataset_corrections"
                " (dataset_key, op, anchor, values_json, old_values_json, actor,"
                "  channel, actor_id, actor_label, created_at, dangling_at, dangling_reason)"
                " VALUES (?, 'update', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dataset_key,
                    anchor,
                    json.dumps(values, ensure_ascii=False),
                    json.dumps(old_values, ensure_ascii=False) if old_values is not None else None,
                    actor,
                    channel,
                    actor_id,
                    actor_label,
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
            if strict_anchors and not matched:
                raise CustomerDatasetError(f"行锚未命中任何行: {anchor!r}")
            cur.execute(
                "INSERT INTO customer_dataset_corrections"
                " (dataset_key, op, anchor, actor, channel, actor_id, actor_label,"
                "  created_at, dangling_at, dangling_reason)"
                " VALUES (?, 'delete', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dataset_key,
                    anchor,
                    actor,
                    channel,
                    actor_id,
                    actor_label,
                    now,
                    None if matched else now,
                    None if matched else _DANGLING_NO_MATCH,
                ),
            )
            correction_id = cur.lastrowid
        else:  # insert — two-step to mint ins:<correction_id>
            cur.execute(
                "INSERT INTO customer_dataset_corrections"
                " (dataset_key, op, anchor, values_json, actor, channel, actor_id, actor_label, created_at)"
                " VALUES (?, 'insert', '', ?, ?, ?, ?, ?, ?)",
                (
                    dataset_key,
                    json.dumps(values, ensure_ascii=False),
                    actor,
                    channel,
                    actor_id,
                    actor_label,
                    now,
                ),
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
                "channel": channel,
            },
        )
        return correction_id

    def correction_add(
        self,
        dataset_key: str,
        op: str,
        anchor: Optional[str],
        values: Optional[dict],
        actor: str = "",
        channel: str = "console",
        actor_id: Optional[str] = None,
        actor_label: Optional[str] = None,
    ) -> dict:
        dataset_key = _validate_key(dataset_key)
        meta = self._meta(dataset_key)
        columns = [c["name"] for c in (meta.columns_json or [])]
        pk_columns = list(meta.pk_columns_json or [])
        op, values = self._validate_edit_op(op, anchor, values, columns)
        with _tx() as cur:
            correction_id = self._correction_apply_tx(
                cur, dataset_key, columns, pk_columns, op, anchor, values,
                actor=actor, channel=channel, actor_id=actor_id, actor_label=actor_label,
                strict_anchors=False,
            )
        self._session.expire_all()
        return {"correction_id": correction_id}

    def write_batch(
        self,
        dataset_key: str,
        ops: list,
        actor: str = "",
        channel: str = "console",
        actor_id: Optional[str] = None,
        actor_label: Optional[str] = None,
    ) -> dict:
        """Business-write surface: a list of edits applied in ONE transaction
        (≤1,000 ops); any rejected op rolls the whole batch back with a
        1-based index pointing at the failing entry. Anchors are strict —
        an update/delete matching no row rejects the batch."""
        dataset_key = _validate_key(dataset_key)
        if not isinstance(ops, list) or not ops:
            raise CustomerDatasetError("ops 必须为非空数组")
        if len(ops) > WRITE_BATCH_CAP:
            raise CustomerDatasetError(f"批量写超过单批上限 {WRITE_BATCH_CAP} 条（收到 {len(ops)}）")
        meta = self._meta(dataset_key)
        columns = [c["name"] for c in (meta.columns_json or [])]
        pk_columns = list(meta.pk_columns_json or [])
        checked: list[tuple[str, Optional[str], dict]] = []
        for i, entry in enumerate(ops):
            if not isinstance(entry, dict):
                raise CustomerDatasetError(f"第 {i + 1} 条不是对象: {entry!r}")
            try:
                op, values = self._validate_edit_op(
                    entry.get("op"), entry.get("anchor"), entry.get("values"), columns
                )
            except CustomerDatasetError as exc:
                raise CustomerDatasetError(f"第 {i + 1} 条失败: {exc}") from exc
            checked.append((op, entry.get("anchor"), values))
        with _tx() as cur:
            correction_ids: list[int] = []
            for i, (op, anchor, values) in enumerate(checked):
                try:
                    correction_ids.append(
                        self._correction_apply_tx(
                            cur, dataset_key, columns, pk_columns, op, anchor, values,
                            actor=actor, channel=channel, actor_id=actor_id,
                            actor_label=actor_label, strict_anchors=True,
                        )
                    )
                except CustomerDatasetError as exc:
                    raise CustomerDatasetError(f"第 {i + 1} 条失败: {exc}") from exc
        self._session.expire_all()
        return {"applied": len(correction_ids), "correction_ids": correction_ids}

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

    def create_declared(
        self,
        dataset_key: str,
        columns: list,
        pk_columns: Optional[list] = None,
        name: Optional[str] = None,
        actor: str = "",
        channel: str = "console",
    ) -> dict:
        """Schema-declared creation (wire-customer-data-engine task 2.3): an
        empty dataset built from a column/type/(pk) declaration — the app-first
        twin of upload/connector creation. Rides the ingest primitives (empty
        snapshot), so the result is indistinguishable from an uploaded one."""
        dataset_key = _validate_key(dataset_key)
        if self._meta(dataset_key, required=False) is not None:
            raise CustomerDatasetError(f"数据集已存在: {dataset_key!r}")
        begun = self.ingest_begin(
            dataset_key, columns, pk_columns=pk_columns, row_count=0, actor=actor, name=name
        )
        committed = self.ingest_commit(begun["ingest_id"])
        return {
            "dataset_key": dataset_key,
            "rows": committed.get("rows", 0),
            "columns": _validate_columns(columns),
            "pk_columns": list(pk_columns or []),
        }

    def add_column(self, dataset_key: str, column: dict, actor: str = "") -> dict:
        """Schema evolution, additive only (task 2.4): one nullable column on
        the effective + base tables and in the metadata. Destructive changes
        (drop/retype/rename) are not offered. Rejected while an ingest is
        open — mid-ingest metadata drift would break chunk column lists."""
        dataset_key = _validate_key(dataset_key)
        meta = self._meta(dataset_key)
        (spec,) = _validate_columns([column])
        existing = [c["name"] for c in (meta.columns_json or [])]
        if spec["name"] in existing:
            raise CustomerDatasetError(f"列已存在: {spec['name']!r}")
        with _tx() as cur:
            open_ingest = cur.execute(
                "SELECT id FROM customer_dataset_ingests WHERE dataset_key=? AND status='open' LIMIT 1",
                (dataset_key,),
            ).fetchone()
            if open_ingest:
                raise CustomerDatasetError(
                    "存在进行中的 ingest，请先提交或中止后再加列（避免列清单漂移）"
                )
            for suffix in ("", "__base"):
                cur.execute(
                    f"ALTER TABLE {_quote(_table_name(dataset_key, suffix))}"
                    f" ADD COLUMN {_quote(spec['name'])} {spec['type']}"
                )
            new_columns = list(meta.columns_json or []) + [spec]
            cur.execute(
                "UPDATE customer_datasets SET columns_json=?, updated_at=? WHERE key=?",
                (json.dumps(new_columns, ensure_ascii=False), _now_db(), dataset_key),
            )
            _wal_append(
                cur,
                "dataset.schema.column_added",
                {"dataset_key": dataset_key, "column": spec["name"], "type": spec["type"], "actor": actor},
            )
        self._session.expire_all()
        return {"column": spec, "columns": new_columns}

    # ── derivation & lineage (§3/§4) ─────────────────────────────────

    @staticmethod
    def _lineage_children(cur, parent_key: str) -> list[str]:
        """Live (still-existing) derived children of a parent dataset."""
        rows = cur.execute(
            "SELECT dataset_key FROM customer_dataset_lineage WHERE parent_key=?",
            (parent_key,),
        ).fetchall()
        live = []
        for (child,) in rows:
            if cur.execute(
                "SELECT 1 FROM customer_datasets WHERE key=?", (child,)
            ).fetchone():
                live.append(child)
        return live

    @staticmethod
    def _upsert_lineage(
        cur, child_key: str, parent_key: str, kind: str, spec: dict, status: str, error: Optional[str] = None
    ) -> None:
        cur.execute(
            "INSERT INTO customer_dataset_lineage"
            " (dataset_key, parent_key, kind, spec_json, status, last_run_at, last_error, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(dataset_key) DO UPDATE SET parent_key=excluded.parent_key,"
            " kind=excluded.kind, spec_json=excluded.spec_json, status=excluded.status,"
            " last_run_at=excluded.last_run_at, last_error=excluded.last_error",
            (
                child_key,
                parent_key,
                kind,
                json.dumps(spec, ensure_ascii=False),
                status,
                _now_db(),
                error,
                _now_db(),
            ),
        )

    def _materialize_child_tx(
        self,
        cur,
        child_key: str,
        columns: list[dict],
        pk_columns: list[str],
        rows: list[dict],
        parent_key: str,
        kind: str,
        spec: dict,
        actor: str,
    ) -> int:
        """Swap `rows` into the child dataset atomically ON THE CALLER'S OPEN
        TRANSACTION (source read and child write share one tx — single writer
        + one transaction = coherent derivation). Oversize results reject the
        whole operation; the child keeps its previous state."""
        limit = _max_rows()
        if len(rows) > limit:
            raise CustomerDatasetError(
                f"派生行数 {len(rows)} 超过上限 {limit}（整批拒绝，子集保持原状）"
            )
        staging = _table_name(child_key, "__staging")
        base = _table_name(child_key, "__base")
        effective = _table_name(child_key)
        cur.execute(f"DROP TABLE IF EXISTS {_quote(staging)}")
        col_defs = ", ".join(f"{_quote(c['name'])} {c['type']}" for c in columns)
        cur.execute(f"CREATE TABLE {_quote(staging)} ({col_defs}, {_quote('__row_hash')} TEXT NOT NULL)")
        col_names = [c["name"] for c in columns]
        collist = ", ".join(_quote(c) for c in col_names + ["__row_hash"])
        placeholders = ", ".join("?" for _ in col_names + ["__row_hash"])
        cur.executemany(
            f"INSERT INTO {_quote(staging)} ({collist}) VALUES ({placeholders})",
            [
                tuple(_coerce(r.get(c)) for c in col_names) + (_row_hash(col_names, r),)
                for r in rows
            ],
        )
        total = len(rows)
        cur.execute(f"DROP TABLE IF EXISTS {_quote(base)}")
        cur.execute(f"ALTER TABLE {_quote(staging)} RENAME TO {_quote(base)}")
        cur.execute(f"DROP TABLE IF EXISTS {_quote(effective)}")
        cur.execute(f"CREATE TABLE {_quote(effective)} AS SELECT * FROM {_quote(base)}")
        size_bytes = self._size_of(cur, effective)
        now = _now_db()
        cur.execute(
            "INSERT INTO customer_datasets"
            " (key, name, columns_json, pk_columns_json, row_count, size_bytes, created_at, updated_at, last_commit_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET columns_json=excluded.columns_json,"
            " pk_columns_json=excluded.pk_columns_json, row_count=excluded.row_count,"
            " size_bytes=excluded.size_bytes, updated_at=excluded.updated_at,"
            " last_commit_at=excluded.last_commit_at",
            (
                child_key,
                child_key,
                json.dumps(columns, ensure_ascii=False),
                json.dumps(pk_columns, ensure_ascii=False),
                total,
                size_bytes,
                now,
                now,
                now,
            ),
        )
        self._upsert_softrefs(cur, child_key)
        self._upsert_lineage(cur, child_key, parent_key, kind, spec, "ok", None)
        _wal_append(
            cur,
            "dataset.derived",
            {
                "dataset_key": child_key,
                "parent_key": parent_key,
                "kind": kind,
                "rows": total,
                "actor": actor,
            },
        )
        return total

    @staticmethod
    def _source_rows_and_meta(
        cur, parent_key: str, filters: Optional[list], sort: Optional[list], limit: Optional[int]
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Read the parent's CURRENT effective rows (with declared types) on
        the caller's cursor. Returns (rows, columns, pk_columns)."""
        row = cur.execute(
            "SELECT columns_json, pk_columns_json FROM customer_datasets WHERE key=?",
            (parent_key,),
        ).fetchone()
        if row is None:
            raise CustomerDatasetError(f"父数据集不存在: {parent_key!r}")
        columns = json.loads(row[0]) if row[0] else []
        pk_columns = list(json.loads(row[1])) if row[1] else []
        names = [c["name"] for c in columns]
        where, params = _build_where(filters, names)
        order_sql = _build_order(sort, names)
        limit_sql = f" LIMIT {max(1, int(limit))}" if limit else ""
        collist = ", ".join(_quote(c) for c in names)
        raw = cur.execute(
            f"SELECT {collist} FROM {_quote(_table_name(parent_key))}"
            f"{(' WHERE ' + where) if where else ''}{order_sql}{limit_sql}",
            params,
        ).fetchall()
        rows = [{c: r[i] for i, c in enumerate(names)} for r in raw]
        return rows, columns, pk_columns

    def derive(
        self,
        child_key: str,
        parent_key: str,
        filters: Optional[list] = None,
        sort: Optional[list] = None,
        limit: Optional[int] = None,
        actor: str = "",
        channel: str = "console",
    ) -> dict:
        """Query derivation (task 3.2): materialize a structured-query result
        over one source dataset into a NEW child dataset, lineage recorded."""
        child_key = _validate_key(child_key)
        parent_key = _validate_key(parent_key)
        if child_key == parent_key:
            raise CustomerDatasetError("派生子集不能与父集相同")
        spec = {"filters": filters or [], "sort": sort or [], "limit": limit}
        with _tx() as cur:
            if cur.execute(
                "SELECT 1 FROM customer_datasets WHERE key=?", (child_key,)
            ).fetchone():
                raise CustomerDatasetError(f"数据集已存在: {child_key!r}（重派生请用 derive_refresh）")
            rows, columns, pk_columns = self._source_rows_and_meta(cur, parent_key, filters, sort, limit)
            total = self._materialize_child_tx(
                cur, child_key, columns, pk_columns, rows, parent_key, "query", spec, actor
            )
        self._session.expire_all()
        return {"dataset_key": child_key, "rows": total, "kind": "query"}

    def join_indicators(
        self,
        child_key: str,
        parent_key: str,
        indicators: list,
        date_column: str,
        region_column: Optional[str] = None,
        default_entity: str = "中国",
        filters: Optional[list] = None,
        sort: Optional[list] = None,
        actor: str = "",
        channel: str = "console",
        _refresh: bool = False,
    ) -> dict:
        """Indicator-join derivation (§4): customer rows × platform indicator
        series. Fetches happen BEFORE the write transaction (HTTP never holds
        the sqlite writer lock); misses join as NULL — never neighbor values.
        ``_refresh`` is set by derive_refresh (lineage already verified)."""
        child_key = _validate_key(child_key)
        parent_key = _validate_key(parent_key)
        if child_key == parent_key:
            raise CustomerDatasetError("派生子集不能与父集相同")
        if not isinstance(indicators, list) or not indicators:
            raise CustomerDatasetError("indicators 必须为非空数组")
        if len(indicators) > JOIN_MAX_CODES:
            raise CustomerDatasetError(f"单次指标 code 数超过上限 {JOIN_MAX_CODES}")

        # ── resolve codes / entities / series via the gateway (outside tx) ──
        gateway = _get_indicator_gateway()
        entries: list[dict] = []
        codes: list[str] = []
        for item in indicators:
            if not isinstance(item, dict):
                raise CustomerDatasetError(f"indicators 元素必须是对象: {item!r}")
            code, concept_id = item.get("code"), item.get("concept_id")
            if code is None and concept_id is None:
                raise CustomerDatasetError("indicators 元素须携带 code 或 concept_id")
            alias = item.get("as") or (str(code) if code is not None else f"concept_{concept_id}")
            if not _COL_RE.match(str(alias)):
                raise CustomerDatasetError(f"非法输出列名: {alias!r}")
            if code is not None:
                codes.append(str(code))
            entries.append({"code": code, "concept_id": concept_id, "alias": alias})
        if codes:
            resolved = gateway.resolve_codes(codes)
            for e in entries:
                if e["code"] is not None:
                    e["concept_id"] = resolved[e["code"]]

        # parent sanity + distinct regions + date span (read-only, no tx)
        meta = self._meta(parent_key)
        names = [c["name"] for c in (meta.columns_json or [])]
        if date_column not in names:
            raise CustomerDatasetError(f"日期列不存在: {date_column!r}")
        if region_column is not None and region_column not in names:
            raise CustomerDatasetError(f"地区列不存在: {region_column!r}")
        for e in entries:
            if e["alias"] in names:
                raise CustomerDatasetError(f"输出列名与源列冲突: {e['alias']!r}")
        where, params = _build_where(filters, names)
        where_sql = f" WHERE {where}" if where else ""
        src = _table_name(parent_key)
        span = _raw_query_one(
            f"SELECT MIN({_quote(date_column)}), MAX({_quote(date_column)}) FROM {_quote(src)}{where_sql}",
            tuple(params),
        )
        start, end = (str(span[0])[:10] if span[0] else None, str(span[1])[:10] if span[1] else None)

        entity_keys: dict[Any, tuple[str, int]] = {}
        unresolved_regions: list[Any] = []
        if region_column is not None:
            regions = [
                r[0]
                for r in _raw_query_all(
                    f"SELECT DISTINCT {_quote(region_column)} FROM {_quote(src)}{where_sql}"
                    f" LIMIT {JOIN_MAX_REGIONS + 1}",
                    tuple(params),
                )
            ]
            if len(regions) > JOIN_MAX_REGIONS:
                raise CustomerDatasetError(f"地区去重值超过上限 {JOIN_MAX_REGIONS}，请先收窄过滤")
            for region in regions:
                if region is None:
                    continue
                resolved = gateway.resolve_entity(str(region))
                if resolved is None or not resolved[0]:
                    unresolved_regions.append(region)
                else:
                    entity_keys[region] = resolved
        else:
            resolved = gateway.resolve_entity(default_entity)
            if resolved is None or not resolved[0]:
                raise IndicatorGatewayError(f"默认实体未解析到: {default_entity!r}")
            entity_keys[default_entity] = resolved

        # fetch series per entity (outside any write tx)
        concept_ids = sorted({e["concept_id"] for e in entries})
        series: dict[tuple[str, int], dict[int, dict[str, Any]]] = {}
        if start and end:
            for entity_value, (etype, eid) in entity_keys.items():
                raw = gateway.read_range(concept_ids, etype, eid, start, end)
                series[(etype, eid)] = {
                    cid: _join_series_index(points) for cid, points in raw.items()
                }

        spec = {
            "filters": filters or [],
            "sort": sort or [],
            "indicators": [
                {"code": e["code"], "concept_id": e["concept_id"], "as": e["alias"]} for e in entries
            ],
            "date_column": date_column,
            "region_column": region_column,
            "default_entity": default_entity,
        }
        with _tx() as cur:
            if not _refresh and cur.execute(
                "SELECT 1 FROM customer_datasets WHERE key=?", (child_key,)
            ).fetchone():
                raise CustomerDatasetError(f"数据集已存在: {child_key!r}（重派生请用 derive_refresh）")
            rows, columns, pk_columns = self._source_rows_and_meta(cur, parent_key, filters, sort, None)
            out_columns = columns + [{"name": e["alias"], "type": "REAL"} for e in entries]
            for r in rows:
                exact, month, year = _join_date_periods(r.get(date_column))
                entity = (
                    entity_keys.get(r.get(region_column)) if region_column is not None
                    else entity_keys.get(default_entity)
                )
                for e in entries:
                    value = None
                    if entity is not None:
                        idx = series.get(entity, {}).get(e["concept_id"], {})
                        value = idx.get(exact)
                        if value is None:
                            value = idx.get(month)
                        if value is None:
                            value = idx.get(year)
                    r[e["alias"]] = value
            total = self._materialize_child_tx(
                cur, child_key, out_columns, pk_columns, rows, parent_key, "join", spec, actor
            )
        self._session.expire_all()
        return {
            "dataset_key": child_key,
            "rows": total,
            "kind": "join",
            "unresolved_regions": unresolved_regions,
        }

    def derive_refresh(self, child_key: str, actor: str = "") -> dict:
        """Re-run a derived dataset's stored spec against the parent's current
        effective state (task 3.3). Join derivations re-fetch series first."""
        child_key = _validate_key(child_key)
        lin = _raw_query_one(
            "SELECT parent_key, kind, spec_json FROM customer_dataset_lineage WHERE dataset_key=?",
            (child_key,),
        )
        if lin is None:
            raise CustomerDatasetError(f"不是派生数据集: {child_key!r}")
        parent_key, kind, spec_json = lin
        spec = json.loads(spec_json) if spec_json else {}
        if kind == "join":
            result = self.join_indicators(
                child_key,
                parent_key,
                indicators=spec.get("indicators", []),
                date_column=spec.get("date_column", ""),
                region_column=spec.get("region_column"),
                default_entity=spec.get("default_entity", "中国"),
                filters=spec.get("filters"),
                sort=spec.get("sort"),
                actor=actor,
                _refresh=True,
            )
            result["refreshed"] = True
            return result
        with _tx() as cur:
            rows, columns, pk_columns = self._source_rows_and_meta(
                cur, parent_key, spec.get("filters"), spec.get("sort"), spec.get("limit")
            )
            total = self._materialize_child_tx(
                cur, child_key, columns, pk_columns, rows, parent_key, "query", spec, actor
            )
        self._session.expire_all()
        return {"dataset_key": child_key, "rows": total, "kind": "query", "refreshed": True}

    def lineage(self, dataset_key: str) -> dict:
        """Lineage rows where this dataset is the child (its derivation) —
        parents are discovered by scanning children rows."""
        dataset_key = _validate_key(dataset_key)
        self._meta(dataset_key)
        own = _raw_query_all(
            "SELECT dataset_key, parent_key, kind, spec_json, status, last_run_at, last_error, created_at"
            " FROM customer_dataset_lineage WHERE dataset_key=?",
            (dataset_key,),
        )
        children = _raw_query_all(
            "SELECT dataset_key, kind, status FROM customer_dataset_lineage WHERE parent_key=?",
            (dataset_key,),
        )
        return {
            "derivation": [
                {
                    "parent_key": r[1],
                    "kind": r[2],
                    "spec": json.loads(r[3]) if r[3] else {},
                    "status": r[4],
                    "last_run_at": r[5],
                    "last_error": r[6],
                }
                for r in own
            ],
            "children": [
                {"dataset_key": r[0], "kind": r[1], "status": r[2]} for r in children
            ],
        }

    def delete(self, dataset_key: str, actor: str = "") -> dict:
        dataset_key = _validate_key(dataset_key)
        name = _table_name(dataset_key)
        now = _now_db()
        with _tx() as cur:
            # §3.1: live derived children block deletion — an orphaned lineage
            # would leave an unexplainable hole in the audit chain
            live_children = self._lineage_children(cur, dataset_key)
            if live_children:
                raise CustomerDatasetError(
                    f"存在活跃派生子集，删除被阻止: {live_children}"
                )
            for suffix in ("", "__base", "__staging"):
                cur.execute(f"DROP TABLE IF EXISTS {_quote(_table_name(dataset_key, suffix))}")
            cur.execute(
                "INSERT INTO customer_dataset_ingests (id, dataset_key, status, started_at, finished_at, actor, error)"
                " VALUES (?, ?, 'deleted', ?, ?, ?, ?)",
                (str(uuid.uuid4()), dataset_key, now, now, actor, "dataset deleted"),
            )
            cur.execute("DELETE FROM customer_dataset_corrections WHERE dataset_key=?", (dataset_key,))
            # a deleted child takes its own lineage row with it
            cur.execute("DELETE FROM customer_dataset_lineage WHERE dataset_key=?", (dataset_key,))
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


def query(
    dataset_key: str,
    filters: Optional[list] = None,
    sort: Optional[list] = None,
    limit: int = 100,
    offset: int = 0,
    count_only: bool = False,
    distinct_column: Optional[str] = None,
    raw: bool = False,
) -> dict:
    """Structured query over a customer dataset: whitelisted filter operators
    (eq/ne/gt/gte/lt/lte/in/notin/like/isnull/notnull, AND-composed), multi-column
    sort, limit/offset paging, exact total and an honest truncated flag; rows
    carry `__row_id` anchors. `count_only=True` returns just the count;
    `distinct_column` returns that column's distinct values (capped at 1,000);
    `raw=True` reads the base snapshot without edits. Responses are capped at
    1,000 rows and queries abort past 5s.

    Args:
        dataset_key: Slug `^[a-z][a-z0-9_]{0,39}$`.
        filters: `[{column, op, value | values}]` — values only for in/notin.
        sort: `[{column, dir: asc|desc}]`, applied in order.
        limit: Page size, clamped to [1, 1000].
        offset: Page offset, >= 0.
        count_only: Return `{count}` only (filters still apply).
        distinct_column: Column name for distinct-value listing.
        raw: Read the immutable base snapshot instead of the effective table.
    """
    svc = _svc()
    try:
        return _ok(
            svc.query(
                dataset_key,
                filters=filters,
                sort=sort,
                limit=limit,
                offset=offset,
                count_only=count_only,
                distinct_column=distinct_column,
                raw=raw,
            )
        )
    except Exception as e:
        return _err(e)


def aggregate(
    dataset_key: str,
    group_by: list,
    measures: list,
    filters: Optional[list] = None,
    raw: bool = False,
) -> dict:
    """GROUP BY aggregation over a customer dataset: multi-column grouping ×
    count/sum/avg/min/max measures, filter-compatible, group rows capped at
    1,000 with exact group_count. sum/avg reject TEXT columns (honest type
    guard); min/max allow any column. `raw=True` reads the base snapshot.

    Args:
        dataset_key: Slug `^[a-z][a-z0-9_]{0,39}$`.
        group_by: Non-empty list of column names.
        measures: `[{func, column?}]` — column required except for bare count.
        filters: Same shape as `query` filters (AND-composed).
        raw: Read the immutable base snapshot instead of the effective table.
    """
    svc = _svc()
    try:
        return _ok(
            svc.aggregate(dataset_key, group_by=group_by, measures=measures, filters=filters, raw=raw)
        )
    except Exception as e:
        return _err(e)


def correction_add(
    dataset_key: str,
    op: str,
    anchor: Optional[str] = None,
    values: Optional[dict] = None,
    actor: str = "",
    channel: str = "console",
    actor_id: Optional[str] = None,
    actor_label: Optional[str] = None,
) -> dict:
    """Add an overlay correction (update/delete/insert); it applies to the
    effective table immediately and is replayed after every future commit.
    Every edit record carries its write channel (console | data-key | agent)
    for audit attribution."""
    svc = _svc()
    try:
        return _ok(
            svc.correction_add(
                dataset_key,
                op,
                anchor,
                values,
                actor=actor,
                channel=channel,
                actor_id=actor_id,
                actor_label=actor_label,
            )
        )
    except Exception as e:
        return _err(e)


def write_batch(
    dataset_key: str,
    ops: list,
    actor: str = "",
    channel: str = "console",
    actor_id: Optional[str] = None,
    actor_label: Optional[str] = None,
) -> dict:
    """Business-write surface (wire-customer-data-engine §2): a list of edit
    operations applied in ONE transaction (≤1,000 ops). Any rejected op rolls
    the whole batch back — zero partial writes — and the error carries the
    1-based index of the failing entry. Anchors are strict here: an
    update/delete that matches no row rejects the batch (business writes must
    hit their anchors; dangling is a connector-world outcome, not an app one).

    Args:
        dataset_key: Slug `^[a-z][a-z0-9_]{0,39}$`.
        ops: `[{op: update|delete|insert, anchor?, values?}]` (same per-op
            shape as correction_add).
        actor: Human-readable actor label (audit).
        channel: Write channel: console | data-key | agent.
        actor_id: Structured actor identity (member id / key fingerprint / agent id).
        actor_label: Display label for the actor.
    """
    svc = _svc()
    try:
        return _ok(
            svc.write_batch(
                dataset_key,
                ops,
                actor=actor,
                channel=channel,
                actor_id=actor_id,
                actor_label=actor_label,
            )
        )
    except Exception as e:
        return _err(e)


def create_declared(
    dataset_key: str,
    columns: list,
    pk_columns: Optional[list] = None,
    name: Optional[str] = None,
    actor: str = "",
    channel: str = "console",
) -> dict:
    """Schema-declared dataset creation: an EMPTY dataset from a column/type
    declaration (optionally with primary-key columns that become the row
    anchor), indistinguishable from an uploaded/connector-created dataset.
    The app-first creation channel for business systems."""
    svc = _svc()
    try:
        return _ok(
            svc.create_declared(
                dataset_key,
                columns,
                pk_columns=pk_columns,
                name=name,
                actor=actor,
                channel=channel,
            )
        )
    except Exception as e:
        return _err(e)


def add_column(dataset_key: str, column: dict, actor: str = "") -> dict:
    """Add ONE nullable column to a dataset (effective + base tables and the
    metadata). Additive-only schema evolution: destructive changes are not
    offered. Rejected while an ingest is open."""
    svc = _svc()
    try:
        return _ok(svc.add_column(dataset_key, column, actor=actor))
    except Exception as e:
        return _err(e)


def derive(
    child_key: str,
    parent_key: str,
    filters: Optional[list] = None,
    sort: Optional[list] = None,
    limit: Optional[int] = None,
    actor: str = "",
    channel: str = "console",
) -> dict:
    """Materialize a structured-query result over `parent_key` into a NEW
    child dataset, lineage recorded (parent + spec). The child inherits the
    parent's columns and pk declaration. Re-derivation of an existing child
    goes through `derive_refresh`."""
    svc = _svc()
    try:
        return _ok(
            svc.derive(
                child_key,
                parent_key,
                filters=filters,
                sort=sort,
                limit=limit,
                actor=actor,
                channel=channel,
            )
        )
    except Exception as e:
        return _err(e)


def join_indicators(
    child_key: str,
    parent_key: str,
    indicators: list,
    date_column: str,
    region_column: Optional[str] = None,
    default_entity: str = "中国",
    filters: Optional[list] = None,
    sort: Optional[list] = None,
    actor: str = "",
    channel: str = "console",
) -> dict:
    """Derive a child dataset = customer rows × platform indicator series.
    Series are fetched through the wire entrance with the cell's credential
    (metered to this customer, same track as artifact materialization).
    Matching: exact date → month period → year period (frequency inferred
    from the series' own points); misses join as NULL — never neighbor
    values. `region_column` (optional) resolves per-row entities by name;
    without it every row uses `default_entity`. At most 10 indicator codes
    per derivation and 64 distinct regions."""
    svc = _svc()
    try:
        return _ok(
            svc.join_indicators(
                child_key,
                parent_key,
                indicators=indicators,
                date_column=date_column,
                region_column=region_column,
                default_entity=default_entity,
                filters=filters,
                sort=sort,
                actor=actor,
                channel=channel,
            )
        )
    except Exception as e:
        return _err(e)


def derive_refresh(child_key: str, actor: str = "") -> dict:
    """Re-run a derived dataset's stored spec against its parent's current
    effective state. Join derivations re-fetch their indicator series first
    (metered reads again)."""
    svc = _svc()
    try:
        return _ok(svc.derive_refresh(child_key, actor=actor))
    except Exception as e:
        return _err(e)


def lineage(dataset_key: str) -> dict:
    """Lineage of a dataset: its own derivation (parent + spec + last run)
    and its live derived children."""
    svc = _svc()
    try:
        return _ok(svc.lineage(dataset_key))
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
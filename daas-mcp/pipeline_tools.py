"""Managed pipeline collections for daas-mcp.

A `pipeline_collection` groups fetch *items*; each item binds a source MCP
(`source_mcp` + `tool` + `arguments_json`) to a `scraw_<slug>` storage table
and a cron cadence. Adding an enabled item triggers an immediate history
backfill (spawn the source MCP via `fastmcp.Client`, call `tool`, upsert the
returned records) and an idempotent `cron-mcp` task + schedule. Removing or
disabling an item unwires the schedule. This is the `data_job` shape from
`add-cron-mcp-data-fetch`, so items migrate 1:1 later.

Tools (11):
  create_pipeline_collection / list_pipeline_collections / get_pipeline_collection
  delete_pipeline_collection / list_pipeline_items / add_pipeline_item
  remove_pipeline_item / enable_pipeline_item / disable_pipeline_item
  update_pipeline_item / sync_pipeline_cron

CLI branches (server.py): --fetch-item / --register-cron / --unregister-cron / --sync-cron
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from daas_database import get_database
from models import PipelineCollection, PipelineCollectionItem

# ── paths ────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parents[2]          # repo root
MCP_ROOT = Path(__file__).resolve().parents[1]            # mcp/
DAAS_MCP_DIR = Path(__file__).resolve().parent            # mcp/daas-mcp/
MCP_JSON_PATH = Path(os.environ.get("MCP_JSON_PATH", REPO_ROOT / ".mcp.json"))

# ── validators ───────────────────────────────────────────────────────
_STORAGE_TABLE_RE = re.compile(r"^scraw_[a-z0-9_]+$")
_IDENT_RE = re.compile(r"^\w+$", re.UNICODE)              # letters/digits/underscore (Unicode)
_CRON_FIELDS = 5


class PipelineError(Exception):
    """Raised for validation / wiring failures."""


def _validate_storage_table(name: str) -> None:
    if not isinstance(name, str) or not _STORAGE_TABLE_RE.match(name):
        raise PipelineError(f"storage_table must match ^scraw_[a-z0-9_]+$; got {name!r}")


def _validate_ident(name: str, field: str) -> None:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise PipelineError(f"Invalid {field} {name!r} (must be word characters only)")


def _validate_cron(expr: str) -> None:
    if not isinstance(expr, str) or len(expr.split()) != _CRON_FIELDS:
        raise PipelineError(f"cron_expr must be 5 fields; got {expr!r}")


def _sanitize_col(name: str) -> str:
    """Make a column name a safe SQLite identifier (allows Chinese chars)."""
    s = re.sub(r"[^\w]", "_", str(name))
    if not s:
        s = "_"
    if s[0].isdigit():
        s = "_" + s
    return s


# ── .mcp.json + convention launch-config resolver ────────────────────
def _load_mcp_servers() -> dict:
    try:
        with open(MCP_JSON_PATH, "r", encoding="utf-8") as f:
            return (json.load(f) or {}).get("mcpServers", {}) or {}
    except FileNotFoundError:
        return {}


def resolve_server_config(source_mcp: str) -> Optional[dict]:
    """Return a launch config {command, args, env, cwd} for `source_mcp`, or None.

    Resolution order: (1) an entry in `.mcp.json`'s mcpServers; (2) the
    `mcp/<source_mcp>/server.py` convention dir (launched via
    `uv run --directory <repo>/mcp/<source_mcp> python server.py`). akshare-mcp
    and the other data-fetch MCPs are not in `.mcp.json`, so the convention
    fallback is the common path.

    `mcp/models` is prepended to PYTHONPATH so the shared `models` package is
    importable in the spawned server (some project venvs lack the editable
    `mcp-models` install; the path injection is harmless where it's present).
    """
    if not isinstance(source_mcp, str) or not source_mcp:
        return None
    models_dir = str(MCP_ROOT / "models")
    servers = _load_mcp_servers()
    cfg = servers.get(source_mcp)
    if cfg:
        env = dict(os.environ)
        if cfg.get("env"):
            env.update(cfg["env"])
        env["PYTHONPATH"] = _prepend_pythonpath(env.get("PYTHONPATH"), models_dir)
        return {
            "command": cfg.get("command", "uv"),
            "args": list(cfg.get("args", [])),
            "env": env,
            "cwd": cfg.get("cwd"),
        }
    conv_dir = MCP_ROOT / source_mcp
    if (conv_dir / "server.py").exists():
        env = dict(os.environ)
        env["PYTHONPATH"] = _prepend_pythonpath(env.get("PYTHONPATH"), models_dir)
        return {
            "command": "uv",
            "args": ["run", "--directory", str(conv_dir), "python", "server.py"],
            "env": env,
            "cwd": None,
        }
    return None


def _prepend_pythonpath(existing: Optional[str], path: str) -> str:
    if not existing:
        return path
    parts = [p for p in existing.split(os.pathsep) if p and p != path]
    return os.pathsep.join([path, *parts])


def _validate_source_mcp(source_mcp: str) -> None:
    if resolve_server_config(source_mcp) is None:
        raise PipelineError(
            f"source_mcp {source_mcp!r} does not resolve: not in .mcp.json and no "
            f"mcp/{source_mcp}/server.py convention dir"
        )


# ── DB helpers ───────────────────────────────────────────────────────
def _session() -> Session:
    return get_database().get_session()


def _engine():
    return get_database().engine


def _get_collection(session: Session, name: str) -> PipelineCollection:
    coll = (
        session.query(PipelineCollection)
        .filter(PipelineCollection.name == name)
        .first()
    )
    if coll is None:
        raise PipelineError(f"Pipeline collection {name!r} not found")
    return coll


def _get_item(session: Session, collection_name: str, name: str) -> PipelineCollectionItem:
    coll = _get_collection(session, collection_name)
    item = (
        session.query(PipelineCollectionItem)
        .filter(
            PipelineCollectionItem.collection_id == coll.id,
            PipelineCollectionItem.name == name,
        )
        .first()
    )
    if item is None:
        raise PipelineError(f"Item {name!r} not found in collection {collection_name!r}")
    return item


def _task_name_for(coll_name: str, item_name: str) -> str:
    return f"pipeline_{coll_name}_{item_name}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── fetch-to-store bridge ────────────────────────────────────────────
def _infer_type(values: list) -> str:
    """Infer a SQLite column type from the first non-null value."""
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            return "INTEGER"
        if isinstance(v, int):
            return "INTEGER"
        if isinstance(v, float):
            return "REAL"
        return "TEXT"
    return "TEXT"


def _upsert_records(table: str, keys: list[str], records: list[dict]) -> int:
    """Create the scraw_<table> if needed and upsert records on `keys`.

    Returns the number of rows upserted. Uses a raw sqlite3 connection so
    positional `?` placeholders work with Chinese column names. All
    identifiers are sanitized + double-quoted; `table` is validator-checked.
    """
    if not records:
        return 0
    # sanitize all column names consistently
    all_cols: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for k in rec.keys():
            sc = _sanitize_col(k)
            if sc not in seen:
                seen.add(sc)
                all_cols.append(sc)
    sani_keys = [_sanitize_col(k) for k in keys]
    for k in sani_keys:
        if k not in seen:
            seen.add(k)
            all_cols.append(k)
    # infer types per column
    col_types: dict[str, str] = {}
    for c in all_cols:
        vals = [rec.get(_orig_for(c, records)) for rec in records]
        col_types[c] = _infer_type(vals)

    eng = _engine()
    conn = eng.raw_connection()
    try:
        cur = conn.cursor()
        col_defs = ", ".join(f'"{c}" {col_types[c]}' for c in all_cols)
        cur.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})')
        # unique index on the upsert keys
        key_list = ", ".join(f'"{k}"' for k in sani_keys)
        idx_name = f"idx_{table}_keys"
        cur.execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{idx_name}" ON "{table}" ({key_list})'
        )
        # detect new columns (later fetches) and ALTER TABLE
        existing = {row[1] for row in cur.execute(f'PRAGMA table_info("{table}")').fetchall()}
        for c in all_cols:
            if c not in existing:
                cur.execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}" {col_types[c]}')
        # build the upsert statement
        col_list = ", ".join(f'"{c}"' for c in all_cols)
        placeholders = ", ".join("?" for _ in all_cols)
        non_key = [c for c in all_cols if c not in sani_keys]
        if non_key:
            update = ", ".join(f'"{c}" = excluded."{c}"' for c in non_key)
            sql = (
                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
                f'ON CONFLICT({key_list}) DO UPDATE SET {update}'
            )
        else:
            sql = (
                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders}) '
                f'ON CONFLICT({key_list}) DO NOTHING'
            )
        # map sanitized col -> original record key (first record that has it)
        orig_map = {c: _orig_for(c, records) for c in all_cols}
        rows = [[rec.get(orig_map[c]) for c in all_cols] for rec in records]
        cur.executemany(sql, rows)
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _orig_for(sani: str, records: list[dict]) -> str:
    """Return the original record key whose sanitized form == `sani`."""
    for rec in records:
        for k in rec.keys():
            if _sanitize_col(k) == sani:
                return k
    return sani


def _extract_records(result: Any) -> tuple[list[dict], list[str]]:
    """Pull (records, columns) from a source-MCP tool result.

    Registry-style MCPs return {"type":"dataframe","columns":[...],"data":[...]}.
    Falls back to dict-as-single-row / list-as-records.
    """
    data = result.data if hasattr(result, "data") else result
    if isinstance(data, dict):
        if "error" in data:
            raise PipelineError(str(data["error"]))
        if data.get("type") == "dataframe" or isinstance(data.get("data"), list):
            records = data.get("data") or []
            columns = data.get("columns") or (list(records[0].keys()) if records else [])
            return records, columns
        # plain dict → single row
        return [data], list(data.keys())
    if isinstance(data, list):
        records = data
        columns = list(records[0].keys()) if records else []
        return records, columns
    raise PipelineError(f"Unsupported source-MCP result type: {type(data).__name__}")


async def fetch_to_store(item: PipelineCollectionItem) -> dict:
    """Spawn the item's source MCP, call `tool` with `arguments_json`, upsert.

    Returns {"status": "ok"|"backfill_failed", "rows": N, "error": str|None}.
    Does NOT update the item row — the caller does that.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    cfg = resolve_server_config(item.source_mcp)
    if cfg is None:
        return {"status": "backfill_failed", "rows": 0, "error": f"source_mcp {item.source_mcp!r} does not resolve"}
    try:
        args = json.loads(item.arguments_json) if item.arguments_json else {}
    except json.JSONDecodeError as e:
        return {"status": "backfill_failed", "rows": 0, "error": f"Invalid arguments_json: {e}"}
    try:
        keys = json.loads(item.upsert_keys_json) if item.upsert_keys_json else []
    except json.JSONDecodeError as e:
        return {"status": "backfill_failed", "rows": 0, "error": f"Invalid upsert_keys_json: {e}"}
    if not keys:
        return {"status": "backfill_failed", "rows": 0, "error": "upsert_keys is empty"}

    try:
        transport = StdioTransport(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env"),
            cwd=cfg.get("cwd"),
        )
        async with Client(transport) as client:
            result = await client.call_tool(item.tool, args)
        records, _cols = _extract_records(result)
        rows = _upsert_records(item.storage_table, keys, records)
        return {"status": "ok", "rows": rows, "error": None}
    except PipelineError as e:
        return {"status": "backfill_failed", "rows": 0, "error": str(e)}
    except Exception as e:
        return {"status": "backfill_failed", "rows": 0, "error": f"{type(e).__name__}: {e}"}


# ── cron-mcp client wiring ───────────────────────────────────────────
def _cron_config() -> dict:
    cfg = resolve_server_config("cron-mcp")
    if cfg is None:
        raise PipelineError(
            "cron-mcp does not resolve: not in .mcp.json and no mcp/cron-mcp/server.py"
        )
    return cfg


def _fetch_item_command(item_id: int) -> str:
    return f"uv run --directory {DAAS_MCP_DIR} python server.py --fetch-item {item_id}"


async def _cron_call(tool: str, arguments: dict) -> dict:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    cfg = _cron_config()
    transport = StdioTransport(
        command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env"), cwd=cfg.get("cwd")
    )
    async with Client(transport) as client:
        result = await client.call_tool(tool, arguments)
    return result.data if hasattr(result, "data") else result


async def _list_cron_tasks() -> list[dict]:
    d = await _cron_call("list_db_tasks", {})
    return d.get("tasks", []) if isinstance(d, dict) else []


async def _list_cron_schedules() -> list[dict]:
    d = await _cron_call("list_schedules", {})
    return d.get("schedules", []) if isinstance(d, dict) else []


async def register_cron_for_item(item: PipelineCollectionItem) -> dict:
    """Idempotently create-or-update the cron-mcp task + schedule for one item.

    Returns {"status": "ok"|"cron_failed", "task": name, "schedule_id": ..., "error": str|None}.
    """
    cmd = _fetch_item_command(item.id)
    try:
        tasks = await _list_cron_tasks()
        existing = next ( (t for t in tasks if t.get("name") == item.task_name), None )
        if existing is None:
            res = await _cron_call(
                "create_task",
                {"name": item.task_name, "command": cmd, "description": f"pipeline item {item.id}", "timeout": 300},
            )
            if not (isinstance(res, dict) and res.get("success")):
                # maybe a race created it; try update
                await _cron_call("update_task", {"name": item.task_name, "command": cmd, "timeout": 300})
        elif existing.get("command") != cmd:
            await _cron_call("update_task", {"name": item.task_name, "command": cmd, "timeout": 300})

        schedules = await _list_cron_schedules()
        mine = [s for s in schedules if s.get("name") == item.task_name or s.get("task") == item.task_name]
        want_enabled = bool(item.enabled)
        # if cron/timezone drifted, recreate
        if mine:
            s = mine[0]
            sid = s.get("id") or s.get("schedule_id")
            if s.get("cron") != item.cron_expr or s.get("timezone") != item.timezone:
                await _cron_call("delete_schedule", {"schedule_id": sid})
                res = await _cron_call(
                    "create_schedule",
                    {"name": item.task_name, "cron": item.cron_expr, "task": item.task_name,
                     "timezone": item.timezone, "enabled": want_enabled},
                )
                sid = res.get("schedule_id") if isinstance(res, dict) else None
            else:
                # ensure enabled state matches by pause/resume if needed
                cur_enabled = bool(s.get("enabled"))
                if cur_enabled != want_enabled:
                    if want_enabled:
                        await _cron_call("resume_schedule", {"schedule_id": sid})
                    else:
                        await _cron_call("pause_schedule", {"schedule_id": sid})
            return {"status": "ok", "task": item.task_name, "schedule_id": sid, "error": None}
        res = await _cron_call(
            "create_schedule",
            {"name": item.task_name, "cron": item.cron_expr, "task": item.task_name,
             "timezone": item.timezone, "enabled": want_enabled},
        )
        if isinstance(res, dict) and res.get("success") is False:
            return {"status": "cron_failed", "task": item.task_name, "schedule_id": None, "error": res.get("error", "create_schedule failed")}
        return {"status": "ok", "task": item.task_name, "schedule_id": res.get("schedule_id") if isinstance(res, dict) else None, "error": None}
    except Exception as e:
        return {"status": "cron_failed", "task": item.task_name, "schedule_id": None, "error": f"{type(e).__name__}: {e}"}


async def unregister_cron_for_item(item: PipelineCollectionItem) -> dict:
    """Delete the cron-mcp schedule + task for an item (idempotent)."""
    err: Optional[str] = None
    try:
        schedules = await _list_cron_schedules()
        for s in schedules:
            if s.get("name") == item.task_name or s.get("task") == item.task_name:
                sid = s.get("id") or s.get("schedule_id")
                await _cron_call("delete_schedule", {"schedule_id": sid})
    except Exception as e:
        err = f"delete_schedule: {type(e).__name__}: {e}"
    try:
        await _cron_call("delete_task", {"name": item.task_name})
    except Exception as e:
        err = (err + "; " if err else "") + f"delete_task: {type(e).__name__}: {e}"
    return {"status": "ok" if err is None else "cron_failed", "error": err}


# ── item status update ───────────────────────────────────────────────
def _record_run(item: PipelineCollectionItem, status: str, rows: Optional[int], error: Optional[str]) -> None:
    item.last_run_at = _now()
    item.last_status = status
    item.last_row_count = rows
    item.error_message = error


# ── collection CRUD ──────────────────────────────────────────────────
async def create_pipeline_collection(name: str, description: Optional[str] = None) -> dict:
    """Create a named pipeline collection."""
    session = _session()
    try:
        existing = session.query(PipelineCollection).filter(PipelineCollection.name == name).first()
        if existing is not None:
            raise PipelineError(f"Pipeline collection {name!r} already exists")
        coll = PipelineCollection(name=name, description=description)
        session.add(coll)
        session.commit()
        return coll.to_dict()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def list_pipeline_collections() -> dict:
    """Every pipeline collection with its item count."""
    session = _session()
    try:
        colls = session.query(PipelineCollection).order_by(PipelineCollection.name).all()
        return {"collections": [c.to_dict() for c in colls]}
    finally:
        session.close()


async def get_pipeline_collection(name: str) -> dict:
    """One pipeline collection with all its items."""
    session = _session()
    try:
        coll = _get_collection(session, name)
        items = (
            session.query(PipelineCollectionItem)
            .filter(PipelineCollectionItem.collection_id == coll.id)
            .order_by(PipelineCollectionItem.id)
            .all()
        )
        d = coll.to_dict()
        d["items"] = [it.to_dict() for it in items]
        return d
    finally:
        session.close()


async def delete_pipeline_collection(name: str) -> dict:
    """Unwire cron for every item, then delete the collection (FK CASCADE removes items)."""
    session = _session()
    try:
        coll = _get_collection(session, name)
        items = (
            session.query(PipelineCollectionItem)
            .filter(PipelineCollectionItem.collection_id == coll.id)
            .all()
        )
        unwired = []
        for it in items:
            if it.task_name:
                await unregister_cron_for_item(it)
                unwired.append(it.task_name)
        session.delete(coll)
        session.commit()
        return {"deleted": name, "unwired_tasks": unwired}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def list_pipeline_items(collection_name: Optional[str] = None) -> dict:
    """List items, optionally filtered to one collection."""
    session = _session()
    try:
        q = session.query(PipelineCollectionItem)
        if collection_name is not None:
            coll = _get_collection(session, collection_name)
            q = q.filter(PipelineCollectionItem.collection_id == coll.id)
        items = q.order_by(PipelineCollectionItem.id).all()
        return {"count": len(items), "items": [it.to_dict() for it in items]}
    finally:
        session.close()


async def add_pipeline_item(
    collection_name: str,
    name: str,
    source_mcp: str,
    tool: str,
    arguments_json: str,
    storage_table: str,
    upsert_keys: list[str],
    cron_expr: str,
    timezone: str = "Asia/Shanghai",
    enabled: bool = True,
    backfill: bool = True,
) -> dict:
    """Validate + store an item. If enabled and backfill, run history backfill
    then register the cron-mcp schedule. Returns {item, backfill, cron}.
    """
    # validate
    _validate_source_mcp(source_mcp)
    if not isinstance(tool, str) or not tool:
        raise PipelineError("tool must be a non-empty string")
    _validate_storage_table(storage_table)
    _validate_cron(cron_expr)
    if not isinstance(upsert_keys, list) or not upsert_keys:
        raise PipelineError("upsert_keys must be a non-empty list")
    for k in upsert_keys:
        _validate_ident(k, "upsert_key")
    try:
        json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError as e:
        raise PipelineError(f"Invalid arguments_json: {e}") from e

    session = _session()
    try:
        coll = _get_collection(session, collection_name)
        dup = (
            session.query(PipelineCollectionItem)
            .filter(PipelineCollectionItem.collection_id == coll.id, PipelineCollectionItem.name == name)
            .first()
        )
        if dup is not None:
            raise PipelineError(f"Item {name!r} already exists in collection {collection_name!r}")
        item = PipelineCollectionItem(
            collection_id=coll.id,
            name=name,
            source_mcp=source_mcp,
            tool=tool,
            arguments_json=arguments_json,
            storage_table=storage_table,
            upsert_keys_json=json.dumps(upsert_keys),
            cron_expr=cron_expr,
            timezone=timezone,
            enabled=enabled,
            task_name=_task_name_for(collection_name, name),
        )
        session.add(item)
        session.commit()
        session.refresh(item)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    backfill_res = {"status": "skipped", "rows": 0, "error": None}
    cron_res = {"status": "skipped", "task": item.task_name, "schedule_id": None, "error": None}
    if enabled and backfill:
        backfill_res = await fetch_to_store(item)
        # re-fetch a fresh session to record status
        s2 = _session()
        try:
            it = s2.get(PipelineCollectionItem, item.id)
            _record_run(it, backfill_res["status"], backfill_res["rows"], backfill_res["error"])
            s2.commit()
        finally:
            s2.close()
        cron_res = await register_cron_for_item(item)
        s3 = _session()
        try:
            it = s3.get(PipelineCollectionItem, item.id)
            if cron_res["status"] == "cron_failed" and it.last_status == "ok":
                it.last_status = "cron_failed"
                it.error_message = cron_res.get("error")
            s3.commit()
        finally:
            s3.close()
    return {"item": item.to_dict(), "backfill": backfill_res, "cron": cron_res}


async def remove_pipeline_item(collection_name: str, name: str) -> dict:
    """Unwire cron then delete the item row. scraw_<table> is left intact."""
    session = _session()
    try:
        item = _get_item(session, collection_name, name)
        task = item.task_name
        if task:
            await unregister_cron_for_item(item)
        session.delete(item)
        session.commit()
        return {"removed": name, "unwired_task": task}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def enable_pipeline_item(collection_name: str, name: str) -> dict:
    """Backfill + register cron for a disabled item; set enabled=1."""
    session = _session()
    try:
        item = _get_item(session, collection_name, name)
        item.enabled = True
        session.commit()
        session.refresh(item)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    backfill_res = await fetch_to_store(item)
    s2 = _session()
    try:
        it = s2.get(PipelineCollectionItem, item.id)
        _record_run(it, backfill_res["status"], backfill_res["rows"], backfill_res["error"])
        s2.commit()
    finally:
        s2.close()
    cron_res = await register_cron_for_item(item)
    return {"item": item.to_dict(), "backfill": backfill_res, "cron": cron_res}


async def disable_pipeline_item(collection_name: str, name: str) -> dict:
    """Delete the cron schedule (keep the task row); set enabled=0."""
    session = _session()
    try:
        item = _get_item(session, collection_name, name)
        item.enabled = False
        session.commit()
        session.refresh(item)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    # pause (not delete) so re-enable is cheap? spec says delete schedule, keep task.
    res = {"status": "ok", "error": None}
    try:
        schedules = await _list_cron_schedules()
        for s in schedules:
            if s.get("name") == item.task_name or s.get("task") == item.task_name:
                sid = s.get("id") or s.get("schedule_id")
                await _cron_call("delete_schedule", {"schedule_id": sid})
    except Exception as e:
        res = {"status": "cron_failed", "error": f"{type(e).__name__}: {e}"}
    return {"item": item.to_dict(), "cron": res}


async def update_pipeline_item(
    collection_name: str,
    name: str,
    arguments_json: Optional[str] = None,
    cron_expr: Optional[str] = None,
    timezone: Optional[str] = None,
    upsert_keys: Optional[list[str]] = None,
    description: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """Update mutable fields. cron/timezone change re-syncs the schedule.
    enabled toggle runs enable/disable. arguments_json change does NOT backfill."""
    session = _session()
    try:
        coll = _get_collection(session, collection_name)
        item = _get_item(session, collection_name, name)
        if arguments_json is not None:
            try:
                json.loads(arguments_json)
            except json.JSONDecodeError as e:
                raise PipelineError(f"Invalid arguments_json: {e}") from e
            item.arguments_json = arguments_json
        if cron_expr is not None:
            _validate_cron(cron_expr)
            item.cron_expr = cron_expr
        if timezone is not None:
            item.timezone = timezone
        if upsert_keys is not None:
            if not upsert_keys:
                raise PipelineError("upsert_keys must be a non-empty list")
            for k in upsert_keys:
                _validate_ident(k, "upsert_key")
            item.upsert_keys_json = json.dumps(upsert_keys)
        # description lives on the collection, not the item — ignore if given
        cron_changed = cron_expr is not None or timezone is not None
        was_enabled = bool(item.enabled)
        if enabled is not None:
            item.enabled = enabled
        session.commit()
        session.refresh(item)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    cron_res = None
    if cron_changed and item.enabled:
        cron_res = await register_cron_for_item(item)
    if enabled is not None:
        if enabled and not was_enabled:
            bf = await fetch_to_store(item)
            s2 = _session()
            try:
                it = s2.get(PipelineCollectionItem, item.id)
                _record_run(it, bf["status"], bf["rows"], bf["error"])
                s2.commit()
            finally:
                s2.close()
            cron_res = await register_cron_for_item(item)
        elif not enabled and was_enabled:
            try:
                schedules = await _list_cron_schedules()
                for s in schedules:
                    if s.get("name") == item.task_name or s.get("task") == item.task_name:
                        sid = s.get("id") or s.get("schedule_id")
                        await _cron_call("delete_schedule", {"schedule_id": sid})
            except Exception:
                pass
    return {"item": item.to_dict(), "cron": cron_res}


async def sync_pipeline_cron() -> dict:
    """Re-apply cron wiring for all enabled items; remove schedules for disabled ones."""
    session = _session()
    try:
        items = session.query(PipelineCollectionItem).all()
    finally:
        session.close()
    synced, removed, failed = [], [], []
    for it in items:
        if it.enabled:
            res = await register_cron_for_item(it)
            if res["status"] == "ok":
                synced.append(it.task_name)
            else:
                failed.append({"item": it.name, "error": res.get("error")})
        else:
            try:
                schedules = await _list_cron_schedules()
                for s in schedules:
                    if s.get("name") == it.task_name or s.get("task") == it.task_name:
                        sid = s.get("id") or s.get("schedule_id")
                        await _cron_call("delete_schedule", {"schedule_id": sid})
                        removed.append(it.task_name)
            except Exception as e:
                failed.append({"item": it.name, "error": str(e)})
    return {"synced": synced, "removed": removed, "failed": failed}


# ── CLI entrypoints (used by server.py --fetch-item etc.) ────────────
def cli_fetch_item(item_id: int) -> int:
    s = _session()
    try:
        item = s.get(PipelineCollectionItem, item_id)
    finally:
        s.close()
    if item is None:
        print(json.dumps({"status": "failed", "error": f"item {item_id} not found"}))
        return 1
    res = asyncio.run(fetch_to_store(item))
    s2 = _session()
    try:
        it = s2.get(PipelineCollectionItem, item.id)
        _record_run(it, res["status"], res["rows"], res["error"])
        s2.commit()
    finally:
        s2.close()
    out = {"status": res["status"], "item": item_id, "rows": res["rows"]}
    if res["error"]:
        out["error"] = res["error"]
    print(json.dumps(out, ensure_ascii=False))
    return 0 if res["status"] == "ok" else 1


def cli_register_cron(item_id: int) -> int:
    s = _session()
    try:
        item = s.get(PipelineCollectionItem, item_id)
    finally:
        s.close()
    if item is None:
        print(json.dumps({"status": "failed", "error": f"item {item_id} not found"}))
        return 1
    res = asyncio.run(register_cron_for_item(item))
    print(json.dumps({"status": res["status"], "task": res.get("task"), "schedule_id": res.get("schedule_id"), "error": res.get("error")}, ensure_ascii=False))
    return 0 if res["status"] == "ok" else 1


def cli_unregister_cron(item_id: int) -> int:
    s = _session()
    try:
        item = s.get(PipelineCollectionItem, item_id)
    finally:
        s.close()
    if item is None:
        print(json.dumps({"status": "failed", "error": f"item {item_id} not found"}))
        return 1
    res = asyncio.run(unregister_cron_for_item(item))
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res["status"] == "ok" else 1


def cli_sync_cron() -> int:
    res = asyncio.run(sync_pipeline_cron())
    print(json.dumps(res, ensure_ascii=False))
    return 0 if not res["failed"] else 1

"""
MCP Server for Dashboard — browse databases, manage cron tasks, query data.

Exposes tools that Claude Code can invoke directly:
  list_databases     — list all registered SQLite databases
  query_table        — run read-only queries against any database
  list_datasources   — list managed datasources
  get_executions     — get cron execution history
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent  # mcp/
load_dotenv(ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=True)

from fastmcp import FastMCP
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Datasource, DatasourceColumn, Dashboard, Execution, Schedule, Task

from dashboard_database import DashboardDatabase

app = FastMCP(name="dashboard-mcp")

# Repo root, for resolving relative sqlite:/// DAAS_DATABASE_URL paths.
# The dashboard-mcp process may be launched with a cwd other than the repo
# root, so a relative `sqlite:///mcp/daas.db` would otherwise resolve
# against the wrong directory and read a different (or empty) DB than
# daas-mcp. Mirrors daas-mcp/daas_database.py `_resolve_url`.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_url(url: str) -> str:
    """Resolve a relative sqlite:/// path against the repo root. Pass through
    otherwise (absolute paths, :memory:, non-sqlite URLs)."""
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"sqlite:///{(_REPO_ROOT / path).resolve()}"
    return url


def _get_db_url() -> str:
    """Get the database URL from env or default to the canonical repo-root
    mcp/daas.db, resolved against the repo root so cwd doesn't matter.
    Never falls back to dashboard-mcp's local daas.db (closes the stale-DB
    gotcha where query_table read mcp/dashboard-mcp/daas.db)."""
    url = os.environ.get("DAAS_DATABASE_URL")
    if url:
        return _resolve_url(url)
    db_path = _REPO_ROOT / "mcp" / "daas.db"
    return f"sqlite:///{db_path}"


def _get_engine():
    url = _get_db_url()
    return create_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
    )


def _get_session():
    engine = _get_engine()
    return sessionmaker(bind=engine)()


def _init_db():
    """Ensure all tables exist."""
    engine = _get_engine()
    Base.metadata.create_all(engine)


# Ensure tables exist at import time
_init_db()


def _connect(name: str, readonly: bool = True) -> sqlite3.Connection:
    """Low-level sqlite3 connection for direct table queries."""
    db_path = _get_db_url().replace("sqlite:///", "")
    if readonly:
        uri = f"file:{db_path}?mode=ro"
        return sqlite3.connect(uri, uri=True)
    return sqlite3.connect(db_path)


@app.tool
def list_databases() -> str:
    """List all known SQLite databases in the MCP directory with table counts."""
    known = ["daas"]
    result = []
    for name in known:
        try:
            conn = _connect(name)
            tables = [
                row[0] for row in
                conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            ]
            conn.close()
            result.append({"name": f"{name}.db", "tables": tables, "table_count": len(tables)})
        except Exception as e:
            result.append({"name": f"{name}.db", "error": str(e)})
    return json.dumps(result, ensure_ascii=False, indent=2)


@app.tool
def query_table(database: str, table: str, limit: int = 50, offset: int = 0) -> str:
    """Run a read-only query against a database table. Returns rows as JSON."""
    try:
        conn = _connect(database)
        cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        rows = conn.execute(f'SELECT * FROM "{table}" LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        conn.close()

        data = []
        for row in rows:
            data.append({cols[i]: row[i] for i in range(len(cols))})

        return json.dumps({
            "columns": cols,
            "rows": data,
            "total": count,
            "limit": limit,
            "offset": offset,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@app.tool
def list_datasources() -> str:
    """List managed datasources from daas.db (schema managed by mcp/models/models.py)."""
    try:
        session = _get_session()
        try:
            rows = session.query(Datasource).order_by(Datasource.name).all()
            result = [
                {
                    "id": r.id,
                    "name": r.name,
                    "db_type": r.db_type,
                    "connection_string": r.connection_string,
                    "description": r.description,
                    "is_readonly": r.is_readonly,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ]
            return json.dumps(result, ensure_ascii=False, indent=2)
        finally:
            session.close()
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@app.tool
def get_executions(database: str = "daas", limit: int = 50, schedule_id: str = "") -> str:
    """Get cron execution history from a database."""
    try:
        conn = _connect(database)
        if schedule_id:
            rows = conn.execute(
                "SELECT * FROM executions WHERE schedule_id = ? ORDER BY started_at DESC LIMIT ?",
                (schedule_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM executions ORDER BY started_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        cols = ["id", "schedule_id", "started_at", "finished_at", "status", "output"]
        result = []
        for row in rows:
            result.append({cols[i]: row[i] for i in range(len(cols))})
        conn.close()
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@app.tool
def get_stats() -> str:
    """Get aggregated statistics across all MCP databases."""
    stats = {}
    try:
        session = _get_session()
        try:
            stats["datasources"] = session.query(Datasource).count()
            stats["schedules"] = session.query(Schedule).count()
            stats["executions"] = session.query(Execution).count()
            stats["tasks"] = session.query(Task).count()
        finally:
            session.close()
    except Exception as e:
        stats["error"] = str(e)

    return json.dumps(stats, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────────
# Dashboard registry — standalone-HTML dashboard metadata (the
# `dashboards` table). DB is the single source of truth; index.html +
# daas.md are regenerated from it on every write. JSON fields
# (source_tables / entity_coverage / time_range / chart_config) accept a
# JSON string OR a list/dict.
# ─────────────────────────────────────────────────────────────────

@app.tool
def register(slug: str, name: str, intro: str, source_tables, refresh_cadence: str,
                       file_path: str, file_url: str, entity_coverage=None, time_range=None,
                       chart_config=None) -> str:
    """Register or update (upsert by slug) a standalone HTML dashboard in the
    `dashboards` table. After writing the row, regenerates index.html + daas.md
    from the DB. `source_tables`/`entity_coverage`/`time_range`/`chart_config`
    accept a JSON string or a list/dict. Returns the stored row (with `action`
    = inserted|updated) or `{"error": ...}`."""
    db = DashboardDatabase()
    result = db.register(slug, name, intro, source_tables, refresh_cadence,
                         file_path, file_url, entity_coverage, time_range, chart_config)
    return json.dumps(result, ensure_ascii=False, indent=2)


@app.tool
def list() -> str:  # noqa: A004 (intentional: registers as dashboard_list)
    """List every registered dashboard (name + slug + intro + file_url), oldest
    first. Returns a JSON array."""
    db = DashboardDatabase()
    return json.dumps(db.list_all(), ensure_ascii=False, indent=2)


@app.tool
def get(slug: str) -> str:
    """Get one dashboard's full metadata by slug: name, intro, source_tables,
    entity_coverage, time_range, refresh_cadence, chart_config, file_path,
    file_url. Returns `{"error": "dashboard '<slug>' not found"}` if absent."""
    db = DashboardDatabase()
    return json.dumps(db.get(slug), ensure_ascii=False, indent=2)


@app.tool
def search(keyword: str) -> str:
    """Case-insensitive keyword search over each dashboard's name + intro +
    refresh_cadence + source_tables. Returns matching `{slug, name, intro}`
    entries, or an empty array if none match."""
    db = DashboardDatabase()
    return json.dumps(db.search(keyword), ensure_ascii=False, indent=2)


@app.tool
def update(slug: str, name=None, intro=None, source_tables=None,
                     entity_coverage=None, time_range=None, refresh_cadence=None,
                     chart_config=None, file_path=None, file_url=None) -> str:
    """Patch one or more fields of an existing dashboard (by slug). Only the
    fields you pass are changed; the rest are left as-is. Regenerates index.html
    + daas.md after the update. Returns the updated row or `{"error": ...}`."""
    db = DashboardDatabase()
    result = db.update(slug, name, intro, source_tables, entity_coverage,
                       time_range, refresh_cadence, chart_config, file_path, file_url)
    return json.dumps(result, ensure_ascii=False, indent=2)


@app.tool
def delete(slug: str) -> str:
    """Delete a dashboard row by slug and regenerate index.html + daas.md. The
    HTML file itself is NOT removed (only the registry row). Returns
    `{"deleted": slug}` or `{"error": ...}`."""
    db = DashboardDatabase()
    return json.dumps(db.delete(slug), ensure_ascii=False, indent=2)

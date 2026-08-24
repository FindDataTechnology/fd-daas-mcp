"""Database singleton for workflow-mcp — SQLAlchemy engine + session factory.

Self-contained (imports only the shared ``models`` package, no gateway-mcp), so
the workflow layer survives leader dissolution (P4). Mirrors
``research-mcp/research_database.py``; shares the same ``DAAS_DATABASE_URL`` /
``daas.db`` as every other group. Re-runs the additive ``workflows`` column
migration (version/manifest/enabled) so a legacy daas.db that predates D4 is
brought up to date here too.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from models import Base

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "daas.db"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_url(url: str) -> str:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"sqlite:///{(_REPO_ROOT / path).resolve()}"
    return url


class Database:
    _instance: "Database | None" = None

    def __init__(self, db_url: str | None = None):
        if db_url is None:
            db_url = os.environ.get("DAAS_DATABASE_URL") or f"sqlite:///{_DEFAULT_DB_PATH}"
        db_url = _resolve_url(db_url)
        self._engine = create_engine(db_url, echo=False)
        if self._engine.dialect.name == "sqlite":
            @event.listens_for(self._engine, "connect")
            def _enable_fk(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
                dbapi_conn.create_function(
                    "REGEXP", 2,
                    lambda pattern, value: 1 if (value is not None and re.search(pattern, str(value)) is not None) else 0,
                )
        self._session_factory = sessionmaker(bind=self._engine)
        Base.metadata.create_all(self._engine)
        self._ensure_workflow_columns()

    def _ensure_workflow_columns(self) -> None:
        """Guarded ALTER for legacy ``workflows`` rows. Fresh tables already
        get these columns from ``create_all``."""
        from sqlalchemy import inspect, text

        insp = inspect(self.engine)
        if "workflows" not in insp.get_table_names():
            return
        existing = {c["name"] for c in insp.get_columns("workflows")}
        with self.engine.begin() as conn:
            if "version" not in existing:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN version INTEGER NOT NULL DEFAULT 1"))
            if "manifest" not in existing:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN manifest TEXT"))
            if "enabled" not in existing:
                conn.execute(text("ALTER TABLE workflows ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"))
            conn.execute(text("UPDATE workflows SET version = 1 WHERE version IS NULL"))
            conn.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_name_version ON workflows (name, version)")
            )

    def get_session(self) -> Session:
        return self._session_factory()

    @property
    def engine(self):
        return self._engine

    @classmethod
    def get_instance(cls) -> "Database":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


def get_database() -> Database:
    return Database.get_instance()

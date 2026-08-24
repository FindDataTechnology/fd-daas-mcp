"""Database singleton for research-mcp - SQLAlchemy engine + session factory.

Mirrors ``daas-mcp/daas_database.py`` but without the legacy additive
migrations (the ``researches`` table is new, created fresh by
``Base.metadata.create_all``). Shares the same ``DAAS_DATABASE_URL`` /
``daas.db`` as every other group; ``Base`` is the shared ``models`` package, so
``create_all`` creates the ``researches`` table (and idempotently skips every
existing table).
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
    """Resolve a relative sqlite:/// path against the repo root. Mirrors
    daas_database._resolve_url so a relative DAAS_DATABASE_URL works regardless
    of the process cwd."""
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"sqlite:///{(_REPO_ROOT / path).resolve()}"
    return url


class Database:
    """Singleton database accessor for the research group."""

    _instance: "Database | None" = None

    def __init__(self, db_url: str | None = None):
        if db_url is None:
            db_url = self._default_url()
        db_url = _resolve_url(db_url)
        self._engine = create_engine(db_url, echo=False)
        # SQLite ignores ON DELETE CASCADE unless PRAGMA foreign_keys=ON.
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
        # Creates `researches` (new) + idempotently skips all existing tables.
        Base.metadata.create_all(self._engine)

    @staticmethod
    def _default_url() -> str:
        url = os.environ.get("DAAS_DATABASE_URL")
        if url:
            return url
        reg_db = os.environ.get("DAAS_REGISTRY_DB")
        if reg_db:
            return f"sqlite:///{reg_db}"
        return f"sqlite:///{_DEFAULT_DB_PATH}"

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

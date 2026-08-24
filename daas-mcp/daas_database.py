"""Database singleton for daas-mcp - SQLAlchemy engine + session factory.

Default DB resolution (when DAAS_DATABASE_URL is unset): a writable, predictable
location - ``<cwd>/daas.db`` if the cwd is writable and not inside the installed
package, otherwise ``~/.fd-daas-mcp/daas.db`` (created on demand). Never inside
the installed package (read-only under a normal ``pip install``). Override with
DAAS_DATABASE_URL.
"""
from __future__ import annotations

import os
import re
import json
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, Session

from models import Base

# Package root (fd-daas-mcp/) - used to detect when the cwd is inside the
# installed package so the default never writes there.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # fd-daas-mcp/
# User data dir for the dotdir fallback (read-only cwd case).
_USER_DATA_DIR = Path.home() / ".fd-daas-mcp"
# Repo root - for anchoring relative sqlite:/// paths in _resolve_url so a
# relative DAAS_DATABASE_URL works regardless of the process cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def is_writable_dir(path: Path) -> bool:
    """True if `path` is an existing writable directory (write-probe)."""
    try:
        if not path.is_dir():
            return False
        fd, name = tempfile.mkstemp(prefix=".daas-writecheck-", suffix=".tmp", dir=str(path))
        os.close(fd)
        os.unlink(name)
        return True
    except OSError:
        return False


def inside_installed_package(path: Path) -> bool:
    """True if `path` resolves under the installed package root (the parent of
    this file's package). Prevents the cwd-default from writing into the
    installed package when the process happens to run from there."""
    try:
        return str(path.resolve()).startswith(str(_PACKAGE_ROOT.resolve()))
    except OSError:
        return False


def default_db_path() -> Path:
    """Resolve a writable default DB path when no env var is set.

    Order: ``<cwd>/daas.db`` if the cwd is writable and not inside the installed
    package; otherwise ``~/.fd-daas-mcp/daas.db`` (directory created on demand).
    Never returns a path inside the installed package.
    """
    cwd = Path.cwd()
    if is_writable_dir(cwd) and not inside_installed_package(cwd):
        return cwd / "daas.db"
    _USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _USER_DATA_DIR / "daas.db"


def _resolve_url(url: str) -> str:
    """Resolve a relative sqlite:/// path against the repo root. Pass through
    otherwise (absolute paths, :memory:, non-sqlite URLs). Mirrors
    process_database.py (now a sibling in this dir, relocated from the former
    process-mcp) so a relative DAAS_DATABASE_URL
    (e.g. `sqlite:///mcp/daas.db`) works regardless of the process cwd.
    """
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"sqlite:///{(_REPO_ROOT / path).resolve()}"
    return url


def resolve_db_url(db_url: str | None = None) -> str:
    """Resolve the DB URL without constructing the engine or creating the
    schema - safe for read-only inspection (``doctor``).

    Explicit ``db_url`` wins; otherwise DAAS_DATABASE_URL / legacy
    DAAS_REGISTRY_DB / :func:`default_db_path`. Applies :func:`_resolve_url`
    (repo-root anchoring for relative paths).
    """
    if db_url is None:
        db_url = Database._default_url()
    return _resolve_url(db_url)


class Database:
    """Singleton database accessor."""

    _instance: "Database | None" = None

    def __init__(self, db_url: str | None = None):
        if db_url is None:
            db_url = self._default_url()
        db_url = _resolve_url(db_url)
        self._resolved_url = db_url
        self._engine = create_engine(db_url, echo=False)
        # SQLite ignores ON DELETE CASCADE unless PRAGMA foreign_keys=ON.
        # ponytail: enable per-connection so cascade deletes actually fire.
        if self._engine.dialect.name == "sqlite":
            @event.listens_for(self._engine, "connect")
            def _enable_fk(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                # ponytail: WAL + busy_timeout fixes "database is locked" when the
                # in-process workflow engine writes observations while the server
                # holds another connection (same process, journal_mode=delete
                # serializes writers and throws). WAL allows concurrent readers +
                # one writer; busy_timeout makes a writer wait instead of failing
                # immediately. Upgrade path: shared connection pool if contention
                # persists.
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=10000")
                cur.close()
                # Register a Python-side REGEXP function so entity-collection
                # rule filters can use `name_regex` via `Entity.name.op('REGEXP')`.
                # SQLite has no built-in REGEXP; this wires Python's `re` in.
                dbapi_conn.create_function(
                    "REGEXP", 2, lambda pattern, value: 1 if (value is not None and re.search(pattern, str(value)) is not None) else 0
                )
        self._session_factory = sessionmaker(bind=self._engine)
        Base.metadata.create_all(self._engine)
        self._migrate_sources_category_id()
        self._migrate_collection_items_sort_order()
        self._migrate_sources_score()
        self._migrate_collection_items_score()
        self._migrate_functions_frequency()
        self._migrate_entity_collections_rule_script()
        self._migrate_entity_collections_rule_id()
        self._migrate_indicator_collections_rule_id()
        self._migrate_legacy_rule_scripts_to_rules()
        self._migrate_drop_process_rules()

    def _migrate_sources_category_id(self) -> None:
        """Idempotent: add `category_id` to a pre-existing `sources` table.

        create_all adds the column on fresh DBs but won't ALTER an existing
        table. SQLite supports ADD COLUMN; guard on PRAGMA table_info so it
        runs exactly once. ponytail: additive only, no destructive migration.
        """
        insp = inspect(self._engine)
        if "sources" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("sources")]
        if "category_id" in cols:
            return
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE sources ADD COLUMN category_id INTEGER"))

    def _migrate_collection_items_sort_order(self) -> None:
        """Idempotent: add `sort_order` to a pre-existing
        `datasource_collection_items` table. ponytail: same pattern as category_id.
        """
        insp = inspect(self._engine)
        if "datasource_collection_items" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("datasource_collection_items")]
        if "sort_order" in cols:
            return
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE datasource_collection_items "
                    "ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
                )
            )

    def _migrate_sources_score(self) -> None:
        """Idempotent: add nullable `score` (REAL) to a pre-existing `sources`
        table. The default priority/quality weight for a datasource; NULL means
        unset. ponytail: same guard pattern as category_id / sort_order.
        """
        insp = inspect(self._engine)
        if "sources" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("sources")]
        if "score" in cols:
            return
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE sources ADD COLUMN score REAL"))

    def _migrate_collection_items_score(self) -> None:
        """Idempotent: add nullable `score` (REAL) to a pre-existing
        `datasource_collection_items` table. A per-collection override of the
        datasource's default score; NULL means inherit the datasource default.
        ponytail: same guard pattern as sort_order.
        """
        insp = inspect(self._engine)
        if "datasource_collection_items" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("datasource_collection_items")]
        if "score" in cols:
            return
        with self._engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE datasource_collection_items ADD COLUMN score REAL")
            )

    def _migrate_functions_frequency(self) -> None:
        """Idempotent: add nullable `frequency` (VARCHAR(64)) to a pre-existing
        `daas_functions` table. Describes the data refresh cadence of a function
        (e.g. daily/weekly/monthly/quarterly/yearly/realtime/irregular); NULL
        means unset. ponytail: same guard pattern as score / sort_order.
        """
        insp = inspect(self._engine)
        if "daas_functions" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("daas_functions")]
        if "frequency" in cols:
            return
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE daas_functions ADD COLUMN frequency VARCHAR(64)"))

    def _migrate_entity_collections_rule_script(self) -> None:
        """Idempotent: add nullable `rule_script` (TEXT) to a pre-existing
        `entity_collections` table. Stores a repo-root-relative path to a Python
        rule script defining `members(ctx)` - the script analogue of `rule_json`.
        Mutually exclusive with `rule_json`. ponytail: same guard pattern as
        the other additive ALTERs.
        """
        insp = inspect(self._engine)
        if "entity_collections" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("entity_collections")]
        if "rule_script" in cols:
            return
        with self._engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE entity_collections ADD COLUMN rule_script TEXT")
            )

    def _migrate_entity_collections_rule_id(self) -> None:
        """Idempotent: add nullable `rule_id` (INTEGER) to a pre-existing
        `entity_collections` table. Canonical reference to a `rules` row; the
        legacy `rule_json`/`rule_script` columns remain for back-compat. On a
        fresh DB, `create_all` creates the column with the FK from the model."""
        insp = inspect(self._engine)
        if "entity_collections" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("entity_collections")]
        if "rule_id" in cols:
            return
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE entity_collections ADD COLUMN rule_id INTEGER"))

    def _migrate_indicator_collections_rule_id(self) -> None:
        """Idempotent: add nullable `rule_id` (INTEGER) to a pre-existing
        `indicator_collections` table - the canonical rule reference for
        indicator collections (which previously had no rule support)."""
        insp = inspect(self._engine)
        if "indicator_collections" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("indicator_collections")]
        if "rule_id" in cols:
            return
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE indicator_collections ADD COLUMN rule_id INTEGER"))

    def _migrate_legacy_rule_scripts_to_rules(self) -> None:
        """Idempotent data migration: for each entity_collection that has a
        legacy `rule_script` set and no `rule_id`, create a `script` rule row
        (target='entity_ids') and point the collection's `rule_id` at it. This
        migrates the live `us_leadership_pool` collection (and any other legacy
        rule_script) onto the unified `rules` store so the engine-backed sync
        takes over from the broken `entity_rule_script` import path."""
        insp = inspect(self._engine)
        if "rules" not in insp.get_table_names() or "entity_collections" not in insp.get_table_names():
            return
        ec_cols = [c["name"] for c in insp.get_columns("entity_collections")]
        if "rule_id" not in ec_cols or "rule_script" not in ec_cols:
            return
        with self._engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, name, rule_script FROM entity_collections "
                    "WHERE rule_script IS NOT NULL AND rule_id IS NULL"
                )
            ).fetchall()
            for row in rows:
                coll_id, coll_name, script_path = row[0], row[1], row[2]
                config = json.dumps({"script_path": script_path, "function": "members"})
                rule_name = coll_name
                clash = conn.execute(
                    text("SELECT id FROM rules WHERE name = :n"), {"n": rule_name}
                ).fetchone()
                if clash is not None:
                    rule_name = f"ec_{coll_id}_{coll_name}"
                result = conn.execute(
                    text(
                        "INSERT INTO rules (name, rule_type, target, config_json, "
                        "description, enabled) VALUES (:name, 'script', 'entity_ids', "
                        ":config, :desc, 1)"
                    ),
                    {"name": rule_name, "config": config, "desc": "migrated from rule_script"},
                )
                conn.execute(
                    text("UPDATE entity_collections SET rule_id = :rid WHERE id = :cid"),
                    {"rid": result.lastrowid, "cid": coll_id},
                )

    def _migrate_drop_process_rules(self) -> None:
        """Idempotent: drop the legacy `process_rules` table (empty; its
        rule-definition role moved to the unified `rules` table's `llm` type)
        and rebuild `process_results` with `rule_id` FK->`rules.id`. No-op once
        `process_rules` is gone (fresh DBs never create it)."""
        insp = inspect(self._engine)
        if "process_rules" not in insp.get_table_names():
            return
        with self._engine.begin() as conn:
            if "process_results" in insp.get_table_names():
                conn.execute(text("DROP TABLE process_results"))
            conn.execute(text("DROP TABLE process_rules"))
        from models import ProcessResult

        ProcessResult.__table__.create(self._engine, checkfirst=True)

    @staticmethod
    def _default_url() -> str:
        url = os.environ.get("DAAS_DATABASE_URL")
        if url:
            return url
        # Legacy: DAAS_REGISTRY_DB is a filesystem path (not a URL). Wrapped
        # to a sqlite URL; _resolve_url then anchors any relative path to the
        # repo root.
        reg_db = os.environ.get("DAAS_REGISTRY_DB")
        if reg_db:
            return f"sqlite:///{reg_db}"
        # Writable default - cwd or ~/.fd-daas-mcp/daas.db. Never inside the
        # installed package. See default_db_path().
        return f"sqlite:///{default_db_path()}"

    def get_session(self) -> Session:
        return self._session_factory()

    @property
    def engine(self):
        """Underlying SQLAlchemy engine - used by pipeline_tools for raw
        sqlite upserts into scraw_<slug> tables."""
        return self._engine

    @property
    def resolved_url(self) -> str:
        """The DB URL this instance was constructed with (after _resolve_url).
        Used by ``init``/``doctor`` to report where the database lives."""
        return self._resolved_url

    @classmethod
    def get_instance(cls) -> "Database":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance


def get_database() -> Database:
    return Database.get_instance()


def provision_database(db_url: str | None = None) -> tuple[Database, str]:
    """Construct (and cache) the database singleton, ensuring the full schema is
    provisioned (``Base.metadata.create_all`` + every group's idempotent
    ``init_db()`` DDL). Returns ``(database, resolved_url)``. Idempotent.

    When ``db_url`` is None, uses the singleton (default resolution: env var or
    writable default path). When ``db_url`` is given, constructs a fresh
    Database for that URL without replacing the singleton (one-shot provisioning
    into a custom path, e.g. ``fd-daas-mcp init --db-url X``).
    """
    if db_url is None:
        db = get_database()
    else:
        db = Database(db_url)
    return db, db.resolved_url

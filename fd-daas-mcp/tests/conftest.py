"""Shared pytest fixtures for the fd-daas-mcp test suite.

Environment notes:
- The ``.venv`` leaks micromamba ``site-packages`` whose logfire pytest plugin
  crashes on import (``opentelemetry._ail_sampling``). Disabled via ``addopts``
  in ``pyproject.toml`` (``-p no:logfire -p no:pytest_logfire``) - a no-op in a
  clean venv.
- ``DAAS_DATABASE_URL`` is pointed at a throwaway SQLite file so cron's
  import-time ``init_db()`` never touches the real ``daas.db``.
- Each test gets a fresh registry cache via the autouse ``reset_registry`` fixture.

Dual-backend legs (openspec: wire-cell-postgres-storage): set
``FD_ENGINE_TEST_BACKEND=postgres`` to run the suite against a PostgreSQL data
root instead of SQLite. Requires ``FD_ENGINE_PG_TEST_URL`` (a one-shot
``postgresql+psycopg://`` test database); without it the whole session skips
with the reason attached to every test.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make fd-daas-mcp/ (the daas package) and models/ importable regardless
# of the cwd pytest was invoked from.
_FD_HOME = Path(__file__).resolve().parents[1]
if str(_FD_HOME) not in sys.path:
    sys.path.insert(0, str(_FD_HOME))
_MODELS = _FD_HOME / "models"
if str(_MODELS) not in sys.path:
    sys.path.insert(0, str(_MODELS))

# ── dual-backend selection (wire-cell-postgres-storage) ───────────────

TEST_BACKEND = os.environ.get("FD_ENGINE_TEST_BACKEND", "sqlite").strip().lower()
PG_TEST_URL = os.environ.get("FD_ENGINE_PG_TEST_URL", "").strip()
PG_LEG_ACTIVE = TEST_BACKEND == "postgres"
PG_LEG_MISSING_URL = PG_LEG_ACTIVE and not PG_TEST_URL


def engine_db_url(name: str) -> str:
    """Data-root URL for one test's throwaway database. sqlite: a fresh file
    under the caller's tmp_path (the legacy behavior). postgres: the shared
    one-shot PG test database — callers pair this with ``reset_pg_schema()``
    for per-test isolation (the PG analogue of a fresh file)."""
    if PG_LEG_ACTIVE:
        return PG_TEST_URL
    return f"sqlite:///{name}"


def reset_pg_schema() -> None:
    """Drop and recreate the public schema of the PG test database (one-shot
    dedicated DB; ORM tables are rebuilt by the fixtures'/create_all paths on
    the next engine init, dynamic dataset tables by the service itself)."""
    if not PG_LEG_ACTIVE:
        return
    from sqlalchemy import create_engine, text

    eng = create_engine(PG_TEST_URL)
    try:
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        eng.dispose()


def dialect_cursor(conn):
    """``conn.cursor()`` wrapped with the engine's dialect proxy, so test-side
    raw introspection (``sqlite_master`` / ``PRAGMA table_info``) runs
    verbatim on both backends — sqlite returns the plain cursor (legacy
    behavior), PG translates. The assertions stay identical on both legs."""
    _CD = _FD_HOME / "customer-dataset-mcp"
    if str(_CD) not in sys.path:
        sys.path.insert(0, str(_CD))
    try:
        import db_dialect
    finally:
        if str(_CD) in sys.path and sys.path[0] == str(_CD):
            sys.path.remove(str(_CD))
    return db_dialect.wrap_cursor(conn.cursor())


# Throwaway DB for any import-time init_db() (cron) - never the real daas.db.
# Override any inherited DAAS_DATABASE_URL so tests never touch the real DB.
# On the PG leg every group binds the one-shot PG test database instead — and
# the schema is dropped once per SESSION so every run starts from a virgin
# data root, the exact semantics the throwaway sqlite file gives the sqlite leg.
_TMPDIR = Path(tempfile.mkdtemp(prefix="fd-daas-mcp-test-"))
if PG_LEG_ACTIVE:
    os.environ["DAAS_DATABASE_URL"] = PG_TEST_URL
    if not PG_LEG_MISSING_URL:
        reset_pg_schema()
else:
    os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"

if PG_LEG_MISSING_URL:
    # Backend selected but no database configured: skip the WHOLE session with
    # the reason attached to every collected item (never fail spuriously).
    def pytest_collection_modifyitems(items):
        skip = pytest.mark.skip(
            reason="FD_ENGINE_TEST_BACKEND=postgres 但未设置 FD_ENGINE_PG_TEST_URL"
            "（整腿跳过；提供 postgresql+psycopg:// 测试库即可运行）"
        )
        for item in items:
            item.add_marker(skip)

from daas.fd_daas_mcp import registry  # noqa: E402


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset the registry build cache + report before and after every test."""
    registry.reset_cache()
    yield
    registry.reset_cache()
    if PG_LEG_ACTIVE:
        # the PG test server caps connections; every registry rebuild spins up
        # ~10 group engine pools and the evicted module graphs only release
        # their pooled psycopg connections under the cyclic collector — run it
        # eagerly so one process never exhausts the server's slots
        import gc

        gc.collect()


@pytest.fixture
def fake_optional_source():
    """Inject a fake optional source whose dep is absent, so ``skipped_optional``
    is exercised deterministically. Restores ``registry.SOURCES`` afterward.

    The absent dep short-circuits ``build()`` before any directory is touched,
    so the nonexistent ``dir`` is never accessed.
    """
    saved = dict(registry.SOURCES)
    registry.SOURCES["__fake_optional__"] = {
        "dir": "__does_not_exist__",
        "inline": True,
        "optional": True,
        "dep": "__definitely_not_a_real_module__",
    }
    registry.reset_cache()
    yield registry.SOURCES["__fake_optional__"]
    registry.SOURCES.clear()
    registry.SOURCES.update(saved)
    registry.reset_cache()


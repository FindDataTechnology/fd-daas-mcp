"""Starter catalog seed - the ``database-autobootstrap`` capability.

Covers: dep-free insert (enabled=False), idempotency, no source-lib imports, and
non-clobbering of user-added sources.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

_FD_HOME = Path(__file__).resolve().parents[1]
for _sub in ("daas-mcp", "models"):
    _p = _FD_HOME / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from daas_database import Database  # noqa: E402
from seed_starter_catalog import (  # noqa: E402
    STARTER_SOURCES,
    seed_starter_catalog,
    should_seed,
)


@pytest.fixture
def db_session(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/seed.db")
    s = db.get_session()
    yield s
    s.close()


def test_starter_sources_all_disabled():
    assert all(src["enabled"] is False for src in STARTER_SOURCES)
    names = {src["name"] for src in STARTER_SOURCES}
    assert {"akshare", "yfinance", "worldbank", "edgar"} <= names


def test_seed_inserts_dep_free_sources(db_session):
    r = seed_starter_catalog(db_session)
    assert r["inserted"] == len(STARTER_SOURCES)
    assert r["skipped"] == 0
    count = db_session.execute(text("SELECT COUNT(*) FROM sources")).scalar()
    assert count == len(STARTER_SOURCES)
    # All seeded sources disabled.
    enabled = [row[0] for row in db_session.execute(text("SELECT enabled FROM sources")).fetchall()]
    assert all(e == 0 for e in enabled)


def test_seed_is_idempotent(db_session):
    seed_starter_catalog(db_session)
    r2 = seed_starter_catalog(db_session)
    assert r2["inserted"] == 0
    assert r2["skipped"] == len(STARTER_SOURCES)


def test_seed_does_not_clobber_user_source(db_session):
    seed_starter_catalog(db_session)
    db_session.execute(
        text("INSERT INTO sources (name, label, enabled) VALUES ('custom', 'My Source', 1)")
    )
    db_session.commit()
    seed_starter_catalog(db_session)  # re-seed
    row = db_session.execute(
        text("SELECT enabled, label FROM sources WHERE name='custom'")
    ).fetchone()
    assert row == (1, "My Source")  # user source untouched


def test_should_seed_policy(db_session):
    # empty -> seed
    assert should_seed(db_session) is True
    seed_starter_catalog(db_session)
    # populated -> skip by default
    assert should_seed(db_session) is False
    # force -> seed
    assert should_seed(db_session, force=True) is True
    # no_seed -> never
    assert should_seed(db_session, no_seed=True) is False


def test_seed_module_imports_no_source_libs():
    """The seed module must not import any source data library."""
    import seed_starter_catalog as mod
    src = Path(mod.__file__).read_text(encoding="utf-8")
    # None of these may appear as a top-level import.
    for forbidden in ("import akshare", "import yfinance", "import world_bank_data",
                      "import edgar", "import edinet", "import dartlab", "import ckanapi"):
        assert forbidden not in src, f"seed module must not import {forbidden!r}"

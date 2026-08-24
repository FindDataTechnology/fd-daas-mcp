"""Shared pytest fixtures for the fd-daas-mcp test suite.

Environment notes:
- The ``.venv`` leaks micromamba ``site-packages`` whose logfire pytest plugin
  crashes on import (``opentelemetry._tail_sampling``). Disabled via ``addopts``
  in ``pyproject.toml`` (``-p no:logfire -p no:pytest_logfire``) - a no-op in a
  clean venv.
- ``DAAS_DATABASE_URL`` is pointed at a throwaway SQLite file so cron's
  import-time ``init_db()`` never touches the real ``daas.db``.
- Each test gets a fresh registry cache via the autouse ``reset_registry`` fixture.
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

# Throwaway DB for any import-time init_db() (cron) - never the real daas.db.
# Override any inherited DAAS_DATABASE_URL so tests never touch the real DB.
_TMPDIR = Path(tempfile.mkdtemp(prefix="fd-daas-mcp-test-"))
os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"

from daas.fd_daas_mcp import registry  # noqa: E402


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset the registry build cache + report before and after every test."""
    registry.reset_cache()
    yield
    registry.reset_cache()


@pytest.fixture
def fake_optional_source():
    """Inject a fake optional source whose dep is absent, so ``skipped_optional``
    is exercised deterministically. Restores ``registry.SOURCES`` afterward.

    The absent dep short-circuits ``build()`` before any directory is touched, so
    the nonexistent ``dir`` is never accessed.
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

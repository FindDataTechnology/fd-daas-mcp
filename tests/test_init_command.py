"""``fd-daas-mcp init`` command - the ``database-autobootstrap`` capability.

Uses CliRunner against the real ``cli`` group; ``init`` is dispatched before the
tool registry is built (verified separately in test_cli.py).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from daas.fd_daas_mcp.cli import cli

_FD_HOME = Path(__file__).resolve().parents[1]
for _sub in ("daas-mcp", "models"):
    _p = _FD_HOME / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from seed_starter_catalog import STARTER_SOURCES  # noqa: E402


def _init(tmp_path, *extra):
    url = f"sqlite:///{tmp_path}/init.db"
    r = CliRunner().invoke(cli, ["init", "--db-url", url, *extra, "--json"])
    return r, url


def test_init_creates_schema_and_seeds(tmp_path):
    r, url = _init(tmp_path)
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["schema_complete"] is True
    assert out["sources_after"] == len(STARTER_SOURCES)
    assert out["seed"]["inserted"] == len(STARTER_SOURCES)
    assert (tmp_path / "init.db").exists()


def test_init_idempotent_skips_seed_on_populated(tmp_path):
    r1, url = _init(tmp_path)
    assert r1.exit_code == 0
    r2 = CliRunner().invoke(cli, ["init", "--db-url", url, "--json"])
    assert r2.exit_code == 0
    out = json.loads(r2.output)
    assert out["seed"] is None  # skipped - already populated
    assert out["sources_after"] == len(STARTER_SOURCES)


def test_init_no_seed_flag_leaves_empty(tmp_path):
    r, _ = _init(tmp_path, "--no-seed")
    assert r.exit_code == 0
    out = json.loads(r.output)
    assert out["seed"] is None
    assert out["sources_after"] == 0


def test_init_seed_flag_idempotent_on_populated(tmp_path):
    _, url = _init(tmp_path)
    r = CliRunner().invoke(cli, ["init", "--db-url", url, "--seed", "--json"])
    assert r.exit_code == 0
    out = json.loads(r.output)
    # Forced seed re-runs the upsert: 0 inserted (all already present).
    assert out["seed"]["inserted"] == 0
    assert out["seed"]["skipped"] == len(STARTER_SOURCES)


def test_init_failure_exits_nonzero(tmp_path):
    # Parent dir does not exist; sqlite cannot open -> provisioning fails.
    url = f"sqlite:///{tmp_path}/no-such-dir/sub/daas.db"
    r = CliRunner().invoke(cli, ["init", "--db-url", url, "--json"])
    assert r.exit_code == 1
    assert "error" in r.output.lower()


def test_init_does_not_build_tool_registry(tmp_path):
    """init runs before the tool registry is built (spec scenario)."""
    from daas.fd_daas_mcp import registry
    registry.reset_cache()
    r, _ = _init(tmp_path)
    assert r.exit_code == 0
    # Accessing init did not populate the registry build cache.
    assert registry._BUILD_CACHE is None, "init must not build the tool registry"

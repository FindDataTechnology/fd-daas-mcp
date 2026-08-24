"""``fd-daas-mcp doctor`` command - the ``database-autobootstrap`` capability.

Covers: healthy DB (exit 0 + counts), missing DB (exit non-zero + no file
created), and the read-only invariant (no writes).
"""
from __future__ import annotations

import json
import os
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


def test_doctor_healthy_after_init(tmp_path):
    url = f"sqlite:///{tmp_path}/d.db"
    assert CliRunner().invoke(cli, ["init", "--db-url", url]).exit_code == 0
    r = CliRunner().invoke(cli, ["doctor", "--db-url", url, "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["healthy"] is True
    assert out["schema_complete"] is True
    assert out["file_exists"] is True
    assert out["row_counts"]["sources"] == len(STARTER_SOURCES)


def test_doctor_missing_db_exits_nonzero_and_creates_no_file(tmp_path):
    url = f"sqlite:///{tmp_path}/never.db"
    assert not os.path.exists(url.replace("sqlite:///", "", 1))
    r = CliRunner().invoke(cli, ["doctor", "--db-url", url, "--json"])
    assert r.exit_code == 1
    out = json.loads(r.output)
    assert out["healthy"] is False
    assert out["schema_complete"] is False
    assert out["file_exists"] is False
    # Read-only invariant: doctor must NOT create the missing file.
    assert not os.path.exists(url.replace("sqlite:///", "", 1)), \
        "doctor created the DB file - violates read-only"


def test_doctor_no_writes_to_existing_db(tmp_path):
    """doctor must not modify an existing DB: row counts unchanged + no new tables."""
    url = f"sqlite:///{tmp_path}/d.db"
    assert CliRunner().invoke(cli, ["init", "--db-url", url]).exit_code == 0
    db_path = url.replace("sqlite:///", "", 1)
    size_before = os.path.getsize(db_path)

    r = CliRunner().invoke(cli, ["doctor", "--db-url", url, "--json"])
    assert r.exit_code == 0
    out = json.loads(r.output)
    # No schema growth, sources unchanged.
    assert out["row_counts"]["sources"] == len(STARTER_SOURCES)
    # File size unchanged (no journal/wal/write).
    assert os.path.getsize(db_path) == size_before


def test_doctor_plain_text_output(tmp_path):
    url = f"sqlite:///{tmp_path}/d.db"
    CliRunner().invoke(cli, ["init", "--db-url", url])
    r = CliRunner().invoke(cli, ["doctor", "--db-url", url])
    assert r.exit_code == 0
    assert "DAAS database:" in r.output
    assert "file_exists: True" in r.output

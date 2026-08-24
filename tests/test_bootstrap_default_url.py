"""Default DB path resolution - the ``database-autobootstrap`` capability.

Covers: writable-cwd default, read-only-cwd -> ~/.fd-daas-mcp fallback, absolute
/ :memory: passthrough, and the invariant that the default never resolves inside
the installed package.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make daas-mcp/ (daas_database) + models/ importable.
_FD_HOME = Path(__file__).resolve().parents[1]
for _sub in ("daas-mcp", "models"):
    _p = _FD_HOME / _sub
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import daas_database as D  # noqa: E402


def test_default_uses_writable_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("DAAS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DAAS_REGISTRY_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    p = D.default_db_path()
    assert p == tmp_path / "daas.db"
    assert not D.inside_installed_package(tmp_path)


def test_default_falls_back_to_user_data_dir_when_cwd_unwritable(monkeypatch, tmp_path):
    monkeypatch.delenv("DAAS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DAAS_REGISTRY_DB", raising=False)
    fake_user_dir = tmp_path / ".fd-daas-mcp"
    monkeypatch.setattr(D, "_USER_DATA_DIR", fake_user_dir)
    # Simulate an unwritable cwd (covers read-only cwd AND root-in-CI, where
    # chmod is ignored - patching the probe is the only robust check).
    monkeypatch.setattr(D, "is_writable_dir", lambda _p: False)
    p = D.default_db_path()
    assert p == fake_user_dir / "daas.db"
    # The dotdir is created on demand.
    assert fake_user_dir.is_dir()


def test_default_never_inside_installed_package(monkeypatch):
    # cwd = the package dir itself -> must NOT resolve to <package>/daas.db.
    pkg = Path(D.__file__).resolve().parent.parent  # fd-daas-mcp/
    monkeypatch.chdir(pkg)
    assert D.inside_installed_package(pkg)
    p = D.default_db_path()
    assert p != pkg / "daas.db"


def test_resolve_db_url_absolute_passthrough():
    assert D.resolve_db_url("sqlite:////abs/path/daas.db") == "sqlite:////abs/path/daas.db"


def test_resolve_db_url_memory_passthrough():
    assert D.resolve_db_url("sqlite:///:memory:") == "sqlite:///:memory:"


def test_default_url_unset_uses_default_path(monkeypatch, tmp_path):
    monkeypatch.delenv("DAAS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DAAS_REGISTRY_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    url = D.Database._default_url()
    assert url == f"sqlite:///{tmp_path}/daas.db"


def test_default_url_env_var_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DAAS_DATABASE_URL", f"sqlite:///{tmp_path}/from-env.db")
    monkeypatch.chdir(tmp_path)
    url = D.Database._default_url()
    assert url == f"sqlite:///{tmp_path}/from-env.db"


def test_provision_database_returns_resolved_url(tmp_path):
    url = f"sqlite:///{tmp_path}/prov.db"
    db, resolved = D.provision_database(url)
    assert resolved == url
    from sqlalchemy import inspect
    tabs = inspect(db.engine).get_table_names()
    assert "sources" in tabs and "observations" in tabs

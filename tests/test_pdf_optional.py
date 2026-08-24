"""Tests for the optional ``pdf`` tool group.

Asserts the optional-group contract from
openspec/changes/add-pdf-vector-search/specs/fd-daas-mcp-test-suite/spec.md:
  - ``pdf`` is registered as an OPTIONAL group gated on ``sqlite_vec``.
  - When the dep is absent, ``pdf`` is ``skipped_optional`` (not ``failed``)
    and the six core groups + ``>= 155`` tools still hold.
  - When the dep is present, ``pdf`` registers its tools (skipped otherwise).

No model download, no network. The "absent" case is simulated by monkeypatching
``registry._can_import`` so the test is hermetic regardless of whether the
``[pdf]`` extra is installed in the running venv.
"""
from __future__ import annotations

import pytest

from daas.fd_daas_mcp import registry

CORE = {"alerts", "cron", "composite", "daas", "dashboard", "gateway"}


def _group_counts(tools):
    counts: dict[str, int] = {}
    for g, _, _ in tools:
        counts[g] = counts.get(g, 0) + 1
    return counts


def test_pdf_is_optional_group_gated_on_sqlite_vec():
    """The pdf group is optional and gated on the sqlite_vec dep."""
    assert "pdf" in registry.SOURCES
    spec = registry.SOURCES["pdf"]
    assert spec.get("optional") is True
    assert spec["dep"] == "sqlite_vec"
    # pdf is NOT a core group (core_groups excludes optional groups).
    assert "pdf" not in registry.core_groups()


def test_pdf_skipped_when_dep_absent(monkeypatch):
    """When sqlite_vec is not importable, pdf is skipped_optional and the core
    six groups + >= 155 tools still hold."""
    real_can_import = registry._can_import

    def fake_can_import(modname: str) -> bool:
        if modname == "sqlite_vec":
            return False
        return real_can_import(modname)

    monkeypatch.setattr(registry, "_can_import", fake_can_import)
    registry.reset_cache()
    tools = registry.build()
    counts = _group_counts(tools)
    rep = registry.build_report()

    skipped_groups = {g for g, _ in rep["skipped_optional"]}
    assert "pdf" in skipped_groups, f"pdf should be skipped_optional; got {rep['skipped_optional']}"
    assert "pdf" not in counts, "pdf should not register any tools when its dep is absent"
    # No pdf entry in failed (absence is not a failure).
    assert not any(f[0] == "pdf" for f in rep["failed"]), "pdf absence must not be a failure"
    # Core invariants unchanged.
    assert CORE <= set(counts), f"missing core groups: {CORE - set(counts)}"
    assert len(tools) >= 155, f"expected >= 155 tools, got {len(tools)}"


def test_pdf_registered_when_dep_present():
    """When sqlite_vec is importable, pdf registers its 6 tools. Skipped
    automatically when the [pdf] extra is not installed in the running venv."""
    if not registry._can_import("sqlite_vec"):
        pytest.skip("sqlite_vec not installed ([pdf] extra absent)")
    registry.reset_cache()
    tools = registry.build()
    counts = _group_counts(tools)
    rep = registry.build_report()

    skipped_groups = {g for g, _ in rep["skipped_optional"]}
    assert "pdf" not in skipped_groups, "pdf should not be skipped when its dep is present"
    assert "pdf" in counts, "pdf should register tools when its dep is present"
    assert counts["pdf"] == 6, f"expected 6 pdf tools, got {counts['pdf']}"
    # Core invariants still hold alongside the optional group.
    assert CORE <= set(counts)
    assert len(tools) >= 155

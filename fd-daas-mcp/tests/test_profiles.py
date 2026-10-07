"""Deployment-profile gating (daas-deployment-profiles, ADR-0001).

Groups declaring ``profiles`` in SOURCES load only under those profiles
(dev loads everything); undeclared groups are universal; unknown profile
values fail fast; the selfcheck runs per profile with profile-skipped
groups excused from the tool-surface ghost check.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_FD_HOME = Path(__file__).resolve().parents[1]
if str(_FD_HOME) not in sys.path:
    sys.path.insert(0, str(_FD_HOME))

from daas.fd_daas_mcp import registry, selfcheck  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    registry.reset_cache()
    yield
    registry.reset_cache()


def _groups(tools: list) -> set[str]:
    return {g for g, _, _ in tools}


def test_unknown_profile_fails_fast():
    with pytest.raises(ValueError, match="未知部署画像"):
        registry.build(profile="cel")
    with pytest.raises(ValueError, match="合法值"):
        registry.validate_profile("wanxing")
    assert registry.validate_profile(None) is None
    assert registry.validate_profile("cell") == "cell"


def test_default_profile_is_universal_only():
    tools = registry.build()
    groups = _groups(tools)
    assert "customer_dataset" not in groups  # cell-gated group absent
    assert {"alerts", "cron", "composite", "daas", "dashboard", "gateway"} <= groups
    report = registry.build_report()
    assert "customer_dataset" in report["skipped_profile"]


def test_cell_profile_loads_customer_dataset():
    tools = registry.build(profile="cell")
    groups = _groups(tools)
    assert "customer_dataset" in groups
    names = {n for g, n, _ in tools if g == "customer_dataset"}
    assert "ingest_begin" in names
    # namespaced name has no daas_ prefix (the group IS the prefix)
    assert registry.namespaced("customer_dataset", "preview") == "customer_dataset_preview"


def test_local_profile_matches_default_universal_set():
    universal = _groups(registry.build())
    local = _groups(registry.build(profile="local"))
    assert local == universal  # no group is local-gated yet


def test_dev_profile_loads_everything():
    dev = _groups(registry.build(profile="dev"))
    assert "customer_dataset" in dev
    # dev == everything loadable: universal groups + all profile-gated groups
    assert dev == _groups(registry.build(profile="cell")) | _groups(registry.build())


def test_build_cache_is_per_profile():
    default_tools = registry.build()
    cell_tools = registry.build(profile="cell")
    assert len(cell_tools) > len(default_tools)  # cached separately, not clobbered
    assert len(registry.build()) == len(default_tools)


def test_selfcheck_per_profile():
    for profile in (None, "cell", "dev", "local"):
        result = selfcheck.run_invariants(profile=profile)
        assert result["ok"] is True, (
            f"selfcheck failed under profile={profile}: "
            + "; ".join(f"{c['name']}={c['detail']}" for c in result["checks"] if not c["ok"])
        )
    cell_result = selfcheck.run_invariants(profile="cell")
    # 11 business tools + provenance_wal_pending/ack (wire-provenance-ledger)
    assert cell_result["group_counts"].get("customer_dataset", 0) == 13
    default_result = selfcheck.run_invariants()
    assert "customer_dataset" not in default_result["group_counts"]


def test_core_groups_exclude_profile_gated():
    core = set(registry.core_groups())
    assert "customer_dataset" not in core
    # the six historical core groups remain core; research/workflow are
    # non-optional non-profile groups and thus core too
    assert {"alerts", "cron", "composite", "daas", "dashboard", "gateway"} <= core

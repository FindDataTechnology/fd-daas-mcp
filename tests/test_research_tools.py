"""research_tools tests: CRUD round-trip, create_missing, component validation,
generate_report (file + column + regenerate), delete cascade (owned pipeline +
cron rows removed; shared collections/rules preserved), refresh (sibling-tool
orchestration via a mocked _sibling_tools), add/remove_component.

Imports research_tools directly (it uses research_database.get_database() +
shared models, no eviction dance needed) - mirrors test_rule_tools.py.
research-mcp/ is added to sys.path for the imports, then removed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_RESEARCH_MCP = Path(__file__).resolve().parents[1] / "research-mcp"
sys.path.insert(0, str(_RESEARCH_MCP))
from research_database import get_database  # noqa: E402
from models import (  # noqa: E402
    Dashboard,
    EntityCollection,
    EntityCollectionItem,
    IndicatorCollection,
    IndicatorCollectionItem,
    IndicatorRule,
    Observation,
    PipelineCollection,
    PipelineCollectionItem,
    Research,
    Schedule,
)
import research_tools  # noqa: E402
sys.path.remove(str(_RESEARCH_MCP))


# ── fixtures ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_researches():
    sess = get_database().get_session()
    sess.query(Research).delete()
    sess.commit()
    sess.close()
    yield
    sess = get_database().get_session()
    sess.query(Research).delete()
    sess.commit()
    sess.close()


@pytest.fixture
def research_dir(tmp_path, monkeypatch):
    d = tmp_path / "researches"
    monkeypatch.setenv("RESEARCH_DIR", str(d))
    return d


def _sess():
    return get_database().get_session()


def _seed_entity_collection(name="ec1", codes=("600519", "000001")) -> str:
    sess = _sess()
    sess.query(EntityCollectionItem).delete()
    sess.query(EntityCollection).filter_by(name=name).delete()
    ec = EntityCollection(name=name)
    sess.add(ec)
    sess.flush()
    for i, code in enumerate(codes):
        sess.add(EntityCollectionItem(collection_id=ec.id, entity_type="stock", code=code, sort_order=i))
    sess.commit()
    sess.close()
    return name


def _seed_indicator_collection(name="ic1", rule_name="rule_a", indicator_name="sma5") -> str:
    sess = _sess()
    sess.query(IndicatorCollectionItem).delete()
    sess.query(IndicatorCollection).filter_by(name=name).delete()
    sess.query(IndicatorRule).filter_by(name=rule_name).delete()
    rule = IndicatorRule(
        name=rule_name,
        datasource="akshare",
        function_name="stock_zh_a_hist",
        source_table="scraw_test_daily",
        date_column="date",
        value_column="close",
        op="sma",
        params_json={"window": 5},
        indicator_name=indicator_name,
    )
    sess.add(rule)
    sess.flush()
    ic = IndicatorCollection(name=name)
    sess.add(ic)
    sess.flush()
    sess.add(IndicatorCollectionItem(collection_id=ic.id, indicator_id=rule.id, sort_order=0))
    sess.commit()
    sess.close()
    return name


def _seed_pipeline_collection(name="pc1", item_name="itm1", task_name="pipeline_pc1_itm1") -> str:
    sess = _sess()
    sess.query(PipelineCollectionItem).delete()
    sess.query(PipelineCollection).filter_by(name=name).delete()
    sess.query(Schedule).filter_by(task_name=task_name).delete()
    pc = PipelineCollection(name=name)
    sess.add(pc)
    sess.flush()
    sess.add(
        PipelineCollectionItem(
            collection_id=pc.id,
            name=item_name,
            source_mcp="akshare",
            tool="call_akshare_function",
            storage_table="scraw_test_daily",
            cron_expr="0 5 * * *",
            enabled=True,
            task_name=task_name,
        )
    )
    sess.add(Schedule(name=task_name, cron_expr="0 5 * * *", task_name=task_name))
    sess.commit()
    sess.close()
    return name


def _seed_dashboard(slug="dash1") -> str:
    sess = _sess()
    sess.query(Dashboard).filter_by(slug=slug).delete()
    sess.add(Dashboard(slug=slug, name="Dash One", intro="an intro", file_path="x", file_url="file:///x"))
    sess.commit()
    sess.close()
    return slug


# ── create ─────────────────────────────────────────────────────────


def test_create_minimal_and_uniqueness():
    r = research_tools.create(name="r1", description="d", status="active")
    assert r["name"] == "r1" and r["status"] == "active"
    assert r["component_refs"] == {}
    dup = research_tools.create(name="r1")
    assert "error" in dup and "already exists" in dup["error"]


def test_create_parses_component_refs():
    r = research_tools.create(name="r2", component_refs='{"rules": ["ra", "rb"]}')
    assert r["component_refs"] == {"rules": ["ra", "rb"]}


def test_create_missing_creates_empty_collections():
    ec = _seed_entity_collection("ec_existing")
    r = research_tools.create(
        name="r3",
        entity_collection_name=ec,  # exists -> attach
        indicator_collection_name="ic_new",  # missing -> create
        pipeline_collection_name="pc_new",  # missing -> create
        create_missing=True,
    )
    assert r["entity_collection_name"] == ec
    assert r["indicator_collection_name"] == "ic_new"
    assert r["pipeline_collection_name"] == "pc_new"
    sess = _sess()
    assert sess.query(IndicatorCollection).filter_by(name="ic_new").first() is not None
    assert sess.query(PipelineCollection).filter_by(name="pc_new").first() is not None
    sess.close()


def test_create_missing_false_errors_on_absent_collection():
    err = research_tools.create(name="r4", entity_collection_name="nope", create_missing=False)
    assert "error" in err and "not found" in err["error"]


def test_create_validates_dashboard_exists():
    err = research_tools.create(name="r5", dashboard_slug="missing-slug")
    assert "error" in err and "dashboard" in err["error"]


# ── get / list ─────────────────────────────────────────────────────


def test_get_resolves_counts():
    ec = _seed_entity_collection("ec1", codes=("600519", "000001"))
    ic = _seed_indicator_collection("ic1", rule_name="rl1", indicator_name="sma5")
    research_tools.create(name="r6", entity_collection_name=ec, indicator_collection_name=ic)
    g = research_tools.get(name="r6")
    assert g["entity_count"] == 2
    assert g["indicator_count"] == 1
    assert g["dangling"] == []


def test_get_reports_dangling_after_component_deleted():
    ec = _seed_entity_collection("ec_gone", codes=("600519",))
    research_tools.create(name="r6b", entity_collection_name=ec)
    # delete the entity collection independently -> research reference dangles
    sess = _sess()
    sess.query(EntityCollection).filter_by(name=ec).delete()
    sess.commit()
    sess.close()
    g = research_tools.get(name="r6b")
    assert g["entity_count"] is None
    assert "entity_collection:ec_gone" in g["dangling"]


def test_list_filters_by_status():
    research_tools.create(name="a", status="draft")
    research_tools.create(name="b", status="active")
    active = research_tools.list(status="active")
    assert [x["name"] for x in active["researches"]] == ["b"]
    allr = research_tools.list()
    assert {x["name"] for x in allr["researches"]} == {"a", "b"}


# ── update ─────────────────────────────────────────────────────────


def test_update_patches_and_validates():
    r = research_tools.create(name="r7", status="draft")
    u = research_tools.update(name="r7", status="archived", description="new")
    assert u["status"] == "archived" and u["description"] == "new"
    err = research_tools.update(name="r7", dashboard_slug="nope")
    assert "error" in err
    ok = research_tools.update(name="r7", dashboard_slug=_seed_dashboard("d7"))
    assert ok["dashboard_slug"] == "d7"


def test_update_reassign_entity_collection():
    ec = _seed_entity_collection("ec_x")
    research_tools.create(name="r8")
    u = research_tools.update(name="r8", entity_collection_name=ec)
    assert u["entity_collection_name"] == "ec_x"


# ── add / remove component ─────────────────────────────────────────


def test_add_remove_component_roundtrip():
    research_tools.create(name="r9", component_refs='{"rules": ["r1"]}')
    added = research_tools.add_component(name="r9", component_type="rule", component_name="r2")
    assert added["component_refs"]["rules"] == ["r1", "r2"]
    # named kind: dashboard
    slug = _seed_dashboard("d9")
    added2 = research_tools.add_component(name="r9", component_type="dashboard", component_name=slug)
    assert added2["dashboard_slug"] == "d9"
    # remove named kind leaves underlying dashboard intact
    removed = research_tools.remove_component(name="r9", component_type="dashboard", component_name=slug)
    assert removed["dashboard_slug"] is None
    sess = _sess()
    assert sess.query(Dashboard).filter_by(slug=slug).first() is not None
    sess.close()
    # remove auxiliary ref
    removed2 = research_tools.remove_component(name="r9", component_type="rule", component_name="r1")
    assert removed2["component_refs"]["rules"] == ["r2"]


def test_add_component_unknown_type_errors():
    research_tools.create(name="r10")
    err = research_tools.add_component(name="r10", component_type="bogus", component_name="x")
    assert "error" in err


# ── generate_report ────────────────────────────────────────────────


def test_generate_report_writes_file_and_column(research_dir):
    ec = _seed_entity_collection("ec_r", codes=("600519",))
    ic = _seed_indicator_collection("ic_r", rule_name="rl_r", indicator_name="sma5")
    sess = _sess()
    # seed an observation for the indicator
    rule = sess.query(IndicatorRule).filter_by(name="rl_r").first()
    sess.add(
        Observation(
            source=rule.datasource,
            function_name=rule.function_name,
            indicator=rule.indicator_name,
            date="2024-01-02",
            value="123.45",
        )
    )
    sess.commit()
    sess.close()
    research_tools.create(name="rep1", entity_collection_name=ec, indicator_collection_name=ic)
    g = research_tools.generate_report(name="rep1")
    fpath = Path(g["report_path"])
    assert fpath.exists()
    assert g["char_count"] > 0
    assert "贵州" not in g["report_path"]  # sanity
    # column matches file
    sess = _sess()
    row = sess.query(Research).filter_by(name="rep1").first()
    assert row.report_md == fpath.read_text(encoding="utf-8")
    assert "123.45" in row.report_md
    sess.close()
    # regenerate overwrites (file + column stay consistent)
    g2 = research_tools.generate_report(name="rep1")
    assert g2["report_path"] == str(fpath)
    sess = _sess()
    row2 = sess.query(Research).filter_by(name="rep1").first()
    assert fpath.read_text(encoding="utf-8") == row2.report_md
    sess.close()


def test_generate_report_research_not_found(research_dir):
    err = research_tools.generate_report(name="nope")
    assert "error" in err


# ── delete cascade ─────────────────────────────────────────────────


def test_delete_removes_row_and_report_file(research_dir):
    research_tools.create(name="del1")
    research_tools.generate_report(name="del1")
    sess = _sess()
    fpath = sess.query(Research).filter_by(name="del1").first().report_path
    sess.close()
    assert Path(fpath).exists()
    res = research_tools.delete(name="del1")
    assert res["deleted"] == "del1" and res["report_file_removed"] is True
    assert not Path(fpath).exists()
    sess = _sess()
    assert sess.query(Research).filter_by(name="del1").first() is None
    sess.close()


def test_delete_unwires_owned_pipeline():
    pc = _seed_pipeline_collection("pc_del", item_name="itm", task_name="pipeline_pc_del_itm")
    research_tools.create(name="del2", pipeline_collection_name=pc)
    res = research_tools.delete(name="del2", remove_pipeline=True)
    assert res["pipeline_removed"] is True
    sess = _sess()
    assert sess.query(PipelineCollection).filter_by(name="pc_del").first() is None
    assert sess.query(PipelineCollectionItem).filter_by(name="itm").first() is None
    assert sess.query(Schedule).filter_by(task_name="pipeline_pc_del_itm").first() is None
    sess.close()


def test_delete_preserves_shared_collections_and_rules():
    ec = _seed_entity_collection("ec_shared", codes=("600519",))
    _seed_indicator_collection("ic_shared", rule_name="rule_shared", indicator_name="ind_shared")
    research_tools.create(
        name="del3",
        entity_collection_name=ec,
        indicator_collection_name="ic_shared",
        component_refs='{"rules": ["rule_shared"]}',
    )
    research_tools.delete(name="del3")
    sess = _sess()
    assert sess.query(EntityCollection).filter_by(name="ec_shared").first() is not None
    assert sess.query(EntityCollectionItem).count() == 1
    assert sess.query(IndicatorCollection).filter_by(name="ic_shared").first() is not None
    assert sess.query(IndicatorRule).filter_by(name="rule_shared").first() is not None
    sess.close()


def test_delete_keep_pipeline_when_flag_false():
    pc = _seed_pipeline_collection("pc_keep", item_name="itm2", task_name="pipeline_pc_keep_itm2")
    research_tools.create(name="del4", pipeline_collection_name=pc)
    res = research_tools.delete(name="del4", remove_pipeline=False)
    assert res["pipeline_removed"] is False
    sess = _sess()
    assert sess.query(PipelineCollection).filter_by(name="pc_keep").first() is not None
    sess.close()


# ── refresh ────────────────────────────────────────────────────────


def test_refresh_uses_sibling_tools(monkeypatch):
    ic = _seed_indicator_collection("ic_rf", rule_name="rl_rf", indicator_name="sma5")
    pc = _seed_pipeline_collection("pc_rf", item_name="itm_rf", task_name="pipeline_pc_rf_itm_rf")
    research_tools.create(name="rf1", indicator_collection_name=ic, pipeline_collection_name=pc)

    calls = {"run_indicator": [], "sync_entity": [], "sync_indicator": []}

    def fake_run_indicator(name):
        calls["run_indicator"].append(name)
        return {"indicator": name, "count": 7}

    def fake_sync_entity(name):
        calls["sync_entity"].append(name)
        return {"added": 0, "removed": 0, "unchanged": 0}

    def fake_sync_indicator(name):
        calls["sync_indicator"].append(name)
        return {"added": 0, "removed": 0, "unchanged": 0}

    monkeypatch.setattr(
        research_tools,
        "_sibling_tools",
        lambda: {
            "daas_run_indicator": fake_run_indicator,
            "daas_sync_entity_collection": fake_sync_entity,
            "daas_sync_indicator_collection": fake_sync_indicator,
        },
    )

    res = research_tools.refresh(name="rf1")
    assert res["name"] == "rf1"
    assert res["indicators"][0]["name"] == "rl_rf"
    assert res["indicators"][0]["status"] == "ok"
    assert res["indicators"][0]["result"]["count"] == 7
    assert calls["run_indicator"] == ["rl_rf"]
    # manual collections (no rule) -> not synced
    assert calls["sync_entity"] == []
    assert calls["sync_indicator"] == []
    # pipeline status reported
    assert res["pipeline"][0]["name"] == "itm_rf"
    assert res["pipeline"][0]["enabled"] is True
    assert "refreshed_at" in res


def test_refresh_reports_unavailable_when_sibling_missing(monkeypatch):
    ic = _seed_indicator_collection("ic_rf2", rule_name="rl_rf2", indicator_name="sma5")
    research_tools.create(name="rf2", indicator_collection_name=ic)
    monkeypatch.setattr(research_tools, "_sibling_tools", lambda: {})
    res = research_tools.refresh(name="rf2")
    assert res["indicators"][0]["status"] == "unavailable"


def test_refresh_research_not_found():
    err = research_tools.refresh(name="nope")
    assert "error" in err

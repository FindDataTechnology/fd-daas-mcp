"""Indicator scores + indicator collections self-check for daas-mcp.

Verifies the score concept + collection concept end-to-end against a TEMP DB
(no touch to mcp/daas.db, no network, no LLM). Exercises:

Indicator scores (capability `indicator-scores`):
  1. `indicator_rules.score` column exists (migration / create_all).
  2. `create_indicator(score=…)` sets the default; omitted → NULL.
  3. `effective_default_score` inherits the datasource's `sources.score` when
     the indicator's score is NULL.
  4. `set-indicator-score` writer subcommand sets / clears (null → inherit).
  5. `update_indicator(clear_score=True)` clears.
  6. `set_indicator_score` error paths: unknown indicator, non-numeric score.

Indicator collections (capability `indicator-collections`):
  7. fresh-DB table creation (Base.metadata.create_all).
  8. CRUD: create / list / get / update / delete collection.
  9. membership: add / remove / re-add no-op (already_member) / remove non-member (not_member).
 10. reorder: full list rewrites sort_order; unknown id + wrong count rejected.
 11. per-item score override set / clear + the 4 resolution scenarios
     (item → indicator → datasource → NULL).
 12. audit log: add_in / remove_out recorded; survives indicator-rule deletion.
 13. cascade: delete collection → items + changes gone; delete indicator rule →
     its membership rows gone (real FK).

Run:
  uv run --directory mcp/daas-mcp python selfcheck_indicator_scores.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from sqlalchemy import text

# ── point at a TEMP db BEFORE importing anything that touches daas.db ──
_TMP_DB = tempfile.mktemp(suffix="_ind_scores_selfcheck.db")
os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

_HERE = Path(__file__).resolve().parent
_MODELS = _HERE.parent / "models"
for _p in (str(_MODELS), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import collection_writer as W  # noqa: E402
from daas_database import get_database  # noqa: E402
from process_database import get_db as get_process_db  # noqa: E402
import process_api  # noqa: E402
import indicator_collection_tools as ICT  # noqa: E402
from registry_service import IndicatorCollectionService, RegistryService  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def _run_writer(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = W.main(argv)
        except SystemExit as e:
            rc = e.code if e.code is not None else 1
    return (rc, out.getvalue(), err.getvalue())


def _last_json(out: str) -> dict | None:
    last = [ln for ln in out.splitlines() if ln.strip()][-1:] if out.strip() else []
    if not last:
        return None
    try:
        return json.loads(last[0])
    except json.JSONDecodeError:
        return None


def _ic_svc() -> IndicatorCollectionService:
    return IndicatorCollectionService(get_database().get_session())


def _reg_svc() -> RegistryService:
    return RegistryService(get_database().get_session())


def _seed() -> None:
    """One datasource (with default score 0.6) + a scraw_prices table for
    indicator rules + two indicator rules (one scored, one not)."""
    reg = _reg_svc()
    reg.create_datasource(name="aksh", label="AKShare", score=0.6)
    # Create a source-data table the indicator rule can bind to.
    eng = get_database().engine
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE scraw_prices (date TEXT, close REAL)"))
        conn.execute(text("INSERT INTO scraw_prices VALUES ('2024-01-01', 10.0)"))
        conn.execute(text("INSERT INTO scraw_prices VALUES ('2024-01-02', 11.0)"))
        conn.execute(text("INSERT INTO scraw_prices VALUES ('2024-01-03', 12.0)"))
    pdb = get_process_db()
    pdb.create_indicator(
        name="rsi_5",
        datasource="aksh",
        source_table="scraw_prices",
        date_column="date",
        value_column="close",
        op="sma",
        params={"window": 2},
    )  # no score → inherits datasource 0.6
    pdb.create_indicator(
        name="sma_20",
        datasource="aksh",
        source_table="scraw_prices",
        date_column="date",
        value_column="close",
        op="sma",
        params={"window": 2},
        score=0.4,
    )


# ── indicator scores ──────────────────────────────────────────


def test_score_column_exists() -> None:
    print("[1] indicator_rules.score column exists")
    eng = get_database().engine
    with eng.connect() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(indicator_rules)")).fetchall()]
    check("score column on indicator_rules", "score" in cols, f"cols={cols!r}")


def test_create_indicator_score() -> None:
    print("[2] create_indicator(score=…) sets the default; omitted → NULL")
    rsi = get_process_db().get_indicator("rsi_5")
    check("rsi_5 score is NULL", rsi and rsi["score"] is None, f"rsi={rsi!r}")
    check("rsi_5 effective_default_score=0.6 (inherits datasource)", rsi and rsi["effective_default_score"] == 0.6, f"rsi={rsi!r}")
    check("rsi_5 datasource_default_score=0.6", rsi and rsi["datasource_default_score"] == 0.6, f"rsi={rsi!r}")
    sma = get_process_db().get_indicator("sma_20")
    check("sma_20 score=0.4", sma and sma["score"] == 0.4, f"sma={sma!r}")
    check("sma_20 effective_default_score=0.4 (own score wins)", sma and sma["effective_default_score"] == 0.4, f"sma={sma!r}")


def test_set_indicator_score_writer() -> None:
    print("[3] set-indicator-score sets / clears (null → inherit)")
    rc, out, _ = _run_writer(
        ["set-indicator-score", "--json", json.dumps({"name": "rsi_5", "score": 0.8})]
    )
    check("set exits 0", rc == 0, f"rc={rc} out={out!r}")
    payload = _last_json(out)
    check("set returns score=0.8", payload and payload.get("score") == 0.8, f"payload={payload!r}")
    check("set effective_default_score=0.8", payload and payload.get("effective_default_score") == 0.8, f"payload={payload!r}")
    # Clear
    rc, out, _ = _run_writer(
        ["set-indicator-score", "--json", json.dumps({"name": "rsi_5", "score": None})]
    )
    check("clear exits 0", rc == 0, f"rc={rc}")
    payload = _last_json(out)
    check("clear returns score=null", payload and payload.get("score") is None, f"payload={payload!r}")
    check("clear effective_default_score=0.6 (inherits again)", payload and payload.get("effective_default_score") == 0.6, f"payload={payload!r}")


def test_update_indicator_clear_score() -> None:
    print("[4] update_indicator clears (DB score=None + wrapper clear_score)")
    pdb = get_process_db()
    pdb.set_indicator_score("sma_20", 0.9)
    # DB-layer clearing: score=None clears (the underlying mechanism).
    out = pdb.update_indicator("sma_20", score=None)
    check("DB score=None → score NULL", out.get("score") is None, f"out={out!r}")
    check("effective_default_score=0.6 (inherits)", out.get("effective_default_score") == 0.6, f"out={out!r}")
    # Wrapper clear_score=True flag translates to the same clear.
    pdb.set_indicator_score("sma_20", 0.9)
    out2 = process_api.update_indicator("sma_20", clear_score=True)
    check("wrapper clear_score=True → score NULL", out2.get("score") is None, f"out2={out2!r}")
    check("wrapper effective_default_score=0.6", out2.get("effective_default_score") == 0.6, f"out2={out2!r}")
    # Restore for later tests
    pdb.set_indicator_score("sma_20", 0.4)


def test_score_error_paths() -> None:
    print("[5] set_indicator_score error paths")
    rc, out, _ = _run_writer(
        ["set-indicator-score", "--json", json.dumps({"name": "nope", "score": 0.5})]
    )
    check("unknown indicator exits non-zero", rc != 0, f"rc={rc}")
    check("error mentions indicator not found", "not found" in (out + "").lower() or "indicator" in (out + "").lower(), f"out={out!r}")
    # Non-numeric score via the API wrapper (returns {"error": ...}).
    res = process_api.set_indicator_score("rsi_5", "not-a-number")  # type: ignore[arg-type]
    check("non-numeric score → error dict", isinstance(res, dict) and "error" in res, f"res={res!r}")


# ── indicator collections ─────────────────────────────────────


def test_collection_crud() -> None:
    print("[6] collection CRUD: create / list / get / update / delete")
    svc = _ic_svc()
    c = svc.create_indicator_collection(name="momentum", description="RSI bundle")
    check("created with item_count 0", c.get("item_count") == 0, f"c={c!r}")
    listing = svc.list_indicator_collections()
    check("list includes momentum", any(x["name"] == "momentum" for x in listing), f"listing={listing!r}")
    # Duplicate name → the tool wrapper returns {"success": False, "error": ...}
    dup = ICT.create_indicator_collection(name="momentum")
    check("duplicate name → error", isinstance(dup, dict) and dup.get("success") is False, f"dup={dup!r}")
    upd = svc.update_indicator_collection(name="momentum", description="updated")
    check("update description", upd.get("description") == "updated", f"upd={upd!r}")
    got = svc.get_indicator_collection("momentum")
    check("get returns items list", "items" in got and got.get("item_count") == 0, f"got={got!r}")
    # unknown collection → tool wrapper error
    unknown = ICT.get_indicator_collection("nope")
    check("unknown collection → error", isinstance(unknown, dict) and unknown.get("success") is False, f"unknown={unknown!r}")


def test_membership() -> None:
    print("[7] membership: add / already_member / remove / not_member")
    svc = _ic_svc()
    r = svc.add_indicator_to_collection("momentum", "rsi_5")
    check("add rsi_5 → added", r.get("action") == "added", f"r={r!r}")
    r2 = svc.add_indicator_to_collection("momentum", "rsi_5")
    check("re-add → already_member (no-op)", r2.get("action") == "already_member", f"r2={r2!r}")
    r3 = svc.add_indicator_to_collection("momentum", "sma_20", score=0.9)
    check("add sma_20 with score=0.9", r3.get("action") == "added", f"r3={r3!r}")
    r4 = svc.remove_indicator_from_collection("momentum", "rsi_5")
    check("remove rsi_5 → removed", r4.get("action") == "removed", f"r4={r4!r}")
    r5 = svc.remove_indicator_from_collection("momentum", "rsi_5")
    check("re-remove → not_member (no-op)", r5.get("action") == "not_member", f"r5={r5!r}")
    # re-add for later tests
    svc.add_indicator_to_collection("momentum", "rsi_5")


def test_reorder() -> None:
    print("[8] reorder: full list rewrites; unknown id + wrong count rejected")
    svc = _ic_svc()
    items = svc.list_indicator_collection_items("momentum")["items"]
    ids = [it["id"] for it in items]
    check("two items present", len(ids) == 2, f"items={items!r}")
    # reverse the order
    svc.reorder_indicator_collection_items("momentum", list(reversed(ids)))
    after = [it["id"] for it in svc.list_indicator_collection_items("momentum")["items"]]
    check("reorder applied", after == list(reversed(ids)), f"after={after!r}")
    # unknown id
    try:
        svc.reorder_indicator_collection_items("momentum", [999999])
        check("unknown id rejected", False, "no error raised")
    except Exception:
        check("unknown id rejected", True)
    # wrong count
    try:
        svc.reorder_indicator_collection_items("momentum", [ids[0]])
        check("wrong count rejected", False, "no error raised")
    except Exception:
        check("wrong count rejected", True)
    # restore original order
    svc.reorder_indicator_collection_items("momentum", ids)


def test_3level_resolution() -> None:
    print("[9] 3-level effective score resolution (item → indicator → datasource)")
    svc = _ic_svc()
    pdb = get_process_db()
    # State: rsi_5 score=NULL (datasource=0.6), sma_20 score=0.4.
    # sma_20 item has override 0.9 (from test 7). rsi_5 item has no override.
    items = {it["indicator_name"]: it for it in svc.list_indicator_collection_items("momentum")["items"]}
    rsi = items.get("rsi_5")
    sma = items.get("sma_20")
    # Scenario A: item NULL, indicator NULL → datasource 0.6
    check("rsi item_score null", rsi and rsi["item_score"] is None, f"rsi={rsi!r}")
    check("rsi indicator_default_score null", rsi and rsi["indicator_default_score"] is None, f"rsi={rsi!r}")
    check("rsi source_default_score 0.6", rsi and rsi["source_default_score"] == 0.6, f"rsi={rsi!r}")
    check("rsi resolved score 0.6 (datasource)", rsi and rsi["score"] == 0.6, f"rsi={rsi!r}")
    # Scenario B: item override 0.9 wins over indicator 0.4 + datasource 0.6
    check("sma item_score 0.9", sma and sma["item_score"] == 0.9, f"sma={sma!r}")
    check("sma indicator_default_score 0.4", sma and sma["indicator_default_score"] == 0.4, f"sma={sma!r}")
    check("sma source_default_score 0.6", sma and sma["source_default_score"] == 0.6, f"sma={sma!r}")
    check("sma resolved score 0.9 (item override)", sma and sma["score"] == 0.9, f"sma={sma!r}")
    # Scenario C: clear sma item override → falls back to indicator 0.4
    cleared = svc.set_indicator_collection_item_score("momentum", "sma_20", None)
    check("sma cleared item_score null", cleared.get("item_score") is None, f"cleared={cleared!r}")
    check("sma resolved score 0.4 (indicator default)", cleared.get("score") == 0.4, f"cleared={cleared!r}")
    # Scenario D: clear sma indicator score too → falls back to datasource 0.6
    pdb.set_indicator_score("sma_20", None)
    sma2 = {it["indicator_name"]: it for it in svc.list_indicator_collection_items("momentum")["items"]}["sma_20"]
    check("sma all-null resolved score 0.6 (datasource)", sma2.get("score") == 0.6, f"sma2={sma2!r}")
    # restore sma_20 score
    pdb.set_indicator_score("sma_20", 0.4)


def test_audit_log_and_writer_score() -> None:
    print("[10] audit log add_in/remove_out + set-indicator-collection-item-score writer")
    svc = _ic_svc()
    # set-indicator-collection-item-score writer → set override on rsi_5
    rc, out, _ = _run_writer(
        [
            "set-indicator-collection-item-score",
            "--json",
            json.dumps({"collection_name": "momentum", "indicator_name": "rsi_5", "score": 0.7}),
        ]
    )
    check("writer set item score exits 0", rc == 0, f"rc={rc} out={out!r}")
    payload = _last_json(out)
    check("writer item_score=0.7", payload and payload.get("item_score") == 0.7, f"payload={payload!r}")
    check("writer resolved score=0.7", payload and payload.get("score") == 0.7, f"payload={payload!r}")
    # clear via writer
    rc, out, _ = _run_writer(
        [
            "set-indicator-collection-item-score",
            "--json",
            json.dumps({"collection_name": "momentum", "indicator_name": "rsi_5", "score": None}),
        ]
    )
    check("writer clear exits 0", rc == 0, f"rc={rc}")
    payload = _last_json(out)
    check("writer cleared item_score null", payload and payload.get("item_score") is None, f"payload={payload!r}")
    # audit log
    chg = svc.list_indicator_collection_changes(collection_name="momentum")
    actions = [c["action"] for c in chg["changes"]]
    check("audit log has add_in", "add_in" in actions, f"actions={actions!r}")
    check("audit log has remove_out", "remove_out" in actions, f"actions={actions!r}")
    # each change enriched with collection_name + indicator_name
    sample = chg["changes"][0]
    check("audit row has collection_name", sample.get("collection_name") == "momentum", f"sample={sample!r}")
    check("audit row has indicator_name", bool(sample.get("indicator_name")), f"sample={sample!r}")


def test_audit_survives_indicator_deletion() -> None:
    print("[11] audit log survives indicator-rule deletion (denormalized name)")
    svc = _ic_svc()
    pdb = get_process_db()
    # add a fresh throwaway indicator, add to collection, delete the indicator
    pdb.create_indicator(
        name="throwaway",
        datasource="aksh",
        source_table="scraw_prices",
        date_column="date",
        value_column="close",
        op="sma",
        params={"window": 2},
    )
    svc.add_indicator_to_collection("momentum", "throwaway")
    before = svc.list_indicator_collection_changes(collection_name="momentum", indicator_name="throwaway")
    n_before = before["total"]
    pdb.delete_indicator("throwaway")
    after = svc.list_indicator_collection_changes(collection_name="momentum", indicator_name="throwaway")
    check("audit rows for throwaway survive deletion", after["total"] == n_before, f"before={n_before} after={after['total']}")
    # membership row should be gone (cascade)
    items = {it["indicator_name"]: it for it in svc.list_indicator_collection_items("momentum")["items"]}
    check("throwaway membership row cascaded away", "throwaway" not in items, f"items={list(items)!r}")


def test_cascade_on_collection_delete() -> None:
    print("[12] delete collection cascades to items + changes")
    svc = _ic_svc()
    svc.create_indicator_collection(name="tmp_coll")
    svc.add_indicator_to_collection("tmp_coll", "rsi_5")
    eng = get_database().engine
    with eng.connect() as conn:
        cid = conn.execute(text("SELECT id FROM indicator_collections WHERE name='tmp_coll'")).scalar()
    svc.delete_indicator_collection("tmp_coll")
    with eng.connect() as conn:
        n_items = conn.execute(text("SELECT count(*) FROM indicator_collection_items WHERE collection_id=:c"), {"c": cid}).scalar()
        n_chg = conn.execute(text("SELECT count(*) FROM indicator_collection_changes WHERE collection_id=:c"), {"c": cid}).scalar()
    check("collection delete cascades to items + changes", n_items == 0 and n_chg == 0, f"items={n_items} chg={n_chg}")


def main() -> int:
    print("=== daas-mcp indicator-scores + indicator-collections selfcheck ===")
    print(f"(temp db: {_TMP_DB})")
    _seed()
    test_score_column_exists()
    test_create_indicator_score()
    test_set_indicator_score_writer()
    test_update_indicator_clear_score()
    test_score_error_paths()
    test_collection_crud()
    test_membership()
    test_reorder()
    test_3level_resolution()
    test_audit_log_and_writer_score()
    test_audit_survives_indicator_deletion()
    test_cascade_on_collection_delete()
    print("===")
    print(f"PASS={PASS} FAIL={FAIL}")
    try:
        os.unlink(_TMP_DB)
    except OSError:
        pass
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

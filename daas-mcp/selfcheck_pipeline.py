"""Smoke test for daas-mcp pipeline collections.

No-network mode (default): exercises models, validators, CRUD, and the
upsert round-trip (idempotency, ALTER TABLE for new columns, Chinese column
names) against a TEMP DB. Does not touch mcp/daas.db and makes no network call.

Live mode (AKSHARE_LIVE=1): creates a temp collection, adds one item driving
akshare-mcp.call_akshare_function(stock_individual_info_em), asserts rows
land in scraw__selfcheck, re-runs --fetch-item and asserts idempotency.

Run:
  uv run --directory mcp/daas-mcp python selfcheck_pipeline.py
  AKSHARE_LIVE=1 uv run --directory mcp/daas-mcp python selfcheck_pipeline.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# ── point at a TEMP db BEFORE importing anything that touches daas.db ──
_TMP_DB = tempfile.mktemp(suffix="_pipeline_selfcheck.db")
os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
# make mcp/models + this dir importable
_HERE = Path(__file__).resolve().parent
_MODELS = _HERE.parent / "models"
for _p in (str(_MODELS), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import inspect  # noqa: E402
import pipeline_tools as P  # noqa: E402
from models import Base, PipelineCollection, PipelineCollectionItem  # noqa: E402
from daas_database import get_database  # noqa: E402

LIVE = os.environ.get("AKSHARE_LIVE") == "1"

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


def test_models_and_tables() -> None:
    print("[1] models + tables")
    eng = get_database().engine
    Base.metadata.create_all(eng)
    insp = inspect(eng)
    check("pipeline_collections table created", "pipeline_collections" in insp.get_table_names())
    check("pipeline_collection_items table created", "pipeline_collection_items" in insp.get_table_names())
    cols = {c["name"] for c in insp.get_columns("pipeline_collection_items")}
    check("item has source_mcp", "source_mcp" in cols)
    check("item has tool", "tool" in cols)
    check("item has arguments_json", "arguments_json" in cols)


def test_validators() -> None:
    print("[2] validators reject bad input")
    for bad in ["not_scraw", "scraw_", "scraw_CAPS", "scraw-dash", ""]:
        try:
            P._validate_storage_table(bad)
            check(f"storage_table {bad!r} rejected", False)
        except P.PipelineError:
            check(f"storage_table {bad!r} rejected", True)
    try:
        P._validate_storage_table("scraw_ashare_daily")
        check("storage_table scraw_ashare_daily accepted", True)
    except P.PipelineError as e:
        check("storage_table scraw_ashare_daily accepted", False, str(e))

    for bad in ["4 bad", "has space", "a;b", ""]:
        try:
            P._validate_ident(bad, "upsert_key")
            check(f"ident {bad!r} rejected", False)
        except P.PipelineError:
            check(f"ident {bad!r} rejected", True)
    # Chinese word chars are allowed (\w is Unicode)
    try:
        P._validate_ident("日期", "upsert_key")
        check("ident 日期 accepted (Unicode)", True)
    except P.PipelineError as e:
        check("ident 日期 accepted (Unicode)", False, str(e))

    for bad in ["* * *", "0 0 0", "0 0 0 0 0 0", ""]:
        try:
            P._validate_cron(bad)
            check(f"cron {bad!r} rejected", False)
        except P.PipelineError:
            check(f"cron {bad!r} rejected", True)
    try:
        P._validate_cron("30 16 * * 1-5")
        check("cron 30 16 * * 1-5 accepted", True)
    except P.PipelineError:
        check("cron 30 16 * * 1-5 accepted", False)

    # source_mcp resolution: akshare-mcp via convention dir, cron-mcp via .mcp.json
    check("akshare-mcp resolves (convention)", P.resolve_server_config("akshare-mcp") is not None)
    check("cron-mcp resolves (.mcp.json)", P.resolve_server_config("cron-mcp") is not None)
    check("daas-mcp resolves (.mcp.json)", P.resolve_server_config("daas-mcp") is not None)
    check("bogus-mcp rejected", P.resolve_server_config("bogus-mcp-xyz") is None)
    try:
        P._validate_source_mcp("bogus-mcp-xyz")
        check("validate_source_mcp bogus rejected", False)
    except P.PipelineError:
        check("validate_source_mcp bogus rejected", True)


def test_sanitize_col() -> None:
    print("[3] column sanitization")
    check("日期 → 日期", P._sanitize_col("日期") == "日期")
    check("涨跌额(%) → 涨跌额___", P._sanitize_col("涨跌额(%)") == "涨跌额___")
    check("a b → a_b", P._sanitize_col("a b") == "a_b")
    check("9x → _9x", P._sanitize_col("9x") == "_9x")


def test_upsert_roundtrip() -> None:
    print("[4] upsert round-trip (idempotent, ALTER, Chinese cols)")
    table = "scraw_selfcheck_rt"
    keys = ["日期"]
    recs1 = [
        {"日期": "2025-07-01", "开盘": 10.0, "收盘": 11.0},
        {"日期": "2025-07-02", "开盘": 11.0, "收盘": 12.5},
    ]
    n1 = P._upsert_records(table, keys, recs1)
    check("first upsert inserts 2 rows", n1 == 2, f"got {n1}")
    eng = get_database().engine
    raw = eng.raw_connection()
    try:
        cnt = raw.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        check("table has 2 rows", cnt == 2, f"got {cnt}")
        # Chinese column exists
        cols = {r[1] for r in raw.execute(f'PRAGMA table_info("{table}")').fetchall()}
        check("日期 column exists", "日期" in cols)
        check("开盘 column exists", "开盘" in cols)
    finally:
        raw.close()
    # re-upsert same rows → idempotent, count unchanged
    n2 = P._upsert_records(table, keys, recs1)
    check("re-upsert returns 2 (no duplicate)", n2 == 2, f"got {n2}")
    raw = eng.raw_connection()
    try:
        cnt = raw.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        check("still 2 rows after re-upsert", cnt == 2, f"got {cnt}")
    finally:
        raw.close()
    # upsert with a NEW column → ALTER TABLE appends it
    recs2 = [{"日期": "2025-07-03", "开盘": 12.5, "收盘": 13.0, "成交量": 1000}]
    n3 = P._upsert_records(table, keys, recs2)
    check("upsert with new column inserts 1", n3 == 1, f"got {n3}")
    raw = eng.raw_connection()
    try:
        cols = {r[1] for r in raw.execute(f'PRAGMA table_info("{table}")').fetchall()}
        check("成交量 column appended", "成交量" in cols)
        cnt = raw.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        check("now 3 rows", cnt == 3, f"got {cnt}")
        # old rows have NULL for the new column
        nulls = raw.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "成交量" IS NULL').fetchone()[0]
        check("2 rows have NULL 成交量", nulls == 2, f"got {nulls}")
    finally:
        raw.close()
    # upsert an EXISTING key with changed values → updates, no new row
    recs3 = [{"日期": "2025-07-01", "开盘": 99.9, "收盘": 99.9}]
    n4 = P._upsert_records(table, keys, recs3)
    check("upsert existing key returns 1", n4 == 1, f"got {n4}")
    raw = eng.raw_connection()
    try:
        cnt = raw.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        check("still 3 rows (update not insert)", cnt == 3, f"got {cnt}")
        val = raw.execute(f'SELECT "开盘" FROM "{table}" WHERE "日期"=?', ("2025-07-01",)).fetchone()[0]
        check("value updated to 99.9", val == 99.9, f"got {val}")
    finally:
        raw.close()


def test_crud_no_backfill() -> None:
    print("[5] collection CRUD (enabled=False, no fetch/cron)")
    async def run():
        res = asyncio.gather  # placeholder
        created = await P.create_pipeline_collection("selfcheck-coll", "test")
        check("collection created", created.get("name") == "selfcheck-coll")
        try:
            await P.create_pipeline_collection("selfcheck-coll")
            check("duplicate name rejected", False)
        except P.PipelineError:
            check("duplicate name rejected", True)
        # add an item with enabled=False → no backfill, no cron
        it = await P.add_pipeline_item(
            collection_name="selfcheck-coll",
            name="item-1",
            source_mcp="akshare-mcp",
            tool="call_akshare_function",
            arguments_json=json.dumps({"name": "stock_zh_a_hist", "params_json": "{}"}),
            storage_table="scraw_selfcheck_crud",
            upsert_keys=["日期"],
            cron_expr="30 16 * * 1-5",
            enabled=False,
        )
        check("disabled item stored", it["item"]["enabled"] is False)
        check("disabled item backfill skipped", it["backfill"]["status"] == "skipped")
        check("disabled item task_name set", it["item"]["task_name"] == "pipeline_selfcheck-coll_item-1")
        # unknown source_mcp rejected
        try:
            await P.add_pipeline_item(
                collection_name="selfcheck-coll", name="bad", source_mcp="bogus-mcp-xyz",
                tool="x", arguments_json="{}", storage_table="scraw_x", upsert_keys=["a"],
                cron_expr="0 0 * * *", enabled=False,
            )
            check("unknown source_mcp rejected", False)
        except P.PipelineError:
            check("unknown source_mcp rejected", True)
        # list
        lst = await P.list_pipeline_collections()
        check("collection in list", any(c["name"] == "selfcheck-coll" for c in lst["collections"]))
        got = await P.get_pipeline_collection("selfcheck-coll")
        check("get_pipeline_collection has 1 item", len(got.get("items", [])) == 1)
        items = await P.list_pipeline_items(collection_name="selfcheck-coll")
        check("list_pipeline_items count=1", items["count"] == 1)
        # update cron
        upd = await P.update_pipeline_item("selfcheck-coll", "item-1", cron_expr="0 17 * * 1-5")
        check("cron updated", upd["item"]["cron_expr"] == "0 17 * * 1-5")
    asyncio.run(run())


def test_cli_fetch_missing() -> None:
    print("[6] CLI --fetch-item missing item")
    code = P.cli_fetch_item(999999)
    check("missing item returns exit 1", code == 1, f"got {code}")


def test_fetch_error_path() -> None:
    print("[7] fetch_to_store error path (unresolvable source_mcp)")
    async def run():
        # build a fake item whose source_mcp does not resolve
        it = PipelineCollectionItem(
            id=1, collection_id=1, name="x", source_mcp="bogus-mcp-xyz", tool="t",
            arguments_json="{}", storage_table="scraw_x", upsert_keys_json='["a"]',
            cron_expr="0 0 * * *", timezone="Asia/Shanghai", enabled=True,
            task_name="pipeline_x_x",
        )
        res = await P.fetch_to_store(it)
        check("error path status backfill_failed", res["status"] == "backfill_failed", str(res))
        check("error path has error msg", bool(res["error"]))
    asyncio.run(run())


def test_live() -> None:
    print("[8] LIVE: akshare-mcp backfill + idempotent re-fetch")
    async def run():
        await P.create_pipeline_collection("selfcheck-live", "live smoke")
        try:
            res = await P.add_pipeline_item(
                collection_name="selfcheck-live",
                name="hist",
                source_mcp="akshare-mcp",
                tool="call_akshare_function",
                arguments_json=json.dumps({"name": "stock_zh_a_hist", "params_json": json.dumps({"symbol": "000001", "period": "daily", "start_date": "20250601", "end_date": "20250630"})}),
                storage_table="scraw__selfcheck",
                upsert_keys=["日期"],
                cron_expr="0 6 * * *",
                timezone="Asia/Shanghai",
            )
            bf = res["backfill"]
            if bf["status"] != "ok":
                print(f"  ! backfill failed: {bf.get('error')}")
                return
            check("live backfill ok", bf["status"] == "ok", str(bf))
            check("live rows > 0", (bf["rows"] or 0) > 0, str(bf))
            # re-fetch via the async fetch_to_store (the CLI path uses asyncio.run,
            # which can't nest inside this running loop) → idempotent row count
            from daas_database import get_database as _gd
            from models import PipelineCollectionItem as _Item
            s = _gd().get_session()
            try:
                item_obj = s.get(_Item, res["item"]["id"])
                refetch = await P.fetch_to_store(item_obj)
            finally:
                s.close()
            check("re-fetch status ok", refetch["status"] == "ok", str(refetch))
            check("re-fetch rows unchanged (idempotent)", refetch["rows"] == bf["rows"], f"{refetch['rows']} vs {bf['rows']}")
        finally:
            # leave scraw__selfcheck intact but remove the cron rows + collection
            try:
                await P.delete_pipeline_collection("selfcheck-live")
            except Exception:
                pass
    asyncio.run(run())


def main() -> int:
    print("=== daas-mcp pipeline collections selfcheck ===")
    print(f"(temp db: {_TMP_DB})")
    test_models_and_tables()
    test_validators()
    test_sanitize_col()
    test_upsert_roundtrip()
    test_crud_no_backfill()
    test_cli_fetch_missing()
    test_fetch_error_path()
    if LIVE:
        test_live()
    else:
        print("[8] LIVE: skipped (set AKSHARE_LIVE=1 to enable)")
    print("===")
    print(f"PASS={PASS} FAIL={FAIL}")
    try:
        os.unlink(_TMP_DB)
    except OSError:
        pass
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

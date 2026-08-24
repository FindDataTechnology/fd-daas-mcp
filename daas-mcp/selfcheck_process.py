"""Self-check for the process tools (relocated from process-mcp to daas-mcp) —
exercises the DB mechanics without an LLM call.

Run:  uv run python selfcheck_process.py

Uses a temp DB. Monkeypatches process_tools.extract_text so run_rule's
incremental cursor / idempotent upsert / injection guard are verified with no
network. A live extract_text is only attempted when LLM_API_KEY is set.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# isolate: temp DB + stub env, before importing the process tools
_TMP = Path(tempfile.gettempdir()) / "daas_process_selfcheck.db"
if _TMP.exists():
    _TMP.unlink()
os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_TMP}"
os.environ.setdefault("LLM_MODEL", "gpt-4o")
os.environ.setdefault("LLM_BASE_URL", "https://api.openai.com/v1")
# leave LLM_API_KEY unset → resolve_model errors, list_models still works

sys.path.insert(0, str(Path(__file__).resolve().parent))

import process_database as pdb  # noqa: E402
import process_tools as T  # noqa: E402
import indicator_tools as IT  # noqa: E402
import process_api as server  # noqa: E402  (run_rule impl + tool wrappers)

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'OK ' if cond else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


# ── 1. list_models works without a key ──────────────────────────
lm = T.list_models()
check("list_models returns models", "models" in lm and len(lm["models"]) >= 1)

# resolve_model must refuse without an api_key (force the no-key path; root
# .env may have provided one, so clear the cache + key)
T._MODELS = None
_saved_key = os.environ.pop("LLM_API_KEY", None)
_saved_key2 = os.environ.pop("OPENAI_API_KEY", None)
try:
    T.resolve_model()
    check("resolve_model raises without api_key", False, "did not raise")
except T.ProcessError:
    check("resolve_model raises without api_key", True)
finally:
    if _saved_key is not None:
        os.environ["LLM_API_KEY"] = _saved_key
    if _saved_key2 is not None:
        os.environ["OPENAI_API_KEY"] = _saved_key2
    T._MODELS = None  # rebuild cache for any later live-extract step

# ── 2. build a scraw_<slug> fixture + discover it ────────────────
db = pdb.get_db()
with db.engine.begin() as conn:
    conn.execute(pdb.text(
        "CREATE TABLE scraw_selfcheck (id INTEGER PRIMARY KEY, body TEXT)"
    ))
    for i in range(3):
        conn.execute(pdb.text("INSERT INTO scraw_selfcheck (body) VALUES (:b)"), {"b": f"row {i} text"})

tables = db.list_source_tables()
check("list_source_tables finds scraw_selfcheck", any(t["name"] == "scraw_selfcheck" for t in tables))
check("list_source_tables excludes scraw_configs", not any(t["name"] == "scraw_configs" for t in tables))

# ── 3. rule CRUD ────────────────────────────────────────────────
schema = {"type": "object", "properties": {"sentiment": {"type": "string"}}}
r = db.create_rule(name="sc", source_table="scraw_selfcheck", text_column="body", schema_json=schema)
check("create_rule ok", r["name"] == "sc" and r["source_table"] == "scraw_selfcheck")
check("list_rules includes sc", any(x["name"] == "sc" for x in db.list_rules()))
check("get_rule ok", db.get_rule("sc")["text_column"] == "body")
check("update_rule max_chars", db.update_rule("sc", max_chars=8000)["max_chars"] == 8000)

# create_rule rejects bad table / column
try:
    db.create_rule(name="bad", source_table="scraw_nope", text_column="body", schema_json=schema)
    check("create_rule rejects missing table", False)
except pdb.ProcessError:
    check("create_rule rejects missing table", True)
try:
    db.create_rule(name="bad2", source_table="scraw_selfcheck", text_column="nope", schema_json=schema)
    check("create_rule rejects missing column", False)
except pdb.ProcessError:
    check("create_rule rejects missing column", True)

# ── 4. injection guard ──────────────────────────────────────────
try:
    db.create_rule(name="inj", source_table="scraw_x; DROP TABLE sources;--", text_column="body", schema_json=schema)
    check("injection blocked in create_rule", False)
except pdb.ProcessError:
    check("injection blocked in create_rule", True)
try:
    db.fetch_source_rows("scraw_x; DROP", "body", 0, 10)
    check("injection blocked in fetch_source_rows", False)
except pdb.ProcessError:
    check("injection blocked in fetch_source_rows", True)

# ── 5. run_rule incremental + idempotent (extract_text mocked) ──
calls = {"n": 0}


def _fake_extract(text, schema, prompt=None, model=None, max_chars=12000):
    calls["n"] += 1
    return {"records": [{"sentiment": "neutral", "src": text}], "count": 1, "chunk_count": 1}


_orig = T.extract_text
T.extract_text = _fake_extract
try:
    # server imports process_tools as T (same module object), so the monkeypatch
    # is visible to _run_rule_impl via T.extract_text — but server captured the
    # name at call time, so patch the module server uses:
    import process_tools as _pt
    _pt.extract_text = _fake_extract

    out1 = server._run_rule_impl("sc", batch=500)
    check("run_rule first: processed=3", out1.get("processed") == 3, str(out1))
    check("run_rule first: cursor advanced", out1.get("next_rowid") == 3, str(out1))
    n_results = db.count_results(r["id"])
    check("process_results has 3 rows", n_results == 3, f"got {n_results}")

    out2 = server._run_rule_impl("sc", batch=500)
    check("run_rule second: processed=0", out2.get("processed") == 0, str(out2))
    check("run_rule second: up_to_date", out2.get("up_to_date") is True, str(out2))
    check("process_results still 3 (no dupes)", db.count_results(r["id"]) == 3)

    # re-run after deleting one result row → idempotent (no dupe), cursor already maxed
    with db.engine.begin() as conn:
        conn.execute(pdb.text("DELETE FROM process_results WHERE rule_id=:rid AND source_rowid=1"), {"rid": r["id"]})
    out3 = server._run_rule_impl("sc", batch=500)
    check("run_rule re-run after delete: up_to_date (cursor already maxed)", out3.get("up_to_date") is True, str(out3))
finally:
    T.extract_text = _orig
    _pt.extract_text = _orig

# ── 6. delete_rule cascades ─────────────────────────────────────
ok = db.delete_rule("sc")
check("delete_rule ok", ok is True)
check("process_results cascaded to 0", db.count_results(r["id"]) == 0)

# ── 7. indicator rule → observations (deterministic, no LLM) ─────
# The LLM path above (run_rule) must not have written any observations.
with db.engine.connect() as conn:
    obs_before = conn.execute(pdb.text("SELECT count(*) FROM observations")).scalar()
check("run_rule path wrote no observations", obs_before == 0, f"got {obs_before}")

# Build a numeric series table + a daas source row (soft-ref target).
with db.engine.begin() as conn:
    conn.execute(pdb.text(
        "CREATE TABLE scraw_ind (id INTEGER PRIMARY KEY, date TEXT, close REAL)"
    ))
    prices = [("d1", 10.0), ("d2", 11.0), ("d3", 12.0), ("d4", 13.0), ("d5", 14.0), ("d6", 15.0)]
    conn.execute(
        pdb.text("INSERT INTO scraw_ind (date, close) VALUES (:d, :c)"),
        [{"d": d, "c": c} for d, c in prices],
    )
    conn.execute(
        pdb.text("INSERT INTO sources (name, label, enabled) VALUES (:n, :l, 1)"),
        {"n": "selfcheck_src", "l": "Selfcheck"},
    )

ops = IT.list_indicator_ops()
check("list_indicator_ops returns >=12 ops", len(ops["ops"]) >= 12, f"got {len(ops['ops'])}")

ind = db.create_indicator(
    name="sma3", datasource="selfcheck_src", source_table="scraw_ind",
    date_column="date", value_column="close", op="sma", params={"window": 3},
    indicator_name="sma3_close",
)
check("create_indicator ok", ind["indicator_name"] == "sma3_close", str(ind))
check("list_indicators includes sma3", any(x["name"] == "sma3" for x in db.list_indicators()))
check("get_indicator ok", db.get_indicator("sma3")["op"] == "sma")
check("update_indicator params", db.update_indicator("sma3", params_json={"window": 2})["params"] == {"window": 2})
# restore window=3 for the run check
db.update_indicator("sma3", params_json={"window": 3})

out = db.run_indicator("sma3")
check("run_indicator wrote 4 rows (warmup skipped)", out.get("rows_written") == 4, str(out))
with db.engine.connect() as conn:
    irows = conn.execute(pdb.text(
        "SELECT indicator, source, function_name, value, metadata FROM observations "
        "WHERE source='selfcheck_src' ORDER BY date"
    )).fetchall()
check("observations indicator correct", all(rw[0] == "sma3_close" for rw in irows), str(irows))
check("observations source correct", all(rw[1] == "selfcheck_src" for rw in irows))
check("observations function_name defaulted to source_table", all(rw[2] == "scraw_ind" for rw in irows))
check("observations value is a string", isinstance(irows[0][3], str) and irows[0][3] == "11.0", str(irows[0][3]))
import json as _json
_meta = _json.loads(irows[0][4]) if irows[0][4] else {}
check("observations metadata.rule_name set", _meta.get("rule_name") == "sma3", str(_meta))

# idempotent re-run
out2 = db.run_indicator("sma3")
with db.engine.connect() as conn:
    n_obs = conn.execute(pdb.text("SELECT count(*) FROM observations WHERE source='selfcheck_src'")).scalar()
check("run_indicator idempotent (no row growth)", out2.get("rows_written") == 4 and n_obs == 4, f"got {n_obs}")

# ad-hoc calculate (no persistence)
calc = IT.calculate(db, "scraw_ind", "date", "close", "pct_change")
check("calculate returns full series", calc.get("count") == 6 and len(calc.get("values", [])) == 6, str(calc)[:200])
with db.engine.connect() as conn:
    n_calc = conn.execute(pdb.text("SELECT count(*) FROM observations WHERE indicator='pct_change'")).scalar()
check("calculate wrote no observations", n_calc == 0, f"got {n_calc}")

# create_indicator validation errors
for args, what in [
    (dict(name="bad1", datasource="nope", source_table="scraw_ind", date_column="date", value_column="close", op="sma", params={"window": 3}), "missing datasource"),
    (dict(name="bad2", datasource="selfcheck_src", source_table="scraw_ind", date_column="date", value_column="close", op="sma", params={}), "missing param"),
    (dict(name="bad3", datasource="selfcheck_src", source_table="scraw_ind", date_column="date", value_column="close", op="magic", params={}), "unknown op"),
    (dict(name="bad4", datasource="selfcheck_src", source_table="scraw_nope", date_column="date", value_column="close", op="sma", params={"window": 3}), "missing table"),
]:
    try:
        db.create_indicator(**args)
        check(f"create_indicator rejects {what}", False)
    except pdb.ProcessError:
        check(f"create_indicator rejects {what}", True)

# injection guard on the indicator path
try:
    db.fetch_indicator_series("scraw_x; DROP", "date", "close")
    check("injection blocked in fetch_indicator_series", False)
except pdb.ProcessError:
    check("injection blocked in fetch_indicator_series", True)

# delete_indicator: soft ref — observations survive
ok = db.delete_indicator("sma3")
check("delete_indicator ok", ok is True)
with db.engine.connect() as conn:
    n_survive = conn.execute(pdb.text("SELECT count(*) FROM observations WHERE source='selfcheck_src'")).scalar()
check("observations survive indicator delete (soft ref)", n_survive == 4, f"got {n_survive}")

# ── 8. CLI branch exits without stdio ───────────────────────────
import subprocess

cli = subprocess.run(
    [sys.executable, str(Path(__file__).resolve().parent / "server.py"), "--run-rule", "nonexistent"],
    capture_output=True, text=True, env={**os.environ},
)
check("CLI --run-rule prints JSON + exit 1", cli.returncode == 1 and "rule not found" in cli.stdout, f"rc={cli.returncode}")

cli_ind = subprocess.run(
    [sys.executable, str(Path(__file__).resolve().parent / "server.py"), "--run-indicator", "nonexistent"],
    capture_output=True, text=True, env={**os.environ},
)
check("CLI --run-indicator prints JSON + exit 1", cli_ind.returncode == 1 and "indicator not found" in cli_ind.stdout, f"rc={cli_ind.returncode}")

# ── 9. live extract_text only if a key is configured ─────────────
if os.environ.get("LLM_API_KEY"):
    try:
        res = T.extract_text("Acme Corp revenue grew 20% to $1.2B.", {"type": "object", "properties": {"company": {"type": "string"}}}, max_chars=12000)
        check("live extract_text returns records", "records" in res, str(res)[:200])
    except Exception as e:
        check("live extract_text returns records", False, str(e))
else:
    print("SKIP live extract_text (LLM_API_KEY unset)")

print()
if _failures:
    print(f"FAILED: {len(_failures)} — {_failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")

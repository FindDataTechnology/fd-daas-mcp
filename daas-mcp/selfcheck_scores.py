"""Score round-trip self-check for daas-mcp.

Verifies the `score` concept end-to-end against a TEMP DB (no touch to
mcp/daas.db, no network, no LLM). Exercises:

  1. `set-source-score` writer subcommand → default score lands on `sources`.
  2. `set-source-score` with `score=null` clears the default.
  3. `add-item` with a `score` sets the per-item override at add time.
  4. `set-item-score` writer subcommand → override lands on the item, and the
     resolved effective score (item override if set, else source default, else
     NULL) is correct in the returned dict.
  5. `set-item-score` with `score=null` clears the override → resolved score
     falls back to the datasource default.
  6. `list_collection` surfaces `score` (resolved), `item_score`, and
     `source_default_score` for every item.
  7. Error paths: `set-item-score` on an item not in the collection, and on an
     unknown collection, raise the right errors.

Run:
  uv run --directory mcp/daas-mcp python selfcheck_scores.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

# ── point at a TEMP db BEFORE importing anything that touches daas.db ──
_TMP_DB = tempfile.mktemp(suffix="_scores_selfcheck.db")
os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

_HERE = Path(__file__).resolve().parent
_MODELS = _HERE.parent / "models"
for _p in (str(_MODELS), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import collection_writer as W  # noqa: E402
from daas_database import get_database  # noqa: E402
from registry_service import RegistryService  # noqa: E402

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
    """Invoke collection_writer.main(argv) in-process, capturing stdout/stderr.
    Returns (exit_code, stdout, stderr). SystemExit from _fail is captured."""
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


def _svc() -> RegistryService:
    return RegistryService(get_database().get_session())


def _seed() -> None:
    """Two datasources + one collection with one whole-source item."""
    svc = _svc()
    svc.create_datasource(name="edgar", label="SEC EDGAR", score=0.5)
    svc.create_datasource(name="edinet", label="Japan EDINET")  # no default score
    svc.create_collection(name="disc", description="selfcheck")
    svc.add_to_collection(collection_name="disc", source_name="edgar")  # inherit default


def test_set_source_score() -> None:
    print("[1] set-source-score sets the default score")
    rc, out, _ = _run_writer(
        ["set-source-score", "--json", json.dumps({"name": "edinet", "score": 0.3})]
    )
    check("exits 0", rc == 0, f"rc={rc} out={out!r}")
    payload = _last_json(out)
    check("returns updated score", payload and payload.get("score") == 0.3, f"payload={payload!r}")
    # Verify it landed in the DB.
    src = _svc()._resolve_source("edinet", None)
    check("row has score=0.3", src is not None and src.score == 0.3, f"src.score={getattr(src,'score',None)!r}")


def test_clear_source_score() -> None:
    print("[2] set-source-score with score=null clears the default")
    rc, out, _ = _run_writer(
        ["set-source-score", "--json", json.dumps({"name": "edinet", "score": None})]
    )
    check("exits 0", rc == 0, f"rc={rc}")
    src = _svc()._resolve_source("edinet", None)
    check("row score is NULL", src is not None and src.score is None, f"src.score={getattr(src,'score',None)!r}")


def test_add_item_with_score() -> None:
    print("[3] add-item with score sets the override at add time")
    rc, out, _ = _run_writer(
        [
            "add-item",
            "--json",
            json.dumps({"collection_name": "disc", "source_name": "edinet", "score": 0.8}),
        ]
    )
    check("exits 0", rc == 0, f"rc={rc} out={out!r}")
    payload = _last_json(out)
    check("item has item_score=0.8", payload and payload.get("item_score") == 0.8, f"payload={payload!r}")
    check("resolved score=0.8", payload and payload.get("score") == 0.8, f"payload={payload!r}")


def test_set_item_score_override_wins() -> None:
    print("[4] set-item-score override wins over datasource default")
    # edgar default is 0.5; set an override of 0.9 on the whole-source item.
    rc, out, _ = _run_writer(
        [
            "set-item-score",
            "--json",
            json.dumps({"collection_name": "disc", "source_name": "edgar", "score": 0.9}),
        ]
    )
    check("exits 0", rc == 0, f"rc={rc} out={out!r}")
    payload = _last_json(out)
    check("item_score=0.9", payload and payload.get("item_score") == 0.9, f"payload={payload!r}")
    check("source_default_score=0.5", payload and payload.get("source_default_score") == 0.5, f"payload={payload!r}")
    check("resolved score=0.9 (override wins)", payload and payload.get("score") == 0.9, f"payload={payload!r}")


def test_clear_item_score_falls_back() -> None:
    print("[5] set-item-score with score=null falls back to the default")
    rc, out, _ = _run_writer(
        [
            "set-item-score",
            "--json",
            json.dumps({"collection_name": "disc", "source_name": "edgar", "score": None}),
        ]
    )
    check("exits 0", rc == 0, f"rc={rc}")
    payload = _last_json(out)
    check("item_score is null", payload and payload.get("item_score") is None, f"payload={payload!r}")
    check("source_default_score=0.5", payload and payload.get("source_default_score") == 0.5, f"payload={payload!r}")
    check("resolved score=0.5 (fallback)", payload and payload.get("score") == 0.5, f"payload={payload!r}")


def test_list_collection_surfaces_scores() -> None:
    print("[6] list_collection surfaces item_score, source_default_score, score")
    # State: edgar default 0.5 (no override after test 5), edinet override 0.8.
    res = _svc().list_collection("disc")
    items = {it["source_name"]: it for it in res["items"]}
    edgar = items.get("edgar")
    edinet = items.get("edinet")
    check("edgar item_score is null", edgar and edgar["item_score"] is None, f"edgar={edgar!r}")
    check("edgar source_default_score=0.5", edgar and edgar["source_default_score"] == 0.5, f"edgar={edgar!r}")
    check("edgar resolved score=0.5 (fallback)", edgar and edgar["score"] == 0.5, f"edgar={edgar!r}")
    check("edinet item_score=0.8", edinet and edinet["item_score"] == 0.8, f"edinet={edinet!r}")
    check("edinet resolved score=0.8 (override)", edinet and edinet["score"] == 0.8, f"edinet={edinet!r}")


def test_error_paths() -> None:
    print("[7] error paths: item not in collection, unknown collection")
    # Item not in collection (section-scoped lookup with no matching item).
    rc, out, err = _run_writer(
        [
            "set-item-score",
            "--json",
            json.dumps({"collection_name": "disc", "source_name": "edgar", "section_name": "Nope", "score": 0.1}),
        ]
    )
    combined = out + err
    check("unknown section exits non-zero", rc != 0, f"rc={rc}")
    check("error mentions section not found", "not found" in combined, f"out={combined!r}")

    # Unknown collection.
    rc, out, err = _run_writer(
        [
            "set-item-score",
            "--json",
            json.dumps({"collection_name": "nope", "source_name": "edgar", "score": 0.1}),
        ]
    )
    combined = out + err
    check("unknown collection exits non-zero", rc != 0, f"rc={rc}")
    check("error mentions collection not found", "not found" in combined, f"out={combined!r}")


def main() -> int:
    print("=== daas-mcp scores selfcheck ===")
    print(f"(temp db: {_TMP_DB})")
    _seed()
    test_set_source_score()
    test_clear_source_score()
    test_add_item_with_score()
    test_set_item_score_override_wins()
    test_clear_item_score_falls_back()
    test_list_collection_surfaces_scores()
    test_error_paths()
    print("===")
    print(f"PASS={PASS} FAIL={FAIL}")
    try:
        os.unlink(_TMP_DB)
    except OSError:
        pass
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

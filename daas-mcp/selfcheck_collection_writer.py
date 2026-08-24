"""Smoke test for the collection-write sidecar path.

Guards that the writer (`collection_writer.py`) resolves to the canonical
`daas.db`, independent of the process cwd and of `DAAS_DATABASE_URL`
being present in the inherited env. (Originally guarded the dashboard's
"create new collection" failure where the spawned writer and the sql.js read
path resolved to different DBs; the writer remains a standalone CLI after the
Next.js dashboard was removed.)

This check runs entirely against a TEMP DB (no touch to daas.db, no
network). It exercises:

  1. The writer `create` subcommand end-to-end (in-process) and confirms the
     row lands in the DB the writer connected to.
  2. The duplicate-name error path.
  3. The `update` (rename) and `delete` subcommands.
  4. The writer's `__file__`-based REPO_ROOT anchor (`parents[2]`) actually
     points at the repo root (the dir containing `mcp/daas-mcp/`), and that
     the repo-root `.env` (where DAAS_DATABASE_URL is defined) lives there.

Run:
  uv run --directory mcp/daas-mcp python selfcheck_collection_writer.py
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
_TMP_DB = tempfile.mktemp(suffix="_collwriter_selfcheck.db")
os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_TMP_DB}"

_HERE = Path(__file__).resolve().parent
_MODELS = _HERE.parent / "models"
for _p in (str(_MODELS), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import collection_writer as W  # noqa: E402
from daas_database import get_database  # noqa: E402
from models import DatasourceCollection  # noqa: E402

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

    The writer calls sys.exit(1) on failure (via _fail); we catch SystemExit
    and report its code. Returns (exit_code, stdout, stderr).
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = W.main(argv)
        except SystemExit as e:
            rc = e.code if e.code is not None else 1
    return (rc, out.getvalue(), err.getvalue())


def _collection_names() -> set[str]:
    s = get_database().get_session()
    try:
        rows = s.query(DatasourceCollection.name).all()
        return {r[0] for r in rows}
    finally:
        s.close()


def test_create_and_read() -> None:
    print("[1] writer `create` → row lands in the connected DB")
    rc, out, _ = _run_writer(
        ["create", "--json", json.dumps({"name": "sc-coll", "description": "selfcheck"})]
    )
    check("create exits 0", rc == 0, f"rc={rc} out={out!r}")
    payload = None
    last = [ln for ln in out.splitlines() if ln.strip()][-1:] if out.strip() else []
    if last:
        try:
            payload = json.loads(last[0])
        except json.JSONDecodeError:
            pass
    check("create prints JSON row", payload and payload.get("name") == "sc-coll", f"out={out!r}")
    check("row is queryable in the DB", "sc-coll" in _collection_names())


def test_duplicate_rejected() -> None:
    print("[2] duplicate name rejected with an error JSON")
    rc, out, err = _run_writer(
        ["create", "--json", json.dumps({"name": "sc-coll"})]
    )
    check("duplicate exits non-zero", rc != 0, f"rc={rc}")
    combined = out + err
    check("error mentions 'already exists'", "already exists" in combined, f"out={combined!r}")


def test_update_and_delete() -> None:
    print("[3] writer `update` (rename) + `delete`")
    rc, out, _ = _run_writer(
        ["update", "--json", json.dumps({"name": "sc-coll", "new_name": "sc-coll-2"})]
    )
    check("rename exits 0", rc == 0, f"rc={rc} out={out!r}")
    check("rename took effect", "sc-coll-2" in _collection_names() and "sc-coll" not in _collection_names())

    rc, out, _ = _run_writer(
        ["delete", "--json", json.dumps({"name": "sc-coll-2"})]
    )
    check("delete exits 0", rc == 0, f"rc={rc} out={out!r}")
    check("delete removed the row", "sc-coll-2" not in _collection_names())


def test_repo_root_anchor() -> None:
    print("[4] writer REPO_ROOT anchor (parents[2]) points at the repo root")
    repo_root = Path(W.__file__).resolve().parents[2]
    check("mcp/daas-mcp/collection_writer.py exists under REPO_ROOT",
          (repo_root / "mcp" / "daas-mcp" / "collection_writer.py").exists())
    env_path = repo_root / ".env"
    check("repo-root .env exists", env_path.exists())
    if env_path.exists():
        text = env_path.read_text()
        has_line = any(
            ln.lstrip().startswith("DAAS_DATABASE_URL=") and not ln.lstrip().startswith("#")
            for ln in text.splitlines()
        )
        check("repo-root .env defines DAAS_DATABASE_URL",
              has_line, "missing non-commented DAAS_DATABASE_URL= line")


def main() -> int:
    print("=== daas-mcp collection_writer selfcheck ===")
    print(f"(temp db: {_TMP_DB})")
    test_create_and_read()
    test_duplicate_rejected()
    test_update_and_delete()
    test_repo_root_anchor()
    print("===")
    print(f"PASS={PASS} FAIL={FAIL}")
    try:
        os.unlink(_TMP_DB)
    except OSError:
        pass
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

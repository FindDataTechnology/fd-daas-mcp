"""Workflow engine tests (Task 4.8): manifest validation + interpolation
(unit) and manifest run over a temp DB with a stubbed dispatcher (engine
execution + end-to-end). No gateway subprocess, no LLM, no network.

``_dispatch_step`` is monkeypatched so the engine's ordering/interpolation/
on_failure/resume logic is exercised without a live fd-open-data-mcp upstream
or the sibling registry.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

_FD_HOME = Path(__file__).resolve().parents[1]
_WF_MCP = _FD_HOME / "workflow-mcp"
if str(_WF_MCP) not in sys.path:
    sys.path.insert(0, str(_WF_MCP))

# cron-mcp also ships a bare ``database`` module and workflow-mcp a
# ``workflow_tools`` (both imported first in the full suite, claiming their
# ``sys.modules`` names). Load workflow-mcp's modules explicitly and pin them so
# workflow_tools' own ``from database import get_database`` resolves here, not
# to cron's database or workflow-mcp's workflow_tools.
def _load_module(name: str, filename: Path):
    spec = importlib.util.spec_from_file_location(name, filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


wdb = _load_module("database", _WF_MCP / "database.py")
manifest = _load_module("manifest", _WF_MCP / "manifest.py")
wt = _load_module("workflow_tools", _WF_MCP / "workflow_tools.py")


def _fresh_db() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    url = f"sqlite:///{tmp.name}"
    os.environ["DAAS_DATABASE_URL"] = url
    wdb.Database._instance = None  # re-read the new env URL
    wdb.get_database()  # create tables
    return url


# ── unit: manifest validation + interpolation ─────────────────────


def test_validate_manifest():
    valid = {"name": "demo", "steps": [{"id": "a", "server": "fd-open-data-mcp", "tool": "read"}]}
    assert manifest.validate_manifest(valid) == []
    # unknown server rejected
    assert manifest.validate_manifest(
        {"name": "x", "steps": [{"id": "a", "server": "nope", "tool": "read"}]}
    )
    # missing steps rejected
    assert manifest.validate_manifest({"name": "x"})
    # duplicate step ids rejected
    dup = {"name": "x", "steps": [
        {"id": "a", "server": "fd-open-data-mcp", "tool": "read"},
        {"id": "a", "server": "fd-open-data-mcp", "tool": "read"},
    ]}
    assert any("duplicate" in e for e in manifest.validate_manifest(dup))


def test_interpolate():
    assert manifest.interpolate_args(
        {"c": "$params.code"}, params={"code": "AAPL"}, results={}, env={}
    ) == {"c": "AAPL"}
    assert manifest.resolve_ref(
        "$steps.f.result.rows", params={}, results={"f": {"rows": [1, 2]}}, env={}
    ) == [1, 2]
    assert manifest.resolve_ref("$env.HOME", params={}, results={}, env={"HOME": "/x"}) == "/x"
    # nested dict/list walk in place
    got = manifest.interpolate({"a": ["$params.x"], "b": {"c": "$params.x"}},
                               params={"x": 1}, results={}, env={})
    assert got == {"a": [1], "b": {"c": 1}}


# ── engine execution + end-to-end (stubbed dispatcher) ────────────


def test_run_end_to_end(monkeypatch):
    _fresh_db()
    calls: list[tuple] = []

    def fake_dispatch(step, args):
        calls.append((step["id"], step["tool"], args))
        return {"result": {"echo": args}}

    monkeypatch.setattr(wt, "_dispatch_step", fake_dispatch)

    wt.register(name="demo", manifest_yaml="""
name: demo
version: 1
params:
  code: AAPL
steps:
  - id: resolve
    server: fd-open-data-mcp
    tool: resolve
    args: {code: "$params.code"}
  - id: fetch
    server: fd-open-data-mcp
    tool: fetch
    args: {concept_id: "$steps.resolve.result.echo.code"}
outputs:
  rows: "$steps.fetch.result.echo"
""")
    summary = wt.run(name="demo", params_json='{"code": "MSFT"}')
    assert summary["status"] == "completed", summary
    assert [s["status"] for s in summary["steps"]] == ["completed", "completed"]
    # params override declared default + step result interpolation
    assert calls[0][2] == {"code": "MSFT"}
    assert calls[1][2] == {"concept_id": "MSFT"}
    assert summary["outputs"] == {"rows": {"concept_id": "MSFT"}}


def test_on_failure_abort(monkeypatch):
    _fresh_db()

    def fake_dispatch(step, args):
        if step["tool"] == "boom":
            return {"error": "kapow"}
        return {"result": {"ok": True}}

    monkeypatch.setattr(wt, "_dispatch_step", fake_dispatch)
    wt.register(name="abortwf", manifest_yaml="""
name: abortwf
steps:
  - {id: s1, server: fd-open-data-mcp, tool: good}
  - {id: s2, server: fd-open-data-mcp, tool: boom}
  - {id: s3, server: fd-open-data-mcp, tool: never}
""")
    r = wt.run(name="abortwf")
    assert r["status"] == "failed"
    assert [s["status"] for s in r["steps"]] == ["completed", "failed"]
    assert r["steps"][1]["error"] == "kapow"


def test_checkpoint_pause_resume(monkeypatch):
    _fresh_db()

    def fake_dispatch(step, args):
        return {"result": {"ok": True}}

    monkeypatch.setattr(wt, "_dispatch_step", fake_dispatch)
    wt.register(name="ckpt", manifest_yaml="""
name: ckpt
steps:
  - {id: s1, server: fd-open-data-mcp, tool: t1, type: checkpoint}
  - {id: s2, server: fd-open-data-mcp, tool: t2}
""")
    r = wt.run(name="ckpt")
    assert r["status"] == "paused"
    assert "resume_token" in r

    r2 = wt.resume(run_id=r["resume_token"], approved=True)
    assert r2["status"] == "completed"
    assert [s["id"] for s in r2["steps"]] == ["s2"]

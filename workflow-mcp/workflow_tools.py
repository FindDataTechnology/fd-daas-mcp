"""Workflow tools for the consolidated fd-daas-mcp server - a manifest-driven,
ordered, idempotent run over gateway (fd-open-data-mcp) and local (fd-daas-mcp)
tools.

A workflow is one row in ``workflows`` whose ``manifest`` column holds a JSON
object (validated by ``manifest.py``): ``{name, version?, params?, steps[],
outputs?}``. Each step is ``{id, server, tool, args?, on_failure?, type?}``
where ``server`` is ``fd-open-data-mcp`` (dispatched via the gateway) or
``fd-daas-mcp`` (dispatched in-proc to a registered sibling tool). ``args`` may
reference ``$params.*``, ``$steps.<id>.result[.<path>]``, and ``$env.*``.

Runs are persisted in ``workflow_runs`` + ``workflow_step_results``. ``on_failure``
is abort (default) / continue / checkpoint; ``type: checkpoint`` pauses after a
successful step. A paused run is resumed via ``workflow_resume(run_id, approved)``,
with the resume cursor (merged params + accumulated results + next step index)
held in a sentinel ``workflow_step_results`` row at ``step_sort_order=0``.

Thin tool functions over direct ORM access (shared ``models`` package). Tools
are registered by ``server.py`` via ``app.tool(<name>)`` and surface as
``workflow_<name>`` on the consolidated server.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import func

from models import Workflow, WorkflowRun, WorkflowStepResult

from database import get_database
from manifest import interpolate, interpolate_args, validate_manifest


def _session():
    return get_database().get_session()


def _now():
    return datetime.now(timezone.utc)


def _sibling_tools() -> dict:
    """Registered sibling tool functions from the registry cache, looked up
    lazily inside tool bodies (never at import time). Keys are namespaced
    (``daas_run_indicator``). See research-mcp/research_tools.py."""
    from daas.fd_daas_mcp.registry import build, namespaced

    return {namespaced(g, n): fn for g, n, fn in build()}


def _parse_manifest_yaml(manifest_yaml: str) -> dict:
    data = yaml.safe_load(manifest_yaml or "")
    if not isinstance(data, dict):
        raise ValueError("manifest must be a YAML object")
    return data


def _validate(manifest: dict) -> None:
    errors = validate_manifest(manifest)
    if errors:
        raise ValueError("invalid manifest: " + "; ".join(errors))


def _next_version(session, name: str) -> int:
    return (session.query(func.max(Workflow.version)).filter(Workflow.name == name).scalar() or 0) + 1


# ── registration / read / update / delete ─────────────────────────


def register(name: str, manifest_yaml: str, description: str | None = None) -> dict:
    """Register a new workflow manifest (YAML). Version bumps on name collision
    unless the manifest declares its own ``version``."""
    manifest = _parse_manifest_yaml(manifest_yaml)
    _validate(manifest)
    session = _session()
    try:
        version = manifest.get("version") or _next_version(session, name)
        if session.query(Workflow).filter_by(name=name, version=version).first():
            raise ValueError(f"workflow {name!r} v{version} already exists")
        row = Workflow(
            name=name, version=version, manifest=json.dumps(manifest),
            description=description, enabled=True,
        )
        session.add(row)
        session.commit()
        return row.to_dict()
    finally:
        session.close()


def get(name: str, version: int | None = None) -> dict:
    """Get a workflow by name (latest version by default)."""
    session = _session()
    try:
        q = session.query(Workflow).filter(Workflow.name == name)
        if version is not None:
            q = q.filter(Workflow.version == version)
        row = q.order_by(Workflow.version.desc()).first()
        return row.to_dict() if row else {"error": f"workflow {name!r} not found"}
    finally:
        session.close()


def list() -> list[dict]:
    """List every workflow (name, version, description, enabled, step_count)."""
    session = _session()
    try:
        rows = session.query(Workflow).order_by(Workflow.name, Workflow.version).all()
        return [r.to_dict() for r in rows]
    finally:
        session.close()


def update(name: str, version: int | None = None, manifest_yaml: str | None = None,
                    description: str | None = None, enabled: bool | None = None) -> dict:
    """Patch a workflow. Only provided fields change. ``version`` defaults to
    the latest version of ``name``."""
    session = _session()
    try:
        q = session.query(Workflow).filter(Workflow.name == name)
        if version is not None:
            q = q.filter(Workflow.version == version)
        row = q.order_by(Workflow.version.desc()).first()
        if row is None:
            return {"error": f"workflow {name!r} not found"}
        if manifest_yaml is not None:
            manifest = _parse_manifest_yaml(manifest_yaml)
            _validate(manifest)
            row.manifest = json.dumps(manifest)
        if description is not None:
            row.description = description
        if enabled is not None:
            row.enabled = enabled
        session.commit()
        return row.to_dict()
    finally:
        session.close()


def delete(name: str, version: int | None = None) -> dict:
    """Delete a workflow. Omit ``version`` to delete every version of ``name``.
    Steps/runs cascade via FK."""
    session = _session()
    try:
        q = session.query(Workflow).filter(Workflow.name == name)
        if version is not None:
            q = q.filter(Workflow.version == version)
        rows = q.all()
        if not rows:
            return {"error": f"workflow {name!r} not found"}
        deleted = len(rows)
        for row in rows:
            session.delete(row)
        session.commit()
        return {"deleted": deleted}
    finally:
        session.close()


def inspect(name: str, version: int | None = None) -> dict:
    """Validate a stored manifest and return its parsed step plan (no execution)."""
    info = get(name, version)
    if "error" in info:
        return info
    # get() returns `manifest` already parsed by Workflow.to_dict()
    manifest = info["manifest"] if isinstance(info["manifest"], dict) else json.loads(info["manifest"])
    _validate(manifest)
    return {
        "name": info["name"],
        "version": info["version"],
        "enabled": info["enabled"],
        "description": info["description"],
        "params": manifest.get("params", {}),
        "outputs": manifest.get("outputs", {}),
        "steps": [
            {k: s.get(k) for k in ("id", "server", "tool", "args", "on_failure", "type")}
            for s in manifest.get("steps", [])
        ],
    }


# ── engine ─────────────────────────────────────────────────────────


def _dispatch_step(step: dict, args: dict) -> dict:
    server = step["server"]
    tool = step["tool"]
    if server == "fd-open-data-mcp":
        # ponytail: P4 re-homes gateway plumbing to gateway-mcp/; the registry
        # evicts gateway_tools after build(), so re-add the dir before import.
        gw = Path(__file__).resolve().parents[1] / "gateway-mcp"
        if str(gw) not in sys.path:
            sys.path.insert(0, str(gw))
        from gateway_tools import call_data_mcp_sync

        return call_data_mcp_sync(server, tool, json.dumps(args))
    if server == "fd-daas-mcp":
        fn = _sibling_tools().get(tool)
        if fn is None:
            return {"error": f"local tool {tool!r} not registered"}
        try:
            return {"result": fn(**args)}
        except Exception as exc:  # noqa: BLE001 - step errors are captured, not raised
            return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": f"unknown server {server!r}"}


def _record_step(session, run_id: int, sort_order: int, status: str, result_value, error: str | None) -> None:
    session.add(WorkflowStepResult(
        run_id=run_id, step_sort_order=sort_order, status=status,
        output_json=json.dumps(result_value, default=str) if result_value is not None else None,
        error=error, ran_at=_now(),
    ))
    # ponytail: commit each step result so the workflow session doesn't hold a
    # write lock into the next step — a later step's external write (another
    # engine) would otherwise deadlock against this transaction.
    session.commit()


def _pause(session, run, params: dict, results: dict, next_index: int) -> None:
    """Persist the resume cursor in a sentinel step result (step_sort_order=0)."""
    run.status = "paused"
    state = json.dumps({"params": params, "results": results}, default=str)
    meta = json.dumps({"next_step_index": next_index})
    sentinel = session.query(WorkflowStepResult).filter_by(run_id=run.id, step_sort_order=0).first()
    if sentinel is None:
        sentinel = WorkflowStepResult(run_id=run.id, step_sort_order=0, status="checkpoint")
        session.add(sentinel)
    sentinel.status = "checkpoint"
    sentinel.output_json = state
    sentinel.meta_json = meta
    sentinel.ran_at = _now()


def _execute(session, run, workflow, manifest: dict, params: dict, results: dict, start_index: int) -> dict:
    """Run manifest steps from ``start_index`` (1-based). Returns a summary dict;
    the run row is left committed by the caller."""
    steps = manifest.get("steps", [])
    env = os.environ
    step_results: list[dict] = []

    for idx in range(start_index, len(steps) + 1):
        step = steps[idx - 1]
        sid = step["id"]
        args = interpolate_args(step.get("args") or {}, params=params, results=results, env=env)
        result = _dispatch_step(step, args)
        ok = isinstance(result, dict) and "error" not in result
        result_value = result.get("result") if isinstance(result, dict) else result
        error = None if ok else (result.get("error") if isinstance(result, dict) else str(result))

        if ok:
            results[sid] = result_value
        _record_step(session, run.id, idx, "completed" if ok else "failed", result_value, error)
        step_results.append({"id": sid, "status": "completed" if ok else "failed",
                             "error": error})

        if ok and step.get("type") == "checkpoint":
            _pause(session, run, params, results, idx + 1)
            return _summary(run, "paused", step_results, None)

        if not ok:
            on_failure = step.get("on_failure", "abort")
            if on_failure == "abort":
                run.status = "failed"
                run.finished_at = _now()
                return _summary(run, "failed", step_results, None)
            if on_failure == "checkpoint":
                _pause(session, run, params, results, idx + 1)
                return _summary(run, "paused", step_results, None)
            # continue → record and move on

    run.status = "completed"
    run.finished_at = _now()
    outputs = interpolate(manifest.get("outputs") or {}, params=params, results=results, env=env)
    return _summary(run, "completed", step_results, outputs)


def _summary(run, status: str, step_results: list[dict], outputs) -> dict:
    out = {
        "run_id": run.id,
        "status": status,
        "steps": step_results,
    }
    if status == "paused":
        out["resume_token"] = run.id
    if outputs is not None:
        out["outputs"] = outputs
    return out


def run(name: str, params_json: str = "{}", version: int | None = None) -> dict:
    """Run a workflow manifest. ``params_json`` merges over the manifest's
    declared ``params``. Returns run id + status; a paused run carries a
    ``resume_token`` for ``resume``."""
    info = get(name, version)
    if "error" in info:
        return info
    manifest = info["manifest"] if isinstance(info["manifest"], dict) else json.loads(info["manifest"])
    _validate(manifest)
    try:
        run_params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError as exc:
        return {"error": f"invalid params_json: {exc}"}
    if not isinstance(run_params, dict):
        return {"error": "params_json must decode to a JSON object"}

    session = _session()
    try:
        workflow = session.query(Workflow).filter(Workflow.id == info["id"]).first()
        run = WorkflowRun(workflow_id=workflow.id, status="running", started_at=_now())
        session.add(run)
        session.flush()  # assign run.id
        # ponytail: release the write lock before steps run. Each step's own
        # writes go through a separate engine (daas_run_indicator → process_db
        # upserts observations); a held transaction here deadlocks them
        # ("database is locked"). Commit the run row, then execute steps; each
        # step result commits too (multi-step manifests with external writes in
        # step 2+ need the same release between steps).
        session.commit()
        params = {**manifest.get("params", {}), **run_params}
        summary = _execute(session, run, workflow, manifest, params, {}, 1)
        session.commit()
        return summary
    finally:
        session.close()


def resume(run_id: int, approved: bool = True) -> dict:
    """Resume a paused run. ``approved=False`` marks it failed."""
    session = _session()
    try:
        run = session.query(WorkflowRun).filter(WorkflowRun.id == run_id).first()
        if run is None:
            return {"error": f"run {run_id} not found"}
        if run.status != "paused":
            return {"error": f"run {run_id} is {run.status!r}, not paused"}
        sentinel = session.query(WorkflowStepResult).filter_by(run_id=run_id, step_sort_order=0).first()
        if sentinel is None:
            return {"error": f"run {run_id} has no checkpoint cursor"}
        state = json.loads(sentinel.output_json)
        params, results = state["params"], state["results"]
        next_index = json.loads(sentinel.meta_json)["next_step_index"]
        session.delete(sentinel)
        if not approved:
            run.status = "failed"
            run.finished_at = _now()
            session.commit()
            return _summary(run, "failed", [], None)
        workflow = session.query(Workflow).filter(Workflow.id == run.workflow_id).first()
        manifest = json.loads(workflow.manifest)
        run.status = "running"
        summary = _execute(session, run, workflow, manifest, params, results, next_index)
        session.commit()
        return summary
    finally:
        session.close()


# Minimal self-check (also exercised by workflow-mcp tests).
if __name__ == "__main__":
    manifest = {
        "name": "demo", "params": {"code": "AAPL"},
        "steps": [{"id": "echo", "server": "fd-daas-mcp", "tool": "daas_list_sources", "args": {}}],
        "outputs": {"rows": "$steps.echo.result"},
    }
    assert validate_manifest(manifest) == [], validate_manifest(manifest)
    got = interpolate_args({"c": "$params.code"}, params={"code": "AAPL"}, results={}, env={})
    assert got == {"c": "AAPL"}, got
    print("workflow_tools ok")

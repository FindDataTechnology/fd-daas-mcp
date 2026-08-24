"""Manifest validation + interpolation for the workflow layer (D4).

A workflow manifest is a JSON object declaring an ordered, idempotent run:

    {
      "name": "core-data-fetch",
      "version": 1,
      "params": {"code": "600519"},              # declared defaults (optional)
      "steps": [
        {"id": "resolve", "server": "fd-open-data-mcp", "tool": "resolve",
         "args": {"code": "$params.code"}},
        {"id": "fetch", "server": "fd-open-data-mcp", "tool": "fetch",
         "args": {"concept_id": "$steps.resolve.result.concept_id"}}
      ],
      "outputs": {"rows": "$steps.fetch.result.rows"}
    }

``validate_manifest`` checks the shape with jsonschema. ``interpolate``
resolves the three reference forms inside ``args``/``outputs``:

  - ``$params.<path>``   -> the run-provided params (falling back to the
                            manifest's declared ``params`` defaults).
  - ``$steps.<id>.result[.<path>]`` -> a prior step's result, then an optional
                            dotted path into it.
  - ``$env.<name>``      -> ``os.environ[name]``.

Only whole-string references are substituted (no in-string templating); a
reference that resolves to a non-string (dict/list/number) is substituted
in place.
"""
from __future__ import annotations

import os
from typing import Any

import jsonschema
from jsonschema import ValidationError

MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "steps"],
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "version": {"type": "integer", "minimum": 1},
        "description": {"type": "string"},
        "params": {"type": "object"},
        "outputs": {"type": "object"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "server", "tool"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "server": {"enum": ["fd-open-data-mcp", "fd-daas-mcp"]},
                    "tool": {"type": "string", "minLength": 1},
                    "args": {"type": "object"},
                    "on_failure": {"enum": ["abort", "continue", "checkpoint"]},
                    "type": {"enum": ["checkpoint"]},
                    "description": {"type": "string"},
                },
            },
        },
    },
}


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return a list of human-readable errors; empty means the manifest is
    valid. Also enforces that step ids are unique (jsonschema cannot)."""
    errors: list[str] = []
    try:
        jsonschema.validate(instance=manifest, schema=MANIFEST_SCHEMA)
    except ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "<root>"
        errors.append(f"{path}: {exc.message}")

    steps = manifest.get("steps") or []
    seen: set[str] = set()
    for step in steps:
        sid = step.get("id")
        if sid in seen:
            errors.append(f"steps: duplicate step id {sid!r}")
        if sid:
            seen.add(sid)
    return errors


def _lookup_path(root: Any, path: list[str]) -> Any:
    cur = root
    for part in path:
        if isinstance(cur, dict):
            if part not in cur:
                raise KeyError(part)
            cur = cur[part]
        elif isinstance(cur, list):
            cur = cur[int(part)]
        else:
            raise TypeError(f"cannot index {type(cur).__name__} by {part!r}")
    return cur


def resolve_ref(ref: str, *, params: dict, results: dict, env: dict) -> Any:
    """Resolve one whole-string ``$...`` reference. Raises KeyError/ValueError
    on a missing target so the caller can fail the step with a clear message."""
    parts = ref.split(".")
    head = parts[0]
    if head == "$params":
        return _lookup_path(params, parts[1:])
    if head == "$steps":
        step_id = parts[1]
        if step_id not in results:
            raise KeyError(f"unknown step id {step_id!r}")
        # $steps.<id>.result[.<path>] — the literal "result" marker is optional.
        path = parts[3:] if len(parts) >= 3 and parts[2] == "result" else parts[2:]
        return _lookup_path(results[step_id], path)
    if head == "$env":
        name = parts[1] if len(parts) > 1 else ""
        if name not in env:
            raise KeyError(f"env var {name!r} not set")
        return env[name]
    raise ValueError(f"unknown reference {ref!r}")


def interpolate(value: Any, *, params: dict, results: dict, env: dict) -> Any:
    """Recursively resolve ``$...`` references in ``value`` (str -> ref, or a
    dict/list walked in place). Non-reference values pass through unchanged."""
    if isinstance(value, str) and value.startswith("$"):
        return resolve_ref(value, params=params, results=results, env=env)
    if isinstance(value, dict):
        return {k: interpolate(v, params=params, results=results, env=env) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, params=params, results=results, env=env) for v in value]
    return value


def interpolate_args(args: dict, *, params: dict, results: dict, env: dict) -> dict:
    """Convenience: interpolate a step's ``args`` dict against the run state."""
    return interpolate(args, params=params, results=results, env=env) if isinstance(args, dict) else {}


# Minimal self-check (also exercised by workflow-mcp tests).
if __name__ == "__main__":
    manifest = {
        "name": "demo",
        "params": {"code": "AAPL"},
        "steps": [
            {"id": "fetch", "server": "fd-open-data-mcp", "tool": "fetch",
             "args": {"code": "$params.code"}},
        ],
        "outputs": {"rows": "$steps.fetch.result.rows"},
    }
    assert validate_manifest(manifest) == [], validate_manifest(manifest)
    bad = {"name": "demo", "steps": [{"id": "a", "server": "x", "tool": "y"}]}
    assert validate_manifest(bad), "invalid server must be rejected"
    got = interpolate_args({"c": "$params.code"}, params={"code": "AAPL"}, results={}, env=os.environ)
    assert got == {"c": "AAPL"}, got
    got = interpolate("$steps.f.result.rows", params={}, results={"f": {"rows": [1]}}, env={})
    assert got == [1], got
    print("manifest ok")

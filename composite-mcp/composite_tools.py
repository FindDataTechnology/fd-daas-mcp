"""Tools for composite-mcp.

Two kinds:

1. Management tools (always present) — plain functions that curate a
   composite's upstreams, selected tools, and chains in daas.db. They take
   a `composite` name so any composite can be curated regardless of which
   one the running instance serves.

2. Served tools (built at startup) — proxied upstream tools (lazy
   `FunctionTool` stubs from `make_proxy_tool`; upstream spawned on call)
   and chained tools (built by `make_chain_tool`). Wired in server.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from fastmcp import Client
from fastmcp.tools import FunctionTool

from composite_database import build_client, get_composite_db

# ═══════════════════════════════════════════════════════════════
# management tools — composites
# ═══════════════════════════════════════════════════════════════


def list() -> str:  # noqa: A004 (intentional: registers as composite_list)
    """List all defined composites (name + description)."""
    rows = get_composite_db().list_composites()
    if not rows:
        return "No composites defined. Use create(name, description)."
    return "\n".join(f"{r['name']}: {r['description'] or '(no description)'}" for r in rows)


def create(name: str, description: Optional[str] = None) -> str:
    """Create a new composite. Raises if the name already exists.

    Args:
        name: Unique composite name (used as COMPOSITE env value to serve it).
        description: Optional human-readable description.
    """
    row = get_composite_db().create_composite(name, description)
    return f"Created composite {row['name']} (id={row['id']}). Set COMPOSITE={name} and restart to serve it."


# ═══════════════════════════════════════════════════════════════
# management tools — upstreams
# ═══════════════════════════════════════════════════════════════


def _require_composite(name: str):
    comp = get_composite_db().get_composite_by_name(name)
    if comp is None:
        raise ValueError(f"composite {name!r} not found")
    return comp


def list_upstreams(composite: str) -> str:
    """List the upstreams defined for a composite."""
    comp = _require_composite(composite)
    rows = get_composite_db().list_upstreams(comp.id)
    if not rows:
        return f"No upstreams in {composite}. Use add_upstream(...)."
    lines = []
    for r in rows:
        if r["transport"] == "http":
            lines.append(f"{r['key']} [http]: {r['url']}")
        else:
            lines.append(f"{r['key']} [stdio]: {r['command']} {' '.join(r['args'])}")
    return "\n".join(lines)


def add_upstream(
    composite: str,
    key: str,
    transport: str,
    command: Optional[str] = None,
    args: Optional[list] = None,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
    url: Optional[str] = None,
) -> str:
    """Add an upstream MCP server to a composite.

    For stdio: provide command, args, cwd (env optional).
    For http: provide url.

    Args:
        composite: Composite name.
        key: Short id used as the mount namespace (served tools become <key>_<tool>).
        transport: 'stdio' or 'http'.
        command: stdio executable.
        args: stdio argv list.
        env: stdio env dict.
        cwd: stdio working directory.
        url: http upstream URL.
    """
    comp = _require_composite(composite)
    if transport == "stdio" and not command:
        raise ValueError("stdio upstream requires 'command'")
    if transport == "http" and not url:
        raise ValueError("http upstream requires 'url'")
    row = get_composite_db().add_upstream(
        comp.id, key, transport, command, args, env, cwd, url
    )
    return f"Added upstream {row['key']} [{row['transport']}] to {composite}."


def remove_upstream(composite: str, key: str) -> str:
    """Remove an upstream (and its selected tools) from a composite."""
    comp = _require_composite(composite)
    get_composite_db().remove_upstream(comp.id, key)
    return f"Removed upstream {key} from {composite} (and its selected tools)."


# ═══════════════════════════════════════════════════════════════
# management tools — tool selection
# ═══════════════════════════════════════════════════════════════


async def list_available_tools(
    composite: str, upstream_key: str, query: Optional[str] = None
) -> str:
    """List the tools an upstream currently exposes (live), optionally filtered.

    Args:
        composite: Composite name.
        upstream_key: Upstream key within the composite.
        query: Optional case-insensitive substring filter on tool name.
    """
    comp = _require_composite(composite)
    upstreams = get_composite_db().list_upstreams(comp.id)
    upstream = next((u for u in upstreams if u["key"] == upstream_key), None)
    if upstream is None:
        raise ValueError(f"upstream {upstream_key!r} not in {composite!r}")

    async with build_client(upstream) as client:
        tools = await client.list_tools()
    names = [t.name for t in tools]
    if query:
        q = query.lower()
        names = [n for n in names if q in n.lower()]
    return json.dumps({"total": len(names), "tools": names}, ensure_ascii=False)


def add_tool(
    composite: str, upstream_key: str, tool_name: str, alias: Optional[str] = None
) -> str:
    """Select an upstream tool to be exposed (proxied) by the composite.

    The tool is served as <upstream_key>_<tool_name> on next start.
    (alias is accepted but unused in v1.)

    Args:
        composite: Composite name.
        upstream_key: Upstream key within the composite.
        tool_name: Tool name on the upstream.
        alias: Reserved (unused in v1).
    """
    comp = _require_composite(composite)
    get_composite_db().add_tool(comp.id, upstream_key, tool_name, alias)
    return f"Added tool {tool_name} from {upstream_key} to {composite}. Served as {upstream_key}_{tool_name} on next start."


def remove_tool(composite: str, upstream_key: str, tool_name: str) -> str:
    """Remove a selected tool from a composite."""
    comp = _require_composite(composite)
    get_composite_db().remove_tool(comp.id, upstream_key, tool_name)
    return f"Removed tool {tool_name} from {upstream_key} in {composite}."


def list_tools(composite: str) -> str:
    """List the tools currently selected for a composite."""
    comp = _require_composite(composite)
    rows = get_composite_db().list_composite_tools(comp.id)
    if not rows:
        return f"No tools selected for {composite}."
    return "\n".join(f"{r['upstream_key']}_{r['tool_name']}" for r in rows)


# ═══════════════════════════════════════════════════════════════
# management tools — chains
# ═══════════════════════════════════════════════════════════════


def add_chained_tool(
    composite: str,
    name: str,
    steps: list,
    description: Optional[str] = None,
) -> str:
    """Define a chained tool: a linear pipeline of upstream tool calls.

    Each step is {"upstream": <key>, "tool": <name>, "input": {param: value}}.
    Input values may be literals or references: "$prev.<path>" (prior step's
    result) or "$step[N].<path>" (any prior step's result). Linear only — no
    branching/conditionals.

    Args:
        composite: Composite name.
        name: Exposed tool name.
        steps: List of step objects.
        description: Optional description.
    """
    comp = _require_composite(composite)
    get_composite_db().add_chain(comp.id, name, steps, description)
    return f"Added chain {name} to {composite}. Served as {name} on next start."


def remove_chained_tool(composite: str, name: str) -> str:
    """Remove a chained tool from a composite."""
    comp = _require_composite(composite)
    get_composite_db().remove_chain(comp.id, name)
    return f"Removed chain {name} from {composite}."


def list_chained_tools(composite: str) -> str:
    """List the chained tools defined for a composite."""
    comp = _require_composite(composite)
    rows = get_composite_db().list_chains(comp.id)
    if not rows:
        return f"No chains defined for {composite}."
    return "\n".join(
        f"{r['name']}: {len(r['steps'])} steps — {r['description'] or ''}" for r in rows
    )


# ═══════════════════════════════════════════════════════════════
# management tools — manifests (manifest-mode CRUD over relational schema)
# ═══════════════════════════════════════════════════════════════


def create_manifest(
    name: str,
    upstreams: list,
    tools: list,
    workflows: Optional[list] = None,
    prompt: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Create a composite manifest: composite + upstreams + tools + workflows + prompt in one call.

    Args:
        name: Unique composite name (used as COMPOSITE env to serve it).
        upstreams: [{key, transport, command?, args?, env?, cwd?, url?}].
        tools: [{upstream (key), tool (name), alias?}].
        workflows: Names of registered workflow manifests to surface inside this composite.
        prompt: System prompt text for the composite surface.
        description: Optional human-readable description.
    """
    row = get_composite_db().create_manifest(
        name, upstreams, tools, workflows, prompt, description
    )
    return json.dumps(row, ensure_ascii=False, default=str)


def update_manifest(
    name: str,
    upstreams: Optional[list] = None,
    tools: Optional[list] = None,
    workflows: Optional[list] = None,
    prompt: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Partially update a composite manifest. Only provided fields change.

    upstreams/tools (when given) REPLACE the existing sets wholesale.
    """
    row = get_composite_db().update_manifest(
        name, upstreams, tools, workflows, prompt, description
    )
    return json.dumps(row, ensure_ascii=False, default=str)


def delete_manifest(name: str) -> str:
    """Delete a composite manifest (cascades upstreams/tools/chains)."""
    return json.dumps(get_composite_db().delete_manifest(name), ensure_ascii=False)


def list_manifests() -> str:
    """List all composites as manifests (name, upstreams, tools, workflows, prompt)."""
    return json.dumps(get_composite_db().list_manifests(), ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════════════
# served tool builders
# ═══════════════════════════════════════════════════════════════


def _dotted(obj: Any, path: str) -> Any:
    """Resolve a dot-path against a dict/list object. Empty path → whole object."""
    if path == "":
        return obj
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise ValueError(f"cannot resolve '{part}' on {type(cur).__name__}")
    return cur


def _resolve_input(value: Any, results: list, current_index: int) -> Any:
    """Resolve a step input value: literal, $prev.<path>, or $step[N].<path>."""
    if not isinstance(value, str):
        return value
    if value.startswith("$prev."):
        if current_index == 0:
            raise ValueError("$prev used in step 0 (no prior step)")
        return _dotted(results[current_index - 1], value[len("$prev."):])
    if value.startswith("$step["):
        close = value.index("]")
        n = int(value[len("$step["):close])
        if n >= current_index:
            raise ValueError(f"$step[{n}] references current or future step")
        path = value[close + 2:]  # skip "]."
        return _dotted(results[n], path)
    return value


def make_chain_tool(name: str, steps: list, upstreams_by_key: dict, description: Optional[str] = None) -> FunctionTool:
    """Build a FunctionTool that runs a linear chain of upstream tool calls.

    upstreams_by_key: {key: upstream_dict} from load_composite.
    Returns a parameterless tool (the pipeline is fully defined by its steps).
    """

    async def _chain() -> Any:
        results: list = []
        for i, step in enumerate(steps):
            upstream = upstreams_by_key.get(step["upstream"])
            if upstream is None:
                raise ValueError(f"step {i}: unknown upstream {step['upstream']!r}")
            raw_input = step.get("input", {}) or {}
            resolved = {
                k: _resolve_input(v, results, i) for k, v in raw_input.items()
            }
            try:
                async with build_client(upstream) as client:
                    result = await client.call_tool(step["tool"], resolved)
            except Exception as exc:  # fail-fast: surface the failing step
                raise RuntimeError(f"chain {name} failed at step {i} ({step['upstream']}.{step['tool']}): {exc}") from exc
            results.append(result.data)
        return results[-1] if results else None

    _chain.__name__ = name
    return FunctionTool.from_function(
        _chain, name=name, description=description or f"Chained tool {name}."
    )


def make_proxy_tool(
    key: str,
    tool_name: str,
    upstream: dict,
    description: Optional[str] = None,
) -> FunctionTool:
    """Build a lazy FunctionTool that forwards to an upstream tool on call.

    The upstream is spawned ONLY when the tool is called (per-call
    `build_client`), so listing the composite's tools never spawns the
    upstream — this avoids the nested stdio spawn that previously failed
    with "Connection closed" when gateway-mcp listed composite-mcp's tools
    (the old `create_proxy` + `app.mount` approach eagerly spawned the
    upstream at list time). Mirrors `make_chain_tool`'s spawn-on-call pattern.

    Args:
        key: The upstream's mount key in the composite (used as the name prefix).
        tool_name: The tool name on the upstream.
        upstream: The upstream dict (from load_composite's `upstreams` list).
        description: Optional tool description.
    """
    served_name = f"{key}_{tool_name}"

    async def _proxy(arguments: str = "{}") -> Any:
        try:
            args_dict = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return {"error": f"Invalid arguments JSON: {exc}"}
        if not isinstance(args_dict, dict):
            return {"error": f"arguments must decode to a JSON object, got {type(args_dict).__name__}"}
        try:
            async with build_client(upstream) as client:
                result = await client.call_tool(tool_name, args_dict)
        except Exception as exc:  # surface any spawn/transport/call error
            return {"error": f"call '{key}.{tool_name}' failed: {type(exc).__name__}: {exc}"}
        # prefer structured .data; fall back to text content
        data = getattr(result, "data", None)
        if data is not None:
            return data
        content = getattr(result, "content", None) or []
        texts = [getattr(c, "text", None) for c in content]
        texts = [t for t in texts if t is not None]
        if not texts:
            return None
        return texts[0] if len(texts) == 1 else texts

    _proxy.__name__ = served_name
    desc = description or (
        f"Proxied tool '{tool_name}' on upstream '{key}'. "
        f"Call with a JSON object of the upstream tool's arguments."
    )
    return FunctionTool.from_function(_proxy, name=served_name, description=desc)


def make_workflow_tool(name: str, description: Optional[str] = None) -> FunctionTool:
    """Build a lazy FunctionTool that runs a registered workflow manifest.

    ``name`` references a row in the ``workflows`` table (registered via
    ``workflow_register``). The workflow engine's ``run`` is imported lazily
    inside the tool body (the workflow-mcp dir is added to sys.path on call),
    so listing the composite's tools never imports the workflow engine — this
    mirrors the spawn-on-call pattern of ``make_proxy_tool``/``make_chain_tool``.

    Args:
        name: The registered workflow manifest name.
        description: Optional tool description.
    """

    async def _wf(params_json: str = "{}") -> Any:
        wf = Path(__file__).resolve().parents[1] / "workflow-mcp"
        if str(wf) not in sys.path:
            sys.path.insert(0, str(wf))
        try:
            from workflow_tools import run as workflow_run  # type: ignore
        except Exception as exc:  # noqa: BLE001 - surface import errors to the caller
            return {"error": f"workflow engine unavailable: {type(exc).__name__}: {exc}"}
        try:
            return workflow_run(name, params_json)
        except Exception as exc:  # noqa: BLE001 - step errors are captured in the summary, not raised
            return {"error": f"workflow {name!r} failed: {type(exc).__name__}: {exc}"}

    _wf.__name__ = name
    desc = description or f"Run workflow '{name}'. Call with a JSON object of workflow params."
    return FunctionTool.from_function(_wf, name=name, description=desc)

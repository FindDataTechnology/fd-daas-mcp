"""Gateway tools — route live data requests to the project's
data-fetch upstream (`fd-open-data-mcp`) via a persistent fastmcp.Client
pool.

Two layers:

1. Async cores (`_list_tools_core`, `_call_tool_core`) — get a reusable
   client from the `gateway_client_pool` singleton (lazy-created, cached
   across calls, rebuilt on config change). Shared by the FastMCP tools and
   the sync wrappers used by the workflow layer.

2. FastMCP tools — `list_data_mcps`, `list_data_mcp_tools`, `call_data_mcp`,
   plus management CRUD (`add_data_mcp`, `remove_data_mcp`,
   `get_data_mcp`). Registered in server.py.

The data-fetch upstream is kept alive between calls via a persistent
client pool; its launch config lives in `gateway_upstreams` (seeded by
`seed_upstreams.py`).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from gateway_client_pool import get_client_pool
from gateway_database import get_gateway_db


# ═══════════════════════════════════════════════════════════════
# serialization helper
# ═══════════════════════════════════════════════════════════════


def _ensure_jsonable(value: Any) -> Any:
    """Best-effort coerce an upstream tool result into JSON-serializable form.

    Upstream MCPs return varied types (dicts, pydantic models, DataFrames,
    strings). Prefer the structured `.data` field when present; this helper
    handles the common non-JSON-native types so the gateway always returns
    clean JSON.
    """
    if value is None:
        return None
    # passthrough native JSON types
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dict, list)):
        return value
    # pydantic v2
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            pass
    # pydantic v1
    if hasattr(value, "dict") and callable(getattr(value, "dict", None)):
        try:
            return value.dict()
        except Exception:  # noqa: BLE001
            pass
    # pandas DataFrame / Series
    try:
        import pandas as pd  # type: ignore
        if isinstance(value, pd.DataFrame):
            return {
                "type": "dataframe",
                "shape": list(value.shape),
                "columns": list(value.columns),
                "data": value.where(value.notna(), None).to_dict(orient="records"),
            }
        if isinstance(value, pd.Series):
            return {
                "type": "series",
                "length": len(value),
                "data": value.where(value.notna(), None).to_dict(),
            }
    except ImportError:
        pass
    # fallback: stringify
    return str(value)


def _extract_result_data(result: Any) -> Any:
    """Pull a JSON-serializable payload out of a fastmcp CallToolResult."""
    # Structured output (preferred)
    data = getattr(result, "data", None)
    if data is not None:
        return _ensure_jsonable(data)
    # Text content fallback
    content = getattr(result, "content", None) or []
    texts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            texts.append(text)
    if not texts:
        return None
    return texts[0] if len(texts) == 1 else texts


# ═══════════════════════════════════════════════════════════════
# async cores — shared by FastMCP tools and sync CrewAI wrappers
# ═══════════════════════════════════════════════════════════════


async def _list_tools_core(server: str) -> dict:
    pool = get_client_pool()
    try:
        client = await pool.get_client(server)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        tools = await client.list_tools()
    except Exception as exc:  # noqa: BLE001 — surface any transport/call error
        return {"error": f"failed to list tools on '{server}': {type(exc).__name__}: {exc}"}
    items = []
    for t in tools:
        item = {"name": getattr(t, "name", None), "description": getattr(t, "description", "") or ""}
        params = getattr(t, "parameters", None)
        if params:
            item["parameters"] = params
        items.append(item)
    return {"server": server, "count": len(items), "tools": items}


async def _call_tool_core(server: str, tool: str, arguments: dict) -> dict:
    pool = get_client_pool()
    try:
        client = await pool.get_client(server)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        result = await client.call_tool(tool, arguments)
    except Exception as exc:  # noqa: BLE001 — surface any call/transport error
        return {"error": f"call '{server}.{tool}' failed: {type(exc).__name__}: {exc}"}
    # fastmcp marks tool-level errors on the result
    if getattr(result, "is_error", False):
        return {"error": f"upstream '{server}' tool '{tool}' returned an error", "result": _extract_result_data(result)}
    return {"server": server, "tool": tool, "result": _extract_result_data(result)}


# ═══════════════════════════════════════════════════════════════
# FastMCP tools — gateway
# ═══════════════════════════════════════════════════════════════


def list_data_mcps(include_disabled: bool = False) -> dict:
    """List the data-fetch MCP upstreams gateway-mcp can route to.

    Each entry is a row from the `gateway_upstreams` table (name, transport,
    enabled, description). Disabled upstreams are hidden unless
    `include_disabled=True`.

    Args:
        include_disabled: If True, also return disabled upstreams.
    """
    rows = get_gateway_db().list_upstreams(include_disabled=include_disabled)
    return {
        "count": len(rows),
        "upstreams": [
            {
                "name": r["name"],
                "transport": r["transport"],
                "enabled": r["enabled"],
                "description": r["description"],
            }
            for r in rows
        ],
    }


async def list_data_mcp_tools(server: str) -> dict:
    """List the tools exposed by a data-fetch MCP upstream (live).

    Reuses the persistent client from the connection pool — the client stays
    alive across calls and is only recreated when the upstream config changes.
    Returns each tool's name + description (+ parameters when available).

    Args:
        server: The upstream name (e.g. 'yfinance').
    """
    return await _list_tools_core(server)


async def call_data_mcp(server: str, tool: str, arguments: str = "{}") -> dict:
    """Call a tool on a data-fetch MCP upstream and return its result.

    Connects to the named upstream via the persistent client pool, invokes
    the named tool with the JSON-deserialized `arguments`, and returns the
    upstream's result. The client stays alive across calls and is only
    recreated when the upstream config changes.

    Args:
        server: The upstream name (e.g. 'yfinance').
        tool: The tool name on that upstream (e.g. 'ticker_history').
        arguments: JSON object string of argument name→value pairs.
                   Example: '{"symbol": "AAPL", "period": "1mo"}'
    """
    try:
        args_dict = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        return {"error": f"Invalid arguments JSON: {exc}"}
    if not isinstance(args_dict, dict):
        return {"error": f"arguments must decode to a JSON object, got {type(args_dict).__name__}"}
    return await _call_tool_core(server, tool, args_dict)


# ═══════════════════════════════════════════════════════════════
# FastMCP tools — management (CRUD over gateway_upstreams)
# ═══════════════════════════════════════════════════════════════


def add_data_mcp(
    name: str,
    transport: str = "stdio",
    command: Optional[str] = None,
    args: Optional[list] = None,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
    enabled: bool = True,
    description: Optional[str] = None,
    url: Optional[str] = None,
) -> dict:
    """Add or update a data-fetch MCP upstream (idempotent upsert by name).

    Args:
        name: Unique upstream name (used as the `server` argument by gateway tools).
        transport: 'stdio' (default) or 'http'.
        command: stdio executable (e.g. 'uv' or a fastmcp binary).
        args: stdio argv list (e.g. ['run','--directory','...','python','server.py']).
        env: optional stdio env overrides (merged with the current env).
        cwd: stdio working directory.
        enabled: If False, the upstream is stored but hidden from gateway tools.
        description: Optional human-readable description.
        url: HTTP transport URL (required when transport='http').
    """
    row = get_gateway_db().upsert_upstream(
        name=name,
        transport=transport,
        url=url,
        command=command,
        args=args,
        env=env,
        cwd=cwd,
        enabled=enabled,
        description=description,
    )
    return {"status": "upserted", "upstream": row}


def remove_data_mcp(name: str) -> dict:
    """Remove a data-fetch MCP upstream from the registry.

    Args:
        name: The upstream name to delete.
    """
    deleted = get_gateway_db().delete_upstream(name)
    return {"status": "deleted" if deleted else "not_found", "name": name}


def get_data_mcp(name: str) -> dict:
    """Get one data-fetch MCP upstream's full launch config.

    Args:
        name: The upstream name.
    """
    row = get_gateway_db().get_upstream(name)
    if row is None:
        return {"error": f"upstream '{name}' not found"}
    return row


# ═══════════════════════════════════════════════════════════════
# health probe — ping each http upstream, auto-flip transport on
# failure/recovery. Mirrors the client-pool fallback path (client_pool.py
# lines 134-165): on http failure + stdio command present → flip to stdio;
# on recovery of a stdio-flipped row whose url is set → flip back to http.
# The pool self-heals on its next get_client() (key change → rebuild).
# ═══════════════════════════════════════════════════════════════


async def _ping_http(url: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Lightweight HTTP reachability probe for an MCP endpoint.

    Builds a throwaway ``Client(StreamableHttpTransport(url))`` and tries
    ``__aenter__`` with a short timeout. Returns ``(reachable, message)``.
    Never raises — network errors are captured as ``reachable=False`` so the
    probe is safe to call from selfcheck.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    client = Client(StreamableHttpTransport(url))
    try:
        await asyncio.wait_for(client.__aenter__(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — any transport error = unreachable
        return False, f"{type(exc).__name__}: {exc}"
    try:
        await asyncio.wait_for(client.__aexit__(None, None, None), timeout=timeout)
    except Exception:  # noqa: BLE001 — teardown errors are irrelevant
        pass
    return True, f"OK ({url})"


async def gateway_health() -> dict:
    """Health-probe each enabled gateway upstream and auto-flip transport.

    For each enabled upstream in ``gateway_upstreams``:

    - **http** transport: ping the row's ``url``. On failure AND the row
      carries a stdio ``command``, flip ``transport`` to ``stdio`` (degraded
      mode — mirrors the client-pool fallback so the next ``get_client()``
      launches the subprocess). On success, no change.
    - **stdio** transport with a ``url`` set (i.e. it was flipped from http by
      a prior fallback): ping the ``url``. On success flip ``transport`` back
      to ``http`` (recovered). On failure, leave as stdio.
    - **stdio**-only (no ``url``): skipped (nothing to probe).

    Returns a per-upstream status report. Never raises — network errors are
    captured as degraded states so the probe is safe to call from selfcheck.
    """
    db = get_gateway_db()
    rows = db.list_upstreams(include_disabled=False)
    results = []
    for row in rows:
        name = row["name"]
        transport = row.get("transport")
        url = row.get("url")
        command = row.get("command")
        entry: dict[str, Any] = {
            "name": name,
            "transport_before": transport,
            "url": url,
        }
        if transport == "http" and url:
            ok, msg = await _ping_http(url)
            entry["reachable"] = ok
            entry["detail"] = msg
            if not ok and command:
                db.set_transport(name, "stdio")
                entry["transport_after"] = "stdio"
                entry["action"] = "flipped-to-stdio (http unreachable, stdio fallback armed)"
            elif not ok:
                entry["transport_after"] = transport
                entry["action"] = "degraded (http down, no stdio fallback)"
            else:
                entry["transport_after"] = transport
                entry["action"] = "healthy"
        elif transport == "stdio" and url:
            # likely flipped from http by the fallback path; probe recovery
            ok, msg = await _ping_http(url)
            entry["reachable"] = ok
            entry["detail"] = msg
            if ok:
                db.set_transport(name, "http")
                entry["transport_after"] = "http"
                entry["action"] = "flipped-to-http (endpoint recovered)"
            else:
                entry["transport_after"] = transport
                entry["action"] = "degraded (stdio mode, http still down)"
        else:
            entry["reachable"] = None
            entry["transport_after"] = transport
            entry["action"] = "skipped (stdio-only upstream, no url to probe)"
        results.append(entry)
    return {"count": len(results), "upstreams": results}


def gateway_health_sync() -> dict:
    """Sync wrapper around :func:`gateway_health` for selfcheck / CLI use.

    Runs the async probe in a fresh event loop. Safe to call from a sync
    context (CLI, selfcheck main). Do NOT call from inside a running event
    loop — invoke ``await gateway_health()`` directly there.
    """
    return asyncio.run(gateway_health())


# ═══════════════════════════════════════════════════════════════
# sync wrappers — for the CrewAI DataCrew (which runs sync tools)
# ═══════════════════════════════════════════════════════════════


def list_data_mcp_tools_sync(server: str) -> dict:
    """Sync wrapper around `list_data_mcp_tools` for CrewAI tools.

    Runs the async core in a fresh event loop. Safe to call from a sync
    CrewAI tool because CrewAI executes tools in a worker thread with no
    running loop.
    """
    return asyncio.run(_list_tools_core(server))


def call_data_mcp_sync(server: str, tool: str, arguments: str = "{}") -> dict:
    """Sync wrapper around `call_data_mcp` for CrewAI tools."""
    try:
        args_dict = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        return {"error": f"Invalid arguments JSON: {exc}"}
    if not isinstance(args_dict, dict):
        return {"error": f"arguments must decode to a JSON object, got {type(args_dict).__name__}"}
    return asyncio.run(_call_tool_core(server, tool, args_dict))

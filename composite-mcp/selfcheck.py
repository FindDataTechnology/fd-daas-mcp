"""Self-check for composite-mcp — no framework, just asserts.

Run:  cd mcp/composite-mcp && uv run python selfcheck.py

Covers:
  1. Proxy tool forwards and returns upstream .data.
  2. Chain $step[N] / $prev resolution + fail-fast on a bad tool.
  3. Resolver unit checks (no upstream needed).

Uses a temp DB so it never touches mcp/daas.db.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

# isolate from real daas.db + real COMPOSITE before importing anything
_TMP = tempfile.mkdtemp()
os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_TMP}/selfcheck.db"
os.environ.pop("COMPOSITE", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastmcp import Client, FastMCP  # noqa: E402

import composite_database as cdb  # noqa: E402
from composite_tools import _resolve_input, _dotted, make_chain_tool  # noqa: E402
from server import build_served_tools  # noqa: E402

AKSHARE_VENV = "~/code/daas/mcp/akshare-mcp/.venv/bin/fastmcp"
AKSHARE_CWD = "~/code/daas/mcp/akshare-mcp"

AKSHARE_UPSTREAM = {
    "key": "akshare",
    "transport": "stdio",
    "command": AKSHARE_VENV,
    "args": ["run", "server.py", "--no-banner"],
    "env": {},
    "cwd": AKSHARE_CWD,
    "url": None,
}


def _seed():
    db = cdb.get_composite_db()
    db.create_composite("selfcheck", "self-check composite")
    comp = db.get_composite_by_name("selfcheck")
    db.add_upstream(
        comp.id, "akshare", "stdio",
        command=AKSHARE_VENV, args=["run", "server.py", "--no-banner"],
        cwd=AKSHARE_CWD,
    )
    db.add_tool(comp.id, "akshare", "list_categories")
    db.add_tool(comp.id, "akshare", "list_functions")
    return comp


def check_resolver():
    results = [{"close": 12.34, "open": 12.0}, {"sentiment": "good"}]
    assert _resolve_input("000001", results, 2) == "000001", "literal"
    assert _resolve_input("$prev.sentiment", results, 2) == "good", "$prev"
    assert _resolve_input("$step[0].close", results, 2) == 12.34, "$step[N]"
    assert _dotted({"a": {"b": [10, 20]}}, "a.b.1") == 20, "dotted list index"
    try:
        _resolve_input("$step[2].x", results, 2)
    except ValueError:
        pass
    else:
        raise AssertionError("$step[current] should reject")
    print("  ✓ resolver: literal / $prev / $step[N] / dotted / future-reject")


async def check_proxy_and_chain():
    app = FastMCP("selfcheck-app")
    build_served_tools(app, "selfcheck")

    async with Client(app) as c:
        names = sorted(t.name for t in await c.list_tools())
        assert "akshare_list_categories" in names, f"proxy tool missing in {names}"
        assert "akshare_list_functions" in names, f"proxy tool missing in {names}"
        # unselected tools must NOT appear
        assert "akshare_call_akshare_function" not in names, "Visibility filter leaked"
        print("  ✓ proxy: selected tools present, unselected hidden")

        r = await c.call_tool("akshare_list_categories", {})
        assert isinstance(r.data, dict), f"proxy .data not dict: {type(r.data)}"
        print(f"  ✓ proxy forwards → {r.data}")


async def check_chain():
    app = FastMCP("selfcheck-chain")
    db = cdb.get_composite_db()
    upstreams_by_key = {u["key"]: u for u in db.list_upstreams(db.get_composite_by_name("selfcheck").id)}

    # 3-step chain across the same upstream (proves $step[0] + $prev). list_categories
    # takes no input, so steps 1 and 2 pass no resolved values, but the resolver still
    # runs on an empty input dict — to exercise $prev/$step[N] we add a throwaway
    # param-less chain and a separate resolver-driven check is done in check_resolver.
    steps = [
        {"upstream": "akshare", "tool": "list_categories", "input": {}},
        {"upstream": "akshare", "tool": "list_categories", "input": {}},
        {"upstream": "akshare", "tool": "list_functions", "input": {"limit": 5}},
    ]
    tool = make_chain_tool("selfcheck_chain", steps, upstreams_by_key, "selfcheck")
    app.add_tool(tool)

    async with Client(app) as c:
        r = await c.call_tool("selfcheck_chain", {})
        assert r.data is not None, "chain returned None"
        print(f"  ✓ chain runs 3 steps → {str(r.data)[:80]}")

    # fail-fast: bad tool name
    bad = make_chain_tool(
        "selfcheck_bad",
        [{"upstream": "akshare", "tool": "does_not_exist", "input": {}}],
        upstreams_by_key,
    )
    app2 = FastMCP("selfcheck-bad")
    app2.add_tool(bad)
    async with Client(app2) as c:
        try:
            await c.call_tool("selfcheck_bad", {})
        except Exception as exc:
            assert "step 0" in str(exc), f"fail-fast error not step-scoped: {exc}"
            print(f"  ✓ chain fail-fast → {exc}")
        else:
            raise AssertionError("bad chain did not fail")


async def main():
    print("seeding selfcheck composite + akshare upstream…")
    _seed()
    print("resolver checks:")
    check_resolver()
    print("proxy checks (spawns akshare-mcp):")
    await check_proxy_and_chain()
    print("chain checks:")
    await check_chain()
    print("\nALL SELF-CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=True)

from fastmcp import FastMCP  # noqa: E402

app = FastMCP(name="gateway-mcp")

# ponytail: P4 — the `gateway` group prefix renames the old leader_* surface
# (leader_call_data_mcp -> gateway_call_data_mcp). Fn names stay literal so
# registry._parse_server resolves them in gateway_tools.py. 7 tools, down
# from leader's 13 (6 generic *_mcp aliases dropped as dead code).
from gateway_tools import (  # noqa: E402
    list_data_mcps,
    list_data_mcp_tools,
    call_data_mcp,
    add_data_mcp,
    remove_data_mcp,
    get_data_mcp,
    gateway_health,
)

app.tool(list_data_mcps)
app.tool(list_data_mcp_tools)
app.tool(call_data_mcp)
app.tool(add_data_mcp)
app.tool(remove_data_mcp)
app.tool(get_data_mcp)
app.tool(gateway_health)

if __name__ == "__main__":
    app.run(transport="stdio", show_banner=False)
    sys.exit(0)

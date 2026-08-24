"""MCP server for the research group - a persisted research bundle that links
an entity collection, indicator collection, rules, dashboard, and cron pipeline
collection under one name, plus a generated markdown report.

Loaded by the consolidated ``fd-daas-mcp`` registry (``inline: False``); tools
are registered here via ``app.tool(<name>)`` and surface as ``research_<name>``
on the consolidated server.
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=True)

from fastmcp import FastMCP  # noqa: E402

app = FastMCP(name="research-mcp")

from research_tools import (  # noqa: E402
    add_component,
    create,
    delete,
    generate_report,
    get,
    list,  # noqa: A004 (intentional: registers as research_list)
    refresh,
    remove_component,
    update,
)

app.tool(create)
app.tool(get)
app.tool(list)
app.tool(update)
app.tool(delete)
app.tool(generate_report)
app.tool(refresh)
app.tool(add_component)
app.tool(remove_component)


if __name__ == "__main__":
    app.run(transport="stdio", show_banner=False)
    sys.exit(0)

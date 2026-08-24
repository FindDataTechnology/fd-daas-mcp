from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=True)

from fastmcp import FastMCP  # noqa: E402

app = FastMCP(name="workflow-mcp")

from workflow_tools import (  # noqa: E402
    register,
    get,
    list,
    update,
    delete,
    run,
    inspect,
    resume,
)

app.tool(register)
app.tool(get)
app.tool(list)
app.tool(update)
app.tool(delete)
app.tool(run)
app.tool(inspect)
app.tool(resume)

if __name__ == "__main__":
    app.run(transport="stdio", show_banner=False)
    sys.exit(0)

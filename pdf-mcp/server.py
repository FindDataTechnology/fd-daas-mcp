"""
MCP Server for the pdf group - local PDF/text vector search.

Exposes 6 tools:
  ingest_document  - ingest a PDF (file path or URL): extract, chunk, embed, persist
  ingest_text      - ingest raw text (no extraction)
  search_documents - semantic KNN search over ingested chunks
  list_documents   - list ingested documents
  get_document     - get one document + chunk sample
  delete_document  - cascade-delete a document + its chunks + vectors

Local only: sentence-transformers embeddings + sqlite-vec vec0 index in daas.db.
No API key, no document egress. The group is optional and gated on `sqlite_vec`
(see daas/fd_daas_mcp/registry.py SOURCES).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load the repo-root .env (cli-anything/.env, where DAAS_DATABASE_URL +
# PDF_EMBEDDING_* are defined) first, then this MCP's own .env with override=True.
# parents[2] reaches the repo root from fd-daas-mcp/pdf-mcp/server.py.
REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=True)

from fastmcp import FastMCP

app = FastMCP(name="pdf-mcp")

# Register tools (inline=False: the registry parses these app.tool() calls for
# tool names, then resolves the functions in pdf_tools.py).
from pdf_tools import (
    ingest_document,
    ingest_text,
    search_documents,
    list_documents,
    get_document,
    delete_document,
    cli_ingest,
    cli_search,
)

app.tool(ingest_document)
app.tool(ingest_text)
app.tool(search_documents)
app.tool(list_documents)
app.tool(get_document)
app.tool(delete_document)


if __name__ == "__main__":
    # CLI branches for cron-mcp shell tasks: run a path in-process and exit
    # (no stdio server start). Mirrors daas-mcp/server.py's --run-rule /
    # --run-indicator / --sync-entity-collection branches.
    #
    # Invoke via:
    #   fd-daas-mcp/.venv/bin/python fd-daas-mcp/pdf-mcp/server.py --pdf-ingest <path|url>
    #   fd-daas-mcp/.venv/bin/python fd-daas-mcp/pdf-mcp/server.py --pdf-search "<query>"
    # (uv run is currently broken for fd-daas-mcp - use .venv/bin/python.)
    if "--pdf-ingest" in sys.argv:
        i = sys.argv.index("--pdf-ingest")
        if i + 1 >= len(sys.argv):
            print(json.dumps({"error": "--pdf-ingest requires a <path|url> argument"}))
            sys.exit(2)
        sys.exit(cli_ingest(sys.argv[i + 1]))
    if "--pdf-search" in sys.argv:
        i = sys.argv.index("--pdf-search")
        if i + 1 >= len(sys.argv):
            print(json.dumps({"error": "--pdf-search requires a <query> argument"}))
            sys.exit(2)
        sys.exit(cli_search(sys.argv[i + 1]))

    app.run(transport="stdio", show_banner=False)

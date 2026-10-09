"""MCP Server for the customer-dataset group — client-owned data in the cell
data root (wire-customer-local-data).

Deployment profile ``cell`` (wire cells; ADR-0001) — the merged fd-daas-mcp
server loads this group only under that profile, and the tools surface as
``customer_dataset_*`` (the group name IS the namespace prefix).
"""
from __future__ import annotations

from fastmcp import FastMCP

app = FastMCP(name="customer-dataset-mcp")

from customer_dataset_tools import (
    ingest_begin,
    ingest_chunk,
    ingest_commit,
    ingest_abort,
    overview,
    preview,
    query,
    aggregate,
    correction_add,
    correction_list,
    correction_dangling,
    correction_revert,
    write_batch,
    create_declared,
    add_column,
    derive,
    join_indicators,
    derive_refresh,
    lineage,
    delete,
    provenance_wal_pending,
    provenance_wal_ack,
)

app.tool(ingest_begin)
app.tool(ingest_chunk)
app.tool(ingest_commit)
app.tool(ingest_abort)
app.tool(overview)
app.tool(preview)
app.tool(query)
app.tool(aggregate)
app.tool(correction_add)
app.tool(correction_list)
app.tool(correction_dangling)
app.tool(correction_revert)
app.tool(write_batch)
app.tool(create_declared)
app.tool(add_column)
app.tool(derive)
app.tool(join_indicators)
app.tool(derive_refresh)
app.tool(lineage)
app.tool(delete)
app.tool(provenance_wal_pending)
app.tool(provenance_wal_ack)

if __name__ == "__main__":
    app.run(transport="stdio", show_banner=False)

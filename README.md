# fd-daas-mcp

Consolidated DAAS MCP server + CLI - hosts the `alerts`, `cron`, `composite`,
`daas`, `dashboard`, `gateway`, and `workflow` tool groups (157 tools) behind one stdio
FastMCP server and one Click CLI. Both consume `daas/fd_daas_mcp/registry.py`
so the server and CLI surfaces cannot drift.

The `gateway` group routes data-fetch calls to a single **`fd-open-data-mcp`**
upstream - a concept-based semantic fetcher (ranking + failover + caching) that
is a Python dependency sourced from the sibling `~/finddata/fd-open-data-mcp`
repo and launched from this venv as `python -m fd_open_data_mcp.server`. It
replaces the 11 former per-source data-fetch MCPs. Fetch via
`gateway_call_data_mcp('fd-open-data-mcp', 'read', …)` directly, or register a
manifest and run it via `workflow_run` for multi-step fetches.

## Layout

```
fd-daas-mcp/
  daas/fd_daas_mcp/   # thin consolidation layer
    server.py                 # FastMCP app - registers every tool as <group>_<tool>
    registry.py               # AST-harvests tools from each <group>-mcp/ (per-group sys.modules isolation)
    cli.py                    # Click CLI - auto-generated from registry.build()
    selfcheck.py              # offline invariants (run_invariants())
  <group>-mcp/                # alerts/cron/composite/daas/dashboard/gateway/workflow tool code (folded, not rewritten)
  models/                     # the mcp-models schema package (editable, [tool.uv.sources])
  bin/fd-daas-mcp-server      # portable launcher (the .mcp.json `command`)
  tests/                      # offline pytest suite
  pyproject.toml
```

## Launch (MCP server)

Repo-root `.mcp.json` points `command` at `fd-daas-mcp/bin/fd-daas-mcp-server`,
a self-locating POSIX shell script that sets `PYTHONPATH` and execs
`.venv/bin/python -m daas.fd_daas_mcp.server`. (Claude Code ignores
`.mcp.json`'s `cwd` field, so the launch is self-locating.)

Run the server manually:

```bash
fd-daas-mcp/bin/fd-daas-mcp-server
```

On startup it logs the registration report:

```
registry: 157 tools across 8 groups (failed=0, skipped_optional=1)
fd-daas-mcp server: registered=157 failed=0 skipped_optional=1
```

## CLI

```bash
fd-daas-mcp <group> <tool> [key=value ...] [--json]   # invoke a tool
fd-daas-mcp                                            # REPL (needs [repl] extra for history)
fd-daas-mcp --help                                     # authoritative live surface
```

The CLI tree is `cli <group> <tool>` (nested Click groups), generated from the
same `registry.build()` as the server.

## Tests

```bash
fd-daas-mcp/.venv/bin/python -m pytest fd-daas-mcp/tests -q
```

Offline, no network, no live MCP transport. See `tests/README.md` for coverage
and environment notes (notably: run via `.venv/bin/python`, not `uv run`, which
re-resolves optional extras from PyPI and fails offline).

## Selfcheck

```bash
fd-daas-mcp/.venv/bin/python -m daas.fd_daas_mcp.selfcheck
```

Verifies: >=155 tools across 6 core groups, known collisions namespaced,
leaf-module isolation, no APScheduler thread (cron suppression), and the
registration report has no core-group failure. The same invariants run as a
pytest assertion in `tests/test_selfcheck.py`.

## Registration report

`registry.build_report()` returns `{"registered": [...], "failed": [...],
"skipped_optional": [...]}`. `failed` lists load-time and `app.tool`-registration
failures as `(group, name, error)`; a core-group entry fails the selfcheck loudly.
`skipped_optional` lists optional groups whose dependency was absent (INFO, not a
failure). `note_failed(group, name, error)` lets the server surface `app.tool`
failures into the report.

## Optional / dropped groups

Optional groups load only when their `"dep"` imports (recorded as
`skipped_optional` when absent). The `pdf` group was **restored** as a local
vector-search group (sqlite-vec + sentence-transformers; see the `pdf` extra and
`openspec/changes/add-pdf-vector-search`). The `scrapling`/`firecrawl`/`massive`
groups remain **dropped** - documented in `registry.py` with archived-restore-spec
pointers:

- `scrapling`, `firecrawl` -> `openspec/changes/archive/2026-07-12-fold-scrapling-add-firecrawl`
- `massive` -> `openspec/changes/archive/2026-07-06-add-massive-datasources`

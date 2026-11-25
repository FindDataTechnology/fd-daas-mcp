---
name: fd-daas-based-data-fetch
description: Fetch financial/economic data by running the registered `data-fetch` workflow manifest, which resolves the entity via fd-open-data-mcp and reads concept values over dates. Returns rows + the source identifier; scraw persistence is an optional follow-up via scripts/upsert.py. Use when the user wants to fetch data for a stock, country, or indicator. No direct sqlite3 resolution, no dispatch.py, no direct Python data-library import.
---

# fd-daas-based-data-fetch (thin shell)

Fetch data by running the `data-fetch` workflow manifest. The manifest owns
resolve→fetch over fd-open-data-mcp; this skill gathers params, runs the
manifest, handles checkpoints, and optionally persists the returned rows. No
direct `sqlite3` entity resolution, no `scripts/dispatch.py`, no
`uv run --with <lib>` direct import — those moved behind the manifest.

## When to use

- "fetch AAPL close price for last month / 查一下比亚迪的日线"
- "get CPI for China / fetch EDGAR filing X"

Do NOT use for: building a dashboard (`fd-daas-dashboard-creator`), creating an
entity collection (`fd-daas-entities-collection-creator`), or computing an
indicator over an existing series (use the `indicators` manifest /
`daas_run_indicator`).

## Step 1 — Gather params

The `data-fetch` manifest takes:

| param | type | how to discover |
|-------|------|-----------------|
| `entity_type` | str (`stock`/`country`) | user intent |
| `entity_id` | int | `gateway_call_data_mcp('fd-open-data-mcp','list_entities', {entity_type, query})` |
| `source` | str (`yfinance`/`akshare`/`edgar`/`edinet`/…) | user or coverage lookup |
| `concept_id` | int | `gateway_call_data_mcp('fd-open-data-mcp','list_concepts', {entity_type})` |
| `dates` | list[str] | user date range, ISO `YYYY-MM-DD` |

## Step 2 — Resolve, then read

Two calls, both on `fd-open-data-mcp` (no server-side workflow):

```python
gateway_call_data_mcp("fd-open-data-mcp", "resolve_entity",
                      {"entity_type": "stock", "entity_id": 1, "source": "yfinance"})
gateway_call_data_mcp("fd-open-data-mcp", "read",
                      {"concept_id": 1, "entity_type": "stock", "entity_id": 1,
                       "dates": ["2025-01-01", "2025-01-31"]})
```

`resolve_entity` returns the source-mapping `identifier` (e.g. `AAPL`); `read`
returns the concept records over `dates`. Keep both — the identifier is the
provenance you cite later, and `read` without a resolved entity is a guess.

## Step 3 — Checkpoint handling

If `status` is `paused`, the manifest hit a `type: checkpoint` step. Inspect
the `resume_token` + the sentinel step at `sort_order=0`, decide (e.g. confirm
the resolved identifier), then:

```python
workflow_resume(run_id=<run_id>, approved=True)   # approved=False marks the run failed
```

`workflow_inspect(name="data-fetch")` shows the validated step plan without
executing — use it to preview before a run.

## Step 4 — Persist (optional, scripts/upsert.py)

The manifest does NOT write to `scraw_*` — persistence stays in this shell.
To persist the returned rows into a scraw table:

```bash
uv run python scripts/upsert.py --table scraw_<slug> --keys date \
  --records '<json: outputs.rows>'
```

For a single-value series bound to an indicator, skip raw persistence and use
the `indicators` manifest (`daas_run_indicator`) which writes `observations`
directly. `upsert.py` backs up `daas.db` to `.bak` first and sets
`PRAGMA foreign_keys=ON`. **No automatic refresh** — re-run Step 2 when you want
fresh data.

## Hard rules

- **Fetch is two gateway calls: `resolve_entity` then `read`.** No direct
  `sqlite3` entity resolution, no `scripts/dispatch.py`, no
  `uv run --with akshare/yfinance/…` direct library import.
- **Persist via `scripts/upsert.py`** (scraw) or the `indicators` manifest
  (observations). Back up `daas.db` before bulk writes (upsert.py does this).
- **Validate dynamic identifiers** against `^[A-Za-z_][A-Za-z0-9_]*$` before
  interpolating table/column names into SQL.

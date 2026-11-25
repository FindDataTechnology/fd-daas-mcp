---
name: fd-daas-fetch-data
description: Look up an entity in the daas registry, resolve which datasource covers it, and define an indicator over its source data — via daas_* tools + the registered `indicators` workflow manifest. Use when the user names a stock/company/country and wants coverage or an indicator ("查一下比亚迪的数据源", "find which datasource covers AAPL", "给这只股票算个 SMA"). No direct sqlite3, no dispatch.py, no run_indicator.py script.
---

# fd-daas-fetch-data (thin shell)

Drive the entity → datasource-coverage → define-indicator flow via `daas_*`
tools + the `indicators` workflow manifest. The `daas_*` tools own entity
resolution + coverage + indicator-rule creation (they proxy through
fd-open-data-mcp where needed); the `indicators` manifest owns the compute +
persist into `observations`. No direct `sqlite3` joins, no
`scripts/dispatch.py`, no `scripts/run_indicator.py` — those moved behind
the tools + manifest.

## When to use

- "查一下比亚迪在 daas 里的数据源 / find which datasource covers AAPL"
- "给这只股票算个 SMA 指标 / create an RSI indicator over TSLA close"
- "look up entity BYD and create an indicator"

Do NOT use for: a one-shot fetch into a scraw table
(`fd-daas-based-data-fetch` + `scripts/upsert.py`), a full research pipeline
(`fd-daas-research`), or building a dashboard (`fd-daas-dashboard-creator`).
This skill stops at indicator creation; it does not persist raw source data.

## Step 1 — Gather params: entity + coverage

Resolve the entity, then its datasource coverage (the modern replacement for
the `entities` JOIN `entity_datasource_links` → `sources`/`daas_functions`/
`daas_function_columns` chain + `dispatch.py --resolve`):

```python
# entity lookup (proxies through fd-open-data-mcp)
daas_search_entities(query="比亚迪", entity_type="stock")     # → entity_id
# OR natural key
daas_get_entity(entity_type="stock", code="002594")

# coverage: identifier per datasource + routing instructions + columns
daas_get_entity_coverage(entity_id=<id>)
```

`daas_get_entity_coverage` returns, per linked datasource: the
`identifier_in_source` to plug into fetches, the available sections (routing
instructions = how to get the data), and the column list. Surface the columns
and ask which series the user wants an indicator over. If the entity is not
found or has no covering datasource, tell the user and STOP — do not invent.

## Step 2 — Create the indicator rule

The modern replacement for `INSERT INTO indicator_rules`:

```python
daas_create_indicator(
    name="spy_ma5",
    datasource="yfinance",                 # must exist in sources (validated)
    source_table="scraw_spy_daily",        # any table in daas.db
    date_column="date",
    value_column="Close",
    op="sma",                             # see daas_list_indicator_ops()
    params={"window": 5},
)
```

Pick the op + params from the catalog: `daas_list_indicator_ops()`. The tool
validates `datasource` exists, `source_table` + columns exist, and `op` is in
the catalog before inserting. Indicator rules accept **any table in
`daas.db`**, not only `scraw_*` — don't reject a user's indicator over an
`observations` or `process_results` table.

## Step 3 — Run the `indicators` manifest

The manifest runs `daas_run_indicator` (the math + source-table read +
`observations` upsert all live inside that one tool) → computes the series
and persists it:

```python
daas_run_indicator(name="spy_ma5")
```

Returns the tool's own result (rows written, or an explicit error naming a
dangling source table). One call replaces the retired `indicators` manifest,
which was a single-step wrapper around exactly this tool. The run does a full
recompute (windowed ops need lookback) and upserts into `observations` keyed on
`(source, function_name, indicator, date)` — idempotent on re-run.

## Step 4 — Checkpoint handling

If `status` is `paused`, the manifest hit a `type: checkpoint` step. Inspect
the `resume_token` + the sentinel step at `sort_order=0`, decide, then:

```python
workflow_resume(run_id=<run_id>, approved=True)   # approved=False marks the run failed
```

`workflow_inspect(name="indicators")` shows the validated step plan without
executing — use it to preview before a run.

## Step 5 — Surface the result

Tell the user: "Indicator `<name>` created over `<source_table>.<value_column>`
(op=<op>, params=<params>); the `indicators` manifest wrote N observations."
For an **ad-hoc** compute (no persisted rule, no `observations` write), skip
Step 2+3 and call `daas_calculate_indicator(source_table=..., date_column=...,
value_column=..., op=..., params=...)` — it returns `{indicator, dates, values,
count}` and writes nothing.

## Hard rules

- **Entity + coverage go through `daas_*` tools.** No direct `sqlite3` joins
  against `entities`/`entity_datasource_links`/`sources`/`daas_functions`/
  `daas_function_columns`; the entity master migrated to fd-open-data-mcp and
  `daas_get_entity_coverage` returns the routing instructions `dispatch.py`
  used to provide.
- **Indicator-rule creation goes through `daas_create_indicator`.** No
  `INSERT INTO indicator_rules`. The tool validates datasource + table +
  columns + op before inserting.
- **Compute + persist goes through `daas_run_indicator`.** No
  `scripts/run_indicator.py`. For ad-hoc (no persist), use
  `daas_calculate_indicator`.
- **Validate dynamic identifiers** against `^[A-Za-z_][A-Za-z0-9_]*$` before
  interpolating table/column names — the `daas_*` tools enforce this; do it
  for any ad-hoc query too.

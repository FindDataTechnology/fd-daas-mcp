# daas-mcp

Multi-source data access + datasource management + managed pipeline collections.

## Pipeline collections (managed fetch + cron)

A *pipeline collection* groups fetch **items**; each item binds a source MCP
(`source_mcp` + `tool` + `arguments_json`) to a `scraw_<slug>` storage table and
a cron cadence. Adding an enabled item **automatically** runs a history backfill
and registers a `cron-mcp` schedule; removing/disabling an item unwires it.

The item model is the `data_job` shape (`source_mcp` / `tool` / `arguments`),
so items migrate 1:1 to `create_data_job` when `add-cron-mcp-data-fetch` lands.

### Worked example

```bash
# 1. Create a collection
uv run --directory mcp/daas-mcp python -c "
import asyncio, sys; sys.path.insert(0,'mcp/daas-mcp')
import pipeline_tools as P
print(asyncio.run(P.create_pipeline_collection('my-pipeline', 'demo')))
"

# 2. Add an item → auto backfill + auto cron schedule
#    (spawns akshare-mcp, calls stock_zh_a_hist, upserts into scraw_ashare_daily,
#     then creates a cron-mcp task + schedule)
uv run --directory mcp/daas-mcp python -c "
import asyncio, json, sys; sys.path.insert(0,'mcp/daas-mcp')
import pipeline_tools as P
r = asyncio.run(P.add_pipeline_item(
    collection_name='my-pipeline', name='ashare-daily',
    source_mcp='akshare-mcp', tool='call_akshare_function',
    arguments_json=json.dumps({'name':'stock_zh_a_hist','params_json':json.dumps({'symbol':'000001','period':'daily','start_date':'20240101','end_date':'20250703'})}),
    storage_table='scraw_ashare_daily', upsert_keys=['日期'],
    cron_expr='30 16 * * 1-5', timezone='Asia/Shanghai'))
print(r)
"

# 3. Inspect — scraw rows + the cron-mcp task/schedule
sqlite3 mcp/daas.db "SELECT COUNT(*) FROM scraw_ashare_daily;"
uv run --directory mcp/daas-mcp python server.py --fetch-item 1   # re-fetch (idempotent)

# 4. Pause / resume / re-wire
#    disable_pipeline_item → deletes the schedule (keeps the task row)
#    enable_pipeline_item  → backfills + recreates the schedule
#    sync_pipeline_cron     → re-applies wiring for all items (recovery path)

# 5. Remove the item (unwires cron; scraw_* left intact) or the whole collection
uv run --directory mcp/daas-mcp python -c "
import asyncio, sys; sys.path.insert(0,'mcp/daas-mcp')
import pipeline_tools as P
print(asyncio.run(P.delete_pipeline_collection('my-pipeline')))
"
```

### Seed the akshare example

`seed_pipeline_from_mapping.py` loads the `t.md` data needs from
`openspec/changes/akshare-cron-data-pipeline/datasource-mapping.md` into a
`pipeline_collection` named `akshare-t-md` (17 items):

```bash
uv run --directory mcp/daas-mcp python seed_pipeline_from_mapping.py --dry-run   # plan
uv run --directory mcp/daas-mcp python seed_pipeline_from_mapping.py             # seed all 17
uv run --directory mcp/daas-mcp python seed_pipeline_from_mapping.py --only ashare-daily
uv run --directory mcp/daas-mcp python seed_pipeline_from_mapping.py --unseed     # remove collection + cron rows
```

Re-run is idempotent (updates existing items, does not duplicate cron rows).
Schedules fire on the next `cron-mcp` start (`load_schedules()` loads enabled
rows into APScheduler).

### Self-check

```bash
uv run --directory mcp/daas-mcp python selfcheck_pipeline.py            # no-network (temp DB)
AKSHARE_LIVE=1 uv run --directory mcp/daas-mcp python selfcheck_pipeline.py   # + one live akshare backfill
```

### Notes

- `source_mcp` resolves via `.mcp.json` `mcpServers` OR a `mcp/<source_mcp>/server.py`
  convention dir; `mcp/models` is injected into `PYTHONPATH` on spawn.
- daas-mcp's own `fetch_data` is intentionally **not** used (its
  `daas-agent-harness` path is mis-resolved and the daas registry has no
  akshare functions) — the bridge calls the source MCPs directly via `fastmcp.Client`.
- Relationship to `akshare-cron-data-pipeline`: that change's standalone
  `register_cron.py` / `fetch_to_store.py` helpers are superseded for collections
  managed here; its `datasource-mapping.md` is the seed source.

---
name: fd-daas-indicators-creator
description: End-to-end create-indicators workflow - extend the fd-daas-fetch-data workflow with a persistent scraw_<slug> storage table and save the fetched data into it. Use this skill whenever the user wants to persist a daas data series to a table - phrases like "把沪深日行情存到一张表里", "save this series to a table", "create a scraw table for this indicator", "persist this data", or any series + "save to table / persist". If steps 1-3 (entity -> coverage -> indicator) are already done, this skill skips straight to steps 4-5 (table + save). There is NO cron scheduler in the stack - refresh is manual (re-run the fetch+upsert). Do NOT use this skill for a one-shot fetch with no persistence (use fd-daas-fetch-data + calculate) or to build a dashboard (use fd-daas-dashboard-creator). Uses sqlite3 + the fd-daas-based-data-fetch scripts - NO MCP tools.
---

# fd-daas-indicators-creator

Extend `fd-daas-fetch-data` with the table/save steps. Steps 1-3 (entity -> datasource -> indicator) come from `fd-daas-fetch-data`; this skill adds steps 4-5. **There is no cron** - the MCP scheduler is gone, so refresh is a manual re-run of the fetch+upsert.

## Mental model

Five steps total; steps 1-3 belong to `fd-daas-fetch-data`:

1. (fetch-data) Check the entities
2. (fetch-data) Find the related datasource
3. (fetch-data) Create indicators
4. **Create the table + save** - `scraw_<slug>` via `sqlite3` CREATE TABLE + `fd-daas-based-data-fetch/scripts/upsert.py`
5. **Verify** - confirm rows landed; document manual refresh

If steps 1-3 are already done (entity linked + indicator exists), skip to step 4.

## Step 0 - Handoff from fd-daas-fetch-data

- Is the entity linked to a datasource? (`sqlite3 daas.db "SELECT * FROM entity_datasource_links WHERE entity_id=<id>"`)
- Does the indicator already exist? (`sqlite3 daas.db "SELECT name FROM indicator_rules WHERE name='<name>'"`)

If both yes -> skip to step 4. If not -> invoke `fd-daas-fetch-data` first, then continue at step 4.

## Step 4 - Create the storage table + save the data

Goal: create `scraw_<slug>` and upsert the fetched rows into it. The fetch itself is two `fd-open-data-mcp` gateway calls (`resolve_entity` then `read`, see `fd-daas-based-data-fetch`), then persist via `upsert.py`.

1. Derive a `scraw_<slug>` name (kebab, `^[A-Za-z_][A-Za-z0-9_]*$`). Create the table on first upsert - `upsert.py` auto-`CREATE TABLE` + `ALTER TABLE ADD COLUMN` for new columns:
   ```bash
   uv run --with pandas python .claude/skills/fd-daas-based-data-fetch/scripts/upsert.py \
     --table scraw_byd_daily --keys date \
     --records '[{"date":"2025-01-02","Close":"101.0","Volume":"1100"}]'
   ```
   (In practice the records come from the `fd-daas-based-data-fetch` fetch snippet - pipe the fetched JSON into `--records`.)
2. `upsert.py` backs up `daas.db` to `.bak` before writing, sets `PRAGMA foreign_keys=ON`, and uses parameterized queries + `INSERT OR REPLACE` on the upsert keys.

**Duplicate table**: if `scraw_<slug>` already exists, offer to upsert fresh rows into it rather than recreating.

## Step 5 - Verify + document refresh

1. Confirm rows landed:
   ```bash
   sqlite3 daas.db "SELECT COUNT(*) FROM scraw_<slug>"
   sqlite3 daas.db "SELECT * FROM scraw_<slug> LIMIT 5"
   ```
2. Show the user: "Saved N rows to `scraw_<slug>`; sample: …".
3. **Document manual refresh** - there is no cron. Tell the user: "To refresh, re-run the fetch+upsert (the `fd-daas-based-data-fetch` flow) - it's idempotent on the upsert keys."

**Zero rows**: if the table is empty, the fetch failed. Report it, suggest checking the dispatch params + library connectivity, and STOP. Do not proceed.

## Gotchas

- **No cron / no scheduler.** The `add_pipeline_item` / `create_task` / `create_schedule` / `sync_pipeline_cron` tools are gone. Refresh is manual. Do not promise scheduled refresh.
- **`upsert_keys`** must be columns in the fetched data (e.g. `["date"]` for daily series). Mismatched keys cause duplicate rows.
- **`daas-mcp`'s `fetch_data` is gone** - the fetch goes through the `data-fetch` workflow manifest (see `fd-daas-based-data-fetch/SKILL.md`); raw persistence to `scraw_*` stays in `upsert.py`.
- This skill stops at table + save. To visualize, hand off to `fd-daas-dashboard-creator`.

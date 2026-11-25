---
name: fd-daas-research
description: Local entry to a daas research study. Point the caller at the research-workflow pack skill (which owns the five stages and their judgements), then handle parameter collection and result surfacing. Use when the user wants a full research pipeline (entity -> indicators -> report). No server-side workflow; no direct sqlite3 plan context.
---

# fd-daas-research (local entry)

This is the **local development entry**, not the execution flow. The research
stages and their judgements live in the `research-workflow` pack skill (pack
「数据 · 自助分析」). Read that skill's body and follow it; this file only adds
what is local-host specific.

## When to use

- "研究一下比亚迪，做指标和看板 / research TSLA - build indicators and a dashboard"
- "research SSE banks - set up indicators and a dashboard"
- "分析这只股票并做个可视化"

Do NOT use for: a one-shot fetch (`fd-daas-based-data-fetch`), only indicators
(`fd-daas-indicators-creator`), only a dashboard (`fd-daas-dashboard-creator`),
or only an entity collection (`fd-daas-entities-collection-creator`).

## Step 1 — Collect params

| param | type | how to discover |
|-------|------|-----------------|
| `name` | str (kebab/snake handle) | derive from entity/group + lens |
| `query` | str (natural-language demand) | user demand verbatim |
| `limit` | int | default 10; raise for broader concept search |

## Step 2 — Execute per `research-workflow`

Follow the pack skill's five stages: frame the question → search and select →
backfill data → build bundle and report → decide whether to promote outward.
The judgements (which hits to keep, whether there is enough data to continue)
are **yours to make at each step**; do not reduce the stages to a fixed script.

Tools are the `fd-open-data-mcp` reads (`ai_search`, `read`, `read_series`)
and the `fd-daas-mcp` research/collection tools. Call them directly.

## Step 3 — Surface the result

Tell the user the research name + the report path. On a repeat demand whose
research already exists, call `research_refresh(name="<name>")` then
`research_generate_report(name="<name>")` — do **not** re-create the bundle
(`research_create` returns an error on an existing name).

**Legacy note.** Older deployments may still carry a `research` row in their
local `workflows` table (`workflow_run("research", …)`). That path is retired:
it never wired the search results into the bundle and failed on re-run. If
`research_create` / `research_refresh` are unavailable, tell the user to
upgrade to pack 「数据 · 自助分析」 (which ships `research-workflow`) rather
than falling back to the old manifest.

**Run-notification** — emit at the end:

    ## Run Complete
    **Skill:** fd-daas-research
    **Status:** research study run + reported
    **Produced:** research `<name>` -> `researches/<name>.md`
    **Next:** re-run anytime to refresh; `research_refresh` to recompute indicators.

## Hard rules

- **The stages and judgements come from `research-workflow`.** This file owns
  only param gathering + result surfacing; it does not carry a second copy of
  the flow (two copies drift).
- **No empty bundle, no empty report.** A research with no entity or indicator
  member must not be reported as a study (see `research-workflow` 阶段 4).
- **No cron by default.** Re-run to refresh the report.

# fd-daas-mcp test suite

Offline pytest suite for the consolidated `fd-daas-mcp` server, registry, and CLI.
No network, no LLM, no live MCP transport.

## Run

From the repo root:

```bash
fd-daas-mcp/.venv/bin/python -m pytest fd-daas-mcp/tests -q
```

Or from `fd-daas-mcp/`:

```bash
.venv/bin/python -m pytest tests -q
```

## What it covers

- `test_registry.py` — `registry.build()` returns ≥155 tools across the 6 core
  groups; known cross-group collisions (`search_functions`, `run_rule`,
  `list_datasources`, `list_categories`, `get_function_detail`) are namespaced;
  leaf-module isolation (`registry_service`, `database`) resolves to distinct
  files; no APScheduler thread starts (cron suppression); cache idempotency.
- `test_registration_report.py` — the structured report (`registered` / `failed`
  / `skipped_optional`): every core group has ≥1 registered tool, no core-group
  failure, an absent optional group is `skipped_optional` (not `failed`), and
  `note_failed` surfaces server-side `app.tool` failures.
- `test_cli.py` — the Click CLI tree has a `<group>`/`<tool>` subcommand per
  registered tool; `key=value` parsing, `--json` output, malformed-arg / missing-
  required exit code 2, and async tools are awaited.
- `test_selfcheck.py` — `selfcheck.run_invariants()` passes under pytest (same
  invariants as the `__main__` selfcheck, no drift).
- `test_<group>_tools.py` (alerts/cron/composite/dashboard/gateway) - dedicated
  behavioral coverage for the five core groups that previously had only
  "registers ≥1 tool" coverage. Each calls the group's tool handlers directly
  against the throwaway DB and asserts return shape + DB side-effects (CRUD
  round-trips, validation rejections, side-effect writes), mirroring
  `test_rule_tools.py`/`test_research_tools.py`. Live side-effects are
  stubbed/suppressed per group (APScheduler for cron, notification dispatch for
  alerts, upstream subprocess for composite/gateway, `_DASH_DIR` redirected to a
  tmp dir for dashboard). See `daas-doc/mcp-test-suite.md` for the per-group
  stubbing strategy.

## Environment notes

- The suite runs via `.venv/bin/python`, **not** `uv run`. `uv run --directory
  fd-daas-mcp` re-resolves optional extras (`crewai`/`pageindex`) from PyPI and
  fails offline; the `.venv` already satisfies the core deps. To install pytest
  into the `.venv` offline: `uv pip install --python .venv/bin/python pytest --offline`.
- The `.venv` leaks micromamba `site-packages` whose `logfire` pytest plugin
  crashes on import (`opentelemetry._tail_sampling`). It is disabled via
  `addopts = "-p no:logfire -p no:pytest_logfire"` in `pyproject.toml` — a no-op
  in a clean venv (unregistered `-p no:X` is tolerated).
- `conftest.py` points `DAAS_DATABASE_URL` at a throwaway SQLite file so cron's
  import-time `init_db()` never touches the real `daas.db`.

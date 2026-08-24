# Quickstart

From zero to a working, queryable DAAS database in four commands.

## 1. Install

```bash
pip install fd-daas-mcp
# or, from source:
uv sync
```

Requires Python 3.12+.

## 2. Provision the database

```bash
fd-daas-mcp init
```

This creates `daas.db` (full schema) and seeds a small, dependency-free starter
catalog of `sources` (`akshare`, `yfinance`, `worldbank`, `edgar` - all
`enabled=False` until you supply credentials/install the source library).

**No environment variables required.** With `DAAS_DATABASE_URL` unset, the
database is created at a writable default:

- `./daas.db` if the current directory is writable and not inside the installed
  package, otherwise
- `~/.fd-daas-mcp/daas.db` (the directory is created on demand).

`init` prints the resolved database path. Re-running `init` is a no-op (it
verifies the schema and skips seeding when the catalog is non-empty).

Flags:

- `--db-url URL` - provision a specific path (one-shot; does not change the default).
- `--seed` / `--no-seed` - force seed / skip seed (default: seed iff `sources` is empty).
- `--json` - machine-readable summary.

## 3. Check health

```bash
fd-daas-mcp doctor
```

Read-only: prints the resolved DB path, whether the file exists, the core-table
schema state, row counts, and which optional extras are installed (e.g.
`sqlite_vec` for the `pdf` group). Exits `0` when healthy, non-zero with a
"run `fd-daas-mcp init`" pointer when not. Never writes to the database.

## 4. Point Claude Code at it

Add the server to `.mcp.json`:

```json
{
  "mcpServers": {
    "fd-daas-mcp": {
      "type": "stdio",
      "command": "fd-daas-mcp-server"
    }
  }
}
```

(From source, use `fd-daas-mcp/bin/fd-daas-mcp-server` as the `command`.)

Then list tools / browse the catalog:

```bash
fd-daas-mcp --help                 # every group + tool
fd-daas-mcp daas list_sources --json
```

## Optional: relocate the database

Set `DAAS_DATABASE_URL` (in a repo-root `.env` or your shell) to an absolute or
repo-relative `sqlite:///` URL and re-run `fd-daas-mcp init`. Absolute URLs and
`:memory:` pass through unchanged; relative paths are anchored to the repo root
when running from a checkout.

## What's NOT seeded

`init` seeds only the `sources` catalog rows (metadata, `enabled=False`). It
does **not**:

- register real `daas_function` param/column metadata,
- fetch any data,
- write to `scraw_*` or `observations`.

Register functions, fetch data, and compute indicators via the `fd-daas-mcp`
tools or the `fd-daas-based-data-fetch` skill.

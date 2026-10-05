# DAAS — Quick Start

A local data platform: one SQLite file behind an MCP server, with data fetch delegated to the `fd-open-data-mcp` upstream. Drive it from **Claude Code** (or any MCP-aware AI agent) in plain language.

## 1. Install (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/FindDataTechnology/fd-daas-mcp/master/install.sh | sh
```

This clones DAAS + the `fd-open-data-mcp` upstream, provisions venvs, inits `daas.db`, and localizes `.mcp.json` to your paths. When it prints `done: ~/code/DAAS`, **160+ MCP tools** and **13 product Claude Code skills** are ready — no further setup. (Exact tool count is runtime-defined — `selfcheck` is the source of truth; latest verified run: 161. `.claude/skills/` holds 32 directories in total, of which 13 are the product skills.)

Requirements: Python 3.10+ and `uv` (the script installs `uv` if missing). Override the install location with `DAAS_DEST` (default `~/code/DAAS`).

## 2. Open it in your AI agent

```bash
cd ~/code/DAAS
claude
```

Then just ask:

- *"fetch SPY's daily OHLC and compute a 5-day SMA"*
- *"build a dashboard of these indicators"*
- *"alert me when RSI crosses 70"*
- *"schedule this fetch nightly"*

The agent auto-loads the 160+ MCP tools from `.mcp.json` (runtime count is authoritative — `selfcheck`) and invokes the right skill when a task matches — `fd-daas-based-data-fetch` handles resolve → fetch → persist, `fd-daas-research` orchestrates a full study, etc.

## 3. Verify (or just ask the agent)

```bash
fd-daas-mcp/.venv/bin/fd-daas-mcp doctor            # path + schema + row counts
fd-daas-mcp/.venv/bin/python -m daas.fd_daas_mcp.selfcheck   # tool count (runtime is authoritative; latest: 161), failed=0
```

## Optional

- **Non-Claude-Code MCP client** (Cursor, Cline, …): the 160+ tools work in any MCP-aware client. The skills are a Claude-Code convenience layer — optional.
- **Source credentials** go in a repo-root `.env`: `HTTP_PROXY`, `EDGAR_IDENTITY`, `EDINET_API_KEY`, `LLM_*`, `ALERTS_FEISHU_WEBHOOK_URL`. See [README.md](README.md#environment-variables).
- **Manual install** (skip the curl script): see [README.md](README.md).

---

Full docs: [README.md](README.md) · [CLAUDE.md](CLAUDE.md) · [docs-site/](docs-site/) (`uv run mkdocs serve`)

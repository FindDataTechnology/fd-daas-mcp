# Changelog

All notable changes to the DAAS repository are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-06

First versioned release — establishes release discipline for the Facet MCP foundation phase (tool-surface contract work).

### Added

- **Tool-surface manifest**: the repo now carries a first-class manifest describing its MCP surface — per-group `commercial|internal` classification across the 9 tool groups, the 13-product-skill list, and a hosting-form declaration (shared vs per-deployment).
- **selfcheck assertions**: `selfcheck` now asserts manifest↔registry consistency and commercial-set closure, so the documented surface cannot silently drift from the registered one.
- **Version discipline**: this changelog, plus synchronized version fields — root `daas` and nested `fd-daas-mcp` `pyproject.toml`, both `uv.lock` files, and the package `__version__` — so builds and image tags are traceable to a version.

### Changed

- **README / QUICKSTART calibration**: overstated numbers corrected to a re-verifiable basis. Tool count is now stated as **160+ (runtime count is authoritative — run `selfcheck`; latest verified run: 161 tools, `failed=0`)** instead of a bare "161". Skills count corrected from "18 skills" to **13 product skills (32 skill directories in-repo: 13 product + 5 openspec-workflow + 14 local-dev)** — docs now advertise only the product surface. Aligned across `README.md`, `README.zh-CN.md`, and `QUICKSTART.md`; `install.sh` output carries no counts and needed no change.

### Historical note

Releases before 0.2.0 predate this changelog; no records were kept and none are reconstructed here.

[0.2.0]: https://github.com/FindDataTechnology/fd-daas-mcp/releases/tag/v0.2.0

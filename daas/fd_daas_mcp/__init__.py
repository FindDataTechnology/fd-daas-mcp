"""fd-daas-mcp - the single consolidated MCP server for the DAAS platform.

This is the only MCP in the project. Tool logic is hosted in-package at
``fd-daas-mcp/<group>-mcp/`` (moved, not rewritten, from the former ``mcp/``
source dirs). The :mod:`registry` imports each group's tool functions with
per-group ``sys.modules`` isolation and re-exposes them under a collision-free
``<group>_<tool>`` namespace on one FastMCP server (and one Click CLI).

Groups (core always-on unless the extra is absent): alerts, cron, composite,
daas, dashboard, gateway, workflow, pdf, research.
"""

__version__ = "0.1.0"

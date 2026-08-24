"""Message rendering for alerts-mcp.

Uses `string.Template.safe_substitute` — NOT `str.format` — because
`string.Template` cannot access object attributes, so a malicious template
cannot exfiltrate attributes. `safe_substitute` leaves unknown placeholders
intact rather than raising.
"""
from __future__ import annotations

from string import Template

__all__ = ["render_message", "TEMPLATE_VARS"]

#: Variables exposed to every message template.
TEMPLATE_VARS = ("latest", "prev", "date", "rule_name", "source", "indicator", "value", "pct_change")


def render_message(template: str, ctx: dict) -> str:
    """Render `template` with `string.Template.safe_substitute`.

    `ctx` may contain any of `TEMPLATE_VARS`; missing ones are left as
    `$<name>` in the output (safe_substitute semantics). Non-string values are
    stringified.
    """
    if not isinstance(template, str) or not template:
        template = "$rule_name: $indicator = $latest"
    mapping = {k: _stringify(v) for k, v in ctx.items() if k in TEMPLATE_VARS}
    return Template(template).safe_substitute(mapping)


def _stringify(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        # Trim trailing zeros for readable alerts (3.0 -> 3, 3.14 -> 3.14).
        return f"{v:g}"
    return str(v)

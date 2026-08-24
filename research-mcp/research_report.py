"""Markdown report assembly for a research bundle.

Pure functions: given the resolved bundle data, return a markdown string.
Kept separate from ``research_tools.py`` so the assembly is unit-testable
without a database. The tool passes a ``generated_at`` timestamp explicitly
(deterministic for tests); no ``datetime.now()`` is called here.
"""
from __future__ import annotations

from typing import Any, Optional


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
    return str(v)


def assemble_markdown(
    *,
    name: str,
    description: Optional[str],
    status: str,
    generated_at: str,
    entities: list[dict],
    indicators: list[dict],
    dashboard: Optional[dict],
    pipeline: list[dict],
    component_refs: dict,
) -> str:
    """Assemble the research markdown report.

    Args:
        entities: list of ``{code, name, ticker, exchange, entity_type}``.
        indicators: list of ``{indicator_name, op, params, rule_name,
            latest_date, latest_value}`` (latest_* may be None).
        dashboard: ``{name, file_url, intro}`` or None.
        pipeline: list of ``{name, cron_expr, enabled, last_status, last_run_at}``.
        component_refs: ``{rules, scraw_tables, indicators}``.
    """
    lines: list[str] = []
    lines.append(f"# Research: {name}")
    lines.append("")
    lines.append(f"- **Status:** {status}")
    lines.append(f"- **Generated:** {generated_at}")
    if description:
        lines.append("")
        lines.append(description.strip())
    lines.append("")

    # Entities
    lines.append("## Entities")
    if entities:
        lines.append("")
        lines.append("| Code | Name | Ticker | Exchange | Type |")
        lines.append("|---|---|---|---|---|")
        for e in entities:
            lines.append(
                f"| {_fmt(e.get('code'))} | {_fmt(e.get('name'))} | "
                f"{_fmt(e.get('ticker'))} | {_fmt(e.get('exchange'))} | "
                f"{_fmt(e.get('entity_type'))} |"
            )
    else:
        lines.append("")
        lines.append("_No entity collection attached._")
    lines.append("")

    # Indicators
    lines.append("## Indicators")
    if indicators:
        lines.append("")
        lines.append("| Indicator | Op | Latest Date | Latest Value | Rule |")
        lines.append("|---|---|---|---|---|")
        for ind in indicators:
            params = ind.get("params") or {}
            op = f"{ind.get('op', '')} {params}" if params else ind.get("op", "")
            lines.append(
                f"| {_fmt(ind.get('indicator_name'))} | {op.strip()} | "
                f"{_fmt(ind.get('latest_date'))} | {_fmt(ind.get('latest_value'))} | "
                f"{_fmt(ind.get('rule_name'))} |"
            )
    else:
        lines.append("")
        lines.append("_No indicator collection attached._")
    lines.append("")

    # Dashboard
    lines.append("## Dashboard")
    if dashboard:
        lines.append("")
        lines.append(f"**{dashboard.get('name', name)}**")
        if dashboard.get("intro"):
            lines.append("")
            lines.append(dashboard["intro"].strip())
        if dashboard.get("file_url"):
            lines.append("")
            lines.append(f"Open: {dashboard['file_url']}")
    else:
        lines.append("")
        lines.append("_No dashboard attached._")
    lines.append("")

    # Pipeline / Cron
    lines.append("## Pipeline / Cron")
    if pipeline:
        lines.append("")
        lines.append("| Item | Cron | Enabled | Last Status | Last Run |")
        lines.append("|---|---|---|---|---|")
        for p in pipeline:
            lines.append(
                f"| {_fmt(p.get('name'))} | {_fmt(p.get('cron_expr'))} | "
                f"{'yes' if p.get('enabled') else 'no'} | "
                f"{_fmt(p.get('last_status'))} | {_fmt(p.get('last_run_at'))} |"
            )
    else:
        lines.append("")
        lines.append("_No pipeline collection attached._")
    lines.append("")

    # Auxiliary refs
    rules = (component_refs or {}).get("rules") or []
    scraw = (component_refs or {}).get("scraw_tables") or []
    aux_ind = (component_refs or {}).get("indicators") or []
    if rules or scraw or aux_ind:
        lines.append("## Auxiliary References")
        if rules:
            lines.append("")
            lines.append(f"**Rules:** {', '.join(rules)}")
        if scraw:
            lines.append("")
            lines.append(f"**Source tables:** {', '.join(scraw)}")
        if aux_ind:
            lines.append("")
            lines.append(f"**Extra indicators:** {', '.join(aux_ind)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"

"""process tool wrappers for daas-mcp — the 19 relocated process-mcp tools.

These are the thin @app.tool wrappers that used to live in process-mcp/server.py,
relocated here as plain functions and registered daas-mcp-style via
`app.tool(fn)` in server.py. The implementation lives in:
  - process_tools.py   (LLM call + ad-hoc extract tools + model registry)
  - indicator_tools.py (math-op catalog + compute_series + ad-hoc calculate)
  - process_database.py (rule/result/indicator CRUD + run_indicator +
                          observations upsert + source-table discovery)

The ProcessDatabase singleton and its engine are unchanged; both daas-mcp's
`Database` and `ProcessDatabase` create engines on the same `mcp/daas.db`.
"""
from __future__ import annotations

import json
from typing import Optional

import process_tools as T
import indicator_tools as IT
from process_database import ProcessError as DbError
from process_database import get_db
from indicator_tools import IndicatorError as IndError


# ── model + source discovery ────────────────────────────────────


def list_models() -> dict:
    """Return configured models with a `vision` flag each. API keys are never returned."""
    return T.list_models()


def list_source_tables() -> dict:
    """Return scraped source-data tables (name LIKE 'scraw_%') with row counts + columns."""
    return {"tables": get_db().list_source_tables()}




# ── ad-hoc extraction tools ─────────────────────────────────────


def extract_text(
    text: str,
    schema: dict,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    max_chars: int = 12000,
) -> dict:
    """Extract structured records from (possibly long) text.

    Long text is chunked (no truncation): per-chunk extraction then a merge pass.
    """
    return T.extract_text(text, schema, prompt=prompt, model=model, max_chars=max_chars)


def extract_image(
    image: str,
    schema: dict,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Extract structured records from an image via a vision-capable model.

    `image` accepts a local file path, an http(s):// URL, or raw base64.
    """
    return T.extract_image(image, schema, prompt=prompt, model=model)


def extract_file(
    path: str,
    schema: dict,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    max_chars: int = 12000,
) -> dict:
    """Read a local .txt/.md/.pdf and extract structured records."""
    return T.extract_file(path, schema, prompt=prompt, model=model, max_chars=max_chars)


# ── indicator tools (deterministic math → observations) ────────


def list_indicator_ops() -> dict:
    """Return the fixed math-op catalog with each op's required params."""
    return IT.list_indicator_ops()


def create_indicator(
    name: str,
    datasource: str,
    source_table: str,
    date_column: str,
    value_column: str,
    op: str,
    params: Optional[dict] = None,
    function_name: Optional[str] = None,
    indicator_name: Optional[str] = None,
    score: Optional[float] = None,
    enabled: bool = True,
) -> dict:
    """Create an indicator rule bound to a datasource + source table.

    Args:
        name: unique indicator-rule name.
        datasource: daas `sources.name` (validated; soft reference).
        source_table: any table in daas.db holding the series.
        date_column: the column to order by (and key observations on).
        value_column: the numeric column the op is computed over.
        op: one of list_indicator_ops (e.g. 'sma', 'pct_change', 'rsi').
        params: op params, e.g. {"window": 5} for sma/rsi/zscore.
        function_name: label written to observations.function_name (defaults to source_table).
        indicator_name: output indicator label (defaults to the rule name).
        score: default priority/quality weight (float); None = inherit the
            datasource's `sources.score`. See set_indicator_score.
        enabled: whether run_indicator will process this rule.
    """
    try:
        return get_db().create_indicator(
            name=name,
            datasource=datasource,
            source_table=source_table,
            date_column=date_column,
            value_column=value_column,
            op=op,
            params=params,
            function_name=function_name,
            indicator_name=indicator_name,
            score=score,
            enabled=enabled,
        )
    except (DbError, IndError) as e:
        return {"error": str(e)}


def list_indicators() -> dict:
    """Return all indicator rules (each with `score` + `effective_default_score`)."""
    return {"indicators": get_db().list_indicators()}


def get_indicator(name: str) -> dict:
    """Return one indicator rule by name (with `score` + `effective_default_score`)."""
    row = get_db().get_indicator(name)
    return row if row is not None else {"error": f"indicator not found: {name}"}


def set_indicator_score(name: str, score: Optional[float]) -> dict:
    """Set the indicator's default `score` (float), or clear it (None → inherit
    the datasource's `sources.score`). Returns the updated indicator dict with
    `effective_default_score`."""
    try:
        return get_db().set_indicator_score(name, score)
    except DbError as e:
        return {"error": str(e)}


def update_indicator(
    name: str,
    datasource: Optional[str] = None,
    source_table: Optional[str] = None,
    date_column: Optional[str] = None,
    value_column: Optional[str] = None,
    op: Optional[str] = None,
    params: Optional[dict] = None,
    function_name: Optional[str] = None,
    indicator_name: Optional[str] = None,
    score: Optional[float] = None,
    clear_score: bool = False,
    enabled: Optional[bool] = None,
) -> dict:
    """Update an indicator rule's fields. Only provided fields change. The
    rule name cannot be renamed. Pass `params` to replace the op params.

    `score`: a float sets the indicator's default score. `clear_score=True`
    clears it back to NULL (inherit the datasource's `sources.score`).
    Mirrors `update_datasource(score=…, clear_score=True)`.
    """
    fields = {
        "datasource": datasource,
        "source_table": source_table,
        "date_column": date_column,
        "value_column": value_column,
        "op": op,
        "params_json": params,
        "function_name": function_name,
        "indicator_name": indicator_name,
        "score": score,
        "enabled": enabled,
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    if clear_score:
        # Thread a clear past the None-filter above. `set_indicator_score`
        # is the dedicated clearer; this is the convenience mirror of
        # update_datasource(clear_score=True).
        fields["score"] = None
    try:
        return get_db().update_indicator(name, **fields)
    except (DbError, IndError) as e:
        return {"error": str(e)}


def delete_indicator(name: str) -> dict:
    """Delete an indicator rule. observations rows it produced are NOT removed
    (soft reference) — they remain identifiable via their `metadata.rule_name`."""
    ok = get_db().delete_indicator(name)
    return {"deleted": name} if ok else {"error": f"indicator not found: {name}"}


def run_indicator(name: str) -> dict:
    """Full-recompute the indicator over its source table → observations.

    Reads the whole source table, computes the op, and upserts every
    (date, value) into the daas `observations` table keyed on
    (source=datasource, function_name, indicator=indicator_name, date).
    Idempotent on re-run via the observations unique constraint.
    """
    try:
        return get_db().run_indicator(name)
    except (DbError, IndError) as e:
        return {"error": str(e)}


def calculate_indicator(
    source_table: str,
    date_column: str,
    value_column: str,
    op: str,
    params: Optional[dict] = None,
    datasource: Optional[str] = None,
    function_name: Optional[str] = None,
    indicator_name: Optional[str] = None,
) -> dict:
    """Ad-hoc: compute an indicator over a source table without persisting.

    Validates table/columns/op/params, reads the series, computes, and returns
    {indicator, dates, values, count}. Writes nothing (no rule, no observations).
    """
    return IT.calculate(
        get_db(),
        source_table,
        date_column,
        value_column,
        op,
        params=params,
        datasource=datasource,
        function_name=function_name,
        indicator_name=indicator_name,
    )


# ── CLI branch helpers (cron-driven) ───────────────────────────




def cli_run_indicator(name: str) -> int:
    """Run an indicator in-process, print JSON summary, return exit code."""
    try:
        summary = get_db().run_indicator(name)
    except (DbError, IndError) as e:
        summary = {"error": str(e)}
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0 if "error" not in summary else 1

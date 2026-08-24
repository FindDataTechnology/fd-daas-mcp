"""process tools (daas-mcp) — deterministic math indicators over a datasource's columns.

Relocated from the former process-mcp. A fixed catalog of pandas one-liners
(pct_change, sma, ema, rsi, ...). No LLM, no `df.eval` of user input. Results
upsert into the daas `observations` table (via process_database.run_indicator);
the ad-hoc `calculate` returns the series without persisting.

Reuses process_database's identifier guard + source-table/column existence checks.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from process_database import ProcessError, validate_identifier


class IndicatorError(Exception):
    """Validation error for indicator ops/params, surfaced as a tool result."""


# ── op catalog ──────────────────────────────────────────────────
# Each fn: (df, value_column, params) -> pandas.Series (aligned with df rows).
# NaN fills warmup windows; callers skip NaN when persisting.
# ponytail: full recompute per run — windowed ops need lookback, an incremental
# last_rowid cursor would compute wrong leading values at the cursor boundary.
# Upgrade path: incremental cursor + a warmup window re-fetch preceding rows.


def _pct_change(df, vc, params):
    return df[vc].pct_change()


def _log_return(df, vc, params):
    prev = df[vc].shift(1)
    return np.log(df[vc] / prev)


def _diff(df, vc, params):
    return df[vc].diff()


def _sma(df, vc, params):
    return df[vc].rolling(int(params["window"])).mean()


def _ema(df, vc, params):
    return df[vc].ewm(span=int(params["span"]), adjust=False).mean()


def _rolling_std(df, vc, params):
    return df[vc].rolling(int(params["window"])).std()


def _rolling_min(df, vc, params):
    return df[vc].rolling(int(params["window"])).min()


def _rolling_max(df, vc, params):
    return df[vc].rolling(int(params["window"])).max()


def _rsi(df, vc, params):
    """Wilder's RSI over `window` periods."""
    window = int(params["window"])
    delta = df[vc].diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _zscore(df, vc, params):
    window = int(params["window"])
    mean = df[vc].rolling(window).mean()
    std = df[vc].rolling(window).std()
    return (df[vc] - mean) / std


def _ratio(df, vc, params):
    other = params["other_column"]
    if other not in df.columns:
        raise IndicatorError(f"other_column not found in source table: {other}")
    return df[vc] / df[other]


def _level(df, vc, params):
    return df[vc].astype(float)


_OPS: dict[str, dict] = {
    "pct_change": {"fn": _pct_change, "required_params": [], "description": "Period-over-period percent change."},
    "log_return": {"fn": _log_return, "required_params": [], "description": "Period-over-period log return ln(p_t/p_{t-1})."},
    "diff": {"fn": _diff, "required_params": [], "description": "Period-over-period difference."},
    "sma": {"fn": _sma, "required_params": ["window"], "description": "Simple moving average over `window` rows."},
    "ema": {"fn": _ema, "required_params": ["span"], "description": "Exponential moving average with `span`."},
    "rolling_std": {"fn": _rolling_std, "required_params": ["window"], "description": "Rolling standard deviation over `window`."},
    "rolling_min": {"fn": _rolling_min, "required_params": ["window"], "description": "Rolling minimum over `window`."},
    "rolling_max": {"fn": _rolling_max, "required_params": ["window"], "description": "Rolling maximum over `window`."},
    "rsi": {"fn": _rsi, "required_params": ["window"], "description": "Wilder's RSI over `window` periods (0-100)."},
    "zscore": {"fn": _zscore, "required_params": ["window"], "description": "Rolling z-score over `window`."},
    "ratio": {"fn": _ratio, "required_params": ["other_column"], "description": "Ratio of value_column / other_column."},
    "level": {"fn": _level, "required_params": [], "description": "Passthrough: the raw value as float."},
}


def list_indicator_ops() -> dict:
    """Return the fixed math-op catalog with each op's required params."""
    return {
        "ops": [
            {"name": n, "required_params": spec["required_params"], "description": spec["description"]}
            for n, spec in _OPS.items()
        ]
    }


def validate_op(op: str, params: Optional[dict]) -> None:
    """Raise IndicatorError if op is unknown or a required param is missing."""
    spec = _OPS.get(op)
    if spec is None:
        raise IndicatorError(f"unknown op: {op}")
    params = params or {}
    for p in spec["required_params"]:
        if p not in params:
            raise IndicatorError(f"op '{op}' requires param '{p}'")


def compute_series(df: pd.DataFrame, value_column: str, op: str, params: Optional[dict]) -> pd.Series:
    """Compute the op over df[value_column] → pandas Series aligned with df rows."""
    validate_op(op, params)
    if value_column not in df.columns:
        raise IndicatorError(f"value_column not found in source table: {value_column}")
    # Coerce to numeric; non-numeric rows become NaN (callers skip them).
    series = pd.to_numeric(df[value_column], errors="coerce")
    work = df.copy()
    work[value_column] = series
    return _OPS[op]["fn"](work, value_column, params or {})


def calculate(
    db,
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

    Validates table/columns/op/params via the shared guard, reads the series,
    computes, and returns {indicator, dates, values, count}. Writes nothing.
    """
    try:
        validate_op(op, params)
    except IndicatorError as e:
        return {"error": str(e)}
    try:
        rows = db.fetch_indicator_series(source_table, date_column, value_column)
    except ProcessError as e:
        return {"error": str(e)}
    if not rows:
        return {"indicator": indicator_name or op, "dates": [], "values": [], "count": 0}

    df = pd.DataFrame(rows, columns=[date_column, value_column])
    try:
        computed = compute_series(df, value_column, op, params)
    except IndicatorError as e:
        return {"error": str(e)}

    dates = [d for d in df[date_column].tolist()]
    values = [None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
              for v in computed.tolist()]
    return {
        "indicator": indicator_name or op,
        "datasource": datasource,
        "function_name": function_name,
        "dates": dates,
        "values": values,
        "count": len(dates),
    }

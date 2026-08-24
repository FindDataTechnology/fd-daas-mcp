"""Dependency-free starter catalog seed.

Inserts a fixed set of ``sources`` rows (``enabled=False``) into a fresh
database so an empty install is not a registry with zero sources. No source
data libraries are imported, no function param/column metadata is registered,
and no data is fetched. Idempotent via ``INSERT ... ON CONFLICT(name) DO
NOTHING`` (the ``sources.name`` UNIQUE constraint guarantees the conflict target).

Run via ``fd-daas-mcp init`` (the ``init`` command seeds on a fresh DB unless
``--no-seed`` is passed). Re-running against an already-seeded catalog is a
no-op.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# Fixed starter sources - widest-appeal, dep-free. All disabled until the user
# supplies credentials and/or installs the source library. Add a source here and
# the next ``init`` will pick it up (idempotent - existing rows untouched).
STARTER_SOURCES: list[dict[str, object]] = [
    {
        "name": "akshare",
        "label": "AKShare (A-share / CN market data)",
        "url": "https://akshare.akfamily.xyz/",
        "description": "Python library for A-share, HK, US, macro & alt data.",
        "enabled": False,
    },
    {
        "name": "yfinance",
        "label": "Yahoo Finance (yfinance)",
        "url": "https://github.com/ranaroussi/yfinance",
        "description": "Yahoo Finance market data (equities, ETFs, FX, crypto).",
        "enabled": False,
    },
    {
        "name": "worldbank",
        "label": "World Bank Open Data",
        "url": "https://data.worldbank.org/",
        "description": "World Bank indicators (macro, demographic, economic).",
        "enabled": False,
    },
    {
        "name": "edgar",
        "label": "SEC EDGAR",
        "url": "https://www.sec.gov/edgar.shtml",
        "description": "US SEC EDGAR filings (10-K, 8-K, etc.).",
        "enabled": False,
    },
]


def should_seed(session: Session, force: bool = False, no_seed: bool = False) -> bool:
    """Decide whether to seed.

    - ``no_seed=True`` -> never seed.
    - ``force=True`` (``--seed``) -> always seed (existing starter rows are
      left intact by the upsert).
    - default -> seed iff the ``sources`` table is empty.
    """
    if no_seed:
        return False
    if force:
        return True
    count = session.execute(text("SELECT COUNT(*) FROM sources")).scalar() or 0
    return count == 0


def seed_starter_catalog(session: Session) -> dict[str, int]:
    """Insert STARTER_SOURCES (idempotent, ``enabled=False``). Returns counts.

    Uses ``INSERT ... ON CONFLICT(name) DO NOTHING`` so existing sources (user-
    created or previously seeded) are never overwritten - their ``enabled``,
    ``label``, ``url`` etc. are left exactly as they were.
    """
    inserted = 0
    skipped = 0
    for src in STARTER_SOURCES:
        result = session.execute(
            text(
                "INSERT INTO sources (name, label, url, description, enabled) "
                "VALUES (:name, :label, :url, :description, :enabled) "
                "ON CONFLICT(name) DO NOTHING"
            ),
            src,
        )
        if result.rowcount and result.rowcount > 0:
            inserted += 1
        else:
            skipped += 1
    session.commit()
    return {"inserted": inserted, "skipped": skipped}

"""One-shot migration: re-key `entity_collection_items` and
`entity_collection_changes` from the integer `entity_id` FK (-> entities.id)
to natural-key `(entity_type, code)` strings.

Design D5 (entity-master migration): entity membership is moving off the
local `entities` table (which Section 3.7 drops). Both collection tables
currently key membership on `entity_id` and FK-cascade into `entities`; after
the migration they carry `(entity_type, code)` columns and a unique constraint
over them, so membership survives the drop of `entities`.

This script is ADDITIVE and NON-DESTRUCTIVE: it adds `entity_type`/`code`
columns (nullable), backfills them from the still-present `entities` table,
swaps the unique constraint to the natural key, and reports any rows it could
not backfill (orphaned entity_id - should be none under FK CASCADE). It does
NOT drop the `entity_id` column (that is 3.7's job, after verification) and
does NOT touch the `entity` ORM relationship. Idempotent: re-running is a
no-op once the columns + index exist.

Usage:
  uv run --directory fd-daas-mcp/daas-mcp python migrate_entity_collection_keys.py --dry-run
  uv run --directory fd-daas-mcp/daas-mcp python migrate_entity_collection_keys.py
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# make this dir + models importable when run via `uv run --directory`
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "models"))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "daas.db"

# Load the repo-root .env (DAAS_DATABASE_URL) so this targets the SAME live
# daas.db the consolidated server uses. Optional: falls back to the default
# path below if python-dotenv is unavailable or .env is absent.
try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(_REPO_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

_TABLES = {
    "entity_collection_items": "uq_entity_collection_item",
    "entity_collection_changes": "uq_entity_collection_change",
}


def _resolve_url(url: str) -> str:
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"sqlite:///{(_REPO_ROOT / path).resolve()}"
    return url


def _default_url() -> str:
    url = os.environ.get("DAAS_DATABASE_URL")
    if url:
        return _resolve_url(url)
    return f"sqlite:///{_DEFAULT_DB_PATH}"


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _add_natural_key_columns(engine, table: str, dry_run: bool) -> bool:
    """Add entity_type + code TEXT columns if absent. Return True if added (or
    would be added in dry-run)."""
    have = _columns(engine, table)
    added = False
    for col in ("entity_type", "code"):
        if col in have:
            continue
        if dry_run:
            print(f"  [dry-run] would ALTER TABLE {table} ADD COLUMN {col} TEXT")
        else:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} TEXT"))
            print(f"  ALTER TABLE {table} ADD COLUMN {col} TEXT")
        added = True
    return added


def _backfill(engine, table: str, dry_run: bool) -> int:
    """Backfill entity_type/code from entities.entity_id. Return count of
    unmappable rows (entity_id with no matching entities row)."""
    have = _columns(engine, table)
    if "entity_type" not in have or "code" not in have:
        # Columns not added yet (dry-run before add step): every row needs the
        # natural key. Report total row count without touching missing columns.
        with engine.connect() as conn:
            total = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if dry_run:
            print(f"  [dry-run] would backfill up to {total} row(s) in {table}")
            return 0
        # Non-dry-run but columns missing: add step should have run first.
        # Add them now defensively, then proceed.
        _add_natural_key_columns(engine, table, dry_run=False)
        have = _columns(engine, table)
    if dry_run:
        # Columns exist (re-run): report rows still missing the natural key.
        with engine.connect() as conn:
            unmapped = conn.execute(text(
                f"SELECT COUNT(*) FROM {table} WHERE entity_type IS NULL OR code IS NULL"
            )).scalar()
        print(f"  [dry-run] would backfill up to {unmapped} row(s) in {table}")
        return unmapped
    with engine.connect() as conn:
        unmapped_before = conn.execute(text(
            f"SELECT COUNT(*) FROM {table} WHERE entity_type IS NULL OR code IS NULL"
        )).scalar()
    with engine.begin() as conn:
        conn.execute(text(
            f"UPDATE {table} "
            f"SET entity_type = (SELECT e.entity_type FROM entities e "
            f"                     WHERE e.id = {table}.entity_id), "
            f"    code = (SELECT e.code FROM entities e "
            f"              WHERE e.id = {table}.entity_id) "
            f"WHERE entity_type IS NULL OR code IS NULL"
        ))
    with engine.connect() as conn:
        unmapped_after = conn.execute(text(
            f"SELECT COUNT(*) FROM {table} WHERE entity_type IS NULL OR code IS NULL"
        )).scalar()
    print(f"  backfilled {table}: {unmapped_before - unmapped_after} row(s) mapped, "
          f"{unmapped_after} orphaned (unmappable entity_id)")
    return unmapped_after


def _swap_unique_constraint(engine, table: str, index_name: str, extra_col: str | None,
                            dry_run: bool) -> None:
    """Ensure a natural-key unique index exists on (collection_id, entity_type,
    code[, extra_col]). Creates a NAMED index `index_name` (idempotent via IF NOT
    EXISTS). The legacy `entity_id`-based UNIQUE constraint backs an autoindex
    (`sqlite_autoindex_<table>_N`) which SQLite does not allow DROPping - it is
    left in place (harmless while `entity_id` stays populated) and removed in
    3.7's final table rebuild when `entity_id` is dropped."""
    cols = "collection_id, entity_type, code" + (f", {extra_col}" if extra_col else "")
    with engine.connect() as conn:
        idx = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=:n"
        ), {"n": index_name}).first()
    if idx is None:
        # named natural-key index not present yet - create it.
        if dry_run:
            print(f"  [dry-run] would CREATE UNIQUE INDEX {index_name} ON {table}({cols})")
        else:
            with engine.begin() as conn:
                conn.execute(text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table}({cols})"
                ))
            print(f"  CREATE UNIQUE INDEX {index_name} ON {table}({cols})")
    else:
        # named index exists - confirm it covers the natural key.
        with engine.connect() as conn:
            info = conn.execute(text(f"PRAGMA index_info({index_name})")).fetchall()
        existing_cols = {row[2] for row in info}
        target_cols = {"collection_id", "entity_type", "code"}
        if extra_col:
            target_cols.add(extra_col)
        if existing_cols == target_cols:
            print(f"  {index_name} already on natural key ({', '.join(sorted(target_cols))})")
        else:
            if dry_run:
                print(f"  [dry-run] would DROP INDEX {index_name} and recreate on ({cols})")
                return
            with engine.begin() as conn:
                conn.execute(text(f"DROP INDEX {index_name}"))
                conn.execute(text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table}({cols})"
                ))
            print(f"  DROP + CREATE UNIQUE INDEX {index_name} ON {table}({cols})")
    # Report the lingering legacy entity_id unique-constraint autoindex.
    if not dry_run:
        with engine.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list('{table}')")).fetchall()
        legacy = [r[1] for r in rows if r[2] == 1 and r[3] == "u"
                  and str(r[1]).startswith("sqlite_autoindex")]
        if legacy:
            print(f"  note: legacy entity_id unique constraint {legacy[0]} left in place "
                  f"(removed in 3.7)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="report plan, write nothing")
    ap.add_argument("--database-url", default=None, help="override DAAS_DATABASE_URL")
    args = ap.parse_args(argv)

    url = _resolve_url(args.database_url) if args.database_url else _default_url()
    engine = create_engine(url)

    insp = inspect(engine)
    for table in _TABLES:
        if not insp.has_table(table):
            print(f"table {table} not present in {url} - skipping")
            continue
    if not insp.has_table("entities"):
        print(f"WARNING: `entities` table not present in {url} - cannot backfill. "
              f"Run this migration BEFORE 3.7 drops `entities`.")
        if not args.dry_run:
            return 1

    print(f"DB: {url}")
    if args.dry_run:
        print("[dry-run] no changes written\n")

    orphans_total = 0
    for table, index_name in _TABLES.items():
        if not insp.has_table(table):
            continue
        print(f"\n== {table} ==")
        _add_natural_key_columns(engine, table, args.dry_run)
        orphans_total += _backfill(engine, table, args.dry_run)
        extra = "changed_at" if table == "entity_collection_changes" else None
        _swap_unique_constraint(engine, table, index_name, extra, args.dry_run)

    print(f"\norphans (unmappable entity_id) total: {orphans_total}")
    if args.dry_run:
        print("(dry-run; no changes written)")
    else:
        # Verify the new unique indexes exist on the natural key.
        for table, index_name in _TABLES.items():
            with engine.connect() as conn:
                info = conn.execute(text(f"PRAGMA index_info({index_name})")).fetchall()
            cols = [row[2] for row in info]
            print(f"  {index_name}: {cols}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

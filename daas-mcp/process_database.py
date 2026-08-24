"""Database helpers for the process tools (LLM extraction + indicators) in daas-mcp.

SQLAlchemy engine + session factory over the shared mcp/daas.db. CRUD for
process_rules / process_results, plus discovery of scraped source-data tables
(scraw_<slug>) and the SQL-injection guard on dynamic table/column names.

Mirrors cnreport_database.py. Relocated from the former process-mcp.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from models import (
    Base,
    DaasSource,
    IndicatorCollection,
    IndicatorCollectionChange,
    IndicatorCollectionItem,
    IndicatorRule,
    ProcessResult,
)

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "daas.db"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProcessError(Exception):
    """Validation/configuration error surfaced as a tool result, not a crash."""


def validate_identifier(name: str) -> None:
    """Raise ProcessError if `name` is not a safe SQL identifier."""
    if not name or not _IDENT_RE.match(name):
        raise ProcessError(f"invalid identifier: {name!r}")


# indicator_tools imports `from process_database import ProcessError,
# validate_identifier` (circular). The consolidated registry loads this module
# under a unique `_fdsrc_daas__process_database` name and evicts `daas-mcp/`
# from sys.path + sys.modules after build(), so the bare deferred
# `import indicator_tools` inside methods failed at call time (the same class
# of regression the rule_engine path-based load fixed). This loader resolves
# indicator_tools from its sibling file path and temporarily exposes THIS
# module under the plain `process_database` name so the circular import binds
# to the live instance. Cached so it runs once per module instance.
_IT_CACHE = None


# Cached indicator_tools module (loaded lazily; see _load_indicator_tools).
_IT_CACHE = None
# The live module object, captured at import time. The consolidated registry
# loads this module under a unique `_fdsrc_*` name and evicts `_fdsrc_*` from
# sys.modules after build(), so sys.modules[__name__] is None at call time -
# this lets _load_indicator_tools re-expose the module under the plain
# `process_database` name for indicator_tools' circular `from process_database
# import ProcessError, validate_identifier`.
_SELF_MODULE = sys.modules.get(__name__)


def _load_indicator_tools():
    global _IT_CACHE
    if _IT_CACHE is not None:
        return _IT_CACHE
    import importlib.util
    _prev = sys.modules.get("process_database")
    if _SELF_MODULE is not None:
        sys.modules["process_database"] = _SELF_MODULE
    try:
        spec = importlib.util.spec_from_file_location("indicator_tools", Path(__file__).resolve().parent / "indicator_tools.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["indicator_tools"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    finally:
        if _prev is not None:
            sys.modules["process_database"] = _prev
        elif _SELF_MODULE is not None:
            sys.modules.pop("process_database", None)
    _IT_CACHE = mod
    return mod


def _resolve_url(url: str) -> str:
    """Resolve a relative sqlite:/// path against the repo root. Pass through otherwise."""
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"sqlite:///{(_REPO_ROOT / path).resolve()}"
    return url


class ProcessDatabase:
    """Engine + session factory + CRUD for the process tables."""

    def __init__(self, database_url: Optional[str] = None):
        if database_url is None:
            _DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            database_url = os.environ.get(
                "DAAS_DATABASE_URL",
                f"sqlite:///{_DEFAULT_DB_PATH}",
            )
        # Resolve a relative sqlite:/// path against the repo root, so the DB
        # opens regardless of cwd (cron launches us via `uv run --directory
        # mcp/daas-mcp`, which sets cwd to mcp/daas-mcp and would break a
        # relative `mcp/daas.db`). Absolute paths and :memory: pass through.
        database_url = _resolve_url(database_url)
        self._database_url = database_url
        self._engine: Optional[Engine] = None
        self._session_factory: Optional[sessionmaker] = None

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self.init_db()
        assert self._engine is not None
        return self._engine

    def get_session(self) -> Session:
        if self._session_factory is None:
            self.init_db()
        assert self._session_factory is not None
        return self._session_factory()

    def init_db(self) -> None:
        self._engine = create_engine(
            self._database_url,
            echo=False,
            connect_args=(
                {"check_same_thread": False}
                if self._database_url.startswith("sqlite")
                else {}
            ),
        )
        # SQLite ignores ON DELETE CASCADE unless PRAGMA foreign_keys=ON.
        # ponytail: enable per-connection so delete_rule cascade fires.
        # WAL + busy_timeout fix "database is locked" when the in-process
        # workflow engine (Database singleton) holds a connection while
        # ProcessDatabase.upsert_observations writes the same sqlite file
        # (journal_mode=delete serializes writers and throws). WAL allows
        # concurrent readers + one writer; busy_timeout makes a writer wait
        # instead of failing immediately. Mirrors daas_database.py.
        if self._engine.dialect.name == "sqlite":

            @event.listens_for(self._engine, "connect")
            def _enable_fk(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=10000")
                cur.close()

        self._session_factory = sessionmaker(bind=self._engine)
        Base.metadata.create_all(self._engine)
        self._migrate_indicator_rules_score()
        logger.info("Process DB initialized: %s", self._database_url)

    def _migrate_indicator_rules_score(self) -> None:
        """Idempotent: add nullable `score` (REAL) to a pre-existing
        `indicator_rules` table. The default priority/quality weight for an
        indicator; NULL means inherit the datasource's `sources.score`.
        create_all adds the column on fresh DBs but won't ALTER an existing
        table. SQLite supports ADD COLUMN; guard on PRAGMA table_info so it
        runs exactly once. ponytail: additive only, no destructive migration.
        Mirrors `_migrate_sources_score` in daas_database.py.
        """
        insp = inspect(self._engine)
        if "indicator_rules" not in insp.get_table_names():
            return
        cols = [c["name"] for c in insp.get_columns("indicator_rules")]
        if "score" in cols:
            return
        with self._engine.begin() as conn:
            conn.execute(text("ALTER TABLE indicator_rules ADD COLUMN score REAL"))

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._session_factory = None

    # ── source-table discovery (scraw_<slug>) ───────────────────

    def table_exists(self, name: str) -> bool:
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
                {"n": name},
            ).fetchone()
            return row is not None

    def column_exists(self, table: str, column: str) -> bool:
        validate_identifier(table)
        with self.engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
        return column in cols

    def list_source_tables(self) -> list[dict]:
        """Return scraped source-data tables (name LIKE 'scraw_%') with row counts + columns.

        Excludes `scraw_configs` — that is the scraping *config* table, not scraped data.
        """
        out: list[dict] = []
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'scraw_%' AND name != 'scraw_configs' ORDER BY name")
            ).fetchall()
            for (tbl,) in rows:
                cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({tbl})")).fetchall()]
                try:
                    cnt = conn.execute(text(f'SELECT count(*) FROM "{tbl}"')).scalar()
                except Exception:
                    cnt = None
                out.append({"name": tbl, "row_count": cnt, "columns": cols})
        return out

    # ── ProcessResult ───────────────────────────────────────────

    def upsert_result(
        self,
        rule_id: int,
        source_table: str,
        source_rowid: int,
        extracted_json: Optional[dict],
        model: Optional[str],
    ) -> None:
        session = self.get_session()
        try:
            existing = (
                session.query(ProcessResult)
                .filter(
                    ProcessResult.rule_id == rule_id,
                    ProcessResult.source_table == source_table,
                    ProcessResult.source_rowid == source_rowid,
                )
                .first()
            )
            if existing is None:
                session.add(
                    ProcessResult(
                        rule_id=rule_id,
                        source_table=source_table,
                        source_rowid=source_rowid,
                        extracted_json=extracted_json,
                        model=model,
                    )
                )
            else:
                existing.extracted_json = extracted_json
                existing.model = model
            session.commit()
        finally:
            session.close()

    def count_results(self, rule_id: int) -> int:
        session = self.get_session()
        try:
            return (
                session.query(ProcessResult)
                .filter(ProcessResult.rule_id == rule_id)
                .count()
            )
        finally:
            session.close()

    def fetch_source_rows(
        self, source_table: str, text_column: str, cursor: int, batch: int
    ) -> list[tuple[int, str]]:
        """Return [(rowid, text)] for rows with rowid > cursor, limited to batch."""
        validate_identifier(source_table)
        validate_identifier(text_column)
        # identifiers validated above; safe to interpolate (cannot bind column/table)
        sql = text(
            f'SELECT rowid, "{text_column}" FROM "{source_table}" '
            f"WHERE rowid > :cursor ORDER BY rowid LIMIT :batch"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"cursor": cursor, "batch": batch}).fetchall()
        return [(r[0], r[1] or "") for r in rows]

    # ── IndicatorRule CRUD + run ────────────────────────────────
    # indicator_tools is imported lazily inside methods to avoid a circular
    # import (indicator_tools imports ProcessError/validate_identifier from
    # this module).

    def _datasource_exists(self, name: str) -> bool:
        """True if `name` is a daas sources.name (soft-reference check)."""
        session = self.get_session()
        try:
            return (
                session.query(DaasSource).filter(DaasSource.name == name).first() is not None
            )
        finally:
            session.close()

    def create_indicator(
        self,
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
        IT = _load_indicator_tools()
        validate_identifier(source_table)
        validate_identifier(date_column)
        validate_identifier(value_column)
        if not self._datasource_exists(datasource):
            raise ProcessError(f"datasource not found: {datasource}")
        if not self.table_exists(source_table):
            raise ProcessError(f"source table not found: {source_table}")
        if not self.column_exists(source_table, date_column):
            raise ProcessError(f"date_column not found in source table: {date_column}")
        if not self.column_exists(source_table, value_column):
            raise ProcessError(f"value_column not found in source table: {value_column}")
        try:
            IT.validate_op(op, params)
        except IT.IndicatorError as e:
            raise ProcessError(str(e))

        session = self.get_session()
        try:
            existing = (
                session.query(IndicatorRule).filter(IndicatorRule.name == name).first()
            )
            if existing is not None:
                raise ProcessError(f"indicator name already exists: {name}")
            row = IndicatorRule(
                name=name,
                datasource=datasource,
                function_name=function_name or source_table,
                source_table=source_table,
                date_column=date_column,
                value_column=value_column,
                op=op,
                params_json=params,
                indicator_name=indicator_name or name,
                score=score,
                enabled=enabled,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.to_dict()
        finally:
            session.close()

    def _datasource_scores(self, session: Session) -> dict:
        """Map every daas sources.name → its default `score` (NULL when unset)."""
        return {
            name: score for (name, score) in session.query(DaasSource.name, DaasSource.score).all()
        }

    @staticmethod
    def _augment_indicator_score(d: dict, ds_scores: dict) -> dict:
        """Add `datasource_default_score` and `effective_default_score` to an
        indicator dict. effective = indicator.score if set, else the datasource's
        sources.score, else NULL (3-level chain level 1→2)."""
        ds_score = ds_scores.get(d.get("datasource"))
        d["datasource_default_score"] = ds_score
        d["effective_default_score"] = d["score"] if d.get("score") is not None else ds_score
        return d

    def list_indicators(self) -> list[dict]:
        session = self.get_session()
        try:
            ds_scores = self._datasource_scores(session)
            return [
                self._augment_indicator_score(r.to_dict(), ds_scores)
                for r in session.query(IndicatorRule).order_by(IndicatorRule.name).all()
            ]
        finally:
            session.close()

    def get_indicator(self, name: str) -> Optional[dict]:
        session = self.get_session()
        try:
            row = session.query(IndicatorRule).filter(IndicatorRule.name == name).first()
            if row is None:
                return None
            ds_scores = self._datasource_scores(session)
            return self._augment_indicator_score(row.to_dict(), ds_scores)
        finally:
            session.close()

    def get_indicator_row(self, name: str) -> Optional[IndicatorRule]:
        session = self.get_session()
        try:
            return session.query(IndicatorRule).filter(IndicatorRule.name == name).first()
        finally:
            session.close()

    def set_indicator_score(self, name: str, score: Optional[float]) -> dict:
        """Set the indicator's default `score` (float) or clear it (None →
        inherit the datasource's `sources.score`). Returns the updated
        indicator dict with `effective_default_score`."""
        session = self.get_session()
        try:
            row = session.query(IndicatorRule).filter(IndicatorRule.name == name).first()
            if row is None:
                raise ProcessError(f"indicator not found: {name}")
            if score is not None and not isinstance(score, (int, float)):
                raise ProcessError("score must be a number or null")
            row.score = float(score) if score is not None else None
            session.commit()
            session.refresh(row)
            ds_scores = self._datasource_scores(session)
            return self._augment_indicator_score(row.to_dict(), ds_scores)
        finally:
            session.close()

    def update_indicator(self, name: str, **fields) -> dict:
        IT = _load_indicator_tools()
        session = self.get_session()
        try:
            row = session.query(IndicatorRule).filter(IndicatorRule.name == name).first()
            if row is None:
                raise ProcessError(f"indicator not found: {name}")
            if "datasource" in fields and fields["datasource"] is not None:
                if not self._datasource_exists(fields["datasource"]):
                    raise ProcessError(f"datasource not found: {fields['datasource']}")
            if "source_table" in fields and fields["source_table"] is not None:
                validate_identifier(fields["source_table"])
                if not self.table_exists(fields["source_table"]):
                    raise ProcessError(f"source table not found: {fields['source_table']}")
            tbl = fields.get("source_table") or row.source_table
            if "date_column" in fields and fields["date_column"] is not None:
                validate_identifier(fields["date_column"])
                if not self.column_exists(tbl, fields["date_column"]):
                    raise ProcessError(
                        f"date_column not found in source table: {fields['date_column']}"
                    )
            if "value_column" in fields and fields["value_column"] is not None:
                validate_identifier(fields["value_column"])
                if not self.column_exists(tbl, fields["value_column"]):
                    raise ProcessError(
                        f"value_column not found in source table: {fields['value_column']}"
                    )
            if "op" in fields or "params_json" in fields:
                new_op = fields.get("op", row.op)
                new_params = fields.get("params_json", row.params_json)
                try:
                    IT.validate_op(new_op, new_params)
                except IT.IndicatorError as e:
                    raise ProcessError(str(e))
            # `score` is handled explicitly so None clears it (the generic loop
            # below skips None). `score` may be None (clear) or a float.
            if "score" in fields:
                new_score = fields["score"]
                if new_score is not None and not isinstance(new_score, (int, float)):
                    raise ProcessError("score must be a number or null")
                row.score = float(new_score) if new_score is not None else None
            for k, v in fields.items():
                if k in ("name", "score") or v is None:
                    continue
                if hasattr(row, k):
                    setattr(row, k, v)
            session.commit()
            session.refresh(row)
            ds_scores = self._datasource_scores(session)
            return self._augment_indicator_score(row.to_dict(), ds_scores)
        finally:
            session.close()

    def delete_indicator(self, name: str) -> bool:
        session = self.get_session()
        try:
            row = session.query(IndicatorRule).filter(IndicatorRule.name == name).first()
            if row is None:
                return False
            # No FK to observations (soft ref): observations rows survive, still
            # identifiable via their metadata.rule_name.
            session.delete(row)
            session.commit()
            return True
        finally:
            session.close()

    def fetch_indicator_series(
        self, source_table: str, date_column: str, value_column: str
    ) -> list[tuple]:
        """Return [(date, value)] for the full source table, ordered by date_column.

        Identifiers are guarded + existence-checked before interpolation
        (they cannot be bind parameters).
        """
        validate_identifier(source_table)
        validate_identifier(date_column)
        validate_identifier(value_column)
        if not self.table_exists(source_table):
            raise ProcessError(f"source table not found: {source_table}")
        if not self.column_exists(source_table, date_column):
            raise ProcessError(f"date_column not found in source table: {date_column}")
        if not self.column_exists(source_table, value_column):
            raise ProcessError(f"value_column not found in source table: {value_column}")
        sql = text(
            f'SELECT "{date_column}", "{value_column}" FROM "{source_table}" '
            f'ORDER BY "{date_column}"'
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [(r[0], r[1]) for r in rows]

    def upsert_observations(
        self,
        source: str,
        function_name: str,
        indicator: str,
        rows: list[tuple],
        metadata: dict,
    ) -> int:
        """Bulk upsert (date, value) pairs into observations on
        (source, function_name, indicator, date). value is stored as str
        (observations.value is String(64), matching existing rows). NaN rows
        are skipped by the caller. Returns the number of rows written.

        Raw INSERT ... ON CONFLICT (executemany) — avoids the ORM bulk-insert
        path which trips on the `metadata` column rename.
        """
        import json

        if not rows:
            return 0
        meta_json = json.dumps(metadata, ensure_ascii=False)
        sql = text(
            "INSERT INTO observations "
            "(source, function_name, indicator, date, value, metadata) "
            "VALUES (:source, :function_name, :indicator, :date, :value, :metadata) "
            "ON CONFLICT(source, function_name, indicator, date) DO UPDATE SET "
            "value=excluded.value, metadata=excluded.metadata"
        )
        records = [
            {
                "source": source,
                "function_name": function_name,
                "indicator": indicator,
                "date": str(d),
                "value": str(v),
                "metadata": meta_json,
            }
            for d, v in rows
        ]
        with self.engine.begin() as conn:
            conn.execute(sql, records)
        return len(records)

    def run_indicator(self, name: str) -> dict:
        """Full-recompute the indicator over its source table → observations.

        Idempotent via the observations unique constraint. No incremental
        cursor (windowed ops need lookback — see indicator_tools ceiling note).
        """
        IT = _load_indicator_tools()
        import numpy as np
        import pandas as pd

        row = self.get_indicator_row(name)
        if row is None:
            return {"error": f"indicator not found: {name}"}
        if not row.enabled:
            return {"error": f"indicator disabled: {name}"}

        try:
            rows = self.fetch_indicator_series(
                row.source_table, row.date_column, row.value_column
            )
        except ProcessError as e:
            return {"error": str(e)}
        if not rows:
            return {"rule": name, "rows_written": 0, "up_to_date": True}

        df = pd.DataFrame(rows, columns=[row.date_column, row.value_column])
        try:
            computed = IT.compute_series(
                df, row.value_column, row.op, row.params_json
            )
        except IT.IndicatorError as e:
            return {"error": str(e)}

        metadata = {
            "rule_name": row.name,
            "op": row.op,
            "params": row.params_json or {},
            "value_column": row.value_column,
        }
        out_rows: list[tuple] = []
        for d, v in zip(df[row.date_column].tolist(), computed.tolist()):
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if np.isnan(fv):
                continue
            out_rows.append((d, fv))

        written = self.upsert_observations(
            row.datasource, row.function_name, row.indicator_name, out_rows, metadata
        )
        return {"rule": name, "rows_written": written, "up_to_date": True}


_process_db: Optional[ProcessDatabase] = None


def get_db(database_url: Optional[str] = None) -> ProcessDatabase:
    global _process_db
    if _process_db is None:
        _process_db = ProcessDatabase(database_url)
        _process_db.init_db()
    return _process_db


def reset_db() -> None:
    global _process_db
    if _process_db is not None:
        _process_db.dispose()
    _process_db = None

"""Database helpers for alerts-mcp.

SQLAlchemy engine + session factory over the shared mcp/daas.db. CRUD for
alert_rules / alert_events, alert-scoped series discovery, the SQL-injection
guard on dynamic table/column names, and the firing recorder (event row +
rule-state update in one transaction).

Mirrors process_database.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from models import AlertEvent, AlertRule, Base

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "daas.db"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VALID_FIRE_MODES = {"every_match", "on_change"}


class AlertError(Exception):
    """Validation/configuration error surfaced as a tool result, not a crash."""


def validate_identifier(name: str) -> None:
    """Raise AlertError if `name` is not a safe SQL identifier."""
    if not name or not _IDENT_RE.match(name):
        raise AlertError(f"invalid identifier: {name!r}")


def _resolve_url(url: str) -> str:
    """Resolve a relative sqlite:/// path against the repo root. Pass through otherwise."""
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"sqlite:///{(_REPO_ROOT / path).resolve()}"
    return url


class AlertDatabase:
    """Engine + session factory + CRUD for the alert tables."""

    def __init__(self, database_url: Optional[str] = None):
        if database_url is None:
            _DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            database_url = os.environ.get(
                "DAAS_DATABASE_URL",
                f"sqlite:///{_DEFAULT_DB_PATH}",
            )
        # Resolve a relative sqlite:/// path against the repo root, so the DB
        # opens regardless of cwd (cron launches us via `uv run --directory
        # mcp/alerts-mcp`, which sets cwd to mcp/alerts-mcp and would break a
        # relative `mcp/daas.db`).
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
        if self._engine.dialect.name == "sqlite":

            @event.listens_for(self._engine, "connect")
            def _enable_fk(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        self._session_factory = sessionmaker(bind=self._engine)
        Base.metadata.create_all(self._engine)
        logger.info("Alert DB initialized: %s", self._database_url)

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._session_factory = None

    # ── identifier-guarded helpers ───────────────────────────────

    def table_exists(self, name: str) -> bool:
        validate_identifier(name)
        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
                {"n": name},
            ).fetchone()
            return row is not None

    def column_exists(self, table: str, column: str) -> bool:
        validate_identifier(table)
        validate_identifier(column)
        with self.engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()]
        return column in cols

    def _validate_series_filter(self, series_filter: dict) -> dict:
        """Validate that every key in series_filter is a safe identifier. Return it as-is."""
        if series_filter is None:
            return {}
        if not isinstance(series_filter, dict):
            raise AlertError("series_filter must be a JSON object")
        for k in series_filter:
            validate_identifier(str(k))
        return series_filter

    # ── series discovery (alert-scoped) ──────────────────────────

    def list_series(self) -> list[dict]:
        """Distinct (source, function_name, indicator) from observations + each
        scraw_* table (excluding scraw_configs), with row count + latest date."""
        out: list[dict] = []
        with self.engine.connect() as conn:
            # observations series
            if self._table_exists_raw(conn, "observations"):
                rows = conn.execute(text(
                    "SELECT source, function_name, indicator, count(*) AS n, "
                    "max(date) AS latest FROM observations "
                    "GROUP BY source, function_name, indicator ORDER BY source, indicator"
                )).fetchall()
                for r in rows:
                    out.append({
                        "table": "observations",
                        "source": r[0],
                        "function_name": r[1],
                        "indicator": r[2],
                        "row_count": r[3],
                        "latest_date": r[4],
                    })
            # scraw_* tables: report table name + row count + latest date (if a
            # `date`-like column exists, best-effort)
            scraw = conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'scraw_%' "
                "AND name != 'scraw_configs' ORDER BY name"
            )).fetchall()
            for (tbl,) in scraw:
                cols = [c[1] for c in conn.execute(text(f"PRAGMA table_info({tbl})")).fetchall()]
                try:
                    cnt = conn.execute(text(f'SELECT count(*) FROM "{tbl}"')).scalar()
                except Exception:
                    cnt = None
                out.append({"table": tbl, "columns": cols, "row_count": cnt})
        return out

    def _table_exists_raw(self, conn, name: str) -> bool:
        row = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
            {"n": name},
        ).fetchone()
        return row is not None

    def get_series_latest(
        self,
        source_table: str,
        series_filter_json: Optional[dict],
        date_column: str,
        value_column: str,
        limit: int = 10,
    ) -> list[dict]:
        """Return the latest `limit` rows of a series ordered by date_column desc.

        Identifiers are validated; filter values are bind params.
        """
        validate_identifier(source_table)
        validate_identifier(date_column)
        validate_identifier(value_column)
        if not self.table_exists(source_table):
            raise AlertError(f"source table not found: {source_table}")
        if not self.column_exists(source_table, date_column):
            raise AlertError(f"date_column not found: {date_column}")
        if not self.column_exists(source_table, value_column):
            raise AlertError(f"value_column not found: {value_column}")
        series_filter = self._validate_series_filter(series_filter_json or {})
        where = ""
        params: dict = {"limit": int(limit)}
        if series_filter:
            clauses = []
            for i, (k, v) in enumerate(series_filter.items()):
                clauses.append(f'"{k}" = :f{i}')
                params[f"f{i}"] = v
            where = "WHERE " + " AND ".join(clauses)
        sql = text(
            f'SELECT "{date_column}", "{value_column}" FROM "{source_table}" '
            f"{where} ORDER BY \"{date_column}\" DESC LIMIT :limit"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"date": str(r[0]), "value": r[1]} for r in rows]

    # ── AlertRule CRUD ───────────────────────────────────────────

    def create_rule(
        self,
        name: str,
        condition: str,
        channels: list | dict,
        source_table: str = "observations",
        series_filter_json: Optional[dict] = None,
        date_column: str = "date",
        value_column: str = "value",
        fire_mode: str = "every_match",
        cooldown_seconds: int = 300,
        message_template: str = "$rule_name: $indicator = $latest",
        enabled: bool = True,
    ) -> dict:
        validate_identifier(source_table)
        validate_identifier(date_column)
        validate_identifier(value_column)
        if not name or not isinstance(name, str):
            raise AlertError("rule name is required")
        if not condition or not isinstance(condition, str):
            raise AlertError("condition is required")
        if fire_mode not in _VALID_FIRE_MODES:
            raise AlertError(f"fire_mode must be one of {sorted(_VALID_FIRE_MODES)}")
        if not channels:
            raise AlertError("channels is required (at least one)")
        if not self.table_exists(source_table):
            raise AlertError(f"source table not found: {source_table}")
        if not self.column_exists(source_table, date_column):
            raise AlertError(f"date_column not found: {date_column}")
        if not self.column_exists(source_table, value_column):
            raise AlertError(f"value_column not found: {value_column}")
        series_filter = self._validate_series_filter(series_filter_json)
        session = self.get_session()
        try:
            existing = session.query(AlertRule).filter(AlertRule.name == name).first()
            if existing is not None:
                raise AlertError(f"rule name already exists: {name}")
            row = AlertRule(
                name=name,
                enabled=enabled,
                source_table=source_table,
                series_filter_json=series_filter,
                date_column=date_column,
                value_column=value_column,
                condition=condition,
                fire_mode=fire_mode,
                cooldown_seconds=int(cooldown_seconds),
                channels_json=channels,
                message_template=message_template,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.to_dict()
        finally:
            session.close()

    def list_rules(self) -> list[dict]:
        session = self.get_session()
        try:
            return [r.to_dict() for r in session.query(AlertRule).order_by(AlertRule.name).all()]
        finally:
            session.close()

    def get_rule(self, name: str) -> Optional[dict]:
        session = self.get_session()
        try:
            row = session.query(AlertRule).filter(AlertRule.name == name).first()
            return row.to_dict() if row else None
        finally:
            session.close()

    def get_rule_row(self, name: str) -> Optional[AlertRule]:
        session = self.get_session()
        try:
            return session.query(AlertRule).filter(AlertRule.name == name).first()
        finally:
            session.close()

    def update_rule(self, name: str, **fields) -> dict:
        session = self.get_session()
        try:
            row = session.query(AlertRule).filter(AlertRule.name == name).first()
            if row is None:
                raise AlertError(f"rule not found: {name}")
            # Validate new identifiers if provided.
            if fields.get("source_table") is not None:
                validate_identifier(fields["source_table"])
                if not self.table_exists(fields["source_table"]):
                    raise AlertError(f"source table not found: {fields['source_table']}")
            tbl = fields.get("source_table") or row.source_table
            if fields.get("date_column") is not None:
                validate_identifier(fields["date_column"])
                if not self.column_exists(tbl, fields["date_column"]):
                    raise AlertError(f"date_column not found: {fields['date_column']}")
            if fields.get("value_column") is not None:
                validate_identifier(fields["value_column"])
                if not self.column_exists(tbl, fields["value_column"]):
                    raise AlertError(f"value_column not found: {fields['value_column']}")
            if fields.get("series_filter_json") is not None:
                fields["series_filter_json"] = self._validate_series_filter(fields["series_filter_json"])
            if fields.get("fire_mode") is not None and fields["fire_mode"] not in _VALID_FIRE_MODES:
                raise AlertError(f"fire_mode must be one of {sorted(_VALID_FIRE_MODES)}")
            for k, v in fields.items():
                if v is None or k in ("name", "id"):
                    continue
                if hasattr(row, k):
                    setattr(row, k, v)
            session.commit()
            session.refresh(row)
            return row.to_dict()
        finally:
            session.close()

    def delete_rule(self, name: str) -> bool:
        session = self.get_session()
        try:
            row = session.query(AlertRule).filter(AlertRule.name == name).first()
            if row is None:
                return False
            session.delete(row)  # FK CASCADE removes alert_events rows
            session.commit()
            return True
        finally:
            session.close()

    # ── firing: event row + rule-state update in one transaction ──

    def record_firing(
        self,
        rule_id: int,
        value_json: dict,
        message_rendered: str,
        channels_results_json: list,
        new_state: bool,
        latest_value,
    ) -> None:
        """Insert an alert_events row and update rule state in one transaction."""
        session = self.get_session()
        try:
            session.add(
                AlertEvent(
                    rule_id=rule_id,
                    value_json=value_json,
                    message_rendered=message_rendered,
                    channels_results_json=channels_results_json,
                )
            )
            row = session.get(AlertRule, rule_id)
            if row is not None:
                row.last_fired_at = datetime.now(timezone.utc)
                row.last_state = new_state
                if latest_value is not None:
                    row.last_value = str(latest_value)
            session.commit()
        finally:
            session.close()

    def update_state(self, rule_id: int, new_state: bool, latest_value) -> None:
        """Update only the rule's last_state/last_value (no event row) — used when
        a rule evaluates false (so on_change can detect the next transition)."""
        session = self.get_session()
        try:
            row = session.get(AlertRule, rule_id)
            if row is not None:
                row.last_state = new_state
                if latest_value is not None:
                    row.last_value = str(latest_value)
                session.commit()
        finally:
            session.close()

    def list_events(self, rule_name: Optional[str] = None, limit: int = 50) -> list[dict]:
        session = self.get_session()
        try:
            q = session.query(AlertEvent).join(AlertRule, AlertEvent.rule_id == AlertRule.id)
            if rule_name:
                q = q.filter(AlertRule.name == rule_name)
            rows = q.order_by(AlertEvent.fired_at.desc()).limit(int(limit)).all()
            return [r.to_dict() for r in rows]
        finally:
            session.close()


_alert_db: Optional[AlertDatabase] = None


def get_db(database_url: Optional[str] = None) -> AlertDatabase:
    global _alert_db
    if _alert_db is None:
        _alert_db = AlertDatabase(database_url)
        _alert_db.init_db()
    return _alert_db


def reset_db() -> None:
    global _alert_db
    if _alert_db is not None:
        _alert_db.dispose()
    _alert_db = None


# Re-export json for callers that build value_json/channel_results payloads.
__all__ = ["AlertError", "AlertDatabase", "get_db", "reset_db", "validate_identifier", "json"]

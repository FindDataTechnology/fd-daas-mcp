"""Dashboard registry CRUD + index/daas.md regeneration for dashboard-mcp.

Backs the 6 dashboard tools (register/list/get/search/update/delete) over the
`dashboards` table (defined in mcp/models/models.py). The DB is the single
source of truth for standalone-HTML dashboard metadata; `index.html` and
`daas.md` are regenerated from it on every write so they can never drift.

Self-contained DB plumbing (own engine + repo-root URL resolution) — mirrors
the per-MCP `*_database.py` pattern (alert_database.py, daas_database.py) so
this module imports only `models` and never crosses back into server.py.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Dashboard

# mcp/dashboard-mcp/ -> repo root (_REPO_ROOT); root .env holds DAAS_DATABASE_URL.
_REPO_ROOT = Path(__file__).resolve().parents[2]  # repo root
_DASH_DIR = _REPO_ROOT / "mcp" / "dashboard-mcp" / "dashboards"

load_dotenv(_REPO_ROOT / ".env")  # root .env (DAAS_DATABASE_URL) first
load_dotenv(Path(__file__).parent / ".env", override=True)

_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _resolve_url(url: str) -> str:
    """Resolve a relative sqlite:/// path against the repo root. Pass through
    otherwise (absolute, :memory:, non-sqlite). Mirrors server.py."""
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"sqlite:///{(_REPO_ROOT / path).resolve()}"
    return url


def _get_db_url() -> str:
    """DAAS_DATABASE_URL (resolved against repo root) or the canonical
    repo-root daas.db — never dashboard-mcp's local daas.db."""
    url = os.environ.get("DAAS_DATABASE_URL")
    if url:
        return _resolve_url(url)
    return f"sqlite:///{(_REPO_ROOT / 'daas.db').resolve()}"


class DashboardDatabase:
    """Singleton over the `dashboards` table."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.engine = create_engine(
            _get_db_url(),
            echo=False,
            connect_args={"check_same_thread": False},
        )
        # Additive: create just the dashboards table if missing.
        Base.metadata.create_all(self.engine, tables=[Dashboard.__table__])
        self.Session = sessionmaker(bind=self.engine)

    def _session(self):
        return self.Session()

    # ── validation ──────────────────────────────────────────────

    @staticmethod
    def _validate_slug(slug: str) -> str | None:
        if not slug or not _SLUG_RE.match(slug):
            return f"slug must match ^[A-Za-z0-9_-]+$ (got {slug!r})"
        return None

    @staticmethod
    def _parse_json(value, field, default):
        """Accept a JSON string, a list/dict, or None. Returns (parsed, error)."""
        if value is None or value == "":
            return default, None
        if isinstance(value, (list, dict)):
            return value, None
        if isinstance(value, str):
            try:
                return json.loads(value), None
            except json.JSONDecodeError as e:
                return None, f"{field} is not valid JSON: {e}"
        return None, f"{field} must be a JSON string, list, or dict (got {type(value).__name__})"

    # ── CRUD ────────────────────────────────────────────────────

    def register(self, slug, name, intro, source_tables, refresh_cadence,
                 file_path, file_url, entity_coverage=None, time_range=None,
                 chart_config=None):
        err = self._validate_slug(slug)
        if err:
            return {"error": err}
        if not name:
            return {"error": "name is required"}
        if not file_path or not file_url:
            return {"error": "file_path and file_url are required"}

        src, err = self._parse_json(source_tables, "source_tables", [])
        if err:
            return {"error": err}
        ent, err = self._parse_json(entity_coverage, "entity_coverage", None)
        if err:
            return {"error": err}
        tr, err = self._parse_json(time_range, "time_range", None)
        if err:
            return {"error": err}
        cc, err = self._parse_json(chart_config, "chart_config", [])
        if err:
            return {"error": err}

        session = self._session()
        try:
            row = session.query(Dashboard).filter(Dashboard.slug == slug).one_or_none()
            action = "updated" if row else "inserted"
            if row is None:
                row = Dashboard(slug=slug)
                session.add(row)
            row.name = name
            row.intro = intro
            row.source_tables = src
            row.entity_coverage = ent
            row.time_range = tr
            row.refresh_cadence = refresh_cadence
            row.chart_config = cc
            row.file_path = file_path
            row.file_url = file_url
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            result = row.to_dict()
            result["action"] = action
        except Exception as e:
            session.rollback()
            return {"error": str(e)}
        finally:
            session.close()

        self._regenerate_index_and_daas()
        return result

    def list_all(self):
        session = self._session()
        try:
            rows = session.query(Dashboard).order_by(Dashboard.created_at).all()
            return [
                {
                    "slug": r.slug,
                    "name": r.name,
                    "intro": r.intro,
                    "file_url": r.file_url,
                }
                for r in rows
            ]
        finally:
            session.close()

    def get(self, slug):
        session = self._session()
        try:
            row = session.query(Dashboard).filter(Dashboard.slug == slug).one_or_none()
            if row is None:
                return {"error": f"dashboard {slug!r} not found"}
            return row.to_dict()
        finally:
            session.close()

    def search(self, keyword):
        if not keyword:
            return []
        kw = keyword.lower()
        session = self._session()
        try:
            rows = session.query(Dashboard).order_by(Dashboard.created_at).all()
            matches = []
            for r in rows:
                hay = " ".join(
                    str(x) for x in [
                        r.name, r.intro, r.refresh_cadence,
                        *(r.source_tables or []),
                    ] if x is not None
                ).lower()
                if kw in hay:
                    matches.append({"slug": r.slug, "name": r.name, "intro": r.intro})
            return matches
        finally:
            session.close()

    def update(self, slug, name=None, intro=None, source_tables=None,
               entity_coverage=None, time_range=None, refresh_cadence=None,
               chart_config=None, file_path=None, file_url=None):
        session = self._session()
        try:
            row = session.query(Dashboard).filter(Dashboard.slug == slug).one_or_none()
            if row is None:
                return {"error": f"dashboard {slug!r} not found"}

            if name is not None:
                row.name = name
            if intro is not None:
                row.intro = intro
            if source_tables is not None:
                v, err = self._parse_json(source_tables, "source_tables", [])
                if err:
                    return {"error": err}
                row.source_tables = v
            if entity_coverage is not None:
                v, err = self._parse_json(entity_coverage, "entity_coverage", None)
                if err:
                    return {"error": err}
                row.entity_coverage = v
            if time_range is not None:
                v, err = self._parse_json(time_range, "time_range", None)
                if err:
                    return {"error": err}
                row.time_range = v
            if refresh_cadence is not None:
                row.refresh_cadence = refresh_cadence
            if chart_config is not None:
                v, err = self._parse_json(chart_config, "chart_config", [])
                if err:
                    return {"error": err}
                row.chart_config = v
            if file_path is not None:
                row.file_path = file_path
            if file_url is not None:
                row.file_url = file_url
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            result = row.to_dict()
        except Exception as e:
            session.rollback()
            return {"error": str(e)}
        finally:
            session.close()

        self._regenerate_index_and_daas()
        return result

    def delete(self, slug):
        session = self._session()
        try:
            row = session.query(Dashboard).filter(Dashboard.slug == slug).one_or_none()
            if row is None:
                return {"error": f"dashboard {slug!r} not found"}
            session.delete(row)
            session.commit()
        except Exception as e:
            session.rollback()
            return {"error": str(e)}
        finally:
            session.close()

        self._regenerate_index_and_daas()
        return {"deleted": slug}

    # ── index.html + daas.md regeneration ───────────────────────

    def _regenerate_index_and_daas(self):
        """Fully rewrite index.html + daas.md from the current `dashboards`
        rows. Idempotent — no append, no drift. Creates the dir on first run."""
        _DASH_DIR.mkdir(parents=True, exist_ok=True)
        rows = self.list_all()

        # index.html — styled scaffold with one <li> per dashboard.
        if rows:
            items = "\n".join(
                f'<li><a href="{_esc_attr(r["slug"])}.html">{_esc_html(r["name"] or r["slug"])}</a>'
                + (f'<br><small>{_esc_html(r["intro"])}</small>' if r.get("intro") else "")
                + "</li>"
                for r in rows
            )
        else:
            items = '<li class="empty">No dashboards yet.</li>'
        index_html = (
            '<!DOCTYPE html>\n'
            '<html lang="en">\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<title>Dashboards</title>\n'
            '<style>\n'
            '  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;'
            ' max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }\n'
            '  h1 { font-size: 1.6rem; }\n'
            '  ul#dashboards { list-style: none; padding: 0; }\n'
            '  ul#dashboards li { padding: 0.6rem 0; border-bottom: 1px solid #eee; }\n'
            '  ul#dashboards a { color: #2563eb; text-decoration: none; font-weight: 500; }\n'
            '  ul#dashboards a:hover { text-decoration: underline; }\n'
            '  ul#dashboards small { color: #666; font-size: 12px; }\n'
            '  .empty { color: #888; }\n'
            '</style>\n</head>\n<body>\n'
            '<h1>Dashboards</h1>\n'
            f'<ul id="dashboards">{items}</ul>\n'
            '</body>\n</html>\n'
        )
        (_DASH_DIR / "index.html").write_text(index_html, encoding="utf-8")

        # daas.md — markdown table (Title | Intro | URL | Source | Refresh).
        lines = ["# Dashboards", "", "| Title | Intro | URL | Source | Refresh |",
                 "|---|---|---|---|---|"]
        # Pull full rows for source_tables / refresh_cadence (list_all omits them).
        full = []
        s = self._session()
        try:
            for r in s.query(Dashboard).order_by(Dashboard.created_at).all():
                full.append(r)
        finally:
            s.close()
        for r in full:
            src = ", ".join(r.source_tables or []) if r.source_tables else ""
            intro = _md_cell(r.intro or "")
            lines.append(
                f"| {_md_cell(r.name)} | {intro} | "
                f"[{r.slug}.html]({r.slug}.html) | {src} | {r.refresh_cadence or ''} |"
            )
        (_DASH_DIR / "daas.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _esc_html(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _esc_attr(s):
    return str(s).replace('"', "&quot;").replace("<", "&lt;")


def _md_cell(s):
    """Escape a markdown table cell: pipes + newlines."""
    return str(s).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

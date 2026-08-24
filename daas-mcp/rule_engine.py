"""Unified rule engine for daas-mcp - dispatches a `Rule` to a type-specific
evaluator and returns the derived item set (entity ids, indicator names, or
row dicts per `target`).

This module is the fix for `No module named 'entity_rule_script'`. The
consolidation registry (`daas/fd_daas_mcp/registry.py`) loads each group's
modules on a *transient* `sys.path` under `_fdsrc_`-prefixed names, then pops
the path and evicts the modules. A bare deferred import
(`from entity_rule_script import run_rule_script`) at sync time therefore can
never resolve. The fix has two parts:

  1. `registry_service.py` imports `RuleEngine` at **top level**, so the class
     reference binds while the group source dir is on `sys.path` and survives
     the registry's path-pop + module eviction (the function/class objects stay
     alive via the importing module's globals).
  2. Script rules are loaded via `importlib.util.spec_from_file_location`
     against the file path - never a bare `import` of a group-local module at
     evaluation time.

Rule types (phase 1 implements `json` + `script`; `position` + `llm` land in
a follow-up - see openspec/changes/unify-rule-tools):
  - `json`     declarative entity filter (entity_type/exchange/country_code/
               codes/name_regex), target=entity_ids.
  - `script`   a Python file defining `members(ctx)`, any target.
  - `position` CSS/xpath/regex/json-path extraction (phase 2).
  - `llm`      natural-language extraction (phase 2).
"""
from __future__ import annotations

import importlib.util
import json as _json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from daas_database import _REPO_ROOT, _resolve_url
from sqlalchemy import text as _sqltext

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(name: str) -> None:
    """Raise ValueError if `name` is not a safe SQL identifier ( Guards the
    position/llm source-table column interpolation - they cannot be bind params)."""
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"invalid identifier: {name!r}")


def _regexp(pattern: str, value: Any) -> int:
    return 1 if (value is not None and re.search(pattern, str(value)) is not None) else 0


class RuleContext:
    """Read-only context handed to a script rule's `members(ctx)`.

    Opens its own sqlite3 connection in `mode=ro` so the script cannot mutate
    daas.db - it can only SELECT. The connection is separate from the caller's
    SQLAlchemy session, so the script sees committed state and cannot interfere
    with the sync transaction. A `REGEXP` function is registered for parity with
    the daas engine (so `name REGEXP '...'` works in `ctx.query`).

    `http_get` and `llm` let a script orchestrate fetch + LLM extraction
    without a new rule type per combination.
    """

    def __init__(self, db_url: str):
        self.db_url = _resolve_url(db_url)
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            url = self.db_url
            if url.startswith("sqlite:///"):
                path = url[len("sqlite:///"):]
                if path == ":memory:":
                    # An in-memory DB can't be reopened from another connection
                    # (each connection gets its own private DB). Share via the
                    # cache=shared URI so the script sees the session's data.
                    self._conn = sqlite3.connect(
                        "file::memory:?cache=shared",
                        uri=True,
                        check_same_thread=False,
                    )
                else:
                    self._conn = sqlite3.connect(
                        f"file:{path}?mode=ro",
                        uri=True,
                        check_same_thread=False,
                    )
            else:
                # Non-sqlite URL (unusual for daas) - best-effort normal connect.
                self._conn = sqlite3.connect(url, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.create_function("REGEXP", 2, _regexp)
        return self._conn

    def query(self, sql: str, params: tuple | list | dict = ()) -> list[dict]:
        """Run a SELECT and return rows as a list of dicts. Read-only by
        construction (the connection is opened in `mode=ro`); any write
        statement raises sqlite3.OperationalError."""
        cur = self._connect().execute(sql, params)
        try:
            rows = cur.fetchall()
        finally:
            cur.close()
        return [dict(r) for r in rows]

    def http_get(self, url: str, headers: Optional[dict] = None, timeout: int = 30) -> str:
        """Fetch a URL and return the response body text. For position/llm-style
        work inside a script. Uses httpx (lazy import - httpx is a transitive dep)."""
        import httpx  # lazy: keep rule_engine import-light

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers or {})
            resp.raise_for_status()
            return resp.text

    def llm(
        self,
        prompt: str,
        text: str,
        schema: Optional[dict] = None,
        model: Optional[str] = None,
    ) -> Any:
        """Call the OpenAI-compatible LLM via the shared process_tools helper,
        validating against `schema` when given. Lazy import so rule_engine does
        not require the LLM stack at import time."""
        import process_tools as T  # lazy

        result = T.extract_text(text, schema or {}, prompt=prompt, model=model)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("records", result)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class RuleEngine:
    """Dispatches a `Rule` (model instance or a legacy shim) to its evaluator.

    `evaluate` is duck-typed: any object with `.rule_type`, `.target`,
    `.config_json`, `.enabled` works (the `Rule` model, or a `SimpleNamespace`
    shim the service builds for legacy `rule_json`/`rule_script` collections).
    """

    @staticmethod
    def evaluate(rule: Any, session: Any, db_url: Optional[str] = None, limit: Optional[int] = None) -> list:
        if not getattr(rule, "enabled", True):
            return []
        rule_type = rule.rule_type
        cfg = rule.config_json or {}
        target = getattr(rule, "target", "entity_ids")
        if rule_type == "json":
            return _eval_json(cfg, session)  # already entity ids
        if rule_type == "script":
            items = _eval_script(cfg, session, db_url)
        elif rule_type == "position":
            items = _eval_position(cfg, session, db_url)
        elif rule_type == "llm":
            items = _eval_llm(cfg, session, target)
        else:
            raise ValueError(f"unknown rule_type: {rule_type!r}")
        if limit is not None:
            items = items[:limit]
        if target == "entity_ids":
            return _normalize_to_entity_ids(items, session)
        return list(items)


# ── json rule ───────────────────────────────────────────────────


def _eval_json(config: dict, session: Any) -> list[tuple[str, str]]:
    """Apply the declarative filter and return matching natural keys.

    Rule keys: entity_type, exchange, country_code, codes (list), name_regex.
    After the entity-master migration (D5/3.7) there is no local `entities`
    table, so only the explicit `codes` form is resolvable here; filter-only
    rules (exchange/country_code/name_regex) need the gateway entity master and
    return `[]`. Mirrors the legacy `EntityCollectionService._rule_entity_ids`.
    """
    codes = config.get("codes")
    if codes:
        et = config.get("entity_type") or "stock"
        return [(et, str(c)) for c in codes]
    # ponytail: filter-only json rules have no local entity master post-migration;
    # resolve via gateway if ever exercised.
    return []


# ── script rule ─────────────────────────────────────────────────


def _eval_script(
    config: dict, session: Any, db_url: Optional[str]
) -> list:
    """Load a Python rule script, call `members(ctx)`, return the raw item list.

    `config`: {"script_path": "<repo-root-relative or absolute>", "function": "members"}.
    The script runs with a read-only `RuleContext`. Normalization to entity ids
    (per `target`) is applied by `RuleEngine.evaluate`, not here.
    """
    script_path = config.get("script_path")
    if not script_path:
        raise ValueError("script rule config requires 'script_path'")
    fn_name = config.get("function", "members")

    p = Path(script_path)
    if not p.is_absolute():
        p = (_REPO_ROOT / script_path).resolve()
    if not p.exists():
        raise FileNotFoundError(
            f"rule script not found: {script_path!r} (resolved to {p})"
        )
    spec = importlib.util.spec_from_file_location(
        f"daas_rule_script_{p.stem}_{abs(hash(p)) % 100000}", p
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        raise TypeError(
            f"rule script {script_path!r} must define a callable `{fn_name}(ctx)`"
        )

    ctx = RuleContext(db_url or str(session.bind.url))
    try:
        result = fn(ctx)
    finally:
        ctx.close()
    return result if result is not None else []


def _normalize_to_entity_ids(items: list, session: Any) -> list[tuple[str, str]]:
    """Resolve a script's returned member items to natural keys (entity_type, code).

    Each item may be:
      - str  -> a stock code (entity_type defaults to 'stock')
      - dict -> {"entity_type":..,"code":..}
    int entity ids are no longer resolvable (no local `entities` table) and are
    skipped. Unknown items are skipped (a sync shouldn't fail over one delisted code).
    """
    keys: list[tuple[str, str]] = []
    for item in items:
        key = _resolve_script_item(item, session)
        if key is not None:
            keys.append(key)
    return keys


def _resolve_script_item(item: Any, session: Any) -> Optional[tuple[str, str]]:
    if isinstance(item, bool):  # bool is a subclass of int; ignore
        return None
    if isinstance(item, int):
        # post-migration: no local entity id mapping to resolve; skip
        return None
    if isinstance(item, str):
        return ("stock", item)
    if isinstance(item, dict):
        if item.get("entity_id") is not None:
            # legacy integer ids no longer resolvable
            return None
        code = item.get("code")
        if code is not None:
            et = item.get("entity_type", "stock")
            return (et, str(code))
    return None


# ── position rule ───────────────────────────────────────────────


def _load_position_source(source: dict, session: Any, db_url: Optional[str]) -> str:
    """Load the text for a position rule from its `source` config.

    source.type ∈ {text, url, file, table}; for `table`, value is
    {table, column} and all rows' column text is joined.
    """
    stype = source.get("type")
    val = source.get("value")
    if stype == "text":
        return val or ""
    if stype == "url":
        ctx = RuleContext(db_url or str(session.bind.url))
        try:
            return ctx.http_get(val)
        finally:
            ctx.close()
    if stype == "file":
        p = Path(val)
        if not p.is_absolute():
            p = (_REPO_ROOT / val).resolve()
        if not p.exists():
            raise FileNotFoundError(f"position source file not found: {val!r} (resolved to {p})")
        return p.read_text(encoding="utf-8", errors="replace")
    if stype == "table":
        table = (val or {}).get("table")
        column = (val or {}).get("column")
        _validate_identifier(table)
        _validate_identifier(column)
        sql = _sqltext(f'SELECT "{column}" FROM "{table}"')
        with session.bind.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return "\n".join(str(r[0]) for r in rows if r[0] is not None)
    raise ValueError(f"unknown position source type: {stype!r}")


def _eval_position(config: dict, session: Any, db_url: Optional[str]) -> list:
    """Extract values from a source at a structural position.

    config: {source: {type, value}, selector_type: css|xpath|regex|jsonpath,
    selector, extract: text|@attr (default text)}. Returns the extracted
    strings. CSS/xpath use lxml+cssselect (lazy); regex uses stdlib; jsonpath
    uses jsonpath-ng (lazy). A missing optional dep raises a clear RuntimeError.
    """
    source = config.get("source") or {}
    text = _load_position_source(source, session, db_url)
    selector_type = config.get("selector_type")
    selector = config.get("selector")
    if not selector_type or not selector:
        raise ValueError("position rule requires selector_type and selector")
    extract = config.get("extract", "text")

    if selector_type == "regex":
        matches = re.findall(selector, text)
        out: list = []
        for m in matches:
            if isinstance(m, tuple):
                out.append(m[0] if m else "")
            else:
                out.append(m)
        return [x for x in out if x]

    if selector_type in ("css", "xpath"):
        try:
            from lxml import html as _lhtml
        except ImportError as e:
            raise RuntimeError(
                "position rule (css/xpath) needs lxml+cssselect (not installed)"
            ) from e
        if selector_type == "css":
            try:
                from cssselect import SelectorError  # noqa: F401
            except ImportError as e:
                raise RuntimeError("position rule (css) needs cssselect (not installed)") from e
            tree = _lhtml.fromstring(text)
            nodes = tree.cssselect(selector)
        else:
            tree = _lhtml.fromstring(text)
            nodes = tree.xpath(selector)
        out = []
        for n in nodes:
            if extract == "text":
                out.append(n.text_content().strip() if hasattr(n, "text_content") else str(n).strip())
            elif extract.startswith("@"):
                out.append(n.get(extract[1:]))
            else:
                out.append(str(n).strip())
        return [x for x in out if x]

    if selector_type == "jsonpath":
        try:
            import jsonpath_ng
        except ImportError as e:
            raise RuntimeError("position rule (jsonpath) needs jsonpath-ng (not installed)") from e
        data = _json.loads(text)
        expr = jsonpath_ng.parse(selector)
        return [m.value for m in expr.find(data)]

    raise ValueError(f"unknown selector_type: {selector_type!r}")


# ── llm rule (member mapping) ────────────────────────────────────


def _eval_llm(config: dict, session: Any, target: str) -> list:
    """Extract records via the shared LLM helper and map them to members.

    For `target='entity_ids'`/`'indicator_names'`, extracts from the config's
    inline `text` (single document) and maps records via `mapping`
    (`code_from` default 'code' / `name_from` default 'name'). For multi-row
    incremental extraction into `process_results`, use `daas_run_rule`
    (`target='rows'`) - this evaluator does not persist.
    """
    import process_tools as T  # lazy: keep rule_engine import-light

    text = config.get("text")
    if text is None:
        raise ValueError(
            "llm member-mapping rule requires 'text' in config; for incremental "
            "table extraction use target='rows' via daas_run_rule, or a script rule"
        )
    schema = config.get("schema_json") or config.get("schema") or {}
    prompt = config.get("prompt")
    model = config.get("model")
    max_chars = config.get("max_chars", 12000)
    result = T.extract_text(text, schema, prompt=prompt, model=model, max_chars=max_chars)
    if "error" in result:
        raise RuntimeError(
            f"llm extraction failed: {result.get('error')}: {result.get('detail')}"
        )
    records = result.get("records", [])
    mapping = config.get("mapping", {})
    if target == "entity_ids":
        key = mapping.get("code_from", "code")
        return [r.get(key) for r in records if isinstance(r, dict) and r.get(key)]
    if target == "indicator_names":
        key = mapping.get("name_from", "name")
        return [r.get(key) for r in records if isinstance(r, dict) and r.get(key)]
    return list(records)


def legacy_shim(rule_type: str, config: dict, target: str = "entity_ids") -> SimpleNamespace:
    """Build a duck-typed rule shim for a legacy `rule_json`/`rule_script`
    collection (no `rule_id`). Lets the engine evaluate legacy collections
    without a `rules` row - back-compat for the D8 precedence."""
    return SimpleNamespace(
        rule_type=rule_type,
        target=target,
        config_json=config,
        enabled=True,
    )


def script_config_from_path(script_path: str) -> dict:
    """Helper: build a `script` rule config_json from a legacy rule_script path."""
    return {"script_path": script_path, "function": "members"}

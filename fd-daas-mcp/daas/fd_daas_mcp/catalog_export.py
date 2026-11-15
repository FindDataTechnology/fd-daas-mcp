"""DAAS catalog export — the official-data-anchor feed for the supply catalog.

Cross-repo contract (paas change ``add-supply-catalog-unification`` task 2.1):
the facet service pulls this face for its ``/api/data-anchors-catalog``
projection (openspec: supply-catalog, "The data-anchor species is fed by the
DAAS catalog face"). v1 scope is the catalog classes — entities and indicators
— with observation/series tables explicitly excluded.

Shape (JSON, read-only, stable ids from primary keys):

    {"as_of": "<ISO-8601>", "anchors": [
        {"id": "ind-<pk>", "name": <indicator_name>, "kind": "indicator",
         "summary": <one line>, "source": <datasource>},
        {"id": "src-<pk>", "name": <label>, "kind": "source",
         "summary": <description>, "source": <name>},
        {"id": "ent-<pk>", "name": <code>, "kind": "entity",
         "summary": <entity_type>, "source": <collection_id>},
    ]}

Idempotency is by construction: the anchors list is a pure function of the
table contents (two exports of an unchanged library are byte-identical except
``as_of``). Enabled rows only — a disabled catalog entry is not a supply
surface.

Serving modes:
- ``--out catalog.json`` writes the file (static hosting, cron-refreshable);
- ``--serve --port N`` answers ``GET /catalog.json`` over stdlib http.server,
  gated by ``DAAS_CATALOG_TOKEN`` bearer when set (same posture as the MCP
  server's BearerAuthMiddleware: unset token = open).

No writes, ever — the export opens the SQLite library read-only.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

#: The v1 catalog classes, as a closed set. New species extend this with a
#: version bump on the consumer side, never by overloading an existing kind.
ANCHOR_KINDS = ("indicator", "source", "entity")


def _rows(db: sqlite3.Connection, sql: str) -> list[dict]:
    cur = db.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def export_catalog(db_path: str) -> dict:
    """Read the catalog tables and project them to the anchor feed shape."""
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        indicators = _rows(
            db,
            "SELECT id, name, indicator_name, datasource FROM indicator_rules "
            "WHERE enabled = 1 ORDER BY id",
        )
        sources = _rows(
            db,
            "SELECT id, name, label, description FROM sources "
            "WHERE enabled = 1 ORDER BY id",
        )
        entities = _rows(
            db,
            "SELECT id, collection_id, entity_type, code FROM entity_collection_items "
            "ORDER BY id",
        )

    anchors = [
        {
            "id": f"ind-{r['id']}",
            "name": r["indicator_name"] or r["name"],
            "kind": "indicator",
            "summary": f"{r['indicator_name'] or r['name']}（{r['datasource']}）",
            "source": r["datasource"],
        }
        for r in indicators
    ] + [
        {
            "id": f"src-{r['id']}",
            "name": r["label"] or r["name"],
            "kind": "source",
            "summary": r["description"] or "",
            "source": r["name"],
        }
        for r in sources
    ] + [
        {
            "id": f"ent-{r['id']}",
            "name": r["code"],
            "kind": "entity",
            "summary": f"{r['entity_type']} {r['code']}",
            "source": str(r["collection_id"]),
        }
        for r in entities
    ]

    return {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "anchors": anchors,
    }


def _serve(db_path: str, host: str, port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    token = os.environ.get("DAAS_CATALOG_TOKEN", "").strip()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib naming
            if self.path.split("?")[0].rstrip("/") not in ("/catalog.json", "", "/"):
                self.send_error(404)
                return
            if token:
                provided = self.headers.get("Authorization", "")
                if provided != f"Bearer {token}":
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", "Bearer")
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
            body = json.dumps(export_catalog(db_path), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "max-age=60")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # keep the quiet posture
            sys.stderr.write(f"[catalog-export] {fmt % args}\n")

    httpd = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(f"[catalog-export] serving {db_path} on http://{host}:{port}/catalog.json\n")
    httpd.serve_forever()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="daas.db", help="SQLite library (read-only)")
    parser.add_argument("--out", help="write catalog.json to this path")
    parser.add_argument("--serve", action="store_true", help="serve GET /catalog.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args(argv)

    if not (args.out or args.serve):
        parser.error("pass --out or --serve")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(export_catalog(args.db), fh, ensure_ascii=False, indent=2)
        sys.stderr.write(f"[catalog-export] wrote {args.out}\n")
    if args.serve:
        _serve(args.db, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

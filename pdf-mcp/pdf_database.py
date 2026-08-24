"""Database layer for the pdf group.

Owns its own SQLAlchemy engine on the same ``daas.db`` (resolved from
``DAAS_DATABASE_URL``), with a ``connect`` listener that loads the ``sqlite-vec``
extension so the ``pdf_chunks_vec`` ``vec0`` virtual table works. The
``pdf_documents`` / ``pdf_chunks`` / ``pdf_meta`` tables are SQLAlchemy models
(created via ``Base.metadata.create_all``); the ``vec0`` table is created at
runtime via raw DDL because SQLAlchemy does not model virtual tables.

Mirrors ``daas_database.py`` for URL resolution (relative ``sqlite:///`` paths
resolved against the repo root) and the ``PRAGMA foreign_keys=ON`` per-connection
pattern. Vectors are L2-normalized at ingest (see ``embedding_client``), so
sqlite-vec's default L2 distance ranking is equivalent to cosine ranking.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from models import Base, PdfChunk, PdfDocument, PdfMeta

# fd-daas-mcp/pdf-mcp/pdf_database.py -> parents[2] = repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

_engine: Any = None
_Session: Any = None


def _load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _resolve_url(url: str) -> str:
    """Resolve a relative sqlite:/// path against the repo root. Pass through
    absolute paths, :memory:, and non-sqlite URLs."""
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        if path and path != ":memory:" and not os.path.isabs(path):
            return f"sqlite:///{(REPO_ROOT / path).resolve()}"
    return url


def _default_url() -> str:
    _load_dotenv()
    return os.environ.get("DAAS_DATABASE_URL") or f"sqlite:///{(REPO_ROOT / 'mcp' / 'daas.db').resolve()}"


def get_engine(db_url: str | None = None):
    """Return the cached SQLAlchemy engine (sqlite-vec loaded per connection).

    A fresh engine is created when ``db_url`` is passed (used by the hermetic
    selfcheck to point at a temp DB). The module-level singleton is used
    otherwise so repeated tool calls share one engine.
    """
    global _engine, _Session
    if db_url is None and _engine is not None:
        return _engine
    _load_dotenv()
    url = _resolve_url(db_url or _default_url())
    engine = create_engine(url, echo=False)

    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record):  # noqa: ANN001
            # Load sqlite-vec so the pdf_chunks_vec vec0 table works.
            dbapi_conn.enable_load_extension(True)
            try:
                import sqlite_vec  # type: ignore

                dbapi_conn.load_extension(sqlite_vec.loadable_path())
            except Exception as e:  # pragma: no cover - dep-gated
                raise RuntimeError(
                    f"sqlite-vec extension load failed: {e}. "
                    "Install with `pip install sqlite-vec` (or `uv sync --extra pdf`)."
                ) from e
            finally:
                dbapi_conn.enable_load_extension(False)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    if db_url is None:
        _engine = engine
        _Session = sessionmaker(bind=engine)
    else:
        # Fresh engine for selfcheck; do not pollute the singleton.
        return engine
    return engine


def get_session():
    if _Session is None:
        get_engine()
    return _Session()


def ensure_tables() -> None:
    """Create the pdf metadata tables (pdf_documents / pdf_chunks / pdf_meta)
    if absent. Does NOT need the embedder dim - safe to call before the dedup
    query / dim check so a fresh DB has the tables before the first SELECT."""
    engine = get_engine()
    Base.metadata.create_all(engine)


def init_schema(dim: int, model_name: str) -> None:
    """Create the pdf tables + the vec0 virtual table (idempotent) and record
    the configured dim/model in pdf_meta. ``ensure_tables`` is called first so
    the metadata tables exist independently of the vec0 table."""
    ensure_tables()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS pdf_chunks_vec "
            f"USING vec0(embedding float[{int(dim)}])"
        ))
        conn.execute(text(
            "INSERT INTO pdf_meta(key, value) VALUES ('embedding_dim', :dim) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        ), {"dim": str(int(dim))})
        conn.execute(text(
            "INSERT INTO pdf_meta(key, value) VALUES ('embedding_model', :m) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        ), {"m": model_name})


def get_stored_dim() -> int | None:
    """Return the dim recorded in pdf_meta, or None if no vec table initialized."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT value FROM pdf_meta WHERE key='embedding_dim'")
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def find_by_hash(file_hash: str):
    """Return the existing PdfDocument for a hash, or None (dedup lookup)."""
    with get_session() as s:
        return s.query(PdfDocument).filter_by(file_hash=file_hash).first()


def insert_document(doc: dict) -> int:
    """Insert a pdf_documents row, return its id."""
    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(text(
            "INSERT INTO pdf_documents "
            "(name, source_type, source_ref, url, file_hash, page_count, "
            " char_count, chunk_count, embedding_model, embedding_dim, status, metadata) "
            "VALUES (:name, :source_type, :source_ref, :url, :file_hash, :page_count, "
            " :char_count, :chunk_count, :embedding_model, :embedding_dim, :status, :metadata)"
        ), {
            "name": doc["name"],
            "source_type": doc["source_type"],
            "source_ref": doc.get("source_ref"),
            "url": doc.get("url"),
            "file_hash": doc.get("file_hash"),
            "page_count": doc.get("page_count"),
            "char_count": doc.get("char_count", 0),
            "chunk_count": doc.get("chunk_count", 0),
            "embedding_model": doc["embedding_model"],
            "embedding_dim": doc["embedding_dim"],
            "status": doc.get("status", "active"),
            "metadata": json.dumps(doc.get("metadata") or {}),
        })
        return r.lastrowid


def insert_chunks(doc_id: int, chunks: list[dict]) -> list[int]:
    """Insert chunk rows, return their ids in chunk_index order."""
    engine = get_engine()
    with engine.begin() as conn:
        for c in chunks:
            conn.execute(text(
                "INSERT INTO pdf_chunks "
                "(doc_id, chunk_index, text, page_number, char_start, char_end, token_count) "
                "VALUES (:doc_id, :chunk_index, :text, :page_number, :char_start, :char_end, :token_count)"
            ), {
                "doc_id": doc_id,
                "chunk_index": c["chunk_index"],
                "text": c["text"],
                "page_number": c.get("page_number"),
                "char_start": c.get("char_start", 0),
                "char_end": c.get("char_end", 0),
                "token_count": c.get("token_count", 0),
            })
        rows = conn.execute(
            text("SELECT id FROM pdf_chunks WHERE doc_id=:d ORDER BY chunk_index"), {"d": doc_id}
        ).fetchall()
    return [r[0] for r in rows]


def insert_vectors(chunk_ids: list[int], vectors: list[list[float]]) -> None:
    """Insert vec0 rows with explicit rowid == pdf_chunks.id. sqlite-vec accepts
    embeddings as JSON array strings."""
    engine = get_engine()
    with engine.begin() as conn:
        for cid, vec in zip(chunk_ids, vectors):
            conn.execute(text(
                "INSERT INTO pdf_chunks_vec(rowid, embedding) VALUES (:id, :vec)"
            ), {"id": cid, "vec": json.dumps([float(x) for x in vec])})


def search_vectors(query_vec: list[float], top_k: int, doc_id: int | None = None) -> list[dict]:
    """KNN over pdf_chunks_vec, joined to pdf_chunks + pdf_documents.

    Global search fetches exactly top_k. doc_id-filtered search fetches a wider
    window (top_k*20) and filters by doc_id in Python, since vec0 KNN returns
    the global nearest (no per-partition filtering in this schema). score is
    derived from sqlite-vec's L2 distance over normalized vectors (lower =
    better -> higher score)."""
    engine = get_engine()
    fetch_k = top_k if doc_id is None else min(top_k * 20, 500)
    qjson = json.dumps([float(x) for x in query_vec])
    # sqlite-vec requires the KNN LIMIT to constrain the vec0 scan directly,
    # so the LIMIT lives in a subquery (with a literal k - vec0 does not accept
    # a parameterized LIMIT) and we JOIN outside it.
    sql = text(
        "SELECT v.rowid, v.distance, c.doc_id, c.chunk_index, c.text, "
        "       c.page_number, d.name "
        "FROM (SELECT rowid, distance FROM pdf_chunks_vec "
        "      WHERE embedding MATCH :q ORDER BY distance LIMIT "
        + str(int(fetch_k))
        + ") v "
        "JOIN pdf_chunks c ON c.id = v.rowid "
        "JOIN pdf_documents d ON d.id = c.doc_id "
        "ORDER BY v.distance"
    )
    with engine.connect() as conn:
        rows = conn.execute(sql, {"q": qjson}).fetchall()
    out: list[dict] = []
    for row in rows:
        _rid, dist, did, cidx, ctxt, pg, dname = row
        if doc_id is not None and did != doc_id:
            continue
        # Normalized L2 distance in [0, 2]; cosine_sim = 1 - dist^2 / 2.
        d = float(dist)
        score = max(0.0, 1.0 - (d * d) / 2.0)
        out.append({
            "doc_id": did,
            "doc_name": dname,
            "chunk_index": cidx,
            "text": ctxt,
            "page_number": pg,
            "score": round(score, 4),
        })
        if len(out) >= top_k:
            break
    return out


def list_documents() -> list[dict]:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, name, source_type, source_ref, url, page_count, "
            "       chunk_count, status, created_at "
            "FROM pdf_documents ORDER BY created_at DESC"
        )).fetchall()
    return [{
        "doc_id": r[0], "name": r[1], "source_type": r[2], "source_ref": r[3],
        "url": r[4], "page_count": r[5], "chunk_count": r[6], "status": r[7],
        "created_at": str(r[8]) if r[8] is not None else None,
    } for r in rows]


def get_document(doc_id: int) -> dict | None:
    engine = get_engine()
    with engine.connect() as conn:
        r = conn.execute(text(
            "SELECT id, name, source_type, source_ref, url, file_hash, page_count, "
            "       char_count, chunk_count, embedding_model, embedding_dim, status, created_at "
            "FROM pdf_documents WHERE id=:d"
        ), {"d": doc_id}).fetchone()
        if r is None:
            return None
        sample = conn.execute(text(
            "SELECT chunk_index, text, page_number FROM pdf_chunks "
            "WHERE doc_id=:d ORDER BY chunk_index LIMIT 3"
        ), {"d": doc_id}).fetchall()
    return {
        "doc_id": r[0], "name": r[1], "source_type": r[2], "source_ref": r[3],
        "url": r[4], "file_hash": r[5], "page_count": r[6], "char_count": r[7],
        "chunk_count": r[8], "embedding_model": r[9], "embedding_dim": r[10],
        "status": r[11], "created_at": str(r[12]) if r[12] is not None else None,
        "sample_chunks": [{
            "chunk_index": s[0], "text": (s[1] or "")[:300], "page_number": s[2],
        } for s in sample],
    }


def delete_document_cascade(doc_id: int) -> int:
    """Delete a doc's vec rows + chunks + the doc row. Returns deleted chunk count."""
    engine = get_engine()
    with engine.begin() as conn:
        ids = conn.execute(
            text("SELECT id FROM pdf_chunks WHERE doc_id=:d"), {"d": doc_id}
        ).fetchall()
        for (cid,) in ids:
            conn.execute(text("DELETE FROM pdf_chunks_vec WHERE rowid=:id"), {"id": cid})
        conn.execute(text("DELETE FROM pdf_documents WHERE id=:d"), {"d": doc_id})
    return len(ids)


def count_documents() -> int:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text("SELECT COUNT(*) FROM pdf_documents")).fetchone()
    return int(row[0]) if row else 0

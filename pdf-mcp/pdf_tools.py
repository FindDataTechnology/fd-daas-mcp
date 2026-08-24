"""pdf tools - local PDF/text ingestion + semantic search.

6 tools:
  ingest_document(file_path|url, ...) - extract, chunk, embed, persist
  ingest_text(text, name, ...)        - chunk+embed raw text (no extraction)
  search(query, top_k, doc_id, ...)   - KNN over pdf_chunks_vec
  list_documents()                    - list ingested docs
  get_document(doc_id)                - doc detail + chunk sample
  delete_document(doc_id)             - cascade delete (doc + chunks + vec)

Plus ``cli_ingest`` / ``cli_search`` in-process helpers for the
``--pdf-ingest`` / ``--pdf-search`` cron branches (mirror ``cli_run_rule``).

All embedding is local (sentence-transformers default); vectors live in the
``pdf_chunks_vec`` ``vec0`` table in ``daas.db``. No API key, no document
egress. SHA-256 dedup makes ingest idempotent per file/text content.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

import embedding_client
import pdf_database

# Structured error returned when the [pdf] extra is absent. Mirrors the
# akshare/es lazy-import guard pattern.
_PDF_EXTRA_ERROR = {
    "error": "pdf extra not installed: uv sync --extra pdf "
    "(or pip install sqlite-vec sentence-transformers pdfplumber)"
}


def _guard():
    """Return None if the sqlite-vec dep is importable, else an error dict."""
    try:
        import sqlite_vec  # noqa: F401
        return None
    except ImportError:
        return dict(_PDF_EXTRA_ERROR)


# ── hashing ──────────────────────────────────────────────────────


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── text extraction ──────────────────────────────────────────────


def _extract_pdf(path: str, max_pages: int | None = None) -> tuple[list[tuple[int, str]], str]:
    """Return (pages, extractor) where pages is [(page_no, text), ...].

    Tries pdfplumber first (better layout + page granularity), falls back to
    pypdf on import/error. Caps at max_pages."""
    try:
        import pdfplumber  # type: ignore

        pages: list[tuple[int, str]] = []
        with pdfplumber.open(path) as pdf:
            n = len(pdf.pages)
            limit = n if max_pages is None else min(n, max_pages)
            for i in range(limit):
                txt = pdf.pages[i].extract_text() or ""
                pages.append((i + 1, txt))
        return pages, "pdfplumber"
    except Exception:
        pass
    from pypdf import PdfReader  # type: ignore

    reader = PdfReader(str(path))
    n = len(reader.pages)
    limit = n if max_pages is None else min(n, max_pages)
    pages = []
    for i in range(limit):
        txt = reader.pages[i].extract_text() or ""
        pages.append((i + 1, txt))
    return pages, "pypdf"


def _download_url(url: str) -> str:
    """Download a URL to a temp file, return the path. Caller unlinks it."""
    req = Request(url, headers={"User-Agent": "fd-daas-mcp/pdf"})
    with urlopen(req, timeout=60) as resp:  # noqa: S310 - URL is user-supplied
        data = resp.read()
    base = url.lower().split("?", 1)[0]
    suffix = ".pdf" if base.endswith(".pdf") else ".txt"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return tmp


# ── chunking ─────────────────────────────────────────────────────


def _split_text(text: str, chunk_size: int = 1000, chunk_overlap: int = 200) -> list[tuple[str, int, int]]:
    """Dependency-free recursive-ish splitter.

    Returns [(chunk_text, char_start, char_end), ...]. Walks the text in a
    sliding window of chunk_size, backing up to the last sentence/line boundary
    in the final 20% when possible, then steps forward by (end - overlap).
    Sentence boundaries: '. ', '。', '!', '?', newline."""
    if not text or not text.strip():
        return []
    chunks: list[tuple[str, int, int]] = []
    n = len(text)
    start = 0
    while start < n:
        end = min(start + chunk_size, n)
        if end < n:
            window_start = start + int(chunk_size * 0.8)
            window = text[window_start:end]
            cut = max(
                window.rfind(". "), window.rfind("。"),
                window.rfind("! "), window.rfind("? "), window.rfind("\n"),
            )
            if cut != -1:
                end = window_start + cut + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((chunk, start, end))
        if end >= n:
            break
        nxt = end - chunk_overlap
        start = nxt if nxt > start else end
    return chunks


def _chunk_pages(pages: list[tuple[int, str]], chunk_size: int, chunk_overlap: int) -> list[dict]:
    """Page-aware chunking: each chunk belongs to exactly one page (so
    page_number is unambiguous). char_start/char_end are within the page."""
    out: list[dict] = []
    for page_no, ptext in pages:
        for ctext, cs, ce in _split_text(ptext, chunk_size, chunk_overlap):
            out.append({
                "text": ctext,
                "page_number": page_no,
                "char_start": cs,
                "char_end": ce,
                "token_count": len(ctext.split()),
            })
    return out


# ── ingest core ──────────────────────────────────────────────────


def _ingest(
    *,
    name: str,
    source_type: str,
    file_hash: str | None,
    pages: list[tuple[int, str]] | None,
    raw_text: str | None,
    source_ref: str | None,
    url: str | None,
    metadata: dict | None,
    chunk_size: int,
    chunk_overlap: int,
    embedding_model_override: str | None = None,
) -> dict:
    """Shared ingest path after extraction/download. pages is None for raw text."""
    # Ensure the metadata tables exist before the dedup query (fresh DB safe).
    pdf_database.ensure_tables()
    # dedup
    if file_hash:
        existing = pdf_database.find_by_hash(file_hash)
        if existing:
            return {
                "doc_id": existing.id,
                "name": existing.name,
                "source_type": existing.source_type,
                "page_count": existing.page_count,
                "chunk_count": existing.chunk_count,
                "deduped": True,
                "embedding_model": existing.embedding_model,
                "status": existing.status,
            }

    embedder = embedding_client.get_embedder()
    dim = embedder.dim()
    model_name = embedding_model_override or embedder.model_name()

    # init schema first so stored dim is available for the mismatch check
    pdf_database.init_schema(dim, model_name)
    stored = pdf_database.get_stored_dim()
    if stored is not None and stored != dim:
        return {
            "error": (
                f"embedding dim mismatch: configured model '{model_name}' has dim {dim} "
                f"but pdf_chunks_vec was created with dim {stored}. "
                f"Re-ingest: delete existing pdf documents (delete_document) or drop "
                f"the pdf_chunks_vec table, then re-ingest with the new model."
            )
        }

    # chunk
    if pages is not None:
        chunk_rows = _chunk_pages(pages, chunk_size, chunk_overlap)
        page_count = len(pages)
        char_count = sum(len(t) for _, t in pages)
    else:
        assert raw_text is not None
        splits = _split_text(raw_text, chunk_size, chunk_overlap)
        chunk_rows = [{
            "text": t, "page_number": None, "char_start": cs, "char_end": ce,
            "token_count": len(t.split()),
        } for t, cs, ce in splits]
        page_count = None
        char_count = len(raw_text)

    if not chunk_rows:
        # scanned PDF / empty text - record the doc with no chunks.
        doc_id = pdf_database.insert_document({
            "name": name, "source_type": source_type, "source_ref": source_ref,
            "url": url, "file_hash": file_hash, "page_count": page_count,
            "char_count": char_count, "chunk_count": 0,
            "embedding_model": model_name, "embedding_dim": dim,
            "status": "no_text", "metadata": metadata,
        })
        return {
            "doc_id": doc_id, "name": name, "source_type": source_type,
            "page_count": page_count, "chunk_count": 0, "deduped": False,
            "embedding_model": model_name, "status": "no_text",
        }

    # assign sequential chunk_index within the document
    for i, c in enumerate(chunk_rows):
        c["chunk_index"] = i

    # embed (one batch)
    vectors = embedder.embed_texts([c["text"] for c in chunk_rows])
    if len(vectors) != len(chunk_rows):
        return {"error": f"embedding count mismatch: {len(vectors)} vs {len(chunk_rows)} chunks"}

    doc_id = pdf_database.insert_document({
        "name": name, "source_type": source_type, "source_ref": source_ref,
        "url": url, "file_hash": file_hash, "page_count": page_count,
        "char_count": char_count, "chunk_count": len(chunk_rows),
        "embedding_model": model_name, "embedding_dim": dim,
        "status": "active", "metadata": metadata,
    })
    chunk_ids = pdf_database.insert_chunks(doc_id, chunk_rows)
    pdf_database.insert_vectors(chunk_ids, vectors)

    return {
        "doc_id": doc_id, "name": name, "source_type": source_type,
        "page_count": page_count, "chunk_count": len(chunk_rows),
        "deduped": False, "embedding_model": model_name, "status": "active",
    }


# ── tool surface ─────────────────────────────────────────────────


def ingest_document(
    file_path: str | None = None,
    url: str | None = None,
    name: str | None = None,
    source_ref: str | None = None,
    metadata: dict | None = None,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
    max_pages: int = 500,
) -> dict:
    """Ingest a PDF from a local file path or HTTP URL: extract text (pdfplumber
    default, pypdf fallback), chunk with overlap, embed locally, and persist to
    daas.db. Exactly one of file_path / url is required. Re-ingesting the same
    file (by SHA-256) is a no-op that returns deduped=true."""
    err = _guard()
    if err:
        return err
    if bool(file_path) == bool(url):
        return {"error": "exactly one of file_path or url is required"}

    tmp: str | None = None
    try:
        if url:
            try:
                tmp = _download_url(url)
                path = tmp
            except Exception as e:
                return {"error": f"download failed: {type(e).__name__}: {e}"}
            source_type = "url"
            if name is None:
                name = os.path.basename(url.split("?", 1)[0]) or "url-document"
        else:
            assert file_path is not None
            path = file_path
            if not os.path.exists(path):
                return {"error": f"file not found: {path}"}
            source_type = "file"
            if name is None:
                name = os.path.basename(path) or "document"

        file_hash = _sha256_file(path)
        try:
            pages, _extractor = _extract_pdf(path, max_pages=max_pages)
        except Exception as e:
            return {"error": f"PDF extraction failed: {type(e).__name__}: {e}"}

        return _ingest(
            name=name, source_type=source_type, file_hash=file_hash, pages=pages,
            raw_text=None, source_ref=source_ref, url=url if url else None,
            metadata=metadata, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        )
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def ingest_text(
    text: str,
    name: str,
    source_ref: str | None = None,
    metadata: dict | None = None,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> dict:
    """Ingest raw text (no PDF extraction): chunk + embed + persist. For plain
    text, HTML-stripped filings, transcripts, etc. source_type='text',
    page_count=null. Re-ingesting identical text (by SHA-256) is a no-op."""
    err = _guard()
    if err:
        return err
    if not text or not text.strip():
        return {"error": "text is required and must be non-empty"}
    if not name or not name.strip():
        return {"error": "name is required"}
    file_hash = _sha256_text(text)
    return _ingest(
        name=name, source_type="text", file_hash=file_hash, pages=None,
        raw_text=text, source_ref=source_ref, url=None, metadata=metadata,
        chunk_size=chunk_size, chunk_overlap=chunk_overlap,
    )


def search_documents(
    query: str,
    top_k: int = 5,
    doc_id: int | None = None,
    min_score: float | None = None,
) -> dict:
    """Semantic search: embed the query with the same backend used at ingest,
    run KNN over pdf_chunks_vec, return ranked chunks with doc_name + page_number
    + score. Optional doc_id restricts to one document; min_score filters low
    results."""
    err = _guard()
    if err:
        return err
    if not query or not query.strip():
        return {"error": "query is required"}

    # Ensure tables exist so get_stored_dim works on a fresh DB; bail before
    # loading the embedder if nothing has been ingested yet.
    pdf_database.ensure_tables()
    stored = pdf_database.get_stored_dim()
    if stored is None:
        return {"query": query, "results": [], "count": 0,
                "note": "no documents ingested yet"}

    embedder = embedding_client.get_embedder()
    dim = embedder.dim()
    if stored != dim:
        return {
            "error": (
                f"embedding dim mismatch: configured model has dim {dim} but stored "
                f"vectors have dim {stored}. Re-ingest after reverting PDF_EMBEDDING_MODEL "
                f"or dropping pdf_chunks_vec."
            )
        }

    qvec = embedder.embed_texts([query])[0]
    results = pdf_database.search_vectors(qvec, top_k, doc_id)
    if min_score is not None:
        results = [r for r in results if r["score"] >= min_score]
    return {"query": query, "results": results, "count": len(results)}


def list_documents() -> dict:
    """List all ingested documents (newest first)."""
    err = _guard()
    if err:
        return err
    return {"documents": pdf_database.list_documents(), "count": pdf_database.count_documents()}


def get_document(doc_id: int) -> dict:
    """Get one document's metadata + a 3-chunk sample."""
    err = _guard()
    if err:
        return err
    doc = pdf_database.get_document(doc_id)
    if doc is None:
        return {"error": f"document {doc_id} not found"}
    return doc


def delete_document(doc_id: int) -> dict:
    """Delete a document and cascade-delete its chunks + vectors. Returns
    {deleted: doc_id, chunks_removed: N}."""
    err = _guard()
    if err:
        return err
    doc = pdf_database.get_document(doc_id)
    if doc is None:
        return {"error": f"document {doc_id} not found"}
    n = pdf_database.delete_document_cascade(doc_id)
    return {"deleted": doc_id, "chunks_removed": n}


# ── cron CLI helpers (mirror cli_run_rule / cli_run_indicator) ───


def cli_ingest(target: str) -> int:
    """Ingest <path|url> in-process, print JSON summary, return exit code."""
    result = ingest_document(
        file_path=target if not target.startswith("http") else None,
        url=target if target.startswith("http") else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if "error" not in result else 1


def cli_search(query: str) -> int:
    """Run search in-process, print JSON, return exit code."""
    result = search_documents(query)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if "error" not in result else 1

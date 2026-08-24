"""Hermetic self-check for the pdf group.

No network, no model download. Uses :class:`embedding_client.FakeEmbedder`
(deterministic bag-of-words vectors) + a real ``sqlite-vec`` extension on a
temp SQLite DB. Exercises:
  1. pdf_ingest_text (chunk + embed + persist)
  2. SHA-256 dedup (re-ingest same text -> deduped=true)
  3. pdf_ingest_document (PDF extraction path via a generated PDF)
  4. pdf_search_documents ranking (relevant doc ranks first)
  5. doc_id filter
  6. pdf_list_documents / pdf_get_document
  7. pdf_delete_document cascade
  8. dim-mismatch error after a model swap

Run: ``fd-daas-mcp/.venv/bin/python fd-daas-mcp/pdf-mcp/selfcheck.py``
(requires the ``[pdf]`` extra / sqlite-vec installed).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Point pdf_database at a temp DB BEFORE it is imported, and add this dir to
# sys.path so `import pdf_database` / `import embedding_client` resolve.
_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))
_TMPDIR = tempfile.mkdtemp(prefix="pdf-selfcheck-")
os.environ["DAAS_DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"
# fpdf2 writes PDFs to the same tmpdir.
os.environ.setdefault("XDG_CACHE_HOME", _TMPDIR)

import embedding_client  # noqa: E402
import pdf_database  # noqa: E402
import pdf_tools  # noqa: E402

# Reset the pdf_database singleton so it picks up the temp DB (it may have been
# initialized against the real daas.db by an earlier import in the same process).
pdf_database._engine = None
pdf_database._Session = None
# Deterministic embedder: no torch, no model download, no network.
embedding_client.set_embedder(embedding_client.FakeEmbedder(dim=256))

_PASSED: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")
    _PASSED.append(ok)
    return ok


def _make_pdf(path: str, lines: list[str]) -> None:
    from fpdf import FPDF  # type: ignore

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    for line in lines:
        pdf.cell(0, 10, txt=line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(path)


def main() -> int:
    # 1. ingest_text
    text_a = (
        "Revenue grew 20 percent year over year. The company expanded into new "
        "markets and improved profit margins. Cloud services drove most of the growth."
    )
    r = pdf_tools.ingest_text(text=text_a, name="docA")
    check("ingest_text", r.get("status") == "active" and r.get("chunk_count", 0) > 0
          and not r.get("deduped"), str(r))
    doc_a = r.get("doc_id")

    # 2. dedup - identical text returns the same doc_id
    r2 = pdf_tools.ingest_text(text=text_a, name="docA-dup")
    check("dedup", r2.get("deduped") is True and r2.get("doc_id") == doc_a, str(r2))

    # 3. ingest a distinct text doc
    text_b = (
        "The factory produced ten thousand units. Manufacturing costs fell due to "
        "automation. Supply chain remained stable throughout the quarter."
    )
    rb = pdf_tools.ingest_text(text=text_b, name="docB")
    check("ingest_text_b", rb.get("status") == "active" and rb.get("doc_id") != doc_a, str(rb))
    doc_b = rb.get("doc_id")

    # 4. pdf_ingest_document - generate a PDF and ingest via the extraction path
    pdf_path = f"{_TMPDIR}/sample.pdf"
    _make_pdf(pdf_path, [
        "Product Launch Announcement.",
        "The new mobile app was released this quarter.",
        "User engagement metrics improved across all regions.",
    ])
    rd = pdf_tools.ingest_document(file_path=pdf_path, name="sample.pdf")
    check("ingest_document", rd.get("status") == "active" and rd.get("chunk_count", 0) > 0
          and rd.get("source_type") == "file", str(rd))
    doc_pdf = rd.get("doc_id")

    # 5. search ranking - "revenue growth" should rank docA first
    s = pdf_tools.search_documents(query="revenue growth", top_k=5)
    top = s.get("results", [])
    check("search_returns_results", s.get("count", 0) > 0 and len(top) > 0, str(s)[:200])
    check("search_ranking", bool(top) and top[0].get("doc_name") == "docA",
          f"top={top[0].get('doc_name') if top else None}")

    # 6. doc_id filter - restrict to docB
    sf = pdf_tools.search_documents(query="factory units", top_k=5, doc_id=doc_b)
    filtered_ok = all(r.get("doc_id") == doc_b for r in sf.get("results", []))
    check("search_doc_id_filter", filtered_ok and sf.get("count", 0) > 0, str(sf)[:200])

    # 7. list_documents + get_document
    lst = pdf_tools.list_documents()
    check("list_documents", lst.get("count", 0) >= 3, f"count={lst.get('count')}")
    gd = pdf_tools.get_document(doc_pdf)
    check("get_document", gd.get("doc_id") == doc_pdf and "sample_chunks" in gd, str(gd)[:160])

    # 8. delete cascade - chunks + vectors gone, search no longer returns it
    dl = pdf_tools.delete_document(doc_pdf)
    check("delete_document", dl.get("deleted") == doc_pdf and dl.get("chunks_removed", 0) > 0, str(dl))
    sd = pdf_tools.search_documents(query="revenue", top_k=20)
    check("delete_cascade", all(r.get("doc_id") != doc_pdf for r in sd.get("results", [])),
          f"deleted doc still in results? {[r.get('doc_id') for r in sd.get('results', [])]}")

    # 9. dim-mismatch error after a model swap
    embedding_client.set_embedder(embedding_client.FakeEmbedder(dim=128))  # different dim
    sm = pdf_tools.search_documents(query="revenue")
    check("dim_mismatch_error", "error" in sm and "mismatch" in sm.get("error", "").lower(),
          str(sm)[:160])
    # restore for any later checks
    embedding_client.set_embedder(embedding_client.FakeEmbedder(dim=256))

    ok = all(_PASSED)
    print(f"\n{sum(_PASSED)}/{len(_PASSED)} checks passed")
    print("=== PDF SELF-CHECK PASSED ===" if ok else "=== PDF SELF-CHECK FAILED ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Embedding backends for the pdf group (local, no egress).

Two backends behind one ``Embedder`` interface:
  - ``SentenceTransformersEmbedder`` (default) - wraps
    ``sentence_transformers.SentenceTransformer``.
  - ``FastembedEmbedder`` (alternative, ONNX, lighter) - wraps
    ``fastembed.TextEmbedding``.

Selection via ``PDF_EMBEDDING_BACKEND`` (default ``sentence-transformers``) and
``PDF_EMBEDDING_MODEL`` (default ``BAAI/bge-m3``) from the repo-root ``.env``.

The backend module is imported lazily inside the constructor so the pdf group
loads (and the server starts) without the ``[pdf]`` extra; a missing backend
raises :class:`EmbedderError`, which ``pdf_tools`` catches and surfaces as a
structured ``{"error": ...}``.

All embeddings are L2-normalized so sqlite-vec's L2 distance ranking is
equivalent to cosine ranking. No document content is transmitted off-machine;
the only network access is the one-time model download from HuggingFace on
first use (a model fetch, not document egress).
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Protocol

# fd-daas-mcp/pdf-mcp/embedding_client.py -> parents[2] = repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


class EmbedderError(Exception):
    """Raised when the selected backend module is not importable."""


class Embedder(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
    def dim(self) -> int: ...
    def model_name(self) -> str: ...


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


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def _deterministic_hash(s: str) -> int:
    """Deterministic string hash. Python's builtin ``hash()`` is randomized per
    process (PYTHONHASHSEED), which would make FakeEmbedder vectors - and thus
    selfcheck search ranking - non-reproducible across runs. This is stable."""
    h = 0
    for c in s:
        h = (h * 131 + ord(c)) & 0x7FFFFFFF
    return h


class SentenceTransformersEmbedder:
    """Default backend. Wraps sentence_transformers.SentenceTransformer."""

    def __init__(self, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as e:  # pragma: no cover - dep-gated
            raise EmbedderError(
                "sentence-transformers not installed: uv sync --extra pdf "
                "(or pip install sentence-transformers)"
            ) from e
        self._model_name = model_name
        self._model = SentenceTransformer(model_name)
        # sentence-transformers renamed get_sentence_embedding_dimension ->
        # get_embedding_dimension (st>=5); fall back for older versions.
        _dim_fn = getattr(self._model, "get_embedding_dimension", None) or \
            self._model.get_sentence_embedding_dimension
        self._dim = int(_dim_fn())

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = self._model.encode(texts, normalize_embeddings=True)
        # `vecs` is a numpy array; convert to plain python lists of floats.
        return [[float(x) for x in v] for v in vecs]

    def dim(self) -> int:
        return self._dim

    def model_name(self) -> str:
        return self._model_name


class FastembedEmbedder:
    """Alternative ONNX backend (lighter, no torch). Wraps fastembed.TextEmbedding."""

    def __init__(self, model_name: str) -> None:
        try:
            from fastembed import TextEmbedding  # type: ignore
        except ImportError as e:  # pragma: no cover - optional
            raise EmbedderError(
                "fastembed not installed: pip install fastembed"
            ) from e
        self._model_name = model_name
        self._model = TextEmbedding(model_name=model_name)
        # fastembed does not expose dim directly; probe with a dummy.
        self._dim = len(next(self._model.embed(["probe"])))

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [_normalize([float(x) for x in v]) for v in self._model.embed(texts)]

    def dim(self) -> int:
        return self._dim

    def model_name(self) -> str:
        return self._model_name


class FakeEmbedder:
    """Deterministic, dependency-free embedder for hermetic selfcheck/tests.

    Maps each text to a deterministic vector via a cheap hash -> dim projection.
    Identical texts produce identical vectors (so dedup/exact-match search works);
    texts sharing tokens land nearby (so ranking is non-random). No network, no
    model download.
    """

    def __init__(self, model_name: str = "fake-embedder", dim: int = 64) -> None:
        self._model_name = model_name
        self._dim = dim

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * self._dim
            tokens = [w for w in t.lower().split() if w]
            for tok in tokens:
                h = _deterministic_hash(tok) % self._dim
                vec[h] += 1.0
            # if empty text, leave zero vector
            out.append(_normalize(vec))
        return out

    def dim(self) -> int:
        return self._dim

    def model_name(self) -> str:
        return self._model_name


_EMBEDDER: Any = None


def get_embedder() -> Any:
    """Return the configured embedder singleton (lazy, cached).

    Raises EmbedderError if the backend module is not importable.
    """
    global _EMBEDDER
    if _EMBEDDER is None:
        _load_dotenv()
        backend = os.environ.get("PDF_EMBEDDING_BACKEND", "sentence-transformers").strip().lower()
        model = os.environ.get("PDF_EMBEDDING_MODEL", "BAAI/bge-m3").strip()
        if backend == "fastembed":
            _EMBEDDER = FastembedEmbedder(model)
        else:
            _EMBEDDER = SentenceTransformersEmbedder(model)
    return _EMBEDDER


def set_embedder(embedder: Any) -> None:
    """Override the singleton (for tests / hermetic selfcheck)."""
    global _EMBEDDER
    _EMBEDDER = embedder


def reset_embedder() -> None:
    """Clear the cached singleton so the next get_embedder() re-reads env."""
    global _EMBEDDER
    _EMBEDDER = None

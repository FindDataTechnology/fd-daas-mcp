"""process tools (daas-mcp) — multi-model LLM extraction (long text + images).

Relocated from the former process-mcp. No SDK: calls an OpenAI-compatible
/chat/completions endpoint via httpx. Reuses the LLM_API_KEY / LLM_BASE_URL /
LLM_MODEL env convention (see cnreport_tools.llm_config), generalized to a
named model registry.

Model registry:
  PROCESS_MODELS env var (JSON): {name: {model, vision?, base_url?, api_key?}}
  Unset → single "default" model from LLM_MODEL / LLM_API_KEY / LLM_BASE_URL.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx

_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # ponytail: hard cap; add resize when callers hit it

_DEFAULT_MODEL = "gpt-4o"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProcessError(Exception):
    """Raised for configuration / validation failures (not surfaced as tool crashes)."""


# ── model registry ──────────────────────────────────────────────

_MODELS: Optional[dict] = None


def load_models() -> dict:
    """Parse PROCESS_MODELS JSON (or fall back to single-model env). Cached."""
    global _MODELS
    if _MODELS is not None:
        return _MODELS

    raw = os.environ.get("PROCESS_MODELS", "").strip()
    shared_key = os.environ.get("LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
    shared_base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    shared_model = os.environ.get("LLM_MODEL", os.environ.get("OPENAI_MODEL", _DEFAULT_MODEL))

    if raw:
        try:
            registry = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ProcessError(f"PROCESS_MODELS is not valid JSON: {e}")
        models: dict[str, dict] = {}
        for name, spec in registry.items():
            spec = spec or {}
            models[name] = {
                "model": spec.get("model", shared_model),
                "vision": bool(spec.get("vision", False)),
                "base_url": (spec.get("base_url") or shared_base).rstrip("/"),
                "api_key": spec.get("api_key") or shared_key,
            }
        _MODELS = models
        return _MODELS

    # ponytail: single-model fallback when PROCESS_MODELS unset
    _MODELS = {
        "default": {
            "model": shared_model,
            "vision": False,
            "base_url": shared_base,
            "api_key": shared_key,
        }
    }
    return _MODELS


def resolve_model(name: Optional[str] = None) -> dict:
    """Return the named model cfg (or the first/default). Raises ProcessError
    if the model is unknown or has no api_key/base_url (so callers fail before
    any network call)."""
    models = load_models()
    if not models:
        raise ProcessError("no models configured (set PROCESS_MODELS or LLM_API_KEY)")
    if name is None:
        cfg = next(iter(models.values()))
    else:
        cfg = models.get(name)
        if cfg is None:
            raise ProcessError(f"unknown model '{name}'; available: {list(models)}")
    label = name or "default"
    if not cfg.get("api_key"):
        raise ProcessError(f"model '{label}' has no api_key (set LLM_API_KEY or per-model api_key)")
    if not cfg.get("base_url"):
        raise ProcessError(f"model '{label}' has no base_url (set LLM_BASE_URL or per-model base_url)")
    return cfg


def list_models() -> dict:
    """Public model list (api keys never serialized)."""
    models = load_models()
    return {
        "models": [
            {"name": n, "model": c["model"], "vision": c["vision"], "base_url": c["base_url"]}
            for n, c in models.items()
        ]
    }


# ── LLM call ────────────────────────────────────────────────────

def _chat(model_cfg: dict, system: str, user_content: Any, json_mode: bool = True) -> str:
    """POST {base_url}/chat/completions; return the assistant message content."""
    url = model_cfg["base_url"] + "/chat/completions"
    payload: dict = {
        "model": model_cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {model_cfg['api_key']}"}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=120.0)
    except httpx.HTTPError as e:
        raise ProcessError(f"HTTP request failed: {e}")

    # Some OpenAI-compatible servers reject response_format → retry without it.
    if json_mode and resp.status_code >= 400 and "response_format" in resp.text:
        payload.pop("response_format", None)
        resp = httpx.post(url, json=payload, headers=headers, timeout=120.0)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _strip_json_fence(text: str) -> str:
    """Strip a leading ```json ... ``` fence if present."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t


def _array_schema(item_schema: dict) -> dict:
    return {"type": "array", "items": item_schema}


def _validate(records: Any, schema: dict) -> Optional[str]:
    """Return None if records validate against schema, else a short error."""
    try:
        import jsonschema

        jsonschema.validate(instance=records, schema=_array_schema(schema))
        return None
    except ImportError:
        return None  # ponytail: no jsonschema installed → skip validation
    except Exception as e:
        return str(e)


def _extract_once(
    model_cfg: dict, system: str, user_content: Any, schema: dict
) -> tuple[Optional[list], Optional[str]]:
    """One extraction attempt. Returns (records, None) or (None, err)."""
    try:
        content = _chat(model_cfg, system, user_content, json_mode=True)
    except httpx.HTTPStatusError as e:
        # response_format rejection already handled in _chat; if still failing,
        # retry once without json_mode and parse a fenced block.
        try:
            content = _chat(model_cfg, system, user_content, json_mode=False)
        except Exception as e2:
            return None, f"LLM call failed: {e2}"
    try:
        data = json.loads(_strip_json_fence(content))
    except json.JSONDecodeError:
        return None, "model did not return valid JSON"
    records = data.get("records") if isinstance(data, dict) else data
    if not isinstance(records, list):
        return None, "model did not return a records array"
    err = _validate(records, schema)
    if err:
        return None, err
    return records, None


def extract_with_retry(
    model_cfg: dict, system: str, user_content: Any, schema: dict
) -> tuple[Optional[list], Optional[str]]:
    """Extract with one retry on failure (mirror cnreport.ai_extract._attempt)."""
    records, err = _extract_once(model_cfg, system, user_content, schema)
    if records is None and err is not None:
        records, err = _extract_once(
            model_cfg,
            system + " Your previous output was invalid; fix it and return strict JSON conforming to the schema.",
            user_content,
            schema,
        )
    return records, err


# ── ad-hoc extraction ───────────────────────────────────────────

_SYSTEM = (
    "You extract structured data from the provided content. "
    "Return ONLY a JSON object with a 'records' array matching the given schema. "
    "Do not include any prose."
)


def _split_chunks(text: str, max_chars: int) -> list[str]:
    """Split text into <=max_chars chunks, preferring paragraph boundaries."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    for para in text.split("\n\n"):
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
        else:
            # hard-cut a too-long paragraph
            for i in range(0, len(para), max_chars):
                chunks.append(para[i : i + max_chars])
    # merge tiny chunks back up to max_chars
    merged: list[str] = []
    for c in chunks:
        if merged and len(merged[-1]) + len(c) + 2 <= max_chars:
            merged[-1] += "\n\n" + c
        else:
            merged.append(c)
    return merged or [text]


def extract_text(
    text: str,
    schema: dict,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    max_chars: int = 12000,
) -> dict:
    """Extract structured records from (possibly long) text via an LLM.

    Long text is chunked map-reduce style (no truncation): each chunk is
    extracted against `schema`, then a merge pass consolidates them.
    """
    try:
        model_cfg = resolve_model(model)
    except ProcessError as e:
        return {"error": str(e)}

    system = _SYSTEM + (f" {prompt}" if prompt else "")
    chunks = _split_chunks(text, max_chars)

    if len(chunks) == 1:
        records, err = extract_with_retry(model_cfg, system, chunks[0], schema)
        if records is None:
            return {"error": "extraction failed", "detail": err, "chunk_count": 1}
        return {"records": records, "count": len(records), "chunk_count": 1}

    # map: per-chunk extraction
    per_chunk: list[list] = []
    failures: list[str] = []
    for i, chunk in enumerate(chunks):
        recs, err = extract_with_retry(model_cfg, system, chunk, schema)
        if recs is None:
            failures.append(f"chunk {i}: {err}")
        else:
            per_chunk.append(recs)

    if not per_chunk:
        return {
            "error": "all chunks failed",
            "detail": failures,
            "chunk_count": len(chunks),
        }

    # reduce: merge pass consolidates per-chunk arrays into one
    all_records = [r for recs in per_chunk for r in recs]
    merge_system = (
        "You consolidate extracted records from multiple chunks of one document into a single "
        "deduplicated array. Return ONLY a JSON object with a 'records' array conforming to the "
        "given schema. Drop duplicates (same logical entity). Do not include any prose."
    )
    merge_user = json.dumps(
        {"schema": schema, "records": all_records}, ensure_ascii=False
    )
    merged, merr = extract_with_retry(model_cfg, merge_system, merge_user, schema)
    if merged is None:
        # ponytail: heuristic merge failed → return the union and flag it
        return {
            "records": all_records,
            "count": len(all_records),
            "chunk_count": len(chunks),
            "merge_notes": f"merge pass failed ({merr}); returned union, duplicates possible",
        }
    return {
        "records": merged,
        "count": len(merged),
        "chunk_count": len(chunks),
        "merge_notes": "merged and deduplicated" if len(merged) < len(all_records) else "merged",
    }


def _encode_image(image: str) -> tuple[str, str]:
    """Return (data_url, media_type). Accepts URL, local path, or raw base64."""
    if image.startswith("http://") or image.startswith("https://"):
        return image, "url"
    p = Path(image)
    if p.exists() and p.is_file():
        data = p.read_bytes()
        if len(data) > _MAX_IMAGE_BYTES:
            raise ProcessError(
                f"image is {len(data)} bytes, exceeds max_image_bytes ({_MAX_IMAGE_BYTES})"
            )
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}", mime
    # assume raw base64
    if len(image) > _MAX_IMAGE_BYTES:
        raise ProcessError(
            f"image base64 is {len(image)} bytes, exceeds max_image_bytes ({_MAX_IMAGE_BYTES})"
        )
    return f"data:image/png;base64,{image}", "image/png"


def extract_image(
    image: str,
    schema: dict,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Extract structured records from an image via a vision-capable model."""
    try:
        model_cfg = resolve_model(model)
    except ProcessError as e:
        return {"error": str(e)}
    if not model_cfg.get("vision"):
        return {"error": f"model '{model or 'default'}' does not support vision"}

    try:
        url, _media = _encode_image(image)
    except ProcessError as e:
        return {"error": str(e)}

    system = _SYSTEM + (f" {prompt}" if prompt else "")
    user_content = [
        {"type": "text", "text": "Extract records from this image per the schema."},
        {"type": "image_url", "image_url": {"url": url}},
    ]
    records, err = extract_with_retry(model_cfg, system, user_content, schema)
    if records is None:
        return {"error": "extraction failed", "detail": err}
    return {"records": records, "count": len(records)}


def _read_file_text(path: str) -> str:
    """Read a local .txt/.md/.pdf to text. Raises ProcessError on bad type."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise ProcessError(f"file not found: {path}")
    suffix = p.suffix.lower()
    if suffix in (".txt", ".md"):
        return p.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise ProcessError(f"pypdf not installed: {e}")
        reader = PdfReader(str(p))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    raise ProcessError(f"unsupported file type: {suffix}")


def extract_file(
    path: str,
    schema: dict,
    prompt: Optional[str] = None,
    model: Optional[str] = None,
    max_chars: int = 12000,
) -> dict:
    """Read a local text/markdown/pdf file and extract structured records."""
    try:
        text = _read_file_text(path)
    except ProcessError as e:
        return {"error": str(e)}
    return extract_text(text, schema, prompt=prompt, model=model, max_chars=max_chars)

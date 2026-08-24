"""Thin CLI sidecar that the dashboard's `/api/chat` route invokes when the
LLM emits a `daas_fetch_data` tool call. Mirrors `daas_tools.fetch_data` but
takes a single JSON payload on argv.

Usage:
  python fetch_data_cli.py --json '{"function": "ckan_package_search",
                                    "params": {"q": "air quality"}}'

  Equivalent form (legacy / explicit):
  python fetch_data_cli.py --json '{"source": "ckan",
                                    "function": "ckan_package_search",
                                    "params": {"q": "..."}}'

`source` is optional — the SourceRouter dispatches by function prefix, so the
function name carries the source. We accept `source` for symmetry with the
chat tool schema and ignore it.

Output: one JSON line on stdout (the serialized fetch result). On error,
exit code != 0 and `{"error": "..."}` on stderr (also mirrored to stdout).
ponytail: shells the same code path the MCP tool already uses.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=True)

from daas_tools import fetch_data  # noqa: E402


def _fail(msg: str) -> None:
    payload = json.dumps({"error": msg})
    print(payload, file=sys.stderr)
    print(payload)
    sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", dest="json_args", default="{}")
    ns = parser.parse_args(argv)

    try:
        args = json.loads(ns.json_args)
    except json.JSONDecodeError as e:
        _fail(f"Invalid --json payload: {e}")
        return 1

    function_name = args.get("function") or args.get("function_name")
    if not function_name:
        _fail("Missing required arg: 'function'")
        return 1

    params = args.get("params") or {}
    if not isinstance(params, dict):
        _fail("'params' must be a JSON object")
        return 1

    result = fetch_data(function_name, json.dumps(params))
    print(json.dumps(result, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

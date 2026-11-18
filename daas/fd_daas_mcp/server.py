"""Consolidated FastMCP server - registers every tool from the registry under
the ``<group>_<tool>`` namespace and runs over stdio or Streamable HTTP.

Entry: ``python -m daas.fd_daas_mcp.server`` (the ``.mcp.json`` launch).
The ``fd-daas-mcp`` console script (``cli:cli``) is the CLI; both consume the
same :mod:`registry` so they cannot drift.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import hmac
from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.middleware import Middleware

from daas.fd_daas_mcp import registry

REPO = Path(__file__).resolve().parents[3]  # repo root
load_dotenv(REPO / ".env")
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("fd-daas-mcp")

# The single FastMCP app the consolidated server runs. The registry harvests
# per-group tool functions and we register them on this `app`; the inline=True
# groups (alerts/cron/dashboard) also have their own per-module `app` (from
# their own server.py) but those module-level FastMCP instances are never
# served - their @app.tool decorator's only job is to attach the function
# object to that module so registry.load_source() can extract it back out.
app = FastMCP("fd-daas-mcp")


class BearerAuthMiddleware:
    """ASGI middleware gating HTTP requests behind a bearer token.

    Active only when ``MCP_BEARER_TOKEN`` is set (read per-request): every
    request must carry ``Authorization: Bearer <token>`` (constant-time
    compare) or it is rejected with 401 before any MCP handling. stdio
    never passes through here, and an unset/empty token disables the gate.
    Shared pattern with fd-cn-report's server.
    """

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    def __call__(self, scope, receive, send):
        token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
        if scope["type"] != "http" or not token:
            return self.asgi_app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"")
        expected = ("Bearer " + token).encode("utf-8", "surrogateescape")
        if hmac.compare_digest(provided, expected):
            return self.asgi_app(scope, receive, send)

        async def _reject(receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})

        return _reject(receive, send)


# Provision the database (create_all + every group's idempotent init_db()) BEFORE
# the registry is built, so every tool sees a fully-created schema. Logs the
# resolved DB path so a first-time user can see where their database lives. The
# env-var precedence + writable-default (cwd / ~/.fd-daas-mcp/) live in
# daas_database; this just triggers the (idempotent) provisioning eagerly.
try:
    _fd_home = Path(__file__).resolve().parents[2]  # fd-daas-mcp/
    for _sub in ("models", "daas-mcp"):
        _p = _fd_home / _sub
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
    from daas_database import provision_database  # type: ignore
    _db, _db_url = provision_database()
    logger.info("fd-daas-mcp database: %s", _db_url)
except Exception as _e:  # noqa: BLE001 - provisioning must not block server start
    logger.warning("fd-daas-mcp database provisioning failed: %s", _e)


def _register_tools(profile: str | None) -> None:
    """Build the registry under ``profile`` and register every tool on ``app``.

    Deferred to startup (not import time) so ``--profile``/``DAAS_PROFILE`` can
    decide the tool surface; unknown profiles fail fast here.
    """
    try:
        registry.validate_profile(profile)
    except ValueError as e:
        raise SystemExit(f"[fd-daas-mcp] {e}")
    _tools = registry.build(profile=profile)
    for _group, _name, _func in _tools:
        try:
            app.tool(name=registry.namespaced(_group, _name))(_func)
        except Exception as e:  # noqa: BLE001 - record + keep going; surfaced via report
            registry.note_failed(_group, _name, f"{type(e).__name__}: {e}")
            logger.warning("failed to register %s_%s: %s", _group, _name, e)

    _report = registry.build_report(profile)
    logger.info(
        "fd-daas-mcp server (profile=%s): registered=%d failed=%d skipped_optional=%d"
        " skipped_profile=%s",
        profile or "default", len(_report.get("registered", [])),
        len(_report.get("failed", [])), len(_report.get("skipped_optional", [])),
        _report.get("skipped_profile", []),
    )


def _session_idle_timeout() -> float:
    """Idle Streamable-HTTP session timeout in seconds (env-overridable).

    Background (production incident, 2026-10-13/14): sessions accumulated to
    the mcp SDK's hard cap of 10_000 and every new session was refused
    ("Refusing to open a new session") — three MCP servers hit it on the same
    day. Root cause here (fastmcp 4.0.11): its session-manager subclass declares
    ``session_idle_timeout: float | None = None`` and passes it *explicitly* to
    the SDK, overriding the SDK's own 1800s default; nothing ever reaps idle
    sessions. Empirically: 3383 sessions created in 26h with zero "idle timeout"
    log lines. The same class of bug prompted a `setdefault` patch in
    fd-open-data-mcp, which is a no-op against an explicitly-passed key.
    This module therefore passes the timeout explicitly at construction.

    Override with MCP_SESSION_IDLE_TIMEOUT (seconds); 0 or negative keeps the
    SDK default (None = reaping disabled — only for debugging).
    """
    raw = os.environ.get("MCP_SESSION_IDLE_TIMEOUT", "1800").strip() or "1800"
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "MCP_SESSION_IDLE_TIMEOUT=%r is not a number; using 1800", raw
        )
        return 1800.0
    return value if value > 0 else 0.0


def main(transport=None, host=None, port=None, profile=None) -> None:
    """Run the fd-daas-mcp server.

    ``MCP_TRANSPORT`` / ``MCP_HOST`` / ``MCP_PORT`` env vars provide the
    fallbacks; with none set this launches stdio exactly as before
    (``app.run(transport="stdio", show_banner=False)``). With
    ``transport="http"`` the Streamable HTTP endpoint is
    ``http://<host>:<port>/mcp`` (host defaults to 127.0.0.1, port to 8311).
    When ``MCP_BEARER_TOKEN`` is set, HTTP requests must carry the matching
    bearer token (401 otherwise).

    The deployment profile (``--profile`` arg, falling back to
    ``DAAS_PROFILE`` env) selects which tool groups load (ADR-0001); an
    unknown profile refuses to start.

    Note: defaults are ``None`` (not ``"stdio"``/``"127.0.0.1"``/``8311``)
    so the env-var fallbacks actually apply — a default of ``"127.0.0.1"`` is
    truthy and would shadow ``MCP_HOST=0.0.0.0`` when the caller passes no
    argument.
    """
    profile = profile or os.environ.get("DAAS_PROFILE", "").strip() or None
    _register_tools(profile)
    transport = transport or os.environ.get("MCP_TRANSPORT") or "stdio"
    if transport == "stdio":
        app.run(transport="stdio", show_banner=False)
        return

    host = host or os.environ.get("MCP_HOST") or "127.0.0.1"
    if port is None:
        port = int(os.environ.get("MCP_PORT", "8311"))

    # Build the ASGI app explicitly (never app.run(transport="http")): fastmcp
    # 4.0.11's run_http_async exposes no idle-timeout knob, and its default
    # leaves idle session reaping OFF (see _session_idle_timeout docstring).
    import uvicorn

    timeout = _session_idle_timeout()
    token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    middleware = [Middleware(BearerAuthMiddleware)] if token else None
    asgi = _build_http_app(middleware, timeout)
    logger.info(
        "Streamable HTTP serving on %s:%s (session_idle_timeout=%ss)",
        host, port, timeout or "SDK default",
    )
    uvicorn.run(asgi, host=host, port=port)


def _build_http_app(middleware, timeout: float):
    """``app.http_app(...)`` with the idle timeout passed only when supported.

    ``session_idle_timeout`` was added to ``FastMCP.http_app`` after 3.4.x;
    the repo pins ``fastmcp>=3.4.2`` so both shapes occur (local venv 3.4.4,
    production 4.0.11). Passing it unconditionally would TypeError on 3.4.x;
    skipping it on a version that needs it is exactly the bug being fixed.
    """
    import inspect

    kwargs = {"session_idle_timeout": timeout or None}
    params = inspect.signature(app.http_app).parameters
    if "session_idle_timeout" not in params:
        # fastmcp < 4: no per-call knob — patch the manager subclass the same
        # way fd-open-data-mcp does, but with an assignment (not setdefault:
        # the subclass passes the key explicitly, so setdefault never fires —
        # the very trap that let production accumulate 3383 sessions).
        _patch_legacy_session_timeout(timeout)
        kwargs = {}
    if middleware:
        kwargs["middleware"] = middleware
    return app.http_app(**kwargs)


def _patch_legacy_session_timeout(timeout: float) -> None:
    """fastmcp <4 fallback: force an idle timeout on every session manager.

    Runs once per process; a no-op when timeout<=0 (debug escape hatch).
    """
    if timeout <= 0:
        return
    try:
        from fastmcp.server.http import FastMCPStreamableHTTPSessionManager
    except Exception:  # noqa: BLE001 - shape differs across versions
        return
    origin = FastMCPStreamableHTTPSessionManager.__init__
    if getattr(origin, "_fd_daas_idle_patch", False):
        return

    def _init(self, *args, **kwargs):
        kwargs["session_idle_timeout"] = timeout  # assignment, not setdefault
        origin(self, *args, **kwargs)

    _init._fd_daas_idle_patch = True
    FastMCPStreamableHTTPSessionManager.__init__ = _init


def _run_cli() -> None:
    """CLI entry point: parse --transport/--host/--port and dispatch to main()."""
    import click

    @click.group()
    @click.version_option("0.1.0")
    def cli():
        """fd-daas-mcp — consolidated DAAS MCP server."""

    @cli.command()
    @click.option("--transport", type=click.Choice(["stdio", "http"]), default="stdio")
    @click.option("--host", default="127.0.0.1", show_default=True)
    @click.option("--port", default=8311, show_default=True, type=int)
    @click.option("--profile", default=None,
                  help="部署画像 local|cell|dev（缺省=通用组；env DAAS_PROFILE 同义）")
    def serve(transport, host, port, profile):
        """Serve the MCP server."""
        main(transport=transport, host=host, port=port, profile=profile)

    cli()


if __name__ == "__main__":
    # When invoked as `python -m daas.fd_daas_mcp.server [flags]`, dispatch to
    # main() which reads MCP_TRANSPORT/MCP_HOST/MCP_PORT env vars. The CLI
    # entrypoint (`fd-daas-mcp serve --transport ... --host ... --port ...`)
    # in cli.py calls main() directly with parsed args.
    main()

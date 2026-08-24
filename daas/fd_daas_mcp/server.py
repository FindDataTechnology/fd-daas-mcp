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

_tools = registry.build()
for _group, _name, _func in _tools:
    try:
        app.tool(name=registry.namespaced(_group, _name))(_func)
    except Exception as e:  # noqa: BLE001 - record + keep going; surfaced via report
        registry.note_failed(_group, _name, f"{type(e).__name__}: {e}")
        logger.warning("failed to register %s_%s: %s", _group, _name, e)

_report = registry.build_report()
logger.info("fd-daas-mcp server: registered=%d failed=%d skipped_optional=%d",
            len(_report["registered"]), len(_report["failed"]),
            len(_report["skipped_optional"]))


def main(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8311) -> None:
    """Run the fd-daas-mcp server.

    ``MCP_TRANSPORT`` / ``MCP_HOST`` / ``MCP_PORT`` env vars provide the
    fallbacks; with none set this launches stdio exactly as before
    (``app.run(transport="stdio", show_banner=False)``). With
    ``transport="http"`` the Streamable HTTP endpoint is
    ``http://<host>:<port>/mcp`` (host defaults to 127.0.0.1, port to 8311).
    When ``MCP_BEARER_TOKEN`` is set, HTTP requests must carry the matching
    bearer token (401 otherwise).
    """
    transport = transport or os.environ.get("MCP_TRANSPORT") or "stdio"
    if transport == "stdio":
        app.run(transport="stdio", show_banner=False)
        return

    host = host or os.environ.get("MCP_HOST") or "127.0.0.1"
    if port is None:
        port = int(os.environ.get("MCP_PORT", "8311"))

    token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    if not token:
        app.run(transport="http", host=host, port=port)
        return

    import uvicorn
    asgi = app.http_app(middleware=[Middleware(BearerAuthMiddleware)])
    uvicorn.run(asgi, host=host, port=port)


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
    def serve(transport, host, port):
        """Serve the MCP server."""
        main(transport=transport, host=host, port=port)

    cli()


if __name__ == "__main__":
    # Check if we were invoked as CLI with serve subcommand
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        import click
        ctx = click.Context(click.Command("serve"))
        # Parse remaining args as serve options
        remaining = sys.argv[2:] if len(sys.argv) > 2 else []
        try:
            # Use Click's parser to handle --transport --host --port flags
            from click.testing import CliRunner
            runner = CliRunner()
            result = runner.invoke(cli, ["serve"] + remaining, prog_name="fd-daas-mcp", standalone_mode=False)
            if result.exception:
                raise result.exception
        except SystemExit:
            pass
    else:
        # Default: run main() (stdio by default, respects MCP_TRANSPORT env)
        import subprocess
        # Re-invoke with proper Click parsing for serve mode
        import sys
        args = sys.argv[1:]
        transport = None
        host = "127.0.0.1"
        port = 8311

        i = 0
        while i < len(args):
            if args[i] == "--transport" and i + 1 < len(args):
                transport = args[i + 1]
                i += 2
            elif args[i] == "--host":
                host = args[i + 1] if i + 1 < len(args) else host
                i += 2
            elif args[i] == "--port":
                port = int(args[i + 1]) if i + 1 < len(args) else port
                i += 2
            else:
                i += 1

        main(transport=transport or os.environ.get("MCP_TRANSPORT"),
             host=os.environ.get("MCP_HOST") or host,
             port=int(os.environ.get("MCP_PORT", port)))

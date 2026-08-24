"""Persistent fastmcp.Client pool for gateway-mcp's data gateway.

Lazily creates and caches fastmcp.Client instances keyed by upstream name.
Reuses clients across calls instead of spawning a new subprocess each time.
Supports both HTTP (StreamableHttpTransport) and stdio (StdioTransport) transports.

Usage:
    from gateway_client_pool.client_pool import get_client_pool, reset_client_pool

    pool = get_client_pool()
    client = await pool.get_client("fd-open-data-mcp")
    tools = await client.list_tools()
    # client stays alive for subsequent calls; pool manages the lifecycle
"""
from __future__ import annotations

import logging
from typing import Optional

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

from gateway_database import get_gateway_db

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════


def _row_key(row: dict) -> tuple:
    """Extract transport-relevant fields from an upstream row as a hashable key.

    Used to detect config changes (transport switch, URL change, command edit)
    that require closing the old client and opening a new one.
    """
    return (
        row.get("transport"),
        row.get("url"),
        row.get("command"),
        tuple((row.get("args") or []) if row.get("args") else []),
        row.get("cwd"),
    )


def _build_client_from_row(row: dict) -> Client:
    """Build a fastmcp.Client from an upstream dict.

    Mirrors ``gateway_database.build_client()`` so the pool and the per-call
    path produce identical clients.
    """
    if row.get("transport") == "http" and row.get("url"):
        return Client(StreamableHttpTransport(row["url"]))

    import os

    env_override = row.get("env") or None
    if env_override:
        env_override = {**os.environ, **env_override}

    return Client(
        StdioTransport(
            command=row.get("command"),
            args=row.get("args") or [],
            env=env_override,
            cwd=row.get("cwd"),
        )
    )


# ═══════════════════════════════════════════════════════════════
# ClientPool
# ═══════════════════════════════════════════════════════════════


class ClientPool:
    """Pool of persistent fastmcp.Client instances, keyed by upstream name.

    Each client is lazily created (``__aenter__`` called on creation so the
    transport is ready) and reused across calls.  On each ``get_client`` the
    upstream row is re-read from the database; if the transport-relevant fields
    changed, the old client is torn down and a fresh one takes its place.
    Callers must **not** close or exit a client obtained from the pool.

    Thread-safety follows the same convention as ``gateway_database``: the pool
    is expected to be used from a single event-loop thread (the MCP server's
    main loop).  No locks are held.
    """

    def __init__(self) -> None:
        self._clients: dict[str, Client] = {}
        self._keys: dict[str, tuple] = {}

    async def get_client(self, name: str) -> Client:
        """Get (or create) a persistent client for the named upstream.

        Steps:
        1.  Read the upstream row from ``gateway_upstreams``.
        2.  If a cached client exists *and* its row key matches → reuse.
        3.  Otherwise, release the old client and build/start a new one.

        Raises:
            ValueError: upstream not found or disabled.
            RuntimeError: transport connection failed.
        """
        db = get_gateway_db()
        row = db.get_upstream(name)
        if row is None:
            raise ValueError(f"upstream '{name}' not found")
        if not row.get("enabled", False):
            raise ValueError(f"upstream '{name}' is disabled")

        new_key = _row_key(row)
        cached_key = self._keys.get(name)
        cached = self._clients.get(name)

        # Fast path: config unchanged → reuse cached client
        if cached is not None and cached_key == new_key:
            return cached

        # Config changed or first access → build new client
        if cached is not None:
            logger.info("Upstream '%s' config changed, recreating client", name)
            await self.release(name)

        client = _build_client_from_row(row)
        # Enter the client's async context manager so the transport starts
        # (subprocess spawn or HTTP connection). We keep it alive until
        # release() is called.
        try:
            await client.__aenter__()
        except Exception as exc:
            # Pooled-stdio fallback: if the HTTP transport failed to start
            # (endpoint unreachable / bad gateway) AND the row carries stdio
            # launch fields (command/args), flip the row's transport to
            # ``stdio`` so we (and subsequent calls) launch the subprocess
            # instead of retrying the dead HTTP endpoint. A health probe can
            # flip it back to ``http`` once the endpoint recovers.
            if row.get("transport") == "http" and row.get("command"):
                logger.warning(
                    "HTTP transport for upstream '%s' failed (%s: %s); "
                    "falling back to stdio subprocess",
                    name, type(exc).__name__, exc,
                )
                db.set_transport(name, "stdio")
                # Re-read so the row + new_key reflect the committed stdio
                # transport; rebuild the client and retry __aenter__.
                row = db.get_upstream(name)
                new_key = _row_key(row)
                client = _build_client_from_row(row)
                try:
                    await client.__aenter__()
                except Exception:
                    logger.exception(
                        "stdio fallback also failed for upstream '%s'", name
                    )
                    raise
            else:
                logger.exception(
                    "Failed to start transport for upstream '%s' "
                    "(no stdio fallback available)", name
                )
                raise

        self._clients[name] = client
        self._keys[name] = new_key
        logger.debug("Cached client for upstream '%s' (key=%s)", name, new_key)
        return client

    async def release(self, name: str) -> None:
        """Close and remove the cached client for *name*.

        Safe to call for a name that is not currently cached (no-op).
        """
        client = self._clients.pop(name, None)
        self._keys.pop(name, None)
        if client is not None:
            await client.__aexit__(None, None, None)
            logger.info("Released client for upstream '%s'", name)

    async def close_all(self) -> None:
        """Close and remove every cached client.

        Intended for graceful shutdown (server stop, SIGTERM, etc.).
        """
        names = list(self._clients.keys())
        for name in names:
            await self.release(name)
        if names:
            logger.info("Closed all %d cached client(s)", len(names))

    @property
    def active_count(self) -> int:
        """Number of currently cached (connected) clients."""
        return len(self._clients)

    def is_cached(self, name: str) -> bool:
        """Return True if *name* currently has a cached (connected) client."""
        return name in self._clients


# ═══════════════════════════════════════════════════════════════
# module singleton — mirrors gateway_database pattern
# ═══════════════════════════════════════════════════════════════

_client_pool: Optional[ClientPool] = None


def get_client_pool() -> ClientPool:
    """Get or create the module-singleton ``ClientPool``.

    Mirrors ``gateway_database.get_gateway_db()``: the first call creates the
    pool; subsequent calls return the same instance.  Call ``reset_client_pool``
    to tear down.
    """
    global _client_pool
    if _client_pool is None:
        _client_pool = ClientPool()
    return _client_pool


def reset_client_pool() -> None:
    """Reset the module singleton, closing all cached clients.

    This is a **synchronous** convenience for shutdown paths that cannot
    ``await`` (signal handlers, ``atexit``).  If called from an event loop it
    will fail with ``RuntimeError`` — in that path call
    ``await get_client_pool().close_all()`` directly instead.
    """
    global _client_pool
    if _client_pool is not None:
        import asyncio

        try:
            asyncio.run(_client_pool.close_all())
        except RuntimeError:
            # Called from inside a running event loop — the caller should use
            # await get_client_pool().close_all() directly.  We silently skip
            # cleanup in this case rather than crashing.
            pass
        _client_pool = None
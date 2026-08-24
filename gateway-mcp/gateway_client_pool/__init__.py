"""Persistent fastmcp.Client pool for gateway-mcp's data gateway.

Lazily creates and caches fastmcp.Client instances keyed by upstream name,
reused across calls. Supports both HTTP (StreamableHttpTransport) and stdio
(StdioTransport) transports.

Usage:
    from gateway_client_pool import get_client_pool

    pool = get_client_pool()
    client = await pool.get_client("fd-open-data-mcp")
    tools = await client.list_tools()
    # ... reuse across calls ...
    await pool.release("fd-open-data-mcp")
    # or on shutdown:
    await reset_client_pool()
"""

from gateway_client_pool.client_pool import ClientPool, get_client_pool, reset_client_pool

__all__ = ["ClientPool", "get_client_pool", "reset_client_pool"]
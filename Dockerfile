# DAAS/FD-DAAS-MCP — container image for the consolidated MCP server.
# Multi-stage: builder installs deps from ., runtime is slim with site-packages.
# Path logic in registry.py and server.py expects parents[3] from server.py, so we
# preserve a tree like /app/fd-daas-mcp/daas/fd_daas_mcp/server.py -> repo root = /app/fd-daas-mcp.

FROM python:3.12-slim AS builder
WORKDIR /build
COPY . .
# Install dependencies (path-sourced mcp-models + fd-open-data-mcp resolved from ./pyproject.toml)
RUN pip install --no-cache-dir --prefix=/install .

# Runtime stage
FROM python:3.12-slim

# Install system deps needed by sentence-transformers (pdf extra) if enabled
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages
COPY --from=builder /install /usr/local

# Create app directory structure mirroring local layout for parents[3] logic
WORKDIR /app
RUN mkdir -p /app/fd-daas-mcp/daas-mcp /app/fd-daas-mcp/models
COPY daas /app/fd-daas-mcp/daas
COPY daas-mcp /app/fd-daas-mcp/daas-mcp
COPY models /app/fd-daas-mcp/models
COPY alerts-mcp /app/fd-daas-mcp/alerts-mcp
COPY cron-mcp /app/fd-daas-mcp/cron-mcp
COPY composite-mcp /app/fd-daas-mcp/composite-mcp
COPY dashboard-mcp /app/fd-daas-mcp/dashboard-mcp
COPY gateway-mcp /app/fd-daas-mcp/gateway-mcp
COPY research-mcp /app/fd-daas-mcp/research-mcp
COPY workflow-mcp /app/fd-daas-mcp/workflow-mcp
COPY pdf-mcp /app/fd-daas-mcp/pdf-mcp

# Create data directory for SQLite persistence (optional, mounted as PVC on k8s)
RUN mkdir -p /app/data && chmod 755 /app/data

EXPOSE 8311

# Default: run HTTP with bearer auth if MCP_BEARER_TOKEN is set
ENV MCP_TRANSPORT=http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8311

ENTRYPOINT ["python", "-m", "daas.fd_daas_mcp.server"]
CMD ["serve"]

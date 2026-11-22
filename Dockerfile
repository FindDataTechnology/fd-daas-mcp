# DAAS — root-level Dockerfile for Gitee Go CI.
#
# Gitee Go's build@docker step always uses the repo root as build context and
# has no context field, while fd-daas-mcp/Dockerfile expects its own directory
# as context (COPY daas, COPY models, ...). This file rebuilds the identical
# image from the root context by prefixing every COPY with fd-daas-mcp/.
# KEEP IN SYNC with fd-daas-mcp/Dockerfile — any change there (deps, layout,
# env) must be mirrored here.

FROM python:3.12-slim AS builder
WORKDIR /build
# Same pip mitigations as fd-daas-mcp/Dockerfile: bound glibc arenas, prefer
# wheels, skip build isolation, Tsinghua mirror.
ENV MALLOC_ARENA_MAX=1 \
    PIP_PREFER_BINARY=1 \
    PIP_NO_BUILD_ISOLATION=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --prefer-binary --prefix=/install \
    "fastmcp>=4.2.4,<5" \
    "click>=8.0" \
    "pandas>=1.0" \
    "sqlalchemy>=2.0" \
    "python-dotenv>=1.0" \
    "apscheduler>=3.11.2" \
    "fd-open-data-mcp" \
    "uvicorn" \
    "starlette" \
    "httpx"

# Runtime stage
FROM python:3.12-slim

COPY --from=builder /install /usr/local

# Mirror local layout so registry.py's parents[3] logic resolves FD_HOME to
# /app/fd-daas-mcp.
WORKDIR /app
RUN mkdir -p /app/fd-daas-mcp/data
COPY fd-daas-mcp/daas /app/fd-daas-mcp/daas
COPY fd-daas-mcp/daas-mcp /app/fd-daas-mcp/daas-mcp
COPY fd-daas-mcp/models /app/fd-daas-mcp/models
COPY fd-daas-mcp/alerts-mcp /app/fd-daas-mcp/alerts-mcp
COPY fd-daas-mcp/cron-mcp /app/fd-daas-mcp/cron-mcp
COPY fd-daas-mcp/composite-mcp /app/fd-daas-mcp/composite-mcp
COPY fd-daas-mcp/dashboard-mcp /app/fd-daas-mcp/dashboard-mcp
COPY fd-daas-mcp/gateway-mcp /app/fd-daas-mcp/gateway-mcp
COPY fd-daas-mcp/research-mcp /app/fd-daas-mcp/research-mcp
COPY fd-daas-mcp/workflow-mcp /app/fd-daas-mcp/workflow-mcp
COPY fd-daas-mcp/pdf-mcp /app/fd-daas-mcp/pdf-mcp
COPY fd-daas-mcp/pyproject.toml /app/fd-daas-mcp/pyproject.toml

ENV PYTHONPATH=/app/fd-daas-mcp

# Default: run HTTP with bearer auth if MCP_BEARER_TOKEN is set
ENV MCP_TRANSPORT=http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8311
# daas.db lives on the PVC mounted at /app/fd-daas-mcp/data
ENV DAAS_DATABASE_URL=sqlite:////app/fd-daas-mcp/data/daas.db

EXPOSE 8311

WORKDIR /app/fd-daas-mcp
ENTRYPOINT ["python", "-m", "daas.fd_daas_mcp.server"]

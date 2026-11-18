# DAAS/fd-daas-mcp — container image for the consolidated MCP server.
# Run from source tree at /app/fd-daas-mcp so the registry's file-relative
# path discovery (parents[3]/"fd-daas-mcp") resolves to the source tree,
# and so the loose <group>-mcp/ dirs the registry AST-harvests are present.
# Only third-party deps (including fd-open-data-mcp from PyPI) are pip-installed;
# the daas package itself and mcp-models are NOT pip-installed — they live in
# the copied source tree and are imported via PYTHONPATH, mirroring local dev.

FROM python:3.12-slim AS builder
WORKDIR /build
# zihan has only ~1.9 GiB free; pip's parallel resolver + pip-compile wheels
# can OOM-kill the build. Mitigations: MALLOC_ARENA_MAX=1 cuts glibc heap
# arenas, --prefer-binary skips C-extension source builds, --no-build-isolation
# disables pip's per-package build env, and the Tsinghua mirror keeps download
# sockets from piling up while waiting on slow PyPI.
ENV MALLOC_ARENA_MAX=1 \
    PIP_PREFER_BINARY=1 \
    PIP_NO_BUILD_ISOLATION=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --prefer-binary --prefix=/install \
    "fastmcp>=3.4.2" \
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

# Copy installed packages
COPY --from=builder /install /usr/local

# Create app directory structure mirroring local layout for parents[3] logic
# (registry.py: REPO = Path(__file__).resolve().parents[3]; FD_HOME = REPO / "fd-daas-mcp")
WORKDIR /app
RUN mkdir -p /app/fd-daas-mcp/data
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
COPY pyproject.toml /app/fd-daas-mcp/pyproject.toml

# PYTHONPATH=/app/fd-daas-mcp puts the source-tree daas package ahead of
# site-packages so __file__ resolves into the source tree (parents[3] = /app).
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

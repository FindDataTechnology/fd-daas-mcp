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
    "fastmcp>=4.2.4,<5" \
    "click>=8.0" \
    "pandas>=1.0" \
    "sqlalchemy>=2.0" \
    "python-dotenv>=1.0" \
    "apscheduler>=3.11.2" \
    "fd-open-data-mcp" \
    "uvicorn" \
    "starlette" \
    "httpx" \
    "akshare==1.19.1"
# ↑ akshare 只服务**刷新作业**（`daas-mcp/refresh_astock_to_today.py` /
#   `seed_astock_*.py`），服务进程本身不 import 它。2026-11-15 之前它只作为
#   pyproject 的可选 extra 存在，于是集群里**没有任何镜像能跑刷新**——通道 B 的
#   价格表因此从 11-06 冻到 11-15（详见 change1 4.4 注记与
#   reports/channel-b-caliber-measured.md 第七节）。装进同一个镜像而不是另建
#   一个，是因为 daas.db 在 node-bound 的 local-path PVC 上、且 cheap 集群禁放
#   scraw 镜像 ⇒ 刷新作业必须落在这台机、并复用这个已带 sqlalchemy 与 models 的
#   环境。代价是镜像增大（akshare 及其依赖）；没有它，"刷新"这条路根本不存在。
#
#   **为什么钉死 1.19.1 而不是 >=1.18**：2026-11-15 在镜像内实测装到的是
#   1.20.63，而 1.20.46 把 `stock_zh_a_daily` → `stock_zh_a_daily_sina`、
#   `stock_zh_a_hist` → `stock_zh_a_hist_em` 全改了名 ⇒ `seed_astock_price`
#   的 sina 与 eastmoney 两条适配器直接 AttributeError，`fetch_bars` 只剩
#   tencent 能赢（实测日志 `route=tencent`）。而 tencent 的 qfq 是**加法式**、
#   volume 量纲是**手**（sina 是股，差 100 倍）⇒ 新行会以另一种口径与量纲
#   接在 sina 历史上，**价格与成交量列都静默错**。
#   1.19.1 是本机（`fd-paper-trading/.venv`）产出并校验过现有数据的那一版。
#   升级适配器是一个独立变更：要同时改 `seed_astock_price` 的三个 `_fetch_via_*`
#   并重跑质量门，不能在"补个依赖"里顺手做。

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

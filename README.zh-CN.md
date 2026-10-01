# DAAS - Data as a Service（数据即服务）

> **谦面 Facet 产品线** · [寻数 FindData](https://www.finddatatech.cloud/products/facet) 的本地自托管数据平台线 —— 单 SQLite 文件 + 合并 MCP 服务器。

> 📖 **快速开始**：[QUICKSTART.md](QUICKSTART.md)（curl 一键安装 → 用 AI 驱动）· English: [README.md](README.md)

分层式金融/经济/统计数据平台 —— 一个 SQLite 文件（`daas.db`）撑起一个聚合 MCP 服务，数据抓取下沉到 `fd-open-data-mcp` 上游。

> **这是什么？** 一个本地数据平台，把 Python 数据库（`akshare`、`yfinance`、`edgar`、`edinet-tools`、`dartlab`、`world_bank_data`、`ckanapi`）变成可查询、可算指标、可出看板、落地到单个 SQLite 文件的存储。你可以通过 **Claude Code 技能**（调用 workflow manifest 的薄壳）驱动，也可以直接用 **聚合 `fd-daas-mcp` MCP 服务** —— 两条路径读写同一个数据库。

---

## 一条命令 → 让 AI 跑起整个平台

```bash
curl -fsSL https://raw.githubusercontent.com/FindDataTechnology/fd-daas-mcp/master/install.sh | sh
```

这一条命令会克隆 DAAS + `fd-open-data-mcp` 上游、配好两个 venv、初始化 `daas.db`、把 `.mcp.json` 改写成你的本地路径。当它打印 `done: ~/code/DAAS` 时，**161 个 MCP 工具** 和 **18 个 Claude Code 技能** 已部署并接好 —— 无需再做任何设置。

然后在 AI agent 里打开这个目录，用自然语言问：

```bash
cd ~/code/DAAS
claude
```

直接说就行，比如：

- *"拉取 SPY 的日线 OHLC 并算 5 日均线"*
- *"用这些指标做一个看板"*
- *"RSI 上穿 70 时提醒我"*
- *"每天晚上定时跑这个抓取"*

agent 两套接口都已就绪 —— **161 个 MCP 工具**横跨 9 个组（`daas · cron · alerts · dashboard · composite · research · pdf · gateway · workflow`）会从 `.mcp.json` 自动加载，`.claude/skills/` 下的 **18 个技能**是 agent 在任务匹配时自动调用的薄手册（`fd-daas-based-data-fetch` 负责 解析→抓取→落库，`fd-daas-research` 编排一整项研究，等等）。

验证安装是否健康（或者直接让 agent 跑）：

```bash
fd-daas-mcp/.venv/bin/fd-daas-mcp doctor            # 路径 + schema + 行数
fd-daas-mcp/.venv/bin/python -m daas.fd_daas_mcp.selfcheck   # 161 个工具，failed=0
```

一次真实的抓取（你让 agent 抓数据时，它跑的就是这个）：

```bash
uv run python .claude/skills/fd-daas-based-data-fetch/scripts/run_indicator.py SPY_ma5
sqlite3 daas.db "SELECT source, COUNT(*) FROM observations GROUP BY source"
```

> 上面的快速开始命令均已在本仓库验证：`SPY_ma5` 是一条真实的 `indicator_rules` 记录，`fd-daas-mcp` 注册表报告 **9 个来源共 161 个工具**（`failed=0, skipped_optional=1`，即可选的 `pdf` 组）。

环境要求：Python 3.10+ 和 `uv`（脚本会自动安装 `uv`）。环境变量覆盖：`DAAS_DEST`（默认 `~/code/DAAS`）、`DAAS_BRANCH`（默认 `master`）、`FINDDATA_HOME`（默认 `~/finddata`）。`dartlab` 抓取需要 3.12：`uv run --python 3.12 --with dartlab ...`。可选凭据（`HTTP_PROXY`、`EDGAR_IDENTITY`、`EDINET_API_KEY`、`LLM_*`、`ALERTS_FEISHU_WEBHOOK_URL`）放在仓库根目录的 `.env` 里 —— 见 [环境变量](#环境变量)。

> **非 Claude Code 的 MCP 客户端？** 这 161 个工具在任何支持 MCP 的客户端（Cursor、Cline……）都能用。技能只是 Claude Code 的便利层 —— 可选，不是驱动服务的必需项。

<details>
<summary>手动安装（跳过 curl 脚本）</summary>

```bash
git clone -b master https://github.com/FindDataTechnology/fd-daas-mcp.git ~/code/DAAS
cd ~/code/DAAS

# 1. venv（数据库作为依赖声明）
uv sync

# 2. 数据库 —— 创建 daas.db（完整 schema + 无依赖的入门目录）。
#    DAAS_DATABASE_URL 是可选的：不设则默认 ./daas.db（可写 cwd）
#    或 ~/.fd-daas-mcp/daas.db。只有要换位置时才设。
fd-daas-mcp/.venv/bin/fd-daas-mcp init       # 一次性建表 + seed
fd-daas-mcp/.venv/bin/fd-daas-mcp doctor      # 只读健康检查

# 3. .env —— 按需放数据源 key（见环境变量）

# 4. 启动 / 健康检查聚合服务
fd-daas-mcp/bin/fd-daas-mcp-server                       # stdio 服务（.mcp.json 启动的就是这个）
fd-daas-mcp/.venv/bin/python -m daas.fd_daas_mcp.selfcheck   # 注册表 + 工具健康（目标 failed=0）
```

如果跳过了 `install.sh`，`fd-open-data-mcp` 上游仍需单独克隆（它是 `fd-daas-mcp` 的路径依赖）。curl 脚本会帮你做这件事；具体在 `~/finddata` 下的兄弟布局见 `install.sh`。

</details>

> **上游：** `fd-open-data-mcp` 数据抓取器是兄弟仓库，位于 `~/finddata/fd-open-data-mcp`（`install.sh` 会自动克隆）。
>
> **文档站：** 完整的角色化文档在 `docs-site/`（MkDocs Material，中英双语）。本地阅读：`uv run mkdocs serve`（浏览器访问 `/DAAS/`），或严格构建：`uv run mkdocs build --strict`。构建/serve/部署见 [`docs-site/README.md`](docs-site/README.md)。

---

## 架构

严格向下依赖 —— 上层绝不向上伸手。

```
L3  用户 MCP 组合   (composite manifest，在 fd-daas-mcp 进程内服务)
L2  workflow manifest (daas.db 的 workflows 表 + 引擎，用 workflow_run 跑)
L1  fd-daas-mcp       (聚合基础设施：daas/cron/alerts/dashboard/composite/research/pdf/gateway/workflow)
L0  fd-open-data-mcp  (唯一数据抓取上游；基于概念的语义抓取器 + entity 主数据)
```

- **L0 — fd-open-data-mcp**（兄弟仓库）：唯一数据抓取面。基于概念的语义抓取器，带排序/故障转移/缓存；持有 entity 主数据（`entities`、`entity_datasource_links`）。以 HTTP `:8300` 服务（stdio 回退）。取代了原先 11 个按数据源拆分的数据抓取 MCP。
- **L1 — fd-daas-mcp**（本仓库）：聚合 stdio 服务，仓库根 [`.mcp.json`](.mcp.json) 的唯一入口。在**一个服务 + 一个 `fd-daas-mcp` Click CLI**背后暴露 **9 个组共 161 个工具**（`daas · cron · alerts · dashboard · composite · research · pdf · gateway · workflow`）。薄聚合层在 `fd-daas-mcp/daas/fd_daas_mcp/`（`server.py`/`registry.py`/`cli.py`/`selfcheck.py`）；每个组的工具代码在包内 `fd-daas-mcp/<group>-mcp/`。
- **L2 — workflow manifest**：manifest 存在 `daas.db` 的 `workflows` 表（用 `workflow_register` 注册，用 `workflow_run` 运行）。`build_workflow_from_goal` 用 LLM 把自然语言目标分解成 manifest。
- **L3 — 用户 MCP 组合**：一个 composite manifest（`{name, upstreams, tools, workflows, prompt}`）策展出一个命名的 MCP 接口面，在聚合服务进程内服务。CRUD 走 `composite_*_manifest`。

抓取技能（`fd-daas-based-data-fetch`、`fd-daas-fetch-data`、`fd-daas-research`）是薄壳：收集参数 → `workflow_run(name, params)` → 处理 checkpoint。它们不再直接调 Python 数据库 —— 抓取走 L1→L0 下沉。

完整架构、约定与 `daas.db` schema 参考见 [`CLAUDE.md`](CLAUDE.md) 与 [`construction/mcp.md`](construction/mcp.md)。

---

## 项目结构

```
daas/
├── .claude/skills/          # Claude Code 技能（fd-daas-based-data-fetch 是核心抓取壳）
├── fd-daas-mcp/             # 聚合 MCP 服务 —— .mcp.json 唯一入口（161 个工具，9 个组）
│   ├── alerts-mcp/          #   告警规则引擎 + 7 个通知渠道
│   ├── composite-mcp/       #   用户 MCP 组合（策展工具 + 内嵌 workflow + prompt）
│   ├── cron-mcp/            #   任务 + 计划注册表（DB 持久化）
│   ├── daas-mcp/            #   数据源/函数/指标/entity 目录 + 计算 + 规则
│   ├── dashboard-mcp/       #   独立 HTML 看板注册表 + 查询
│   ├── gateway-mcp/         #   L0 上游注册 + 调用路由（原 leader 网关那半）
│   ├── workflow-mcp/        #   基于 manifest 的多步数据 workflow（原 leader workflow 那半）
│   ├── pdf-mcp/             #   本地 PDF/文本语义搜索（sqlite-vec）[可选]
│   ├── research-mcp/        #   持久化研究 bundle（集合 + 指标 + 看板 + 报告）
│   ├── bin/fd-daas-mcp-server      # 启动器
│   └── daas/fd_daas_mcp/   # server.py / registry.py / cli.py / selfcheck.py
├── daas.db                  # 共享 SQLite 数据库（作为演示数据集发布：注册表 + observations + scraw_*）
├── dashboards/              # 独立 HTML 看板（+ index.html、daas.md）
├── construction/            # 架构文档（mcp.md —— L0/L1/L2/L3 分层）
└── .env                     # DAAS_DATABASE_URL、代理、数据源 auth key、LLM 配置……
```

---

## `daas.db` 数据模型

一个 SQLite 文件，路径取自 `DAAS_DATABASE_URL`（相对 `sqlite:///` 路径按仓库根解析；`PRAGMA foreign_keys=ON` 走 FK 级联，`PRAGMA journal_mode=WAL` + `busy_timeout=10000` 避开 "database is locked"）。表按角色分组：

| 角色 | 表 | 装什么 |
|---|---|---|
| **注册表 / 目录** | `sources`、`daas_functions`、`daas_function_columns`、`entities`、`entity_datasource_links`、`indicator_rules` | 数据源/函数/列目录；股票/国家及其在各源的标识；指标绑定（表 + 列 + op + params） |
| **计算序列** | `observations` | 指标输出 —— 每行一个 `(source, function_name, indicator, date)` 点；由 `run_indicator.py` upsert。看板和告警读它。 |
| **抓取的源数据** | `scraw_<slug>` | 一次抓取拉下来的原始行（`upsert.py` 自动建表）。`observations` 由这些*计算*而来。 |
| **集合 + 规则** | `entity_collections*`、`indicator_collections*`、`rules`、`process_results` | 命名的 entity/指标集合 + 加入/移出审计日志；统一 `rules` 存储（json/script/position/llm）驱动成员关系 + LLM 抽取 |
| **MCP 运营态** | `dashboards`、`alert_rules`、`alert_events`、`schedules`、`tasks`、`gateway_upstreams`、`workflows`、`workflow_runs`、`workflow_run_steps`、`composites`、`researches` | 看板注册表、告警引擎、cron 状态、gateway/workflow/composite/research 状态 |

从仓库根直接查：`sqlite3 daas.db "SELECT …"`。

---

## 技能（`.claude/skills/`）

技能是纯 Markdown（`SKILL.md`）+ Python 脚本 —— agent 在任务匹配时自动调用的薄手册。抓取技能收集参数后调用 `workflow_run`；它们不再直接调 Python 数据库（抓取走 L1→L0）。**仓库随附 18 个技能：**

| 技能 | 用途 |
|---|---|
| **`fd-daas-based-data-fetch`** *(核心抓取壳)* | 对 `daas.db` 解析 entity + 指标，再 `workflow_run(name, params)` 经 fd-open-data-mcp 抓取并落库到 `scraw_*` / `observations`。 |
| `fd-daas-fetch-data` | Entity → 覆盖 → 指标的工作流（sqlite3 + 核心脚本）。 |
| `fd-datasource-akshare` | A 股 OHLCV/基本面，走外部 `scraw-akshare` Scrapy 项目。 |
| `fd-daas-research` | 编排 分析 → [集合] → 指标 → 看板 → 持久化为 `research` bundle + markdown 报告。 |
| `fd-daas-brainstorm` | 通过对话澄清研究目标 → `daas-doc/research/<plan>.md`（不写 `daas.db`）。 |
| `fd-daas-indicators-creator` | 把抓取的序列落库到 `scraw_<slug>` 表（手动刷新 —— 无 cron）。 |
| `fd-daas-dashboard-creator` | 构建独立 ECharts HTML 看板并注册。 |
| `fd-daas-dashboard` | 查找 / 打开 / 检视已有看板（只读）。 |
| `fd-daas-entities-collection` / `-creator` | 定义规则驱动的 entity 集合 / 日常集合操作。 |
| `fd-daas-indicators-collection-creator` | 策展指标集合 + 导出 CSV/markdown（带解析后分数）。 |
| `fd-daas-rules-creator` | 编写统一规则（json/script/position/llm）、挂到集合、试跑、同步。 |
| `fd-daas-pdf` | 把 PDF/文本摄入本地向量库（sqlite-vec）并语义搜索。需 `[pdf]` extra。 |
| `openspec-*`（5 个） | 规格驱动的变更生命周期：propose → apply → sync → archive。 |

---

## MCP 工具组（`fd-daas-mcp`）

聚合服务暴露 **9 个组共 161 个工具**（`failed=0, skipped_optional=1`，即可选的 `pdf` 组）。目录是组级粒度（每个工具的细节走服务自身的内省 / `selfcheck`）。

| 组 | 前缀 | 工具数 | 用途 |
|---|---|---|---|
| **daas** | `daas_*` | 87 | 数据源/函数/列/entity/指标目录、指标计算、LLM 抽取、集合、entity 覆盖、统一规则。 |
| **dashboard** | `dashboard_*` | 11 | 独立 HTML 看板注册表（CRUD）、表查询、统计、索引重建。 |
| **alerts** | `alerts_*` | 10 | 基于观测序列的告警规则引擎 + 7 个通知渠道（Telegram/Discord/Slack/Twitter/DingTalk/Feishu/WeCom）。 |
| **cron** | `cron_*` | 13 | DB 持久化的任务 + 计划注册表；临时 `run_now`；执行历史。 |
| **composite** | `composite_*` | 16 | 用户 MCP 组合（L3）：从上游策展工具 + 内嵌 workflow + prompt。 |
| **research** | `research_*` | 9 | 持久化研究 bundle，串联集合/指标/看板/流水线 + markdown 报告。 |
| **gateway** | `gateway_*` | 7 | L0 上游注册 CRUD + 调用路由到 `fd-open-data-mcp`（原 `leader` 网关那半）。 |
| **workflow** | `workflow_*` | 8 | 基于 manifest 的多步数据抓取：register/run/resume/inspect（原 `leader` workflow 那半）。 |
| **pdf** | `pdf_*` | — | 本地 PDF/文本语义搜索（sqlite-vec + sentence-transformers）。可选 —— 以 `sqlite_vec` 导入为开关。 |

> 旧的 `leader` 组已解散：它的网关路由那半成了 `gateway_*`，workflow-manifest 那半成了 `workflow_*`。Harness 注册表 / 快照 / 溯源能力已删除。

启动：`fd-daas-mcp/bin/fd-daas-mcp-server`（stdio）。服务端和 `fd-daas-mcp` CLI 都消费 `registry.build()`，所以两个接口面不会漂移。

---

## 环境变量

单一仓库根 `.env` 持有全部配置；脚本和 MCP 服务自动加载。（标注可选的 key 仅在用到对应功能时才需要。）

| Key | 用途 | 是否必需 |
|---|---|---|
| `DAAS_DATABASE_URL` | 指向 `daas.db` 的 `sqlite:///` URL（相对路径按仓库根解析，或绝对路径）。可选：不设则默认 `./daas.db`（可写 cwd）或 `~/.fd-daas-mcp/daas.db`。运行 `fd-daas-mcp init` 建表。 | 可选 |
| `HTTP_PROXY` | 数据库的出站代理。 | 可选 |
| `EDGAR_IDENTITY` | SEC EDGAR 身份串（`"Name email@domain"`）。 | 用 edgar 时 |
| `EDINET_API_KEY` | 日本 EDINET 文档抓取 key。 | 用 edinet 时 |
| `CKAN_PORTAL_URL` | CKAN 门户基址 URL。 | 用 ckan 时 |
| `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` | 抽取 / workflow 规划器共用的 LLM 端点。 | 用 LLM 功能时 |
| `LEADER_MODELS`、`LEADER_MODEL_HIGH/BALANCE/FAST` | workflow 规划器（`build_workflow_from_goal`）的各档模型覆盖。名字保留；仅描述性标签改为"workflow 规划器"。 | 可选 |
| `ALERTS_FEISHU_WEBHOOK_URL` | 飞书告警渠道的 webhook。 | 用飞书告警时 |
| `DASHBOARD_PORT` | 看板应用端口。 | 可选 |

---

## 给 AI agent 的话

如果你是在本仓库里操作的 AI agent（如 Claude Code）：

- **抓数据走 workflow 路径。** 用 `fd-daas-based-data-fetch`：通过 `sqlite3` 对 `daas.db` 解析 entity + 指标，然后 `workflow_run(name, params)` —— manifest 把抓取下沉到 `gateway_call` → `fd-open-data-mcp`（L0）并落库到 `scraw_<slug>` / `observations`。多步抓取用 `build_workflow_from_goal` 生成 manifest。
- **工作流：** 解析 → 抓取（经 L0）→ 落库。在 `daas.db` 解析 entity+指标；经网关抓取；落进 `scraw_<slug>`（原始）或 `observations`（算好的指标）。
- **其他一切用 MCP 服务** —— 浏览目录、创建指标/集合/规则、cron 排程、告警、构建/查找看板、PDF 语义搜索、composite 编排、研究 bundle。这些是 `fd-daas-mcp` 的工具（9 个组共 161 个）。
- **从仓库根用 `sqlite3` 查 `daas.db`**（`sqlite3 daas.db "…"`）。FK 级联用 `PRAGMA foreign_keys=ON`。
- **权威架构 + schema 参考：** [`CLAUDE.md`](CLAUDE.md)（有 `## daas.db` 列出所有表）和 [`construction/mcp.md`](construction/mcp.md)（L0/L1/L2/L3 分层参考）。

---

## License

Apache 2.0.

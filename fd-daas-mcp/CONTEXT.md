# fd-daas-mcp

DAAS 引擎——謙面（本地）与 wire cell（云端）共用的多组合并 MCP 服务与 CLI。
一个代码库、多个部署画像；本文件只放概念，不放实现。

## Language

**工具组（tool group）**:
能力的装载与命名单元：一个 `*-mcp` 目录（或 daas-mcp 核心目录），registry 按组装载并在合并服务里以 `<组>_<工具名>` 暴露。
_Avoid_: 模块混称（模块是代码组织）、插件混称（无运行时安装语义）

**合并服务（merged server）**:
`daas/fd_daas_mcp/server.py`：经 registry 装载全部激活工具组为一个 FastMCP 端点（stdio 或 http）与一个 Click CLI 的唯一入口；server 与 CLI 共用 registry，面不漂移。
_Avoid_: 网关混称（gateway 是数据面工具组）、主服务泛称

**部署画像（deployment profile）**:
运行环境形态的组级开关：`local`（謙面本地）/ `cell`（wire 云托管 cell，萬星去处同属此画像）/ `dev`（全量）。组声明所属画像，合并服务按画像装载；无 profiles 字段的组为通用组（所有画像加载，缺省行为向后兼容）。代码唯一，分叉被拒绝（ADR-0001）。
_Avoid_: 版本/edition 混称（画像不是分叉）、环境混称（那是 env 变量）、租户混称

**工具面清单（tool surface manifest）**:
`tool_surface.yaml` 对每个工具的 commercial/internal 分级：commercial（可进商业供应面）必须显式逐工具条目，新增工具不得静默商业化。分级与画像正交：分级是商业面保险丝，画像是部署开关。
_Avoid_: 权限混称（那是鉴权）、白名单混称（那是 wire 的共享白名单）

**customer_dataset 组**:
客户自有数据的物化与修正层工具组（ingest 三阶段/预览/修正/删除），cell 画像专属；物化三层 `cust_<key>`/`__base`/`__staging`，行锚与修正语义见 wire 侧契约。
_Avoid_: 数据源混称（datasource 是引擎的连接抽象）、爬虫混称

# One engine, deployment profiles — forking the DAAS engine is rejected

fd-daas-mcp 同时服务謙面（本地产品）与 wire cell（云端 B2B）。云端差异能力（首个案例：wire-customer-local-data 的客户数据集组）通过**组级部署画像**解决：工具组在 registry SOURCES 里声明所属画像（`local`/`cell`；`dev`=全量），合并服务按画像装载；无 profiles 字段的组为通用组，缺省行为向后兼容。代码永远只有一个源。明确拒绝 fork 两个版本——2026-09 的 DAAS 分叉与收敛（「本地为准、三端一致」）已经付过一次同步成本的学费。

## Considered Options

- **fork 两个版本/仓**（云端版 vs 本地版）：每次能力与修复都要双份同步，漂移只是时间问题；且「引擎零改动」的构建纪律（cell-image 整树 COPY）已经证明大家在绕开分叉硬扛。否决。
- **部署画像**（本决策）：新能力 = 新组 + 画像标注；组级门控复用既有 SOURCES 依赖门控与工具面清单机制，谢绝工具级门控（宁严勿滥）。

## Consequences

- 未知画像值一律 fail-fast；未激活组按 pdf 组 skipped_optional 先例记 INFO，不报错。
- `customer_dataset` 组为首个画像组（cell 专属），拆出核心组后 wire 侧工具全名回归 `customer_dataset_*`（无 daas_ 前缀）——wire 契约随组名维护。
- 工具面清单（tool_surface.yaml）与画像正交：清单管「能不能商业化」，画像管「在不在部署里」。
- 謙面不传画像 = 通用组全集，工具集相对现状仅少 customer_dataset 组（它从未属于本地语义）。

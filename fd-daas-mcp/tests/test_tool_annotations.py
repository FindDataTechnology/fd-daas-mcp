"""工具行为属性契约（mcp-tool-annotations，change mcp-surface-truth-up 3.4）。

三件事必须同时成立，否则客户端的自动批准策略就是建在沙子上：

1. **完整性** —— 运行时注册表里的每个工具都带齐四项 hint。注册表由
   构建服务器并列举工具得到，**不是**数装饰器：服务器实际提供什么才是真值。
   本仓尤其如此——工具名来自 `registry._parse_server()` 用 AST 读
   `daas-mcp/server.py`，那个文件里的 `app` 从不执行。
2. **集合相等** —— 运行时名字集合与 `TOOL_ANNOTATIONS` 逐名相等。这抓的是
   「新增工具忘了声明」——它会以静默默认（全 false）通过第 1 条。
3. **与实现一致** —— 写持久状态的工具不得被标只读。`WRITES_STATE_*` 下面的
   期望集是读实现写出来的，不是从注记表反推的；改了工具的副作用而不改声明，
   这个文件就会红。
"""
from __future__ import annotations

import pytest

fastmcp = pytest.importorskip("fastmcp", reason="需要本仓运行时 venv")

from daas.fd_daas_mcp.annotations import HINTS, TOOL_ANNOTATIONS, annotations_for  # noqa: E402

#: 明确写持久状态、且从工具名看不出是写的工具（名字靠动词判会漏）。
WRITES_STATE_BY_NAME_HIDDEN = {
    # 执行取数 → 落提取结果/观测行
    "daas_fetch_data", "daas_extract_text", "daas_extract_image", "daas_extract_file",
    "daas_run_rule", "daas_test_rule", "daas_run_indicator", "daas_calculate_indicator",
    # 网关：会向下游 server 发调用
    "gateway_call_data_mcp",
    # 复合工具：增删上游/工具即改注册表
    "composite_add_upstream", "composite_remove_upstream",
    "composite_add_tool", "composite_remove_tool",
    "composite_add_chained_tool", "composite_remove_chained_tool",
}


def test_annotation_table_covers_registry_exactly():
    """表 ⊇ 每组能注册的名字。

    不能只比 `registry.build()` 的默认画像：`customer_dataset`（profile=cell）与
    `pdf`（optional，靠 sqlite_vec）在这个进程里可能没加载，但它们在别的部署里会
    注册——表缺它们就会在那些部署上 KeyError 失败。所以按**全部组**的声明算。
    """
    import ast
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    groups = {
        "alerts": "alerts-mcp", "composite": "composite-mcp", "cron": "cron-mcp",
        "daas": "daas-mcp", "dashboard": "dashboard-mcp", "gateway": "gateway-mcp",
        "research": "research-mcp", "workflow": "workflow-mcp", "pdf": "pdf-mcp",
        "customer_dataset": "customer-dataset-mcp",
    }
    declared_names: set[str] = set()
    for grp, d in groups.items():
        tree = ast.parse((repo / d / "server.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
                fn = n.value.func
                if isinstance(fn, ast.Attribute) and fn.attr in ("tool", "add_tool") and n.value.args:
                    a = n.value.args[0]
                    if isinstance(a, ast.Name) and a.id != "tool":
                        declared_names.add(f"{grp}_{a.id}")
                    elif isinstance(a, ast.Constant) and isinstance(a.value, str):
                        declared_names.add(f"{grp}_{a.value}")
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in n.decorator_list:
                    t = dec.func if isinstance(dec, ast.Call) else dec
                    if isinstance(t, ast.Attribute) and t.attr == "tool":
                        declared_names.add(f"{grp}_{n.name}")

    table = set(TOOL_ANNOTATIONS)
    assert declared_names - table == set(), f"可注册但未声明：{sorted(declared_names - table)}"
    assert table - declared_names == set(), f"声明了但任何组都不注册：{sorted(table - declared_names)}"


def test_every_entry_declares_all_four_hints():
    for name, hints in TOOL_ANNOTATIONS.items():
        missing = [h for h in HINTS if h not in hints]
        assert not missing, f"{name} 缺 hint: {missing}"
        extra = set(hints) - set(HINTS)
        assert not extra, f"{name} 有契约外的键: {sorted(extra)}"


def test_unknown_name_raises_not_defaults():
    with pytest.raises(KeyError):
        annotations_for("no_such_tool_ever")


def test_write_tools_are_not_marked_readonly():
    offenders = [
        n for n in WRITES_STATE_BY_NAME_HIDDEN
        if n in TOOL_ANNOTATIONS and TOOL_ANNOTATIONS[n]["readOnlyHint"]
    ]
    assert not offenders, f"写状态工具被标只读：{offenders}"


def test_total_split_is_documented():
    """只读/写两侧的数字是公开口径（台账与 surface 都引用），变了要有人看。

    分母 199 = 十个组声明名的并集（161 主面 + 31 customer_dataset + 6 pdf + 1 audit）。
    """
    ro = sum(1 for h in TOOL_ANNOTATIONS.values() if h["readOnlyHint"])
    assert len(TOOL_ANNOTATIONS) == 199, f"表条目数变化（现 199）：{len(TOOL_ANNOTATIONS)}"
    assert ro == 71, f"只读工具数变化（现 71）：{ro}——确认新工具的副作用后同步本断言"

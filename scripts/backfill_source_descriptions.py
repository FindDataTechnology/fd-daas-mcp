#!/usr/bin/env python3
"""把 fd-cn-gov 各 archive 源的 MANIFEST 描述回灌进 daas.db 的 sources 表。

为什么需要它：`sources.description` 是公开目录（facet 的 data-anchor 物种）里
每条数据源的「一句话」。2026-06-30 那批 archive 源是用脚手架灌进去的，描述被
写成了 `Create <name> datasource` 这种占位；而 fd-cn-gov 的每个 scraper 脚本
MANIFEST 里早就有完整描述 —— 真数据一直在上游，只是没走这条链。结果读者在目录
里看到 8 条 `Create pbc_xinwen_archive datasource`。

修的是数据不是代码（写入方已不在当前代码里），所以这里给一个可重放的脚本：
库重建后再跑一次即可。**只填空占位**，任何已经有真实描述的源都不会被覆盖。

用法：
    python3 scripts/backfill_source_descriptions.py --db /path/to/daas.db
    python3 scripts/backfill_source_descriptions.py --db ... --dry-run

源真值是 fd-cn-gov 的 `fd_cn_gov/scripts/*_archive.py::MANIFEST.description`
（本脚本用 AST 读，不 import —— 那些脚本有外部依赖，缺一个就全跑不起来）。
"""

from __future__ import annotations

import argparse
import ast
import sqlite3
import sys
from pathlib import Path

#: 占位形态：脚手架写进去的那一句。
PLACEHOLDER = "Create % datasource"


def manifest_descriptions(scripts_dir: Path) -> dict[str, str]:
    """从每个 scraper 的 MANIFEST 里取 (name, description)。AST 读，不执行。

    为什么要 AST 而不是 import：这些脚本 `import requests` / `scrapling` 等，
    在离线或精简环境里 import 会整批失败 —— 那样这条回灌就只能在「装了全部
    依赖的机器」上跑，等于没有。字符串拼接（相邻字面量）按 Python 语义合并。
    """
    out: dict[str, str] = {}
    for path in sorted(scripts_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "MANIFEST" for t in node.targets):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            kwargs = {kw.arg: kw.value for kw in node.value.keywords if kw.arg}
            name_node, desc_node = kwargs.get("name"), kwargs.get("description")
            if not isinstance(name_node, ast.Constant) or not isinstance(name_node.value, str):
                continue
            try:
                desc = ast.literal_eval(desc_node)
            except (ValueError, SyntaxError):
                continue
            if isinstance(desc, str) and desc.strip():
                out[name_node.value] = " ".join(desc.split())
    return out


def backfill(db_path: Path, scripts_dir: Path, *, dry_run: bool = False) -> list[tuple[str, str]]:
    """把占位描述替换成 MANIFEST 原文。返回 [(name, new_description), …]。"""
    descs = manifest_descriptions(scripts_dir)
    if not descs:
        raise SystemExit(f"no MANIFEST descriptions found under {scripts_dir}")

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name, description FROM sources WHERE description LIKE ?",
            (PLACEHOLDER,),
        ).fetchall()
        changed: list[tuple[str, str]] = []
        for name, _old in rows:
            new = descs.get(name)
            if not new:
                print(f"  ! {name}: placeholder but no MANIFEST description — left alone", file=sys.stderr)
                continue
            changed.append((name, new))
            if not dry_run:
                conn.execute("UPDATE sources SET description = ? WHERE name = ?", (new, name))
        if not dry_run:
            conn.commit()
        return changed
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill daas.db sources.description from fd-cn-gov MANIFESTs")
    ap.add_argument("--db", required=True, help="path to daas.db")
    ap.add_argument(
        "--scripts-dir",
        default=str(Path(__file__).resolve().parents[2] / "fd-cn-gov" / "fd_cn_gov" / "scripts"),
        help="directory holding the scraper scripts (default: sibling fd-cn-gov checkout)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"no such database: {db}")
    changed = backfill(db, Path(args.scripts_dir), dry_run=args.dry_run)

    verb = "would update" if args.dry_run else "updated"
    print(f"{verb} {len(changed)} source description(s)")
    for name, desc in changed:
        print(f"  {name}: {desc[:88]}{'…' if len(desc) > 88 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

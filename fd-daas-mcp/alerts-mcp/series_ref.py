"""把通道 B 的**口径记录**翻成 `PriceSeriesRef`（change1 task 4.4）。

## 为什么这一层必须存在

研究取价读的是本库的 `scraw_<code>_daily` 表，而"这条序列可不可比"的唯一凭据是
`scraw_configs` 里那行 `{"adjust","source","rows"}` 加上表自己的 `max(date)`。
此前 `get_series_latest` 只回 `[{"date","value"}]`——**没有源身份、没有复权口径、
没有水位**，于是三路（研究/交易/结算）在注册表层永远没有共同身份。

## 三条硬纪律（照抄 fd-paper-trading 的 `build_series_ref`，理由同源）

1. **不确定就不出 ref。** 口径标成 `unknown` 不等于"确认不复权"；复权没有锚点
   就说不清两批是否可比。出不来时返回**原因字符串**，让调用方按三态留痕，
   而不是留一个空对象。
2. **发出去之前先验一遍。** 但验的不能是造它的那段代码——那会"我造的我验"恒真。
   所以指纹用的是**逐字节复刻自 `fd_quant.contracts.models` 的实现**（见下），
   并用契约自己算出的向量钉住（`test_alerts_series_ref.py` 里的两个 sha256）。
3. **指纹是身份指纹，不是序列值指纹。** 身份 =（源/标的/口径/水位/锚点/窗口/行数）；
   拿序列值的指纹填会得到"同样输入、不同指纹"，比不出 ref 更糟。

## 窗口与行数描述的是**整张表**，不是这次返回的那一页

`get_series_latest` 的 `limit` 是分页参数，不是数据身份。若把窗口设成返回的那几行，
换个 `limit` 就换指纹，两条读路径就再也比不了。所以这里的 `window_start`/`window_end`/
`row_count` 一律取**表内**的 `min(date)`/`max(date)`/`count(*)`：ref 认的是"这串价格
是哪张表、哪一批、什么口径"，而那一页只是它的一个切片。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

#: 价格表形状。**只认 6 位代码**：美股/宏观表（`scraw_aapl_daily`）走另一套来源，
#: 替它们出 ref 等于替一个没声明过的口径背书。
PRICE_TABLE_RE = re.compile(r"^scraw_(\d{6})_daily$")


def is_price_table(name: str) -> Optional[str]:
    """`scraw_<6位>_daily` → 6 位代码；其余返回 None。"""
    m = PRICE_TABLE_RE.match(name or "")
    return m.group(1) if m else None

#: 契约的 schema 版本。与 `fd_quant.contracts.models.SUPPORTED_SCHEMA_VERSION` 同步；
#: 上游改版本时这里的向量测试会红，那时连 `_VECTORS` 一起改。
SUPPORTED_SCHEMA_VERSION = "1"

#: `scraw_configs.columns_json.source` → (类别, 该类的契约口径)。
#: **不在表里就不出 ref**：换了抓取路径而没有更新登记，没人能替它背书。
SOURCE_CLASS = {
    "stock_zh_a_hist": ("stock", "qfq"),
    "index_zh_a_hist": ("index", "none"),
}

#: 口径词 → 契约词。只放确定的；`unknown` 与没见过的取值落到 None（= 出不来）。
CALIBER_BY_ADJUST = {
    "qfq": "qfq",
    "hfq": "hfq",
    "none": "none",
    "not_applicable": "not_applicable",
}

_REQUIRED = ("source_id", "symbol", "adjustment", "as_of_max", "fingerprint")


def compute_price_series_fingerprint(
    *,
    source_id: str,
    symbol: str,
    adjustment: str,
    as_of_max: str,
    adjustment_anchor: Optional[str] = None,
    db_source: Optional[str] = None,
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
    row_count: Optional[int] = None,
) -> str:
    """身份指纹：sha256 over canonical JSON。

    **逐字节复刻** `fd-quant/fd_quant/contracts/models.py` 的同名函数（canonical
    JSON = `sort_keys=True, separators=(",",":"), ensure_ascii=False`；`db_source`
    缺省取 `source_id`；不含浮点）。跨仓复刻的危险是静默分叉，所以
    `test_alerts_series_ref.py` 用上游算出的两个 sha256 当向量——两边算法一变就红。
    """
    payload = {
        "source_id": source_id,
        "db_source": db_source if db_source is not None else source_id,
        "symbol": symbol,
        "adjustment": adjustment,
        "adjustment_anchor": adjustment_anchor,
        "as_of_max": as_of_max,
        "window_start": window_start,
        "window_end": window_end,
        "row_count": row_count,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def check_ref(d: dict) -> list[str]:
    """最小校验：必填字段在场、口径词合法、锚点与复权口径自洽。

    刻意只查**契约里写明的硬规则**，不重复造 ref 的逻辑——它是独立于
    `build_ref` 的一段约束检查（两边同时写反才会同形通过）。
    """
    errs = [f for f in _REQUIRED if not d.get(f)]
    if d.get("adjustment") not in set(CALIBER_BY_ADJUST.values()):
        errs.append("adjustment")
    if d.get("adjustment") in ("qfq", "hfq") and not d.get("adjustment_anchor"):
        errs.append("adjustment_anchor")
    return errs


def build_ref(
    *,
    table: str,
    code: str,
    config: Optional[dict],
    max_date: Optional[str],
    min_date: Optional[str],
    row_count: int,
    db_source: Optional[str] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """造 ref；出不来说明原因。返回 `(ref, None)` 或 `(None, reason)`。

    reason 取值：`no_config` / `bad_config` / `unknown_source` / `caliber_unknown` /
    `no_as_of_max` / `no_adjustment_anchor` / `invalid:<字段>`。
    """
    if not isinstance(config, dict):
        return None, "no_config" if config is None else "bad_config"
    source = config.get("source")
    if source not in SOURCE_CLASS:
        return None, "unknown_source"
    klass, expected = SOURCE_CLASS[source]
    adjust = CALIBER_BY_ADJUST.get(str(config.get("adjust") or "").strip().lower())
    if adjust is None:
        return None, "caliber_unknown"
    if adjust != expected:
        # 记录与端点应有的口径不一致：这不是"可以出个 ref 说明一下"的情形，
        # 是两个事实互相矛盾，出 ref 等于替其中一个背书。
        return None, "caliber_unknown"
    if not max_date:
        return None, "no_as_of_max"
    # 锚点只随前/后复权走。`none`/`not_applicable` 挂一个锚点是噪音，而且会与
    # 登记表里 `index-seed` 那档明确写的 `adjustment_anchor: null` 自相矛盾。
    anchor = str(config.get("created_at") or "")[:10] or None
    if adjust in ("qfq", "hfq"):
        if not anchor:
            # 前复权没有锚点，读者无法判断两批是否可比。宁可不出。
            return None, "no_adjustment_anchor"
    else:
        anchor = None

    source_id = f"daas_akshare_{klass}_{adjust}"
    ref = {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "source_id": source_id,
        "symbol": code,
        "adjustment": adjust,
        "as_of_max": max_date,
        "window_start": min_date,
        "window_end": max_date,
        "row_count": int(row_count),
        "fingerprint": compute_price_series_fingerprint(
            source_id=source_id,
            symbol=code,
            adjustment=adjust,
            as_of_max=max_date,
            adjustment_anchor=anchor,
            db_source=db_source,
            window_start=min_date,
            window_end=max_date,
            row_count=int(row_count),
        ),
    }
    if anchor:
        ref["adjustment_anchor"] = anchor
    if db_source:
        ref["db_source"] = db_source
    errs = check_ref(ref)
    if errs:
        return None, f"invalid:{errs[0]}"
    return ref, None

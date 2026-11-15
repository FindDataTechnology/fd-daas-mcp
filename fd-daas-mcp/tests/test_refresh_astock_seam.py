"""`refresh_astock_to_today._check_seam` 的判据测试。

## 这道守卫为什么必须存在

刷新走 `sp.fetch_bars`，路由是**择优**的（sina → 东财 → tencent）。今天谁赢
不一定等于种子那天谁赢，而两个适配器在接缝那一天上给的数**可能不一样**：

* **qfq 基准**：sina 是乘性、tencent 是加性。分红后前复权会被整体重算，
  存的历史与刚抓的这一批就不是一个基准 —— 接缝处 close 会跳。
* **volume 量纲**：sina 给「股」、tencent 与东财给「手」，**差整 100 倍**。

`sp._validate_series` 只在**本次抓到的窗口内部**判非正与 >±120% 跳变，它看不见
「接缝那一行，库里存的与刚抓的不一样」；成交量更不在它的判据里。2026-11-15 在
集群内实测到了这个形态：镜像里 akshare 1.20.63 把 `stock_zh_a_daily` 与
`stock_zh_a_hist` 都改了名，sina/东财两条适配器直接 AttributeError，只剩
tencent 能用 —— 也就是说**作业一跑就会用另一套口径与量纲接在 sina 历史上**，
而所有既有判据都是绿的。

所以 `_check_seam` 在**写入之前**拿库里那一天的 close/volume 当锚比一遍；
三条结论各有测试：一致 ⇒ None、口径分叉 ⇒ `seam_mismatch`、
量纲差 ⇒ `seam_volume_scale`。

测试只碰纯函数与内存/临时 sqlite：不连生产库、不发网络请求。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # -> fd-daas-mcp/
SCRIPT = os.path.join(REPO, "daas-mcp", "refresh_astock_to_today.py")


def _load():
    """按仓内既有约定用 spec 加载（`daas-mcp/` 不是包）。

    这个模块 import 了 `seed_astock_price`（进而 import sqlalchemy 与 models），
    所以加载前要把 `daas-mcp` 与 `models` 放进 sys.path —— 与生产镜像里的
    目录布局一致（/app/fd-daas-mcp/daas-mcp:/app/fd-daas-mcp）。
    """
    name = "daas_refresh_astock"
    if name in sys.modules:
        return sys.modules[name]
    for extra in (os.path.join(REPO, "daas-mcp"), REPO):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


refresh = _load()

DMAX = "2026-11-13"


def _db(rows) -> tuple[sqlite3.Connection, str]:
    """建一张真的 `scraw_600519_daily` 并把 rows 写进去（guard 会去查它）。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE "scraw_600519_daily" '
                '(date TEXT PRIMARY KEY, "Open" REAL, "High" REAL, '
                '"Low" REAL, "Close" REAL, "Volume" INTEGER)')
    con.executemany('INSERT INTO "scraw_600519_daily" VALUES (?,?,?,?,?,?)', rows)
    con.commit()
    return con, path


def _bars(close, volume, date=DMAX):
    return [{"date": date, "open": close, "high": close, "low": close,
             "close": close, "volume": volume}]


# ------------------------------------------------------------------ 一致

def test_identical_seam_passes():
    con, path = _db([(DMAX, 1, 1, 1, 1453.0, 4284772)])
    try:
        assert refresh._check_seam(con, "600519", DMAX, _bars(1453.0, 4284772)) is None
    finally:
        con.close()
        os.unlink(path)


def test_small_rounding_difference_passes():
    """同一来源的不同抓取批次可能在小数末位有细微差 —— 1% 容差内放行。"""
    con, path = _db([(DMAX, 1, 1, 1, 1453.0, 4284772)])
    try:
        assert refresh._check_seam(con, "600519", DMAX, _bars(1453.29, 4284772)) is None
    finally:
        con.close()
        os.unlink(path)


def test_empty_table_has_no_seam():
    """空表：整段都是新数据，没有接缝可比（返回 None，不拦）。"""
    con, path = _db([])
    try:
        assert refresh._check_seam(con, "600519", None, _bars(1453.0, 1)) is None
    finally:
        con.close()
        os.unlink(path)


# ------------------------------------------------------------------ 口径分叉

def test_close_divergence_is_refused():
    """qfq 基准换了（分红后重算）⇒ 接缝处 close 差 >1%，必须拒绝追加。

    夹具刻意用 1420 vs 1453（-2.27%）：`1441.2` 那种只差 0.81% 的值**在容差内**，
    拿它当"分叉"会让这条测试永远绿（我第一版就是这么写的，实测放行）。
    """
    con, path = _db([(DMAX, 1, 1, 1, 1453.0, 4284772)])
    try:
        why = refresh._check_seam(con, "600519", DMAX, _bars(1420.0, 4284772))
        assert why is not None and why.startswith("seam_mismatch"), why
        assert "2026-11-13" in why and "存 1453" in why
    finally:
        con.close()
        os.unlink(path)


def test_close_divergence_boundary_is_exactly_one_percent():
    """边界：恰好 1% 放行（`<=`），略超 1% 拦下 —— 边界只放一边。"""
    con, path = _db([(DMAX, 1, 1, 1, 100.0, 10)])
    try:
        assert refresh._check_seam(con, "600519", DMAX, _bars(101.0, 10)) is None
        assert refresh._check_seam(con, "600519", DMAX, _bars(101.5, 10)) is not None
    finally:
        con.close()
        os.unlink(path)


# ------------------------------------------------------------------ 量纲

def test_volume_scale_change_is_refused():
    """sina（股）→ tencent（手）：volume 差整 100 倍，close 完全一致。

    这是最危险的一种 —— 价格列看起来毫无问题，成交量列从此混两种单位，
    而任何"非正/跳变"式质量门都看不见成交量。
    """
    con, path = _db([(DMAX, 1, 1, 1, 1453.0, 4284772)])
    try:
        why = refresh._check_seam(con, "600519", DMAX, _bars(1453.0, 42847))
        assert why is not None and why.startswith("seam_volume_scale"), why
        assert "×0.01" in why or "×1e-02" in why, why
    finally:
        con.close()
        os.unlink(path)


def test_volume_scale_boundary_is_inclusive():
    """边界：比值 5 与 1/5 都算通过（`<=`/`>=`），6 倍被拦。"""
    con, path = _db([(DMAX, 1, 1, 1, 100.0, 1000)])
    try:
        assert refresh._check_seam(con, "600519", DMAX, _bars(100.0, 5000)) is None
        assert refresh._check_seam(con, "600519", DMAX, _bars(100.0, 200)) is None
        assert refresh._check_seam(con, "600519", DMAX, _bars(100.0, 6001)) is not None
        assert refresh._check_seam(con, "600519", DMAX, _bars(100.0, 166)) is not None
    finally:
        con.close()
        os.unlink(path)


def test_zero_volume_does_not_refuse():
    """成交量 0（停牌日/源缺失）不该被量纲判据误伤 —— 判不出就不拦。"""
    con, path = _db([(DMAX, 1, 1, 1, 100.0, 1000)])
    try:
        assert refresh._check_seam(con, "600519", DMAX, _bars(100.0, 0)) is None
    finally:
        con.close()
        os.unlink(path)


# ------------------------------------------------------------------ 判不出

def test_missing_seam_row_is_visible_but_not_fatal():
    """抓回来的窗口里没有接缝那一天（停牌/路由不完整）⇒ 留痕，但不判死。

    这条刻意不拦：把「抓取不完整」判成「口径分叉」会让人去重播整表，
    而真正的原因可能只是那天停牌。
    """
    con, path = _db([(DMAX, 1, 1, 1, 100.0, 1000)])
    try:
        why = refresh._check_seam(con, "600519", DMAX, _bars(100.0, 1000, "2026-11-16"))
        assert why is not None and why.startswith("seam_unverifiable"), why
    finally:
        con.close()
        os.unlink(path)


def test_missing_close_in_stored_row_is_not_fatal():
    """库里那行 close 读不到（NULL）⇒ 判不出，交给质量门，不在此拦。"""
    con, path = _db([(DMAX, None, 1, 1, None, 1000)])
    try:
        assert refresh._check_seam(con, "600519", DMAX, _bars(100.0, 1000)) is None
    finally:
        con.close()
        os.unlink(path)


# ------------------------------------------------------------------ 接线

def test_seam_hit_makes_the_run_exit_nonzero():
    """接缝命中必须让整轮**非零退出**，否则 CronJob 是绿的、缺陷只在日志里。

    这一条钉的是 `main()` 的返回式：`1 if (errors or seamed) else 0`。
    直接读源码是刻意的 —— 跑 `main()` 要真抓网络，而这里要钉的是那一个表达式。
    """
    src = open(SCRIPT, encoding="utf-8").read()
    assert "return 1 if (errors or seamed) else 0" in src, \
        "接缝命中没接进退出码，CronJob 会对着一份没写进去的数据报绿"
    # 守卫必须真的被调用（写好了不调用 = 死守卫，读文件的人会以为有保护）
    assert "seam = _check_seam(con, code, dmax, bars)" in src

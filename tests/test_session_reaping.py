"""Session-idle reaping tests (production incident 2026-10-13/14).

Three MCP servers hit the mcp SDK's 10_000-session cap the same day and every
new session was refused ("Refusing to open a new session"). This module pins
the daas-mcp fix: the idle timeout must reach the SDK constructor.

Root cause pinned here: fastmcp's ``FastMCPStreamableHTTPSessionManager``
declares ``session_idle_timeout: float | None = None`` and passes it
*explicitly* to the SDK, overriding the SDK's own 1800s default. A
``kwargs.setdefault(...)`` patch (the shape used in fd-open-data-mcp) is a
no-op against an explicitly-passed key. Production evidence: 3383 sessions
created in 26h, zero "idle timeout" log lines.

Tests are version-tolerant: fastmcp >=4 takes the kwarg on ``http_app``;
3.4.x takes the legacy constructor patch (this venv runs 3.4.4, production
4.0.11). Both paths are asserted to deliver the value to the SDK.
"""
from __future__ import annotations

import inspect

import pytest

from daas.fd_daas_mcp import server as S


# ---------------------------------------------------------------- env knob


def test_session_idle_timeout_defaults_to_1800(monkeypatch):
    monkeypatch.delenv("MCP_SESSION_IDLE_TIMEOUT", raising=False)
    assert S._session_idle_timeout() == 1800.0


def test_session_idle_timeout_env_override(monkeypatch):
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "600")
    assert S._session_idle_timeout() == 600.0


def test_session_idle_timeout_non_numeric_falls_back(monkeypatch):
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "later")
    assert S._session_idle_timeout() == 1800.0


def test_session_idle_timeout_zero_disables(monkeypatch):
    """0/负值 = 显式关闭（调试逃生口；默认不再走到这条）。"""
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "0")
    assert S._session_idle_timeout() == 0.0


# ------------------------------------------------- kwargs reach the SDK


def test_http_app_receives_timeout_or_legacy_patch_fires(monkeypatch):
    """核心回归：两种 fastmcp 世代都必须把 1800 送达 SDK 构造链。

    - fastmcp >=4（生产 4.0.11）：``_build_http_app`` 必须把
      ``session_idle_timeout`` 作为 kwarg 传给 ``http_app``（该世代管理器在
      lifespan 里由 fastmcp 自己构造，kwarg 一路透传）；
    - fastmcp 3.4.x（本 venv）：``http_app`` 无该参数 → 装 legacy 构造补丁，
      以 **赋值**（非 setdefault）写进 SDK 收到的 kwargs。
    """
    import fastmcp.server.http as H

    params = inspect.signature(S.app.http_app).parameters
    if "session_idle_timeout" in params:
        captured: dict = {}

        def fake_http_app(**kwargs):
            captured.update(kwargs)
            return object()

        # 只要 middleware 与 timeout 两个 kwarg 的传递契约
        monkeypatch.setattr(S, "_build_http_app", S._build_http_app)
        monkeypatch.setattr(S.app, "http_app", fake_http_app)
        S._build_http_app(None, 1800.0)
        assert captured.get("session_idle_timeout") == 1800.0
        return

    # legacy 路径：装补丁后用「显式 None」姿态构造，断言被改成 1800
    captured_kwargs: list = []

    def capture_init(self, *a, **kw):
        captured_kwargs.append(dict(kw))

    monkeypatch.setattr(H.FastMCPStreamableHTTPSessionManager, "__init__", capture_init)
    S._build_http_app(None, 1800.0)  # 应安装 legacy 补丁（基于 capture_init 再包一层）
    # fastmcp 3.4.x 的 http_app() 不构造管理器（lifespan 才构造）；
    # 手动模拟 fastmcp 的构造调用——补丁必须把显式 None 改成 1800。
    H.FastMCPStreamableHTTPSessionManager.__init__(
        object(), app=None, session_idle_timeout=None
    )
    assert captured_kwargs[-1].get("session_idle_timeout") == 1800.0


def test_legacy_patch_uses_assignment_not_setdefault(monkeypatch):
    """防复发：显式传递 None 的键，setdefault 是空操作——必须赋值。"""
    import fastmcp.server.http as H

    captured: dict = {}

    class Fake:
        def __init__(self, *a, **kw):  # pragma: no cover - 被 origin 取代
            pass

    captured_kwargs: list = []

    def origin(self, *a, **kw):
        captured_kwargs.append(dict(kw))

    # 模拟 fastmcp 4.0.11 子类：显式传 session_idle_timeout=None
    def subclass_init(self, *a, **kw):
        kw["session_idle_timeout"] = kw.get("session_idle_timeout", None)
        origin(self, *a, **kw)

    monkeypatch.setattr(H.FastMCPStreamableHTTPSessionManager, "__init__", subclass_init)
    S._patch_legacy_session_timeout(900.0)
    # 现在再用「显式 None」的姿态构造：赋值补丁必须把它改成 900
    H.FastMCPStreamableHTTPSessionManager.__init__(object(), session_idle_timeout=None)
    assert captured_kwargs and captured_kwargs[-1]["session_idle_timeout"] == 900.0


def test_patch_is_idempotent(monkeypatch):
    """重复安装不叠加（幂等标记）。"""
    import fastmcp.server.http as H

    S._patch_legacy_session_timeout(900.0)
    first = H.FastMCPStreamableHTTPSessionManager.__init__
    S._patch_legacy_session_timeout(900.0)
    assert H.FastMCPStreamableHTTPSessionManager.__init__ is first


def test_zero_timeout_leaves_constructors_untouched(monkeypatch):
    """timeout=0（调试关闭）不装补丁——SDK 行为保持原样。"""
    import fastmcp.server.http as H

    before = H.FastMCPStreamableHTTPSessionManager.__init__
    S._patch_legacy_session_timeout(0.0)
    assert H.FastMCPStreamableHTTPSessionManager.__init__ is before


def test_main_http_never_calls_app_run_http(monkeypatch):
    """生产路径不再走 app.run(transport="http")（4.0.11 该路径无 idle 旋钮）。"""
    calls: list = []
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "1800")
    monkeypatch.setattr(S, "_register_tools", lambda profile: None)
    monkeypatch.setattr(
        S.app, "run", lambda **kw: calls.append(("run", kw)), raising=False
    )
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda asgi, **kw: calls.append(("uvicorn", kw)))
    S.main(transport="http", host="127.0.0.1", port=18311)
    assert calls and calls[0][0] == "uvicorn"  # 走 uvicorn(asgi)，不走 app.run

"""内嵌后端（线程里的 uvicorn）的启停验收。

这一层如果不可靠，桌面端所有页面都是在沙子上盖楼，所以断言写得比别处狠：

  · `start()` 必须等到 `/api/health` 真的返回 200，不是等"线程活着"；
  · 端口必须是临时分配的，**不能**是网页版那个写死的 8000（否则两个客户端互斥、
    而且桌面版会在局域网网卡上开门）；
  · `stop()` 必须真的跑完后端 lifespan 的 shutdown —— 这是本项目历史上第一次
    走到那条代码路径（此前 15 次会话全是硬停，日志里 `Shutting down` 出现 0 次）。
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.request

import pytest

from core import backend_host as bh


def _get_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


@pytest.fixture(scope="module")
def started():
    """整个模块共用一次启动（真起一次后端要几秒，别每个测试重来）。"""
    h = bh.BackendHost()
    yield h
    h.stop()


# ---------------------------------------------------------------- 启动

def test_base_url_before_start_is_an_error(started):
    with pytest.raises(bh.BackendStartError):
        _ = started.base_url


def test_start_waits_for_real_health_response(started):
    base = started.start(timeout=90.0)
    assert base == started.base_url
    status, body = _get_json(f"{base}/api/health")
    assert status == 200
    # 这两个字段是就绪判据，桌面端拿它当"服务起来了"的唯一凭据
    assert body["ok"] is True
    assert body["service"] == "yidao-backend"
    assert started.alive


def test_port_is_ephemeral_and_not_the_web_default(started):
    """桌面端不能沿用 8000：那是网页版写死的端口，同机共存会互相顶掉。"""
    assert started.port and started.port != 8000
    assert started.port > 1024
    assert _port_open(started.host, started.port)


def test_backend_starts_without_a_console(monkeypatch):
    """**无控制台也必须起得来**（pythonw：`sys.stdout`/`sys.stderr` 都是 None）。

    现场（第 34 轮）：双击 `启动围棋平台.bat` 走的是 `pythonw.exe`，uvicorn 默认那份
    日志配置里的 `DefaultFormatter` 构造时会调 `sys.stdout.isatty()` → AttributeError
    → `dictConfig` 抛 `ValueError: Unable to configure formatter 'default'`，
    **内嵌后端直接起不来** —— 玩家双击只看到一句「后端起不来」。这条链在 pytest 与
    smoke 里全是 `python.exe`（有控制台），所以从 P1 起一直没被走过。

    这一条就把 None 摆出来，起一次真后端（不是桩）：起得来才算修好。
    """
    monkeypatch.setattr(bh.sys, "stdout", None)
    monkeypatch.setattr(bh.sys, "stderr", None)
    h = bh.BackendHost()
    try:
        base = h.start(timeout=90.0)
        status, body = _get_json(f"{base}/api/health")
        assert status == 200 and body["ok"] is True
    finally:
        h.stop(timeout=20.0)


def test_uvicorn_log_config_avoids_isatty(monkeypatch):
    """配置里两个格式化器都必须写死 `use_colors`，否则 pythonw 下就是上面那声炸。"""
    import uvicorn
    cfg = bh.uvicorn_log_config(uvicorn)
    assert cfg is not None
    for name in ("default", "access"):
        assert cfg["formatters"][name]["use_colors"] is False, cfg["formatters"][name]
    # 没有控制台时返回 None：整份配置都不给，uvicorn 就不会去碰 None 上的属性
    monkeypatch.setattr(bh.sys, "stdout", None)
    assert bh.uvicorn_log_config(uvicorn) is None


def test_bound_on_loopback_only(started):
    """只监听 127.0.0.1。

    桌面端不读 `config.host`（host 是 `BackendHost` 显式传给 uvicorn 的），所以
    这条验的是“传进去的确实是回环”，而不是“默认值碰巧对了”。后者的默认值
    已从 `0.0.0.0` 改成 `127.0.0.1`（P5 顺带项），两边现在同口径。
    """
    assert started.host == "127.0.0.1"
    assert not _port_open("127.0.0.2", started.port)


# ---------------------------------------------------------------- 关停

def test_stop_runs_lifespan_shutdown_and_releases_port():
    """单独起一个实例来验关停：必须看见 lifespan 的 shutdown 真的执行了。

    判据用 `go.main` 的日志行，而不是"线程不在了" —— 线程被 daemon 强杀也会
    "不在"，但那样 KataGo 子进程会被留在系统里。这正是本项目以前反复出现的状态。
    """
    h = bh.BackendHost()
    h.start(timeout=90.0)
    port = h.port
    assert _port_open(h.host, port)

    seen: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    logger = logging.getLogger("go.main")
    handler = Capture()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        h.stop(timeout=20.0)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert "服务已停止" in seen, f"lifespan 的 shutdown 没跑完，日志只有：{seen}"
    assert h._thread is None and h._server is None
    assert not _port_open(h.host, port), "端口没有随关停释放"


def test_stop_is_idempotent():
    h = bh.BackendHost()
    h.stop()                      # 没启动过就停：不该抛
    h.start(timeout=90.0)
    h.stop(timeout=20.0)
    h.stop(timeout=20.0)          # 停两次：同样不该抛


# ---------------------------------------------------------------- 自恢复

def test_restart_gives_a_fresh_working_url():
    """线程崩掉后的自恢复路径。端口会变，所以调用方必须重新取 base_url。"""
    h = bh.BackendHost()
    first = h.start(timeout=90.0)
    first_port = h.port
    second = h.restart(timeout=90.0)
    status, body = _get_json(f"{second}/api/health")
    assert status == 200 and body["ok"] is True
    assert second != first or _port_open(h.host, first_port)
    h.stop(timeout=20.0)


def test_start_surfaces_thread_error_instead_of_timed_out(monkeypatch):
    """线程里报错要立刻带出来，不能干等到 60 秒超时。"""
    class ExplodingServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            raise OSError("模拟：端口被占")

    monkeypatch.setattr(bh, "_ThreadedServer", ExplodingServer)
    h = bh.BackendHost()
    t0 = time.monotonic()
    with pytest.raises(bh.BackendStartError, match="模拟：端口被占") as exc:
        h.start(timeout=30.0, attempts=1)
    assert time.monotonic() - t0 < 5, f"没有提前失败，而是等到了超时：{exc}"

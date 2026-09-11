"""玩家版启动器（launcher/gui.py）的验收。

第 34 轮按用户澄清重做：第 33 轮把启动器整个删了，用户说「你没修启动器，也怪我没说」——
原意是**修掉启动器里的开发者项目**，不是删掉启动器。于是从 git 历史恢复并重写为
「登录 + 启动」合一窗口。测的是界面这一层：

  · 登录/注册/免登录/令牌失效回退四条路都走 FakeApi（真后端由 smoke 第 4 趟验）；
  · 「进入弈道」先停内嵌后端再拉起客户端 —— spawn 可替换，测试拦截它；
  · 安装 KataGo 走假 InstallRunner（与设置页同接口：output/finished 两个信号）；
  · 配置大模型走账号 PUT（与客户端设置页同一条链路）；
  · 开发者入口一条都不许出现（那是用户点名的「各种项目」）；
  · 单实例锁真两进程（与桌面端同套路）。

为什么放在 `desktop/tests`：图形界面需要 PySide6，只有 `desktop\\.venv` 有；
GUI 模块用 importlib 按路径加载，不往 `sys.path` 里塞 launcher/（免得撞名）。
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QPushButton

from core import settings as settings_mod
from tests import harness as H

ROOT = Path(__file__).resolve().parent.parent.parent
LAUNCHER = ROOT / "launcher"


@pytest.fixture(scope="module")
def G():
    spec = importlib.util.spec_from_file_location("launcher_gui_under_test",
                                                  LAUNCHER / "gui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prefs(tmp_path):
    return settings_mod.Prefs(str(tmp_path / "client.ini"))


def drain(qapp, predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ------------------------------------------------------------------ 桩

class _Hook:
    """Reply.finished 的桩。给了 auto 回复就在 connect 时**当场发射**：
    窗口接完回调后状态立刻可查（真实网络是异步的，桩不需要学它异步——
    用 `QTimer.singleShot` 模拟异步反而会被 pytest 的事件循环压到超时才发）。"""

    def __init__(self, auto=None):
        self._auto = auto
        self._cbs: list = []

    def connect(self, cb):
        self._cbs.append(cb)
        if self._auto is not None:
            self.emit(*self._auto)

    def emit(self, data, err=None):
        for cb in list(self._cbs):
            cb(data, err)


class _Reply:
    """ApiClient 那个 Reply 的桩：真实代码拿 `.finished.connect(...)` 接回调。"""

    def __init__(self, auto=None):
        self.finished = _Hook(auto)


class FakeApi:
    """记录 (method, url, body)，回复按 (method, url) 从 auto 表取。"""

    def __init__(self, auto=None):
        self.auto = dict(auto or {})
        self.calls: list[tuple] = []

    def _reply(self, method, url, body=None):
        self.calls.append((method, url, body))
        return _Reply(self.auto.get((method, url)))

    def get(self, url):
        return self._reply("GET", url)

    def post(self, url, body=None):
        return self._reply("POST", url, body)

    def put(self, url, body=None):
        return self._reply("PUT", url, body)

    def last_body(self, method, url):
        for m, u, b in reversed(self.calls):
            if m == method and u == url:
                return b
        return None


class FakeHost:
    def __init__(self):
        self.port = 6553
        self.base_url = "http://127.0.0.1:6553"
        self.started = 0
        self.stopped = 0

    def start(self, timeout=0.0):
        self.started += 1
        return self.base_url

    def stop(self):
        self.stopped += 1


class FakeRunner(QObject):
    """假 InstallRunner：接口面与 settings.InstallRunner 一致（两个信号 + start/stop）。

    必须继承 QObject：PySide6 的 Signal 只在 QObject 实例上才绑成可 connect 的
    信号对象（首跑直接在 `runner.output.connect` 上报 `Signal has no attribute connect`）。
    """

    output = Signal(str)
    finished = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


def user_payload(**over):
    u = {"id": "u1", "username": "棋童", "displayName": "棋童",
         "hintMode": True, "demotionEnabled": False,
         "llmConfig": {"baseUrl": "", "model": "", "hasApiKey": False},
         "progress": {"rankName": "18级", "rankWins": 0, "winsRequired": 3}}
    u.update(over)
    return u


def login_reply(token="tk1"):
    return {"token": token, "user": user_payload()}


def _texts(widget) -> list[str]:
    return [x.text() for x in widget.findChildren(QLabel)] + \
           [x.text() for x in widget.findChildren(QPushButton)]


_DEV_ENTRIES = ("只启动后端", "停止所有服务", "运行后端测试", "端到端冒烟自检",
                "段位标定", "网络源设置", "打开项目目录", "查看日志")


def make_window(G, api, prefs, host=None, runner_factory=None, spawn=None):
    return G.MainWindow(api=api, prefs=prefs,
                        host=host or FakeHost(),
                        runner_factory=runner_factory or (lambda p: FakeRunner(p)),
                        spawn=spawn or (lambda: True))


# ------------------------------------------------------------------ 界面骨架

def test_window_looks_right_and_has_no_dev_entries(G, qapp, prefs):
    win = make_window(G, FakeApi(), prefs)
    win.show()
    qapp.processEvents()
    texts = _texts(win)
    assert any("弈道" in t for t in texts), texts
    assert any("从 18 级练到九段" in t for t in texts), texts
    assert any("进入弈道" in t for t in texts), "「进入弈道」按钮必须存在"
    assert any("安装 KataGo" in t for t in texts), texts
    assert "登录弈道" in [t for t in texts if t], texts
    # 开发者入口一条都不许出现（第 34 轮重做的理由）
    for dev in _DEV_ENTRIES:
        assert not any(dev in t for t in texts), f"开发者入口还在：{dev!r}"
    # 没登录时欢迎态与已登录动作都藏着
    assert not win.btnEnter.isVisible() and not win.btnLogout.isVisible()
    assert win.edUser.isVisible() and win.edPw.isVisible()
    # 表单预填上次的用户名；底部的 LLM 入口未登录不可点
    prefs.last_username = "老名字"
    prefs.sync()
    win2 = make_window(G, FakeApi(), prefs)
    assert win2.edUser.text() == "老名字"
    assert not win2.btnLlm.isEnabled(), "没登录不该能配大模型"
    win.close()
    win.deleteLater()


def test_the_window_icon_is_the_go_board(G, qapp):
    icon = G.app_icon.icon()
    assert not icon.isNull(), "启动器没有窗口图标"
    image = icon.pixmap(32, 32).toImage()
    wood = sum(1 for y in range(image.height()) for x in range(image.width())
               if H.is_wood(image.pixelColor(x, y)))
    assert wood > 100, f"图标里没有木色棋盘（只有 {wood} 个木色像素）"


def test_window_has_no_clipped_text(G, qapp, prefs):
    win = make_window(G, FakeApi(), prefs)
    win.show()
    H.settle(qapp, 0.3)
    offenders, scanned = H.clipped_texts(win)
    assert scanned >= 10, f"只扫到 {scanned} 个控件 —— 界面没真建起来"
    assert offenders == [], f"这些控件的文字被裁了：{offenders}"
    assert H.blank_ratio(win) < 0.9, "窗口几乎是空白"
    win.close()
    win.deleteLater()


# ------------------------------------------------------------------ 登录

def _ready(G, qapp, win):
    win.show()
    qapp.processEvents()
    win.start_backend()
    assert drain(qapp, lambda: win.backend_ready), "内嵌后端没就绪"


def test_login_flow_writes_token_and_shows_welcome(G, qapp, prefs):
    api = FakeApi({("POST", "/api/auth/login"): (login_reply(), None)})
    win = make_window(G, api, prefs)
    _ready(G, qapp, win)
    win.edUser.setText("棋童")
    win.edPw.setText("mimashou123")
    win._submit_login()
    assert drain(qapp, lambda: win.btnEnter.isVisible()), "登录后没进欢迎态"
    assert prefs.token == "tk1" and prefs.last_username == "棋童", \
        f"令牌没写进共享 ini：{prefs.token!r}"
    assert win.welcome.text().startswith("欢迎回来，棋童"), win.welcome.text()
    assert win.btnLlm.isEnabled(), "登录后该能配大模型"
    assert "18级" in win.welcome.text()
    assert api.last_body("POST", "/api/auth/login") == {"username": "棋童",
                                                        "password": "mimashou123"}
    win.close()
    win.deleteLater()


def test_register_rejects_short_password_without_a_call(G, qapp, prefs):
    api = FakeApi()
    win = make_window(G, api, prefs)
    _ready(G, qapp, win)
    win.edUser.setText("新棋童")
    win.edPw.setText("123")
    win._submit_register()
    assert win.loginError.isVisible() and "至少 6 位" in win.loginError.text()
    assert not api.calls, "短密码不该发请求"


def test_register_flow(G, qapp, prefs):
    api = FakeApi({("POST", "/api/auth/register"): (login_reply("tk2"), None)})
    win = make_window(G, api, prefs)
    _ready(G, qapp, win)
    win.edUser.setText("新棋童")
    win.edPw.setText("mimashou123")
    win._submit_register()
    assert drain(qapp, lambda: win.btnEnter.isVisible())
    assert prefs.token == "tk2"
    body = api.last_body("POST", "/api/auth/register")
    assert body["username"] == "新棋童" and body["displayName"] == "新棋童"
    win.close()
    win.deleteLater()


def test_bad_login_shows_error_and_keeps_form(G, qapp, prefs):
    api = FakeApi({("POST", "/api/auth/login"): (None, Exception("用户名或密码错误"))})
    win = make_window(G, api, prefs)
    _ready(G, qapp, win)
    win.edUser.setText("棋童")
    win.edPw.setText("wrong")
    win._submit_login()
    assert drain(qapp, lambda: win.loginError.isVisible())
    assert "用户名或密码错误" in win.loginError.text()
    assert not win.btnEnter.isVisible(), "登录失败不该放行"
    assert prefs.token == "", "失败不该写令牌"
    win.close()
    win.deleteLater()


def test_saved_token_skips_the_form(G, qapp, prefs):
    prefs.token = "tk-old"
    prefs.sync()
    api = FakeApi({("GET", "/api/auth/me"): ({"user": user_payload()}, None)})
    win = make_window(G, api, prefs)
    _ready(G, qapp, win)
    assert drain(qapp, lambda: win.btnEnter.isVisible()), "免登录没直接进欢迎态"
    assert prefs.token == "tk-old"
    win.close()
    win.deleteLater()


def test_expired_token_falls_back_to_the_form(G, qapp, prefs):
    prefs.token = "tk-过期"
    prefs.sync()
    api = FakeApi({("GET", "/api/auth/me"): (None, Exception("401"))})
    win = make_window(G, api, prefs)
    _ready(G, qapp, win)
    assert drain(qapp, lambda: win.edUser.isVisible()
                 and "已失效" in win.loginError.text()), \
        (f"失效令牌该回表单并说清楚：{win.loginError.text()!r} "
         f"edUser={win.edUser.isVisible()} btnEnter={win.btnEnter.isVisible()} "
         f"win={win.isVisible()}")
    assert prefs.token == "", "失效令牌必须清掉"
    win.close()
    win.deleteLater()


def test_logout_clears_token_and_returns_to_form(G, qapp, prefs):
    prefs.token = "tk1"
    prefs.sync()
    api = FakeApi({("GET", "/api/auth/me"): ({"user": user_payload()}, None)})
    win = make_window(G, api, prefs)
    _ready(G, qapp, win)
    assert drain(qapp, lambda: win.btnEnter.isVisible())
    win._logout()
    assert prefs.token == "" and win.edUser.isVisible()
    assert not win.btnEnter.isVisible()
    win.close()
    win.deleteLater()


# ------------------------------------------------------------------ 进入弈道

def test_enter_game_stops_backend_then_spawns_and_closes(G, qapp, prefs):
    prefs.token = "tk"
    prefs.sync()
    host = FakeHost()
    spawned = []
    api = FakeApi({("GET", "/api/auth/me"): ({"user": user_payload()}, None)})
    win = make_window(G, api, prefs, host=host, spawn=lambda: (spawned.append(1), True)[1])
    _ready(G, qapp, win)
    assert drain(qapp, lambda: win.btnEnter.isVisible())
    win._enter_game()
    assert drain(qapp, lambda: bool(spawned) and not win.isVisible(), timeout=20.0), \
        ("进入弈道后没拉起客户端并关窗 "
         f"btnEnter={win.btnEnter.isVisible()} win={win.isVisible()} "
         f"user={bool(win.user)} entering={win._entering} "
         f"state={win.lblState.text()!r}")
    assert host.stopped >= 1, "先停内嵌后端再拉客户端（两个 uvicorn 撞 SQLite）"
    assert not win.isVisible()
    win.deleteLater()


def test_enter_game_reports_failure_and_stays(G, qapp, prefs):
    prefs.token = "tk"
    prefs.sync()
    api = FakeApi({("GET", "/api/auth/me"): ({"user": user_payload()}, None)})
    win = make_window(G, api, prefs, spawn=lambda: False)
    _ready(G, qapp, win)
    assert drain(qapp, lambda: win.btnEnter.isVisible())
    win._enter_game()
    assert drain(qapp, lambda: "没能启动" in win.lblState.text(), timeout=20.0)
    assert win.isVisible(), "拉不起来不该关窗"
    win.close()
    win.deleteLater()


# ------------------------------------------------------------------ 引擎状态与安装

def test_engine_status_updates_the_badge(G, qapp, prefs):
    prefs.token = "tk"
    prefs.sync()
    status = {"engine": {"active": "katago",
                         "katago": {"available": True, "binary": "x", "model": "y"}}}
    api = FakeApi({("GET", "/api/auth/me"): ({"user": user_payload()}, None),
                   ("GET", "/api/system/status"): (status, None)})
    win = make_window(G, api, prefs)
    _ready(G, qapp, win)
    assert drain(qapp, lambda: win.btnEnter.isVisible())
    assert drain(qapp, lambda: win.engineBadge.text().startswith("KataGo 就绪"),
                 timeout=10.0), win.engineBadge.text()
    win.close()
    win.deleteLater()


def test_install_runner_streams_output_and_recovers(G, qapp, prefs):
    runner = FakeRunner()
    api = FakeApi({("GET", "/api/auth/me"): ({"user": user_payload()}, None)})
    win = make_window(G, api, prefs, runner_factory=lambda p: runner)
    _ready(G, qapp, win)
    win._on_install_clicked()
    assert runner.started == 1 and win.btnInstall.text() == "停止"
    assert win.installLog.isVisible()
    assert not win.btnQuit.isEnabled(), "安装中该锁其他入口"
    runner.output.emit("下载引擎包 windows-cuda.zip\n")
    qapp.processEvents()
    assert "下载引擎包" in win.installLog.toPlainText()
    runner.finished.emit(0, "")
    qapp.processEvents()
    assert win.btnInstall.text() == "安装 KataGo" and win.installer is None
    assert "[安装完成]" in win.installLog.toPlainText()
    assert win.btnQuit.isEnabled()
    win.close()
    win.deleteLater()


# ------------------------------------------------------------------ 配置大模型

def test_llm_requires_login_first(G, qapp, prefs):
    api = FakeApi()
    win = make_window(G, api, prefs)
    _ready(G, qapp, win)
    win._on_llm_clicked()
    assert "先登录" in win.loginError.text(), "没登录就点配置要有人话"
    assert not api.calls, "没登录不该发请求"
    win.close()
    win.deleteLater()


def test_llm_saves_to_the_account(G, qapp, prefs, monkeypatch):
    prefs.token = "tk"
    prefs.sync()

    class _FakeDialog:
        def __init__(self, parent=None):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def payload(self):
            return ("https://api.deepseek.com/v1", "deepseek-chat", "sk-x")

    monkeypatch.setattr(G, "LLMDialog", _FakeDialog)
    api = FakeApi({("GET", "/api/auth/me"): ({"user": user_payload()}, None),
                   ("PUT", "/api/auth/me/llm"): (
                       {"llmConfig": {"baseUrl": "https://api.deepseek.com/v1",
                                      "model": "deepseek-chat", "hasApiKey": True}}, None)})
    win = make_window(G, api, prefs)
    _ready(G, qapp, win)
    assert drain(qapp, lambda: win.btnEnter.isVisible())
    win._on_llm_clicked()
    assert drain(qapp, lambda: "已配置" in win.lblStatus.text(), timeout=10.0), \
        win.lblStatus.text()
    assert api.last_body("PUT", "/api/auth/me/llm") == {
        "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat", "apiKey": "sk-x"}
    win.close()
    win.deleteLater()


def test_llm_dialog_hides_the_key_by_default(G, qapp):
    dialog = G.LLMDialog()
    assert dialog.edKey.echoMode() == QLineEdit.EchoMode.Password
    dialog.chkShow.setChecked(True)
    assert dialog.edKey.echoMode() == QLineEdit.EchoMode.Normal
    assert dialog.payload() == ("", "", "")
    dialog.edUrl.setText("  http://localhost:11434/v1  ")
    assert dialog.payload()[0] == "http://localhost:11434/v1"
    dialog.deleteLater()


# ------------------------------------------------------------------ 单实例锁

def test_the_production_spawn_command_is_the_client_entry(G, monkeypatch):
    """生产那份 spawn 必须起 `desktop/app.py`，且**不设** GO_CLIENT_INI。

    不设它是刻意的：启动器与客户端都按 `Prefs(None)` 落 AppData 那份 ini，
    设了反而会把两边的偏好转到两个地方（令牌就不共享了）。真起进程的那条链
    由 `smoke_launch.py` 第 4 趟验，这里只钉命令与环境的形状。
    """
    seen: dict = {}

    class _FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            seen["kw"] = kw

    monkeypatch.setattr(G.subprocess, "Popen", _FakePopen)
    assert G._spawn_client() is True
    cmd = seen["cmd"]
    assert cmd[0].endswith("pythonw.exe"), cmd
    assert cmd[1].endswith("app.py") and "desktop" in cmd[1], cmd
    assert "GO_CLIENT_INI" not in seen["kw"]["env"], "不该给客户端指另一份偏好文件"
    assert seen["kw"]["env"]["PYTHONUTF8"] == "1"
    assert seen["kw"]["cwd"] == str(G.DESKTOP)


def test_second_launcher_process_wakes_first_and_exits(G, qapp):
    """真两个 `gui.main()` 进程：第二个必须安静退出（code 0），第一个保持锁。"""
    from core import single_instance as si

    name = f"Yidao.launcher.t{os.getpid()}{time.time_ns() % 100000}"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8",
               GO_LAUNCHER_PIPE=name)
    cmd = [sys.executable, str(LAUNCHER / "gui.py")]
    first = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if si.Gate(name).is_running():
                break
            time.sleep(0.05)
        assert si.Gate(name).is_running(), "第一个启动器没把锁占上（起不来？）"

        second = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rc = second.wait(timeout=30)
        assert rc == 0, f"第二个启动器没有安静退出：{rc}"
        assert first.poll() is None, "第二个启动器把先开的顶掉了"
    finally:
        first.kill()
        first.wait(timeout=15)
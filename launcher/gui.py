"""弈道 · 启动器（玩家版）：登录 + 启动合一窗口。

第 33 轮把它整个删掉了 —— 用户的话是「启动器的各种项目，消除，做成登录页和启动页，
更偏玩家审美」，当时按「删除启动器」执行了；第 34 轮用户澄清：**要修的是启动器里的
开发者项目，不是启动器本身**。于是从 git 历史恢复并重做成本文件：

  · 登录 + 启动合一：账号登录/注册（复用桌面端 ApiClient 与 Prefs，令牌写进同一个
    AppData ini，客户端启动即免登录）；
  · 引擎状态（复用大厅 `engine_state` 口径）；安装 KataGo（复用设置页 InstallRunner）；
    配置大模型（写入账号，立即生效，不需要重启后端）；
  · 一切开发者入口（只启动后端 / 停止服务 / 运行后端测试 / 冒烟自检 / 段位标定 /
    网络源设置 / 打开项目目录 / 日志面板）全部删掉 —— 那正是用户点名的「各种项目」。

与旧启动器的架构差异（都是刻意为之）：
  · 不再有 `launcher.py` 终端菜单与动作核心：玩家版没有控制台；环境引导的边界在
    第 33 轮 §33.9 已说清，不旧事重提；
  · 后端由本窗口拉起一个（内嵌，复用桌面端 BackendHost）：登录、引擎状态、安装后体检
    都打在它上面；「进入弈道」时**先停掉它**再拉起客户端 —— 客户端自带内嵌后端，
    两个 uvicorn 同时写同一份 SQLite 会撞锁；
  · LLM 配置从「写 backend/.env + 重启后端」改成「写账号」（客户端设置页同一条链路），
    立即生效；客户端设置页仍是完整版，这里只给最常用的三个字段。

线程模型：后端启动与「进入弈道」时的停靠都走后台线程（照抄桌面端 app.py 的先例——
主线程等待 = 开窗即假死）。安装走 QProcess。所有回包经信号回主线程。
"""
from __future__ import annotations

import ctypes
import getpass
import hashlib
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

if getattr(sys, "frozen", False):
    # PyInstaller 打包后 __file__ 指向临时解压目录（onefile）或 _internal（onedir），
    # 都不是项目根；exe 自身的路径才是。约定：exe 放在项目根目录（与 desktop/ 同级）。
    ROOT = Path(sys.executable).resolve().parent
    if (ROOT / "backend" / "data").is_dir():
        # paths.data_dir() 的 FROZEN 分支默认指向 LOCALAPPDATA；但本项目 exe 与
        # bat/vbs 三个入口必须共用同一份 backend/data（账号、对局、题库进度）。
        # data_dir() 的优先级恰好是「显式环境变量 > LOCALAPPDATA」，用 setdefault 钉住。
        os.environ.setdefault("GO_DATA_DIR", str(ROOT / "backend" / "data"))
else:
    ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
# desktop/ 加进 path 才能 import core / ui
sys.path.insert(0, str(DESKTOP))

from PySide6.QtCore import Qt, QTimer, Signal                                # noqa: E402
from PySide6.QtWidgets import (                                           # noqa: E402
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout, QWidget,
)

from core import backend_host as bh                                        # noqa: E402
from core import crash_log                                                 # noqa: E402
from core import settings as settings_mod                                  # noqa: E402
from core.api import ApiClient, err_text                                   # noqa: E402
from ui import app_icon                                                    # noqa: E402
from ui import theme                                                       # noqa: E402
from ui.pages.lobby import ENGINE_KIND, engine_state                       # noqa: E402
from ui.pages.settings import InstallRunner                                # noqa: E402

APP_TITLE = "弈道"
TAGLINE = "AI 围棋教学 · 从 18 级练到九段"
#: 引擎状态轮的节拍：装引擎/自愈时想看得见变化；3 秒一次本地请求不算负担。
STATUS_TICK_MS = 3000


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class MainWindow(QWidget):
    """登录 + 启动合一窗口。"""

    #: 后端线程 → 主线程的就绪/失败投递。不能在工作线程里直接 `QTimer.singleShot`：
    #: 那会把定时器排进**调用线程**的事件循环，而工作线程没有事件循环，回调永不触发。
    backendState = Signal(bool)
    #: 「进入弈道」线程停完后端后 → 主线程的「拉起客户端」投递（同上，跨线程信号）。
    launchStep = Signal()

    def __init__(self, api=None, prefs=None, host=None, runner_factory=None,
                 spawn=None, gate=None):
        super().__init__()
        self.backendState.connect(self._on_boot_done)
        self.launchStep.connect(self._spawn_and_exit)
        self._prefs = prefs if prefs is not None else settings_mod.Prefs(
            os.environ.get("GO_CLIENT_INI") or None)
        self._host = host if host is not None else bh.BackendHost()
        # api 不传就按「host + prefs」现造一个：端口与令牌都要取「当时」的值，
        # 不能在建窗口那一刻冻结（后端是异步起的，端口那时还没有）。
        self._api = api if api is not None else ApiClient(
            lambda: (self._host.base_url if getattr(self._host, "port", None)
                     else "http://127.0.0.1:0"),
            lambda: self._prefs.token)
        # 显式传 venv 的 python：exe 化后 sys.executable 是启动器自己，
        # 让 InstallRunner 兜底取它只会把自己再拉一遍。
        self._runner_factory = runner_factory or (
            lambda parent: InstallRunner(parent, program=_venv_python()))
        self._spawn = spawn if spawn is not None else _spawn_client
        self._gate = gate
        self.user: dict = {}
        self.backend_ready = False
        self._entering = False
        self._boot_thread: threading.Thread | None = None
        self._busy_login = False
        self.installer = None
        self._status: dict = {}
        self._build_ui()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(STATUS_TICK_MS)
        self._status_timer.timeout.connect(self._poll_status)

    # ---------------------------------------------------------------- 装配

    def _build_ui(self) -> None:
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(app_icon.icon())
        self.setFixedSize(560, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(12)

        # ---- 品牌头：图标 + 应用名 + 标语 + 引擎徽章
        head = QHBoxLayout()
        head.setSpacing(14)
        mark = QLabel(self)
        mark.setPixmap(app_icon.pixmap(56))
        words = QVBoxLayout()
        words.setSpacing(2)
        title = QLabel(APP_TITLE, self)
        title.setStyleSheet(f"font-size: 24px; font-weight: 700; color: {theme.INK};"
                            f" background: transparent;")
        tail = QLabel(TAGLINE, self)
        tail.setProperty("role", "muted")
        words.addWidget(title)
        words.addWidget(tail)
        self.engineBadge = QLabel("启动中…", self)
        self.engineBadge.setProperty("role", "badge")
        self.engineBadge.setStyleSheet(theme.badge_style("muted"))
        head.addWidget(mark)
        head.addLayout(words, 1)
        head.addWidget(self.engineBadge, 0, Qt.AlignTop)
        root.addLayout(head)

        # ---- 账号卡：登录表单 ←→ 已登录欢迎态，切换只动这一张卡
        self.card = QWidget(self)
        self.card.setObjectName("launcherCard")
        self.card.setStyleSheet(f"QWidget#launcherCard {{ background: {theme.PANEL};"
                                f" border: 1px solid {theme.LINE}; border-radius: 12px; }}")
        self.cardLay = QVBoxLayout(self.card)
        self.cardLay.setContentsMargins(24, 20, 24, 18)
        self.cardLay.setSpacing(10)
        root.addWidget(self.card)

        self.lblLoginTitle = QLabel("登录弈道", self.card)
        self.lblLoginTitle.setProperty("role", "h2")
        self.cardLay.addWidget(self.lblLoginTitle)

        self.edUser = QLineEdit(self._prefs.last_username or "", self.card)
        self.edUser.setPlaceholderText("用户名")
        self.edPw = QLineEdit(self.card)
        self.edPw.setPlaceholderText("密码（注册至少 6 位）")
        self.edPw.setEchoMode(QLineEdit.EchoMode.Password)
        self.cardLay.addWidget(self.edUser)
        self.cardLay.addWidget(self.edPw)
        self.edPw.returnPressed.connect(self._submit_login)
        self.edUser.returnPressed.connect(self.edPw.setFocus)

        self.btnLogin = QPushButton("登录", self.card)
        self.btnLogin.setProperty("role", "primary")
        self.btnRegister = QPushButton("注册新账号", self.card)
        loginRow = QHBoxLayout()
        loginRow.addWidget(self.btnLogin)
        loginRow.addWidget(self.btnRegister)
        loginRow.addStretch(1)
        self.cardLay.addLayout(loginRow)

        self.loginNote = QLabel(
            "新账号从 18级 起步：累计 3 胜进入晋升战，晋升战 2 连胜即可升到 17级。", self.card)
        self.loginNote.setProperty("role", "muted")
        self.loginNote.setWordWrap(True)
        self.cardLay.addWidget(self.loginNote)

        self.loginError = QLabel("", self.card)
        self.loginError.setWordWrap(True)
        self.loginError.setStyleSheet(f"color: {theme.DANGER}; background: transparent;")
        self.loginError.setVisible(False)
        self.cardLay.addWidget(self.loginError)

        # --- 已登录欢迎态（先建后藏）
        self.welcome = QLabel("", self.card)
        self.welcome.setProperty("role", "h2")
        self.enterHint = QLabel("", self.card)
        self.enterHint.setProperty("role", "muted")
        self.enterHint.setWordWrap(True)
        self.lblStatus = QLabel("", self.card)
        self.lblStatus.setProperty("role", "muted")
        self.lblStatus.setWordWrap(True)
        self.btnEnter = QPushButton("进入弈道", self.card)
        self.btnEnter.setProperty("role", "primary")
        self.btnEnter.setMinimumHeight(44)
        self.btnLogout = QPushButton("退出登录", self.card)
        self.btnLogout.setProperty("role", "ghost")
        welcomeRow = QHBoxLayout()
        welcomeRow.addWidget(self.btnEnter, 1)
        welcomeRow.addWidget(self.btnLogout)
        for w in (self.welcome, self.enterHint, self.btnEnter, self.btnLogout,
                  self.lblStatus):
            self.cardLay.addWidget(w)
            w.setVisible(False)

        # ---- 安装输出（没在装就藏着）
        self.installLog = QPlainTextEdit(self)
        self.installLog.setReadOnly(True)
        self.installLog.setMaximumBlockCount(400)
        self.installLog.setFixedHeight(96)
        self.installLog.setVisible(False)
        root.addWidget(self.installLog)

        # ---- 底部工具行
        foot = QHBoxLayout()
        self.btnInstall = QPushButton("安装 KataGo", self)
        self.btnInstall.setProperty("role", "ghost")
        self.btnLlm = QPushButton("配置大模型（复盘讲解）", self)
        self.btnLlm.setProperty("role", "ghost")
        self.btnQuit = QPushButton("退出", self)
        self.btnQuit.setProperty("role", "ghost")
        foot.addWidget(self.btnInstall)
        foot.addWidget(self.btnLlm)
        foot.addStretch(1)
        foot.addWidget(self.btnQuit)
        root.addLayout(foot)

        # ---- 底部进度与状态
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setVisible(False)
        self.lblState = QLabel("正在准备…", self)
        self.lblState.setProperty("role", "muted")
        root.addWidget(self.progress)
        root.addWidget(self.lblState)

        # ---- 接线（一律绑定方法，见 core/api.py Reply 的口径）
        self.btnLogin.clicked.connect(self._submit_login)
        self.btnRegister.clicked.connect(self._submit_register)
        self.btnEnter.clicked.connect(self._enter_game)
        self.btnLogout.clicked.connect(self._logout)
        self.btnInstall.clicked.connect(self._on_install_clicked)
        self.btnLlm.clicked.connect(self._on_llm_clicked)
        self.btnQuit.clicked.connect(self._quit)
        self._show_form()

    # ---------------------------------------------------------------- 后端

    def start_backend(self) -> None:
        """后台线程里把内嵌后端拉起来；就绪后回主线程。"""
        self._set_state("正在准备…", busy=True)
        self._boot_thread = threading.Thread(target=self._boot_run,
                                             name="launcher-backend", daemon=True)
        self._boot_thread.start()

    def _boot_run(self) -> None:
        try:
            self._host.start(timeout=120.0)
        except Exception as exc:  # noqa: BLE001  线程里抛出去没人接
            self._boot_error = str(exc) or repr(exc)
            self.backendState.emit(False)
            return
        self._boot_error = ""
        self.backendState.emit(True)

    def _on_boot_done(self, ok: bool) -> None:
        if not ok:
            self._on_backend_failed()
            return
        self._handle_ready()

    def _on_backend_failed(self) -> None:
        crash_log.write(f"启动器：内嵌后端起不来：{self._boot_error}")
        QMessageBox.critical(
            self, "弈道 启动失败",
            f"{self._boot_error}\n\n日志：{crash_log.log_path()}")
        self._set_state("后端没起来，退出重试。", busy=False)

    def _handle_ready(self) -> None:
        self.backend_ready = True
        crash_log.write(f"启动器：内嵌后端就绪 {self._host.base_url}")
        self._set_state("", busy=False)
        self._status_timer.start()
        self._boot()

    def _set_state(self, text: str, busy: bool = False) -> None:
        self.lblState.setText(text)
        self.progress.setVisible(busy)
        for b in (self.btnLogin, self.btnRegister, self.btnEnter,
                  self.btnInstall, self.btnLlm, self.btnQuit):
            b.setEnabled(not busy)
        self._set_llm_enabled()

    def _set_llm_enabled(self) -> None:
        self.btnLlm.setEnabled(bool(self.user) and not self.progress.isVisible())

    # ---------------------------------------------------------------- 登录

    def _boot(self) -> None:
        """有旧令牌就先试免登录；不行就停在登录表单。"""
        if self._prefs.token:
            self._api.get("/api/auth/me").finished.connect(self._on_me)
        else:
            self._show_form()

    def _on_me(self, data, err) -> None:
        user = (data or {}).get("user") if isinstance(data, dict) else None
        if err is not None or not user:
            self._prefs.token = ""
            self._prefs.sync()
            self._show_form()
            self.loginError.setText("上次的登录已失效，请重新登录。")
            self.loginError.setVisible(True)
            return
        crash_log.write(f"启动器：免登录成功 {user.get('username')}")
        self._enter(user)

    def _submit_login(self) -> None:
        self._submit("login")

    def _submit_register(self) -> None:
        self._submit("register")

    def _submit(self, mode: str) -> None:
        if self._busy_login or not self.backend_ready:
            return
        username = self.edUser.text().strip()
        password = self.edPw.text()
        if not username or not password:
            self._fail("请填写用户名与密码")
            return
        if mode == "register" and len(password) < 6:
            self._fail("密码至少 6 位")
            return
        self._busy_login = True
        self.btnLogin.setText("登录中…" if mode == "login" else "注册中…")
        for b in (self.btnLogin, self.btnRegister):
            b.setEnabled(False)
        body = {"username": username, "password": password}
        if mode == "register":
            body["displayName"] = username
        self._api.post(f"/api/auth/{mode}", body).finished.connect(self._on_auth_reply)

    def _on_auth_reply(self, data, err) -> None:
        self._busy_login = False
        self.btnLogin.setText("登录")
        for b in (self.btnLogin, self.btnRegister):
            b.setEnabled(True)
        if err is not None or not data or not data.get("token"):
            self._fail(err_text(err) or "服务没有返回令牌")
            return
        self._prefs.token = data["token"]
        self._prefs.last_username = (data["user"] or {}).get("username", "")
        self._prefs.sync()
        self.edPw.clear()
        crash_log.write(f"启动器：登录成功 {self._prefs.last_username}")
        self._enter(data["user"])

    def _fail(self, message: str) -> None:
        self.loginError.setText(message)
        self.loginError.setVisible(True)

    def _enter(self, user: dict) -> None:
        self.user = user
        self.loginError.setVisible(False)
        name = user.get("displayName") or user.get("username") or ""
        rank = (user.get("progress") or {}).get("rankName") or "—"
        self.welcome.setText(f"欢迎回来，{name}（{rank}）")
        self.enterHint.setText("登录已就绪：点「进入弈道」打开对局客户端；"
                               "引擎与大模型也能在这里先配好。")
        self._show_welcome()
        self._paint_account()
        self._poll_status(force=True)
        self._set_llm_enabled()

    def _show_form(self) -> None:
        self.user = {}
        self.welcome.setVisible(False)
        self.enterHint.setVisible(False)
        self.btnEnter.setVisible(False)
        self.btnLogout.setVisible(False)
        self.lblStatus.setVisible(False)
        self.lblLoginTitle.setVisible(True)
        self.edUser.setVisible(True)
        self.edPw.setVisible(True)
        self.btnLogin.setVisible(True)
        self.btnRegister.setVisible(True)
        self.loginNote.setVisible(True)
        self._set_llm_enabled()

    def _show_welcome(self) -> None:
        for w in (self.lblLoginTitle, self.edUser, self.edPw, self.btnLogin,
                  self.btnRegister, self.loginNote, self.loginError):
            w.setVisible(False)
        self.welcome.setVisible(True)
        self.enterHint.setVisible(True)
        self.btnEnter.setVisible(True)
        self.btnLogout.setVisible(True)
        self.lblStatus.setVisible(True)

    def _logout(self) -> None:
        self._prefs.token = ""
        self._prefs.sync()
        self._show_form()

    # ---------------------------------------------------------------- 引擎状态

    def _poll_status(self, force: bool = False) -> None:
        if not self.backend_ready or not self.user:
            return
        self._api.get("/api/system/status").finished.connect(self._on_status)

    def _on_status(self, data, err) -> None:
        if err is not None or not isinstance(data, dict):
            return
        self._status = data
        # `engine_state` 吃的是**整包** `/api/system/status`（大厅那半边也这么传）：
        # 传内层 `data["engine"]` 会让它再取一次 `engine` 键、拿到空 dict，于是一台
        # 装着 KataGo 的机器被报成「未检测到」（启动器自己的测试当场抓出来的）。
        kind, text = engine_state(data)
        self.engineBadge.setText(text)
        self.engineBadge.setStyleSheet(theme.badge_style(ENGINE_KIND.get(kind, "muted")))
        self._paint_account()

    def _paint_account(self) -> None:
        """欢迎态那行小字：大模型是否配好 + 当前账号。

        单独一个方法是因为有三处要它（进欢迎态、状态轮回包、刚存完配置）——
        存完配置直接改名，不必等下一次状态轮才显示「已配置」。
        """
        if not self.user:
            self.lblStatus.setText("")
            return
        llm = (self.user.get("llmConfig") or {})
        name = self.user.get("displayName") or self.user.get("username") or ""
        self.lblStatus.setText(
            f"大模型讲解：{'已配置' if llm.get('hasApiKey') else '未配置（复盘讲解走模板）'}"
            f" · 当前账号：{name}")

    # ---------------------------------------------------------------- 进入弈道

    def _enter_game(self) -> None:
        if self._entering or not self.user:
            return
        self._entering = True
        self._set_state("正在进入弈道…", busy=True)
        crash_log.write("启动器：进入弈道（先停本窗口的内嵌后端，再拉起客户端）")
        threading.Thread(target=self._stop_then_launch, name="launcher-enter",
                         daemon=True).start()

    def _stop_then_launch(self) -> None:
        try:
            self._host.stop()
        except Exception:  # noqa: BLE001  停不干净也照拉客户端，客户端自管后端
            crash_log.write(f"启动器：停内嵌后端失败（继续）：{traceback.format_exc()}")
        self.launchStep.emit()

    def _spawn_and_exit(self) -> None:
        if not self._spawn():
            crash_log.write("启动器：客户端拉起失败")
            self._set_state("客户端没能启动，请查看日志后重试。", busy=False)
            self._entering = False
            return
        crash_log.write("启动器：客户端已拉起，本窗口退出")
        self.close()

    def _quit(self) -> None:
        self.close()

    # ---------------------------------------------------------------- 安装 KataGo

    def _on_install_clicked(self) -> None:
        if self.installer is not None:
            self._stop_install()
            return
        runner = self._runner_factory(self)
        self.installer = runner
        runner.output.connect(self._append_install)
        runner.finished.connect(self._on_install_done)
        self.installLog.setPlainText("[安装已开始]")
        self.installLog.setVisible(True)
        self.btnInstall.setText("停止")
        self._set_state("正在安装 KataGo…", busy=True)
        runner.start()

    def _append_install(self, text: str) -> None:
        self.installLog.appendPlainText(str(text).rstrip())

    def _on_install_done(self, code: int, _message: str) -> None:
        self.installer = None
        self.btnInstall.setText("安装 KataGo")
        self._set_state("", busy=False)
        if code == 0:
            self.installLog.appendPlainText("[安装完成]")
        else:
            self.installLog.appendPlainText("[安装失败]：原因见上方输出，可稍后重试。")
        self._poll_status()

    def _stop_install(self) -> None:
        if self.installer is not None:
            self.installer.stop()

    # ---------------------------------------------------------------- 配置大模型

    def _on_llm_clicked(self) -> None:
        if not self.user:
            self.loginError.setText("先登录才能配置大模型（配置存在你的账号上）。")
            self.loginError.setVisible(True)
            return
        dlg = LLMDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        url, model, key = dlg.payload()
        self._set_state("正在保存大模型配置…", busy=True)
        self._api.put("/api/auth/me/llm", {"baseUrl": url, "model": model,
                                           "apiKey": key}).finished.connect(
            self._on_llm_saved)

    def _on_llm_saved(self, data, err) -> None:
        self._set_state("", busy=False)
        if err is not None:
            self.loginError.setText(f"保存失败：{err_text(err)}")
            self.loginError.setVisible(True)
            return
        cfg = (data or {}).get("llmConfig") or {}
        self.user["llmConfig"] = cfg
        self._paint_account()
        self._poll_status(force=True)

    # ---------------------------------------------------------------- 收尾

    def closeEvent(self, ev):                                # noqa: N802
        self._status_timer.stop()
        if self.installer is not None:
            self.installer.stop()
            self.installer = None
        try:
            self._host.stop()
        except Exception:  # noqa: BLE001  退出时停不干净只记日志
            pass
        super().closeEvent(ev)

    def wake(self) -> None:
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()


# ---------------------------------------------------------------------------
# 配置大模型（最小版：三个字段。完整版在客户端设置页，两边同一条账号链路）
# ---------------------------------------------------------------------------

class LLMDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("配置大模型（复盘讲解）")
        self.setModal(True)
        self.edUrl = QLineEdit(self)
        self.edUrl.setPlaceholderText("https://api.deepseek.com/v1")
        self.edModel = QLineEdit(self)
        self.edModel.setPlaceholderText("deepseek-chat / qwen-plus / kimi-k2 …")
        self.edKey = QLineEdit(self)
        self.edKey.setEchoMode(QLineEdit.EchoMode.Password)
        self.edKey.setPlaceholderText("留空表示不修改")
        self.chkShow = QCheckBox("显示 Key", self)
        self.chkShow.toggled.connect(
            lambda on: self.edKey.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))

        grid = QGridLayout()
        grid.addWidget(QLabel("接口地址", self), 0, 0)
        grid.addWidget(self.edUrl, 0, 1)
        grid.addWidget(QLabel("模型名", self), 1, 0)
        grid.addWidget(self.edModel, 1, 1)
        keyRow = QHBoxLayout()
        keyRow.addWidget(self.edKey, 1)
        keyRow.addWidget(self.chkShow)
        grid.addWidget(QLabel("API Key", self), 2, 0)
        grid.addLayout(keyRow, 2, 1)

        hint = QLabel("任何 OpenAI 兼容接口都可以（DeepSeek / 通义千问 / Kimi / Ollama）。"
                      "保存后立即生效，客户端与启动器共用这份配置。", self)
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.addLayout(grid)
        root.addWidget(hint)
        root.addWidget(buttons)
        self.resize(500, self.sizeHint().height())

    def payload(self) -> tuple[str, str, str]:
        return (self.edUrl.text().strip(), self.edModel.text().strip(),
                self.edKey.text().strip())


# ---------------------------------------------------------------------------
# 拉起客户端（可替换：测试要拦截它）
# ---------------------------------------------------------------------------

def _venv_python() -> str:
    """项目的 pythonw.exe；找不到就退回 sys.executable（脚本形态下它本来就是）。"""
    pythonw = DESKTOP / ".venv" / "Scripts" / "pythonw.exe"
    return str(pythonw) if pythonw.exists() else sys.executable


def _spawn_client() -> bool:
    """用项目的 pythonw 拉起 `desktop/app.py`。

    pythonw 无控制台，客户端自带内嵌后端与日志；环境继承本进程，GO_CLIENT_INI
    不设 = 与启动器共用同一份 AppData 偏好文件，令牌即登录态。
    """
    pythonw = DESKTOP / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.exists():
        return False
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        subprocess.Popen([str(pythonw), str(DESKTOP / "app.py")],
                         cwd=str(DESKTOP), env=env,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 单实例与入口
# ---------------------------------------------------------------------------

def _set_app_user_model_id(app_id: str = "Yidao.Launcher") -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:                                     # noqa: BLE001
        pass


def build_gate():
    """单实例锁：双击两次只开一个窗口。复用桌面端 Gate；名字按「安装目录 + 用户」
    散列（同机两份 checkout 各开一份不互相拦）；`GO_LAUNCHER_PIPE` 供测试指定。"""
    from core import single_instance as si
    env = os.environ.get("GO_LAUNCHER_PIPE", "").strip()
    if env:
        return si.Gate(env)
    key = f"{str(ROOT).lower()}|{getpass.getuser().lower()}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return si.Gate(f"Yidao.launcher.{digest}")


def gui_log_path() -> Path:
    return DESKTOP / "logs" / "launcher.log"


def main(argv: list[str] | None = None) -> int:
    _set_app_user_model_id()                       # 必须早于窗口创建
    theme.configure_hi_dpi()                       # 必须在 QApplication 之前
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("YidaoLauncher")
    theme.apply_app_font(app)
    app.setStyleSheet(theme.QSS)
    app.setWindowIcon(app_icon.icon())

    crash_log.install(os.environ.get("GO_CLIENT_LOG_DIR") or None)
    window = MainWindow()

    gate = build_gate()
    if not gate.acquire():
        return 0                                    # 已有启动器在跑：唤起请求已发出
    window._gate = gate
    gate.activated.connect(window.wake)
    app.aboutToQuit.connect(gate.release)
    window.start_backend()
    window.show()
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:                              # noqa: BLE001
        text = traceback.format_exc()
        try:
            gui_log_path().parent.mkdir(parents=True, exist_ok=True)
            gui_log_path().write_text(text, encoding="utf-8")
        except OSError:
            pass
        try:
            app = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, "启动器启动失败", f"{text}\n\n已写入 {gui_log_path()}")
        except Exception:                          # noqa: BLE001
            pass
        raise
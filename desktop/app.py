"""桌面客户端入口。

    python desktop/app.py        # 带控制台，看得见日志
    pythonw desktop/app.py       # 不带控制台（将来打包 exe 就是这种）

坑（本轮发现）：本文件叫 `app.py`，而**后端的顶层包也叫 `app/`**（uvicorn 要导
`app.main:app`）。直接 `import app` 会把这个名字占掉，后端线程里就变成
`'app' is not a package` —— 而起不来。以脚本方式跑没这个问题（入口模块名是
`__main__`），所以上面两种启动方式都对；验收脚本要用它就换个模块名加载，
见 scripts/smoke_launch.py 里的注释。

启动顺序是有意为之的：

  1. `configure_hi_dpi()` 必须在 QApplication 之前（放后面不生效，且没有报错）；
  2. 先出一个"正在启动本地服务"的小窗，**再**在后台线程里起后端 ——
     后端要建库、载题库（实测本机约 1 秒），把它放在主线程就是开窗即假死；
  3. 后端就绪才建主窗口并自动尝试免登录；
  4. 退出时 `host.stop()` 走完整 lifespan —— 这是本项目第一次能优雅关停后端。

P5 加的四条全局项都落在 `main()` 开头那十几行，顺序也不能换：

  · `crash_log.install()` 排在最早 —— 后面每一步都可能炸，而一个写不进日志的炸
    就是用户眼里的「双击图标没反应」；
  · 单实例锁排在开窗之前：第二个实例的任务是「把先开的叫回来然后自己退」，
    而不是先弹一个 splash 再消失；
  · 退出时除了 `host.stop()` 还要 `gate.release()` 与一条「退出」日志；
  · 窗口图标与托盘图标共用 `ui/app_icon.icon()`（画出来的，不带 png 资源）。
"""
from __future__ import annotations

import os
import sys
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QVBoxLayout,
    QWidget,
)

from core import backend_host as bh
from core import crash_log, paths
from core import single_instance as si
from core.settings import Prefs
from ui import app_icon
from ui import shell as shell_mod
from ui import theme


class Launcher(QObject):
    """在后端就绪前把主窗口挡起来。槽都是绑定方法（见 api.Reply 的说明）。"""

    ready = Signal(str)
    failed = Signal(str)

    def __init__(self, host: bh.BackendHost, on_shell_ready=None, gate=None):
        super().__init__()
        self._host = host
        self._on_shell_ready = on_shell_ready
        #: 单实例门。传进来就把它的新启动请求接到窗口上（不传就是不管，测试与
        #: 验收脚本可以只跑一个窗口）。不许静默失去这一根线：`app.py` 里必须传。
        self._gate = gate
        self.ready.connect(self._on_ready)
        self.failed.connect(self._on_failed)

    def start(self) -> None:
        threading.Thread(target=self._run, name="backend-launch", daemon=True).start()

    def _run(self) -> None:
        try:
            self.ready.emit(self._host.start(timeout=120.0))
        except Exception as exc:  # noqa: BLE001  线程里抛出去没人接，必须折成信号
            self.failed.emit(str(exc) or repr(exc))

    def _on_ready(self, base_url: str) -> None:
        self._splash.close()
        shell = shell_mod.Shell(self._host, self.prefs)
        shell.show()
        shell.boot()
        self.shell = shell
        # 这一行是给「一直停在正在启动本地服务」这类报障留的证据：之前日志里有
        # 启动、有单实例锁、有退出，独独没有「窗口真出来了」这一步，于是卡在
        # splash 还是卡在开窗之后问不出来。同一条线也是验收能读的口径 ——
        # 子进程里的窗口没法从这里量。
        crash_log.write(f"主窗口已出现 pid={os.getpid()} 后端={base_url}")
        if self._gate is not None:
            # 二次启动的请求只能落在一个已经存着的窗口上，所以接在这里而不是 `main()` 里。
            # 绑定方法（`Shell.wake`）而不是闭包：见上面那句注释与 api.Reply 的说明。
            self._gate.activated.connect(shell.wake)
        # 验收脚本用的缝：拿一个真窗口去驱动、去截图。生产调用不传，行为不变。
        if self._on_shell_ready is not None:
            self._on_shell_ready(shell)

    def _on_failed(self, message: str) -> None:
        self._splash.close()
        crash_log.write(f"后端起不来：{message}")
        QMessageBox.critical(
            None, "弈道 启动失败",
            f"{message}\n\n日志：{crash_log.log_path()}\n"
            f"数据：{paths.data_dir()}",
        )
        QApplication.instance().quit()


def make_splash() -> QWidget:
    """玩家向的启动画面：品牌 + 一句说明 + 不确定进度条。

    早先的文案是「正在启动本地服务… / 首次启动会建库并载入死活题库」
    —— 那是给开发者看的部署说明；玩家眼里「本地服务」「建库」都是黑话。
    启动画面只负责一件事：让等待的人知道窗口正在准备、别急着关它。
    圆角卡片要透明底（`WA_TranslucentBackground`），否则方角会露出来。
    """
    splash = QWidget()
    splash.setWindowFlags(Qt.Window | Qt.Dialog | Qt.FramelessWindowHint)
    splash.setAttribute(Qt.WA_TranslucentBackground)

    card = QWidget(splash)
    card.setObjectName("splashCard")
    card.setStyleSheet(f"QWidget#splashCard {{ background: {theme.PANEL};"
                       f" border: 1px solid {theme.LINE}; border-radius: 16px; }}")
    v = QVBoxLayout(card)
    v.setContentsMargins(26, 22, 26, 20)
    v.setSpacing(10)

    brand = QHBoxLayout()
    brand.setSpacing(14)
    mark = QLabel(card)
    mark.setPixmap(app_icon.pixmap(56))
    title = QLabel("弈道", card)
    title.setStyleSheet(f"font-size: 26px; font-weight: 700; color: {theme.INK};"
                        f" background: transparent;")
    tail = QLabel("AI 围棋教学 · 从 18 级练到九段", card)
    tail.setProperty("role", "muted")
    words = QVBoxLayout()
    words.setSpacing(2)
    words.addWidget(title)
    words.addWidget(tail)
    brand.addWidget(mark)
    brand.addLayout(words)
    brand.addStretch(1)
    v.addLayout(brand)

    bar = QProgressBar(card)
    bar.setRange(0, 0)          # 不确定进度条：真在动，只是不知道还要多久
    v.addWidget(bar)
    status = QLabel("正在准备…", card)
    status.setProperty("role", "muted")
    status.setAlignment(Qt.AlignCenter)
    v.addWidget(status)

    card.setFixedSize(440, 168)
    splash.setFixedSize(440, 168)
    return splash


#: 二次启动那句提示在自己消失前停留多久。5 秒足够读完两行字，又长到不会
#: 让人怀疑「刚才是不是弹过东西」。比它长就是拿一个无关紧要的告知拖住退出。
NOTICE_MS = 5000

#: 把日志目录指到别处的环境变量。与 `GO_CLIENT_INI` 同一个动机，但这里不是
#: 「顺手」：子进程形态的验收（真两进程跑 `app.py`）没法像同进程那样传
#: `install(log_dir=...)`，不开这个口子就会往开发者真实的 `logs/` 里写一堆假崩溃，
#: 而「打开日志目录」这个菜单项给出的正是那一个目录。
LOG_DIR_ENV = "GO_CLIENT_LOG_DIR"


def _autoclosing_notice(ms: int = NOTICE_MS) -> None:
    """一个**不要求点击**的提示框：自己开、到点自己关、事件循环跟着结束。

    为什么不用 `QMessageBox.information()`：那是模态的，等于把「第二次启动」
    变成一件需要用户收尾的事 —— 而第二次启动本来是一个**无操作**（唤起请求已经
    发出去了，什么也不需要他确认）。连点三次图标就是三个叠着的框，每一个都在
    等一次点击；无人值守的自动化验收也会直接挂在 `exec()` 上。
    同一个理由在本仓已有一条先例：`ui/chrome.py` 的「最近日志」用 `box.open()`
    而不是 `exec()`。关闭与退出各一个定时器：先关框再退循环，顺序反了会
    看见一个被截断的框（或者根本来不及关）。
    """
    app = QApplication.instance()
    box = QMessageBox(None)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle("已经在运行")
    box.setText("弈道已经开着了，刚才那次启动只是去叫它把窗口拿回来。\n"
                "要是没看见窗口，看一看任务栏（Windows 可能只闪一下图标而不抢焦点）。")
    box.setModal(False)
    box.show()
    QTimer.singleShot(ms, box.close)
    QTimer.singleShot(ms + 200, app.quit)
    app.exec()


#: 任务栏/跳转列表上的应用身份。不设的话 Windows 会拿宿主解释器（pythonw.exe）的身份
#: 来分组与取图标 —— 从源码跑时任务栏就可能显示 Python 的图标而不是我们的棋盘。
APP_USER_MODEL_ID = "Yidao.Desktop"


def _set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> None:
    """Windows 上给进程一个显式的 AppUserModelID（其它平台是空操作）。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:                                     # noqa: BLE001
        pass                                              # 设不上不影响功能，只是图标可能回落


def main(on_shell_ready=None, gate=None, acquire_lock: bool = True) -> int:
    """启动客户端，返回退出码。

    三个参数都是**验收缝**，生产调用一个也不传：
      · `on_shell_ready` —— 拿到真窗口去驱动、去截图；
      · `gate` / `acquire_lock` —— 单实例锁用哪个门、要不要拿（自动化脚本
        只跑一个窗口时用得到，见 `scripts/smoke_launch.py`）。
    """
    _set_app_user_model_id()                  # 必须早于窗口创建
    theme.configure_hi_dpi()                  # 必须在 QApplication 之前
    paths.apply_env()                         # 把 backend/ 放进 sys.path、定好 GO_DATA_DIR

    app = QApplication(sys.argv)
    app.setApplicationName("Yidao")
    app.setOrganizationName("Yidao")
    app.setStyle("Fusion")                   # QSS 在 windowsvista 上表现不可控
    theme.apply_app_font(app)
    app.setStyleSheet(theme.QSS)
    app.setWindowIcon(app_icon.icon())       # 任务栏与 alt-tab 上的那枚

    crash_log.install(os.environ.get(LOG_DIR_ENV) or None)   # 越早越好：后面每一步都可能炸

    if gate is None:
        gate = si.Gate()
    if acquire_lock and not gate.acquire():
        # 已经有实例在跑：把话说清楚再退。静默退出更糟 —— 用户看到的是
        # 「双击了图标却没反应」，而下一次他就不信这个程序了。
        _autoclosing_notice()
        crash_log.write("第二个实例：已把唤起请求发给先开的进程，本进程退出")
        return 0
    crash_log.write(f"单实例管道：{gate.name}（held={gate.held}）")

    # `GO_CLIENT_INI` 把偏好指到别处。留这个口子是为了**验收能真跑而不脏环境**：
    # 验收脚本会注册一个冒烟账号并写 token，走默认路径就会顶掉开发者自己的登录。
    prefs = Prefs(os.environ.get("GO_CLIENT_INI") or None)
    host = bh.BackendHost()

    launcher = Launcher(host, on_shell_ready, gate)
    launcher.prefs = prefs
    launcher._splash = make_splash()
    launcher._splash.show()
    launcher.start()

    app.aboutToQuit.connect(host.stop)       # 走完整 lifespan，带走引擎子进程
    app.aboutToQuit.connect(gate.release)
    app.aboutToQuit.connect(_on_quit)
    return app.exec()


def _on_quit() -> None:
    """退出时把日志收尾做完：限流窗口里攒下的重复次数不补就会丢。"""
    crash_log.flush_repeats()
    crash_log.write("退出：事件循环结束（aboutToQuit）")


if __name__ == "__main__":
    raise SystemExit(main())

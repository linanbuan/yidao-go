"""桌面惯例那一圈：全局动作、系统托盘、快捷键。

与 `shell.py` 的分工：shell 管「有哪些页面、页面之间怎么跳」，这里只管
「Windows 用户期待在任务栏右下角看到什么、以及哪些键能干什么」。**这里不实现任何业务动作**：
每个动作都是对已有东西的一次调用 —— 切页走 `shell.go()`，退出走 `shell.close()`，
导出/悔棋这类页面内动作直接点页面上那个对应的按钮。

为什么页面内的动作要"点按钮"而不是自己调后端：同一个动作有两个入口就是两份实现，
两份实现一定会漂移（按钮那边改了禁用条件，这条还照发请求）。让它去
`QPushButton.click()`，禁用逻辑、二次确认、音效、状态栏提示全都自动跟着页面走。
代价是可用性要跟着按钮同步，见 `sync()`。

四条与桌面惯例有关的口径：
  · **没有菜单栏**（2026-09-08 撤掉）：窗口顶部那一条「文件/视图/窗口/帮助」对这个
    应用是纯开销 —— 页面按钮与左侧导航已经覆盖了它的绝大部分，剩下的
    （打开日志目录/快捷键/关于/退出）搬进设置页。第 33 轮起「打开数据目录」
    与「查看最近日志」两个开发者动作也已删除，设置页的日志入口在引擎卡片里。
    动作本体全部 `shell.addAction()` 挂到窗口上，**快捷键一条不少**（Ctrl+Q/L/H、
    F5、F1、Alt+1~N、Ctrl+S/E/K）。撤掉菜单栏的直接收益是页面视口多回 25px。
  · **快捷键只有一条来源**：快捷键就是动作自己的 `shortcut`，而设置页那张
    「键盘快捷键」表是从这些 QAction 上**读**出来的（见 `shortcut_rows()`）。
    另外写一份表就会漂（表里有、实际按不动，是最难查的一类"看着有"缺陷）。
  · **X 仍然是真退出**，不藏进托盘。默认把退出藏起来意味着「关掉窗口后端还在跑」
    成为默认行为，而这个应用的后台是一个带子进程的引擎池。收进托盘只由显式动作
    （`Ctrl+H` / 托盘菜单）与一条可勾选的偏好（最小化时收进托盘）承担。
  · **没有托盘就不给"隐藏"这条路**：`hide()` 之后没有能把窗口点回来的东西，
    那是一条一去不回的路（测试跑的 offscreen 平台就是这个状态）。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMenu, QMessageBox, QSystemTrayIcon,
)

from core import crash_log, paths
from ui import app_icon

APP_TITLE = "弈道"

#: 页面内动作的镜像表：`(动作键, 页面键, 按钮属性名, 菜单文本, 快捷键)`。
#: 新增一条之前先确认页面上**真有**那个按钮 —— 这个表的全部意义就是不另起一套实现。
MIRRORED: tuple[tuple[str, str, str, str, str], ...] = (
    ("export_sgf", "game", "btnSgf", "导出 SGF 棋谱(&S)", "Ctrl+S"),
    ("export_md", "review", "btnExport", "导出复盘报告(&M)", "Ctrl+E"),
    ("takeback", "game", "btnTakeback", "悔棋两手(&K)", "Ctrl+K"),
)
#: 切页快捷键从 `Alt+1` 起（不是 `Ctrl+1`：那是浏览器的「切标签页」，
#: 从网页版搬过来的人会顺手按错；`Alt+数字` 是 Windows 程序的常规做法）。
#: 托盘不可用时「隐藏到托盘」的说明（禁用而不藏起来：藏起来用户会以为功能没了）。
NO_TRAY_HINT = "这台机器上没有可用的系统托盘，隐藏之后就没有地方能把窗口点回来。"

#: 设置页「应用」卡片上要摆的按钮（顺序即显示顺序）。这些动作**没有页面按钮可镜像**，
#: 菜单栏撤掉之后设置页就是它们唯一的可见入口 —— 少了这一处，它们就只剩快捷键，
#: 用户不知道它们存在（托盘只有隐藏/显示/退出三条）。
#: 「打开日志目录」不在这里：它住在设置页的引擎卡片里（装引擎失败的那一屏，
#: 日志就在那个语境里有用）；「打开数据目录」与「查看最近日志」是开发者工具，
#: 第 33 轮随启动器一起删除。
SETTINGS_KEYS: tuple[str, ...] = ("keys", "about", "quit")


def open_local(target) -> bool:
    """在资源管理器里打开一个目录/文件。单独一个函数是为了**可替换**：
    测试不许真开 Explorer（那会在用户机器上弹一堆窗口），见 `test_global_flow.py`。"""
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(target)))))


def plain(text: str) -> str:
    """去掉 Qt 的助记符标记（`导出 SGF 棋谱(&S)` → `导出 SGF 棋谱(S)`）。

    菜单上要留着 `&`（Alt+S 才管用），但同一份文本贴进「快捷键」那张表时
    留着 `&` 就成了一个用户看不懂的字面字符。`&&` 才是字面 &，所以按对处理。
    """
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "&" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append("&" if nxt == "&" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class Chrome(QObject):
    """主窗口的菜单 / 托盘 / 快捷键。由 `Shell` 持有，不单独使用。"""

    def __init__(self, shell, prefs, parent=None, tray_available: bool | None = None):
        super().__init__(parent)
        self._shell = shell
        self._prefs = prefs
        #: 能不能收进托盘是**系统能力**，不是偏好。offscreen 平台量出来是 False
        #: （实测），所以这一条要能注入 —— 单元测试必须能把「有托盘」那条分支也走一遍，
        #: 否则那半代码永远没被测过。传 None 就是照实问系统。
        self.tray_available = (QSystemTrayIcon.isSystemTrayAvailable()
                               if tray_available is None else bool(tray_available))
        self.icon = app_icon.icon()
        self.actions: dict[str, QAction] = {}
        #: 最后一次 `notify()` 的原文（没托盘时走状态栏，测试拿这一项断「用户得着了提示」）
        self.last_message = ""
        self._mirrors: list[tuple[str, str, str]] = []      # (动作键, 页面键, 按钮属性)

        self._build_actions()
        self.tray, self.tray_menu = self._build_tray()
        self.sync()

    # ---------------------------------------------------------------- 建

    def _act(self, key: str, text: str, shortcut: str = "", tip: str = "") -> QAction:
        act = QAction(text, self)
        if shortcut:
            act.setShortcut(shortcut)
        if tip:
            act.setToolTip(tip)
            act.setStatusTip(tip)
        self.actions[key] = act
        return act

    def _build_actions(self) -> None:
        """建全部 QAction，并**逐个挂到窗口上**。

        原来这些动作住在 `QMenuBar` 里、靠菜单栏在窗口里存活；菜单栏撤掉后必须显式
        `shell.addAction()` —— QAction 的快捷键只在「它属于当前窗口的控件树」时才生效，
        光创建不挂上去就是「表里有、实际按不动」。这一条是本文件最容易再犯的错。
        """
        for key, page_key, attr, text, sc in MIRRORED:
            act = self._act(key, text, sc, f"等同于{self._page_title(page_key)}页上的「{plain(text)}」按钮。")
            act.setData((page_key, attr))
            act.triggered.connect(lambda _=False, k=key: self._fire_mirror(k))
            self._mirrors.append((key, page_key, attr))

        act = self._act("open_logs", "打开日志目录(&L)", "Ctrl+L",
                        f"出问题时先把这个目录发给支持：{paths.LOGS_DIR}")
        act.triggered.connect(lambda _=False: self._open_logs())
        act = self._act("quit", "退出(&Q)", "Ctrl+Q", "退出会停掉本机的 AI 引擎。")
        act.triggered.connect(self._quit)

        for i, (key, title, _phase) in enumerate(self._shell.NAV):
            act = self._act(f"go_{key}", title, f"Alt+{i + 1}")
            act.setCheckable(True)
            act.triggered.connect(lambda _=False, k=key: self._shell.go(k))

        self._act("refresh", "刷新这一页(&R)", "F5").triggered.connect(self._refresh)

        act = self._act("hide", "隐藏到托盘(&H)", "Ctrl+H")
        act.triggered.connect(self._hide_to_tray)
        act = self._act("min_tray", "最小化时收进托盘(&M)", "",
                        "勾上以后点标题栏的最小化就是收进托盘；不勾就是普通最小化。")
        act.setCheckable(True)
        act.setChecked(bool(self._prefs.minimize_to_tray))
        act.toggled.connect(self._set_minimize_to_tray)
        self._act("show", "显示主窗口(&W)", "").triggered.connect(self._shell.wake)

        self._act("keys", "键盘快捷键(&K)", "F1").triggered.connect(self._show_shortcuts)
        self._act("about", "关于(&A)", "").triggered.connect(self._show_about)

        for act in self.actions.values():
            self._shell.addAction(act)

    def _build_tray(self):
        """托盘。菜单**总是**建（它就是那三条动作的落点），只有图标看系统给不给。

        父控件传窗口而不是 `self`：`Chrome` 是 `QObject`，而 `QMenu` 是控件，
        `QMenu(Chrome)` 会直接 `TypeError`（首跑就红在这里 —— 托盘不可用时这一段
        照样会跑，所以它不是边缘分支，是每个窗口都走的一条路）。
        """
        menu = QMenu(self._shell)
        menu.setObjectName("traymenu")
        a_show = QAction("显示主窗口", menu)
        a_show.triggered.connect(self._shell.wake)
        a_hide = QAction("隐藏到托盘", menu)
        a_hide.triggered.connect(self._hide_to_tray)
        a_quit = QAction("退出", menu)
        a_quit.triggered.connect(self._quit)
        menu.addAction(a_show)
        menu.addAction(a_hide)
        menu.addSeparator()
        menu.addAction(a_quit)
        self.tray_actions = {"show": a_show, "hide": a_hide, "quit": a_quit}
        if not self.tray_available:
            return None, menu
        tray = QSystemTrayIcon(self.icon, self._shell)
        tray.setToolTip(APP_TITLE)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        return tray, menu

    # ---------------------------------------------------------------- 动作

    def _page_title(self, key: str) -> str:
        return dict((k, t) for k, t, _p in self._shell.NAV).get(key, key)

    def _button(self, page_key: str, attr: str):
        page = self._shell.pages.get(page_key)
        return getattr(page, attr, None) if page is not None else None

    def _fire_mirror(self, key: str) -> None:
        act = self.actions[key]
        page_key, attr = act.data()
        btn = self._button(page_key, attr)
        if btn is None or not btn.isVisible():
            # 可用性一般由 `sync()` 管着，走到这里说明「打开菜单之后状态又变了」
            # （例如菜单开着时终局回包到了）。给一句话，不要静默不响应。
            self._shell.status.setText(
                f"「{plain(act.text())}」要在{self._page_title(page_key)}页里用。")
            return
        if not btn.isEnabled():
            self._shell.status.setText(
                f"{self._page_title(page_key)}页上的「{plain(act.text())}」现在按不动，"
                f"以那一页上的说明为准。")
            return
        btn.click()

    def _refresh(self) -> None:
        page = self._shell.current_page()
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()
            self._shell.status.setText("已刷新。")
        else:
            self._shell.status.setText("这一页没有需要刷新的内容。")

    def _open_dir(self, target) -> None:
        p = Path(target)
        p.mkdir(parents=True, exist_ok=True)     # 没建过的目录直接 openUrl 会让资源管理器报「路径不存在」
        open_local(p)

    def _open_logs(self) -> None:
        self._open_dir(paths.LOGS_DIR)

    def _hide_to_tray(self) -> None:
        if not self.tray_available:
            self._shell.status.setText(NO_TRAY_HINT)
            return
        self._shell.hide()
        self.notify("已收进托盘", "点托盘图标可以拿回窗口；要真退出请用托盘菜单里的「退出」。")

    def _quit(self) -> None:
        if not self._shell.close():        # 走 closeEvent：拆 WS/音效 + aboutToQuit 关掉后端
            return
        # 从托盘隐着的时候不保证 Qt 的「最后一个窗口已关」判定会自己收场（它看的是
        # 可见窗口），所以自己叫一下退出 —— 宁可重复一次，也不能让进程隐形地留着。
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _set_minimize_to_tray(self, on: bool) -> None:
        self._prefs.minimize_to_tray = bool(on)
        self._prefs.sync()

    def _on_tray_activated(self, reason) -> None:
        """单击/双击托盘图标切换显示。Windows 上用户习惯是**双击**，
        但 Win11 的许多应用已经改成单击唤回，所以两种都认。"""
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            if self._shell.isVisible():
                self._shell.hide()
            else:
                self._shell.wake()

    def notify(self, title: str, body: str = "") -> None:
        """气泡（有托盘时）或状态栏（没有时）。同一个口径，免得某条路径没人说话。"""
        self.last_message = f"{title} {body}".strip()
        if self.tray is not None:
            self.tray.showMessage(title, body, self.icon, 4000)
        else:
            self._shell.status.setText(self.last_message)

    # ---------------------------------------------------------------- 可用性

    def sync(self) -> None:
        """按「当前显示的是哪一页、那一页上的按钮此刻能不能点」重算菜单项。

        只看 `isVisible()` 就够判断「是不是这一页」：不在 QStackedWidget 当前页上的
        控件，其 `isVisible()` 是 False（被父级藏着）。
        """
        for key, page_key, attr in self._mirrors:
            btn = self._button(page_key, attr)
            ok = btn is not None and btn.isVisible() and btn.isEnabled()
            self.actions[key].setEnabled(bool(ok))
            if btn is not None and btn.toolTip():
                self.actions[key].setToolTip(btn.toolTip())     # 页面已经写明了理由就照抄
        cur = self._shell.current_page()
        for key, title, _phase in self._shell.NAV:
            self.actions[f"go_{key}"].setChecked(self._shell.pages.get(key) is cur)
        self.actions["hide"].setEnabled(self.tray_available)
        self.actions["hide"].setToolTip("" if self.tray_available else NO_TRAY_HINT)
        self.tray_actions["hide"].setEnabled(self.tray_available)
        self.actions["min_tray"].setEnabled(self.tray_available)
        self.actions["min_tray"].setChecked(bool(self._prefs.minimize_to_tray))
        self.actions["refresh"].setEnabled(cur is not None)

    # ---------------------------------------------------------------- 帮助

    def shortcut_rows(self) -> list[tuple[str, str]]:
        """`(按键, 做什么)` 列表，全部从真实 QAction 上读 —— 不另写一份表。"""
        rows = []
        for act in self.actions.values():
            sc = act.shortcut().toString()
            if sc:
                rows.append((sc, plain(act.text())))
        rows.sort(key=lambda r: r[0])
        return rows

    def about_text(self) -> str:
        """「关于」框的内容 —— 玩家向，不再是部署信息。

        早先这里印着 Qt/Python 版本、数据目录、本地服务地址：那是开发者
        在诊断时才关心的东西。第 33 轮改成「这是什么 + 出问题去哪看日志」。
        """
        return (f"{APP_TITLE}\n\n"
                f"AI 围棋教学：与 AI 对弈、死活练习、逐手复盘讲解，"
                f"从 18 级一路练到九段。\n\n"
                f"遇到问题可查看日志：{crash_log.log_path()}")

    def _show_shortcuts(self) -> None:
        lines = "".join(f"<tr><td><b>{k}</b></td><td>&nbsp;{v}</td></tr>"
                        for k, v in self.shortcut_rows())
        QMessageBox.information(self._shell, "键盘快捷键",
                                f"<table>{lines}</table>"
                                "<p>左右方向键在复盘页里逐手翻看。</p>")

    def _show_about(self) -> None:
        QMessageBox.about(self._shell, "关于", self.about_text())

    # ---------------------------------------------------------------- 窗口事件协助

    def should_hide_on_minimize(self) -> bool:
        return (self.tray_available and bool(self._prefs.minimize_to_tray)
                and self.tray is not None)

"""P5 全局项验收：菜单栏、快捷键、系统托盘、单实例锁、崩溃日志、窗口几何。

这一层的缺陷有一个共同特点：**行为断言管不着**。菜单少接一条线、快捷键跟别人撞了、
托盘没了还能把窗口藏成一去不回、日志写了但没人写进去 —— 页面本身一切正常，
红的全在页面外面。所以这里每一条都对着一件"用户看得见、测试看不见"的事：

  · 菜单项的可用性必须**跟着页面上那个按钮**走（口径见 `ui/chrome.py` 文档：
    菜单不许另起一套实现）；
  · 快捷键必须真的能按动（`QTest.keyClick` 走 Qt 的 shortcut 分发，不是调函数）；
  · 托盘不可用时"隐藏"这条路必须禁掉并说明（本机 offscreen 量出来就是不可用）；
  · 二次启动必须唤起**已经开着**的那个窗口，而不是开出第二个；
  · 异常必须落盘、必须限流（实测同一异常 200ms 能打 23 条）、且不许改控制流。

跑法与其余桌面测试相同（`QT_QPA_PLATFORM=offscreen`）。所有对话框与"打开资源管理器"
都被换成计数器 —— 测试不许在开发者机器上弹真窗口。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QPoint, QRect, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from core import backend_host as bh
from core import crash_log
from core import paths
from core import single_instance as si
from core.settings import Prefs
from ui import app_icon
from ui import chrome as chrome_mod
from ui import shell as shell_mod
from ui import theme as theme_mod
from ui.pages import review as review_page
from ui.widgets.parts import WrapLabel
from tests import harness as H

PASSWORD = "quanju123"
DESKTOP = Path(__file__).resolve().parent.parent


def drain(qapp, predicate, timeout: float = 15.0) -> bool:
    """转事件循环直到条件成立（跨线程投递不是在 processEvents 的一瞬到达的）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(scope="module")
def host():
    h = bh.BackendHost()
    h.start(timeout=90.0)
    yield h
    h.stop()


@pytest.fixture
def prefs(tmp_path):
    """偏好指到临时 ini：测试会写 token 与窗口几何，不许碰开发者自己的 AppData。"""
    return Prefs(str(tmp_path / "client.ini"))


def test_prefs_migrates_the_old_org_file_once(tmp_path):
    """更名（GoTeach → Yidao）后第一次运行：旧 ini 原样搬过来，登录态与几何不丢。

    搬的是**整个文件**：逐键复制既漏得了将来新增的键，也保不住 QSettings 的分组写法。
    """
    old = tmp_path / "GoTeach" / "client.ini"
    new = tmp_path / "Yidao" / "client.ini"
    old.parent.mkdir(parents=True)
    old.write_text("[auth]\ntoken=abc\n", encoding="utf-8")

    assert Prefs._migrate_legacy(str(new), str(old)) is True
    assert new.read_text(encoding="utf-8") == old.read_text(encoding="utf-8")

    # 第二次不再搬，更不许覆盖已经写过的新文件
    new.write_text("[auth]\ntoken=def\n", encoding="utf-8")
    assert Prefs._migrate_legacy(str(new), str(old)) is False
    assert "def" in new.read_text(encoding="utf-8")


def test_prefs_migration_is_a_noop_when_there_is_nothing_to_migrate(tmp_path):
    new = tmp_path / "Yidao" / "client.ini"
    assert Prefs._migrate_legacy(str(new), str(tmp_path / "nope.ini")) is False
    assert not new.exists()


def test_prefs_knows_the_new_org_and_the_old_one():
    """org 名与旧名都写在类上：改名时这条会红，提醒别丢用户的登录态。"""
    assert Prefs.ORG == "Yidao"
    assert Prefs.LEGACY_ORG == "GoTeach"


@pytest.fixture
def make_shell(qapp, host, prefs):
    """建真窗口（可选"有托盘"），默认顺手注册一个新账号进到主界面。

    为什么默认要真登录：`go()` 之后页面按钮的 `isVisible()` 决定菜单可用性，
    而没登录时 stack 停在登录页 —— 那时量到的"菜单是灰的"是**布局之外**的原因，
    测不出镜像逻辑（`test_pages` 里那条假绿教训在这里同样成立）。
    """
    built: list = []

    def make(tray_available: bool = False, login: bool = True):
        sh = shell_mod.Shell(host, prefs, tray_available=tray_available)
        sh.resize(1280, 800)
        sh.show()
        qapp.processEvents()
        if login:
            new_user(qapp, sh)
        built.append(sh)
        return sh

    yield make
    for sh in built:
        sh.close()
        sh.deleteLater()
    qapp.processEvents()


def new_user(qapp, sh) -> str:
    username = "g" + uuid.uuid4().hex[:8]
    QTest.keyClicks(sh._login.user, username)
    QTest.keyClicks(sh._login.pw, PASSWORD)
    QTest.mouseClick(sh._login.btnRegister, Qt.LeftButton)
    assert drain(qapp, lambda: sh._stack.currentIndex() == 1), \
        f"注册没进去：{sh._login.error.text()!r}"
    return username


def press(widget, key, mod=Qt.KeyboardModifier.NoModifier) -> None:
    """按键并给事件循环几轮 —— shortcut 分发发生在按键之后的派发里。"""
    QTest.keyClick(widget, key, mod)
    H._process(4)


class FakeFileDialog:
    """页面上的「另存为」在测试里必须是个计数器：模式框会把整支测试挂死。"""

    calls: list[tuple] = []

    @classmethod
    def getSaveFileName(cls, *_a, **_k):
        cls.calls.append(_a)
        return "", None


class FakeBox:
    """`QMessageBox` 的替身（同样是为了不弹真窗），记下每次弹的标题与正文。

    代码里会读 `QMessageBox.Icon.Information` / `StandardButton.Close` 两个枚举，
    替身里得有同名东西，不然换成替身的那一刻就 AttributeError。
    """

    shown: list[tuple[str, str]] = []
    opened = 0

    class Icon:
        Information = 1

    class StandardButton:
        Close = 1

    @staticmethod
    def information(_parent, title, text, *_a, **_k):
        FakeBox.shown.append((title, text))
        return 1

    def __init__(self, *a, **k):
        self.args = a
        FakeBox.shown.append((str(a[1]) if len(a) > 1 else "",
                              str(a[2]) if len(a) > 2 else ""))

    def setDetailedText(self, text):            # noqa: N802  沿用 Qt 的名字
        self.detail = text

    def open(self):                             # noqa: N802
        FakeBox.opened += 1


# ---------------------------------------------------------------- 动作与入口

def test_there_is_no_menu_bar_and_the_content_keeps_its_height(make_shell, qapp):
    """菜单栏已撤（2026-09-08）：窗口里不该再有 `QMenuBar`，页面视口拿回它占的高度。

    撤之前实测 1280x800 下内容区 746 高（菜单条 25px 是从页面视口里扣的），
    撤之后回到 771 —— 这条同时钉住「别哪天又把它加回来」。
    """
    from PySide6.QtWidgets import QMenuBar

    sh = make_shell()
    assert not sh.findChildren(QMenuBar), "菜单栏又回来了"
    assert sh.layout().itemAt(0).widget() is sh._stack, "布局第一项应当是内容区"
    assert sh.content_size().height() >= 760, \
        f"内容视口只有 {sh.content_size().height()} 高（撤菜单栏前是 746）"


def test_every_action_is_registered_on_the_window_and_reachable(make_shell, qapp):
    """动作不许「没人接着」，也不许「没有入口」。

    两条都是这一层最难查的静默缺陷：
    ① `connect` 写错目标名字 → 点了没反应（`receivers("2triggered()")` 是 PySide6 里
       唯一能问出这件事的口径）；
    ② 菜单栏撤掉后，动作如果没 `shell.addAction()` 挂到窗口上，快捷键就变成
       「表里有、按不动」——光创建 QAction 是没有快捷键的。
    每个动作还必须至少有一个可见入口：页面按钮镜像 / 设置页「应用」卡片 /
    托盘菜单 / 快捷键（`Alt+数字`、F5 这类）。
    """
    sh = make_shell()
    chrome = sh.chrome
    orphans = [k for k, a in chrome.actions.items()
               if not a.receivers("2triggered()") and not a.receivers("2toggled(bool)")]
    assert not orphans, f"这些动作没接任何槽：{orphans}"

    on_window = set(sh.actions())
    missing = [k for k, a in chrome.actions.items() if a not in on_window]
    assert not missing, f"这些动作没挂到窗口上（快捷键会失效）：{missing}"

    mirrored = {k for k, _, _ in chrome._mirrors}
    known = (mirrored | set(chrome_mod.SETTINGS_KEYS) | set(chrome.tray_actions)
             | {k for k in chrome.actions if k.startswith("go_")}
             | {"refresh", "min_tray"})
    # 没入口就得有快捷键（`open_logs` 就是这样：它的可见按钮在设置页引擎卡片里，
    # 不在这张表的任何一组里，靠 Ctrl+L 与 F1 那张表被找到）。
    unreachable = [k for k, a in chrome.actions.items()
                   if k not in known and not a.shortcut().toString()]
    assert not unreachable, f"这些动作既没入口也没快捷键：{unreachable}"


def test_every_nav_page_has_a_checkable_action(make_shell, qapp):
    """每一页都要有对应的可勾选动作：加页面只动 `Shell.NAV` 一张表，靠这一条兜底。"""
    sh = make_shell()
    for i, (key, title, _phase) in enumerate(shell_mod.NAV):
        act = sh.chrome.actions.get(f"go_{key}")
        assert act is not None, f"导航里的 {key} 没有对应动作"
        assert act.text() == title
        assert act.shortcut().toString() == f"Alt+{i + 1}"
        assert act.isCheckable(), "切页动作不可勾选，就看不出当前在哪一页"


def test_the_current_page_is_the_only_checked_nav_item(make_shell, qapp):
    sh = make_shell()
    sh.go("ranks")
    H._process()
    checked = [k for k, a in sh.chrome.actions.items()
               if k.startswith("go_") and a.isChecked()]
    assert checked == ["go_ranks"], checked


def test_the_shortcut_table_is_read_from_the_actions(make_shell, qapp):
    """「帮助 → 键盘快捷键」那张表必须是读出来的，不是另写一份。

    另写的表迟早会漂成「表里有、实际按不动」，而那是最气人的一类缺陷。
    """
    sh = make_shell()
    chrome = sh.chrome
    rows = dict(chrome.shortcut_rows())
    from_actions = {a.shortcut().toString(): chrome_mod.plain(a.text())
                    for a in chrome.actions.values() if a.shortcut().toString()}
    assert rows == from_actions, "表与 QAction 上的快捷键不是同一份东西了"
    want = {"Ctrl+S", "Ctrl+E", "Ctrl+K", "Ctrl+L", "Ctrl+Q", "Ctrl+H", "F5", "F1",
            "Alt+1", "Alt+6"}
    missing = want - set(rows)
    assert not missing, f"这些键没进表：{missing}"
    assert all("&" not in label for label in rows.values()), "表里留着助记符 &"


def test_no_two_actions_share_a_shortcut(make_shell, qapp):
    """两个动作同一个键 = 其中一个永远按不动（而且没人报错）。"""
    sh = make_shell()
    seen: dict[str, str] = {}
    for key, act in sh.chrome.actions.items():
        sc = act.shortcut().toString()
        if not sc:
            continue
        assert sc not in seen, f"{sc} 同时挂在 {seen.get(sc)} 与 {key} 上"
        seen[sc] = key


# ---------------------------------------------------------------- 按得动的快捷键

def test_alt_number_really_switches_page(make_shell, qapp):
    """走 Qt 的按键分发，不是直接调 `go()`。

    实测（offscreen + PySide6 6.11.2）：`QTest.keyClick(窗口, Alt+3)` 会触发
    `QAction` 的 shortcut；焦点落在子控件（输入框）上时同样有效。
    """
    sh = make_shell()
    hits: list[str] = []
    sh.pageChanged.connect(hits.append)
    press(sh, Qt.Key_3, Qt.AltModifier)
    assert hits and hits[-1] == "tsumego", hits
    assert type(sh.current_page()).__name__ == "TsumegoPage"


def test_alt_number_works_while_the_focus_is_in_a_field(make_shell, qapp):
    """用户按 Alt+2 时焦点大概率在某个输入框里 —— 那条路也必须是通的。"""
    sh = make_shell()
    sh.go("settings")
    hits: list[str] = []
    sh.pageChanged.connect(hits.append)
    focused = sh._pages["settings"].findChild(QPushButton) or sh._pages["settings"]
    focused.setFocus()
    press(focused, Qt.Key_2, Qt.AltModifier)
    assert hits[-1:] == ["game"], hits


def test_f5_refreshes_the_current_page(make_shell, qapp):
    sh = make_shell()
    sh.status.setText("")
    press(sh, Qt.Key_F5)
    assert sh.status.text() == "已刷新。", sh.status.text()


def test_ctrl_s_does_nothing_off_the_game_page(make_shell, qapp):
    """`Ctrl+S` 只在有 SGF 可导的时候有意义：在大厅按下去不该导出任何东西。"""
    sh = make_shell()
    sh.go("lobby")
    calls: list[str] = []
    real_fire = sh.chrome._fire_mirror
    sh.chrome._fire_mirror = lambda k: (calls.append(k), real_fire(k))[1]
    sh.status.setText("")
    press(sh, Qt.Key_S, Qt.ControlModifier)
    H._process()
    assert not calls, "没在对局页也去点导出按钮了"
    assert not sh.chrome.actions["export_sgf"].isEnabled(), "灰项在大厅里居然是可点的"


# ---------------------------------------------------------------- 镜像项

def test_mirror_items_are_disabled_while_off_their_page(make_shell, qapp):
    sh = make_shell()
    sh.go("lobby")
    for key in ("export_sgf", "export_md", "takeback"):
        assert sh.chrome.actions[key].isEnabled() is False, f"{key} 在大厅里不该可点"


def test_mirror_item_tracks_the_real_button_state(make_shell, qapp):
    """可用性必须**等于**页面上那个按钮的状态，且改完会跟着改。

    这是「同一个动作两个入口一定漂移」那件事的机械化：先让它可点，
    再把按钮禁掉并 `sync()`，菜单不许还亮着。
    """
    sh = make_shell()
    sh.go("review")
    page = sh.pages["review"]
    chrome = sh.chrome
    page.game_id = "abcd1234ef56"
    page.report = None
    page._paint_buttons()
    chrome.sync()
    assert chrome.actions["export_md"].isEnabled() is False
    page.report = {"summary": "有一份报告"}
    page._paint_buttons()
    chrome.sync()
    assert chrome.actions["export_md"].isEnabled() is True, "按钮亮了菜单还灰着（两份实现）"
    page.btnExport.setEnabled(False)
    chrome.sync()
    assert chrome.actions["export_md"].isEnabled() is False


def test_sync_recomputes_the_items(make_shell, qapp):
    """`sync()` 是可用性的唯一重算入口：页面状态一变，动作就得跟着变。"""
    sh = make_shell()
    sh.go("review")
    page = sh.pages["review"]
    act = sh.chrome.actions["export_md"]
    page.report = {"summary": "有"}
    page._paint_buttons()
    sh.chrome.sync()
    assert act.isEnabled()
    page.report = None
    page._paint_buttons()
    assert act.isEnabled(), "前置条件：没重算时它还是旧的"
    sh.chrome.sync()
    assert act.isEnabled() is False, "重算之后还留着一条已经按不动的导出"


def test_the_action_clicks_the_page_button_instead_of_its_own_copy(make_shell, qapp,
                                                                   monkeypatch):
    """动作走的必须是页面那个按钮的实现 —— 连文件名拼接都在页面里。"""
    FakeFileDialog.calls = []
    monkeypatch.setattr(review_page, "QFileDialog", FakeFileDialog)
    sh = make_shell()
    sh.go("review")
    page = sh.pages["review"]
    page.game_id = "abcd1234ef56"
    page.report = {"summary": "有"}
    page._paint_buttons()
    sh.chrome.sync()
    assert sh.chrome.actions["export_md"].isEnabled()
    sh.chrome.actions["export_md"].trigger()
    H._process()
    assert len(FakeFileDialog.calls) == 1, FakeFileDialog.calls
    args = FakeFileDialog.calls[0]
    assert "导出复盘报告" in str(args[1]), args
    assert "review_abcd1234.md" in str(args[2]), f"文件名不是页面那套拼法：{args}"


def test_a_mirror_that_cannot_act_says_so(make_shell, qapp):
    """按不动时得说话：静默不响应就是用户眼里的「这个菜单坏了」。"""
    sh = make_shell()
    sh.go("review")
    chrome = sh.chrome
    page = sh.pages["review"]
    page.report = None
    page._paint_buttons()
    sh.status.setText("")
    chrome._fire_mirror("export_md")            # 绕开 QAction 的禁用短路，走守卫本身
    assert "按不动" in sh.status.text(), sh.status.text()
    sh.go("lobby")
    sh.status.setText("")
    chrome._fire_mirror("export_sgf")           # 对局页还没建 = 没有那个按钮
    assert "对局页" in sh.status.text(), sh.status.text()


# ---------------------------------------------------------------- 托盘

def test_without_a_tray_hiding_is_disabled_and_explained(make_shell, qapp):
    """offscreen 平台量出来 `isSystemTrayAvailable() == False`，与用户关掉托盘一样。

    这一条要禁掉而不是藏起来：藏起来用户以为功能没了，禁掉 + 说明才知道为什么。
    """
    sh = make_shell(tray_available=False)
    chrome = sh.chrome
    assert chrome.tray is None
    assert chrome.actions["hide"].isEnabled() is False
    assert chrome.actions["hide"].toolTip() == chrome_mod.NO_TRAY_HINT
    assert chrome.tray_actions["hide"].isEnabled() is False
    sh.status.setText("")
    chrome._hide_to_tray()
    assert sh.isVisible(), "没有托盘还藏了窗口 —— 一去不回"
    assert sh.status.text() == chrome_mod.NO_TRAY_HINT


def test_a_trayless_machine_never_hides_on_minimize(make_shell, qapp):
    """偏好开着但机器没托盘：最小化只能是普通最小化。

    这是最坏的一种组合（任务栏上找不到图标、右下角也没有），所以它由
    `should_hide_on_minimize()` 同时看两样东西挡掉。
    """
    sh = make_shell(tray_available=False)
    sh.prefs.minimize_to_tray = True
    sh.chrome.sync()
    assert sh.chrome.should_hide_on_minimize() is False
    sh.showMinimized()
    H._process(6)
    assert sh.isVisible(), "没托盘还把窗口收走了"
    assert sh.chrome.actions["min_tray"].isEnabled() is False, "勾了一个不会生效的开关"


def test_with_a_tray_the_window_can_be_parked_and_woken(make_shell, qapp):
    """有托盘那一支必须也被跑到（注入 `tray_available=True`），否则那半代码没人验。"""
    sh = make_shell(tray_available=True)
    chrome = sh.chrome
    assert chrome.tray is not None
    assert chrome.actions["hide"].isEnabled()
    chrome._hide_to_tray()
    H._process(4)
    assert not sh.isVisible(), "说收进托盘却没收起来"
    assert "收进托盘" in chrome.last_message, chrome.last_message
    sh.wake()
    H._process(4)
    assert sh.isVisible(), "唤回之后窗口还是藏着的"
    assert sh.isMinimized() is False, "从最小化收进去的，唤回得是正常态"


def test_minimizing_goes_to_the_tray_only_when_asked(make_shell, qapp):
    sh = make_shell(tray_available=True)
    sh.chrome._set_minimize_to_tray(True)
    sh.showMinimized()
    assert drain(qapp, lambda: not sh.isVisible(), 3.0), "勾了收进托盘却还挂在最小化上"
    sh.wake()
    sh.chrome._set_minimize_to_tray(False)
    sh.showMinimized()
    H._process(6)
    assert sh.isVisible() and sh.isMinimized(), "没勾就得是普通最小化，不能把窗口收走"
    sh.wake()


def test_the_tray_preference_survives_a_restart(make_shell, qapp, tmp_path):
    """勾选写盘、下次启动照原样 —— 不写盘的开关等于没有。"""
    sh = make_shell(tray_available=True)
    sh.chrome.actions["min_tray"].trigger()
    assert sh.chrome.actions["min_tray"].isChecked()
    again = Prefs(str(tmp_path / "client.ini"))
    assert again.minimize_to_tray is True, "菜单勾了但没 sync() 到磁盘"
    sh.chrome._set_minimize_to_tray(False)


def test_quit_closes_the_window_and_then_ends_the_loop(make_shell, qapp, monkeypatch):
    """退出走的是 `close()`（有确认与拆解），之后自己叫一下 `quit()`。

    从托盘隐着的时候不保证 Qt 的「最后一个窗口已关」判定会收场，所以那一声不能省。
    """
    sh = make_shell(tray_available=True)
    monkeypatch.setattr(chrome_mod, "QApplication", _Quitter)
    _Quitter.calls = 0
    sh.chrome._quit()
    assert _Quitter.calls == 1
    assert not sh.isVisible()


def test_a_cancelled_close_does_not_end_the_process(make_shell, qapp, monkeypatch):
    """`close()` 被拒（关不掉）时不许退出 —— 症状会是「程序没了但窗口还挂着」。"""
    sh = make_shell(tray_available=True)

    class NoClose:
        def close(self, *a):
            return False

    # 换 `chrome._shell` 而不是给窗口贴一个 `close`：同一个对象上的
    # monkeypatch 要到用例末尾才还，而夹具收尾时就要用真 `close()`。
    real_shell = sh.chrome._shell
    sh.chrome._shell = NoClose()
    monkeypatch.setattr(chrome_mod, "QApplication", _Quitter)
    _Quitter.calls = 0
    try:
        sh.chrome._quit()
    finally:
        sh.chrome._shell = real_shell
    assert _Quitter.calls == 0
    assert sh._watchdog.isActive(), "没退成就把看门狗停了（后端从此没人看）"


class _Quitter:
    calls = 0

    @classmethod
    def instance(cls):
        return cls

    @classmethod
    def quit(cls):
        cls.calls += 1


def test_closing_the_window_is_a_real_quit(make_shell, qapp, tmp_path):
    """口径：X 是真退出，不是藏进托盘（后台是带子进程的引擎池）。"""
    sh = make_shell(tray_available=True)
    sh.show()
    H._process()
    assert sh.close() is True
    assert not sh.isVisible()
    assert sh._watchdog.isActive() is False, "关窗前没停后端看门狗"
    text = Path(tmp_path / "client.ini").read_text(encoding="utf-8")
    assert "geometry=" in text, f"退出时没存窗口几何：{text!r}"


def test_notify_falls_back_to_the_status_bar(make_shell, qapp):
    """两条路都得有人说话：有托盘发气泡，没托盘写状态栏（不许静默）。"""
    sh = make_shell(tray_available=False)
    sh.status.setText("")
    sh.chrome.notify("引擎掉了", "已改用内置启发式。")
    assert sh.chrome.last_message == "引擎掉了 已改用内置启发式。"
    assert "引擎掉了" in sh.status.text()


# ---------------------------------------------------------------- 单实例锁

def unique_name() -> str:
    """每用例一根自己的管道：名字带随机后缀就不会与开发机上真开着的客户端撞。"""
    return f"Yidao.test-{uuid.uuid4().hex[:10]}"


def child_knock(name: str, payload: bytes, qapp, timeout: float = 45.0) -> str:
    """在**另一个进程**里敲一下管道，返回子进程报回的那行（`connected=... wrote=...`）。

    为什么不直接在测试进程里再开一个 `QLocalSocket`：同进程的写法连试四条都不稳
    （循环被客户端占死、异步写未送达、服务端 200ms 读窗口撞不上），
    详细的实测记录在 `tests/child_pipe.py` 的 docstring 里 —— 这一条必须红绿分明，
    不能靠运气。载荷走十六进制参数：要能发出「认不出的字节」才算测到白名单。
    """
    out = tmp_child_file()
    proc = subprocess.Popen(
        [sys.executable, str(DESKTOP / "tests" / "child_pipe.py"),
         si.sanitize(name), payload.hex(), str(out)],
        cwd=str(DESKTOP),
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8"))
    try:
        got = drain(qapp, lambda: out.exists() and bool(
            out.read_text(encoding="utf-8", errors="ignore").strip()), timeout)
        assert got, "第二个进程没能报回结果（起不来？）"
        return read_child(out)
    finally:
        proc.wait(timeout=90)
        out.unlink(missing_ok=True)


def test_second_launch_wakes_the_first_window(make_shell, qapp):
    """计划口径：单实例「二次启动只唤起已有窗口」—— 用**真两个进程**验。

    子进程只跑 `Gate`（不建窗口），它报回的 `acquire()=False` 就是 `app.py` 里
    「不开第二个窗口」那个分支拿到的同一个返回值 —— 两边的口径就此对上了。
    """
    name = unique_name()
    sh = make_shell()
    first = si.Gate(name)
    assert first.acquire() is True, "第一个实例拿不到锁"
    first.activated.connect(sh.wake)              # 与 `app.py` 里接的是同一个槽
    sh.hide()
    H._process()
    assert not sh.isVisible(), "前置条件：窗口得先是藏着的"

    out = tmp_child_file()
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from core import single_instance as si;"
        "g = si.Gate(%r);"
        "open(r'%s', 'w', encoding='utf-8').write("
        "f'{g.acquire()}|{g.held}|{g.is_running()}')" % (str(DESKTOP), name, str(out))
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(DESKTOP), env=env)
    try:
        assert drain(qapp, lambda: out.exists() and bool(
            out.read_text(encoding="utf-8", errors="ignore").strip()), 45.0), \
            "第二个进程没能报回结果"
        acquired, held, running = read_child(out).split("|")
        assert acquired == "False", f"第二个进程也拿到了锁 → 它会开第二个窗口：{acquired}"
        assert held == "False" and running == "True", f"它看见的不是先开那个：{running}"
        assert drain(qapp, lambda: sh.isVisible(), 15.0), "二次启动没把先开的窗口叫回来"
        assert first.wake_requests == 1
        assert first.held is True, "叫回来之后锁不能丢"
    finally:
        proc.wait(timeout=90)
        out.unlink(missing_ok=True)
        first.release()


def load_entry():
    """按文件加载 `desktop/app.py`，模块名换成 `desktop_entry`。

    不能 `import app`：后端的顶层包也叫 `app/`，占掉这个名字 uvicorn 就报
    `'app' is not a package`（同 `scripts/smoke_launch.py` 里的处理与 app.py 的文档）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("desktop_entry", DESKTOP / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_second_instance_talks_but_never_asks_for_a_click(qapp, monkeypatch):
    """拿不到锁那一句要说清，但**不许模态**：第二次启动本质上是一个无操作。

    原写法是 `QMessageBox.information(...)`（模态）。后果不只是无人值守的验收
    永久挂在 `exec()` 上（下面那两条真两进程的就是在这儿停住的）：用户连点
    三次图标就是三个叠着的框，每一个都在等一次「确定」—— 而窗口已经被唤回来了，
    还要他再确认一遍自己没做错事。同一个理由仓里已有一条先例：`ui/chrome.py` 的
    「最近日志」用 `box.open()` 而不是 `exec()`。
    """
    entry = load_entry()
    seen: list = []

    class SpyBox(entry.QMessageBox):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen.append(self)

    monkeypatch.setattr(entry, "QMessageBox", SpyBox)
    t0 = time.monotonic()
    entry._autoclosing_notice(ms=80)        # 不点任何东西，只等它自己走完
    dt = time.monotonic() - t0

    assert seen, "第二次启动什么都没弹：用户看到的还是「双击图标没反应」"
    box = seen[0]
    assert box.isModal() is False, "还是模态框：没人点它就永远不退出"
    assert "已经在运行" in box.windowTitle(), box.windowTitle()
    assert "任务栏" in box.text(), box.text()        # 要说「没看见窗口时看哪儿」
    assert not box.isVisible(), f"到点没关，还挂在屏幕上：{box.text()}"
    assert 0.05 <= dt < 3.0, f"没有自己退出（跑了 {dt:.2f}s）"


def test_two_real_client_processes_leave_one_window(qapp, tmp_path):
    r"""计划口径「真两进程二次启动」：跑两份**完整的 `app.py`**，第二份得自己走。

    与上面两条的分工（三条各顶一个缺口，缺一个就有半边没人看）：
      · `test_second_launch_wakes_the_first_window` —— 真子进程敲管道，验前半句
        「先开的那个窗口被叫回来了」（这一条跨进程看不到别人的 `wake_requests`）；
      · `..._talks_but_never_asks_for_a_click` —— 验那句话本身不模态；
      · 本条 —— 真的把入口跑两遍：第二个进程必须**不需要人点任何东西**就退出。
        【实测】把 `_autoclosing_notice` 换回原来的模态写法（`QMessageBox.information`）
        后，本条就红在「60 秒还没自己退出」那一句上 —— 不模态本条绿，模态本条红，
        不需要有人盯着一个挂死的窗口。

    两个子进程都把管道名顶到本趟专用的一根（`GO_CLIENT_PIPE`）：用默认名就会与
    开发者自己开着的那个客户端抢 —— 测试看见的「唤起成功」其实是唤走了人家那个。

    为什么不拿 `Popen.pid` 认人（实测事实，本条差点因此假绿）：这台机器上
    `.venv\Scripts\python.exe` 是一个**重定向器**，它会再投一个真正的解释器进程，
    于是 `Popen.pid` 与子进程里的 `os.getpid()` 根本不是同一个数（实测 18028 vs 11324）。
    把日志按进程分开（一人一个 `GO_CLIENT_LOG_DIR`）既能绕开它，又多一个好处：
    「第二份没开窗」可以直接在它自己的日志里断言，不靠共用一个文件去区分谁写的。
    """
    name = unique_name()
    # 合并字典而不是 `dict(os.environ, 键=值)`：管道名那个键取自 `si.PIPE_NAME_ENV`，
    # 是一个**表达式**而不是标识符，写成关键字参数就是语法错误。
    base_env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONIOENCODING": "utf-8",
        si.PIPE_NAME_ENV: name,
        "GO_DATA_DIR": str(tmp_path / "data"),
        "GO_CLIENT_INI": str(tmp_path / "client.ini"),
        # 这一条验的是入口与实例锁，与引擎无关。不关掉就会在真两进程那一支里
        # 多拉一个 katago.exe（几百 MB 显存），而 GPU 只有一个：慢且互相干扰。
        "GO_KATAGO_ENABLED": "false",
    }

    def launch(tag: str) -> tuple[subprocess.Popen, Path, Path]:
        out = tmp_path / f"{tag}.out"
        log = tmp_path / f"{tag}-logs" / "client.log"
        env = {**base_env, "GO_CLIENT_LOG_DIR": str(log.parent)}
        fh = out.open("wb")
        p = subprocess.Popen([sys.executable, str(DESKTOP / "app.py")],
                             cwd=str(DESKTOP), env=env, stdout=fh, stderr=fh,
                             stdin=subprocess.DEVNULL)
        p._log_handle = fh          # 只能这么挂：句柄被闭了就什么都看不见了
        return p, out, log

    def tail(path: Path) -> str:
        try:
            return path.read_bytes().decode("utf-8", errors="replace")[-1500:]
        except OSError as exc:
            return f"<读不到输出：{exc!r}>"

    def log_of(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "<还没建子进程日志>"

    first, first_out, first_log = launch("first")
    second = second_out = second_log = None
    probe = si.Gate(name)
    try:
        assert drain(qapp, lambda: probe.is_running(), 120.0), (
            f"第一个客户端 120 秒内没占住管道，它的输出：\n{tail(first_out)}")
        assert first.poll() is None, f"第一个进程自己退了：\n{tail(first_out)}"
        # 光「占住了管道 + 进程还活」不够：锁是在开窗口**之前**拿的，而开窗之后
        # 还有一道模态的「本地服务起不来」。不卡这一步，本条的名字（leave one
        # window）可以在一个窗口都没有时绿 —— 管道那边只保证 splash 都还没关。
        assert drain(qapp, lambda: "主窗口已出现" in log_of(first_log), 120.0), (
            "第一个客户端拿住了锁却没开出主窗口（还停在启动画面？），它的日志：\n"
            f"{log_of(first_log)}")

        second, second_out, second_log = launch("second")
        assert drain(qapp, lambda: second.poll() is not None, 60.0), (
            "第二个进程 60 秒还没自己退出（还在等一次点击？它不是无操作）：\n"
            f"{tail(second_out)}")
        assert second.returncode == 0, (
            f"第二个进程退出码 {second.returncode}：\n{tail(second_out)}")
        assert first.poll() is None, "第二个进程把先开的那个弄死了"
        assert probe.is_running(), "第二份跑完，第一份的入口名字没了（从此叫不动）"
        # 第二份只该留下一句「已把唤起请求发出去，本进程退出」，不许开窗。
        text = log_of(second_log)
        assert "主窗口已出现" not in text, f"第二个进程给自己也开了一个窗口：\n{text}"
        assert "已把唤起请求发给先开的进程" in text, (
            f"第二个进程退出了，但没说它是去叫过窗口才退的：\n{text}")
    finally:
        for p in (second, first):
            if p is not None and p.poll() is None:
                # 窗口开在一个**重定向器投出来的子进程**里，`terminate()` 只杀得到
                # 那个壳子 —— 不杀子树就会漏一个真客户端在机器上（下面紧接着
                # 一句就是查这个）。
                kill_tree(p)
        for p in (second, first):
            if p is None:
                continue
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutError:
                p.kill()
            finally:
                handle = getattr(p, "_log_handle", None)
                if handle is not None:
                    handle.close()
        # probe 从没 `listen` 过，这一句按设计是个空操作（`release()` 只许清自己
        # 占下的名字）；写在这儿是为了万一它哪天拿到了锁，不把管道留给下一趟。
        probe.release()
    # 收尾自己也要被查：只杀了重定向器时，真客户端还在跑、还在监听那个名字。
    # 不卡这一句，下一趟测试（以及开发者本人）就会撞上「刚才那个客户端怎么还在」。
    # 写在 `finally` 外面而不是里面：`finally` 里的失败会把真正那条缺陷的
    # 异常顶掉，排查时会拿着「没杀干净」去查一个根本不相关的问题。
    assert drain(qapp, lambda: not probe.is_running(), 30.0), (
        f"子树没杀干净：那个名字还有人接。子进程日志：\n{log_of(first_log)}")


def kill_tree(proc: subprocess.Popen) -> None:
    """杀掉一个进程**及其子进程**（Windows）。

    不用 `proc.terminate()`：它只杀 `Popen.pid` 那一个，而这台机器上的 venv
    `python.exe` 会重投一个真解释器（见上面那条测试的文档），症状是「测试过了，
    但开发者背上多了一个在跑的客户端」。

    带 `/F` 而不是只发 `WM_CLOSE`：本条的子进程数据目录在临时区，根本不该给
    它一个「退出时问一句」的机会（一个询问框就能把收尾挂住）。优雅关停那条路
    不在这里验（同进程的 `test_global_flow` 与 `smoke_launch` 已经验过
    `aboutToQuit` 会 `host.stop()`）。

    【实测】拿上面那条的变异品（一个永远不会有人点的模态框）跑过一遍：
    本函数跑完后 `Get-CimInstance Win32_Process` 查不到命令行含 `app.py` 的残留
    进程 —— 子树确实杀干净了，测试没在开发者背上留一个客户端。
    （末尾那句「管道名字没了」在那次并没能跑：它在 `try` 外面，而当时身体在
    `try` 里就红了。把收尾断言搬进 `finally` 会把真正的失败顶掉，不值得。）
    """
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True, timeout=30)


def tmp_child_file() -> Path:
    """子进程写的回报文件（放在 `artifacts/` 下，跟其余临时产物一起清）。"""
    p = DESKTOP / "artifacts" / f"_child_{uuid.uuid4().hex[:8]}.txt"
    if p.exists():
        p.unlink()
    return p


def read_child(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def test_the_loser_does_not_remove_the_winners_pipe(qapp):
    """第二个实例退出时 `aboutToQuit` 会走 `gate.release()`。

    它从没 `listen` 过，因此**不许**去清那个名字 —— 清了就是
    「先开的那个从此叫不动，而且看不出来」。
    """
    name = unique_name()
    first = si.Gate(name)
    assert first.acquire() is True
    second = si.Gate(name)
    assert second.acquire() is False
    second.release()
    assert first.held is True, "输的那个把赢的管道名清掉了"
    report = child_knock(name, si.REQUEST, qapp)
    assert report.startswith("connected=True"), f"第一个实例已经不接了：{report}"
    assert child_wakes(first, 1, qapp), f"名字叫对了却叫不动：{first.wake_requests}"
    first.release()


def child_wakes(gate: si.Gate, expect: int, qapp, timeout: float = 15.0) -> bool:
    """数唤起次数要先把服务端那一轮循环转完（增量而不是绝对值：
    上面那个 `second.acquire()` 里也发过一次请求，那点字节什么时候落地不由
    这条测试决定）。"""
    return drain(qapp, lambda: gate.wake_requests >= expect, timeout)


def test_an_unrecognized_request_does_not_steal_the_foreground(qapp):
    """管道名不带权限控制：本机任何进程都能写。收到东西就抢前台
    等于把焦点交给随便一个本机程序，所以只认 `REQUEST` 前缀。

    为什么不拿子进程报的 `wrote=True` 当「字节真送到了」的证据：实测它是
    客户端侧的异步写状态，服务端读完就把连接关了，同一payload 也会报
    `wrote=False`。不靠它，靠三步把「不 vacuous」钉住：
      ① 先发认不出的字节 → 唤起次数仍是 0；
      ② 再对同一根管道发 `activate` → 必须变成 1（这一句就证明了同一条链路
         本测试里是通的，于是①的 0 不是「根本没送到」）；
      ③ 再转两秒 → 必须是**恰好** 1，不是 2（迟到的垃圾不能补一声）。
    """
    name = unique_name()
    g = si.Gate(name)
    assert g.acquire() is True
    junk = child_knock(name, b"open C:/some/where/game.sgf", qapp)
    assert junk.startswith("connected=True"), f"连都没连上：{junk}"
    H.settle(qapp, 2.0)          # 「不该醒」只能靠转足时间断，不能靠一瞬的判定
    assert g.wake_requests == 0, "认不出的字节串也把窗口叫出来了"
    ok = child_knock(name, si.REQUEST, qapp)
    assert ok.startswith("connected=True"), f"第二次连都没连上：{ok}"
    assert drain(qapp, lambda: g.wake_requests == 1, 15.0), \
        f"前缀对了却叫不动（链路本身不通，那①就是 vacuous）：{g.wake_requests}"
    H.settle(qapp, 2.0)
    assert g.wake_requests == 1, f"多出了一声：{g.wake_requests}"
    g.release()


def test_is_running_asks_without_waking(qapp):
    name = unique_name()
    first = si.Gate(name)
    assert first.acquire() is True
    probe = si.Gate(name)
    assert probe.is_running() is True
    other = si.Gate(unique_name())
    assert other.is_running() is False, "探路探到了一个根本没人的名字（假信号）"
    H.settle(qapp, 0.6)
    assert first.wake_requests == 0, "只是问一句，不该把人家窗口叫出来"
    first.release()


def test_release_lets_the_same_name_be_taken_again(qapp):
    name = unique_name()
    a = si.Gate(name)
    assert a.acquire() is True
    assert a.acquire() is True, "重复 acquire 不许把锁弄丢"
    a.release()
    assert a.held is False
    b = si.Gate(name)
    assert b.acquire() is True, "放手的名字没清掉：同进程重开就永远起不来"
    b.release()


def test_a_broken_lock_still_lets_the_client_start(qapp):
    """这一层是打磨，不是安全边界：拦住启动比开两个窗口严重得多。"""
    class DeafGate(si.Gate):
        def _try_listen(self) -> bool:
            return False

    g = DeafGate(unique_name())
    assert g.acquire() is True, "锁自己坏了就把启动拦死 → 用户看到的是「双击没反应」"
    assert g.held is False
    assert g.last_error, "放行是放行了，但死因得留痕（启动时会进日志）"


def test_the_pipe_name_env_gives_a_second_client_its_own_door(monkeypatch):
    """`GO_CLIENT_PIPE` 存在的理由：验收要同机开两个真客户端而不去碰开发者那个。

    钉三件事：它真的能换掉名字（否则两个客户端仍然互拦）；它仍然过
    `sanitize`（名字是递给系统 API 的，不能把任意字符递过去）；没设时行为不变
    —— 这一条防的是「以后有人拿这个口子把默认名改没了，单实例本身失效」。
    """
    plain = si.default_name()
    monkeypatch.setenv(si.PIPE_NAME_ENV, "Yidao.test 第二份/a")
    named = si.default_name()
    assert named != plain, "环境变量没被读：两个客户端还是会互相拦"
    assert named.startswith("Yidao.test") and named != "Yidao.test 第二份/a"
    assert all(c.isalnum() or c in "._-" for c in named), named
    monkeypatch.setenv(si.PIPE_NAME_ENV, "   ")
    assert si.default_name() == plain, "空值也算设过了？那默认链就断了"


def test_the_pipe_name_is_this_install_only(monkeypatch):
    n1, n2 = si.default_name(), si.default_name()
    assert n1 == n2 and n1.startswith(si.SERVER_PREFIX)
    assert "围棋" not in n1 and "\\" not in n1, f"名字里不该有原文路径：{n1}"
    # 同机两份 checkout 各开一份（开发常态）：目录不同就必须是不同的名字
    monkeypatch.setattr(paths, "APP_DIR", Path("D:/other/checkout/desktop"))
    assert si.default_name() != n1
    assert si.sanitize("") == si.SERVER_PREFIX
    cleaned = si.sanitize("中文 名字/a.b-c")
    assert all(c.isalnum() or c in "._-" for c in cleaned), cleaned


# ---------------------------------------------------------------- 崩溃日志

@pytest.fixture
def crash_file(tmp_path):
    """把崩溃日志指到临时目录，结束后把三个钩子还回去。

    不传 `log_dir` 就是往开发者真实的 `desktop/logs/` 里塞假崩溃。
    """
    d = tmp_path / "logs"
    path = crash_log.install(log_dir=d)
    yield path
    crash_log.uninstall()


def raise_here(marker: str) -> None:
    """抛一次，把 (类型, 值, 栈) 交给 Qt 用的那个入口。"""
    try:
        raise RuntimeError(marker)
    except RuntimeError as exc:
        return type(exc), exc, exc.__traceback__


def other_raise_site(marker: str):
    try:
        raise RuntimeError(marker)
    except RuntimeError as exc:
        return type(exc), exc, exc.__traceback__


def test_an_exception_from_a_qt_slot_lands_in_the_log(make_shell, qapp, crash_file):
    """真的从 `QTimer` 槽里抛（而不是直接调 `write()`）。

    因为要验的就是「Qt 回调 Python 时异常走哪」这一条：实测 6.11.2 它会调
    `sys.excepthook` 然后继续跑，窗口不没、什么也不说 —— 没这个文件就是死无对证。
    """
    from PySide6.QtCore import QTimer

    sh = make_shell()
    keep: list = []

    def raiser():
        raise RuntimeError("槽里炸一次")

    keep.append(raiser)
    boom = QTimer()
    boom.setInterval(10)
    boom.timeout.connect(raiser)
    boom.start()
    ok = drain(qapp, lambda: crash_file.exists() and "槽里炸一次" in
               crash_file.read_text(encoding="utf-8"), 10.0)
    boom.stop()
    assert ok, f"槽里的异常没落盘：{crash_file}"
    assert sh.isVisible(), "一处炸了不该把整个窗口带走"
    assert "内部异常" in sh.status.text(), "落盘了但当场没人说话（用户只看到按钮没反应）"


def test_the_original_stderr_hook_still_runs(tmp_path):
    """口径 1：异常不许被吞掉 —— 写完日志照旧交给上一个钩子。"""
    calls: list = []
    real = sys.excepthook
    sys.excepthook = lambda *a: calls.append(a)
    try:
        crash_log.install(log_dir=tmp_path / "logs")
        crash_log._hook_excepthook(*raise_here("仍旧要上 stderr"))
        crash_log.uninstall()
        assert len(calls) == 1, f"钩子链断了：{calls}"
        assert sys.excepthook is not crash_log._hook_excepthook, "uninstall 没还回去"
        assert "仍旧要上 stderr" in crash_file_tail(tmp_path)
    finally:
        sys.excepthook = real


def crash_file_tail(tmp_path) -> str:
    return (tmp_path / "logs" / crash_log.LOG_NAME).read_text(encoding="utf-8")


def test_repeats_of_the_same_crash_are_merged_once(crash_file):
    """口径 2：必须限流。实测 10ms 定时器里的同一个异常 200ms 就打了 23 条。"""
    args = raise_here("同一个缺陷")
    for _ in range(30):
        crash_log._hook_excepthook(*args)
    text = crash_file.read_text(encoding="utf-8")
    assert crash_log.recorded == 1
    assert text.count("同一个缺陷") == 1
    crash_log.flush_repeats()
    tail = crash_file.read_text(encoding="utf-8")
    assert "29 次" in tail, "崩得多频繁这个最要紧的信息不能丢"


def test_two_different_raise_sites_are_two_entries(crash_file):
    """只用类型+消息会把手十个不同按钮的同一个 except 分支并成一条，所认末帧。"""
    crash_log._hook_excepthook(*raise_here("甲处"))
    crash_log._hook_excepthook(*other_raise_site("乙处"))
    assert crash_log.recorded == 2
    text = crash_file.read_text(encoding="utf-8")
    assert "甲处" in text and "乙处" in text


def test_a_thread_crash_is_logged_with_its_thread_name(crash_file):
    """后端线程、KataGo 看门狗、安装器都在子线程里 —— 那才是真需要事后看的。"""
    def boom():
        raise ValueError("子线程里的炸")

    t = threading.Thread(target=boom, name="katago-watchdog")
    t.start()
    t.join()
    text = crash_file.read_text(encoding="utf-8")
    assert "子线程里的炸" in text, text[-400:]
    assert "katago-watchdog" in text, "没写是哪个线程，日志就只能看一半"


def test_the_log_rotates_instead_of_growing_forever(crash_file, monkeypatch):
    monkeypatch.setattr(crash_log, "ROTATE_AT", 500)
    monkeypatch.setattr(crash_log, "KEEP_TAIL", 200)
    for i in range(6):
        crash_log.write(f"第 {i} 行，后面垫一段足够长的内容以便真的碰到上限。" * 3)
    # 滚动是从字节中间切开的，尾部可能剩半个 UTF-8 字符：读的时候必须宽容
    text = crash_file.read_bytes().decode("utf-8", "replace")
    assert text.startswith("[滚动]"), "到上限了却没滚 → 几分钟就能写爆磁盘"
    assert len(text) < 1200, f"滚完还是这么长：{len(text)}"


def test_writing_the_log_never_raises(tmp_path, monkeypatch):
    """口径 3：日志目录只读、盘满、正在关机 —— 任何一种都不能从 excepthook 里抛。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("这是一个文件，不是目录", encoding="utf-8")
    monkeypatch.setattr(crash_log, "_path", blocker / "client.log")
    crash_log.write("写不下去的一行")            # 不抛就算过
    crash_log._hook_excepthook(*raise_here("也写不下去"))
    assert crash_log.recorded >= 1


def test_the_log_can_be_pointed_at_a_new_dir_after_the_window_exists(make_shell,
                                                                    tmp_path):
    """两种顺序都得通知到窗口。

    真启动是「先装日志后开窗」（`app.main()`），验收脚本常常反过来；
    `install()` 不传 `on_new` 时把已有回调清掉，后面那一种就会「落盘了却没人说话」。
    """
    sh = make_shell()
    crash_log.install(log_dir=tmp_path / "later")
    try:
        crash_log._hook_excepthook(*raise_here("顺序反了也要说"))
        assert "内部异常" in sh.status.text(), sh.status.text()
        assert "顺序反了也要说" in (tmp_path / "later" / "client.log").read_text(
            encoding="utf-8")
    finally:
        crash_log.uninstall()


# ---------------------------------------------------------------- 窗口几何

#: 「恢复后的尺寸」不许按像素相等断言：实测两档平台各有一个不能预测的偏移 ——
#: offscreen 那块 800x800 虚拟屏会把窗口**夹**小（1240x780 回来是 1180x774），
#: 而真 `windows` 平台会按窗口**框**把它报大（存 760 高、恢复出来 770）。
#: 所以这一组只问「有没有默默回到默认那一档」：存档一律用 `minimumSize`
#: （小屏上夹不下去、大屏上不缩，两边都贴着实存值），而默认尺寸 1280x800
#: 与它差 100x40 —— 容差 24 既容得下框/取整，又容不下“完全没恢复”。
GEOMETRY_TOL = 24


def assert_restored_size(shell, want: tuple) -> None:
    """带口径的断言：先自验「默认档与存档确实差得够多」，否则下面两条会退化为永真。"""
    d = shell_mod.DEFAULT_SIZE
    assert min(abs(d[0] - want[0]), abs(d[1] - want[1])) > GEOMETRY_TOL, \
        f"存进去的尺寸 {want} 离默认档 {d} 太近，这条测试区分不了两者"
    got = (shell.width(), shell.height())
    assert all(abs(g - w) <= GEOMETRY_TOL for g, w in zip(got, want)), \
        f"恢复出来是 {got}，存进去的是 {want}（差超 {GEOMETRY_TOL}px 就是没拿到存值）"


def test_geometry_round_trips_through_the_ini(make_shell, host, qapp, tmp_path):
    """存一次、重启恢复一次。量到的硬事实：QSettings 的 INI 把 QByteArray 存成
    `geometry=@ByteArray(\\x1\\xd9...)` 这种**带类型前缀的文本**，但读回来仍是
    `QByteArray`（上一版记的「读回来是 str」是猜的，实测推翻了它）。

    两边都得防：文件里必须是可读文本（拿二进制当配置存，下一个工具就会把它坏掉），
    而代码不许假定只有一种类型 —— 错一种就是「窗口默默回到默认尺寸」。
    """
    ini = tmp_path / "client.ini"
    sh = make_shell()
    # 存**最小尺寸**而不是随手一个 1240x780：`restoreGeometry` 会自己把窗口夹进
    # 可用屏幕（实测 offscreen 那块虚拟屏只有 800x800，1240x780 存进去、恢复出来
    # 是 1180x774），于是「恢复后的精确尺寸」这个期望值其实是**开发者显示器
    # 的尺寸**。而等于 `minimumSize` 的那一档两边都对：小屏上夹不下去（下限就
    # 是它），大屏上根本不夹。它又与 `DEFAULT_SIZE`(1280x800) 不同，所以
    # 「默默回到默认」这个被守住的场景仍然会红。
    want = (sh.minimumWidth(), sh.minimumHeight())
    sh.resize(*want)
    sh.move(120, 90)
    H._process()
    assert sh.close() is True
    raw = Prefs(str(ini)).window_geometry()
    assert raw and len(raw) > 8, f"几何没落盘：{raw!r}"
    line = [ln for ln in ini.read_text(encoding="utf-8", errors="replace")
            .splitlines() if ln.startswith("geometry=")]
    assert len(line) == 1, line
    assert all(32 <= ord(ch) < 127 for ch in line[0]), \
        f"几何不是可读文本，而是直接丢了二进制进 ini：{line[0][:40]!r}"

    again = shell_mod.Shell(host, Prefs(str(ini)))
    try:
        assert again.geometry_source == "restored", "存了却没恢复（静默回到默认尺寸）"
        assert_restored_size(again, want)
    finally:
        again.close()
        again.deleteLater()


def test_a_base64_text_geometry_is_decoded_before_restore(host, tmp_path):
    """上一量发现读回来是 QByteArray，但 `str` 这一支不能因为「本机量不到」就不测：
    换平台（注册表后端）或手改过的 ini 都会给文本，而窗口代码里的
    `QByteArray.fromBase64` 就是为它设的。直接把 base64 文本喂进那条恢复路径。
    """
    sh = shell_mod.Shell(host, Prefs(str(tmp_path / "client.ini")))
    try:
        want = (sh.minimumWidth(), sh.minimumHeight())   # 不被屏幕夹，见上面那条
        sh.resize(*want)
        blob = bytes(sh.saveGeometry().toBase64().data())
    finally:
        sh.close()
        sh.deleteLater()

    class TextPrefs(Prefs):
        def window_geometry(self):
            return blob.decode("ascii")          # 就是 INI 里那种 base64 文本

    again = shell_mod.Shell(host, TextPrefs(str(tmp_path / "client.ini")))
    try:
        assert again.geometry_source == "restored", "base64 文本没被解码就丢了"
        assert_restored_size(again, want)
    finally:
        again.close()
        again.deleteLater()


def test_a_geometry_from_a_disconnected_screen_falls_back(make_shell, host, qapp,
                                                          tmp_path):
    """多屏拔掉副屏后，保存的几何可能整个在屏幕外 —— 症状是「双击图标没反应」。

    这一条**不能靠真把窗口挪出屏幕来触发**：实测 `restoreGeometry` 自己就会把它
    夹回屏内（move(4000,4000) 存下再恢复，`geometry_source` 仍是 `restored`），
    于是那条守卫永远不会红 —— 挡着的东西必须能被测到，所以喂一个假的屏幕列表
    （见 `Shell._screen_rects` 的注释）。
    """
    ini = str(tmp_path / "client.ini")
    sh = make_shell()
    sh.move(1400, 100)
    H._process()
    sh._save_geometry()
    assert sh.close() is True

    class OneFarScreen(shell_mod.Shell):
        def _screen_rects(self):
            # 坐标给到 9 万像素：真恢复出来的窗口永远不可能与它相交（而拿一块
            # 「小到装不下」的屏当假屏幕是不行的：`restoreGeometry` 已经把窗口
            # 夹回屏内，那一小块反而与它相交，守卫就测不到了）。
            return [QRect(99999, 99999, 200, 200)]

    again = OneFarScreen(host, Prefs(ini))
    try:
        assert again.geometry_source == "offscreen", "拿着一个屏幕外的几何继续跑"
        assert again.geometry().topLeft() == QPoint(80, 60), again.geometry()
        # 「默认档」现在还要夹进可用屏幕（审计 M11）——离屏回退给的也是夹过的那一档。
        assert (again.width(), again.height()) == \
            again._fit_size(*shell_mod.DEFAULT_SIZE)
    finally:
        again.close()
        again.deleteLater()


def test_a_restored_window_lands_on_a_screen_that_still_exists(make_shell, host, qapp,
                                                               tmp_path):
    """与上面一对：用**真屏幕**时，恢复完的窗口必须与某块屏相交。

    不注入、不假 —— 这一条守的是“正常路径别被上面那道守卫误伤”（否则
    所有人的窗口都会永远回到同一个左上角，而测试全绿）。
    """
    ini = str(tmp_path / "client.ini")
    sh = make_shell()
    sh.move(1400, 100)
    H._process()
    sh._save_geometry()
    assert sh.close() is True
    again = shell_mod.Shell(host, Prefs(ini))
    try:
        assert again.geometry_source == "restored", again.geometry_source
        rect = again.geometry()
        screens = again._screen_rects()
        assert screens and any(r.intersects(rect) for r in screens), \
            f"恢复到了屏幕外：{rect} vs {screens}"
    finally:
        again.close()
        again.deleteLater()


def test_garbage_geometry_becomes_the_default_and_does_not_raise(make_shell, host,
                                                                qapp, tmp_path):
    """一个看起来像几何、其实不是的值：`restoreGeometry` 只会静默返 False。"""
    ini = str(tmp_path / "client.ini")
    Prefs(ini).save_window("bm90LWEtZ2VvbWV0cnk=", "")      # 合法 base64、不是几何
    again = shell_mod.Shell(host, Prefs(ini))
    try:
        assert again.geometry_source == "default"
        # 同上一处：默认档先过 `_fit_size`（审计 M11，把窗口夹进可用屏幕）。
        assert (again.width(), again.height()) == \
            again._fit_size(*shell_mod.DEFAULT_SIZE), again.size()
    finally:
        again.close()
        again.deleteLater()


def test_a_qbytearray_geometry_is_used_as_is(make_shell, host, qapp, tmp_path):
    """上面两条走的是“从 ini 读回来”；这里直接交 `saveGeometry()` 那个对象。

    实测 INI 读回来也是 QByteArray，所以这一支才是本机用户的真实路径；
    而 `isinstance(raw, QByteArray)` 这个判断不能被删（删了就会拿着一个 QByteArray
    去 `fromBase64(str(...))`，几何就此恢复不回来）。
    """
    sh = make_shell()
    want = (sh.minimumWidth(), sh.minimumHeight())     # 不被屏幕夹，见上面那条
    sh.resize(*want)
    H._process()
    blob = sh.saveGeometry()
    assert isinstance(blob, QByteArray), type(blob)
    sh.close()

    class RawPrefs(Prefs):
        def window_geometry(self):            # 直接给 QByteArray，跳过 INI 那一圈
            return blob

    again = shell_mod.Shell(host, RawPrefs(str(tmp_path / "client.ini")))
    try:
        assert again.geometry_source == "restored"
        assert_restored_size(again, want)
    finally:
        again.close()
        again.deleteLater()


def test_no_saved_geometry_keeps_the_default_size(make_shell, qapp):
    sh = make_shell(login=False)
    assert sh.geometry_source == "default"
    # 不写死 1180x760：最小尺寸按屏幕算（审计 M11），小屏上的设计值会被压低。
    # 不变的是"默认尺寸不小于自己的最小值"这条不变量。
    assert (sh.width(), sh.height()) >= (sh.minimumWidth(), sh.minimumHeight()), \
        "默认尺寸比自己的最小值还小"


# ---------------------------------------------------------------- 帮助与目录

def test_the_logs_action_points_at_the_logs_folder(make_shell, qapp, monkeypatch,
                                                   tmp_path):
    asked: list = []
    monkeypatch.setattr(chrome_mod, "open_local", lambda t: asked.append(str(t)) or True)
    logs = tmp_path / "本机日志目录"            # 不指到临时目录就会在仓库里建一个 logs/
    monkeypatch.setattr(paths, "LOGS_DIR", logs)
    sh = make_shell()
    sh.chrome.actions["open_logs"].trigger()
    assert asked == [str(logs)], asked
    assert logs.is_dir(), "没先 mkdir 的话资源管理器会报「路径不存在」"


def test_the_shortcut_dialog_shows_the_same_table(make_shell, qapp, monkeypatch):
    FakeBox.shown = []
    monkeypatch.setattr(chrome_mod, "QMessageBox", FakeBox)
    sh = make_shell()
    sh.chrome.actions["keys"].trigger()
    assert len(FakeBox.shown) == 1, "F1 那条菜单没把对话框接上"
    title, body = FakeBox.shown[0]
    assert title == "键盘快捷键"
    assert body.count("<tr>") == len(sh.chrome.shortcut_rows())
    assert "Ctrl+S" in body and "Alt+1" in body


def test_the_about_text_is_player_facing(make_shell, qapp):
    """「关于」框是给学员看的：品牌与用途，不是 Qt/Python 版本与部署地址。

    第 33 轮起不再报告：`Qt x · Python x`、数据目录、本地服务地址 ——
    那些是诊断信息；学员点「关于」想知道「这是什么」。（日志路径保留：
    出问题时它是唯一能让支持找到现场证据的线索。）
    """
    sh = make_shell()
    text = sh.chrome.about_text()
    assert chrome_mod.APP_TITLE in text
    assert "Qt" not in text and "Python" not in text
    assert "://" not in text, f"「关于」里不该有服务地址：{text}"
    assert crash_log.log_path().name in text


# ---------------------------------------------------------------- 图标

def test_the_app_icon_draws_a_board_at_every_size():
    """图标是画的，所以必须能量：一个全色的圆片会被读成「一个棋罐」而不是一盘棋。"""
    ic = app_icon.icon()
    assert not ic.isNull()
    sizes = {s.width() for s in ic.availableSizes()}
    assert {16, 32, 48, 256} <= sizes, sizes
    img = app_icon.pixmap(64).toImage()
    dark = light = wood = empty = 0
    for y in range(0, 64, 2):
        for x in range(0, 64, 2):
            c = img.pixelColor(x, y)
            if c.alpha() < 200:
                empty += 1
                continue
            # 黑白子按**中性色**认，不能按“够不够白”：实测白子 `#d4d4d4`
            # 的 luma 只到 212（阈值 230 就永远数不到子，一条假红）；
            # 而木头是暖色（R>B），拿“三色相等”刚好能把两者分开。
            neutral = abs(c.red() - c.green()) <= 6 and abs(c.green() - c.blue()) <= 6
            if neutral and H.luma(c) < 70:
                dark += 1
            elif neutral and H.luma(c) > 150:
                light += 1
            elif H.is_wood(c):
                wood += 1
    assert dark >= 3 and light >= 3, f"一黑一白两枚子没画出来：{dark}/{light}"
    assert wood >= 20, f"木盘底色太少：{wood}"
    assert empty > 0, "圆角之外的部分应当是透明的"


# ---------------------------------------------------------------- 关键帧

def test_screenshot_of_the_settings_page_with_the_app_panel(make_shell, qapp):
    """设置页新增的「应用」卡片 + 半透明用户名只能看图，顺手钉住不裁字。

    这一屏是菜单栏撤掉之后「快捷键 / 关于 / 退出」唯一的可见入口，
    所以它值得一张自己的关键帧。第 33 轮起设置页内容收在滚动区里，
    「应用」卡片在内容最底部 —— 截图前先滚到底，否则拍的是内容顶部。
    """
    sh = make_shell()
    sh.go("settings")
    page = sh.pages["settings"]
    # 先让真实那一次 `/api/auth/me` 回包落地，再灌一个显示名 —— 顺序反了会被回包覆盖
    # （首跑就红在这里：标签里是 fixture 注册的随机用户名）。
    H.settle(qapp, 0.2)
    page._user = {"displayName": "临安不安"}
    page._paint_user()
    bar = page.scrollArea.verticalScrollBar()
    bar.setValue(bar.maximum())
    H.settle(qapp, 0.1)
    path = H.snap(page, "p6_01_settings_app_panel")
    assert path.exists() and path.stat().st_size > 0

    offenders, scanned = H.clipped_texts(page)
    assert scanned >= 20, f"只扫到 {scanned} 个控件 —— 页面没建起来，这条就是假绿"
    assert offenders == [], f"这些控件的文字被裁了：{offenders}"
    assert page.appButtons["quit"].isVisible(), "「应用」卡片没画出来"
    assert "临安不安" in page.lblUser.text()


def test_the_app_panel_buttons_are_wired_to_the_trigger_method(make_shell, qapp,
                                                               monkeypatch):
    """五个按钮各自要走到 `_trigger_app(键)`。

    不点真动作：退出会关窗、关于/快捷键会弹模态框，点下去测试就挂住了 ——
    「按钮 → 方法」这一段接线与「方法 → 动作」那一段分开测（见下一条）。
    """
    sh = make_shell()
    sh.go("settings")
    page = sh.pages["settings"]
    assert set(page.appButtons) == set(chrome_mod.SETTINGS_KEYS)

    seen: list[str] = []
    monkeypatch.setattr(page, "_trigger_app", lambda key: seen.append(key))
    for key in chrome_mod.SETTINGS_KEYS:
        page.appButtons[key].click()
    assert seen == list(chrome_mod.SETTINGS_KEYS)


def test_trigger_app_fires_the_real_action_not_a_copy(make_shell, qapp):
    """`_trigger_app` 触发的是窗口上那个动作本体（菜单栏撤了，实现仍然只有一份）。"""
    sh = make_shell()
    sh.go("settings")
    page = sh.pages["settings"]
    hits: list[str] = []

    class _FakeAction:
        def __init__(self, key):
            self.key = key

        def trigger(self):
            hits.append(self.key)

    class _FakeChrome:
        def __init__(self):
            self.actions = {k: _FakeAction(k) for k in chrome_mod.SETTINGS_KEYS}

    page._chrome = _FakeChrome()
    for key in chrome_mod.SETTINGS_KEYS:
        page._trigger_app(key)
    assert hits == list(chrome_mod.SETTINGS_KEYS)

    # chrome 没注入时不能静默：按钮点下去得说一句，而不是什么都不发生
    page._chrome = None
    page._trigger_app("about")
    assert "chrome 未注入" in page.errorBar.label.text(), page.errorBar.label.text()


def test_the_window_title_is_the_app_name_and_the_user_sits_in_settings(make_shell, qapp):
    """标题只报应用名；登录名在设置页右上角（半透明），两处都不许串。"""
    sh = make_shell()
    assert sh.windowTitle() == chrome_mod.APP_TITLE
    sh.go("settings")
    page = sh.pages["settings"]
    page._user = {"username": "demo", "displayName": "演示用户"}
    page._paint_user()
    assert page.lblUser.text() == "当前登录：演示用户"
    assert "演示用户" not in sh.windowTitle(), "用户名又跑回标题里了"
    assert "rgba(" in page.lblUser.styleSheet(), page.lblUser.styleSheet()
    assert page.lblUser.graphicsEffect() is None, \
        "别再用 QGraphicsOpacityEffect：它让父控件走离屏渲染，会留残影"


def test_the_user_tag_is_drawn_translucent(make_shell, qapp):
    """半透明得在**像素**上成立：最深的一笔要比不透明的 MUTED 浅一截。

    只断样式串里有 `rgba(` 不够 —— Qt 认不出的写法会静默忽略整条规则（本项目
    在 QSS 上吃过两次同样的亏：`text:` 与 `hasWordWrap`）。不透明的 MUTED 在
    这个底色上的亮度约 141，55% 透明约 187。
    """
    sh = make_shell()
    sh.go("settings")
    page = sh.pages["settings"]
    H.settle(qapp, 0.2)
    page._user = {"displayName": "临安不安"}
    page._paint_user()
    H.settle(qapp, 0.1)

    img = H.grab_image(page)
    dpr = page.devicePixelRatioF()
    origin = page.lblUser.mapTo(page, QPoint(0, 0))
    darkest = 255
    for y in range(origin.y(), origin.y() + page.lblUser.height()):
        for x in range(origin.x(), origin.x() + page.lblUser.width()):
            px = img.pixelColor(int(x * dpr), int(y * dpr))
            darkest = min(darkest, H.luma(px))
    assert darkest < 230, f"标签里一个字都没画出来（最深 {darkest}）"
    assert darkest > 165, f"登录名不是半透明（最深 {darkest}；不透明 MUTED 约 141）"


def test_no_wrapped_label_is_squeezed_below_its_text(make_shell, qapp):
    """折行标签不许被压得比自己的文字还矮 —— 那正是用户报的「字符重叠」。

    `QLabel(wordWrap=True)` 的 `minimumSizeHint()` 只按一行算，布局一挤就把第二行
    画到第一行上面（实测 h=25 而 `heightForWidth` 要 34）。`WrapLabel` 把最小高度
    改成按宽度算出来的真实高度；这条断言是它的守门人。
    """
    sh = make_shell()
    sh.go("settings")
    page = sh.pages["settings"]
    H.settle(qapp, 0.2)
    checked = 0
    for lab in page.findChildren(WrapLabel):
        if not lab.text() or not lab.isVisible():
            continue
        need = lab.heightForWidth(lab.width())
        if need <= 0:
            continue
        assert lab.height() >= need, \
            f"折行标签被压扁：h={lab.height()} < 需要 {need}（{lab.text()[:24]!r}）"
        checked += 1
    assert checked >= 4, f"只量到 {checked} 个折行标签，这条是假绿"

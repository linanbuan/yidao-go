"""启动链真窗口验收：从**生产入口** `app.main()` 真起一趟窗口。

    python desktop/scripts/smoke_launch.py

和 pytest 那批的分工：那些测控件与契约，用的是夹具里的 QApplication；
这一支走的是真实启动链 —— HiDPI 设置 → splash → 后端 daemon 线程 → Shell →
`boot()` 免登录 → QSettings 落盘，一个都不绕过。它红了就等于双击打不开，
而测试可能还是全绿（夹具会自己起后端、自己建 Shell）。

跑四趟，共用同一份临时 ini：
  1) 无 token  → 真点「注册新账号」→ 进大厅；
  2) 复用同一份 ini → `boot()` 直接免登录进大厅。
     第二趟才有意义：它证明 token 真的 round-trip 过磁盘，不只是内存里传了个对象。
  3) 再复用 → **大厅里真开一局**，在对局页真点一手、等 AI 回一手、真认输到结算。
     这一趟的存在理由：前两支 E2E 跑在 offscreen 夹具里（DPR 1.0），
     而**对局页从没在真窗口 + 真 DPR 下被人看过**（第 18 轮之前的旧欠账）。
     它不重做 E2E 的断言，只证「真窗口下能开局能落子能收尾」，并留下关键帧。
  4) **启动器这条玩家入口**（第 34 轮加）：真窗口起 `launcher/gui.py`，真注册/真登录，
     点「进入弈道」把**真客户端子进程**拉起来，并断言客户端自己的日志里出现
     「主窗口已出现」+ 令牌在共享 ini 里。前三趟走的都是 `app.main()`（客户端入口），
     只有这一趟走的是玩家双击 bat 的那条链。

数据目录与 ini 都在临时目录：不碰 `backend/data` 里你下过的棋，也不碰 AppData。
结论会另外写一份 `desktop/artifacts/smoke_latest.txt`（UTF-8，程序自己写）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent.parent
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

PASSWORD = "yanshou123"

# 验收结论里有中文，而 PowerShell 的管道会二次解码把中文搞坏（本项目踩过）。
# 所以除了 stdout，再让程序**自己**往 UTF-8 文件里写一份，核对时读那份。
REPORT = Path(os.environ["GO_SMOKE_REPORT"]) if os.environ.get("GO_SMOKE_REPORT") else None

#: 入口模块（`desktop/app.py` 以 `desktop_entry` 之名加载）。`run_pass()` 装进来，
#: `_observe()` 读它 —— 标题断言要用 `shell_mod.chrome_mod.APP_TITLE`。
#: 放在模块级（而不是让 `run_pass` 用局部名）是因为 `_observe` 是另一个函数：
#: 第 23 轮加标题断言时漏了这一步，验收脚本从那时起每次都自己抛 NameError（见 §5-95）。
app_mod = None

#: 启动器模块（第 34 轮第 4 趟用）。同样放模块级：驱动函数不止一个。
launcher_mod = None


def say(msg: str = "") -> None:
    print(msg)
    if REPORT is not None:
        with REPORT.open("a", encoding="utf-8") as fh:
            fh.write(msg + "\n")


def _wait(app, predicate, timeout=30.0) -> bool:
    """转事件循环直到条件成立。跨线程投递不是在 processEvents 的一瞬到达的。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _capture(widget, name: str) -> Path:
    from PySide6.QtWidgets import QApplication

    from core import paths
    d = paths.ARTIFACTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    widget.raise_()
    app = QApplication.instance()
    for _ in range(3):
        app.processEvents()
    img = widget.grab().toImage()
    out = d / f"{name}.png"
    img.save(str(out))
    say(f"    截图 {out.name}  {img.width()}x{img.height()}px")
    return out


class Acceptance:
    """一趟验收：挂进 `app.main()` 的 on_shell_ready，驱动窗口，收集结论。"""

    def __init__(self, autologin: bool, tag: str, gate=None, game: bool = False):
        self.autologin = autologin
        self.tag = tag
        #: 第三趟才走对局页；前两趟不顺便下一手，否则它们的失败会被这一步的噪声掩盖
        self.game = game
        self.problems: list[str] = []
        self.facts: list[str] = []
        self.shell = None
        #: 只在窗口还开着的时候读它：`main()` 返回时 aboutToQuit 已经把锁放了，
        #: 那时候再读 held 就必定是 False，报出来的数没有意义。
        self.gate = gate
        self._done = False
        from PySide6.QtCore import QTimer
        self._timer = QTimer

    def on_shell(self, shell):
        """在 `Launcher._on_ready` 里被调用（主线程）。回调用绑定方法，不写 lambda。"""
        self.shell = shell
        self._timer.singleShot(300, self._drive)
        # 硬超时兜底：哪一步卡住都不该让进程永远挂着（上一版真卡过 300 秒）
        self._timer.singleShot(200_000 if self.game else 90_000, self._timeout_and_quit)

    def _timeout_and_quit(self):
        if not self._done:
            # 第三趟要开局、等 AI、走结算，90 秒不够；它也不该把前两趟的结论拖住
            budget = 200 if self.game else 90
            self.problems.append(f"{budget} 秒没走完验收，强制退出")
            self._finish()

    # ------------------------------------------------------------

    def _drive(self):
        """跑一遍观察。

        外层必须接住异常：上一版里 `page.games.item(0)` 在列表还没回填时是 None，
        AttributeError 从定时器回调逃出去之后 Qt 只打日志不退出，进程就永远卡在
        事件循环里 —— 验收脚本自己出错，不该表现为"客户端挂死"。
        """
        try:
            self._observe()
        except Exception as exc:  # noqa: BLE001
            import traceback
            self.problems.append(f"验收脚本自己抛了：{exc!r}")
            self.facts.append(traceback.format_exc(limit=4))
            self._finish()

    def _observe(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        sh = self.shell
        self.facts.append(f"窗口可见={sh.isVisible()} 尺寸={sh.width()}x{sh.height()} "
                          f"DPR={sh.devicePixelRatioF()}")
        if not sh.isVisible():
            self.problems.append("窗口没真的显示出来")

        if sh._stack.currentIndex() == 0:
            if self.autologin:
                self.problems.append("第二趟本该免登录，却停在登录页 —— token 没 round-trip")
            else:
                # 先拍登录页本尊再去点注册：上一版这张图是在登录之后才拍的，
                # 名叫 _login 却画着大厅 —— 真窗口 + 真实 DPR 下的登录页反而没人看过。
                _capture(sh, "page_04_app_login")
                QTest.keyClicks(sh._login.user, "p1" + uuid.uuid4().hex[:8])
                QTest.keyClicks(sh._login.pw, PASSWORD)
                QTest.mouseClick(sh._login.btnRegister, Qt.MouseButton.LeftButton)

        if not _wait(app, lambda: sh._stack.currentIndex() == 1, 40.0):
            self.problems.append(f"没进主界面，页面上的错误是：{sh._login.error.text()!r}")
            return self._finish(app)

        page = sh.current_page()
        if type(page).__name__ != "LobbyPage":
            self.problems.append(f"登录后落到了 {type(page).__name__}，不是大厅")
            return self._finish(app)
        if not _wait(app, lambda: page.badge.text() != "—", 20.0):
            self.problems.append(f"大厅统计没回填，提示条：{page.notice.text()!r}")
        self.facts.append(f"标题={sh.windowTitle()!r} 徽章={page.badge.text()!r} "
                          f"战绩={page.statCards['record'].text()!r} "
                          f"引擎={page.engineBadge.text()!r}")
        if app_mod.shell_mod.chrome_mod.APP_TITLE not in sh.windowTitle():
            self.problems.append(f"标题不对：{sh.windowTitle()!r}")
        # 对局列表是**另一个** GET，比 auth/me 慢到是正常的；等不到也不当错——
        # 新账号就是没对局，占位文案与空列表两种都要能过。
        _wait(app, lambda: page.games.count() > 0, 8.0)
        first = page.games.item(0)
        self.facts.append(f"对局列表 {page.games.count()} 行："
                          f"{first.text() if first else '（一行没有）'!r}")
        if first is None:
            self.problems.append("对局列表一行都没画：占位文案也没写进去")

        vp = sh.content_size()
        hint = page.sizeHint()
        self.facts.append(f"内容视口 {vp.width()}x{vp.height()}，大厅 sizeHint "
                          f"{hint.width()}x{hint.height()}")
        if self.gate is not None:
            self.facts.append(f"单实例管道 {self.gate.name} held={self.gate.held} "
                              f"窗口几何来源={sh.geometry_source}")
        if not sh.chrome.actions:                # 菜单一项都没建起来 = P5 那条链没接上
            self.problems.append("菜单栏里一个动作都没有")
        if hint.width() > vp.width() or hint.height() > vp.height():
            self.problems.append("大厅在真实窗口里放不下，会出现滚动条")

        # 状态栏：已经进了主界面还挂着“请先登录”就是假信息（上一版真挂着，靠看截图才抓到）
        self.facts.append(f"状态栏={sh.status.text()!r}")
        if "登录" in sh.status.text():
            self.problems.append(f"已进主界面，状态栏还写着：{sh.status.text()!r}")

        # 四张统计卡：等宽 + 铺满整行（曾有一张卡被摆进布局两次，整行是散的）
        row = page.layout().itemAt(1).layout()
        items = [row.itemAt(i).widget() for i in range(row.count())]
        cards = [c for c in items if c is not None]
        self.facts.append(f"统计卡行 {row.count()} 项，宽度={[c.width() for c in cards]}，"
                          f"右边界={cards[-1].geometry().right() if cards else '—'} 页宽={page.width()}")
        if row.count() != 4 or len(cards) != 4:
            self.problems.append(f"统计卡那行有 {row.count()} 个项，应当正好是 4 张卡")
        elif max(c.width() for c in cards) - min(c.width() for c in cards) > 2 \
                or cards[-1].geometry().right() < page.width() - 20:
            self.problems.append(f"四张卡没等宽或没铺满：{[c.width() for c in cards]}")

        _capture(sh, "page_05_app_lobby")
        if self.game:                                   # 只有第三趟进这里
            return self._play_one_game(app, sh)
        self._finish(app)

    # ------------------------------------------------- 第三趟：对局页真下一手

    def _play_one_game(self, app, sh) -> None:
        """大厅真开一局 → 对局页真点一手 → 等 AI 回一手 → 真认输到结算。

        这一趟**不写 assert**：它是验收脚本，抛出来只会被 `_drive` 的 except 接住
        变成「脚本自己抛了」，看不出是界面的问题。每一步都往 problems 里写人话。
        与 E2E 的分工：E2E 钉行为细节（这一支不重复），这一支只证**真窗口下能用**。
        """
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        from ui.pages import game as game_mod

        lb = sh.current_page()
        lb.cbSize.setCurrentIndex(lb.cbSize.findData(9))
        lb.cbTime.setCurrentIndex(lb.cbTime.findData(30))
        if lb.cbSize.currentData() != 9 or lb.cbTime.currentData() != 30:
            self.problems.append(f"开局表单没能选到 9 路 / 30 秒："
                                 f"{lb.cbSize.currentData()!r} {lb.cbTime.currentData()!r}")
        QTest.mouseClick(lb.btnStart, Qt.LeftButton)
        if not _wait(app, lambda: isinstance(sh.current_page(), game_mod.GamePage), 40.0):
            self.problems.append(f"点了「对阵 AI」没切到对局页，大厅提示：{lb.notice.text()!r}")
            return self._finish(app)
        g = sh.current_page()
        if not _wait(app, lambda: g.status == "open" and g.phase == "playing" and g.size == 9, 60.0):
            self.problems.append(f"对局页首帧没落进来：status={g.status} "
                                 f"phase={g.phase} size={g.size} error={g.error!r}")
            return self._finish(app)
        self.facts.append(f"对局页已连上：连接标签={g.connBadge.text()!r} "
                          f"我执={'黑' if g.player_color == game_mod.BLACK else '白'} 限时={g.move_seconds}s")
        _capture(sh, "smoke_game_open")

        _wait(app, lambda: g.is_my_turn, 60.0)      # 抽到白子时 AI 先手，等一手是正常的
        before, played = len(g.moves), None
        for x, y in ((2, 2), (6, 6), (2, 6), (6, 2), (4, 4)):
            if g.board[y][x]:
                continue
            pos = g.boardView.center(x, y).toPoint()
            QTest.mouseClick(g.boardView, Qt.LeftButton, Qt.NoModifier, pos)
            if _wait(app, lambda: len(g.moves) >= before + 2, 90.0):
                played = (x, y)
                break
            if g.error:
                self.facts.append(f"({x},{y}) 被服务端拒：{g.error!r}，换下一个候选")
                g.clear_error()
        if played is None:
            self.problems.append(f"在对局页上落不下一手：phase={g.phase} "
                                 f"nextColor={g.next_color} error={g.error!r}")
            return self._finish(app)
        self.facts.append(f"真点了一手 {played}，盘上手顺 {len(g.moves)} 手（含 AI 那一手）；"
                          f"思考态={g.thinking} 连接标签={g.connBadge.text()!r}")
        _capture(sh, "smoke_game_mid")

        QTest.mouseClick(g.btnResign, Qt.LeftButton)
        if not g.btnResignOk.isVisible():
            self.problems.append("点认输没出二次确认（或者直接结了两步）")
        else:
            QTest.mouseClick(g.btnResignOk, Qt.LeftButton)
        if not _wait(app, lambda: g.game_end is not None and g.phase == "finished", 40.0):
            self.problems.append(f"认输后没到结算：phase={g.phase} game_end={g.game_end!r}")
            return self._finish(app)
        self.facts.append(f"结算面板：标题={g.endTitle.text()!r} 结果={g.endAlert.label.text()!r} "
                          f"面板可见={g.endPanel.isVisible()}")
        if not g.endTitle.text().strip() or not g.endAlert.label.text().strip():
            self.problems.append("结算面板上的字是空的")
        _capture(sh, "smoke_game_end")
        self._finish(app)

    def _finish(self, app=None) -> None:
        if self._done:
            return
        self._done = True
        if app is None:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
        _wait(app, lambda: False, 0.3)          # 拿"永不成立"的条件当可控等待：让最后一帧真画完
        say(f"  [{self.tag}]")
        for f in self.facts:
            say(f"    · {f}")
        for p in self.problems:
            say(f"    × {p}")
        app.quit()


def _load_entry():
    """把 `desktop/app.py` 以**别的模块名**加载进来。

    不能 `import app`：后端的顶层包也叫 `app/`，uvicorn 要导 `app.main:app`；
    这个名字一旦被入口模块占掉，后端就报 `'app' is not a package` 起不来。
    真用户是 `python desktop/app.py`，入口名是 `__main__`，碰不到这个坑；
    验收脚本要直接调 `main()`，就必须换个名字。这条约束已写进 app.py 的 docstring。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("desktop_entry", DESKTOP / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_pass(tag: str, autologin: bool, game: bool = False) -> int:
    # `global`：`_observe()` 里要读 `app_mod.shell_mod.chrome_mod.APP_TITLE` 来对标题，
    # 而它在函数里只是个局部名 —— 第 23 轮（更名弈道）加那条标题断言时漏了这一步，
    # 于是这个验收脚本**从那时起每次都自己抛 NameError**、三趟全红（`_observe` 的
    # `except` 会把它记成「验收脚本自己抛了」）。第 32 轮跑真窗口验收时才撞出来。
    global app_mod
    app_mod = _load_entry()
    # 用一根**本趟专用**的管道名走完整的单实例链（acquire → 接线 → release），
    # 而不是关掉这条链：关掉它就会留一个「测试里永远不走」的分支，而它恰好是
    # 「双击图标没反应」这类报告的现场。名字带 pid 是因为真客户端可能正在开发者
    # 机器上跑着 —— 那时第二趟该照常起窗口，而不是弹一个模式框把验收挂死。
    gate = app_mod.si.Gate(f"Yidao.smoke-{os.getpid()}")
    acc = Acceptance(autologin, tag, gate, game=game)
    rc = app_mod.main(on_shell_ready=acc.on_shell, gate=gate)
    say(f"  退出码 {rc}，问题 {len(acc.problems)} 条，"
        f"本进程被唤起请求命中 {gate.wake_requests} 次")
    return 1 if acc.problems else 0


# ---------------------------------------------------------------------------
# 第 4 趟：启动器（玩家入口）—— 登录 → 进入弈道 → 真客户端子进程起来
# ---------------------------------------------------------------------------

def _load_launcher_entry():
    """把 `launcher/gui.py` 按路径加载成 `launcher_entry`（与 `_load_entry` 同由）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("launcher_entry",
                                                  DESKTOP.parent / "launcher" / "gui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherPass:
    """第 4 趟：真窗口跑玩家版启动器，真注册，点「进入弈道」，验真客户端起来了。

    与前三趟的分工：那三趟从 `app.main()`（客户端入口）出发；本趟从玩家双击 bat 的
    那条链出发 —— 启动器窗口自己起内嵌后端、自己登录、自己把客户端拉起来。
    客户端是**真子进程**（生产那份 pythonw + desktop/app.py），本趟只多两件事：
    记下它的 PID 好在收尾时杀干净、以及把它的日志目录指到临时区好读证据。
    """

    def __init__(self, tag: str):
        self.tag = tag
        self.problems: list[str] = []
        self.facts: list[str] = []
        self.proc = None
        self._done = False

    # ---- 驱动

    def _spawn(self) -> bool:
        """生产同款命令（pythonw + desktop/app.py），只为拿得到 PID 而自己 spawn。"""
        pythonw = DESKTOP / ".venv" / "Scripts" / "pythonw.exe"
        if not pythonw.exists():
            self.problems.append("项目 venv 里没有 pythonw.exe")
            return False
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [str(pythonw), str(DESKTOP / "app.py")], cwd=str(DESKTOP), env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True

    def run(self) -> int:
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        from core import paths, settings as settings_mod
        from ui import theme

        global launcher_mod
        launcher_mod = _load_launcher_entry()
        theme.configure_hi_dpi()
        app = QApplication.instance() or QApplication(sys.argv)
        theme.apply_app_font(app)
        app.setStyleSheet(theme.QSS)

        prefs = settings_mod.Prefs(os.environ.get("GO_CLIENT_INI") or None)
        win = launcher_mod.MainWindow(prefs=prefs)
        win._spawn = self._spawn                 # 只为拿 PID；命令与生产一字不差
        win.show()
        win.start_backend()

        def finish():
            if self._done:
                return
            self._done = True
            say(f"  [{self.tag}]")
            for f in self.facts:
                say(f"    · {f}")
            for p in self.problems:
                say(f"    × {p}")
            if self.proc is not None and self.proc.poll() is None:
                # 真窗口留在桌面上会碍事，也会让下一趟的单实例/端口互相干扰
                _kill_tree(self.proc)
            app.quit()

        def timeout():
            if not self._done:
                self.problems.append("90 秒没走完启动器验收，强制退出")
                finish()

        QTimer.singleShot(90_000, timeout)

        def drive():
            try:
                self._observe(app, win, prefs, QTest)
            except Exception as exc:  # noqa: BLE001  验收脚本自己出错也要有结论
                import traceback
                self.problems.append(f"验收脚本自己抛了：{exc!r}")
                self.facts.append(traceback.format_exc(limit=4))
            finish()

        QTimer.singleShot(300, drive)
        return app.exec()

    # ---- 观察

    def _observe(self, app, win, prefs, QTest):
        from PySide6.QtCore import Qt

        from core import paths

        if not _wait(app, lambda: win.backend_ready, 90.0):
            self.problems.append("启动器的内嵌后端没就绪")
            return
        self.facts.append(f"窗口可见={win.isVisible()} 尺寸={win.width()}x{win.height()} "
                          f"DPR={win.devicePixelRatioF()}")
        if not win.isVisible():
            self.problems.append("启动器窗口没真的显示出来")
        _capture(win, "smoke_launcher_login")
        if not win.edUser.isVisible():
            self.problems.append("启动器没停在新装的登录表单上（临时 ini 应当是空的）")

        # 真注册（真窗口里敲字、真点按钮）
        name = "smk" + uuid.uuid4().hex[:8]
        QTest.keyClicks(win.edUser, name)
        QTest.keyClicks(win.edPw, PASSWORD)
        QTest.mouseClick(win.btnRegister, Qt.MouseButton.LeftButton)
        if not _wait(app, lambda: win.btnEnter.isVisible(), 60.0):
            self.problems.append(f"启动器注册后没进欢迎态：{win.loginError.text()!r}")
            return
        self.facts.append(f"启动器已登录：{win.welcome.text()!r}")
        if win.welcome.text() != f"欢迎回来，{name}（18级）":
            self.problems.append(f"欢迎语不对：{win.welcome.text()!r}")
        if not prefs.token:
            self.problems.append("登录没把令牌写进共享 ini（客户端将无法免登录）")
        # 等真状态到达再断言：徽章停在「启动中…」说明状态轮没拿到回包；
        # 而「未检测到」会把一台装着 KataGo 的机器说成没装（第 34 轮修过一次解析错）。
        _wait(app, lambda: not win.engineBadge.text().startswith("启动中"), 20.0)
        self.facts.append(f"引擎徽章={win.engineBadge.text()!r} "
                          f"账号行={win.lblStatus.text()!r}")
        if win.engineBadge.text().startswith("启动中") or "未知" in win.engineBadge.text():
            self.problems.append(f"引擎状态没读到：{win.engineBadge.text()!r}")
        _capture(win, "smoke_launcher_ready")

        # 进入弈道：真子进程客户端
        logs = Path(os.environ["GO_CLIENT_LOG_DIR"])
        QTest.mouseClick(win.btnEnter, Qt.MouseButton.LeftButton)
        if not _wait(app, lambda: self.proc is not None, 30.0):
            self.problems.append("点了「进入弈道」没有拉起客户端进程")
            return
        client_log = logs / "client.log"
        ok = _wait(app, lambda: client_log.exists()
                   and "主窗口已出现" in client_log.read_text(encoding="utf-8",
                                                              errors="replace"), 120.0)
        if not ok:
            tail = ""
            if client_log.exists():
                tail = client_log.read_text(encoding="utf-8", errors="replace")[-400:]
            self.problems.append(f"客户端没在 120 秒内开出主窗口，日志尾部：{tail}")
            return
        self.facts.append(f"客户端子进程 PID={self.proc.pid} 已开出主窗口")
        if not _wait(app, lambda: not win.isVisible(), 20.0):
            self.problems.append("进入弈道后启动器窗口没有自己退出")
        self.facts.append(f"启动器已退出={not win.isVisible()}；"
                          f"共享 ini 令牌长度={len(prefs.token)}")


def _kill_tree(proc) -> None:
    """杀进程树：`.venv\\pythonw.exe` 是重定向器，只 terminate 壳子会漏掉真客户端。"""
    if os.name != "nt":
        proc.kill()
        return
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True, check=False)


def run_launcher_pass(tag: str) -> int:
    acc = LauncherPass(tag)
    rc = acc.run()
    say(f"  退出码 {rc}，问题 {len(acc.problems)} 条")
    return 1 if acc.problems else 0


def main() -> int:
    global REPORT
    import tempfile
    if REPORT is None:                      # 总控进程：先定好报告文件并清空，别接上一次的尾巴
        REPORT = DESKTOP / "artifacts" / "smoke_latest.txt"
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("", encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="go-p1-smoke-"))
    env = {
        "GO_DATA_DIR": str(tmp / "data"),
        "GO_CLIENT_INI": str(tmp / "client.ini"),
        "GO_KATAGO_ENABLED": "false",
        "PYTHONIOENCODING": "utf-8",
        "GO_SMOKE_REPORT": str(REPORT),       # 各趟子进程共用同一份：结论按顺序拼在一起
        # 第 4 趟的启动器会派一个真客户端子进程；日志目录指到临时区，读得到证据、
        # 又不往开发者真实的 logs/ 里写。
        "GO_CLIENT_LOG_DIR": str(tmp / "client-logs"),
    }
    say(f"临时数据目录 {tmp}")
    here = str(DESKTOP / "scripts" / "smoke_launch.py")
    total = 0
    for tag, autologin, game, launcher in (
            ("第 1 趟：注册登录", False, False, False),
            ("第 2 趟：免登录", True, False, False),
            ("第 3 趟：真窗口下开一局", True, True, False),
            ("第 4 趟：启动器登录 → 进入弈道", True, False, True)):
        say(f"== {tag} ==")
        which = ("launcher" if launcher else
                 "game" if game else ("auto" if autologin else "login"))
        penv = dict(env)
        if launcher:
            # 启动器那一趟要**自己那份空 ini**：共用前三趟的会让它直接免登录，
            # 「注册 → 进欢迎态 → 进入弈道」那条玩家主路径就走不到了
            # （首跑就是这样：它欢迎的是前三趟那个账号，注册一步被静默跳过）。
            penv["GO_CLIENT_INI"] = str(tmp / "launcher-client.ini")
            penv["GO_CLIENT_LOG_DIR"] = str(tmp / "launcher-client-logs")
        r = subprocess.run([sys.executable, here, "--pass", which],
                           env={**os.environ, **penv},
                           timeout=260 if (game or launcher) else 150)
        total += r.returncode
    say("\n== 结论 ==")
    say("启动链真窗口验收：" + ("通过（四趟都无问题）" if total == 0 else f"未通过（{total} 趟有红）"))
    say(f"结论文件：{REPORT}")
    say(f"令牌文件：{env['GO_CLIENT_INI']}")
    if Path(env["GO_CLIENT_INI"]).exists():
        say("内容：\n" + Path(env["GO_CLIENT_INI"]).read_text(encoding="utf-8").strip())
    return 1 if total else 0


if __name__ == "__main__":
    if "--pass" in sys.argv:
        which = sys.argv[sys.argv.index("--pass") + 1]
        if which == "game":
            raise SystemExit(run_pass("对局页真下一手", True, game=True))
        if which == "launcher":
            raise SystemExit(run_launcher_pass("启动器登录→进入弈道"))
        raise SystemExit(run_pass("免登录" if which == "auto" else "注册登录", which == "auto"))
    raise SystemExit(main())

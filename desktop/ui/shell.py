"""主窗口外壳：左侧导航 + 页面栈 + 后端看门狗。

对应 frontend/src/App.tsx 的路由壳。原生端没有 URL，所以"切页"就是换
QStackedWidget 的当前页；导航项与网页版一一对应，将来加页面只动 NAV 这张表。

后端由这里持有：`host` 已经在别的线程跑起来了，本类只负责
  · 把 base_url 与 token 提供给 ApiClient（都是可调用，端口变了不用重建）；
  · 定时看一眼后端线程，死了就自重启一次并把新端口告诉所有页面；
  · 401 时退回登录页（令牌过期或被清库）。

菜单/托盘/快捷键不在这里：那些在 `ui/chrome.py`，本文件只负责把它们摆进布局、
在切页时叫它重算一次可用性、以及提供 `go()` / `wake()` / 当前页上的按钮给它们调。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QByteArray, QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QSizePolicy, QStackedWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from core import api as api_mod
from core import crash_log
from core import settings as settings_mod
from core.sound import SoundPlayer
from ui import chrome as chrome_mod
from ui import theme
from ui.pages import game as game_page
from ui.pages import lobby, login
from ui.pages import ranks as ranks_page
from ui.pages import review as review_page
from ui.pages import settings as settings_page
from ui.pages import tsumego as tsumego_page

# (键, 标题, 交付阶段)。没交付的键显示占位页，而不是不显示 —— 导航少一半
# 会让人以为程序坏了，而"这页 P3 交付"至少说清了发生了什么。
NAV: tuple[tuple[str, str, str], ...] = (
    ("lobby", "大厅", ""),
    ("game", "对局", ""),
    ("tsumego", "死活练习", ""),
    ("ranks", "段位", ""),
    ("review", "复盘", ""),
    ("settings", "设置", ""),
)

#: 没有存过几何时用的默认窗口大小（与 `resize()` 那一条同一个数，不写两处）
DEFAULT_SIZE = (1280, 800)

#: 设计最小尺寸（低于此不保证布局，与网页版 L19 同边界）。**它是上限而不是定值**：
#: 小屏上会被 `_min_size_for_screen()` 压到屏幕装得下为止（审计 M11）。
DESIGN_MIN_SIZE = (1180, 760)
#: 压最小尺寸时给系统留的余量（任务栏 / 窗口框）。高度留得多一些：Windows 的
#: 任务栏在底部，而"底部不可达"正是 M11 报的那个症状。
MIN_MARGIN = (24, 72)


class PlaceholderPage(QWidget):
    """未交付页面的说明。"""

    def __init__(self, title: str, phase: str, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 24, 24, 24)
        t = QLabel(title, self)
        t.setProperty("role", "title")
        d = QLabel(f"这一页在计划的 {phase} 阶段交付，本轮还没有接线。", self)
        d.setProperty("role", "muted")
        v.addWidget(t)
        v.addWidget(d)
        v.addStretch(1)


class Shell(QWidget):
    """桌面端主窗口。"""

    #: 导航表。挂在类上是为了 `Chrome` 与测试都能从窗口对象拿到它（它们都不该
    #: 自己另写一份页面清单 —— 那是“菜单里有、页面上没”这类漂移的源头）。
    NAV = NAV

    #: 后端自重启换了端口时发出来，让 WS 等长连接自己重连
    baseUrlChanged = Signal(str)
    #: 当前页变了。菜单项的可用性全跟这个走（`Ctrl+S` 只在该页上才有意义）
    pageChanged = Signal(str)
    #: 后台线程里的后端重启结果（成功带新 base_url，失败带错误文本）。
    #: 必须是信号：`restart()` 会 `_wait_ready` 阻塞轮询，跑在主线程就是最长
    #: 180 秒的窗口冻结（审计 1.5）。
    backendRecovered = Signal(str)
    backendRecoverFailed = Signal(str)
    #: 一条新异常落盘（可能在任意线程触发，见 `_on_crash`）
    crashRecorded = Signal(str)

    def __init__(self, host, prefs: settings_mod.Prefs | None = None, parent=None,
                 tray_available: bool | None = None):
        super().__init__(parent)
        self._host = host
        self._prefs = prefs or settings_mod.Prefs()
        self._api = api_mod.ApiClient(lambda: self._base_url, lambda: self._prefs.token)
        self._api.unauthorized.connect(self._on_unauthorized)
        self._sound = SoundPlayer(self._prefs)
        self._pages: dict[str, QWidget] = {}
        self._restarting = False
        self._progress: dict = {}
        #: 窗口尺寸是从偏好里恢复的还是默认的（测试要能看出走了哪条分支）
        self.geometry_source = "default"

        self.setWindowTitle(chrome_mod.APP_TITLE)
        # 最小尺寸按屏幕算（审计 M11）：写死 1180x760 在 1366x768 笔记本与
        # 1080p@150% 缩放（逻辑区约 1280x720）上都装不下，底部按钮不可达。
        self.setMinimumSize(*self._min_size_for_screen())
        self.resize(*self._fit_size(*DEFAULT_SIZE))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget(self)
        root.addWidget(self._stack)

        # 0 号是登录页，1 号是主界面；先放登录页，boot() 决定要不要立刻切过去
        self._login = login.LoginPage(self._api, self._prefs, self)
        self._login.loggedIn.connect(self._enter)
        self._stack.addWidget(self._login)
        self._main = self._build_main()
        self._stack.addWidget(self._main)
        self._stack.setCurrentIndex(0)

        self.status = QLabel("", self)
        self.status.setProperty("role", "muted")
        self.status.setContentsMargins(14, 6, 14, 8)
        root.addWidget(self.status)

        # 没有菜单栏（2026-09-08 撤掉，口径写在 chrome.py 顶部）：Chrome 只负责建动作、
        # 把动作挂到窗口上（快捷键）与托盘；动作的可见入口在设置页的「应用」卡片。
        self.chrome = chrome_mod.Chrome(self, self._prefs, self, tray_available)
        # 一律连绑定方法（临时闭包会被静默丢投递，见 api.Reply 的说明）
        self.pageChanged.connect(self._on_page_changed)
        self.backendRecovered.connect(self._on_backend_recovered)
        self.backendRecoverFailed.connect(self._on_backend_recovered)
        self.crashRecorded.connect(self._show_crash)

        # 后端线程死了要能自己爬起来：5 秒看一眼，只重启一次（反复重启会掩盖真问题）
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(5000)
        self._watchdog.timeout.connect(self._check_backend)
        self._watchdog.start()

        self._restore_geometry()
        # 异常落盘之外还要让人当场看见（不然就是“按钮没反应”而没人知道为什么）。
        # 接在窗口上而不是接在 install() 上：启动器/测试可以先装日志后开窗，
        # 顺序不能要求（`install()` 时连 shell 都还没建）。
        crash_log.set_notifier(self._on_crash)

    # ---------------------------------------------------------------- 装配

    def _build_main(self) -> QWidget:
        wrap = QWidget(self)
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        rail = QWidget(wrap)
        rail.setObjectName("navRail")
        rail.setStyleSheet(f"QWidget#navRail {{ background: {theme.PANEL};"
                           f" border-right: 1px solid {theme.LINE}; }}")
        rail.setFixedWidth(168)
        rv = QVBoxLayout(rail)
        rv.setContentsMargins(10, 14, 10, 14)
        rv.setSpacing(4)
        # 导航栏顶部不放品牌字：窗口标题已经是「弈道」，这里再来一行「围棋教学」
        # 既重复又俗（2026-09-08 用户点名删掉）。段位徽章直接顶到最上面。
        # 段位徽章常驻导航：它是“我这局赢了没升级”最直接的回答，
        # 放大厅里只在看得到大厅时有效，对局页上正需要它（网页版放在顶栏，同一个理由）
        self.rankBadge = QLabel("", rail)
        self.rankBadge.setProperty("role", "badge")
        self.rankBadge.setAlignment(Qt.AlignCenter)
        self.rankBadge.setWordWrap(True)
        self.rankBadge.setVisible(False)
        rv.addWidget(self.rankBadge)
        rv.addSpacing(8)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for i, (key, title, phase) in enumerate(NAV):
            btn = QToolButton(rail)
            btn.setText(title if not phase else f"{title} ·{phase}")
            btn.setCheckable(True)
            btn.setAutoRaise(True)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setMinimumHeight(32)
            btn.clicked.connect(lambda _=False, k=key: self.go(k))
            self._group.addButton(btn, i)
            rv.addWidget(btn)
        rv.addStretch(1)

        self._content = QStackedWidget(wrap)
        h.addWidget(rail)
        h.addWidget(self._content, 1)
        return wrap

    def _ensure_page(self, key: str, phase: str) -> QWidget:
        page = self._pages.get(key)
        if page is not None:
            return page
        if key == "lobby":
            page = lobby.LobbyPage(self._api, self)
            # 一律连绑定方法：连到临时闭包上会被静默丢投递（见 api.Reply 文档）
            page.openRequested.connect(self._on_open_game)
            page.reviewRequested.connect(self._on_review_requested)
            page.gameStarted.connect(self._on_open_game)
        elif key == "game":
            page = game_page.GamePage(self._api, self._sound, self)
            page.rankChanged.connect(self.set_progress)
            page.backRequested.connect(self._on_back_to_lobby)
            page.reviewRequested.connect(self._on_review_requested)
        elif key == "tsumego":
            # 音效与对局页共用一个播放器：一次跑程里“该出声的音”记在同一本账上，
            # P3 验收要断言的就是这本账（见 SoundPlayer.played）
            page = tsumego_page.TsumegoPage(self._api, self._sound, self,
                                            prefs=self._prefs)
        elif key == "ranks":
            page = ranks_page.RanksPage(self._api, self)
        elif key == "review":
            page = review_page.ReviewPage(self._api, self._sound, self)
            # 这一行不能省：页面自己发 `backRequested`，只有外壳知道该切去哪。
            # 漏接的症状很轻：「返回大厅」看着能点、点下去纹丝不动（e2e 撞上过一次）。
            page.backRequested.connect(self._on_back_to_lobby)
        elif key == "settings":
            # 设置页要同时摸三样东西：接口（引擎/LLM/偏好）、本机偏好（音量）与
            # 播放器（试听）。只给它 api 的话，音量那半就会去新开一份 Prefs ——
            # 两份账，改了滑杆对局里却不出声。
            page = settings_page.SettingsPage(self._api, self._sound, self._prefs, self,
                                              chrome=self.chrome)
        else:
            title = dict((k, t) for k, t, _p in NAV)[key]
            page = PlaceholderPage(title, phase, self)
        self._pages[key] = page
        self._content.addWidget(page)
        return page

    # ---------------------------------------------------------------- 页面跳转

    def _on_open_game(self, game_id: str = "") -> None:
        """开新局与“回到那一局”走同一条路：切页 + 让对局页接上去。"""
        if not game_id:
            self.status.setText("没有拿到对局编号，回不了棋盘。")
            return
        self.go("game")
        page = self._pages["game"]
        page.open_game(game_id)
        self.status.setText("")

    def _on_back_to_lobby(self) -> None:
        self.go("lobby")

    def _on_review_requested(self, game_id: str) -> None:
        """对局页的「生成 AI 复盘」：切过去并让复盘页接上这一局。

        没终局的局也能进 —— 后端会回 `reviewStatus='none'`，那一页给的是
        「暂无复盘报告 + 立即生成」，比在按钮上禁掉再让人猜为什么不能点清楚。
        """
        if not game_id:
            self.status.setText("没有拿到对局编号，进不了复盘页。")
            return
        self.go("review")
        page = self._pages["review"]
        page.open_game(game_id)
        self.status.setText("")

    # ---------------------------------------------------------------- 段位徽章

    def set_progress(self, progress) -> None:
        p = progress or {}
        self._progress = dict(p)
        name = p.get("rankName") or ""
        if not name:
            self.rankBadge.setVisible(False)
            return
        self.rankBadge.setText(f"{name}　{p.get('rankWins', 0)}/{p.get('winsRequired', 0)} 胜")
        self.rankBadge.setStyleSheet(theme.badge_style("promo" if p.get("inPromotion")
                                                      else "rank"))
        self.rankBadge.setVisible(True)

    # ---------------------------------------------------------------- 导航

    def boot(self) -> None:
        """有旧令牌就先试着免登录进去；不行就停在登录页。"""
        if not self._prefs.token:
            self.status.setText("请先登录。")
            self._login.user.setFocus()
            return
        reply = self._api.get("/api/auth/me")
        reply.finished.connect(self._on_boot_reply)

    def _on_boot_reply(self, data, err) -> None:
        if err is not None or not data or not data.get("user"):
            self._prefs.token = ""
            self._prefs.sync()
            self.status.setText("上次的登录已失效，请重新登录。")
            return
        self._enter(data["user"])

    def _enter(self, user) -> None:
        self._stack.setCurrentIndex(1)
        # 登录成功后得把 boot 留下的“请先登录。”擦掉：状态栏只有一条，
        # 不清就会在已经进去了的界面上挂着“请先登录。”—— 看截图才发现的。
        self.status.setText("")
        # 标题只留应用名：登录名是「谁在用」，不是窗口该报的事（它在设置页右上角，
        # 半透明显示，见 settings.py 的 `lblUser`）。
        self.setWindowTitle(chrome_mod.APP_TITLE)
        self.set_progress(user.get("progress"))
        self.go("lobby")

    def _on_unauthorized(self) -> None:
        """令牌过期 / 被清库：送回登录页，而不是让每个页面各自卡在“读取失败”。

        停在登录页时不响应 —— 那里的 401 是“密码错了”，不是“令牌没了”；
        跟着走一次会把刚写好的错误文案和输入框一起清掉。
        多个请求同时 401 会连发好几次，靠同一个判断自然幂等。
        """
        if self._stack.currentIndex() == 0:
            return
        # 先收掉每个页面的长连接与定时器，再回登录页（审计 M4）：退回登录只是换了
        # 一屏，隐藏页面上的对局 WS、复盘轮询、死活页"答对自动下一题"、上下钟 tick
        # 全都还在跑 —— 令牌已经失效，它们只会持续 401，还会在用户重新登录后
        # 悄悄把旧请求的响应画进新会话。
        self._shutdown_pages()
        self._prefs.token = ""
        self._prefs.sync()
        self._stack.setCurrentIndex(0)
        self.status.setText("登录已失效，请重新登录。")
        self._login.pw.clear()
        self._login.user.setFocus()

    def _shutdown_pages(self) -> None:
        """叫每个页面收掉自己的长连接/定时器（没有 `shutdown` 的页面跳过）。"""
        for page in self._pages.values():
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                shutdown()

    def go(self, key: str) -> None:
        for i, (k, title, phase) in enumerate(NAV):
            if k == key:
                self._ensure_page(k, phase)
                self._content.setCurrentWidget(self._pages[k])
                btn = self._group.button(i)
                if btn is not None:
                    btn.setChecked(True)
                page = self._pages[k]
                if hasattr(page, "refresh"):
                    page.refresh()
                # 可用性重算放在 refresh 之后：refresh 会当场改一些按钮的 enabled
                # （比如“没有进行中就禁掉开局”），先 emit 就会拿到一个旧状态。
                self.pageChanged.emit(k)
                return

    def _on_page_changed(self, _key: str) -> None:
        self.chrome.sync()

    def current_page(self) -> QWidget:
        """当前真正显示给用户的页面（登录态下就是登录页）。"""
        if self._stack.currentIndex() == 0:
            return self._login
        return self._content.currentWidget() or self._main

    def content_size(self) -> QSize:
        """页面可用的视口尺寸：无滚动条断言要用它和页面的 sizeHint 比。"""
        return self._content.size()

    # ---------------------------------------------------------------- 对外供取
    # `Chrome` 与测试需要读这三样，但不该去碰私有字段（碰了就只能靠「今天恰好
    # 叫这个名字」，改个名就是静默坏掉）。

    @property
    def prefs(self) -> settings_mod.Prefs:
        return self._prefs

    @property
    def pages(self) -> dict:
        return self._pages

    @property
    def base_url(self) -> str:
        """当前服务地址。端口还没定下来就返回空串（不是 `:0`）—— “关于”里
        拿它判「本地服务：还没起来」，拿一个假地址回去就会把谎话发给用户。"""
        return self._base_url if getattr(self._host, "port", None) else ""

    # ---------------------------------------------------------------- 托盘与窗口

    def wake(self) -> None:
        """把窗口拿回来（托盘点击、二次启动都走这里）。

        先 `show()` 再 `showNormal()`：从托盘隐下去的窗口是完全 hidden 的，
        而 `showNormal()` 的语义是「从最小化恢复」；两步都跑一次是幂等的
        （本来就正常显示时什么也不变）。

        诚实边界：Windows 会拦「从后台抢焦点」（前台锁定策略），所以不保证真拿到
        键盘焦点 —— 那是系统行为。本机（offscreen）能验的是「从 hidden 变回可见」
        这一段，它在 `tests/test_global_flow.py` 里钉着。
        """
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.status.setText("")

    def changeEvent(self, ev) -> None:                          # noqa: N802
        """「最小化时收进托盘」：在状态已经变成 minimized 之后把它藏起来。

        只能在 `changeEvent` 里做：`showMinimized()` 没有可以拦下来的信号，
        而 Win+D / 点任务栏都只会改窗口状态。
        用 `QTimer.singleShot(0, hide)` 而不是当场 `hide()`：这个函数自己还在
        处理一次状态变化的途中，在它的调用栈里重入窗口可见性是 Qt 里最容易
        留下「状态与显示不一致」的地方，延一轮是标准做法。
        """
        super().changeEvent(ev)
        if ev.type() != QEvent.Type.WindowStateChange:
            return
        # `chrome` 在构造函数后半段才挂上，而 `changeEvent` 从 `super().__init__`
        # 那一刻就会开始跑（任何一次窗口状态变化都会进来）。
        chrome = getattr(self, "chrome", None)
        if chrome is None:
            return
        if not self.isMinimized() or not chrome.should_hide_on_minimize():
            return
        QTimer.singleShot(0, self.hide)

    def _restore_geometry(self) -> None:
        """开机恢复上次的窗口位置与尺寸。三条都必须有：

        ① 存回来的形态**不是一种**：注册表后端给 `QByteArray`，INI 后端按实测
          也给 `QByteArray`（文件里写的是 `geometry=@ByteArray(\\x1\\xd9...)` 这种
          base64 文本）。两种都得吃下 —— 丢错类型给 `restoreGeometry` 只会静默
          返 False，症状是「窗口默默回到默认尺寸，谁也不发现」；
        ② 保存的几何可能在拔了副屏之后完全跑到屏幕外面 —— 症状是
          「双击图标没反应」（其实开了，只是看不见），所以要用可用区域验一下；
        ③ 验不过就回到默认尺寸，而不是拿着一个坏值继续跑。
        """
        raw = self._prefs.window_geometry()
        if not raw:
            return
        data = raw if isinstance(raw, QByteArray) else QByteArray.fromBase64(
            str(raw).encode("ascii", "ignore"))
        if not data.size() or not self.restoreGeometry(data):
            self.geometry_source = "default"
            return
        rect = self.geometry()
        rects = self._screen_rects()
        on_screen = not rects or any(r.intersects(rect) for r in rects)
        if not on_screen:
            self.setGeometry(80, 60, *self._fit_size(*DEFAULT_SIZE))
            self.geometry_source = "offscreen"
            return
        # 屏幕上（可能只是**沾到**一点点）：尺寸还得夹进可用区域 —— 存窗口时用
        # 大屏、恢复时接小屏（拔掉副屏/换显示器）是最常见的一档，几何"intersects"
        # 只保证能看到一角，底部照样够不着（审计 M11）。
        fit = self._fit_size(rect.width(), rect.height())
        if fit != (rect.width(), rect.height()):
            self.resize(*fit)
            rect = self.geometry()
            if not any(r.intersects(rect) for r in rects):
                self.move(max(0, min(rect.x(), rects[0].right() - rect.width())),
                          max(0, min(rect.y(), rects[0].bottom() - rect.height())))
        self.geometry_source = "restored"

    # ---------------------------------------------------------------- 尺寸/屏幕

    def _available(self):
        """主屏的可用区域（已扣掉任务栏）；拿不到就是 None（无头环境）。"""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            rects = self._screen_rects()
            return rects[0] if rects else None
        return screen.availableGeometry()

    def _min_size_for_screen(self) -> tuple[int, int]:
        """窗口最小尺寸，封顶在"屏幕装得下"那一档。

        设计值是 `DESIGN_MIN_SIZE`（1180x760），屏幕比它大时原样返回；比它小时
        按可用区域减去余量。下限兜在 (640, 520)，免得极端小屏上把窗口压成一个
        没法用的条（审计 M11）。
        """
        width, height = DESIGN_MIN_SIZE
        avail = self._available()
        if avail is None:
            return width, height
        return (min(width, max(640, avail.width() - MIN_MARGIN[0])),
                min(height, max(520, avail.height() - MIN_MARGIN[1])))

    def _fit_size(self, width: int, height: int) -> tuple[int, int]:
        """把请求的窗口尺寸夹进可用区域，但**不缩到最小尺寸以下**。

        与 `_min_size_for_screen` 是一对：一个定下限、一个把实际尺寸拉回下限之上，
        合起来保证「窗口既装得下、又不小于布局能画的尺寸」。
        """
        avail = self._available()
        if avail is None:
            return width, height
        return (max(self.minimumWidth(), min(width, avail.width())),
                max(self.minimumHeight(), min(height, avail.height())))

    def _screen_rects(self) -> list:
        """当前所有屏幕的可用区域。

        抽一个方法是为了能注入：实测 `restoreGeometry` 自己就会把跑到屏幕外的
        窗口夹回屏幕内（单屏机器上这条守卫永远不会被真几何触发），
        而多屏拔掉副屏那个场景必须得有代码挡着 —— 挡着的东西得能被测到。
        """
        return [s.availableGeometry() for s in QGuiApplication.screens()]

    def _save_geometry(self) -> None:
        self._prefs.save_window(self.saveGeometry(), QByteArray())
        self._prefs.sync()

    def _on_crash(self, summary: str) -> None:
        """一条新异常落盘后的通知（可能在**任意线程**被调）。

        只做一次信号发射：`crash_log._record` 是在抛出异常的那个线程里回调的，
        直接在这里写 QLabel 属于跨线程碰控件（未定义行为，审计 M10）。
        Qt 信号跨线程是队列投递，槽在窗口所属线程执行。
        """
        self.crashRecorded.emit(summary)

    def _show_crash(self, summary: str) -> None:
        """主线程槽：把刚记下的异常提示出来（限流期间不会被调，见 `crash_log` 口径 2）。"""
        self.status.setText(f"刚记下一次内部异常（不影响继续用）：{summary[:120]}")

    # ---------------------------------------------------------------- 后端

    @property
    def _base_url(self) -> str:
        return self._host.base_url if getattr(self._host, "port", None) else "http://127.0.0.1:0"

    def _check_backend(self) -> None:
        """看门狗槽（主线程，每 5 秒）。

        **只做检测**：重启必须搬进后台线程。`BackendHost.restart` 走
        `start(attempts=3)` → `_wait_ready` 用 `time.sleep(0.05)` 阻塞轮询最多
        3×60 秒；在 GUI 主线程里跑就是「后端一崩，整个窗口最长 180 秒不重绘、
        按钮无响应」，Windows 直接把标题标成「无响应」—— 这违反项目在
        `app.py` 给自己立的「启动不放主线程」规矩，看门狗这条此前漏了同一刀。
        """
        if self._host.alive or self._restarting:
            return
        self._restarting = True
        self.status.setText("遇到问题，正在自动修复…")
        threading.Thread(target=self._restart_worker, name="go-backend-restart",
                         daemon=True).start()

    def _restart_worker(self) -> None:
        """后台线程：重启内嵌后端。结果只通过信号回主线程。"""
        try:
            url = self._host.restart(timeout=60.0)
        except Exception as exc:  # noqa: BLE001  重启失败只提示，不让窗口跟着崩
            self.backendRecoverFailed.emit(str(exc))
            return
        self.backendRecovered.emit(url)

    def _on_backend_recovered(self, payload: str) -> None:
        """主线程槽：重启结束（成功给了新 base_url，失败给的是错误文本）。"""
        self._restarting = False
        if not payload.startswith("http"):
            self.status.setText(f"自动修复失败：{payload}")
            self._watchdog.stop()
            return
        self.baseUrlChanged.emit(payload)
        self.status.setText("已自动恢复。")
        page = self._pages.get("game")
        if isinstance(page, game_page.GamePage):
            page.on_base_url_changed()      # 长连接必须自己重接，不然会挂在旧端口上
        page = self._content.currentWidget()
        if hasattr(page, "refresh"):
            page.refresh()

    def closeEvent(self, ev):                                # noqa: N802
        """关窗前拆掉长连接与音效：留着 WS，后端的 accept 协程会一直挂在上面。"""
        for page in self._pages.values():
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                shutdown()
        self._watchdog.stop()
        self._save_geometry()
        super().closeEvent(ev)
